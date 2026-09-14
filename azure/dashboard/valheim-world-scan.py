#!/usr/bin/env python3
"""Deep, read-only scan of the Valheim world files -> /var/lib/valheim-status/world_extra.json

The collector (valheim-status-collect.py) runs this with a 60 s timeout whenever the newest
.fwl2 mtime changes, and merges the JSON into status.world.extra.

Everything here is read-only: the world directory is only ever opened for reading and the
output is written atomically (tmp + rename) into /var/lib/valheim-status.

File format notes (reverse engineered on world "Vancouver Island", save version 41):

  *.chunk   uncompressed ZDO records.  Header: uint16 version (41), uint32 record count.
            Each record is  <2..6 bytes of revision/flags> <float32 x> <float32 y>
            <float32 z> <uint32 prefab stable-hash> <property groups>, i.e. the object's
            position sits in the 12 bytes IMMEDIATELY BEFORE its prefab hash.  Verified by
            parsing a 182 byte chunk byte-exactly (2 portal_wood records, ends on EOF) and
            by the Player_tombstone records, whose own "spawnpoint" Vector3 property
            matches the position in front of the hash to within a metre.
            Property groups are <uint8 count> (<uint32 key-hash> <value>)*, keys being the
            same stable hash of the variable name ("scale", "tamed", "ownerName", ...).

  *.db2     int32 version, double netTime, then a gzip stream at byte 16.  The decompressed
            block starts with the ZoneSystem list: uint32 count, then count * (int16 zone x,
            int16 zone y), followed by two uint32 and the global keys as length-prefixed
            strings ("defeated_eikthyr" & co).  The first 81 entries of the list are a
            duplicate of the 9x9 zone block around spawn written with a +15625 bias on both
            axes; they are dropped by the range filter and the set is de-duplicated anyway.
"""

import glob
import json
import os
import struct
import time
import zlib

WORLD_DIR = "/home/valheim/data/worlds_local/Vancouver Island"
OUT = "/var/lib/valheim-status/world_extra.json"

WORLD_RADIUS = 10000.0   # metres, the playable disc
ZONE_SIZE = 64.0         # metres per zone side
GRID = 8.0               # basemap downsample for "build" points
CAP = 4000               # maximum number of basemap points
MAX_ZONE = 200           # |zone coord| sanity bound (world radius / 64 = 156)

# prefab -> basemap kind.  Only prefabs that players build/own; anything not listed is ignored.
PREFABS = {
    # --- wood ---------------------------------------------------------------
    "wood_wall": "build", "wood_wall_half": "build", "wood_wall_roof": "build",
    "wood_wall_log": "build", "wood_wall_log_4x0.5": "build",
    "wood_floor": "build", "wood_floor_1x1": "build",
    "wood_beam": "build", "wood_beam_1": "build", "wood_beam_26": "build", "wood_beam_45": "build",
    "wood_pole": "build", "wood_pole2": "build", "wood_pole_log": "build", "wood_pole_log_4": "build",
    "wood_roof": "build", "wood_roof_45": "build", "wood_roof_top": "build",
    "wood_roof_icorner": "build", "wood_roof_ocorner": "build",
    "wood_door": "build", "wood_gate": "build", "wood_stair": "build", "wood_stepladder": "build",
    "wood_fence": "build", "wood_ledge": "build", "wood_dragon": "build", "wood_stack": "build",
    "piece_sharpstakes": "build", "piece_trap_wood": "build",
    # --- stone and marble ---------------------------------------------------
    "stone_wall_1x1": "build", "stone_wall_2x1": "build", "stone_wall_4x2": "build",
    "stone_floor": "build", "stone_floor_2x2": "build", "stone_stair": "build",
    "stone_arch": "build", "stone_pile": "build", "stone_pole": "build",
    "blackmarble_floor": "build", "blackmarble_column_1": "build", "blackmarble_2x2x1": "build",
    "blackmarble_post01": "build", "piece_blackmarble_bench": "build", "piece_blackmarble_table": "build",
    # --- dark wood ----------------------------------------------------------
    "darkwood_beam": "build", "darkwood_beam4x4": "build", "darkwood_pole": "build",
    "darkwood_pole4": "build", "darkwood_roof": "build", "darkwood_roof_45": "build",
    "darkwood_arch": "build", "darkwood_decowall": "build", "darkwood_raven": "build",
    "darkwood_wolf": "build",
    # --- crafting and storage ----------------------------------------------
    "piece_workbench": "build", "forge": "build", "piece_cauldron": "build",
    "piece_artisanstation": "build", "piece_stonecutter": "build", "piece_spinningwheel": "build",
    "smelter": "build", "charcoal_kiln": "build", "blastfurnace": "build", "windmill": "build",
    "fermenter": "build", "piece_beehive": "build", "eitrrefinery": "build",
    "piece_preptable": "build", "piece_oven": "build", "piece_magetable": "build",
    "piece_bathtub": "build", "piece_cartographytable": "build",
    "piece_chest_wood": "build", "piece_chest": "build", "piece_chest_private": "build",
    "piece_chest_blackmetal": "build",
    # --- comfort and light --------------------------------------------------
    "piece_chair": "build", "piece_table": "build", "piece_throne01": "build",
    "piece_banner01": "build", "piece_maypole": "build", "itemstand": "build",
    "fire_pit": "build", "hearth": "build", "piece_groundtorch_wood": "build",
    "piece_groundtorch": "build", "piece_walltorch": "build", "piece_brazierceiling01": "build",
    "piece_dvergr_lantern": "build", "ArmorStand": "build",
    # --- the kinds the map colours separately -------------------------------
    "portal_wood": "portal",
    "bed": "bed", "piece_bed02": "bed",
    "Karve": "ship", "Raft": "ship", "VikingShip": "ship", "Longship": "ship",
    "Player_tombstone": "tomb",
    "guard_stone": "ward",
}

TAMEABLE = {"boar": "Boar", "wolf": "Wolf", "lox": "Lox", "hen": "Hen", "asksvin": "Asksvin"}


def stable_hash(s):
    """Valheim's StringExtensionMethods.GetStableHashCode."""
    a = b = 5381
    for i in range(0, len(s), 2):
        a = ((a << 5) + a ^ ord(s[i])) & 0xffffffff
        if i + 1 < len(s):
            b = ((b << 5) + b ^ ord(s[i + 1])) & 0xffffffff
    return (a + b * 1566083941) & 0xffffffff


def h4(s):
    return struct.pack("<I", stable_hash(s))


K_OWNERNAME = h4("ownerName")
K_TAMED = h4("tamed")


def newest(pattern):
    files = glob.glob(pattern)
    return max(files, key=os.path.getmtime) if files else None


def read_7bit_string(b, off, limit=64):
    """length-prefixed UTF-8 as written by BinaryWriter/ZPackage"""
    n = shift = 0
    while True:
        if off >= len(b) or shift > 21:
            return None, off
        c = b[off]
        off += 1
        n |= (c & 0x7f) << shift
        if not c & 0x80:
            break
        shift += 7
    if n == 0 or n > limit or off + n > len(b):
        return None, off
    try:
        s = b[off:off + n].decode("utf-8")
    except Exception:
        return None, off + n
    if any(ord(ch) < 32 for ch in s):
        return None, off + n
    return s, off + n


def valid_pos(x, y, z):
    return (abs(x) < 10500.0 and abs(z) < 10500.0 and -200.0 < y < 10000.0
            and x == x and y == y and z == z)


def zones_in_world():
    """number of 64 m zones whose centre lies inside the 10 km world disc"""
    n = 0
    r = int(WORLD_RADIUS / ZONE_SIZE) + 2
    rsq = WORLD_RADIUS * WORLD_RADIUS
    for i in range(-r, r + 1):
        dx = i * ZONE_SIZE
        for j in range(-r, r + 1):
            dz = j * ZONE_SIZE
            if dx * dx + dz * dz <= rsq:
                n += 1
    return n


def scan_zones(db_path, notes):
    """explored zones from the ZoneSystem list at the head of the decompressed .db2 block"""
    try:
        raw = open(db_path, "rb").read()
        i = raw.find(b"\x1f\x8b\x08", 0, 64)
        if i < 0:
            notes.append("no gzip stream in the .db2, explored zones unavailable")
            return None
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw[i:])
        if len(dec) < 8:
            return None
        count = struct.unpack_from("<I", dec, 0)[0]
        end = 4 + 4 * count
        if not (0 < count < 2000000) or end + 8 > len(dec):
            notes.append("ZoneSystem zone list did not parse (count %d), explored zones unavailable" % count)
            return None
        nkeys = struct.unpack_from("<I", dec, end + 4)[0]
        if nkeys > 20000:
            notes.append("ZoneSystem zone list did not parse (global key count %d)" % nkeys)
            return None
        pairs = struct.unpack_from("<%dh" % (2 * count), dec, 4)
        zs = set()
        for k in range(count):
            x, y = pairs[2 * k], pairs[2 * k + 1]
            if abs(x) <= MAX_ZONE and abs(y) <= MAX_ZONE:
                zs.add((x, y))
        if len(zs) * 2 < count:
            notes.append("only %d of %d zone entries looked like zone coordinates" % (len(zs), count))
        if not zs:
            return None
        total = zones_in_world()
        return {"zones_generated": len(zs), "zones_total": total,
                "pct": round(100.0 * len(zs) / total, 3)}
    except Exception as e:
        notes.append("explored zones failed: %s" % e)
        return None


def main():
    t0 = time.time()
    notes = []
    out = {"scanned_at": time.time(), "fwl_mtime": None, "explored": None, "tombstones": [],
           "wards": 0, "tamed": None, "basemap": {"extent": int(WORLD_RADIUS), "points": []},
           "notes": ""}

    fwl = newest(os.path.join(WORLD_DIR, "*_main.*.fwl2"))
    db = newest(os.path.join(WORLD_DIR, "*_main.*.db2"))
    if fwl:
        out["fwl_mtime"] = os.path.getmtime(fwl)

    # ---------------------------------------------------------------- explored zones
    if db:
        out["explored"] = scan_zones(db, notes)
    else:
        notes.append("no .db2 file found")

    # ---------------------------------------------------------------- ZDOs from the chunk files
    blob = b""
    try:
        parts = []
        for fn in sorted(os.listdir(WORLD_DIR)):
            if not fn.endswith(".chunk"):
                continue
            try:
                with open(os.path.join(WORLD_DIR, fn), "rb") as f:
                    parts.append(f.read())
            except OSError:
                pass  # the server rewrites chunk files on every save
        blob = b"".join(parts)
    except Exception as e:
        notes.append("could not read the chunk files: %s" % e)

    counts = {}
    kind_points = {"build": [], "portal": [], "bed": [], "ship": [], "tomb": [], "ward": []}
    tomb_hits = []
    suspect = 0
    if blob:
        for prefab, kind in PREFABS.items():
            needle = h4(prefab)
            i = blob.find(needle)
            n = 0
            while i >= 0:
                if i >= 12:
                    x, y, z = struct.unpack_from("<fff", blob, i - 12)
                    if valid_pos(x, y, z):
                        n += 1
                        kind_points[kind].append((x, z))
                        if kind == "tomb":
                            tomb_hits.append(i)
                    else:
                        suspect += 1
                i = blob.find(needle, i + 1)
            if n:
                counts[prefab] = n

    out["wards"] = len(kind_points["ward"])

    # ---------------------------------------------------------------- tombstone owners
    for i in tomb_hits:
        x, _y, z = struct.unpack_from("<fff", blob, i - 12)
        owner = None
        q = blob.find(K_OWNERNAME, i + 4, i + 400)
        if q >= 0:
            owner, _ = read_7bit_string(blob, q + 4, 40)
        out["tombstones"].append({"owner": owner or "unknown", "x": round(x, 1), "z": round(z, 1)})
    if tomb_hits and all(t["owner"] == "unknown" for t in out["tombstones"]):
        notes.append("no ownerName string found next to the tombstone ZDOs")

    # ---------------------------------------------------------------- tamed animals
    if blob:
        try:
            spots = []
            for key, prefab in TAMEABLE.items():
                needle = h4(prefab)
                i = blob.find(needle)
                while i >= 0:
                    if i >= 12:
                        x, y, z = struct.unpack_from("<fff", blob, i - 12)
                        if valid_pos(x, y, z):
                            spots.append((i, key))
                    i = blob.find(needle, i + 1)
            spots.sort()
            tamed = dict((k, 0) for k in TAMEABLE)
            q = blob.find(K_TAMED)
            while q >= 0:
                if q + 8 <= len(blob) and struct.unpack_from("<i", blob, q + 4)[0] == 1:
                    # attribute to the nearest creature ZDO that starts before the key
                    best = None
                    for pos, key in spots:
                        if pos < q:
                            best = (pos, key)
                        else:
                            break
                    if best and q - best[0] < 400:
                        tamed[best[1]] += 1
                q = blob.find(K_TAMED, q + 1)
            out["tamed"] = tamed
        except Exception as e:
            notes.append("tamed animals failed: %s" % e)

    # ---------------------------------------------------------------- basemap
    points = []
    for kind in ("portal", "bed", "ship", "tomb", "ward"):
        for x, z in kind_points[kind]:
            points.append([int(round(x)), int(round(z)), kind])
    grid = GRID
    builds = kind_points["build"]
    while True:
        cells = set()
        for x, z in builds:
            cells.add((int(x // grid), int(z // grid)))
        if len(cells) + len(points) <= CAP or grid >= 512:
            break
        grid *= 2
    for cx, cz in sorted(cells):
        points.append([int(round((cx + 0.5) * grid)), int(round((cz + 0.5) * grid)), "build"])
    points = points[:CAP]
    out["basemap"] = {"extent": int(WORLD_RADIUS), "grid": int(grid), "points": points}

    # ---------------------------------------------------------------- sanity checks and notes
    bad = [p for p in points if (p[0] * p[0] + p[1] * p[1]) > (WORLD_RADIUS + 500) ** 2]
    if bad:
        notes.append("%d points outside the world radius were dropped" % len(bad))
        out["basemap"]["points"] = [p for p in points if p not in bad]
    total_zdos = sum(counts.values())
    notes.insert(0, "positions read from the 12 bytes in front of each prefab hash in the .chunk ZDO "
                    "records; %d objects of %d known prefabs, %d build pieces on a %d m grid (%d cells)"
                    % (total_zdos, len(counts), len(builds), int(grid), len(cells)))
    if suspect:
        notes.append("%d hash matches had an implausible position and were ignored" % suspect)
    if out["explored"]:
        notes.append("explored = unique zones in the ZoneSystem list of the .db2, out of the %d "
                     "64 m zones inside the 10 km world" % out["explored"]["zones_total"])
    if out["tamed"] is not None and not sum(out["tamed"].values()):
        notes.append("no tamed animals found (the 'tamed' ZDO key is absent from every chunk)")
    if not counts.get("portal_wood"):
        notes.append("no portals found, the prefab list may be out of date")
    notes.append("counts: " + ", ".join("%s %d" % (k, counts[k]) for k in
                                        sorted(counts, key=lambda k: -counts[k])[:8]))
    notes.append("scan took %.2f s" % (time.time() - t0))
    out["notes"] = "; ".join(notes)

    # ---------------------------------------------------------------- write
    d = os.path.dirname(OUT)
    os.makedirs(d, exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)
    return out


if __name__ == "__main__":
    res = main()
    summary = dict(res)
    summary["basemap"] = {"extent": res["basemap"]["extent"], "points": len(res["basemap"]["points"])}
    print(json.dumps(summary, indent=1))
