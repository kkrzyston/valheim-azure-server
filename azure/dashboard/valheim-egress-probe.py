#!/usr/bin/env python3
"""valheim-egress-probe.py -- 1 Hz sampler of game egress, to test one specific hypothesis.

The question: with three players online the VM is 98% idle, no UDP buffer errors, no NIC drops,
empty socket queues and healthy per-player ping -- and yet game egress plateaus around 240-276
KB/s and, in 4,025 samples over 30 days, never once exceeded 276,425 B/s. If the box is not the
limiter, the application is: Valheim's ZDOMan gives each peer a send budget (historically
m_dataPerSec = 61440 B/s), and once that budget is the binding constraint, more players or a
busier world do not buy more bytes -- they buy staler updates for everyone. Which is exactly what
"it only lags when we're all in one place" sounds like from the inside.

The only way to tell a ceiling from a coincidence is resolution. The dashboard collector samples
once a minute, which smooths a hard 1-second ceiling into a soft-looking average; the journal's
`Connections N ZDOS:X sent:Y recv:Z` line prints once every ten minutes and `sent:` is an
instantaneous snapshot, not an interval rate, so no rate can honestly be derived from it. So:
one sample a second, from nftables counters, with the mean packet size recorded alongside the
byte rate -- because a saturated *byte* budget and a saturated *packet* path look identical in a
byte-rate graph and completely different in bytes-per-packet.

What it writes, once a second, only while players are online:

  /var/lib/valheim-status/egress-YYYY-MM-DD.jsonl
    t    wall clock, 1 decimal place
    txb  game bytes sent in this second     txp  packets sent
    rxb  game bytes received                rxp  packets received
    sz   txb/txp -- mean packet size. This is the falsifiable one: near-MTU is consistent with a
         byte budget saturating, small packets say the limit is elsewhere.
    n    players online
    sq   tx_queue on the game socket, from /proc/net/udp. If the kernel were the backpressure
         this would be non-zero; it proves the queue is empty while the rate is pinned.
    rtt  A2S round trip to the local query port, every 5th sample only.

Costs, deliberately: one short-lived `nft` call and two small /proc reads per second while anyone
is online, buffered in memory and written once every 15 seconds -- not once a second. Zero work
and zero writes when the server is empty. The player-count gate reads the collector's existing
status.json (rewritten every 60 s) rather than querying the game, so deciding whether to measure
costs nothing that could itself perturb the measurement.

Nothing installation-specific is baked in: ports and paths come from the environment, the same
way every other script here reads /etc/valheim-server.env.

Testing off the VM, with no nftables, no game and no root:
  python3 valheim-egress-probe.py --selftest    exercises every parser and the write path against
                                                fixtures, in a temp directory, and exits non-zero
                                                on the first failure.
  python3 valheim-egress-probe.py --once        take and print one live sample pair (needs nft).
"""
import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------- config (env only)
# Same convention as valheim-medals.py's MEDALS_* / valheim-restartd.py's RESTARTD_*: every value
# has a working default, and `.strip() or <default>` rather than os.environ.get's default, because
# an EnvironmentFile= line that is present but empty sets the variable to "" and get() would
# happily hand that back.
def _env(name, default):
    return os.environ.get(name, "").strip() or default


def _env_num(name, default, cast=float):
    try:
        return cast(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


STATUS = _env("EGRESS_STATUS", "/var/www/valheim/status.json")
LIB = _env("EGRESS_DIR", "/var/lib/valheim-status")
NFT_HELPER = _env("EGRESS_NFT_HELPER", "/usr/local/sbin/valheim-meter-nft.sh")
NFT_TABLE = ("inet", "valheim_meter")
GAME_PORT = _env_num("VALHEIM_GAME_PORT", 2456, int)
QUERY_PORT = _env_num("VALHEIM_QUERY_PORT", 2457, int)
RETAIN_DAYS = _env_num("EGRESS_RETAIN_DAYS", 7, int)
FLUSH_SEC = _env_num("EGRESS_FLUSH_SEC", 15.0)
IDLE_POLL_SEC = _env_num("EGRESS_IDLE_POLL_SEC", 10.0)
GATE_RECHECK_SEC = _env_num("EGRESS_GATE_RECHECK_SEC", 10.0)
STATUS_MAX_AGE = _env_num("EGRESS_STATUS_MAX_AGE", 180.0)
RTT_EVERY = _env_num("EGRESS_RTT_EVERY", 5, int)
A2S_TIMEOUT = _env_num("EGRESS_A2S_TIMEOUT", 0.5)
NFT_REPAIR_SEC = _env_num("EGRESS_NFT_REPAIR_SEC", 60.0)

_said = set()


def log(msg, once_key=None):
    """stderr, so systemd puts it in the journal. once_key collapses a condition that will repeat
    every single second -- a missing counter table, a malformed status.json -- into one line."""
    if once_key is not None:
        if once_key in _said:
            return
        _said.add(once_key)
    print(f"valheim-egress-probe: {msg}", file=sys.stderr, flush=True)


def unsay(once_key):
    """Re-arm a once-only message, so a condition that recovers and then recurs is reported again
    rather than staying quiet forever after the first occurrence."""
    _said.discard(once_key)


# ---------------------------------------------------------------- nftables counters
def parse_counters(blob):
    """{name: (bytes, packets)} out of `nft -j list counters table ...`. Returns whatever it can
    find; the caller decides whether the counters it needs are present."""
    out = {}
    for node in blob.get("nftables", []):
        c = node.get("counter") if isinstance(node, dict) else None
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        if isinstance(name, str) and "bytes" in c and "packets" in c:
            out[name] = (int(c["bytes"]), int(c["packets"]))
    return out


def read_counters():
    """(txb, txp, rxb, rxp) cumulative, or None if the table is not readable. One `nft` call for
    both counters rather than one each -- this runs every second."""
    try:
        p = subprocess.run(["nft", "-j", "list", "counters", "table", NFT_TABLE[0], NFT_TABLE[1]],
                           capture_output=True, text=True, timeout=5)
        if p.returncode != 0:
            raise RuntimeError((p.stderr or "").strip()[:200] or f"nft exit {p.returncode}")
        c = parse_counters(json.loads(p.stdout))
        tx, rx = c.get("game_tx"), c.get("game_rx")
        if tx is None or rx is None:
            raise RuntimeError("game_tx/game_rx missing from the table")
        return tx[0], tx[1], rx[0], rx[1]
    except Exception as e:
        log(f"cannot read the nftables counters ({e}); sampling is paused until the table is back",
            once_key="counters")
        return None


def repair_counters():
    """Reinstall the meter table. Something else on the box can flush it (an nftables.service
    reload, a careless `nft flush ruleset`), and a 30-day experiment that quietly stops collecting
    on day 3 is worse than useless. Rate-limited by the caller."""
    try:
        p = subprocess.run([NFT_HELPER, "install"], capture_output=True, text=True, timeout=20)
        if p.returncode == 0:
            log("reinstalled the nftables meter table")
            return True
        log(f"could not reinstall the meter table: {(p.stderr or p.stdout).strip()[:200]}",
            once_key="repair")
    except Exception as e:
        log(f"could not run {NFT_HELPER}: {e!r}", once_key="repair")
    return False


# ---------------------------------------------------------------- socket send queue
_UDP_RE = re.compile(r"^\s*\d+:\s+[0-9A-Fa-f]+:([0-9A-Fa-f]{4})\s+\S+\s+\S+\s+([0-9A-Fa-f]+):")


def parse_udp_queue(text, port):
    """Sum of tx_queue across every UDP socket bound to `port`, from the /proc/net/udp table.
    Columns: sl, local_address(hex ip:hex port), rem_address, st, tx_queue:rx_queue, ...

    This is the control for "is the kernel the bottleneck?". If the send queue is empty every
    second while the byte rate is pinned, the application is not even trying to send more --
    which is the whole claim. Returns None if nothing is bound to the port at all, so an
    unreadable measurement never masquerades as a measured zero."""
    total, found = 0, False
    for line in text.splitlines():
        m = _UDP_RE.match(line)
        if not m or int(m.group(1), 16) != port:
            continue
        found = True
        total += int(m.group(2), 16)
    return total if found else None


def read_send_queue(port):
    total, found = 0, False
    for path in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(path) as f:
                v = parse_udp_queue(f.read(), port)
        except Exception:
            continue
        if v is not None:
            total += v
            found = True
    if not found:
        log(f"no UDP socket bound to port {port} in /proc/net/udp[6]", once_key="sq")
        return None
    unsay("sq")
    return total


# ---------------------------------------------------------------- A2S round trip
_A2S_QUERY = b"\xff\xff\xff\xffTSource Engine Query\x00"


def a2s_rtt(addr, timeout=A2S_TIMEOUT):
    """Milliseconds for a full A2S_INFO exchange against the local query port, challenge included
    -- deliberately the same exchange valheim-status-collect.py already times as `a2s_rtt_ms`, so
    the two series are directly comparable. Loopback, and the meter table excludes loopback, so
    this never lands in the numbers it is measured against."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        t0 = time.monotonic()
        s.sendto(_A2S_QUERY, addr)
        d, _ = s.recvfrom(4096)
        if d[4:5] == b"A":  # challenge -- answer it, and time the whole round trip
            s.sendto(_A2S_QUERY + d[5:9], addr)
            s.recvfrom(4096)
        return round((time.monotonic() - t0) * 1000, 1)
    except Exception:
        return None
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


# ---------------------------------------------------------------- the player-count gate
def read_status(path, now, max_age=STATUS_MAX_AGE):
    """(players, why). `players` is an int, or None when the file cannot be trusted -- in which
    case the caller treats it as nobody online and does not measure. A status.json older than
    max_age means the collector has stopped, so its player count is a claim about the past."""
    try:
        with open(path) as f:
            st = json.load(f)
    except FileNotFoundError:
        return None, "status.json is missing"
    except Exception as e:
        return None, f"status.json is unreadable ({e})"
    gen = st.get("generated")
    if not isinstance(gen, (int, float)):
        return None, "status.json has no usable 'generated' timestamp"
    if now - gen > max_age:
        return None, f"status.json is {int(now - gen)}s stale (collector stopped?)"
    n = (st.get("players") or {}).get("count")
    if not isinstance(n, int):
        return None, "status.json has no players.count"
    return n, ""


# ---------------------------------------------------------------- the sample
def build_record(t, prev, cur, n, sq, rtt):
    """One second of deltas, or None if the counters went backwards -- which means the table was
    recreated under us and this interval spans a reset. Dropping the sample is right: a bogus
    negative or absurd positive rate in the data set is worse than a one-second hole in it."""
    txb, txp, rxb, rxp = (c - p for c, p in zip(cur, prev))
    if txb < 0 or txp < 0 or rxb < 0 or rxp < 0:
        return None
    rec = {"t": round(t, 1), "txb": txb, "txp": txp, "rxb": rxb, "rxp": rxp, "n": n}
    if txp:
        rec["sz"] = round(txb / txp, 1)
    if sq is not None:
        rec["sq"] = sq
    if rtt is not None:
        rec["rtt"] = rtt
    return rec


# ---------------------------------------------------------------- output file
_NAME_RE = re.compile(r"^egress-(\d{4})-(\d{2})-(\d{2})\.jsonl$")


def egress_path(lib, t):
    return os.path.join(lib, "egress-%s.jsonl" % datetime.fromtimestamp(t).strftime("%Y-%m-%d"))


def flush_records(lib, records):
    """Plain append, grouped by the day each record belongs to. Deliberately NOT the collector's
    append_and_trim(): that one reads the whole file back, re-sorts it and rewrites it on every
    call, which is O(n) per write and would be re-writing a 86,400-line file once every 15
    seconds by the end of a day. Append only; trimming happens once a day, by unlinking whole
    files. Returns the number of records written."""
    if not records:
        return 0
    by_day = {}
    for r in records:
        by_day.setdefault(egress_path(lib, r["t"]), []).append(r)
    written = 0
    for path, rows in by_day.items():
        fresh = not os.path.exists(path)
        try:
            with open(path, "a") as f:
                for r in rows:
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")
            if fresh:
                # Explicit, like the collector's save_json: everything under /var/lib/valheim-status
                # is read by the analysis script and by the medals engine running unprivileged, and
                # leaving the mode to the umask means a later UMask= hardening of this unit breaks
                # them silently.
                os.chmod(path, 0o644)
            written += len(rows)
        except Exception as e:
            log(f"could not append {len(rows)} samples to {path}: {e!r}", once_key="flush")
    if written:
        unsay("flush")
    return written


def prune(lib, now, days=RETAIN_DAYS):
    """Unlink whole days older than the retention window. The date comes from the filename, not
    from mtime: a file still being appended to has today's mtime regardless of which day it
    holds, and any stray touch would otherwise resurrect a file we meant to drop."""
    cutoff = (datetime.fromtimestamp(now) - timedelta(days=days)).date()
    dropped = []
    try:
        names = os.listdir(lib)
    except Exception as e:
        log(f"cannot list {lib} to prune old samples: {e!r}", once_key="prune")
        return dropped
    for name in names:
        m = _NAME_RE.match(name)
        if not m:
            continue
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            continue  # egress-2026-13-45.jsonl is not a day; leave it alone rather than guess
        if d < cutoff:
            try:
                os.unlink(os.path.join(lib, name))
                dropped.append(name)
            except Exception as e:
                log(f"could not unlink {name}: {e!r}", once_key="prune")
    if dropped:
        log("pruned %d file(s) older than %d days: %s" % (len(dropped), days, ", ".join(sorted(dropped))))
    return dropped


# ---------------------------------------------------------------- main loop
def run():
    os.makedirs(LIB, exist_ok=True)
    log("started: game port %d, query port %d, flush every %gs, %d-day retention, samples in %s"
        % (GAME_PORT, QUERY_PORT, FLUSH_SEC, RETAIN_DAYS, LIB))
    prune(LIB, time.time())

    buf = []
    prev = None                 # last cumulative counter read, or None when not sampling
    n = 0                       # players online per the last gate check
    i = 0                       # sample index, for the every-Nth A2S probe
    next_tick = time.monotonic()
    next_gate = 0.0
    next_flush = time.monotonic() + FLUSH_SEC
    next_repair = 0.0
    last_prune_day = datetime.fromtimestamp(time.time()).date()

    while True:
        mono, wall = time.monotonic(), time.time()

        # --- the gate. Re-read the collector's status.json rather than asking the game anything.
        if mono >= next_gate:
            next_gate = mono + (GATE_RECHECK_SEC if prev is not None else IDLE_POLL_SEC)
            players, why = read_status(STATUS, wall)
            if players is None:
                log(why + "; treating the server as empty", once_key="status")
                players = 0
            else:
                unsay("status")
            if players > 0 and n == 0:
                log(f"{players} player(s) online -- sampling at 1 Hz")
            elif players == 0 and n > 0:
                log("server empty -- sampling stopped")
            n = players

        # --- empty server: no reads, no writes, no counters. Just wait.
        if n <= 0:
            if buf:
                flush_records(LIB, buf)
                buf = []
            prev, i = None, 0
            next_tick = mono + min(IDLE_POLL_SEC, max(0.0, next_gate - mono))
            time.sleep(max(0.05, next_tick - time.monotonic()))
            continue

        # --- one sample
        cur = read_counters()
        if cur is None:
            prev = None
            if mono >= next_repair:
                next_repair = mono + NFT_REPAIR_SEC
                if repair_counters():
                    unsay("counters")
        elif prev is None:
            prev = cur          # first read after a gap only establishes the baseline
            unsay("counters")
        else:
            rtt = a2s_rtt(("127.0.0.1", QUERY_PORT)) if RTT_EVERY > 0 and i % RTT_EVERY == 0 else None
            rec = build_record(wall, prev, cur, n, read_send_queue(GAME_PORT), rtt)
            prev = cur
            if rec is None:
                log("counters went backwards (table recreated?); re-baselining", once_key="reset")
            else:
                unsay("reset")
                buf.append(rec)
        i += 1

        # --- one write per FLUSH_SEC, not one per sample
        if time.monotonic() >= next_flush:
            next_flush = time.monotonic() + FLUSH_SEC
            if buf:
                flush_records(LIB, buf)
                buf = []
            today = datetime.fromtimestamp(wall).date()
            if today != last_prune_day:
                last_prune_day = today
                prune(LIB, wall)

        # --- absolute deadline, so a slow second does not make every later second late
        next_tick += 1.0
        slack = next_tick - time.monotonic()
        if slack < -5.0:        # suspended VM, clock jump, or a very bad minute: resynchronise
            next_tick = time.monotonic() + 1.0
            slack = 1.0
        time.sleep(max(0.0, slack))


# ---------------------------------------------------------------- selftest (no nft, no game, no root)
def selftest():
    fails = []

    def check(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + (("  -- " + str(detail)) if not cond and detail else ""))
        if not cond:
            fails.append(name)

    print("counters")
    blob = {"nftables": [{"metainfo": {"version": "1.0.9"}},
                         {"counter": {"family": "inet", "table": "valheim_meter", "name": "game_tx",
                                      "handle": 1, "packets": 1000, "bytes": 1200000}},
                         {"counter": {"family": "inet", "table": "valheim_meter", "name": "game_rx",
                                      "handle": 2, "packets": 400, "bytes": 60000}}]}
    c = parse_counters(blob)
    check("parses both counters", c == {"game_tx": (1200000, 1000), "game_rx": (60000, 400)}, c)
    check("empty ruleset yields nothing", parse_counters({"nftables": []}) == {})
    check("garbage does not raise", parse_counters({"nftables": ["x", {"table": {}}, {"counter": 7}]}) == {})

    print("socket send queue")
    proc = (" sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
            " 123: 00000000:0998 00000000:0000 07 0000002A:00000000 00:00000000 00000000 0 0 20114 2 0 0\n"
            " 124: 00000000:0999 00000000:0000 07 00000000:00000000 00:00000000 00000000 0 0 20115 2 0 0\n"
            " 125: 0100007F:1F40 00000000:0000 07 00000005:00000000 00:00000000 00000000 0 0 20116 2 0 0\n")
    check("tx_queue for the game port", parse_udp_queue(proc, 0x0998) == 42, parse_udp_queue(proc, 0x0998))
    check("a quiet port reads zero, not None", parse_udp_queue(proc, 0x0999) == 0)
    check("an unbound port reads None", parse_udp_queue(proc, 9999) is None)
    check("a header-only table reads None", parse_udp_queue(" sl local_address\n", 0x0998) is None)

    print("player-count gate")
    d = tempfile.mkdtemp(prefix="egress-selftest-")
    now = time.time()
    sp = os.path.join(d, "status.json")
    with open(sp, "w") as f:
        json.dump({"generated": int(now), "players": {"count": 3}}, f)
    check("fresh status yields the count", read_status(sp, now)[0] == 3)
    with open(sp, "w") as f:
        json.dump({"generated": int(now - 600), "players": {"count": 3}}, f)
    check("stale status is refused", read_status(sp, now)[0] is None, read_status(sp, now))
    with open(sp, "w") as f:
        f.write("{not json")
    check("malformed status is refused", read_status(sp, now)[0] is None)
    check("missing status is refused", read_status(os.path.join(d, "nope.json"), now)[0] is None)
    with open(sp, "w") as f:
        json.dump({"generated": int(now), "players": {}}, f)
    check("status without a count is refused", read_status(sp, now)[0] is None)

    print("records")
    r = build_record(1770000000.04, (1000, 10, 500, 5), (1061440, 52, 3000, 45), 1, 0, 12.5)
    check("byte and packet deltas", r and (r["txb"], r["txp"], r["rxb"], r["rxp"]) == (1060440, 42, 2500, 40), r)
    check("mean packet size is recorded", r and r["sz"] == round(1060440 / 42, 1), r)
    check("wall clock to 1dp", r and r["t"] == 1770000000.0, r)
    check("rtt and sq carried", r and r["rtt"] == 12.5 and r["sq"] == 0, r)
    check("a counter reset is dropped, not recorded",
          build_record(1.0, (5000, 50, 0, 0), (10, 1, 0, 0), 1, 0, None) is None)
    check("zero packets means no sz (never a divide by zero)",
          "sz" not in build_record(1.0, (5, 5, 0, 0), (5, 5, 0, 0), 2, 0, None))

    print("write path")
    # Today's timestamps on purpose: the retention block below runs prune() over this same
    # directory, and a fixture dated years ago would (correctly) be swept away before the
    # "today's file survives" check could mean anything.
    noon = datetime.fromtimestamp(now).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    rows = [{"t": round(noon + k, 1), "txb": 240000 + k, "txp": 200, "rxb": 9000, "rxp": 90,
             "sz": 1200.0, "n": 3, "sq": 0} for k in range(30)]
    check("all rows written", flush_records(d, rows) == 30)
    check("second flush appends rather than rewrites", flush_records(d, rows[:5]) == 5)
    path = egress_path(d, rows[0]["t"])
    with open(path) as f:
        lines = [json.loads(l) for l in f if l.strip()]
    check("file holds every appended row", len(lines) == 35, len(lines))
    check("round-trips unchanged", lines[0] == rows[0], lines[0])
    check("one line per record, no rewrite of history", lines[:30] == rows, "ordering changed")
    check("mode is 0644", (os.stat(path).st_mode & 0o777) == 0o644 or os.name == "nt",
          oct(os.stat(path).st_mode & 0o777))

    print("retention")
    for name in ("egress-2000-01-01.jsonl", "egress-2000-01-02.jsonl", "egress-2026-13-45.jsonl",
                 "samples.jsonl", "events.jsonl"):
        open(os.path.join(d, name), "w").close()
    dropped = set(prune(d, now, days=7))
    check("old days unlinked", dropped == {"egress-2000-01-01.jsonl", "egress-2000-01-02.jsonl"}, dropped)
    check("today's file survives", os.path.exists(path))
    check("events.jsonl is never touched", os.path.exists(os.path.join(d, "events.jsonl")))
    check("samples.jsonl is never touched", os.path.exists(os.path.join(d, "samples.jsonl")))
    check("an unparseable date is left alone", os.path.exists(os.path.join(d, "egress-2026-13-45.jsonl")))

    print("")
    if fails:
        print("selftest FAILED: " + ", ".join(fails))
        return 1
    print("selftest passed (fixtures in %s)" % d)
    return 0


def main():
    ap = argparse.ArgumentParser(description="1 Hz Valheim egress probe")
    ap.add_argument("--selftest", action="store_true",
                    help="run every parser and the write path against fixtures; needs no nft, game or root")
    ap.add_argument("--once", action="store_true",
                    help="print a single live sample (two counter reads a second apart) and exit")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.once:
        first = read_counters()
        if first is None:
            print("no counters -- is the meter table installed? (valheim-meter-nft.sh install)")
            return 1
        time.sleep(1.0)
        second = read_counters()
        if second is None:
            return 1
        rec = build_record(time.time(), first, second, (read_status(STATUS, time.time())[0] or 0),
                           read_send_queue(GAME_PORT), a2s_rtt(("127.0.0.1", QUERY_PORT)))
        print(json.dumps(rec, separators=(",", ":")))
        return 0
    try:
        run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
