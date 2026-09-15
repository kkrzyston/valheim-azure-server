#!/usr/bin/env python3
"""valheim-stall-calib.py -- player-gated, self-stopping calibration sampler for the `rq` proxy.

Purpose: answer one question -- does valheim-egress-probe.py's `rq` (UDP rx_queue depth on the
game port, from /proc/net/udp) actually rise during a known, timestamped, server-induced
main-loop stall? World saves are that known stall: the journal logs

  "PrepareSave: ZDOExtraData.PrepareSave done [NNNms]"

every ~30 minutes (the default 1800s save interval), 151-159ms observed on this box. `rq` only
rises if packets arrive AND the main loop is too stalled to drain them -- so it can only be
calibrated while players are actually connected and sending. A one-off run on an empty server
(the predecessor to this script, which sat at 0 players all day) cannot calibrate it at all.

This version fixes that by being player-gated, like valheim-egress-probe.py: idle (near-zero
cost) while the server is empty, high-rate only while `players > 0`, and it counts save windows
that occurred while someone was connected. Once it has counted enough of them it stops sampling
at high rate permanently (state persisted to disk, so a restart or reboot does not re-arm it) --
this is a calibration exercise with a defined endpoint, not a permanent probe.

Why 10 Hz for rq/sq: the stall is ~150-190ms. A period much longer than that has a material
chance of missing it, or of not bracketing it with a clean before/during/after triple. At 10 Hz
(100ms period) a 150ms+ stall is reliably covered by at least one sample squarely inside it, with
baseline samples immediately either side. Below ~10 Hz the instrument stops being able to see the
thing it exists to see -- a 5 Hz sampler has a 200ms period, so a 155ms stall spans under one
sample on average and can be missed by phase alone. rq/sq come from /proc/net/udp, two tiny file
reads with no syscall round trip to anything -- see the module's own measurement note below for
the empirical cost.

A2S is sampled far less often than rq/sq, and less often than the predecessor script used: unlike
the /proc reads, an A2S query is a real UDP round trip the game server's query thread has to
answer, so hammering it is exactly the self-perturbation the brief warns against. rq is the signal
this run actually needs calibrated (rtt is already all-but-ruled-out by valheim-egress-probe.py's
own R6), so A2S here exists only as a secondary corroborating signal and is throttled hard: it
starts at A2S_HZ_START (1 Hz) and backs off by half on consecutive timeouts, same controller as
the predecessor script, floor A2S_HZ_MIN.

Output: JSONL to $STALLCALIB_DIR/stallcalib-YYYY-MM-DD.jsonl (default /var/lib/valheim-status),
one file per UTC day, one line per KEPT sample (a sample that fails a sanity check is dropped,
never fabricated -- same convention as valheim-egress-probe.py). Fields per line:
  t     wall clock (UTC ISO8601, ms precision)
  mono  monotonic clock, seconds, float -- the only clock rows are aligned on for dt/latency math
  dt    seconds since the previous KEPT sample (monotonic-derived; a gap here is itself a finding)
  n     player count in effect when the row was sampled (from status.json, the same gate source
        valheim-egress-probe.py uses)
  rq    UDP rx_queue depth on the game port, or field omitted if unreadable (never written as 0)
  sq    UDP tx_queue depth on the game port, or field omitted if unreadable
  rtt_ms  A2S round-trip in ms, present only on ticks where A2S was actually queried this cycle
  rtt_status  "ok" | "timeout" | "error:..." | absent (not an A2S tick)

State (survives restart/reboot): $STALLCALIB_DIR/stallcalib-state.json --
  {"save_windows_captured": N, "journal_cursor": "...", "done": bool, "done_at": "..."}
Once "done" is true the service idles forever (cheap status polling only, no sampling, no writes)
until the state file is removed by an operator who wants to re-arm it.

Drop accounting: every dropped sample increments an in-memory counter by reason, surfaced on
every heartbeat log line (never silently discarded, never fabricated/interpolated).

Retention: bounded on BOTH age (STALLCALIB_RETAIN_DAYS, default 14) and total on-disk size
(STALLCALIB_MAX_MB, default 60) -- whole day-files are unlinked, oldest first, whenever either
bound is exceeded. Enforced on every flush and on startup, so it holds across kill/restart. No
personal data of any kind is recorded: queue depths, timings and player *counts* only, never
addresses or player names.

CPU cost, measured (see PR write-up for the full note): the predecessor one-off ran continuously
at 10 Hz rq/sq with a 5 Hz-backing-off A2S for ~83 minutes with 0 players online and used ~12s of
CPU time against a wall-clock 5001s window -- about 0.24% of one core. That number is a lower
bound for a gated run since A2S here runs at a fifth of that rate, but is a reasonable estimate of
the loop's own overhead since /proc reads dominate cost at 0 players; see the PR note for an
in-session measurement while players were online.

Usage:
  sudo python3 valheim-stall-calib.py                  # run under systemd (see the .service unit)
  python3 valheim-stall-calib.py --selftest             # parser/state/retention checks, no root
  python3 valheim-stall-calib.py --status                # print state.json and exit
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
from datetime import datetime, timedelta, timezone


def log(msg):
    print("valheim-stall-calib: " + msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- config (env only, same
# convention as valheim-egress-probe.py: .strip() or default, never os.environ.get's own default,
# because an EnvironmentFile= line present-but-empty sets "" and get()'s default never fires.)
def _env(name, default):
    return os.environ.get(name, "").strip() or default


def _env_num(name, default, cast=float):
    raw = _env(name, None)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        log(f"WARNING: {name}={raw!r} is not a number -- falling back to {default}.")
        return default


STATUS = _env("STALLCALIB_STATUS", "/var/www/valheim/status.json")
LIB = _env("STALLCALIB_DIR", "/var/lib/valheim-status")
STATE_PATH_DEFAULT = os.path.join(LIB, "stallcalib-state.json")
GAME_PORT = _env_num("VALHEIM_GAME_PORT", 2456, int)
QUERY_PORT = _env_num("VALHEIM_QUERY_PORT", 2457, int)
VALHEIM_UNIT = _env("STALLCALIB_GAME_UNIT", "valheim.service")

SAMPLE_HZ = _env_num("STALLCALIB_SAMPLE_HZ", 10.0)          # rq/sq rate -- see module docstring
SAMPLE_PERIOD = 1.0 / SAMPLE_HZ
A2S_HZ_START = _env_num("STALLCALIB_A2S_HZ_START", 1.0)     # throttled hard -- rq is the signal
A2S_HZ_MIN = _env_num("STALLCALIB_A2S_HZ_MIN", 0.2)
A2S_TIMEOUT = _env_num("STALLCALIB_A2S_TIMEOUT", 0.4)
A2S_CONSEC_TIMEOUT_BACKOFF = _env_num("STALLCALIB_A2S_BACKOFF_N", 2, int)

DT_MIN = _env_num("STALLCALIB_DT_MIN", 0.0)
DT_MAX = _env_num("STALLCALIB_DT_MAX", 5.0)

STATUS_MAX_AGE = _env_num("STALLCALIB_STATUS_MAX_AGE", 180.0)   # gate only
IDLE_POLL_SEC = _env_num("STALLCALIB_IDLE_POLL_SEC", 10.0)
HEARTBEAT_SEC = _env_num("STALLCALIB_HEARTBEAT_SEC", 1800.0)

# Target: see the PR write-up for the full justification. Short version -- rq's baseline is
# currently flat zero (0 players all day produced not one non-zero rq reading), so a single
# non-zero excursion during a save window is already informative; it is not a mean-comparison
# problem the way rtt's INCONCLUSIVE-leaning-BLIND verdict was, which is why this needs far fewer
# than the ~10+ windows the rtt analysis wanted. TARGET_SAVE_WINDOWS independent windows (a save
# tick that occurred while >0 players were connected and being sampled) rules out a single
# coincidental packet-timing artifact while keeping the ask small: at one save every ~30 minutes,
# 5 windows is ~2.5 hours of cumulative play time, realistic within a few evening sessions.
TARGET_SAVE_WINDOWS = _env_num("STALLCALIB_TARGET_WINDOWS", 5, int)

RETAIN_DAYS = _env_num("STALLCALIB_RETAIN_DAYS", 14, int)
MAX_MB = _env_num("STALLCALIB_MAX_MB", 60.0)

JOURNAL_POLL_SEC = _env_num("STALLCALIB_JOURNAL_POLL_SEC", 30.0)
JOURNAL_MARKER = "PrepareSave: ZDOExtraData.PrepareSave done"

BUF_MAX = _env_num("STALLCALIB_BUF_MAX", 6000, int)


# ---------------------------------------------------------------- /proc/net/udp (kept standalone
# / independent of valheim-egress-probe.py deliberately -- see that script's ownership-fence note
# this one inherits: this script must never import or depend on the running probe's file).
_UDP_RE = re.compile(r"^\s*\d+:\s+[0-9A-Fa-f]+:([0-9A-Fa-f]{4})\s+\S+\s+\S+\s+"
                     r"([0-9A-Fa-f]+):([0-9A-Fa-f]+)")


def parse_udp_queue(text, port):
    """(tx_queue, rx_queue) summed across every UDP socket bound to `port`. None if nothing
    bound -- an unreadable measurement must never masquerade as a measured zero."""
    tx, rx, found = 0, 0, False
    for line in text.splitlines():
        m = _UDP_RE.match(line)
        if not m or int(m.group(1), 16) != port:
            continue
        found = True
        tx += int(m.group(2), 16)
        rx += int(m.group(3), 16)
    return (tx, rx) if found else None


def read_socket_queues(port, paths=("/proc/net/udp", "/proc/net/udp6")):
    tx, rx, found = 0, 0, False
    for path in paths:
        try:
            with open(path) as f:
                v = parse_udp_queue(f.read(), port)
        except Exception:
            continue
        if v is not None:
            tx += v[0]
            rx += v[1]
            found = True
    return (tx, rx) if found else (None, None)


# ---------------------------------------------------------------- A2S round trip
_A2S_QUERY = b"\xff\xff\xff\xffTSource Engine Query\x00"


def a2s_probe(addr, timeout=A2S_TIMEOUT):
    """(rtt_ms, status). status is 'ok', 'timeout' or 'error:...'. A timeout IS data -- never
    swallowed, same convention as valheim-egress-probe.py's a2s_probe."""
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        t0 = time.monotonic()
        s.sendto(_A2S_QUERY, addr)
        d, _ = s.recvfrom(4096)
        if d[4:5] == b"A":
            s.sendto(_A2S_QUERY + d[5:9], addr)
            d, _ = s.recvfrom(4096)
        return round((time.monotonic() - t0) * 1000, 1), "ok"
    except socket.timeout:
        return None, "timeout"
    except Exception as e:
        return None, f"error:{e!r}"
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass


class A2SRateController:
    """Starts at A2S_HZ_START. Halves (down to A2S_HZ_MIN) on A2S_CONSEC_TIMEOUT_BACKOFF
    consecutive timeouts. Never speeds back up mid-run."""

    def __init__(self, hz_start, hz_min):
        self.hz = hz_start
        self.hz_min = hz_min
        self._consec_timeout = 0

    def period(self):
        return 1.0 / self.hz

    def record(self, status):
        if status == "timeout":
            self._consec_timeout += 1
            if self._consec_timeout >= A2S_CONSEC_TIMEOUT_BACKOFF and self.hz > self.hz_min:
                old = self.hz
                self.hz = max(self.hz_min, self.hz / 2)
                log(f"A2S backoff: {self._consec_timeout} consecutive timeouts at {old:.2f} Hz "
                    f"-- halving to {self.hz:.2f} Hz")
                self._consec_timeout = 0
        else:
            self._consec_timeout = 0


# ---------------------------------------------------------------- the player-count gate (same
# shape/behaviour as valheim-egress-probe.py's read_status -- kept standalone per the ownership
# fence, not imported)
def read_status(path, now, max_age=None):
    """(players, why, generated). players is an int, or None when the file cannot be trusted, in
    which case the caller treats it as nobody online."""
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
    if isinstance(n, bool) or not isinstance(n, int) or n < 0:
        return None, f"status.json has no usable players.count ({n!r})", gen
    return n, "", gen


# ---------------------------------------------------------------- state (persists the
# calibration's progress and "done" flag across restarts/reboots)
def load_state(path):
    try:
        with open(path) as f:
            st = json.load(f)
    except FileNotFoundError:
        return {"save_windows_captured": 0, "journal_cursor": None, "done": False}
    except Exception as e:
        log(f"WARNING: could not read state file {path} ({e!r}) -- starting fresh at 0 windows")
        return {"save_windows_captured": 0, "journal_cursor": None, "done": False}
    if not isinstance(st, dict):
        return {"save_windows_captured": 0, "journal_cursor": None, "done": False}
    st.setdefault("save_windows_captured", 0)
    st.setdefault("journal_cursor", None)
    st.setdefault("done", False)
    return st


def save_state(path, state):
    """Atomic write (tmp + rename) so a crash mid-write never leaves a half-written state file
    that load_state has to guess about."""
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".stallcalib-state-", dir=d)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception as e:
        log(f"WARNING: could not persist state to {path}: {e!r}")


# ---------------------------------------------------------------- journal: counting save windows
# that occurred while players were connected. journalctl --show-cursor gives a resumable cursor so
# a restart mid-run does not recount (or miss) a save tick.
def count_new_save_windows(unit, cursor):
    """(count_of_new_PrepareSave_lines, new_cursor_or_None, error_or_None).

    One journalctl call per poll (JOURNAL_POLL_SEC, default 30s) -- far cheaper than the 10 Hz
    /proc reads, and only needed often enough to not miss a save relative to a ~30-minute save
    interval. Uses a cursor rather than a wall-clock window so a slow poll or a service restart
    never double-counts or silently skips a line."""
    cmd = ["journalctl", "-u", unit, "--no-pager", "-o", "cat", "--show-cursor"]
    if cursor:
        cmd += ["--after-cursor", cursor]
    else:
        cmd += ["--since", "5 minutes ago"]     # first run: do not replay the whole journal
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as e:
        return 0, cursor, f"exec:{e!r}"
    if p.returncode != 0:
        return 0, cursor, f"exit{p.returncode}: {(p.stderr or '').strip()[:200]}"
    lines = p.stdout.splitlines()
    new_cursor = cursor
    count = 0
    for line in lines:
        if line.startswith("-- cursor: "):
            new_cursor = line[len("-- cursor: "):].strip()
            continue
        if JOURNAL_MARKER in line:
            count += 1
    return count, new_cursor, None


# ---------------------------------------------------------------- output file + retention
_NAME_RE = re.compile(r"^stallcalib-(\d{4})-(\d{2})-(\d{2})\.jsonl$")


def out_path(lib, dt_utc):
    return os.path.join(lib, "stallcalib-%s.jsonl" % dt_utc.strftime("%Y-%m-%d"))


def flush_records(lib, records):
    """Append, grouped by UTC day. Returns records it could NOT write (kept buffered, retried),
    same discipline as valheim-egress-probe.py's flush_records -- a write failure must never
    silently discard a sample."""
    if not records:
        return []
    by_day = {}
    for r in records:
        by_day.setdefault(out_path(lib, datetime.fromisoformat(r["t"].replace("Z", "+00:00"))),
                          []).append(r)
    unwritten = []
    for path, rows in sorted(by_day.items()):
        fresh = not os.path.exists(path)
        try:
            with open(path, "a") as f:
                for r in rows:
                    f.write(json.dumps(r, separators=(",", ":")) + "\n")
            if fresh:
                os.chmod(path, 0o644)
        except Exception as e:
            log(f"WARNING: cannot write {len(rows)} sample(s) to {path}: {e!r} -- holding in memory")
            unwritten.extend(rows)
    return unwritten


def prune(lib, now, retain_days=None, max_mb=None):
    """Unlink whole day-files, oldest first, until BOTH the age bound and the size bound are
    satisfied. Age comes from the filename (a file being appended to has today's mtime regardless
    of which day it holds); size is the sum of stallcalib-*.jsonl bytes on disk. This is the fix
    for the unbounded-growth class this session already found in a sibling script (relay-check) --
    retention here is enforced on every flush, not just at prune time, and holds across
    kill/restart because it is recomputed from the files themselves, not from in-memory state."""
    retain_days = RETAIN_DAYS if retain_days is None else retain_days
    max_mb = MAX_MB if max_mb is None else max_mb
    cutoff = (datetime.fromtimestamp(now, timezone.utc) - timedelta(days=retain_days)).date()
    dropped = []
    try:
        names = sorted(n for n in os.listdir(lib) if _NAME_RE.match(n))
    except Exception as e:
        log(f"WARNING: cannot list {lib} to prune old samples: {e!r}")
        return dropped

    def day_of(name):
        m = _NAME_RE.match(name)
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()

    # age bound first
    keep = []
    for name in names:
        try:
            d = day_of(name)
        except ValueError:
            continue
        if d < cutoff:
            try:
                os.unlink(os.path.join(lib, name))
                dropped.append(name)
            except Exception as e:
                log(f"WARNING: could not unlink {name}: {e!r}")
        else:
            keep.append(name)

    # size bound: drop oldest-first (by filename, which sorts chronologically) until under cap
    def total_mb(names_):
        total = 0
        for n in names_:
            try:
                total += os.path.getsize(os.path.join(lib, n))
            except OSError:
                pass
        return total / (1024 * 1024)

    keep.sort()
    while keep and total_mb(keep) > max_mb:
        victim = keep.pop(0)
        try:
            os.unlink(os.path.join(lib, victim))
            dropped.append(victim)
        except Exception as e:
            log(f"WARNING: could not unlink {victim} while enforcing the {max_mb}MB cap: {e!r}")
            break

    if dropped:
        log(f"pruned {len(dropped)} file(s) enforcing retention "
            f"({retain_days}d / {max_mb:g}MB): {', '.join(sorted(dropped))}")
    return dropped


# ---------------------------------------------------------------- main loop
def run(state_path=None):
    state_path = state_path or STATE_PATH_DEFAULT
    os.makedirs(LIB, exist_ok=True)
    state = load_state(state_path)
    ctrl = A2SRateController(A2S_HZ_START, A2S_HZ_MIN)

    stats = {"written": 0, "dt": 0, "overflow": 0}
    buf = []
    day = None
    last_mono = None
    next_a2s_mono = 0.0
    next_journal_poll = 0.0
    next_beat = time.monotonic() + HEARTBEAT_SEC
    next_tick = time.monotonic()
    last_prune_wall = 0.0
    was_sampling = False

    log(f"started: target {TARGET_SAVE_WINDOWS} save window(s) with players connected, "
        f"{state['save_windows_captured']} already captured, done={state['done']}, "
        f"{SAMPLE_HZ:g} Hz rq/sq, A2S starting at {ctrl.hz:g} Hz, retention "
        f"{RETAIN_DAYS}d/{MAX_MB:g}MB, out_dir={LIB}")
    if state["done"]:
        log("calibration already complete (see stallcalib-state.json) -- idling only; remove "
            "the state file to re-arm")

    def heartbeat(msg):
        log(f"heartbeat: {msg}; {stats['written']:,} samples written, {len(buf)} buffered, "
            f"{state['save_windows_captured']}/{TARGET_SAVE_WINDOWS} save windows captured"
            + (f"; dropped dt={stats['dt']}" if stats["dt"] else "; no samples dropped")
            + (f"; overflow={stats['overflow']}" if stats["overflow"] else ""))

    while True:
        mono, wall = time.monotonic(), time.time()

        if mono >= next_beat:
            next_beat = mono + HEARTBEAT_SEC
            heartbeat("done" if state["done"] else ("sampling" if was_sampling else "idle"))

        if wall - last_prune_wall >= 3600.0:
            last_prune_wall = wall
            prune(LIB, wall)

        if state["done"]:
            if buf:
                buf = flush_records(LIB, buf)
            time.sleep(IDLE_POLL_SEC)
            continue

        players, why, generated = read_status(STATUS, wall)
        if players is None:
            players = 0
        sampling = players > 0

        if sampling and not was_sampling:
            log(f"{players} player(s) online -- sampling at {SAMPLE_HZ:g} Hz")
        elif was_sampling and not sampling:
            log("server empty -- sampling stopped")
            if buf:
                buf = flush_records(LIB, buf)
            last_mono = None
            heartbeat("idle")
        was_sampling = sampling

        if not sampling:
            time.sleep(IDLE_POLL_SEC)
            next_tick = time.monotonic()
            continue

        # --- count save windows that occurred while someone was connected, on a slow poll
        if mono >= next_journal_poll:
            next_journal_poll = mono + JOURNAL_POLL_SEC
            n_new, new_cursor, err = count_new_save_windows(VALHEIM_UNIT, state.get("journal_cursor"))
            if err:
                log(f"WARNING: could not poll the journal for save windows ({err}) -- "
                    f"window count may undercount this stint")
            else:
                state["journal_cursor"] = new_cursor
                if n_new:
                    state["save_windows_captured"] += n_new
                    log(f"captured {n_new} save window(s) with players connected "
                        f"({state['save_windows_captured']}/{TARGET_SAVE_WINDOWS} total)")
                    save_state(state_path, state)
                if state["save_windows_captured"] >= TARGET_SAVE_WINDOWS:
                    state["done"] = True
                    state["done_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    save_state(state_path, state)
                    if buf:
                        buf = flush_records(LIB, buf)
                    log(f"calibration complete: {state['save_windows_captured']} save window(s) "
                        f"captured with players connected -- high-rate sampling stops now; "
                        f"idling from here on")
                    continue

        # --- one 10 Hz sample: rq/sq always, A2S throttled
        tx, rx = read_socket_queues(GAME_PORT)
        rtt_ms, rtt_status = None, None
        if mono >= next_a2s_mono:
            rtt_ms, rtt_status = a2s_probe(("127.0.0.1", QUERY_PORT))
            ctrl.record(rtt_status)
            next_a2s_mono = mono + ctrl.period()

        now_wall_dt = datetime.now(timezone.utc)
        dt = None if last_mono is None else round(mono - last_mono, 4)
        if last_mono is not None and not (DT_MIN <= dt <= DT_MAX):
            stats["dt"] += 1
            last_mono = mono
        else:
            row = {"t": now_wall_dt.isoformat(timespec="milliseconds"), "mono": round(mono, 4),
                   "dt": dt, "n": players}
            if rx is not None:
                row["rq"] = rx
            if tx is not None:
                row["sq"] = tx
            if rtt_status is not None:
                row["rtt_status"] = rtt_status
                if rtt_ms is not None:
                    row["rtt_ms"] = rtt_ms
            buf.append(row)
            stats["written"] += 1
            last_mono = mono

            if len(buf) >= 150:      # ~15s of samples at 10 Hz -- same cadence as the egress probe
                buf = flush_records(LIB, buf)
                if len(buf) > BUF_MAX:
                    lost = len(buf) - BUF_MAX
                    stats["overflow"] += lost
                    buf = buf[lost:]
                    log(f"WARNING: sample buffer full ({BUF_MAX}) -- discarded {lost} oldest "
                        f"sample(s), {stats['overflow']} lost in total")

        next_tick += SAMPLE_PERIOD
        slack = next_tick - time.monotonic()
        if slack < -2.0:
            last_mono = None
            next_tick = time.monotonic() + SAMPLE_PERIOD
            slack = SAMPLE_PERIOD
        time.sleep(max(0.0, slack))


# ---------------------------------------------------------------- selftest
def selftest(keep=False):
    fails = []

    def check(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name +
              (("  -- " + str(detail)) if not cond and detail else ""))
        if not cond:
            fails.append(name)

    d = tempfile.mkdtemp(prefix="stallcalib-selftest-")
    try:
        print("udp queue parsing")
        sample = (
            "  sl  local_address rem_address   st tx_queue:rx_queue tr tm->when retrnsmt uid timeout inode\n"
            "  123: 0100007F:0990 00000000:0000 07 0000000A:0000001E 00:00000000 00000000     0        0 0\n"
        )
        check("parse_udp_queue finds bound port", parse_udp_queue(sample, 0x0990) == (10, 30))
        check("parse_udp_queue misses unbound port", parse_udp_queue(sample, 0x0991) is None)

        print("A2S rate controller")
        ctrl = A2SRateController(5.0, 1.0)
        for _ in range(A2S_CONSEC_TIMEOUT_BACKOFF):
            ctrl.record("timeout")
        check("halves on consecutive timeouts", ctrl.hz == 2.5)
        ctrl.record("ok")
        check("resets consec-timeout counter on success", ctrl._consec_timeout == 0)
        ctrl2 = A2SRateController(1.0, 1.0)
        for _ in range(10):
            ctrl2.record("timeout")
        check("never drops below the floor", ctrl2.hz == 1.0)

        print("player-count gate (mirrors valheim-egress-probe.py's read_status)")
        now = time.time()
        sp = os.path.join(d, "status.json")

        def st_write(obj):
            with open(sp, "w") as f:
                json.dump(obj, f)

        st_write({"generated": int(now), "players": {"count": 2}})
        check("fresh status yields the count", read_status(sp, now)[0] == 2)
        st_write({"generated": int(now - 600), "players": {"count": 2}})
        check("stale status is refused", read_status(sp, now)[0] is None)
        st_write({"generated": int(now), "players": {"count": True}})
        check("a boolean count is refused", read_status(sp, now)[0] is None)
        check("missing status is refused", read_status(os.path.join(d, "nope.json"), now)[0] is None)

        print("state persistence")
        stp = os.path.join(d, "state.json")
        st0 = load_state(stp)
        check("fresh state starts at 0 windows, not done", st0 == {"save_windows_captured": 0,
              "journal_cursor": None, "done": False})
        st0["save_windows_captured"] = 3
        st0["journal_cursor"] = "s=abc;i=1"
        save_state(stp, st0)
        st1 = load_state(stp)
        check("state round-trips through save/load", st1["save_windows_captured"] == 3 and
              st1["journal_cursor"] == "s=abc;i=1")
        with open(stp, "w") as f:
            f.write("{not json")
        check("corrupt state file does not crash -- restarts at 0, not done",
              load_state(stp) == {"save_windows_captured": 0, "journal_cursor": None, "done": False})

        print("journal cursor parsing")

        class FakeCompleted:
            def __init__(self, out, rc=0):
                self.stdout, self.returncode, self.stderr = out, rc, ""

        import unittest.mock as mock
        with mock.patch("subprocess.run", return_value=FakeCompleted(
                "some log line\n"
                "09/15/2026 10:16:42: PrepareSave: ZDOExtraData.PrepareSave done [159ms]\n"
                "another line\n"
                "-- cursor: s=abc;i=42\n")):
            n, cur, err = count_new_save_windows("valheim.service", None)
            check("counts exactly one PrepareSave line", n == 1, n)
            check("captures the trailing cursor", cur == "s=abc;i=42", cur)
            check("no error on a clean run", err is None)
        with mock.patch("subprocess.run", return_value=FakeCompleted("", rc=1)):
            n, cur, err = count_new_save_windows("valheim.service", "s=old")
            check("a journalctl failure is reported, not swallowed as zero-with-no-error",
                  n == 0 and err is not None, (n, err))
            check("cursor is unchanged on failure", cur == "s=old")
        with mock.patch("subprocess.run", return_value=FakeCompleted(
                "PrepareSave: ZDOExtraData.PrepareSave done [151ms]\n"
                "PrepareSave: ZDOExtraData.PrepareSave done [149ms]\n"
                "-- cursor: s=abc;i=99\n")):
            n, cur, err = count_new_save_windows("valheim.service", "s=abc;i=42")
            check("counts multiple new windows since the last cursor", n == 2, n)

        print("write path")
        rows = [{"t": "2026-09-15T12:00:%02d.000+00:00" % k, "mono": 1.0 + k, "dt": 0.1, "n": 2,
                 "rq": 0, "sq": 0} for k in range(10)]
        check("all rows written", flush_records(d, rows) == [])
        p = out_path(d, datetime(2026, 9, 15))
        check("file exists at the expected path", os.path.exists(p))
        with open(p) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        check("every row landed, unchanged", len(lines) == 10 and lines[0]["rq"] == 0)
        check("second flush appends, does not rewrite", flush_records(d, rows[:2]) == [] and
              sum(1 for _ in open(p)) == 12)
        check("an unwritable target hands rows back for retry",
              flush_records(os.path.join(d, "no-such-dir"), rows[:3]) == rows[:3])

        print("retention: age bound")
        for name in ("stallcalib-2000-01-01.jsonl", "stallcalib-2000-01-02.jsonl",
                     "stallcalib-2026-13-45.jsonl", "notes.jsonl"):
            with open(os.path.join(d, name), "w") as f:
                f.write("x" * 100)
        dropped = set(prune(d, time.time(), retain_days=7, max_mb=1000))
        check("old days unlinked by age", {"stallcalib-2000-01-01.jsonl",
              "stallcalib-2000-01-02.jsonl"} <= dropped, dropped)
        check("today's file survives", os.path.exists(p))
        check("an unparseable date is left alone", os.path.exists(os.path.join(d, "stallcalib-2026-13-45.jsonl")))
        check("a foreign file is never touched", os.path.exists(os.path.join(d, "notes.jsonl")))

        print("retention: size bound (THE lock this item exists for)")
        d2 = tempfile.mkdtemp(prefix="stallcalib-selftest-size-", dir=d)
        for i in range(5):
            with open(os.path.join(d2, f"stallcalib-2026-01-0{i+1}.jsonl"), "wb") as f:
                f.write(b"x" * (1024 * 1024))   # 1 MiB each, 5 MiB total
        dropped2 = prune(d2, time.time(), retain_days=3650, max_mb=2.0)
        remaining = [n for n in os.listdir(d2) if _NAME_RE.match(n)]
        total = sum(os.path.getsize(os.path.join(d2, n)) for n in remaining) / (1024 * 1024)
        check("oldest files are unlinked until under the MB cap", total <= 2.0, total)
        check("the newest file(s) survive, not the oldest",
              "stallcalib-2026-01-05.jsonl" in remaining, remaining)
        check("stallcalib-2026-01-01.jsonl (oldest) was the first evicted",
              "stallcalib-2026-01-01.jsonl" in dropped2, dropped2)

        print("dt sanity")
        check("DT bounds are sane for a 10 Hz sampler", DT_MIN == 0.0 and DT_MAX >= 1.0)
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
    ap = argparse.ArgumentParser(description="player-gated Valheim stall (rq) calibration sampler")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--keep-fixtures", action="store_true")
    ap.add_argument("--status", action="store_true", help="print the persisted state and exit")
    ap.add_argument("--state-path", default=None)
    a = ap.parse_args()
    if a.selftest:
        return selftest(keep=a.keep_fixtures)
    if a.status:
        st = load_state(a.state_path or STATE_PATH_DEFAULT)
        print(json.dumps(st, indent=2))
        return 0
    try:
        run(state_path=a.state_path)
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
