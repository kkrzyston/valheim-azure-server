#!/usr/bin/env python3
"""valheim-egress-probe.py -- 1 Hz sampler of game egress, to test one specific hypothesis.

The question: with three players online the VM is 98% idle, no UDP buffer errors, no NIC drops,
empty socket queues and healthy per-player ping -- and yet game egress plateaus and, in 4,025
samples over 30 days, never once exceeded 276,425 B/s. If the box is not the limiter, the
application is: Valheim's ZDOMan gives each peer a send budget, and once that budget binds, more
players or a busier world do not buy more bytes -- they buy staler updates for everyone. Which is
what "it only lags when we're all in one place" sounds like from the inside.

The only way to tell a ceiling from a coincidence is resolution. The dashboard collector samples
once a minute, which smooths a hard 1-second ceiling into a soft-looking average; the journal's
`Connections N ZDOS:X sent:Y recv:Z` line prints once every ten minutes and `sent:` is an
instantaneous snapshot, not an interval rate, so no rate can honestly be derived from it. So: one
sample a second, from nftables counters, with mean packet size recorded alongside the byte rate --
because a saturated *byte* budget and a saturated *packet* path look identical in a byte-rate
graph and completely different in bytes-per-packet.

EVERY FIELD HERE EXISTS TO BE FALSIFIABLE, INCLUDING THE ONES ABOUT THE PROBE ITSELF. A probe that
fabricates a plausible number is worse than one that records nothing, because the analysis cannot
tell the difference afterwards. So every row carries the interval it actually spans and the age
and corroboration of its own player count, and any row that cannot be trusted is dropped rather
than written:

  t    wall clock, 1 decimal place
  dt   seconds this row actually spans, from a monotonic clock. NOT assumed to be 1.0: a stalled
       loop (host migration, a paused VM, an NTP step) would otherwise turn a 60-second gap into
       one row reading 15 MB/s with a perfectly plausible packet size -- and one such row prints
       REFUTED. Rows outside DT_MIN..DT_MAX are dropped.
  txb  game bytes sent during dt          txp  packets sent
  rxb  game bytes received                rxp  packets received
  sz   txb/txp -- mean packet size. The falsifiable one: near-MTU is consistent with a byte
       budget saturating, small packets say the limit is packets, syscalls or a tick.
  n    players online       na  how many seconds old that figure was when the row was written
  np   addresses in the nftables `peers` set -- an independent check on n. A joining player
       appears there within one packet; status.json can be a minute behind. A row where np > n
       has an n that is too low, which reads as a false ceiling breach, so it is dropped.
  sq   tx_queue on the game socket, from /proc/net/udp. If the kernel were applying the
       backpressure this would be non-zero.
  qtb  bytes on the Steam query port during dt -- counted separately precisely so that it is NOT
       in txb, since the modelled ceiling describes per-peer game traffic and nothing else.
  v6   present only when IPv6 game traffic was seen. The peer set is IPv4-only, so a v6 player
       would desynchronise bytes from player count; this makes that visible instead of silent.
  rtt  A2S round trip to the local query port, every RTT_EVERY-th sample. On a timeout the row
       carries `rtt_to` (the bound, in ms) instead: a timeout means the game stalled for longer
       than the bound, which is exactly the event this field exists to catch. Dropping it would
       hide the spike AND lower the median it is compared against.

Costs, deliberately: one short-lived `nft` call and two small file reads per second while anyone
is online, buffered in memory and written once every 15 seconds. Zero work and zero writes when
the server is empty. Nothing installation-specific is baked in: ports and paths come from the
environment, like every other script here.

Testing off the VM, with no nftables, no game and no root:
  python3 valheim-egress-probe.py --selftest   every parser, every drop rule and the write path,
                                               against fixtures, in a temp directory.
  python3 valheim-egress-probe.py --once       one live sample pair, printed (needs nft).
"""
import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta


def log(msg):
    print("valheim-egress-probe: " + msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- degraded-state logging
# The brief for this experiment named the failure to avoid by name: "quietly stopped collecting on
# day 3". A once-per-process warning IS that failure -- the probe says "counters are gone" at
# 02:14 on day 3 and then says nothing for twenty-seven days, and in the journal an idle probe is
# byte-identical to a wedged one. So: warn on first occurrence, keep warning on a timer for as
# long as the condition lasts, say how many times it happened, and say so again when it clears.
# Plus an unconditional heartbeat, so that silence always means "not running".
_warned = {}


def warn(key, msg, every=None):
    every = WARN_EVERY if every is None else every
    now = time.monotonic()
    e = _warned.get(key)
    if e is None:
        _warned[key] = {"t": now, "n": 1, "first": now}
        log(msg)
        return
    e["n"] += 1
    if now - e["t"] >= every:
        log(f"{msg}  [still happening: {e['n']} time(s) in the last {int(now - e['t'])}s, "
            f"{int(now - e['first'])}s since it started]")
        e["t"], e["n"] = now, 0


def recovered(key, msg):
    e = _warned.pop(key, None)
    if e is not None:
        log(f"{msg} (was broken for {int(time.monotonic() - e['first'])}s)")


# ---------------------------------------------------------------- config (env only)
# Same convention as valheim-medals.py's MEDALS_* / valheim-restartd.py's RESTARTD_*: every value
# has a working default, and `.strip() or <default>` rather than os.environ.get's default, because
# an EnvironmentFile= line that is present but empty sets the variable to "" and get() would hand
# that back.
def _env(name, default):
    return os.environ.get(name, "").strip() or default


def _env_num(name, default, cast=float):
    """An unparseable value is LOUD. A silent fallback here would cost the experiment weeks:
    EGRESS_RETAIN_DAYS=30d parses as nothing, would silently become the default, and prune() would
    delete the early weeks without a word. A config typo must never look like a configuration."""
    raw = _env(name, None)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        log(f"WARNING: {name}={raw!r} is not a number -- falling back to {default}. Fix it: a "
            f"misconfigured retention or interval silently changes what this records.")
        return default


STATUS = _env("EGRESS_STATUS", "/var/www/valheim/status.json")
LIB = _env("EGRESS_DIR", "/var/lib/valheim-status")
NFT_HELPER = _env("EGRESS_NFT_HELPER", "/usr/local/sbin/valheim-meter-nft.sh")
NFT_TABLE = ("inet", "valheim_meter")
GAME_PORT = _env_num("VALHEIM_GAME_PORT", 2456, int)
QUERY_PORT = _env_num("VALHEIM_QUERY_PORT", 2457, int)
# 35 days, not 7: the experiment is described everywhere as a month-long window, and at roughly
# 1.5 MB a day, 35 days is about 45 MB. Seven days of retention on a thirty-day experiment
# destroys the evidence to save nothing.
RETAIN_DAYS = _env_num("EGRESS_RETAIN_DAYS", 35, int)
FLUSH_SEC = _env_num("EGRESS_FLUSH_SEC", 15.0)
IDLE_POLL_SEC = _env_num("EGRESS_IDLE_POLL_SEC", 10.0)
STATUS_MAX_AGE = _env_num("EGRESS_STATUS_MAX_AGE", 180.0)   # gate only: "is anyone on at all"
N_MAX_AGE = _env_num("EGRESS_N_MAX_AGE", 65.0)              # recorded n: must be fresher than this
RTT_EVERY = _env_num("EGRESS_RTT_EVERY", 5, int)
A2S_TIMEOUT = _env_num("EGRESS_A2S_TIMEOUT", 0.5)
NFT_REPAIR_SEC = _env_num("EGRESS_NFT_REPAIR_SEC", 60.0)
WARN_EVERY = _env_num("EGRESS_WARN_EVERY", 900.0)
HEARTBEAT_SEC = _env_num("EGRESS_HEARTBEAT_SEC", 3600.0)
DT_MIN = _env_num("EGRESS_DT_MIN", 0.80)
DT_MAX = _env_num("EGRESS_DT_MAX", 1.25)
# ~66 minutes of samples held in memory if the disk refuses writes. Bounded, because an unbounded
# buffer turns a full disk into an OOM kill; dropping the OLDEST keeps the most recent hour, the
# part most likely to still be diagnosable when someone notices.
BUF_MAX = _env_num("EGRESS_BUF_MAX", 4000, int)


# ---------------------------------------------------------------- nftables: counters + peers
def parse_table(blob):
    """({counter_name: (bytes, packets)}, {peer addresses}) from `nft -j list table ...`.

    One `nft` call gets both: the counters the experiment turns on, and the peer set that both the
    collector (per-player ping) and this probe (an independent check on the player count) read.
    Two calls a second would be two forks a second for no reason."""
    counters, peers = {}, set()
    for node in blob.get("nftables", []):
        if not isinstance(node, dict):
            continue
        c = node.get("counter")
        if isinstance(c, dict) and isinstance(c.get("name"), str) and "bytes" in c and "packets" in c:
            try:
                counters[c["name"]] = (int(c["bytes"]), int(c["packets"]))
            except (TypeError, ValueError):
                pass
        s = node.get("set")
        if isinstance(s, dict) and s.get("name") == "peers":
            for e in s.get("elem", []):
                # A set with `flags timeout` returns {"elem": {"val": ..., "expires": ...}};
                # without it, a bare string. Handle both.
                if isinstance(e, dict):
                    e = e.get("elem", e)
                    e = e.get("val") if isinstance(e, dict) else e
                if isinstance(e, str):
                    peers.add(e)
    return counters, peers


def read_table():
    """(counters_dict, peers) or (None, err_class).

    err_class names each failure mode rather than collapsing them into "cannot read", because the
    caller decides whether to REBUILD the table based on it -- and a 5-second `nft` timeout under
    heavy load means something entirely different from a table that is genuinely gone."""
    try:
        p = subprocess.run(["nft", "-j", "list", "table", NFT_TABLE[0], NFT_TABLE[1]],
                           capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except Exception as e:
        return None, f"exec:{e!r}"
    if p.returncode != 0:
        return None, "absent" if "No such file" in (p.stderr or "") else f"exit{p.returncode}"
    try:
        counters, peers = parse_table(json.loads(p.stdout))
    except Exception as e:
        return None, f"parse:{e!r}"
    if "game_tx" not in counters or "game_rx" not in counters:
        return None, "incomplete"
    return counters, peers


def table_present():
    """True / False / None(unknown). Asked before any rebuild, because the rebuild is destructive
    -- it zeroes the counters and empties the peer set the collector depends on -- and read_table()
    failing is NOT evidence that the table is gone. The likeliest cause of an `nft` timeout is
    load, and the busiest seconds are exactly the ones the experiment needs. Rebuilding on that
    evidence would destroy the measurement it was trying to rescue."""
    try:
        p = subprocess.run(["nft", "list", "tables"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if p.returncode != 0:
        return None
    return NFT_TABLE[1] in p.stdout


def repair_table():
    try:
        p = subprocess.run([NFT_HELPER, "install"], capture_output=True, text=True, timeout=20)
        if p.returncode == 0:
            log("rebuilt the nftables meter table")
            return True
        warn("repair", f"could not rebuild the meter table: {(p.stderr or p.stdout).strip()[:200]}")
    except Exception as e:
        warn("repair", f"could not run {NFT_HELPER}: {e!r}")
    return False


# ---------------------------------------------------------------- socket send queue
_UDP_RE = re.compile(r"^\s*\d+:\s+[0-9A-Fa-f]+:([0-9A-Fa-f]{4})\s+\S+\s+\S+\s+([0-9A-Fa-f]+):")


def parse_udp_queue(text, port):
    """Sum of tx_queue across every UDP socket bound to `port`, from the /proc/net/udp table.
    Columns: sl, local_address(hex ip:hex port), rem_address, st, tx_queue:rx_queue, ...

    The control for "is the kernel the bottleneck?". Returns None when nothing is bound to the
    port, so an unreadable measurement never masquerades as a measured zero."""
    total, found = 0, False
    for line in text.splitlines():
        m = _UDP_RE.match(line)
        if not m or int(m.group(1), 16) != port:
            continue
        found = True
        total += int(m.group(2), 16)
    return total if found else None


def read_send_queue(port, paths=("/proc/net/udp", "/proc/net/udp6")):
    """(value, reason). An I/O error and "the game is not listening" are different facts and get
    different reasons -- reading them both as "no socket bound" would let a /proc mount problem
    look like a dead game server."""
    total, found, errs = 0, False, []
    for path in paths:
        try:
            with open(path) as f:
                v = parse_udp_queue(f.read(), port)
        except Exception as e:
            errs.append(f"{path}: {e!r}")
            continue
        if v is not None:
            total += v
            found = True
    if found:
        return total, "ok"
    if len(errs) == len(paths):
        return None, "unreadable: " + "; ".join(errs)[:200]
    return None, f"no UDP socket bound to port {port}"


# ---------------------------------------------------------------- A2S: round trip AND player count
_A2S_QUERY = b"\xff\xff\xff\xffTSource Engine Query\x00"


def parse_a2s_players(d):
    """Player count out of an A2S_INFO reply, laid out the way the collector already parses it."""
    try:
        i = 6
        for _ in range(4):
            i = d.index(b"\x00", i) + 1
        return d[i + 2]
    except Exception:
        return None


def a2s_probe(addr, timeout=None):
    """(rtt_ms, players, status). status is "ok", "timeout" or "error:...".

    A TIMEOUT IS DATA. It means the game did not answer within the bound, which is the exact event
    R6 exists to detect. Swallowing it would hide the spike AND lower the median that R6's
    threshold is derived from -- biasing the analysis toward finding nothing wrong, which is the
    direction that flatters the hypothesis. So the caller is told which of the three happened and
    records it.

    The exchange (challenge included) is deliberately the same one the collector already times as
    `a2s_rtt_ms`, so the two series are comparable. Loopback, which the meter table excludes, so
    this never lands in the numbers it is measured against."""
    timeout = A2S_TIMEOUT if timeout is None else timeout
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        t0 = time.monotonic()
        s.sendto(_A2S_QUERY, addr)
        d, _ = s.recvfrom(4096)
        if d[4:5] == b"A":                       # challenge -- answer it, time the whole trip
            s.sendto(_A2S_QUERY + d[5:9], addr)
            d, _ = s.recvfrom(4096)
        return round((time.monotonic() - t0) * 1000, 1), parse_a2s_players(d), "ok"
    except socket.timeout:
        return None, None, "timeout"
    except Exception as e:
        return None, None, f"error:{e!r}"
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


# ---------------------------------------------------------------- the player-count gate
def read_status(path, now, max_age=None):
    """(players, why, generated). `players` is an int, or None when the file cannot be trusted --
    in which case the caller treats it as nobody online and does not measure. A status.json older
    than max_age means the collector has stopped, so its count is a claim about the past."""
    max_age = STATUS_MAX_AGE if max_age is None else max_age
    try:
        with open(path) as f:
            st = json.load(f)
    except FileNotFoundError:
        return None, "status.json is missing", None
    except Exception as e:
        return None, f"status.json is unreadable ({e})", None
    if not isinstance(st, dict):
        return None, "status.json is not an object", None
    gen = st.get("generated")
    if isinstance(gen, bool) or not isinstance(gen, (int, float)):
        return None, "status.json has no usable 'generated' timestamp", None
    if now - gen > max_age:
        return None, f"status.json is {int(now - gen)}s stale (collector stopped?)", gen
    n = (st.get("players") or {}).get("count") if isinstance(st.get("players"), dict) else None
    # isinstance(True, int) is True in Python, so a bare isinstance check reads {"count": true} as
    # one player online and starts sampling an empty server. Exclude bool explicitly, and refuse a
    # negative count rather than carry it into the arithmetic.
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return None, f"status.json has no usable players.count ({n!r})", gen
    return n, "", gen


def choose_n(a2s_n, a2s_age, status_n, status_age):
    """(n, age, source) -- whichever player count is freshest.

    status.json is rewritten once a minute, so its count can be a minute old. A third player
    joining a saturated two-player session therefore produced a minute of rows labelled n=2
    carrying three players' worth of bytes: a 1.5x apparent ceiling breach, which fires R1 and
    prints REFUTED on the first busy evening. A2S is queried every few seconds on loopback and is
    authoritative, so it wins whenever it is fresher."""
    cands = [(age, n, src) for n, age, src in
             ((a2s_n, a2s_age, "a2s"), (status_n, status_age, "status"))
             if n is not None and age is not None]
    if not cands:
        return None, None, None
    age, n, src = min(cands)
    return n, age, src


# ---------------------------------------------------------------- the sample
def build_record(t, dt, prev, cur, n, na, npeers, sq, rtt, query_tx=None, v6=None):
    """(record, None) or (None, reason).

    Every rejection here is a row NOT written, and that is the point: a hole is honest and the
    analysis can see it, whereas a fabricated rate is indistinguishable from a real one and is
    exactly what would make this experiment produce a confident wrong answer.

    `prev`/`cur` are (txb, txp, rxb, rxp) cumulative counter tuples."""
    if dt is None or not (DT_MIN <= dt <= DT_MAX):
        return None, "dt"           # a stall, a clock step, a paused VM -- not a one-second rate
    txb, txp, rxb, rxp = (c - p for c, p in zip(cur, prev))
    if txb < 0 or txp < 0 or rxb < 0 or rxp < 0:
        return None, "reset"        # counters went backwards: the table was recreated under us
    if n is None or na is None or na > N_MAX_AGE:
        return None, "nage"         # no trustworthy player count for this interval
    if npeers is not None and npeers > n:
        return None, "npeers"       # someone is sending who is not in the count yet: n is too low
    rec = {"t": round(t, 1), "dt": round(dt, 3), "txb": txb, "txp": txp, "rxb": rxb, "rxp": rxp,
           "n": n, "na": int(na)}
    if txp:
        rec["sz"] = round(txb / txp, 1)
    if npeers is not None:
        rec["np"] = npeers
    if sq is not None:
        rec["sq"] = sq
    if query_tx:
        rec["qtb"] = query_tx
    if v6:
        rec["v6"] = v6
    if rtt is not None:
        val, status = rtt
        if status == "ok" and val is not None:
            rec["rtt"] = val
        elif status == "timeout":
            rec["rtt_to"] = round(A2S_TIMEOUT * 1000, 1)
        else:
            rec["rtt_err"] = 1
    return rec, None


# ---------------------------------------------------------------- output file
_NAME_RE = re.compile(r"^egress-(\d{4})-(\d{2})-(\d{2})\.jsonl$")


def egress_path(lib, t):
    return os.path.join(lib, "egress-%s.jsonl" % datetime.fromtimestamp(t).strftime("%Y-%m-%d"))


def flush_records(lib, records):
    """Append, grouped by the day each record belongs to (the 15-second buffer straddles midnight
    every night, so this branch is not hypothetical). Returns the records it could NOT write, so
    the caller keeps them buffered and retries -- the previous version returned a count nobody
    looked at while the caller cleared the buffer unconditionally, which meant a full disk or one
    EPERM produced a single log line and then silently discarded every sample forever.

    Deliberately NOT the collector's append_and_trim(): that one reads the whole file back,
    re-sorts it and rewrites it on every call, which by evening would be rewriting an 86,400-line
    file once every fifteen seconds. Append only; trimming unlinks whole files once a day."""
    if not records:
        return []
    by_day = {}
    for r in records:
        by_day.setdefault(egress_path(lib, r["t"]), []).append(r)
    unwritten = []
    for path, rows in sorted(by_day.items()):
        fresh = not os.path.exists(path)
        try:
            with open(path, "a") as f:
                for r in rows:
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")
            if fresh:
                # Explicit, like the collector's save_json: everything under /var/lib/valheim-status
                # is read by the analysis and by the medals engine running unprivileged, and leaving
                # the mode to the umask means a later UMask= hardening breaks them silently.
                os.chmod(path, 0o644)
            recovered("flush", f"writing samples to {path} works again")
        except Exception as e:
            warn("flush", f"cannot write {len(rows)} sample(s) to {path}: {e!r} -- holding them in "
                          f"memory and retrying")
            unwritten.extend(rows)
    return unwritten


def prune(lib, now, days=None):
    """Unlink whole days older than the retention window. The date comes from the filename, not
    from mtime: a file still being appended to has today's mtime regardless of which day it holds,
    and any stray touch would otherwise resurrect a file we meant to drop."""
    days = RETAIN_DAYS if days is None else days
    cutoff = (datetime.fromtimestamp(now) - timedelta(days=days)).date()
    dropped = []
    try:
        names = os.listdir(lib)
    except Exception as e:
        warn("prune", f"cannot list {lib} to prune old samples: {e!r}")
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
                warn("prune", f"could not unlink {name}: {e!r}")
    if dropped:
        log("pruned %d file(s) older than %d days (EGRESS_RETAIN_DAYS): %s"
            % (len(dropped), days, ", ".join(sorted(dropped))))
    return dropped


# ---------------------------------------------------------------- main loop
def run():
    os.makedirs(LIB, exist_ok=True)
    log(f"started: game port {GAME_PORT}, query port {QUERY_PORT}, flush every {FLUSH_SEC:g}s, "
        f"retention {RETAIN_DAYS} days, accepting intervals {DT_MIN:g}-{DT_MAX:g}s, player count "
        f"must be <={N_MAX_AGE:g}s old, samples in {LIB}")

    stats = {"written": 0, "dt": 0, "reset": 0, "nage": 0, "npeers": 0,
             "a2s_timeout": 0, "a2s_error": 0, "overflow": 0}
    buf = []
    prev = prev_mono = None
    prev_q = prev_v6 = None
    n = 0
    i = 0
    a2s_n, a2s_at = None, None
    next_tick = time.monotonic()
    next_flush = time.monotonic() + FLUSH_SEC
    next_repair = 0.0
    next_beat = time.monotonic() + HEARTBEAT_SEC
    last_prune_day = None

    def heartbeat(state):
        drops = ", ".join(f"{k}={stats[k]}" for k in ("dt", "reset", "nage", "npeers", "overflow")
                          if stats[k])
        log(f"heartbeat: {state}; {stats['written']:,} samples written, {len(buf)} buffered"
            + (f"; dropped {drops}" if drops else "; no samples dropped")
            + (f"; a2s timeouts {stats['a2s_timeout']}" if stats["a2s_timeout"] else ""))

    def maybe_prune(wall):
        # Retention runs on a day boundary regardless of whether anyone is online. It used to live
        # inside the flush block, below the "nobody online -> continue" early exit, which made it
        # "enforced only while someone is playing" -- so a quiet fortnight left a fortnight of
        # extra files on disk and the operator's retention setting meant nothing.
        nonlocal last_prune_day
        today = datetime.fromtimestamp(wall).date()
        if today != last_prune_day:
            last_prune_day = today
            prune(LIB, wall)

    maybe_prune(time.time())

    while True:
        mono, wall = time.monotonic(), time.time()
        maybe_prune(wall)

        # --- the gate: is anyone on at all? The collector's status.json, not the game.
        players, why, generated = read_status(STATUS, wall)
        if players is None:
            warn("status", why + "; treating the server as empty")
            status_n, status_age = None, None
            players = 0
        else:
            recovered("status", "status.json is readable again")
            status_n, status_age = players, wall - generated
        if players > 0 and n == 0:
            log(f"{players} player(s) online -- sampling at 1 Hz")
        elif players == 0 and n > 0:
            log("server empty -- sampling stopped")
            heartbeat("idle")
        n = players

        if mono >= next_beat:
            next_beat = mono + HEARTBEAT_SEC
            heartbeat("sampling" if n > 0 else "idle, server empty")

        # --- empty server: no counters, no writes. Just wait.
        if n <= 0:
            if buf:
                buf = flush_records(LIB, buf)
            prev = prev_mono = prev_q = prev_v6 = None
            a2s_n, a2s_at, i = None, None, 0
            time.sleep(IDLE_POLL_SEC)
            next_tick = time.monotonic()
            continue

        counters, peers = read_table()
        if counters is None:
            err = peers                      # the error class, when the read failed
            warn("counters", f"cannot read the nftables meter table ({err}); sampling is paused")
            prev = prev_mono = prev_q = prev_v6 = None
            # Only rebuild on evidence that it is actually gone. `nft` timing out under the very
            # load this experiment exists to measure is not that evidence, and the rebuild would
            # zero the counters and empty the collector's peer set as collateral.
            if mono >= next_repair:
                next_repair = mono + NFT_REPAIR_SEC
                present = table_present()
                if present is False:
                    log(f"meter table is confirmed absent (read failed with: {err}) -- rebuilding")
                    if repair_table():
                        recovered("counters", "meter table is readable again")
                elif present is None:
                    warn("nftdown", "cannot even list nftables tables -- NOT rebuilding on a guess; "
                                    "the rebuild is destructive and this is not evidence")
                else:
                    warn("nftpresent", f"the meter table exists but could not be read ({err}) -- NOT "
                                       f"rebuilding; that would destroy live counters")
            i += 1
        else:
            recovered("counters", "meter table is readable again")
            cur = counters["game_tx"][0], counters["game_tx"][1], \
                counters["game_rx"][0], counters["game_rx"][1]
            q = counters.get("query_tx", (0, 0))[0]
            v6 = counters.get("game_tx6", (0, 0))[0] + counters.get("game_rx6", (0, 0))[0]
            npeers = len(peers)
            if npeers == 0:
                # Players online and nobody in the peer set is positive evidence of breakage, not
                # an absence of information -- it silently zeroes the dashboard's ping series.
                warn("nopeers", f"{n} player(s) online but the nftables peers set is empty -- "
                                f"per-player ping is silently off; is the `in` chain installed?")
            else:
                recovered("nopeers", "the peers set has addresses again")

            rtt = None
            if RTT_EVERY > 0 and i % RTT_EVERY == 0:
                ms, pc, st = a2s_probe(("127.0.0.1", QUERY_PORT))
                rtt = (ms, st)
                if st == "ok":
                    if pc is not None:
                        a2s_n, a2s_at = pc, wall
                    recovered("a2s", "the A2S query port is answering again")
                elif st == "timeout":
                    stats["a2s_timeout"] += 1
                    warn("a2s", f"A2S did not answer within {A2S_TIMEOUT:g}s -- recorded as a "
                                f"latency spike at the bound, not discarded")
                else:
                    stats["a2s_error"] += 1
                    warn("a2s", f"A2S query failed ({st})")

            eff_n, eff_age, _src = choose_n(a2s_n, (wall - a2s_at) if a2s_at else None,
                                            status_n, status_age)
            sq, sq_why = read_send_queue(GAME_PORT)
            if sq is None:
                warn("sq", f"no socket send queue reading: {sq_why}")
            else:
                recovered("sq", "the game socket's send queue is readable again")

            if prev is None:
                prev, prev_mono, prev_q, prev_v6 = cur, mono, q, v6   # baseline only
            else:
                d_v6 = v6 - prev_v6
                rec, why_drop = build_record(wall, mono - prev_mono, prev, cur, eff_n, eff_age,
                                             npeers, sq, rtt, query_tx=max(0, q - prev_q),
                                             v6=max(0, d_v6))
                prev, prev_mono, prev_q, prev_v6 = cur, mono, q, v6
                if d_v6 > 0:
                    warn("v6", f"{d_v6} bytes of IPv6 game traffic seen. The peer set is IPv4-only, "
                               f"so a v6 player's bytes are NOT in txb and their ping is missing -- "
                               f"per-player scaling (R2) will be wrong while this persists.")
                if rec is None:
                    stats[why_drop] = stats.get(why_drop, 0) + 1
                    warn("drop:" + why_drop, {
                        "dt": "dropped a sample whose interval was not ~1s (a stall, a paused VM or "
                              "a clock step). A hole is honest; a fabricated rate is not.",
                        "reset": "counters went backwards -- the meter table was recreated; "
                                 "re-baselining",
                        "nage": f"dropped a sample: no player count fresher than {N_MAX_AGE:g}s "
                                f"was available",
                        "npeers": "dropped a sample: more addresses are sending than the player "
                                  "count knows about, so the count is stale (someone just joined)",
                    }.get(why_drop, f"dropped a sample ({why_drop})"))
                else:
                    buf.append(rec)
                    stats["written"] += 1
            i += 1

        # --- one write per FLUSH_SEC, not one per sample
        if time.monotonic() >= next_flush:
            next_flush = time.monotonic() + FLUSH_SEC
            if buf:
                buf = flush_records(LIB, buf)
                if len(buf) > BUF_MAX:
                    lost = len(buf) - BUF_MAX
                    stats["overflow"] += lost
                    buf = buf[lost:]
                    warn("overflow", f"the sample buffer is full ({BUF_MAX}) because writes keep "
                                     f"failing -- discarded the {lost} oldest sample(s), "
                                     f"{stats['overflow']} lost in total")

        # --- absolute deadline, so a slow second does not make every later second late
        next_tick += 1.0
        slack = next_tick - time.monotonic()
        if slack < -5.0:
            # A big jump: suspended VM, host migration, NTP step. Re-baseline explicitly -- the
            # counters kept climbing across the gap and the next delta must not be charged to one
            # second. (build_record's dt check catches it too; belt and braces, because this is
            # the specific failure that fabricates a 15 MB/s sample.)
            log(f"resynchronising after a {-slack:.1f}s gap -- re-baselining the counters")
            prev = prev_mono = prev_q = prev_v6 = None
            next_tick = time.monotonic() + 1.0
            slack = 1.0
        time.sleep(max(0.0, slack))


# ---------------------------------------------------------------- selftest
def selftest(keep=False):
    fails = []

    def check(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name +
              (("  -- " + str(detail)) if not cond and detail else ""))
        if not cond:
            fails.append(name)

    def skip(name, why):
        print("  skip " + name + "  -- " + why)

    d = tempfile.mkdtemp(prefix="egress-selftest-")
    try:
        print("nftables table (counters + peers, one read)")
        blob = {"nftables": [{"metainfo": {"version": "1.0.9"}},
                             {"counter": {"name": "game_tx", "packets": 1000, "bytes": 1200000}},
                             {"counter": {"name": "game_rx", "packets": 400, "bytes": 60000}},
                             {"counter": {"name": "query_tx", "packets": 3, "bytes": 4000}},
                             {"counter": {"name": "game_tx6", "packets": 0, "bytes": 0}},
                             {"set": {"name": "peers", "type": "ipv4_addr", "flags": ["timeout"],
                                      "elem": [{"elem": {"val": "203.0.113.7", "expires": 88}},
                                               {"elem": {"val": "198.51.100.4", "expires": 12}}]}}]}
        c, p = parse_table(blob)
        check("parses every counter", c["game_tx"] == (1200000, 1000) and c["query_tx"] == (4000, 3), c)
        check("parses the peer set", p == {"203.0.113.7", "198.51.100.4"}, p)
        check("empty set yields no peers", parse_table({"nftables": [{"set": {"name": "peers"}}]})[1] == set())
        check("bare-string elements work",
              parse_table({"nftables": [{"set": {"name": "peers", "elem": ["192.0.2.9"]}}]})[1] == {"192.0.2.9"})
        check("garbage does not raise", parse_table({"nftables": ["x", {"counter": 7}, {"set": "no"}]}) == ({}, set()))
        check("a non-numeric counter is skipped, not crashed",
              parse_table({"nftables": [{"counter": {"name": "game_tx", "bytes": "lots", "packets": 1}}]})[0] == {})

        print("socket send queue")
        proc = (" sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"
                " 123: 00000000:0998 00000000:0000 07 0000002A:00000000 00:00000000 00000000 0 0 20114 2 0 0\n"
                " 124: 00000000:0999 00000000:0000 07 00000000:00000000 00:00000000 00000000 0 0 20115 2 0 0\n")
        check("tx_queue for the game port", parse_udp_queue(proc, 0x0998) == 42, parse_udp_queue(proc, 0x0998))
        check("a quiet port reads zero, not None", parse_udp_queue(proc, 0x0999) == 0)
        check("an unbound port reads None", parse_udp_queue(proc, 9999) is None)
        good = os.path.join(d, "udp")
        with open(good, "w") as f:
            f.write(proc)
        check("a readable table yields the value", read_send_queue(0x0998, (good,)) == (42, "ok"))
        v, why = read_send_queue(0x0998, (os.path.join(d, "nope"),))
        check("an I/O failure says 'unreadable', not 'no socket bound'",
              v is None and why.startswith("unreadable"), why)
        v, why = read_send_queue(9999, (good,))
        check("a readable table with no match says 'no socket bound'",
              v is None and "no UDP socket" in why, why)

        print("player-count gate")
        now = time.time()
        sp = os.path.join(d, "status.json")

        def st_write(obj):
            with open(sp, "w") as f:
                json.dump(obj, f)

        st_write({"generated": int(now), "players": {"count": 3}})
        check("fresh status yields the count", read_status(sp, now)[0] == 3)
        check("its age is returned, not assumed", abs(read_status(sp, now)[2] - int(now)) < 2)
        st_write({"generated": int(now - 600), "players": {"count": 3}})
        check("stale status is refused", read_status(sp, now)[0] is None, read_status(sp, now))
        with open(sp, "w") as f:
            f.write("{not json")
        check("malformed status is refused", read_status(sp, now)[0] is None)
        check("missing status is refused", read_status(os.path.join(d, "nope.json"), now)[0] is None)
        st_write({"generated": int(now), "players": {}})
        check("status without a count is refused", read_status(sp, now)[0] is None)
        st_write({"generated": int(now), "players": {"count": True}})
        check("a boolean count is refused (isinstance(True, int) is True!)", read_status(sp, now)[0] is None,
              read_status(sp, now))
        st_write({"generated": int(now), "players": {"count": "3"}})
        check("a string count is refused", read_status(sp, now)[0] is None)
        st_write({"generated": int(now), "players": {"count": None}})
        check("a null count is refused", read_status(sp, now)[0] is None)
        st_write({"generated": int(now), "players": {"count": -1}})
        check("a negative count is refused", read_status(sp, now)[0] is None)
        st_write({"generated": True, "players": {"count": 3}})
        check("a boolean timestamp is refused", read_status(sp, now)[0] is None)
        st_write({"generated": int(now), "players": 3})
        check("players as a scalar is refused", read_status(sp, now)[0] is None)

        print("which player count wins")
        check("A2S wins when fresher", choose_n(3, 2.0, 2, 55.0) == (3, 2.0, "a2s"))
        check("status wins when A2S is stale", choose_n(2, 90.0, 3, 5.0) == (3, 5.0, "status"))
        check("A2S alone is used", choose_n(4, 1.0, None, None)[0] == 4)
        check("neither available yields nothing", choose_n(None, None, None, None) == (None, None, None))
        check("a count with no age is not usable", choose_n(4, None, None, None) == (None, None, None))

        print("records, and the rows refused")
        base, later = (1000, 10, 500, 5), (1061440, 52, 3000, 45)
        r, _ = build_record(1770000000.04, 1.0, base, later, 3, 2, 3, 0, (12.5, "ok"), query_tx=900, v6=0)
        check("byte and packet deltas", r and (r["txb"], r["txp"], r["rxb"], r["rxp"]) == (1060440, 42, 2500, 40), r)
        check("mean packet size is recorded", r and r["sz"] == round(1060440 / 42, 1))
        check("dt is recorded, not assumed", r and r["dt"] == 1.0)
        check("player-count age and peer count are recorded", r and r["na"] == 2 and r["np"] == 3)
        check("query-port bytes are kept out of txb but still recorded", r and r["qtb"] == 900)
        check("rtt carried", r and r["rtt"] == 12.5)
        check("no v6 key when there is no v6 traffic", r and "v6" not in r)
        check("v6 traffic is recorded when present",
              build_record(1.0, 1.0, base, later, 3, 2, 3, 0, None, v6=500)[0]["v6"] == 500)
        check("a 60s stall is dropped, not recorded as one second",
              build_record(1.0, 60.0, base, later, 3, 2, 3, 0, None) == (None, "dt"))
        check("a 0.2s tick is dropped too", build_record(1.0, 0.2, base, later, 3, 2, 3, 0, None)[1] == "dt")
        check("an unknown interval is dropped", build_record(1.0, None, base, later, 3, 2, 3, 0, None)[1] == "dt")
        check("a counter reset is dropped",
              build_record(1.0, 1.0, (5000, 50, 0, 0), (10, 1, 0, 0), 3, 2, 3, 0, None)[1] == "reset")
        check("a stale player count is dropped", build_record(1.0, 1.0, base, later, 3, 999, 3, 0, None)[1] == "nage")
        check("more senders than the count knows about is dropped (someone just joined)",
              build_record(1.0, 1.0, base, later, 2, 2, 3, 0, None)[1] == "npeers")
        check("no player count at all is dropped",
              build_record(1.0, 1.0, base, later, None, None, None, 0, None)[1] == "nage")
        to = build_record(1.0, 1.0, base, later, 3, 2, 3, 0, (None, "timeout"))[0]
        check("an A2S timeout is recorded, not swallowed", to and to.get("rtt_to") and "rtt" not in to, to)
        er = build_record(1.0, 1.0, base, later, 3, 2, 3, 0, (None, "error:x"))[0]
        check("an A2S error is recorded too", er and er.get("rtt_err") == 1)
        check("zero packets means no sz (never a divide by zero)",
              "sz" not in build_record(1.0, 1.0, (5, 5, 0, 0), (5, 5, 0, 0), 2, 1, 2, 0, None)[0])

        print("write path, and what happens when it fails")
        noon = datetime.fromtimestamp(now).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        rows = [{"t": round(noon + k, 1), "dt": 1.0, "txb": 240000 + k, "txp": 200, "rxb": 9000,
                 "rxp": 90, "sz": 1200.0, "n": 3, "na": 2, "np": 3, "sq": 0} for k in range(30)]
        check("all rows written", flush_records(d, rows) == [])
        check("second flush appends rather than rewrites", flush_records(d, rows[:5]) == [])
        path = egress_path(d, rows[0]["t"])
        with open(path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        check("file holds every appended row", len(lines) == 35, len(lines))
        check("round-trips unchanged", lines[0] == rows[0], lines[0])
        check("no rewrite of history", lines[:30] == rows)
        # The 15 s buffer straddles midnight every night, so a batch spanning two days must land in
        # two files -- a branch that never runs in a test that only ever uses one timestamp.
        straddle = [{"t": noon, "txb": 1}, {"t": noon + 86400, "txb": 2}]
        check("a batch spanning midnight is split across two files", flush_records(d, straddle) == [])
        check("  ...and the second day got its own file", os.path.exists(egress_path(d, noon + 86400)))
        check("  ...which holds only its own row",
              len(open(egress_path(d, noon + 86400)).read().strip().splitlines()) == 1)
        if os.name == "nt":
            skip("mode is 0644", "POSIX modes are not meaningful on Windows -- verify on the VM")
        else:
            check("mode is 0644", (os.stat(path).st_mode & 0o777) == 0o644, oct(os.stat(path).st_mode & 0o777))
        check("an unwritable target hands the records back for retry",
              flush_records(os.path.join(d, "nonexistent-subdir"), rows[:4]) == rows[:4])

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
        check("the default retention covers a month-long experiment", RETAIN_DAYS >= 30, RETAIN_DAYS)

        print("degraded-state reporting")
        _warned.clear()
        warn("x", "first", every=10000)
        n1 = _warned["x"]["n"]
        for _ in range(5):
            warn("x", "again", every=10000)
        check("repeats are counted, not lost", _warned["x"]["n"] == n1 + 5, _warned.get("x"))
        check("a condition stays flagged until it clears", "x" in _warned)
        recovered("x", "cleared")
        check("recovery clears the flag", "x" not in _warned)
        warn("y", "brief", every=0)
        warn("y", "brief", every=0)
        check("a zero interval re-logs every time rather than going quiet", _warned["y"]["n"] == 0)
    finally:
        if keep:
            print("\nfixtures kept in " + d)
        else:
            shutil.rmtree(d, ignore_errors=True)

    print("")
    if fails:
        print("selftest FAILED: " + ", ".join(fails))
        return 1
    print("selftest passed")
    return 0


def main():
    ap = argparse.ArgumentParser(description="1 Hz Valheim egress probe")
    ap.add_argument("--selftest", action="store_true",
                    help="every parser, drop rule and the write path, against fixtures; no nft, game or root needed")
    ap.add_argument("--keep-fixtures", action="store_true", help="with --selftest, leave the temp directory behind")
    ap.add_argument("--once", action="store_true",
                    help="print a single live sample (two counter reads a second apart) and exit")
    a = ap.parse_args()
    if a.selftest:
        return selftest(keep=a.keep_fixtures)
    if a.once:
        first, peers = read_table()
        if first is None:
            print(f"no counters: {peers}. Is the meter table installed? ({NFT_HELPER} ensure)")
            return 1
        t0 = time.monotonic()
        time.sleep(1.0)
        second, peers = read_table()
        if second is None:
            print(f"second read failed: {second if second else peers}")
            return 1
        wall = time.time()
        players, why, generated = read_status(STATUS, wall)
        if players is None:
            print(f"note: no usable player count -- {why}")
        ms, pc, st = a2s_probe(("127.0.0.1", QUERY_PORT))
        if st != "ok":
            print(f"note: A2S probe {st}")
        sq, sq_why = read_send_queue(GAME_PORT)
        if sq is None:
            print(f"note: no send-queue reading -- {sq_why}")
        eff_n, eff_age, src = choose_n(pc, 0.0 if pc is not None else None,
                                       players, (wall - generated) if generated else None)
        print(f"note: player count {eff_n} from {src}, {eff_age}s old" if eff_n is not None
              else "note: no usable player count from either source")
        rec, drop = build_record(
            wall, time.monotonic() - t0,
            (first["game_tx"][0], first["game_tx"][1], first["game_rx"][0], first["game_rx"][1]),
            (second["game_tx"][0], second["game_tx"][1], second["game_rx"][0], second["game_rx"][1]),
            eff_n, eff_age, len(peers), sq, (ms, st),
            query_tx=second.get("query_tx", (0, 0))[0] - first.get("query_tx", (0, 0))[0],
            v6=second.get("game_tx6", (0, 0))[0] - first.get("game_tx6", (0, 0))[0])
        print(json.dumps(rec, separators=(",", ":")) if rec else f"sample refused: {drop}")
        return 0
    try:
        run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
