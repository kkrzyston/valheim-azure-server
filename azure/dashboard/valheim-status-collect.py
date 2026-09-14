#!/usr/bin/env python3
"""valheim-status-collect.py -- gather Valheim server status into JSON for the dashboard.

Runs every minute from valheim-status.timer (as root: needs the journal and /proc).
Reads the valheim.service journal incrementally (cursor file), tracks player sessions,
queries the Steam A2S port for the live player count, samples CPU/memory/disk, and writes:

  /var/www/valheim/status.json    current state + recent events (rewritten every run)
  /var/www/valheim/history.json   player-count / cpu samples: per-minute for 24 h, per-10-min for 7 d
  /var/lib/valheim-status/        state.json, cursor, samples.jsonl (30 days), events.jsonl (kept forever, v4)

v3 additions: server settings (cmdline + .fwl2), world day/bosses/built counts (.db2/.chunk, rescanned
only when the world file changes), deaths, raids, outdated clients, newcomers, restart causes, update
history, uptime %, NIC throughput, pairwise "together" time, heat map, records, Steam news (1 h cache),
Discord counts (10 min cache). Network calls have 5 s timeouts and never abort the run.
"""
import json, os, re, socket, struct, subprocess, sys, time, shutil, glob, zlib, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, date, timedelta
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    LOCAL_TZ = None

# Read from /etc/valheim-server.env (see valheim-status.service's EnvironmentFile=). Falls back
# to generic values so the script still runs (with placeholder-looking output) if that file is
# missing, rather than crashing.
SERVER_NAME = os.environ.get("SERVER_NAME", "Valheim Server").strip() or "Valheim Server"
WORLD_NAME = os.environ.get("WORLD_NAME", "Dedicated").strip() or "Dedicated"
SERVER_ADDRESS = os.environ.get("SERVER_ADDRESS", "").strip()
DISCORD_INVITE = os.environ.get("DISCORD_INVITE", "").strip()

WEB = "/var/www/valheim"
LIB = "/var/lib/valheim-status"
UNIT = "valheim.service"
MANIFEST = "/home/valheim/server/steamapps/appmanifest_896660.acf"
WORLD_DIR = f"/home/valheim/data/worlds_local/{WORLD_NAME}"
WORLDS_LOCAL = "/home/valheim/data/worlds_local"
BACKUP_DIR = "/home/valheim/backups"
AUTOUPDATE_LOG = "/var/log/valheim-autoupdate.log"
A2S_ADDR = ("127.0.0.1", 2457)
MAX_EVENTS_IN_STATUS = 60
RETAIN_SECONDS = 30 * 86400
NIC = "eth0"
NEWS_CACHE = f"{LIB}/news.json"
DISCORD_CACHE = f"{LIB}/discord.json"
NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=892970&count=10&maxlength=300"
# DISCORD_INVITE is read from /etc/valheim-server.env above; empty means "no Discord card" --
# the fetch below is skipped entirely rather than hitting the API with a blank invite code.
DISCORD_URL = f"https://discord.com/api/v10/invites/{DISCORD_INVITE}?with_counts=true"
NET_TIMEOUT = 5
JOURNAL_EPOCH = "2026-09-10 13:40"

RAID_LABELS = {
    "army_eikthyr": "Eikthyr rallies the creatures of the forest",
    "army_theelder": "The forest is moving",
    "army_bonemass": "A foul smell from the swamp",
    "army_moder": "A cold wind blows from the mountains",
    "army_goblin": "The horde is attacking",
    "army_seekers": "They sought you out",
    "army_gjall": "What's that sound?",
    "army_charred": "The Ashlands are calling",
    "foresttrolls": "The ground is shaking",
    "blobs": "You are being hunted",
    "skeletons": "Skeleton surprise",
    "surtlings": "There's a smell of sulfur in the air",
    "wolves": "You are being hunted",
    "bats": "You stirred the cauldron",
}
BOSS_KEYS = {"eikthyr": "defeated_eikthyr", "elder": "defeated_gdking", "bonemass": "defeated_bonemass",
             "moder": "defeated_dragon", "yagluth": "defeated_goblinking", "queen": "defeated_queen", "fader": "defeated_fader"}
BOSS_ORDER = ["eikthyr", "elder", "bonemass", "moder", "yagluth", "queen", "fader"]
# raid name -> bosses that must already be defeated for it to fire
RAID_IMPLIES = {"army_theelder": ["eikthyr", "elder"], "army_bonemass": ["bonemass"], "army_moder": ["moder"],
                "army_goblin": ["yagluth"], "army_seekers": ["queen"], "army_gjall": ["queen"], "army_charred": ["fader"]}
BUILT_PREFABS = {"portals": "portal_wood", "beds": "bed", "workbenches": "piece_workbench", "chests": "piece_chest_wood",
                 "karves": "Karve", "rafts": "Raft", "longships": "VikingShip", "tombstones": "Player_tombstone"}

os.makedirs(WEB, exist_ok=True)
os.makedirs(LIB, exist_ok=True)
now = time.time()
t_run0 = now


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def sh(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=60).stdout.strip()


def local_dt(ts):
    return datetime.fromtimestamp(ts, LOCAL_TZ) if LOCAL_TZ else datetime.fromtimestamp(ts)


def local_epoch(naive_dt):
    """epoch for a naive datetime expressed in the VM's local zone"""
    if LOCAL_TZ:
        return naive_dt.replace(tzinfo=LOCAL_TZ).timestamp()
    return naive_dt.timestamp()


state = load_json(f"{LIB}/state.json", {
    "online": {},        # steamid -> {name, since, steam_name}
    "pending": [],       # [(steamid, ts)] connections waiting for a character name
    "steam_names": {},   # steamid -> profile name from "Player history entry"
    "cpu_prev": None,
    "last_update_line": "",
    "invocation": "",
})

# ---------------------------------------------------------------- service facts
active = sh("systemctl", "is-active", UNIT) == "active"
props = dict(l.split("=", 1) for l in sh("systemctl", "show", UNIT, "-p", "ActiveEnterTimestamp", "-p", "MainPID", "-p", "InvocationID", "-p", "NRestarts").splitlines() if "=" in l)
main_pid = int(props.get("MainPID") or 0)
invocation = props.get("InvocationID", "")
nrestarts = int(props.get("NRestarts") or 0)
started_at = None
if active and props.get("ActiveEnterTimestamp"):
    try:
        started_at = datetime.strptime(props["ActiveEnterTimestamp"].rsplit(" ", 1)[0], "%a %Y-%m-%d %H:%M:%S").astimezone().timestamp()
    except Exception:
        started_at = None
uptime_s = float(open("/proc/uptime").read().split()[0])
boot_time = now - uptime_s

# ---------------------------------------------------------------- server settings (game cmdline; never the password)
# These are fallbacks used only when /proc/<pid>/cmdline can't be read (process gone, permissions);
# generic values, not this deployment's specifics -- the live cmdline overrides them below.
settings = {"name": SERVER_NAME, "port": 2456, "world": WORLD_NAME, "seed": None, "max_players": 10,
            "public": False, "crossplay": False, "save_interval_s": 1800, "backups": 4, "backup_short_s": 7200,
            "backup_long_s": 43200, "modifiers": {}, "password_protected": True}
try:
    argv = [a.decode("utf-8", "replace") for a in open(f"/proc/{main_pid}/cmdline", "rb").read().split(b"\0") if a]
    i = 0
    def _vals(i):  # arguments following argv[i] up to the next flag
        j = i + 1
        while j < len(argv) and not argv[j].startswith("-"):
            j += 1
        return argv[i + 1:j], j
    while i < len(argv):
        a = argv[i]
        v, j = _vals(i)
        if a == "-name" and v:
            settings["name"] = " ".join(v)
        elif a == "-port" and v:
            settings["port"] = int(v[0])
        elif a == "-world" and v:
            settings["world"] = " ".join(v)
        elif a == "-password":
            settings["password_protected"] = bool(v and v[0])
        elif a == "-public":
            settings["public"] = bool(v) and v[0] == "1"
        elif a == "-crossplay":
            settings["crossplay"] = True
        elif a == "-saveinterval" and v:
            settings["save_interval_s"] = int(v[0])
        elif a == "-backups" and v:
            settings["backups"] = int(v[0])
        elif a == "-backupshort" and v:
            settings["backup_short_s"] = int(v[0])
        elif a == "-backuplong" and v:
            settings["backup_long_s"] = int(v[0])
        elif a == "-modifier" and len(v) >= 2:
            settings["modifiers"][v[0]] = v[1]
        i = max(j, i + 1)
except Exception:
    pass

# ---------------------------------------------------------------- journal (incremental)
events = []  # new events this run: {t, kind, text, ...}
cursor_file = f"{LIB}/cursor"
cmd = ["journalctl", "-u", UNIT, "-o", "json", "--no-pager", "--cursor-file", cursor_file]
if not os.path.exists(cursor_file):
    cmd += ["--since", JOURNAL_EPOCH]
out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout

restarted = bool(invocation and state.get("invocation") and invocation != state["invocation"])
if restarted:
    # the server restarted since last run: everyone who was online is gone
    for sid, p in list(state["online"].items()):
        events.append({"t": now, "kind": "leave", "name": p["name"], "note": "server restarted"})
    state["online"] = {}
    state["pending"] = []
state["invocation"] = invocation

re_conn = re.compile(r"Got connection SteamID (\d+)")
re_char = re.compile(r"Got character ZDOID from (.+?)\s*: (-?\d+):(\d+)")
re_close = re.compile(r"Closing socket (\d+)")
re_hist = re.compile(r"Player history entry with index \d+:\s+(.+?) \(Steam_(\d+),")
re_save = re.compile(r"World save \(5/5\) done\. Total time \[(\d+)ms\]")
re_savenum = re.compile(r"=> Save number (\d+)")
re_ver = re.compile(r"Valheim version: (\S+) \(network version (\d+)\)")
re_zdos = re.compile(r"Connections (\d+) ZDOS:(\d+)\s+sent:(\d+) recv:(\d+)")
re_wrongpw = re.compile(r"Peer (\d+) has wrong password")
re_raid = re.compile(r"Random event set:\s*(\S+)")
re_netver = re.compile(r"Network version check, their:(\d+), mine:(\d+)")
seen_steamids = state.setdefault("seen_steamids", {})


def parse_extra(msg, t):
    """death / raid / outdated events (shared by the incremental read and the one-off backfill)"""
    m = re_char.search(msg)
    if m and m.group(2) == "0" and m.group(3) == "0":
        return {"t": t, "kind": "death", "name": m.group(1).strip()}
    m = re_raid.search(msg)
    if m:
        name = m.group(1)
        return {"t": t, "kind": "raid", "name": name, "label": RAID_LABELS.get(name, name)}
    m = re_netver.search(msg)
    if m and m.group(1) != m.group(2):
        return {"t": t, "kind": "outdated", "their": int(m.group(1)), "mine": int(m.group(2))}
    return None


for line in out.splitlines():
    try:
        j = json.loads(line)
    except Exception:
        continue
    msg = j.get("MESSAGE", "")
    if not isinstance(msg, str):
        continue
    t = int(j.get("__REALTIME_TIMESTAMP", "0")) / 1e6
    m = re_hist.search(msg)
    if m:
        state["steam_names"][m.group(2)] = m.group(1).strip()
        continue
    m = re_conn.search(msg)
    if m:
        sid = m.group(1)
        state["pending"].append([sid, t])
        if sid not in state["steam_names"] and sid not in seen_steamids:
            events.append({"t": t, "kind": "newcomer", "steamid_suffix": sid[-4:]})
        seen_steamids[sid] = t
        continue
    m = re_char.search(msg)
    if m:
        name = m.group(1).strip()
        if m.group(2) == "0" and m.group(3) == "0":
            # character ZDOID reset to none = the player died; never a join
            events.append({"t": t, "kind": "death", "name": name})
            continue
        # respawn of someone already online: same name, new ZDOID
        if any(p["name"] == name for p in state["online"].values()):
            continue
        if state["pending"]:
            sid, ts = state["pending"].pop(0)
        else:
            sid, ts = f"unknown-{int(t)}", t
        state["online"][sid] = {"name": name, "since": ts, "steam_name": state["steam_names"].get(sid, "")}
        events.append({"t": t, "kind": "join", "name": name})
        continue
    m = re_close.search(msg)
    if m:
        sid = m.group(1)
        p = state["online"].pop(sid, None)
        state["pending"] = [x for x in state["pending"] if x[0] != sid]
        if p:
            events.append({"t": t, "kind": "leave", "name": p["name"], "played": int(t - p["since"])})
        continue
    m = re_wrongpw.search(msg)
    if m:
        events.append({"t": t, "kind": "denied", "name": state["steam_names"].get(m.group(1), "someone"), "text": "tried to join with the wrong password"})
        continue
    m = re_save.search(msg)
    if m:
        state["last_save"] = {"t": t, "ms": int(m.group(1))}
        continue
    m = re_savenum.search(msg)
    if m:
        state["save_number"] = int(m.group(1))
        continue
    m = re_ver.search(msg)
    if m:
        ver = re.sub(r"^l-", "", m.group(1))  # the server logs "l-1.0.12"; players know it as 1.0.12
        if state.get("version") and state["version"] != ver:
            events.append({"t": t, "kind": "update", "text": f"Game updated {state['version']} to {ver}"})
        state["version"] = ver
        state["network_version"] = int(m.group(2))
        events.append({"t": t, "kind": "start", "text": f"Server started on Valheim {ver}"})
        continue
    m = re_zdos.search(msg)
    if m:
        state["zdos"] = int(m.group(2))
        state["net"] = {"t": t, "sent": int(m.group(3)), "recv": int(m.group(4))}
        continue
    e = parse_extra(msg, t)
    if e:
        events.append(e)

# one-off backfill of death / raid / outdated events from the whole journal (the cursor had already
# passed them when these kinds were introduced)
if not state.get("backfill_v3"):
    try:
        have = set()
        try:
            with open(f"{LIB}/events.jsonl") as f:
                for l in f:
                    x = json.loads(l)
                    have.add((x["kind"], x.get("name"), int(x["t"])))
        except Exception:
            pass
        for e in events:
            have.add((e["kind"], e.get("name"), int(e["t"])))
        bf = subprocess.run(["journalctl", "-u", UNIT, "-o", "json", "--no-pager", "--since", JOURNAL_EPOCH,
                             "-g", "Random event set|ZDOID from .*: 0:0|Network version check"],
                            capture_output=True, text=True, timeout=120).stdout
        for line in bf.splitlines():
            try:
                j = json.loads(line)
                msg = j.get("MESSAGE", "")
                t = int(j.get("__REALTIME_TIMESTAMP", "0")) / 1e6
                e = parse_extra(msg, t) if isinstance(msg, str) else None
            except Exception:
                continue
            if e and (e["kind"], e.get("name"), int(e["t"])) not in have:
                events.append(e)
                have.add((e["kind"], e.get("name"), int(e["t"])))
        state["backfill_v3"] = True
    except Exception:
        pass

# drop stale pending connections (never sent a character = failed join)
state["pending"] = [x for x in state["pending"] if now - x[1] < 300]
# forget newcomer sightings after 30 days
for sid in list(seen_steamids):
    if now - seen_steamids[sid] > RETAIN_SECONDS:
        del seen_steamids[sid]

# ---------------------------------------------------------------- Steam A2S_INFO
a2s = {}
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    q = b"\xff\xff\xff\xffTSource Engine Query\x00"
    t0 = time.time()
    s.sendto(q, A2S_ADDR)
    d, _ = s.recvfrom(4096)
    if d[4:5] == b"A":
        s.sendto(q + d[5:9], A2S_ADDR)
        d, _ = s.recvfrom(4096)
    a2s["rtt_ms"] = round((time.time() - t0) * 1000, 1)
    i = 6
    strs = []
    for _ in range(4):
        e = d.index(b"\x00", i)
        strs.append(d[i:e].decode("utf-8", "replace"))
        i = e + 1
    i += 2  # appid
    a2s["players"], a2s["max_players"] = d[i], d[i + 1]
    a2s["name"] = strs[0]
    tail = d[i + 3:]
    m = re.search(rb"g=([0-9.]+),n=(\d+)", tail)
    if m:
        a2s["game_version"] = m.group(1).decode()
except Exception as e:
    a2s = {"error": str(e)}
finally:
    try:
        s.close()
    except Exception:
        pass

# reconcile: A2S is authoritative for the count
live_count = a2s.get("players")
if live_count is not None and live_count == 0 and state["online"]:
    for sid, p in list(state["online"].items()):
        events.append({"t": now, "kind": "leave", "name": p["name"], "played": int(now - p["since"])})
    state["online"] = {}

# ---------------------------------------------------------------- player ping (server -> player, ICMP)
# Players connect directly to the game port, so whoever is sending to it is a player. Each address
# is pinged from the VM; routers that drop ICMP show as no reply. Addresses never leave this machine.
#
# This used to be `tcpdump -i any -c 80 'udp and dst port 2456'` for three seconds, once a minute,
# every minute that anyone was online -- an AF_PACKET tap copying packets out of the game's hot
# receive path purely to learn a handful of addresses we already had a cheaper way to know. The
# `peers` set in the `inet valheim_meter` nftables table (see valheim-meter-nft.sh) collects the
# same addresses in the packet path itself, with a 15-minute element timeout so it prunes itself.
# Reading it is one short-lived `nft` call against an in-kernel set.
def _nft_set_elements(blob):
    """Addresses out of `nft -j list set ...`. Elements of a set with `flags timeout` come back as
    {"elem": {"val": "1.2.3.4", "timeout": 900, "expires": 812}} rather than a bare string, and an
    empty set has no "elem" key at all -- handle both shapes."""
    out = set()
    for node in blob.get("nftables", []):
        s = node.get("set") if isinstance(node, dict) else None
        if not isinstance(s, dict):
            continue
        for e in s.get("elem", []):
            if isinstance(e, dict):
                e = e.get("elem", e)
                e = e.get("val") if isinstance(e, dict) else e
            if isinstance(e, str):
                out.add(e)
    return out


def peer_ips():
    try:
        out = subprocess.run(["nft", "-j", "list", "set", "inet", "valheim_meter", "peers"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            raise RuntimeError((out.stderr or "").strip()[:200] or f"nft exit {out.returncode}")
        return _nft_set_elements(json.loads(out.stdout)) - {"127.0.0.1"}
    except Exception as e:
        # Not silent, but not once a minute forever either: losing the peer set costs the dashboard
        # its per-player ping column, which is worth a line in the journal -- and worth exactly one
        # line an hour while it stays broken. The usual cause is that valheim-egress.service (whose
        # ExecStartPre= installs the table) has not run, or something flushed the table.
        if now - state.get("peer_src_warn", 0) > 3600:
            state["peer_src_warn"] = now
            print(f"peer_ips: cannot read the nftables peers set ({e}); per-player ping is off. "
                  "Check: systemctl status valheim-egress; valheim-meter-nft.sh show", file=sys.stderr)
        return set()

def icmp_ping(ip):
    try:
        out = subprocess.run(["ping", "-c", "3", "-i", "0.2", "-W", "1", "-q", ip], capture_output=True, text=True, timeout=8).stdout
        m = re.search(r"= [\d.]+/([\d.]+)/", out)
        return round(float(m.group(1))) if m else None
    except Exception:
        return None

pings = {}
if state["online"]:
    ips = peer_ips()
    seen = state.setdefault("peer_seen", {})
    for ip in ips:
        seen.setdefault(ip, now)
    for ip in list(seen):
        if ip not in ips and now - seen[ip] > 900 and ip not in state.get("ip_of", {}).values():
            del seen[ip]
    ip_of = state.setdefault("ip_of", {})
    # forget mappings for players who left
    for sid in list(ip_of):
        if sid not in state["online"]:
            del ip_of[sid]
    # pair still-unmapped players with unclaimed addresses in the order they appeared
    unmapped = sorted([sid for sid in state["online"] if sid not in ip_of], key=lambda k: state["online"][k]["since"])
    unclaimed = sorted([ip for ip in ips if ip not in ip_of.values()], key=lambda ip: seen[ip])
    for sid, ip in zip(unmapped, unclaimed):
        ip_of[sid] = ip
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = dict(zip(ips, ex.map(icmp_ping, ips)))
    for sid, ip in ip_of.items():
        pings[sid] = results.get(ip)
else:
    state["ip_of"] = {}

# ---------------------------------------------------------------- machine
def cpu_sample():
    with open("/proc/stat") as f:
        parts = f.readline().split()[1:]
    vals = list(map(int, parts))
    idle = vals[3] + vals[4]
    return idle, sum(vals)

idle, total = cpu_sample()
cpu_pct = None
if state.get("cpu_prev"):
    pi, pt = state["cpu_prev"]
    if total > pt:
        cpu_pct = round(100 * (1 - (idle - pi) / (total - pt)), 1)
state["cpu_prev"] = [idle, total]

mem = {}
with open("/proc/meminfo") as f:
    for l in f:
        k, v = l.split(":", 1)
        mem[k] = int(v.split()[0]) * 1024
mem_total, mem_avail = mem["MemTotal"], mem["MemAvailable"]
swap_used = mem.get("SwapTotal", 0) - mem.get("SwapFree", 0)
load1 = float(open("/proc/loadavg").read().split()[0])
disk = shutil.disk_usage("/")
rss = None
if main_pid:
    try:
        with open(f"/proc/{main_pid}/status") as f:
            for l in f:
                if l.startswith("VmRSS:"):
                    rss = int(l.split()[1]) * 1024
    except Exception:
        pass

# NIC counters -> bytes/s over the last run interval
rx_bytes = tx_bytes = None
net_bps = {"rx_bps": None, "tx_bps": None}
try:
    rx_bytes = int(open(f"/sys/class/net/{NIC}/statistics/rx_bytes").read())
    tx_bytes = int(open(f"/sys/class/net/{NIC}/statistics/tx_bytes").read())
    prev = state.get("nic_prev")
    if prev and now - prev[0] > 1 and rx_bytes >= prev[1] and tx_bytes >= prev[2]:
        net_bps = {"rx_bps": round((rx_bytes - prev[1]) / (now - prev[0])), "tx_bps": round((tx_bytes - prev[2]) / (now - prev[0]))}
    state["nic_prev"] = [now, rx_bytes, tx_bytes]
except Exception:
    pass

world_bytes = 0
try:
    for root, _, files in os.walk(WORLD_DIR):
        for fn in files:
            world_bytes += os.path.getsize(os.path.join(root, fn))
except Exception:
    pass
backups = []
try:
    for fn in sorted(os.listdir(BACKUP_DIR)):
        p = os.path.join(BACKUP_DIR, fn)
        backups.append({"name": fn, "bytes": os.path.getsize(p), "t": os.path.getmtime(p)})
except Exception:
    pass

# ---------------------------------------------------------------- world file: day, bosses, built things
def stable_hash(s):
    a = b = 5381
    for i in range(0, len(s), 2):
        a = ((a << 5) + a ^ ord(s[i])) & 0xffffffff
        if i + 1 < len(s):
            b = ((b << 5) + b ^ ord(s[i + 1])) & 0xffffffff
    return (a + b * 1566083941) & 0xffffffff


def read_7bit_string(b, off):
    n = shift = 0
    while True:
        c = b[off]
        off += 1
        n |= (c & 0x7f) << shift
        if not c & 0x80:
            break
        shift += 7
    return b[off:off + n].decode("utf-8", "replace"), off + n


def newest(pattern):
    files = glob.glob(pattern)
    return max(files, key=os.path.getmtime) if files else None


def scan_world(fwl, db):
    """expensive part (~7 MB of chunk files); only when the world file changed"""
    res = {"fwl_mtime": os.path.getmtime(fwl), "scanned_at": now, "seed": None, "seed_num": None, "world_name": None,
           "modifiers": {}, "bosses": {"source": "unknown"}, "built": {}, "eventrate": None}
    try:
        b = open(fwl, "rb").read()
        off = 8  # int32 length, int32 version
        res["world_name"], off = read_7bit_string(b, off)
        res["seed"], off = read_7bit_string(b, off)
        res["seed_num"] = struct.unpack_from("<q", b, off)[0]
        m = re.search(rb"preset ([a-z_:]+)", b)
        if m:
            for part in m.group(1).decode().split(":"):
                k, _, v = part.partition("_")
                if k:
                    res["modifiers"][k] = v
        m = re.search(rb"eventrate (\d+)", b)
        if m:
            res["eventrate"] = int(m.group(1))
    except Exception:
        pass
    try:
        raw = open(db, "rb").read()
        i = raw.find(b"\x1f\x8b\x08", 0, 64)
        dec = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw[i:]) if i >= 0 else b""
        if dec:
            res["bosses"] = {"source": "keys"}
            for boss, key in BOSS_KEYS.items():
                res["bosses"][boss] = key.encode() in dec
    except Exception:
        pass
    try:
        blob = b"".join(open(os.path.join(WORLD_DIR, fn), "rb").read() for fn in os.listdir(WORLD_DIR) if fn.endswith(".chunk"))
        for k, prefab in BUILT_PREFABS.items():
            res["built"][k] = blob.count(struct.pack("<I", stable_hash(prefab)))
    except Exception:
        pass
    return res


world_day = None
fwl = None
try:
    fwl = newest(os.path.join(WORLD_DIR, "*_main.*.fwl2"))
    db = newest(os.path.join(WORLD_DIR, "*_main.*.db2"))
    if db:
        with open(db, "rb") as f:
            head = f.read(12)
        net_time = struct.unpack_from("<d", head, 4)[0]
        world_day = int(net_time // 1800) + 1
    if fwl and db and (not state.get("world_scan") or state["world_scan"].get("fwl_mtime") != os.path.getmtime(fwl)):
        state["world_scan"] = scan_world(fwl, db)
except Exception:
    pass
world_scan = state.get("world_scan") or {}
if world_scan.get("modifiers"):
    settings["modifiers"] = dict(world_scan["modifiers"], **{k: v for k, v in settings["modifiers"].items()})
for k in ("combat", "deathpenalty", "resources", "raids", "portals"):
    settings["modifiers"].setdefault(k, "default")
settings["seed"] = world_scan.get("seed")
settings["max_players"] = a2s.get("max_players", settings["max_players"])

# ---------------------------------------------------------------- deep world scan (on world file change)
world_extra_mtime = None
try:
    if fwl:
        world_extra_mtime = os.path.getmtime(fwl)
        if world_extra_mtime != state.get("world_extra_mtime") and os.path.exists("/usr/local/sbin/valheim-world-scan.py"):
            subprocess.run(["python3", "/usr/local/sbin/valheim-world-scan.py"], timeout=60, capture_output=True)
            state["world_extra_mtime"] = world_extra_mtime
except Exception:
    pass

# ---------------------------------------------------------------- merge side files
try:
    extra = load_json(f"{LIB}/world_extra.json", {})
    if extra:
        world_scan["extra"] = extra
except Exception:
    pass
backup_offsite = {}
try:
    offsite = load_json(f"{LIB}/offsite.json", {})
    if offsite:
        backup_offsite = {"offsite": offsite}
except Exception:
    pass

# Restart history, written by valheim-restart-exec.py. This is the token-free copy -- the
# page's live view comes from /var/www/valheim/restart-state.json, which carries the CSRF
# token and is therefore not world-readable. Absent until the restart feature is deployed.
restart_info = {}
try:
    restart_info = load_json(f"{LIB}/restart.json", {}) or {}
except Exception:
    pass

# ---------------------------------------------------------------- build id / auto-update
build = None
try:
    m = re.search(r'"buildid"\s+"(\d+)"', open(MANIFEST).read())
    build = m.group(1) if m else None
except Exception:
    pass
autoupdate = {"last_check": None, "status": "unknown", "last_line": "", "history": []}
updating_times = []  # epochs of "updating A -> B" lines, for restart classification
try:
    lines = open(AUTOUPDATE_LOG).read().splitlines()
    for l in lines:
        m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) updating (\d+) -> (\d+)", l)
        if m:
            try:
                ts = local_epoch(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
            except Exception:
                continue
            updating_times.append(ts)
            autoupdate["history"].append({"t": ts, "from_build": m.group(2), "to_build": m.group(3)})
    autoupdate["history"] = autoupdate["history"][::-1][:20]
    checks = [l for l in lines if re.search(r"(up to date|update available|updating|updated OK|ERROR|could not|not active)", l)]
    if checks:
        last = checks[-1]
        autoupdate["last_line"] = last
        try:
            autoupdate["last_check"] = datetime.strptime(last[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            pass
        if "up to date" in last or "updated OK" in last:
            autoupdate["status"] = "current"
        elif "update available" in last:
            autoupdate["status"] = "pending"
            m = re.search(r"\((\d+) -> (\d+)\)", last)
            if m:
                autoupdate["pending_build"] = m.group(2)
        elif "updating" in last:
            autoupdate["status"] = "updating"
        else:
            autoupdate["status"] = "error"
    if state.get("last_update_line") != autoupdate["last_line"] and "updated OK" in autoupdate["last_line"]:
        m = re.search(r"build (\d+)", autoupdate["last_line"])
        events.append({"t": autoupdate["last_check"] or now, "kind": "update", "text": f"Auto-updater installed Steam build {m.group(1) if m else '?'}"})
    state["last_update_line"] = autoupdate["last_line"]
except Exception:
    pass


def systemd_span(s):
    """'1d 4h 5min 25.9s' -> seconds"""
    units = {"y": 31557600, "month": 2629800, "w": 604800, "d": 86400, "h": 3600, "min": 60, "s": 1, "ms": 1e-3, "us": 1e-6}
    return sum(float(n) * units[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(month|min|ms|us|y|w|d|h|s)\b", s))


next_update_check = None
try:
    tp = dict(l.split("=", 1) for l in sh("systemctl", "show", "valheim-update.timer", "-p", "NextElapseUSecRealtime", "-p", "NextElapseUSecMonotonic").splitlines() if "=" in l)
    if tp.get("NextElapseUSecRealtime"):
        v = sh("date", "-d", tp["NextElapseUSecRealtime"], "+%s")
        next_update_check = int(v) if v.isdigit() else None
    if next_update_check is None and tp.get("NextElapseUSecMonotonic"):
        next_update_check = int(boot_time + systemd_span(tp["NextElapseUSecMonotonic"]))
except Exception:
    pass

next_backup = None
try:
    auto = sorted(fn for fn in os.listdir(WORLDS_LOCAL) if "_backup_auto-" in fn)
    if auto:
        m = re.search(r"(\d{8}-\d{6})$", auto[-1])
        last_bk = local_epoch(datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")) if m else os.path.getmtime(os.path.join(WORLDS_LOCAL, auto[-1]))
        next_backup = int(last_bk + settings["backup_short_s"])
except Exception:
    pass

# ---------------------------------------------------------------- Azure maintenance (IMDS scheduled events)
azure_maintenance = {"fetched": None, "incarnation": None, "events": [], "error": None}
try:
    imds_cache = state.setdefault("imds_cache", {"fetched": 0, "error": None, "data": None})
    if now - imds_cache.get("fetched", 0) >= 300:  # cache for 5 minutes
        try:
            req = urllib.request.Request("http://169.254.169.254/metadata/scheduledevents?api-version=2020-07-01",
                                        headers={"Metadata": "true"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
                imds_cache = {"fetched": now, "error": None, "data": data}
                state["imds_cache"] = imds_cache
        except Exception as e:
            imds_cache["fetched"] = now
            imds_cache["error"] = str(e)[:200]
            state["imds_cache"] = imds_cache
    if imds_cache.get("data"):
        data = imds_cache["data"]
        azure_maintenance["fetched"] = imds_cache.get("fetched")
        azure_maintenance["incarnation"] = data.get("DocumentIncarnation")
        azure_maintenance["events"] = [{"id": e.get("EventId"), "type": e.get("EventType"), "status": e.get("EventStatus"),
                                       "not_before": e.get("NotBefore"), "description": e.get("Description"),
                                       "resources": e.get("Resources")} for e in data.get("Events", [])]
    if imds_cache.get("error"):
        azure_maintenance["error"] = imds_cache["error"]
except Exception as e:
    azure_maintenance["error"] = str(e)[:200]

# ---------------------------------------------------------------- OS updates
os_updates = {"pending": None, "security": None, "reboot_required": False, "checked": int(now)}
try:
    updates_avail = f"{LIB}/updates-available"
    if os.path.exists("/var/lib/update-notifier/updates-available"):
        updates_avail = "/var/lib/update-notifier/updates-available"
    try:
        with open(updates_avail) as f:
            content = f.read()
        m = re.search(r"(\d+) updates? can be applied immediately", content, re.IGNORECASE)
        if m:
            os_updates["pending"] = int(m.group(1))
        m = re.search(r"(\d+) of these updates? (?:are|is) (?:a )?standard security updates?", content, re.IGNORECASE)
        if m:
            os_updates["security"] = int(m.group(1))
        elif os_updates["pending"] is not None:
            os_updates["security"] = 0
    except Exception:
        pass
    os_updates["reboot_required"] = os.path.exists("/var/run/reboot-required")
except Exception:
    pass

# ---------------------------------------------------------------- persist events + samples
def append_and_trim(path, new_items, keep_after):
    items = []
    try:
        with open(path) as f:
            items = [json.loads(l) for l in f if l.strip()]
    except Exception:
        pass
    items.extend(new_items)
    items = [x for x in items if x.get("t", 0) >= keep_after]
    items.sort(key=lambda x: x["t"])
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        for x in items:
            f.write(json.dumps(x, separators=(",", ":")) + "\n")
    # Explicit, like save_json: events.jsonl and samples.jsonl are read by the medals engine
    # running as the unprivileged valheim-bot user. Leaving the mode to the process umask
    # meant a later UMask= hardening of this unit would make them unreadable, breaking
    # Hermodr's stats silently.
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return items

players_now = live_count if live_count is not None else len(state["online"])
server_online = active and "players" in a2s
pg = {p["name"]: pings[sid] for sid, p in state["online"].items() if pings.get(sid) is not None}
sample = {"t": int(now), "p": players_now, "c": cpu_pct, "m": round(100 * (1 - mem_avail / mem_total), 1),
          "o": 1 if server_online else 0, "rx": rx_bytes, "tx": tx_bytes, "w": world_bytes}
if pg:
    sample["pg"] = pg
samples = append_and_trim(f"{LIB}/samples.jsonl", [sample], now - RETAIN_SECONDS)
all_events = append_and_trim(f"{LIB}/events.jsonl", events, 0)

# all-time tallies per player (sessions, seconds, deaths), fed by leave/death events; never trimmed
totals = state.setdefault("totals", {})
def tally(e):
    t = totals.setdefault(e["name"], {"sessions": 0, "seconds": 0, "last_seen": 0})
    t["sessions"] += 1
    t["seconds"] += e["played"]
    t["last_seen"] = max(t["last_seen"], e["t"])
def tally_death(e):
    t = totals.setdefault(e["name"], {"sessions": 0, "seconds": 0, "last_seen": 0})
    t["deaths"] = t.get("deaths", 0) + 1
    t["last_death"] = max(t.get("last_death") or 0, e["t"])
if not state.get("totals_seeded"):
    for e in all_events:
        if e["kind"] == "leave" and e.get("played"):
            tally(e)
    state["totals_seeded"] = True
else:
    for e in events:
        if e["kind"] == "leave" and e.get("played"):
            tally(e)
if not state.get("deaths_seeded"):
    for t in totals.values():
        t["deaths"], t["last_death"] = 0, None
    for e in all_events:
        if e["kind"] == "death":
            tally_death(e)
    state["deaths_seeded"] = True
else:
    for e in events:
        if e["kind"] == "death":
            tally_death(e)

# ping per player over the last 24 h (from samples' pg)
ping_hist = {}
for s_ in samples:
    if s_["t"] >= now - 86400 and s_.get("pg"):
        for n, ms in s_["pg"].items():
            ping_hist.setdefault(n, []).append(ms)

online_by_name = {p["name"]: p for p in state["online"].values()}
player_stats = []
for name in set(totals) | set(online_by_name):
    t = totals.get(name, {"sessions": 0, "seconds": 0, "last_seen": 0})
    cur = int(now - online_by_name[name]["since"]) if name in online_by_name else None
    sessions = t["sessions"] + (1 if cur is not None else 0)
    total = t["seconds"] + (cur or 0)
    ph = ping_hist.get(name)
    player_stats.append({"name": name, "online": cur is not None, "current": cur, "sessions": sessions,
                         "total": total, "avg": round(total / sessions) if sessions else 0,
                         "last_seen": now if cur is not None else t["last_seen"],
                         "deaths": t.get("deaths", 0), "last_death": t.get("last_death"),
                         "avg_ping_24h": round(sum(ph) / len(ph)) if ph else None,
                         "worst_ping_24h": max(ph) if ph else None})
player_stats.sort(key=lambda x: (-x["online"], -x["total"]))

# time played per player over the last 7 days (from leave events + those still online)
week = now - 7 * 86400
played = {}
for e in all_events:
    if e["kind"] == "leave" and e.get("played") and e["t"] >= week:
        played[e["name"]] = played.get(e["name"], 0) + e["played"]
for p in state["online"].values():
    played[p["name"]] = played.get(p["name"], 0) + int(now - p["since"])
peak7 = max([s["p"] for s in samples if s["t"] >= week] + [0])

# ---------------------------------------------------------------- sessions rebuilt from the event log (30 days)
sessions_list = []  # (name, start, end)
open_since = {}
for e in all_events:
    if e["kind"] == "join":
        if e["name"] in open_since:  # a join without a leave: close the old one here
            sessions_list.append((e["name"], open_since[e["name"]], e["t"]))
        open_since[e["name"]] = e["t"]
    elif e["kind"] == "leave" and e["name"] in open_since:
        sessions_list.append((e["name"], open_since.pop(e["name"]), e["t"]))
for name, st in open_since.items():
    if name in online_by_name:
        sessions_list.append((name, st, None))


def online_at(t):
    return sorted({n for n, a, b in sessions_list if a <= t <= (b if b is not None else now)})


together = {}
for i, (n1, a1, b1) in enumerate(sessions_list):
    for n2, a2, b2 in sessions_list[i + 1:]:
        if n1 == n2:
            continue
        b1_end = b1 if b1 is not None else now
        b2_end = b2 if b2 is not None else now
        ov = min(b1_end, b2_end) - max(a1, a2)
        if ov > 0:
            key = tuple(sorted((n1, n2)))
            together[key] = together.get(key, 0) + ov
together_list = sorted([{"a": a, "b": b, "seconds": int(v)} for (a, b), v in together.items()], key=lambda x: -x["seconds"])

records = {"longest_session": None, "biggest_gathering": None, "longest_streak": None, "most_deaths": None}
if sessions_list:
    n, a, b = max(sessions_list, key=lambda s_: (s_[2] if s_[2] is not None else now) - s_[1])
    b_end = b if b is not None else now
    records["longest_session"] = {"name": n, "seconds": int(b_end - a), "t": a}
if samples:
    best = max(samples, key=lambda s_: (s_["p"], -s_["t"]))
    if best["p"] > 0:
        records["biggest_gathering"] = {"count": best["p"], "t": best["t"]}
streaks = {}
for n, a, b in sessions_list:
    b_end = b if b is not None else now
    d0, d1 = local_dt(a).date(), local_dt(b_end).date()
    days = streaks.setdefault(n, {})
    while d0 <= d1:
        days[d0] = max(days.get(d0, 0), b_end)
        d0 += timedelta(days=1)
for n, days in streaks.items():
    ds = sorted(days)
    run, start = 1, 0
    for i in range(1, len(ds) + 1):
        if i < len(ds) and (ds[i] - ds[i - 1]).days == 1:
            continue
        length = i - start
        if not records["longest_streak"] or length > records["longest_streak"]["days"]:
            records["longest_streak"] = {"name": n, "days": length, "end_t": days[ds[i - 1]]}
        start = i
md = max(totals.items(), key=lambda kv: kv[1].get("deaths", 0), default=None)
if md and md[1].get("deaths", 0) > 0:
    records["most_deaths"] = {"name": md[0], "deaths": md[1]["deaths"]}

# ---------------------------------------------------------------- sessions last 7 days and calendar
sessions_7d = []
calendar = {}
try:
    week_start = now - 7 * 86400
    for name, start, end in sessions_list:
        # include if session overlaps with last 7 days
        session_end = end if end else now
        if session_end >= week_start:
            sessions_7d.append({"name": name, "start": int(start), "end": None if end is None else int(end)})
    sessions_7d.sort(key=lambda x: x["start"])

    # calendar: per player, per date (local), seconds played; split sessions at local midnight
    year_start = now - 365 * 86400
    for name, start, end in sessions_list:
        session_end = end if end else now
        # only include if session overlaps with last 365 days
        if session_end >= year_start:
            if name not in calendar:
                calendar[name] = {}
            # walk from session start to end in local time, splitting at midnight
            session_start_local = max(start, year_start)
            dt = local_dt(session_start_local).replace(hour=0, minute=0, second=0, microsecond=0)
            end_dt = local_dt(session_end).replace(hour=0, minute=0, second=0, microsecond=0)
            while dt <= end_dt:
                day_end_ts = local_epoch(dt + timedelta(days=1))
                session_end_ts = min(session_end, day_end_ts)
                seconds_today = int(max(0, session_end_ts - max(session_start_local, local_epoch(dt))))
                if seconds_today > 0:
                    date_key = dt.strftime("%Y-%m-%d")
                    calendar[name][date_key] = calendar[name].get(date_key, 0) + seconds_today
                dt = dt + timedelta(days=1)
except Exception:
    pass

# raids
raid_events = [e for e in all_events if e["kind"] == "raid"][::-1]
raids = {"count_7d": sum(1 for e in raid_events if e["t"] >= week),
         "last": {"t": raid_events[0]["t"], "name": raid_events[0]["name"], "label": raid_events[0]["label"]} if raid_events else None,
         "list": [{"t": e["t"], "name": e["name"], "label": e["label"], "online": online_at(e["t"])} for e in raid_events[:15]]}

# bosses: from the world file keys, else inferred from raids seen
bosses = dict(world_scan.get("bosses") or {"source": "unknown"})
if bosses.get("source") != "keys":
    bosses = {"source": "inferred" if raid_events else "unknown"}
    for b_ in BOSS_ORDER:
        bosses[b_] = False
    for e in raid_events:
        for b_ in RAID_IMPLIES.get(e["name"], []):
            bosses[b_] = True
for b_ in BOSS_ORDER:
    bosses.setdefault(b_, False)

# heat map: local weekday x hour over 30 days of samples
cnt = [[0] * 24 for _ in range(7)]
busy = [[0] * 24 for _ in range(7)]
psum = [[0] * 24 for _ in range(7)]
for s_ in samples:
    dt = local_dt(s_["t"])
    wd, h = dt.weekday(), dt.hour
    cnt[wd][h] += 1
    psum[wd][h] += s_["p"] or 0
    if (s_["p"] or 0) > 0:
        busy[wd][h] += 1
heatmap = {"busy": [[round(busy[w][h] / cnt[w][h], 3) if cnt[w][h] else 0 for h in range(24)] for w in range(7)],
           "avg": [[round(psum[w][h] / cnt[w][h], 2) if cnt[w][h] else 0 for h in range(24)] for w in range(7)],
           "weeks": round((now - min(s_["t"] for s_ in samples)) / 604800, 1) if samples else 0}

# uptime over 30 days: share of samples where the server answered
osamples = [s_["o"] for s_ in samples if "o" in s_]
uptime_30d_pct = round(100 * sum(osamples) / len(osamples), 2) if osamples else None

# world growth over 7 days
growth_7d = 0
wk = [s_["w"] for s_ in samples if s_["t"] >= week and s_.get("w")]
if len(wk) >= 2:
    growth_7d = wk[-1] - wk[0]

# ---------------------------------------------------------------- restarts (cause classification)
restart_log = state.setdefault("restart_log", [])


def restart_cause(t, crashed=False):
    if any(0 <= t - u <= 300 for u in updating_times):
        return "update"
    if crashed:
        return "crash"
    if t - boot_time < 600:
        return "boot"
    return "manual"


if not state.get("restart_log_seeded"):
    for e in all_events:
        if e["kind"] == "start":
            m = re.search(r"Valheim (\S+)", e.get("text", ""))
            restart_log.append({"t": e["t"], "cause": restart_cause(e["t"]), "version": m.group(1) if m else None})
    state["restart_log_seeded"] = True
elif restarted:
    t_r = started_at or now
    restart_log.append({"t": t_r, "cause": restart_cause(t_r, crashed=nrestarts > state.get("nrestarts", 0)), "version": state.get("version")})
state["nrestarts"] = nrestarts
restart_log.sort(key=lambda x: x["t"])
del restart_log[:-50]

# ---------------------------------------------------------------- news + discord (cached, best effort)
def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "valheim-dashboard/1.0 (+vm-valheim)"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


news_cache = load_json(NEWS_CACHE, {"fetched": 0, "items": []})
if now - news_cache.get("fetched", 0) > 3600:
    try:
        items = fetch_json(NEWS_URL).get("appnews", {}).get("newsitems", [])
        news_cache = {"fetched": now, "items": [{"t": it.get("date"), "title": it.get("title"), "url": it.get("url")}
                                                for it in items if it.get("feedlabel") == "Community Announcements"][:5]}
        save_json(NEWS_CACHE, news_cache)
    except Exception as e:
        news_cache["error"] = str(e)[:200]
        news_cache["fetched"] = now - 3600 + 600  # retry in 10 min, keep the old items
        try:
            save_json(NEWS_CACHE, news_cache)
        except Exception:
            pass
news = news_cache.get("items", [])

if DISCORD_INVITE:
    discord_cache = load_json(DISCORD_CACHE, {"fetched": 0})
    if now - discord_cache.get("fetched", 0) > 600:
        try:
            d_ = fetch_json(DISCORD_URL)
            discord_cache = {"name": (d_.get("guild") or {}).get("name"), "members": d_.get("approximate_member_count"),
                             "online": d_.get("approximate_presence_count"), "invite": f"https://discord.gg/{DISCORD_INVITE}", "fetched": int(now)}
            save_json(DISCORD_CACHE, discord_cache)
        except Exception as e:
            discord_cache["error"] = str(e)[:200]
            discord_cache["fetched"] = now - 600 + 120  # retry in 2 min
            try:
                save_json(DISCORD_CACHE, discord_cache)
            except Exception:
                pass
    discord = {"name": discord_cache.get("name"), "members": discord_cache.get("members"), "online": discord_cache.get("online"),
               "invite": f"https://discord.gg/{DISCORD_INVITE}", "fetched": discord_cache.get("fetched")}
else:
    # No DISCORD_INVITE configured -- omit the card entirely rather than emit a broken link.
    discord = {"name": None, "members": None, "online": None, "invite": None, "fetched": None}

# ---------------------------------------------------------------- history.json
DAY_KEYS = ("t", "p", "c", "m", "o", "pg")
day = [{k: s_.get(k) for k in DAY_KEYS if k in s_ or k == "o"} for s_ in samples if s_["t"] >= now - 86400]
buckets = {}
for s_ in samples:
    if s_["t"] < now - 7 * 86400:
        continue
    b = s_["t"] // 600 * 600
    x = buckets.setdefault(b, {"t": b, "p": 0, "c": [], "m": [], "o": None})
    x["p"] = max(x["p"], s_["p"])
    if s_["c"] is not None:
        x["c"].append(s_["c"])
    x["m"].append(s_["m"])
    if "o" in s_:
        x["o"] = max(x["o"] or 0, s_["o"])
week_series = []
for b in sorted(buckets):
    x = buckets[b]
    week_series.append({"t": x["t"], "p": x["p"], "c": round(sum(x["c"]) / len(x["c"]), 1) if x["c"] else None, "m": round(sum(x["m"]) / len(x["m"]), 1), "o": x["o"]})
save_json(f"{WEB}/history.json", {"generated": int(now), "day": day, "week": week_series})

# ---------------------------------------------------------------- status.json
status = {
    "generated": int(now),
    "server": {
        "name": a2s.get("name", settings["name"]),
        "world": settings["world"],
        "address": SERVER_ADDRESS,
        "online": server_online,
        "service_active": active,
        "started_at": started_at,
        "restarts_since_boot": nrestarts,
        "version": a2s.get("game_version") or state.get("version"),
        "network_version": state.get("network_version"),
        "build": build,
        "max_players": settings["max_players"],
        "a2s_rtt_ms": a2s.get("rtt_ms"),
        "zdos": state.get("zdos"),
        "net_last": state.get("net"),
        "modifiers": ", ".join(f"{k}: {v}" for k, v in settings["modifiers"].items() if v != "default") or "default",
        "crossplay": settings["crossplay"],
        "settings": settings,
        "uptime_30d_pct": uptime_30d_pct,
        "restarts": restart_log[::-1][:20],
        "next_update_check": next_update_check,
        "next_backup": next_backup,
    },
    "players": {
        "count": players_now,
        "online": sorted([{"name": p["name"], "steam_name": p.get("steam_name", ""), "since": p["since"], "ping_ms": pings.get(sid), "ping_checked": sid in state.get("ip_of", {})} for sid, p in state["online"].items()], key=lambda x: x["since"]),
        "known": sorted(set(state["steam_names"].values())),
        "played_7d": sorted([{"name": k, "seconds": v} for k, v in played.items()], key=lambda x: -x["seconds"]),
        "peak_7d": peak7,
        "stats": player_stats,
        "tracking_since": 1789080000,
        "together": together_list,
        "heatmap": heatmap,
        "records": records,
        "records_window": "all",
        "sessions_7d": sessions_7d,
        "calendar": calendar,
    },
    "world": {
        "last_save": state.get("last_save"),
        "save_number": state.get("save_number"),
        "bytes": world_bytes,
        "backups": backups[-5:],
        "day": world_day,
        "bosses": bosses,
        "built": dict(world_scan.get("built") or {}, scanned_at=world_scan.get("scanned_at")),
        "growth_7d_bytes": growth_7d,
        "raids": raids,
        **({"extra": world_scan["extra"]} if "extra" in world_scan else {}),
    },
    "updates": autoupdate,
    "machine": {
        "cpu_pct": cpu_pct, "load1": load1,
        "mem_total": mem_total, "mem_used": mem_total - mem_avail,
        "swap_used": swap_used, "valheim_rss": rss,
        "disk_total": disk.total, "disk_used": disk.used,
        "uptime_s": uptime_s,
        "net": net_bps,
        "os_updates": os_updates,
    },
    "azure": {
        "maintenance": azure_maintenance,
    },
    "backup": backup_offsite,
    "restart": restart_info,
    "news": news,
    "discord": discord,
    "events": all_events[-MAX_EVENTS_IN_STATUS:][::-1],
    "collect_ms": int((time.time() - t_run0) * 1000),
}
save_json(f"{WEB}/status.json", status)
save_json(f"{LIB}/state.json", state)
