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
  {"save_windows_captured": N, "journal_cursor": "...", "done": bool, "done_at": "...",
   "windows": [{"at": "...", "players": N}, ...]}
Once "done" is true the service idles forever (cheap status polling only, no sampling, no writes)
until the state file is removed by an operator who wants to re-arm it.

Journal cursor is re-seeded at EVERY gate-open (every players 0->N transition), not just the
first-ever one. An idle gap between sessions (server empty, then a player reconnects) must never
let a stale cursor from before the gap sweep in that gap's journal history and credit its save
windows -- that happened once already (see PR write-up: an idle-period cursor swept ~3 hours of
empty-server saves and jumped the counter 3->8 in one poll, tripping a false "done"). Re-seeding
unconditionally on every gate-open closes that hole for good: only saves that occur after THIS
connection can ever be credited, no matter how long the server sat empty before it.

A window is only credited toward save_windows_captured if at least STALLCALIB_MIN_PLAYERS_FOR_WINDOW
(default 2) players were online for the whole interval since the last journal poll (approximated
as the minimum player count observed during that interval). Sampling itself still runs at n=1 --
that data remains useful context -- but a 1-player window is weak evidence for what this
calibration is trying to see: rq is quantized at 2112 bytes (one packet), and a single player's
inbound rate may simply be too low for a ~150-200ms stall to push a backlog past that quantum, so
a flat-zero 1-player window does not distinguish "no stall" from "stall too small to see at n=1".
Every CREDITED window's player count is recorded in state["windows"] for audit.

Drop accounting: every dropped sample increments an in-memory counter by reason, surfaced on
every heartbeat log line (never silently discarded, never fabricated/interpolated).

Retention: bounded on BOTH age (STALLCALIB_RETAIN_DAYS, default 14) and total on-disk size
(STALLCALIB_MAX_MB, default 60) -- whole day-files are unlinked, oldest first, whenever either
bound is exceeded. Enforced hourly (not on every flush -- prune() is only called from the
main-loop hourly timer), and effectively also at startup because that timer's initial state
guarantees its first firing is immediate. The file actively being appended to this cycle is never
evicted, even if it alone exceeds the size cap. No personal data of any kind is recorded: queue
depths, timings and player *counts* only, never addresses or player names.

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


def _positive(value, name, default):
    """Domain guard for values that must be > 0 (e.g. a rate used as 1/x). _env_num only catches
    cast failures, not a syntactically-valid-but-domain-invalid value like 0 or a negative number
    -- STALLCALIB_SAMPLE_HZ=0 would otherwise divide-by-zero at import time and crash the service
    before it ever logs anything useful."""
    if value <= 0:
        log(f"WARNING: {name}={value:g} must be positive -- falling back to {default:g}.")
        return default
    return value


STATUS = _env("STALLCALIB_STATUS", "/var/www/valheim/status.json")
LIB = _env("STALLCALIB_DIR", "/var/lib/valheim-status")
STATE_PATH_DEFAULT = os.path.join(LIB, "stallcalib-state.json")
GAME_PORT = _env_num("VALHEIM_GAME_PORT", 2456, int)
QUERY_PORT = _env_num("VALHEIM_QUERY_PORT", 2457, int)
VALHEIM_UNIT = _env("STALLCALIB_GAME_UNIT", "valheim.service")

SAMPLE_HZ = _positive(_env_num("STALLCALIB_SAMPLE_HZ", 10.0), "STALLCALIB_SAMPLE_HZ", 10.0)
SAMPLE_PERIOD = 1.0 / SAMPLE_HZ          # rq/sq rate -- see module docstring
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

# A save window only counts toward TARGET_SAVE_WINDOWS if at least this many players were online
# for the whole interval since the last journal poll -- see module docstring. Sampling still runs
# at n=1; only the counter is gated.
MIN_PLAYERS_FOR_WINDOW = _env_num("STALLCALIB_MIN_PLAYERS_FOR_WINDOW", 2, int)

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


def status_transition_message(why, last_why):
    """The log message for a *change* in read_status()'s 'why', or None if unchanged since the
    last tick. Isolates the transition-only logging policy (log on change, never spam every tick)
    so it is unit-testable without capturing stderr. Without this, a genuinely empty server and a
    broken/stale/corrupt status.json are indistinguishable in the logs -- 'why' was previously
    unpacked and discarded (silent skip on a broken collector)."""
    if why == last_why:
        return None
    if why:
        return f"status gate: {why}"
    if last_why:
        return "status gate: status.json OK again"
    return None


def dt_is_sane(dt):
    """True if a computed dt (seconds since the previous KEPT sample) falls within
    [DT_MIN, DT_MAX]. A sample outside this range is dropped rather than kept with an implausible
    delta (clock jump, long stall, restart). Extracted from run()'s inline check so it can be
    exercised directly instead of only via a constants-only assertion."""
    return DT_MIN <= dt <= DT_MAX


# ---------------------------------------------------------------- state (persists the
# calibration's progress and "done" flag across restarts/reboots)
def _fresh_state():
    return {"save_windows_captured": 0, "journal_cursor": None, "done": False, "windows": []}


def load_state(path):
    try:
        with open(path) as f:
            st = json.load(f)
    except FileNotFoundError:
        return _fresh_state()
    except Exception as e:
        log(f"WARNING: could not read state file {path} ({e!r}) -- starting fresh at 0 windows")
        return _fresh_state()
    if not isinstance(st, dict):
        return _fresh_state()
    st.setdefault("save_windows_captured", 0)
    st.setdefault("journal_cursor", None)
    st.setdefault("done", False)
    st.setdefault("windows", [])
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
        # No cursor yet -- this should be rare: reseed_journal_cursor_at_gate_open() seeds one at
        # gate-open (every players 0->N transition) precisely so this branch is not the normal
        # path. If it IS hit (e.g. the seed call itself failed), keep the blind spot as narrow as
        # possible: a save in the preceding JOURNAL_POLL_SEC is indistinguishable from one during
        # play, but a save from minutes before player connect is not -- so this must never be a
        # multi-minute window. (Previously hardcoded to "5 minutes ago", which could credit a
        # pre-connection save toward save_windows_captured; see item 1 of the PR#8 review.)
        cmd += ["--since", f"{max(1, int(JOURNAL_POLL_SEC))} seconds ago"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as e:
        return 0, cursor, f"exec:{e!r}"
    if p.returncode != 0:
        # journalctl --show-cursor exits 1 (not 0), with BOTH stdout and stderr empty, when the
        # filter matched nothing at all -- observed with `--since "5 minutes ago"` on a quiet
        # journal. That is "zero new save windows", not a failure: a real failure (bad unit name,
        # a corrupt journal) always produces stderr text. Treating the silent case as an error
        # would spam a warning every JOURNAL_POLL_SEC on a quiet server, which is exactly the
        # kind of noise that trains an operator to stop reading the log.
        if p.returncode == 1 and not p.stdout and not p.stderr:
            return 0, cursor, None
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


def credited_count(n_new, min_players_since_poll, threshold=None):
    """How many of `n_new` newly-observed PrepareSave lines should be credited toward
    save_windows_captured, given the minimum player count observed since the previous poll.

    A window counts only if at least `threshold` (STALLCALIB_MIN_PLAYERS_FOR_WINDOW, default 2)
    players were online for the ENTIRE interval since the last poll -- approximated here as the
    minimum player count sampled during that interval, since a save may land anywhere inside it.
    A single connected player is weak evidence for the stall this calibration is trying to see
    (rq is quantized at 2112 bytes/1 packet; one player's inbound rate may be too low to push a
    ~150-200ms stall's backlog past even that one quantum) -- so 1-player windows are still
    sampled (the data stays useful context) but never counted toward the target.

    Returns 0 if min_players_since_poll is None (no player-count sample landed in this interval,
    e.g. the gate opened and closed between polls)."""
    threshold = MIN_PLAYERS_FOR_WINDOW if threshold is None else threshold
    if min_players_since_poll is None:
        return 0
    return n_new if min_players_since_poll >= threshold else 0


def seed_journal_cursor(unit):
    """(cursor_or_None, error_or_None). Returns the journal cursor at "now" -- i.e. the position
    after 0 matched lines (`-n 0`) -- WITHOUT counting or skipping anything. Used to seed
    state['journal_cursor'] at gate-open time (see reseed_journal_cursor_at_gate_open) so a later
    --after-cursor poll can only ever see journal entries written after this call, never anything
    from before. Shares count_new_save_windows's "rc=1 with empty stdout+stderr means nothing
    matched" convention (an empty journal for this unit)."""
    cmd = ["journalctl", "-u", unit, "--no-pager", "-o", "cat", "--show-cursor", "-n", "0"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as e:
        return None, f"exec:{e!r}"
    if p.returncode != 0:
        if p.returncode == 1 and not p.stdout and not p.stderr:
            return None, None
        return None, f"exit{p.returncode}: {(p.stderr or '').strip()[:200]}"
    for line in p.stdout.splitlines():
        if line.startswith("-- cursor: "):
            return line[len("-- cursor: "):].strip(), None
    return None, None


def reseed_journal_cursor_at_gate_open(state, unit):
    """Called every time sampling starts (every players 0 -> >0 transition), UNCONDITIONALLY --
    not only on the first-ever gate-open. Mutates state in place and returns True if it (re)seeded
    a cursor (caller should persist state).

    History: PR#8 fixed the "on the very first invocation ever, journal_cursor is None" case
    (item 1 of that review) by seeding a cursor at gate-open, but only when journal_cursor was
    still None. That reintroduced the identical defect on the SECOND+ gate-open: a session that
    ran, disconnected leaving journal_cursor pointing at that session's end, then sat empty for
    hours before someone reconnected, would poll --after-cursor <the old cursor> on reconnect --
    sweeping the ENTIRE empty-server gap and crediting every save in it as "captured with players
    connected". This is exactly what happened on 2026-09-15: a ~3-hour idle gap got swept in one
    poll and jumped save_windows_captured 3->8, tripping a false "done" before the evening's real
    play session could be captured.

    The fix is to never trust an existing cursor across a gate transition: a save may only be
    credited if it occurred while the gate was OPEN, and the only way to guarantee that is to
    discard all journal history up to the moment of THIS gate-open, every time, regardless of
    whether a cursor already existed."""
    cursor, err = seed_journal_cursor(unit)
    if err:
        log(f"WARNING: could not seed journal cursor at gate-open ({err}) -- falling back to "
            f"the narrow {JOURNAL_POLL_SEC:g}s lookback on the next poll instead")
        return False
    if cursor:
        had_prior = state.get("journal_cursor") is not None
        state["journal_cursor"] = cursor
        log(("re-seeded" if had_prior else "seeded") +
            " journal cursor at gate-open -- discarding any prior/idle-period journal history; "
            "only save windows from this moment forward can be credited toward the target")
        return True
    return False


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


def prune(lib, now, retain_days=None, max_mb=None, active_name=None):
    """Unlink whole day-files, oldest first, until BOTH the age bound and the size bound are
    satisfied. Age comes from the filename (a file being appended to has today's mtime regardless
    of which day it holds); size is the sum of stallcalib-*.jsonl bytes on disk. This is the fix
    for the unbounded-growth class this session already found in a sibling script (relay-check) --
    retention here is enforced hourly by run()'s main loop, and holds across kill/restart because
    it is recomputed from the files themselves, not from in-memory state.

    `active_name` (basename only) is the file being actively appended to THIS cycle, e.g. today's
    stallcalib-YYYY-MM-DD.jsonl. It is never chosen as an eviction victim by the size bound, even
    if it alone exceeds max_mb -- oldest-first eviction with only one file on disk (today's, still
    being written) would otherwise delete live, in-progress data instead of shrinking anything."""
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
        evictable = [n for n in keep if n != active_name]
        if not evictable:
            log(f"WARNING: {total_mb(keep):.1f}MB exceeds the {max_mb:g}MB cap but the only "
                f"file(s) left are the one actively being appended to this cycle "
                f"({active_name!r}) -- leaving it in place rather than deleting live data")
            break
        victim = evictable[0]
        keep.remove(victim)
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
    last_why = None
    min_players_since_poll = None

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
            + (f"; overflow={stats['overflow']}" if stats["overflow"] else "")
            + (f"; status gate: {last_why}" if last_why else ""))

    while True:
        mono, wall = time.monotonic(), time.time()

        if mono >= next_beat:
            next_beat = mono + HEARTBEAT_SEC
            heartbeat("done" if state["done"] else ("sampling" if was_sampling else "idle"))

        if wall - last_prune_wall >= 3600.0:
            last_prune_wall = wall
            active = os.path.basename(out_path(LIB, datetime.fromtimestamp(wall, timezone.utc)))
            prune(LIB, wall, active_name=active)

        if state["done"]:
            if buf:
                buf = flush_records(LIB, buf)
            time.sleep(IDLE_POLL_SEC)
            continue

        players, why, generated = read_status(STATUS, wall)
        msg = status_transition_message(why, last_why)
        if msg:
            log(msg)
        last_why = why
        if players is None:
            players = 0
        sampling = players > 0

        if sampling and not was_sampling:
            log(f"{players} player(s) online -- sampling at {SAMPLE_HZ:g} Hz")
            if reseed_journal_cursor_at_gate_open(state, VALHEIM_UNIT):
                save_state(state_path, state)
            min_players_since_poll = None
        elif was_sampling and not sampling:
            log("server empty -- sampling stopped")
            if buf:
                buf = flush_records(LIB, buf)
            last_mono = None
            min_players_since_poll = None
            heartbeat("idle")
        was_sampling = sampling

        if not sampling:
            time.sleep(IDLE_POLL_SEC)
            next_tick = time.monotonic()
            continue

        min_players_since_poll = (players if min_players_since_poll is None
                                   else min(min_players_since_poll, players))

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
                    credited = credited_count(n_new, min_players_since_poll)
                    if credited:
                        state["save_windows_captured"] += credited
                        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        for _ in range(credited):
                            state["windows"].append({"at": now_iso, "players": min_players_since_poll})
                        log(f"captured {credited} save window(s) with >= {MIN_PLAYERS_FOR_WINDOW} "
                            f"player(s) connected ({state['save_windows_captured']}/"
                            f"{TARGET_SAVE_WINDOWS} total; min players in this interval: "
                            f"{min_players_since_poll})")
                    skipped = n_new - credited
                    if skipped:
                        log(f"saw {skipped} save window(s) but did NOT count them -- only "
                            f"{min_players_since_poll} player(s) online during this interval, "
                            f"below the {MIN_PLAYERS_FOR_WINDOW}-player threshold for counting")
                    if credited:
                        save_state(state_path, state)
                # Reset the per-interval player-count floor for the NEXT poll interval,
                # regardless of whether this poll found any new save lines.
                min_players_since_poll = players
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
        if last_mono is not None and not dt_is_sane(dt):
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
              "journal_cursor": None, "done": False, "windows": []})
        st0["save_windows_captured"] = 3
        st0["journal_cursor"] = "s=abc;i=1"
        save_state(stp, st0)
        st1 = load_state(stp)
        check("state round-trips through save/load", st1["save_windows_captured"] == 3 and
              st1["journal_cursor"] == "s=abc;i=1")
        with open(stp, "w") as f:
            f.write("{not json")
        check("corrupt state file does not crash -- restarts at 0, not done",
              load_state(stp) == {"save_windows_captured": 0, "journal_cursor": None,
                                   "done": False, "windows": []})

        print("journal cursor parsing")

        class FakeCompleted:
            def __init__(self, out, rc=0, err=""):
                self.stdout, self.returncode, self.stderr = out, rc, err

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
        with mock.patch("subprocess.run", return_value=FakeCompleted("", rc=1, err="No journal files were found.")):
            n, cur, err = count_new_save_windows("valheim.service", "s=old")
            check("a journalctl failure is reported, not swallowed as zero-with-no-error",
                  n == 0 and err is not None, (n, err))
            check("cursor is unchanged on failure", cur == "s=old")
        with mock.patch("subprocess.run", return_value=FakeCompleted("", rc=1, err="")):
            # Observed on the VM: journalctl --show-cursor exits 1 with BOTH stdout and stderr
            # empty when the filter (e.g. `--since "5 minutes ago"`) matched nothing at all. That
            # is "zero new save windows", not a failure -- see count_new_save_windows's own
            # comment. Getting this wrong means a warning every 30s on a quiet journal.
            n, cur, err = count_new_save_windows("valheim.service", "s=old")
            check("rc=1 with empty stdout AND stderr is 'nothing matched', not an error",
                  n == 0 and err is None, (n, err))
            check("cursor is preserved (nothing to advance it to)", cur == "s=old")
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

        print("dt sanity (real lock on dt_is_sane, not just a constants-only assertion)")
        check("a plausible dt at 10 Hz is sane", dt_is_sane(0.1))
        check("dt exactly at DT_MIN is accepted (inclusive lower bound)", dt_is_sane(DT_MIN))
        check("dt exactly at DT_MAX is accepted (inclusive upper bound)", dt_is_sane(DT_MAX))
        check("a negative dt (clock went backwards) is rejected", not dt_is_sane(-0.05))
        check("a dt beyond DT_MAX (implausible gap/stall) is rejected", not dt_is_sane(DT_MAX + 1.0))

        print("SAMPLE_HZ domain guard (STALLCALIB_SAMPLE_HZ=0 must not crash the process)")
        check("zero is rejected, falls back to the given default", _positive(0.0, "X", 10.0) == 10.0)
        check("a negative value is rejected", _positive(-5.0, "X", 10.0) == 10.0)
        check("a valid positive value passes through unchanged", _positive(2.5, "X", 10.0) == 2.5)
        env = dict(os.environ)
        env["STALLCALIB_SAMPLE_HZ"] = "0"
        # Load the module fresh (as a non-__main__ script via runpy, NOT --selftest again --
        # that would recurse forever since the child inherits STALLCALIB_SAMPLE_HZ=0 too) and
        # confirm the module-level SAMPLE_HZ/SAMPLE_PERIOD computation survived without a
        # ZeroDivisionError crash.
        probe = ("import runpy, sys; "
                 "ns = runpy.run_path(sys.argv[1]); "
                 "print(ns['SAMPLE_HZ'], ns['SAMPLE_PERIOD'])")
        r = subprocess.run([sys.executable, "-c", probe, os.path.abspath(__file__)],
                            env=env, capture_output=True, text=True, timeout=30)
        check("module import does not crash with STALLCALIB_SAMPLE_HZ=0 (falls back instead)",
              r.returncode == 0, (r.returncode, r.stderr[-300:]))
        check("the fallback SAMPLE_HZ is the positive default, not 0",
              r.stdout.split()[:1] == ["10.0"], r.stdout)

        print("status 'why' is logged on transition, not silently discarded (item 2)")
        check("no message when why is unchanged (both healthy)",
              status_transition_message("", "") is None)
        check("no message when why is unchanged (still broken, same reason)",
              status_transition_message("status.json is missing", "status.json is missing") is None)
        check("logs when status becomes broken",
              status_transition_message("status.json is missing", "") ==
              "status gate: status.json is missing")
        check("logs recovery back to healthy",
              status_transition_message("", "status.json is missing") ==
              "status gate: status.json OK again")

        print("journal cursor seeding at gate-open -- pre-connection saves must never be "
              "credited (item 1)")
        with mock.patch("subprocess.run", return_value=FakeCompleted("-- cursor: s=seed;i=5\n")) as m:
            cur, err = seed_journal_cursor("valheim.service")
            check("seed_journal_cursor returns the cursor from a 0-line query",
                  cur == "s=seed;i=5" and err is None, (cur, err))
            check("seed_journal_cursor asks for 0 lines (must not count anything)",
                  "-n" in m.call_args[0][0] and
                  m.call_args[0][0][m.call_args[0][0].index("-n") + 1] == "0", m.call_args)

        st_seed = {"save_windows_captured": 0, "journal_cursor": None, "done": False, "windows": []}
        with mock.patch("subprocess.run", return_value=FakeCompleted("-- cursor: s=seed;i=5\n")):
            changed = reseed_journal_cursor_at_gate_open(st_seed, "valheim.service")
        check("first-ever gate-open seeds the cursor",
              changed and st_seed["journal_cursor"] == "s=seed;i=5", st_seed)

        # THE lock this item exists for: a PrepareSave that happened while the server was still
        # empty (i.e. before the seeded cursor) must never be credited once the cursor is seeded.
        with mock.patch("subprocess.run", return_value=FakeCompleted(
                "PrepareSave: ZDOExtraData.PrepareSave done [151ms]\n-- cursor: s=post;i=6\n")) as m3:
            n, cur2, err2 = count_new_save_windows("valheim.service", st_seed["journal_cursor"])
            check("post-seed poll queries --after-cursor from the seeded point, not a wide window",
                  "--after-cursor" in m3.call_args[0][0], m3.call_args)
            check("a save after the seeded cursor IS counted (the instrument still works)",
                  n == 1 and err2 is None, (n, err2))

        print("journal cursor is RE-seeded on EVERY gate-open, not just the first-ever one -- "
              "this is THE fix for tonight's false-'done' bug (an idle gap between sessions must "
              "never let a stale cursor sweep that gap's saves once a player reconnects)")
        # A second (or Nth) gate-open arrives with a journal_cursor already set, left over from
        # the PREVIOUS session -- exactly the shape of the live incident: a player disconnected
        # at ~12:01, the server sat empty for ~3 hours, then someone reconnected at 14:56:54 with
        # the stale ~12:01 cursor still in state.
        st_stale = {"save_windows_captured": 3, "journal_cursor": "s=stale_from_prev_session;i=1",
                    "done": False, "windows": []}
        with mock.patch("subprocess.run",
                         return_value=FakeCompleted("-- cursor: s=fresh_at_reconnect;i=99\n")) as m4:
            changed4 = reseed_journal_cursor_at_gate_open(st_stale, "valheim.service")
        check("gate-open re-seeds EVEN THOUGH a journal_cursor already existed from a prior "
              "session -- THE assertion this whole item is for",
              changed4 and st_stale["journal_cursor"] == "s=fresh_at_reconnect;i=99", st_stale)
        check("the reseed call itself still asks for 0 lines (must not count anything)",
              "-n" in m4.call_args[0][0] and
              m4.call_args[0][0][m4.call_args[0][0].index("-n") + 1] == "0", m4.call_args)

        # End-to-end shape of the live incident: with the cursor now pointing at the moment of
        # reconnect, a poll for new save windows must query --after-cursor from THAT fresh point,
        # so an idle-gap save (one that happened before reconnect, while the server sat empty)
        # is structurally unreachable -- it is before the cursor journalctl is told to start from.
        with mock.patch("subprocess.run", return_value=FakeCompleted(
                "-- cursor: s=fresh_at_reconnect;i=99\n")) as m5:   # no PrepareSave lines: the
            # idle-gap saves are before this cursor and journalctl --after-cursor cannot see them
            n5, cur5, err5 = count_new_save_windows("valheim.service", st_stale["journal_cursor"])
            args5 = m5.call_args[0][0]
            check("poll after re-seed queries --after-cursor from the FRESH (post-reconnect) "
                  "cursor, not the stale pre-idle-gap one",
                  "--after-cursor" in args5 and
                  args5[args5.index("--after-cursor") + 1] == "s=fresh_at_reconnect;i=99", args5)
            check("no idle-gap windows are credited (journalctl only sees post-reconnect entries)",
                  n5 == 0 and err5 is None, (n5, err5))

        print("counted-window player-count gate: >= STALLCALIB_MIN_PLAYERS_FOR_WINDOW players "
              "required for the WHOLE interval to count a window (item 3) -- a lone player is "
              "still sampled but must never move save_windows_captured")
        check("default threshold is 2", MIN_PLAYERS_FOR_WINDOW == 2, MIN_PLAYERS_FOR_WINDOW)
        check("1 player online for the interval: window is seen but NOT credited",
              credited_count(1, 1) == 0)
        check("2 players (the default threshold) online: window IS credited",
              credited_count(1, 2) == 1)
        check("more than the threshold: still credited (>= not ==)", credited_count(1, 5) == 1)
        check("multiple new windows in one poll are all credited/withheld together",
              credited_count(3, 2) == 3 and credited_count(3, 1) == 0)
        check("no player-count sample landed this interval (None) -- never credited, not even "
              "with a low explicit threshold", credited_count(1, None, threshold=1) == 0)
        check("threshold is configurable via the credited_count() argument",
              credited_count(1, 1, threshold=1) == 1 and credited_count(1, 1, threshold=2) == 0)

        # Defense in depth: even if seeding itself failed (cursor still None when polling), the
        # blind-spot window must be bounded to JOURNAL_POLL_SEC, never the old 5-minute fallback.
        with mock.patch("subprocess.run", return_value=FakeCompleted("")) as m4:
            count_new_save_windows("valheim.service", None)
            args = m4.call_args[0][0]
            since_val = args[args.index("--since") + 1]
            check("no-cursor fallback window is bounded to JOURNAL_POLL_SEC, not a 5-minute "
                  "pre-connection blind spot",
                  "5 minutes" not in since_val and str(int(JOURNAL_POLL_SEC)) in since_val,
                  since_val)

        print("retention: never evict the actively-written file, even if it alone exceeds the "
              "cap (item 5)")
        d3 = tempfile.mkdtemp(prefix="stallcalib-selftest-active-", dir=d)
        active_name = "stallcalib-2026-02-01.jsonl"
        with open(os.path.join(d3, active_name), "wb") as f:
            f.write(b"x" * (3 * 1024 * 1024))    # 3 MiB alone, over a 2MB cap
        dropped3 = prune(d3, time.time(), retain_days=3650, max_mb=2.0, active_name=active_name)
        check("the actively-written file survives even though it alone exceeds the size cap",
              os.path.exists(os.path.join(d3, active_name)) and active_name not in dropped3,
              dropped3)
        # and confirm the guard is specific to the active file, not a general "give up" -- an
        # older, non-active file over the cap is still evicted normally.
        d4 = tempfile.mkdtemp(prefix="stallcalib-selftest-active2-", dir=d)
        with open(os.path.join(d4, "stallcalib-2026-01-01.jsonl"), "wb") as f:
            f.write(b"x" * (1024 * 1024))
        with open(os.path.join(d4, active_name), "wb") as f:
            f.write(b"x" * (1024 * 1024))
        dropped4 = prune(d4, time.time(), retain_days=3650, max_mb=1.0, active_name=active_name)
        check("a non-active file over the cap is still evicted normally",
              "stallcalib-2026-01-01.jsonl" in dropped4 and active_name not in dropped4, dropped4)
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
