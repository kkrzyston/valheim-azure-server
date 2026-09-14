#!/usr/bin/env python3
"""valheim-restart-exec.py -- the caretaker: the only thing in this system allowed to stop or
start valheim.service, and only for a request a Discord role holder approved.

Runs as root, Type=oneshot, every 15 s from valheim-restart-exec.timer (OnUnitInactiveSec, so a
tick can never overlap a 20-minute execution) and optionally the moment a spool file appears
(valheim-restart-exec.path). One pass per invocation; all waiting is a persisted deadline, never a
sleep.

  requests/  (restartd writes)  -> validate + sanitize -> inbox/   (the bot reads)
  verdicts/  (the bot writes)   -> approve/deny        -> lock, backup, stop, confirm save, start, A2S

The request file on disk IS the state, and after creation this script is its only writer. `state`
is the persisted phase: it is written with an atomic replace *before and after every side effect*,
so a tick that dies mid-restart resumes from where it stopped instead of repeating an irreversible
step. `attempts` is bumped on every entry into the execution sequence; at 3 the request becomes
failed_needs_admin and systemd is never touched for it again.

Three rails that exist because of how this particular server is built:

  * valheim.service has ConditionPathExists=/home/valheim/data/.world-migrated. If that marker is
    missing, `systemctl start` **exits 0** and the unit just goes inactive -- a clean shutdown
    followed by a permanent outage, reported as a success. So the marker is checked before we stop
    anything, and it is NEVER re-created here: it guards world integrity, and touching it is how
    you start a brand-new world on top of a real one.
  * Once systemd parks valheim.service in `failed` (StartLimitBurst=5 / StartLimitIntervalSec=600),
    valheim-autoupdate.sh sees `is-active` false and deliberately exits 0 -- nothing self-heals,
    and the hall stays shut until a human runs `systemctl reset-failed valheim`. We therefore stay
    far below the start limit and refuse to act at all when the unit is already failed.
  * KillMode=mixed with TimeoutStopSec=180 means a hung shutdown ends in SIGKILL of the process
    group, possibly mid-write to _main.N.db2. If `Result` is `timeout` after our stop, we do NOT
    start: a torn save would be loaded and then written over the good backup at the next periodic
    save. That is the one case that ends at failed_needs_admin with the backup path in the alert.

--dry-run performs every check, every journal read and every Discord post, and prints what it
would do instead of calling systemctl. The single guarded call site is
SystemctlController._run(mutating=True); grep this file for "systemctl" and you will find it in
exactly one function.

Every path is overridable by an environment variable (see the block below) and every side effect
goes through an injectable seam (SystemctlController, A2SProbe, Journal, Webhook, world lock), so
the state machine can be exercised against fixtures on a machine with no systemd at all.
"""
import argparse
import base64
import glob
import hashlib
import hmac
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import time

try:                      # POSIX-only; absent under the Windows test harness
    import fcntl
except ImportError:
    fcntl = None
try:
    import pwd
except ImportError:
    pwd = None
try:
    import grp
except ImportError:
    grp = None


def env(name, default):
    return os.environ.get(name, default)


LIB = env("VR_LIB", "/var/lib/valheim-restart")
REQUESTS_DIR = env("VR_REQUESTS", os.path.join(LIB, "requests"))
INBOX_DIR = env("VR_INBOX", os.path.join(LIB, "inbox"))
VERDICTS_DIR = env("VR_VERDICTS", os.path.join(LIB, "verdicts"))
ARCHIVE_DIR = env("VR_ARCHIVE", os.path.join(LIB, "archive"))
QUARANTINE_DIR = env("VR_QUARANTINE", os.path.join(LIB, "quarantine"))
HISTORY_PATH = env("VR_HISTORY", os.path.join(LIB, "history.json"))
GATE_PATH = env("VR_GATE", os.path.join(LIB, "gate.json"))
SECRET_PATH = env("VR_SECRET", os.path.join(LIB, "secret.csrf"))
WEB_STATE_PATH = env("VR_WEB_STATE", "/var/www/valheim/restart-state.json")
STATUS_PATH = env("VR_STATUS", "/var/www/valheim/status.json")
COLLECTOR_PATH = env("VR_COLLECTOR_OUT", "/var/lib/valheim-status/restart.json")
JSONL_LOG = env("VR_LOG", "/var/log/valheim-restart.jsonl")
LOCK_PATH = env("VR_LOCK", "/var/lock/valheim-world.lock")
BACKUP_SH = env("VR_BACKUP_SH", "/home/valheim/backup.sh")
BACKUP_DIR = env("VR_BACKUP_DIR", "/home/valheim/backups")
MIGRATED_MARKER = env("VR_MIGRATED_MARKER", "/home/valheim/data/.world-migrated")
UNIT = env("VR_UNIT", "valheim.service")
SIBLING_UNITS = env("VR_SIBLING_UNITS", "valheim-update.service,valheim-offsite.service").split(",")
A2S_HOST = env("VR_A2S_HOST", "127.0.0.1")
A2S_PORT = int(env("VR_A2S_PORT", "2457"))
WEBHOOK_URL = env("DISCORD_WEBHOOK_URL", "").strip()
DASH_URL = env("DASHBOARD_URL", "").strip()
GUILD_ID = env("VR_DISCORD_GUILD_ID", "").strip()
RESTARTD_USER = env("VR_RESTARTD_USER", "valheim-restartd")
BOT_USER = env("VR_BOT_USER", "valheim-bot")
CADDY_GROUP = env("VR_CADDY_GROUP", "caddy")
SYSLOG_TAG = env("VR_SYSLOG_TAG", "valheim-restart")

# Timing rails. All of them are deadlines written into the request file, never sleeps.
COUNTDOWN_WITH_PLAYERS = int(env("VR_COUNTDOWN", "300"))
MILESTONES = (300, 120, 60, 30, 10)
APPROVAL_WINDOW = int(env("VR_APPROVAL_WINDOW", "900"))        # awaiting_approval -> expired_unapproved
APPROVED_WINDOW = int(env("VR_APPROVED_WINDOW", "300"))        # approved -> expired_approval
LOCK_BUDGET = int(env("VR_LOCK_BUDGET", "600"))                # waiting_for_lock -> aborted_lock_timeout
MIN_BETWEEN_RESTARTS = int(env("VR_MIN_BETWEEN", "1800"))
MAX_PER_DAY = int(env("VR_MAX_PER_DAY", "3"))
STATUS_MAX_AGE = int(env("VR_STATUS_MAX_AGE", "180"))
START_VERIFY_BUDGET = int(env("VR_START_BUDGET", "900"))       # ExecStartPre runs steamcmd; be patient
STOP_WAIT = int(env("VR_STOP_WAIT", "240"))                    # TimeoutStopSec=180 plus slack
MAX_ATTEMPTS = int(env("VR_MAX_ATTEMPTS", "3"))
ARCHIVE_AFTER = int(env("VR_ARCHIVE_AFTER", str(7 * 86400)))
# A verdict is tiny. A request file grows as we append phase history, backup facts and evidence,
# so it gets a bigger ceiling -- still bounded, because an unbounded read is how a spool becomes a
# memory exhaustion bug.
MAX_SPOOL_BYTES = int(env("VR_MAX_SPOOL_BYTES", "8192"))
MAX_REQUEST_BYTES = int(env("VR_MAX_REQUEST_BYTES", "32768"))
CSRF_BUCKET = 300

REASONS = {
    "not_responding": "the server is not responding",
    "cannot_join": "cannot join the world",
    "lag": "the world is lagging badly",
    "stuck_after_update": "stuck after an update",
    "other": "something else",
}
# Zero length is allowed on purpose: a requester who gives no name is recorded as "" -- the
# truth -- rather than as a name nobody typed. Never substitute a placeholder into the audit
# trail; display wording is a separate concern (see display_nickname).
NICK_RE = re.compile(r"^[A-Za-z0-9 _.\-]{0,24}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SPOOL_NAME_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.json$")
# The bot keeps a create-only "<id>.announce" companion next to its verdicts, so that a bot
# restart between posting the approval buttons and someone pressing one does not lose the
# Discord message. This script never parses it -- it only skips it and sweeps it.
MARKER_SUFFIX = ".announce"

ACTIVE_STATES = ("pending_validation", "awaiting_approval", "approved", "counting_down",
                 "waiting_for_lock", "backing_up", "stopping", "stopped_verified", "starting",
                 "verifying")
TERMINAL_STATES = ("rejected_invalid", "failed_precheck", "denied", "expired_unapproved",
                   "expired_approval", "aborted", "aborted_lock_timeout", "failed_needs_admin",
                   "succeeded")
# The four states a tick may safely wake up in and continue (PLAN-v5). Each handler starts by
# asking systemd what is actually true, which is what makes resuming idempotent.
RESUMABLE = ("stopping", "stopped_verified", "starting", "verifying")

O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
O_BINARY = getattr(os, "O_BINARY", 0)     # 0 on Linux; keeps the Windows test harness honest
HAVE_DIR_FD = os.open in getattr(os, "supports_dir_fd", set())

_TIME_SHIFT = float(env("VR_TIME_SHIFT", "0"))


def now():
    """Wall clock, shiftable for tests (VR_TIME_SHIFT seconds). Every deadline in this script is
    absolute epoch, so shifting the clock is enough to rehearse a countdown."""
    return time.time() + _TIME_SHIFT


def dur(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600} h {sec % 3600 // 60} min"
    if sec >= 60:
        return f"{sec // 60} min"
    return f"{sec} s"


def mask_ip(addr):
    """The bot has no business with a full address, so the sanitized copy gets 203.0.113.x."""
    if not isinstance(addr, str) or not addr:
        return "unknown"
    if ":" in addr:                      # IPv6: keep the first three groups
        parts = addr.split(":")
        return ":".join(parts[:3]) + ":x"
    parts = addr.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return ".".join(parts[:3]) + ".x"
    return "unknown"


def clean_text(value, limit):
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable())[:limit]


def display_nickname(claim):
    """An empty nickname means no name was given. Render that as a lower-case description, so it
    reads as one and can never be mistaken for a name somebody typed -- and never store the
    substitute anywhere."""
    return clean_text((claim or {}).get("nickname") or "", 24).strip() or "someone"


def load_json(path, default=None, max_bytes=1 << 20):
    try:
        with open(path, "rb") as f:
            return json.loads(f.read(max_bytes))
    except Exception:
        return default


def save_json_atomic(path, obj, mode=0o644, group=None):
    """tmp + os.replace, as every other script here does. Mode (and group) are set on the tmp file
    before the rename so the destination is never briefly world-readable -- restart-state.json
    carries the CSRF token and must stay 0640 root:caddy."""
    tmp = f"{path}.tmp.{os.getpid()}"
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    try:
        os.chmod(tmp, mode)
        if group and grp is not None:
            os.chown(tmp, -1, grp.getgrnam(group).gr_gid)
    except Exception:
        pass
    os.replace(tmp, path)


def uid_of(user, fallback_env):
    forced = os.environ.get(fallback_env)
    if forced is not None:
        return int(forced)
    if pwd is None:
        return 0
    try:
        return pwd.getpwnam(user).pw_uid
    except Exception:
        return -1          # unresolvable user: no spool file can match, so everything quarantines


RESTARTD_UID = uid_of(RESTARTD_USER, "VR_RESTARTD_UID")
BOT_UID = uid_of(BOT_USER, "VR_BOT_UID")


# ---------------------------------------------------------------- logging
class Log:
    """/var/log/valheim-restart.jsonl (append-only) plus `logger -t valheim-restart` into the
    journal. Both get the same record: every state transition, the requester's claim, the
    approver's identity, player counts, backup path and sha256, save evidence and total downtime."""

    def __init__(self, path=JSONL_LOG, tag=SYSLOG_TAG, echo=False):
        self.path = path
        self.tag = tag
        self.echo = echo
        self.syslog = shutil.which("logger") if env("VR_SYSLOG", "1") == "1" else None

    def __call__(self, event, **fields):
        rec = {"t": round(now(), 3), "event": event}
        rec.update(fields)
        line = json.dumps(rec, separators=(",", ":"), default=str)
        try:
            d = os.path.dirname(self.path)
            if d and not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
            fresh = not os.path.exists(self.path)
            with open(self.path, "a") as f:
                f.write(line + "\n")
            if fresh:
                # 0640 root:adm, like the rest of /var/log: the audit trail carries nicknames,
                # approver ids and IP fragments and has no business being world-readable.
                try:
                    os.chmod(self.path, 0o640)
                    if grp is not None:
                        os.chown(self.path, 0, grp.getgrnam("adm").gr_gid)
                except Exception:
                    pass
        except Exception:
            pass
        if self.syslog:
            try:
                subprocess.run([self.syslog, "-t", self.tag, line], timeout=5)
            except Exception:
                pass
        if self.echo:
            print(line)


# ---------------------------------------------------------------- seams
class SystemctlController:
    """The only place in this program that runs systemctl.

    Reads (show / is-active) always run for real, including under --dry-run, because a rehearsal
    that cannot see the truth is worthless. Mutations (stop / start / reset-failed) go through
    _run(mutating=True), which is the single guarded call site: under --dry-run it records the call
    and returns without executing it. There is no other path to systemctl in this file."""

    def __init__(self, dry_run=False, log=None):
        self.dry_run = dry_run
        self.log = log
        self.calls = []          # every mutation asked for, executed or not

    def _run(self, args, mutating):
        if mutating:
            self.calls.append(tuple(args))
            if self.dry_run:
                if self.log:
                    self.log("dry_run_systemctl", args=list(args))
                print(f"[dry-run] would run: systemctl {' '.join(args)}")
                return ""
        out = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=600)
        return out.stdout.strip()

    # --- reads
    def show(self, unit, props):
        raw = self._run(["show", unit, *[f"-p{p}" for p in props]], mutating=False)
        got = {}
        for line in raw.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                got[k] = v
        return {p: got.get(p, "") for p in props}

    def is_active(self, unit):
        return self._run(["is-active", unit], mutating=False) == "active"

    # --- mutations (the guarded call site above)
    def stop(self, unit):
        return self._run(["stop", unit], mutating=True)

    def start(self, unit):
        return self._run(["start", unit], mutating=True)

    def reset_failed(self, unit):
        return self._run(["reset-failed", unit], mutating=True)


class Journal:
    """Positive evidence that the world flushed, read from the journal rather than inferred from an
    exit code. The collector already parses both of these lines; same regexes."""

    re_save = re.compile(r"World save \(5/5\) done\. Total time \[(\d+)ms\]")
    re_savenum = re.compile(r"=> Save number (\d+)")

    def read(self, unit, invocation, since_ts):
        args = ["journalctl", "--no-pager", "-o", "cat", "-n", "4000"]
        if invocation:
            args.append(f"_SYSTEMD_INVOCATION_ID={invocation}")
        else:
            args += ["-u", unit]
        args += ["--since", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since_ts - 2))]
        try:
            return subprocess.run(args, capture_output=True, text=True, timeout=60).stdout
        except Exception:
            return ""

    def save_evidence(self, unit, invocation, since_ts):
        text = self.read(unit, invocation, since_ts)
        saves = self.re_save.findall(text)
        nums = self.re_savenum.findall(text)
        return {
            "confirmed": bool(saves),
            "save_ms": int(saves[-1]) if saves else None,
            "save_number": int(nums[-1]) if nums else None,
            "lines_seen": len(text.splitlines()),
        }


class A2SProbe:
    """Steam A2S_INFO against 127.0.0.1:2457 -- the same query and challenge handling the collector
    uses. Liveness is 'the game answered', never 'systemctl exited 0'."""

    def __init__(self, host=A2S_HOST, port=A2S_PORT, timeout=3):
        self.addr = (host, port)
        self.timeout = timeout

    def info(self):
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(self.timeout)
            q = b"\xff\xff\xff\xffTSource Engine Query\x00"
            t0 = time.time()
            s.sendto(q, self.addr)
            d, _ = s.recvfrom(4096)
            if d[4:5] == b"A":                   # challenge: echo the 4 bytes back
                s.sendto(q + d[5:9], self.addr)
                d, _ = s.recvfrom(4096)
            i = 6
            for _ in range(4):
                i = d.index(b"\x00", i) + 1
            i += 2                               # appid
            return {"ok": True, "players": d[i], "max_players": d[i + 1],
                    "rtt_ms": round((time.time() - t0) * 1000, 1)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass


class Webhook:
    """Countdown posts go through the webhook, not the bot, and edit one message in place -- the
    same pattern valheim-alert.py uses for its live board. A dead or crash-looping bot therefore
    cannot stall a countdown already in flight, and the channel gets one message that changes
    instead of five that pile up."""

    COLORS = {"bad": 0xD0604A, "ok": 0x8FB877, "warn": 0xE0A44B, "info": 0x93A8AB, "torch": 0xE3A54A}

    def __init__(self, url=WEBHOOK_URL, dash=DASH_URL, log=None, rehearsal=False):
        self.url = url
        self.dash = dash
        self.log = log
        self.rehearsal = rehearsal

    def _call(self, url, body, method):
        import urllib.request
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Content-Type": "application/json", "User-Agent": "valheim-restart/1"})
        return urllib.request.urlopen(req, timeout=10)

    def _body(self, title, text, level, fields=None):
        if self.rehearsal:
            title = f"Rehearsal: {title}"          # a dry run still posts; say so in the channel
        embed = {"title": title, "description": text, "color": self.COLORS.get(level, 0x93A8AB),
                 "footer": {"text": "1g49ye on Vancouver Island"}}
        if fields:
            embed["fields"] = fields
        if self.dash:
            embed["url"] = self.dash
        return {"username": "Hermóðr", "embeds": [embed], "allowed_mentions": {"parse": []}}

    def create(self, title, text, level="info", fields=None):
        if not self.url:
            if self.log:
                self.log("discord_skipped", why="no webhook configured", title=title)
            return None
        try:
            resp = self._call(f"{self.url}?wait=true", self._body(title, text, level, fields), "POST")
            created = json.loads(resp.read())
            return {"id": created.get("id"), "channel_id": created.get("channel_id")}
        except Exception as e:
            if self.log:
                self.log("discord_create_failed", error=str(e), title=title)
            return None

    def edit(self, message_id, title, text, level="info", fields=None):
        """True when the message is (or stays) ours to edit; False only when Discord says it is
        gone, so the caller creates a fresh one. A transient network error returns True on purpose:
        losing one milestone edit is better than posting a duplicate countdown every 15 s."""
        if not self.url or not message_id:
            return False
        try:
            self._call(f"{self.url}/messages/{message_id}",
                       self._body(title, text, level, fields), "PATCH").read()
            return True
        except Exception as e:
            if self.log:
                self.log("discord_edit_failed", error=str(e), message_id=message_id)
            return getattr(e, "code", None) != 404


class WorldLock:
    """/var/lock/valheim-world.lock, taken NON-BLOCKING: on contention the request parks in
    waiting_for_lock and retries on the next tick, with a 10 min budget. All holders are root, so
    the lock is a courtesy between the four world-touching scripts, not a permission boundary."""

    def __init__(self, path=LOCK_PATH):
        self.path = path
        self.fd = None

    def acquire(self):
        if fcntl is None:
            return False
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self):
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None

    def free(self):
        """Informational probe for the pre-flight: take it and give it straight back."""
        if self.acquire():
            self.release()
            return True
        return False


def make_lock(path=LOCK_PATH):
    return WorldLock(path)


# ---------------------------------------------------------------- the spool
class Spool:
    """One directory, one expected writer. Every read goes through validate(): O_NOFOLLOW against a
    directory fd opened once (so the directory itself cannot be swapped under us mid-flight), then
    fstat asserting a regular file, the expected st_uid, st_nlink == 1 and a bounded st_size.
    Anything that fails is moved to quarantine/ and never parsed.

    The st_uid check on verdicts/ IS THE ENTIRE TRUST BOUNDARY BETWEEN THE BOT AND THIS SCRIPT.
    A verdict is a command to stop the game server; the only thing that makes it trustworthy is
    that the kernel says the bot's uid created the file in a 1730 directory nothing else can write.
    Do not relax it, do not add a fallback, do not "fix" it by trusting a field inside the JSON."""

    def __init__(self, path, allowed_uids, quarantine=None, max_bytes=MAX_SPOOL_BYTES, log=None):
        self.path = path
        # A tuple, because requests/ legitimately holds two writers over a file's life: restartd
        # created it, and root (this script, its only writer afterwards) rewrote it at every phase.
        # verdicts/ gets exactly one uid, and that single value is the trust boundary.
        self.allowed_uids = tuple(allowed_uids) if allowed_uids is not None else None
        self.quarantine = quarantine
        self.max_bytes = max_bytes
        self.log = log
        self.fd = None
        if HAVE_DIR_FD:
            try:
                self.fd = os.open(path, os.O_RDONLY | O_DIRECTORY)
            except Exception as e:
                # On Linux this must not happen: without the directory fd, a swapped directory is
                # no longer detectable. Say so loudly and keep going with full paths.
                if log:
                    log("spool_dirfd_failed", path=path, error=str(e))

    def _open_kwargs(self):
        return {"dir_fd": self.fd} if self.fd is not None else {}

    def _full(self, name):
        return name if self.fd is not None else os.path.join(self.path, name)

    def names(self):
        try:
            return sorted(n for n in os.listdir(self.path) if n.endswith(".json"))
        except Exception:
            return []

    def read(self, name):
        """(data, None) or (None, reason). A bad file is quarantined here, not by the caller --
        after its descriptor is closed, so the move cannot fail on an open file."""
        fd = None
        data = None
        reason = None
        try:
            fd = os.open(self._full(name), os.O_RDONLY | O_NOFOLLOW | O_BINARY,
                         **self._open_kwargs())
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                reason = "not a regular file"
            elif self.allowed_uids is not None and st.st_uid not in self.allowed_uids:
                reason = f"owned by uid {st.st_uid}, expected one of {self.allowed_uids}"
            elif st.st_nlink != 1:
                reason = f"st_nlink is {st.st_nlink}"
            elif st.st_size <= 0 or st.st_size > self.max_bytes:
                reason = f"size {st.st_size}"
            else:
                data = json.loads(os.read(fd, self.max_bytes).decode("utf-8"))
                if not isinstance(data, dict):
                    data, reason = None, "not a JSON object"
        except OSError as e:
            # ELOOP from O_NOFOLLOW lands here: a symlink in the spool is never followed. So does
            # EISDIR, for a directory planted where a request file should be.
            reason = f"open failed: {type(e).__name__} errno {e.errno}"
        except Exception as e:
            reason = f"unreadable: {type(e).__name__}"
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass
        if reason is not None:
            if self.log:
                self.log("quarantined", spool=os.path.basename(self.path), file=name, why=reason)
            self.move_to(name, self.quarantine)
            return None, reason
        return data, None

    def write(self, name, data, mode=0o600):
        tmp = f".{name}.tmp"
        kw = self._open_kwargs()
        fd = os.open(self._full(tmp), os.O_CREAT | os.O_TRUNC | os.O_WRONLY | O_NOFOLLOW | O_BINARY,
                     mode, **kw)
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(data, separators=(",", ":"), default=str).encode())
            f.flush()
            os.fsync(f.fileno())
        if self.fd is not None:
            os.rename(tmp, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            try:
                os.fsync(self.fd)
            except Exception:
                pass
        else:
            os.replace(self._full(tmp), self._full(name))
        try:
            os.chmod(self._full(name), mode, **({"dir_fd": self.fd} if self.fd is not None else {}))
        except Exception:
            pass

    def move_to(self, name, dest_dir):
        if not dest_dir:
            return
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dst = os.path.join(dest_dir, name)
            if os.path.exists(dst):
                dst = os.path.join(dest_dir, f"{int(now())}-{name}")
            os.replace(os.path.join(self.path, name), dst)
        except Exception as e:
            if self.log:
                self.log("move_failed", file=name, dest=dest_dir, error=str(e))

    def unlink(self, name):
        try:
            if self.fd is not None:
                os.unlink(name, dir_fd=self.fd)
            else:
                os.unlink(self._full(name))
        except Exception:
            pass

    def sweep_junk(self, older_than=3600):
        """Abandoned *.part files from a restartd that died between create and rename."""
        try:
            for n in os.listdir(self.path):
                if n.endswith(".part") or n.startswith("."):
                    p = os.path.join(self.path, n)
                    if now() - os.path.getmtime(p) > older_than:
                        os.unlink(p)
        except Exception:
            pass


# ---------------------------------------------------------------- the executor
class Executor:
    def __init__(self, ctl=None, probe=None, hook=None, journal=None, lock_factory=make_lock,
                 log=None, dry_run=False):
        self.dry_run = dry_run
        self.log = log or Log()
        self.ctl = ctl or SystemctlController(dry_run=dry_run, log=self.log)
        self.probe = probe or A2SProbe()
        self.hook = hook or Webhook(log=self.log, rehearsal=dry_run)
        self.journal = journal or Journal()
        self.lock_factory = lock_factory
        self.requests = Spool(REQUESTS_DIR, (RESTARTD_UID, 0), QUARANTINE_DIR,
                              max_bytes=MAX_REQUEST_BYTES, log=self.log)
        # inbox/ is root-authored and only ever read by the bot; we write and unlink, never parse.
        self.inbox = Spool(INBOX_DIR, None, QUARANTINE_DIR, log=self.log)
        # One uid, and it is the whole trust boundary between the bot and this script.
        self.verdicts = Spool(VERDICTS_DIR, (BOT_UID,), QUARANTINE_DIR, log=self.log)
        self.history = load_json(HISTORY_PATH, {}) or {}
        self.history.setdefault("executed", [])
        self.history.setdefault("recent", [])

    # ---- persistence -------------------------------------------------
    def put(self, name, req, new_state=None, **fields):
        """Persist the request file. Called before AND after every side effect: `state` is the
        phase, and the file is the only thing that survives a killed tick."""
        # Fields first, then the transition: the terminal-state bookkeeping below reads them (the
        # `recent` entry's downtime_s comes from the same call that ends the request).
        req.update(fields)
        if new_state and new_state != req.get("state"):
            hist = req.setdefault("phase_history", [])
            hist.append({"t": round(now(), 3), "from": req.get("state"), "to": new_state})
            del hist[:-40]
            self.log("transition", id=req.get("id"), **{"from": req.get("state"), "to": new_state},
                     attempts=req.get("attempts", 0))
            req["state"] = new_state
            if new_state in TERMINAL_STATES:
                self.remember(req)
        req["updated_at"] = round(now(), 3)
        self.requests.write(name, req)

    def remember(self, req):
        entry = {"id": req.get("id"), "state": req.get("state"), "reason": (req.get("claim") or {}).get("reason"),
                 "at": round(now()), "approver": ({"display": (req.get("approver") or {}).get("display")}
                                                  if req.get("approver") else None),
                 "downtime_s": req.get("downtime_s"),
                 "unwedged": bool(req.get("unwedge_used"))}
        self.history["recent"] = ([entry] + [r for r in self.history["recent"]
                                             if r.get("id") != entry["id"]])[:10]

    def save_history(self):
        cutoff = now() - 86400 * 2
        self.history["executed"] = [t for t in self.history["executed"] if t > cutoff]
        save_json_atomic(HISTORY_PATH, self.history, 0o600)

    # ---- world facts -------------------------------------------------
    def status(self):
        s = load_json(STATUS_PATH)
        if not isinstance(s, dict):
            return None, "the dashboard's status file is missing"
        gen = s.get("generated")
        if not isinstance(gen, (int, float)) or now() - gen > STATUS_MAX_AGE:
            return None, "the dashboard's status file is stale"
        return s, None

    def players(self):
        """(count, names, source). A2S is authoritative and instant; status.json (up to a minute
        old) only fills in names, and is the fallback when the game is not answering at all.
        Never assume an empty server."""
        s, _ = self.status()
        names = [p.get("name") for p in ((s or {}).get("players") or {}).get("online", []) if p.get("name")]
        info = self.probe.info()
        if info.get("ok"):
            return int(info["players"]), names[:int(info["players"])] or names, "a2s"
        if s is not None:
            return int(((s.get("players") or {}).get("count")) or 0), names, "status.json"
        return None, names, "unknown"

    def unit_facts(self):
        return self.ctl.show(UNIT, ["ActiveState", "SubState", "Result", "ConditionResult",
                                    "InvocationID", "NRestarts"])

    # ---- pre-flight --------------------------------------------------
    def preflight(self, check_lock=False, allow_unwedge=False, name=None, req=None):
        """Every item here blocks execution. Returns a list of human-readable blockers, in the
        page's voice, because they are shown on the dashboard as `why_not`.

        `check_lock` probes the shared lock (informational only; the real acquisition is
        non-blocking, in run_sequence). `allow_unwedge` is passed from ONE call site -- run_sequence,
        immediately before the lock, the backup and the stop -- so the single permitted
        `reset-failed` happens as late as possible and a crash-looping unit cannot waste it minutes
        before it is needed."""
        out = []
        if not os.path.exists(MIGRATED_MARKER):
            # NEVER create this marker, and never "clear" anything about it. It is
            # valheim.service's ConditionPathExists; without it a `start` exits 0 and the unit
            # quietly stays down, and re-creating it is how a fresh world gets generated on top of a
            # real one. This guard is about world integrity, not about a stuck unit.
            out.append("the world-migrated marker is missing; a person must look at the server")
        facts = self.unit_facts()
        if facts.get("ConditionResult") not in ("yes", ""):
            # Also never cleared, for the same reason as the marker above.
            out.append("systemd says the game's start condition is not met")

        # The crisis this whole feature exists for: the game crash-loops, systemd burns
        # StartLimitBurst=5 inside StartLimitIntervalSec=600 and parks the unit in `failed`, and
        # valheim-autoupdate.sh then sees is-active false and deliberately exits 0 -- so nothing
        # self-heals and the hall stays shut until somebody opens SSH. Owner's decision
        # (2026-09-14): allow ONE `reset-failed` per request to un-wedge that, whether or not
        # Result is start-limit-hit. What makes it safe is that every other rail still applies --
        # the 30-minute spacing, the 3-per-24-h cap (so at most three clears in a day), the world
        # lock, the mandatory verified backup, the marker and the start condition above.
        if self.wedged(facts):
            if allow_unwedge and req is not None and not req.get("unwedge_used"):
                facts = self.unwedge(name, req, facts)
            if (allow_unwedge or (req is not None and req.get("unwedge_used"))) and self.wedged(facts):
                out.append("systemd has the game parked in a failed state and clearing it did not "
                           "help; a person must look at the server")
            # Otherwise a wedged unit is deliberately NOT a blocker here: it must not stop a request
            # from being filed or approved, or the button would be dead in exactly the outage it
            # exists for. The clear happens at execution time, above.
        last = max(self.history["executed"]) if self.history["executed"] else None
        if last and now() - last < MIN_BETWEEN_RESTARTS:
            out.append(f"the last restart was {dur(now() - last)} ago; the hall rests for "
                       f"{dur(MIN_BETWEEN_RESTARTS - (now() - last))} more")
        if len([t for t in self.history["executed"] if now() - t < 86400]) >= MAX_PER_DAY:
            out.append("three restarts have already happened today")
        s, why = self.status()
        if s is None:
            out.append(why)
        for unit in [u for u in SIBLING_UNITS if u.strip()]:
            if self.ctl.is_active(unit.strip()):
                out.append(f"{unit.strip()} is running right now")
        if check_lock and not self.lock_factory().free():
            out.append("another world job holds the shared lock")
        return out

    @staticmethod
    def wedged(facts):
        """systemd is holding the game down and a plain `start` will not work: the unit is parked in
        `failed`, or the start rate limiter has tripped. One `reset-failed` clears either."""
        return facts.get("ActiveState") == "failed" or facts.get("Result") == "start-limit-hit"

    def unwedge(self, name, req, facts):
        """One `systemctl reset-failed valheim`, once per request, recorded before it happens.

        The flag is persisted BEFORE the call, so a tick killed between the two cannot come back and
        clear a second time. This is separate from, and in addition to, the single post-start
        reset-failed in do_verify(): one clear for a failure that was already there when we arrived,
        one for a failure our own start caused. Neither can repeat, and neither loops across ticks."""
        was = {"ActiveState": facts.get("ActiveState"), "Result": facts.get("Result"),
               "NRestarts": facts.get("NRestarts")}
        self.put(name, req, None, unwedge_used=True, unwedged_at=round(now(), 3), unwedged_from=was)
        self.log("unwedged_service", id=req.get("id"), was=was,
                 why="systemd had the game parked; clearing it once before this restart")
        self.ctl.reset_failed(UNIT)
        after = self.unit_facts()
        self.log("unwedge_result", id=req.get("id"), now_state=after.get("ActiveState"),
                 now_result=after.get("Result"), cleared=not self.wedged(after))
        return after

    # ---- ingest ------------------------------------------------------
    def load_requests(self):
        self.requests.sweep_junk()
        out = []
        for name in self.requests.names():
            data, why = self.requests.read(name)
            if data is None:
                continue
            rid = data.get("id")
            # A filename that does not match its own id means somebody tried to influence a name.
            if not isinstance(rid, str) or not UUID_RE.match(rid) or name != rid + ".json":
                self.log("quarantined", spool="requests", file=name, why="filename does not match id")
                self.requests.move_to(name, QUARANTINE_DIR)
                continue
            out.append((name, data))
        return out

    def valid_claim(self, data):
        claim = data.get("claim")
        if not isinstance(claim, dict):
            return "no claim"
        # Unknown keys are rejected here as well as in restartd: this script is what acts on the
        # claim, so it does not inherit anyone else's validation.
        if set(claim) - {"nickname", "reason", "ack_players"}:
            return "unexpected keys in the claim"
        if claim.get("reason") not in REASONS:
            return "unknown reason"
        if not isinstance(claim.get("nickname"), str):
            return "unacceptable nickname"
        claim["nickname"] = claim["nickname"].strip()   # whitespace only means no name was given
        if not NICK_RE.match(claim["nickname"]):
            return "unacceptable nickname"
        if not isinstance(claim.get("ack_players"), bool):
            return "ack_players is not a bool"
        if data.get("schema") != 1:
            return "unknown schema"
        return None

    def ingest(self, name, req, active_others):
        bad = self.valid_claim(req)
        if bad:
            self.put(name, req, "rejected_invalid", why_not=bad)
            return
        if active_others:
            self.put(name, req, "failed_precheck", why_not="another restart is already in flight")
            self.alert("A restart request could not be taken", "Another request is already in "
                       "flight, so this one was set aside.", "info")
            return
        count, names, source = self.players()
        req["players_at_request"] = {"count": count, "names": names, "source": source}
        if count and count > 0 and not req["claim"]["ack_players"]:
            self.put(name, req, "rejected_invalid", why_not="players are online and were not acknowledged")
            return
        blockers = self.preflight()
        if blockers:
            self.put(name, req, "failed_precheck", why_not="; ".join(blockers))
            self.alert("A restart cannot be done right now",
                       "Someone asked for a restart, but: " + "; ".join(blockers), "warn")
            return
        expires = now() + APPROVAL_WINDOW
        self.write_inbox(req, expires)
        self.put(name, req, "awaiting_approval", expires_at=expires, attempts=0,
                 milestones_posted=[], inbox_written_at=round(now(), 3))
        self.log("request_received", id=req["id"], claim=req["claim"],
                 players_at_request=req["players_at_request"],
                 remote_ip=mask_ip((req.get("source") or {}).get("remote_ip")),
                 authenticated_identity=None)

    def write_inbox(self, req, expires):
        """Sanitize ONCE, here, in the most-trusted component: the bot then only ever parses JSON
        that root wrote. The IP is masked -- the bot has no business with a full address -- and the
        two honest limits travel with the record so the embed cannot forget to say them."""
        s, _ = self.status()
        srv = (s or {}).get("server") or {}
        world = (s or {}).get("world") or {}
        claim = req["claim"]
        count = (req.get("players_at_request") or {}).get("count")
        doc = {
            "schema": 1,
            "id": req["id"],
            "created_at": req.get("received_at"),
            "state": "awaiting_approval",
            "expires_at": expires,
            "claim": {"nickname": clean_text(claim["nickname"], 24),
                      "reason": claim["reason"],
                      "reason_label": REASONS[claim["reason"]],
                      "ack_players": bool(claim["ack_players"])},
            "source": {"remote_ip_masked": mask_ip((req.get("source") or {}).get("remote_ip")),
                       "user_agent": clean_text((req.get("source") or {}).get("user_agent") or "", 120),
                       "auth_realm": "viking",
                       "authenticated_identity": None},
            "players_at_request": req.get("players_at_request"),
            "server": {"online": srv.get("online"), "service_active": srv.get("service_active"),
                       "version": srv.get("version"), "day": world.get("day"),
                       "last_save_ms": (world.get("last_save") or {}).get("ms"),
                       "uptime_30d_pct": srv.get("uptime_30d_pct")},
            "countdown_s": 0 if not count else COUNTDOWN_WITH_PLAYERS,
            "verdict_path": os.path.join(VERDICTS_DIR, req["id"] + ".json"),
            "notes": [
                "The requester is not authenticated: viking/hammerhead is one shared password.",
                "Players in the game see no warning at all; the countdown reaches Discord and the "
                "dashboard only.",
            ],
            "dry_run": self.dry_run,
        }
        self.inbox.write(req["id"] + ".json", doc, 0o640)

    # ---- verdicts ----------------------------------------------------
    def apply_verdicts(self, requests):
        by_id = {d.get("id"): (n, d) for n, d in requests}
        for name in self.verdicts.names():
            # Only "<uuid>.json" is a verdict. names() already filters to *.json, so the bot's
            # "<id>.announce" marker never gets here; this shape check is the belt to that braces.
            # A companion file is NOT a malformed verdict: quarantining one per request would fill
            # quarantine/ with noise and bury the signal it exists to carry. So: skipped, silently,
            # and never opened. Markers are cleared by sweep_markers() once the request is done.
            if not SPOOL_NAME_RE.match(name):
                continue
            data, why = self.verdicts.read(name)
            if data is None:
                continue           # already quarantined, including the wrong-uid case
            vid = data.get("id")
            verdict = data.get("verdict")
            if (not isinstance(vid, str) or not UUID_RE.match(vid) or name != vid + ".json"
                    or verdict not in ("approve", "deny") or data.get("schema") != 1):
                self.log("quarantined", spool="verdicts", file=name, why="malformed verdict")
                self.verdicts.move_to(name, QUARANTINE_DIR)
                continue
            target = by_id.get(vid)
            if not target or target[1].get("state") != "awaiting_approval":
                self.log("verdict_ignored", id=vid, why="no request awaiting approval")
                self.verdicts.move_to(name, QUARANTINE_DIR)
                continue
            rname, req = target
            approver = data.get("approver") if isinstance(data.get("approver"), dict) else {}
            display = clean_text(str(approver.get("display") or "someone"), 64)
            # The raw Discord id is logged (accountability) but never leaves this machine in any
            # file the page or the collector can read.
            self.log("verdict", id=vid, verdict=verdict, approver_display=display,
                     approver_discord_id=approver.get("discord_id"), note=clean_text(str(data.get("note") or ""), 200))
            if verdict == "deny":
                self.put(rname, req, "denied", approver={"display": display},
                         why_not="a role holder said no")
            else:
                self.put(rname, req, "approved", approver={"display": display},
                         approved_at=round(now(), 3))
            self.verdicts.unlink(name)

    # ---- countdown ---------------------------------------------------
    def begin_countdown(self, name, req):
        count, names, source = self.players()
        if count is None:
            # Never assume an empty server: no count means no fast path and no execution yet.
            self.put(name, req, None, why_not="the game is not answering; waiting for a count")
            return
        wait = 0 if count == 0 else COUNTDOWN_WITH_PLAYERS
        restart_at = now() + wait
        # The post below IS the T-<wait> notice, so record that milestone as done; otherwise the
        # next tick would immediately edit the message again to say the same thing.
        self.put(name, req, "counting_down", restart_at=restart_at, countdown_s=wait,
                 players_at_approval={"count": count, "names": names, "source": source},
                 milestones_posted=[wait] if wait in MILESTONES else [], why_not=None)
        self.post_countdown(name, req, heads_up=True)

    def countdown_text(self, req, extra=None):
        claim = req.get("claim") or {}
        left = max(0, int((req.get("restart_at") or now()) - now()))
        who = (req.get("players_at_approval") or {}).get("names") or []
        nick = clean_text(claim.get("nickname") or "", 24).strip()
        asker = f"**{nick}**" if nick else display_nickname(claim)   # no bold on a description
        lines = [
            f"{asker} asked for a restart: "
            f"{REASONS.get(claim.get('reason'), 'no reason given')}.",
            f"Approved by {(req.get('approver') or {}).get('display', 'a role holder')}.",
            f"The world goes down in **{dur(left)}**." if left else "The world is going down **now**.",
        ]
        if who:
            lines.append("In the world: " + ", ".join(clean_text(n, 24) for n in who) + ".")
        else:
            lines.append("Nobody is in the world.")
        if req.get("unwedge_used"):
            lines.append("Note: systemd had the game parked in a failed state, so it was cleared "
                         "once before this restart. Something went badly wrong earlier.")
        lines.append("_Nobody in the game will see a warning: this server runs no mods and has no "
                     "RCON, so this channel and the dashboard are the only notice._")
        if extra:
            lines.append(extra)
        return "\n".join(lines)

    def post_countdown(self, name, req, heads_up=False, extra=None, level="warn", title=None):
        title = title or ("The hall closes shortly" if req.get("countdown_s") else "The hall is closing now")
        text = self.countdown_text(req, extra)
        mid = req.get("message_id")
        if mid and self.hook.edit(mid, title, text, level):
            return
        created = self.hook.create(title, text, level)
        if created and created.get("id"):
            url = (f"https://discord.com/channels/{GUILD_ID}/{created['channel_id']}/{created['id']}"
                   if GUILD_ID and created.get("channel_id") else None)
            self.put(name, req, None, message_id=created["id"], message_url=url)

    def tick_countdown(self, name, req):
        left = (req.get("restart_at") or 0) - now()
        count, names, source = self.players()

        # Escalation: 0 -> N means somebody joined after approval. Never silently take the fast
        # path; give them the full grace period.
        if count and count > 0 and (req.get("countdown_s") or 0) == 0:
            self.put(name, req, None, countdown_s=COUNTDOWN_WITH_PLAYERS,
                     restart_at=now() + COUNTDOWN_WITH_PLAYERS, milestones_posted=[],
                     escalated_at=round(now(), 3),
                     players_at_approval={"count": count, "names": names, "source": source})
            self.log("escalated", id=req["id"], players=count)
            self.post_countdown(name, req, extra="Someone joined after this was approved, so the "
                                                 "full five minutes start now.")
            return
        # N -> 0 may shorten it, once, with a short heads-up rather than an instant kill.
        if count == 0 and (req.get("countdown_s") or 0) > 0 and left > 30 and not req.get("shortened_at"):
            self.put(name, req, None, restart_at=now() + 10, shortened_at=round(now(), 3))
            self.log("shortened", id=req["id"])
            self.post_countdown(name, req, extra="The world emptied, so this happens sooner.")
            return

        posted = list(req.get("milestones_posted") or [])
        due = [m for m in MILESTONES if m <= (req.get("countdown_s") or 0) and left <= m and m not in posted]
        if due:
            posted.extend(due)
            self.put(name, req, None, milestones_posted=posted)
            self.post_countdown(name, req)

        if now() >= (req.get("restart_at") or 0):
            self.run_sequence(name, req)

    # ---- execution ---------------------------------------------------
    def run_sequence(self, name, req):
        """stop then start, never a bare `restart`. Phases persist before and after every side
        effect; a killed tick re-enters here and each handler begins by asking systemd what is
        actually true, which is what makes resuming safe instead of a second restart."""
        if req.get("attempts", 0) >= MAX_ATTEMPTS:
            return self.needs_admin(name, req, f"{req.get('attempts')} attempts already")
        # The only call site that passes allow_unwedge: immediately before the lock, the backup
        # and the stop, never during validation or the countdown.
        blockers = self.preflight(allow_unwedge=True, name=name, req=req)
        if blockers:
            if req.get("state") in RESUMABLE:
                # Already past the stop, so this is not a pre-check any more and "nothing was
                # touched" would be a lie: the hall is down and a person has to look.
                return self.needs_admin(name, req, "; ".join(blockers))
            self.put(name, req, "failed_precheck", why_not="; ".join(blockers))
            self.alert("The restart was called off", "Before touching anything: "
                       + "; ".join(blockers), "warn")
            return

        resuming = req.get("state") in RESUMABLE
        if not resuming and req.get("state") != "waiting_for_lock":
            self.put(name, req, "waiting_for_lock",
                     lock_wait_started=req.get("lock_wait_started") or round(now(), 3))

        # Take the lock BEFORE counting an attempt: lock contention is not a failed attempt, and
        # burning the 3-attempt budget on a nightly backup that happens to be running would turn a
        # recoverable wait into failed_needs_admin.
        lock = self.lock_factory()
        if not lock.acquire():
            waited = now() - (req.get("lock_wait_started") or now())
            if waited > LOCK_BUDGET:
                self.put(name, req, "aborted_lock_timeout",
                         why_not="another world job held the lock for ten minutes")
                self.alert("The restart gave up waiting", "Another world job (backup or update) "
                           "held the shared lock for ten minutes, so the restart was abandoned. "
                           "Nothing was stopped.", "warn")
            else:
                self.put(name, req, "waiting_for_lock", why_not="waiting for another world job")
                self.log("waiting_for_lock", id=req["id"], waited_s=int(waited))
            return
        try:
            self.put(name, req, None, attempts=req.get("attempts", 0) + 1, why_not=None)
            if resuming:
                self.log("resumed", id=req["id"], state=req.get("state"), attempts=req["attempts"])
            self.with_lock(name, req)
        finally:
            lock.release()

    def with_lock(self, name, req):
        # Resume-safe: each step is entered only from the phase before it, and each one asks
        # systemd what is actually true before doing anything irreversible.
        if req["state"] in ("waiting_for_lock", "backing_up"):
            if not self.do_backup(name, req):
                return
        if req["state"] in ("backing_up", "stopping"):
            if not self.do_stop(name, req):
                return
        if req["state"] == "stopped_verified":
            if not self.do_start(name, req):
                return
        if req["state"] in ("starting", "verifying"):
            self.do_verify(name, req)

    def do_backup(self, name, req):
        """A verified tarball before anything is touched. Abort before the service on any failure:
        a restart without a good backup is not worth four people's world."""
        self.put(name, req, "backing_up")
        before = set(glob.glob(os.path.join(BACKUP_DIR, "*.tgz")))
        try:
            out = subprocess.run(["/usr/bin/env", "bash", BACKUP_SH], capture_output=True,
                                 text=True, timeout=1800)
            rc = out.returncode
        except Exception as e:
            rc, out = 1, None
            self.log("backup_failed", id=req["id"], error=str(e))
        after = set(glob.glob(os.path.join(BACKUP_DIR, "*.tgz")))
        fresh = sorted(after - before, key=lambda p: os.path.getmtime(p))
        tarball = fresh[-1] if fresh else None
        if rc != 0 or not tarball:
            self.put(name, req, "aborted", why_not="the backup did not produce a tarball")
            self.alert("The restart was called off", "backup.sh did not produce a fresh tarball, "
                       "so nothing was stopped. The world is untouched.", "bad")
            return False
        info = self.verify_tarball(tarball)
        if not info["ok"]:
            self.put(name, req, "aborted", why_not=f"the backup looks wrong: {info['why']}",
                     backup=info)
            self.alert("The restart was called off", f"The fresh backup `{os.path.basename(tarball)}` "
                       f"did not verify ({info['why']}), so nothing was stopped.", "bad")
            return False
        self.put(name, req, None, backup=info)
        self.log("backup_verified", id=req["id"], **info)
        return True

    def verify_tarball(self, path):
        info = {"path": path, "ok": False, "why": None, "bytes": None, "entries": None, "sha256": None}
        try:
            info["bytes"] = os.path.getsize(path)
            if info["bytes"] < 1 << 20:                  # a real Vancouver Island save is tens of MB
                info["why"] = f"only {info['bytes']} bytes"
                return info
            # tar -tzf, via the stdlib so there is nothing to quote: same listing, same failure.
            entries = 0
            with tarfile.open(path, "r:gz") as tf:
                for _ in tf:
                    entries += 1
                    if entries > 20000:
                        break
            info["entries"] = entries
            if entries < 3:
                info["why"] = f"only {entries} entries"
                return info
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            info["sha256"] = h.hexdigest()
            info["ok"] = True
        except Exception as e:
            info["why"] = f"{type(e).__name__}: {e}"
        return info

    def do_stop(self, name, req):
        s, _ = self.status()
        facts = self.unit_facts()
        count, names, source = self.players()
        # On a resume every one of these keeps its original value: the invocation id is the journal
        # target for the save evidence, and after the stop it is gone from systemd.
        self.put(name, req, "stopping",
                 players_at_exec=req.get("players_at_exec")
                 or {"count": count, "names": names, "source": source},
                 save_number_before=req.get("save_number_before")
                 if req.get("save_number_before") is not None
                 else ((s or {}).get("world") or {}).get("save_number"),
                 invocation=req.get("invocation") or facts.get("InvocationID"),
                 stop_requested_at=req.get("stop_requested_at") or round(now(), 3))
        self.log("stopping", id=req["id"], players_at_exec=count,
                 save_number_before=req.get("save_number_before"), invocation=req.get("invocation"))

        if facts.get("ActiveState") in ("active", "activating", "reloading", "deactivating"):
            self.ctl.stop(UNIT)
        deadline = now() + STOP_WAIT
        while now() < deadline:
            facts = self.unit_facts()
            if facts.get("ActiveState") in ("inactive", "failed"):
                break
            if self.dry_run:
                break                     # nothing was stopped, so nothing will go inactive
            time.sleep(2)
        facts = self.unit_facts()
        if not self.dry_run and facts.get("ActiveState") not in ("inactive", "failed"):
            # Still deactivating after TimeoutStopSec plus slack. Starting now would race systemd's
            # own shutdown of the same unit, so stop here and let the next tick (or a human) see it.
            return self.needs_admin(name, req, "the game has not finished stopping",
                                    extra="The server was NOT started again. "
                                          f"`systemctl status {UNIT}` shows where it is stuck.")
        result = facts.get("Result")
        evidence = self.journal.save_evidence(UNIT, req.get("invocation"), req["stop_requested_at"])
        before = req.get("save_number_before")
        if not evidence["confirmed"] and before is not None and evidence.get("save_number") is not None:
            evidence["confirmed"] = evidence["save_number"] > before
        self.put(name, req, None, save_evidence=evidence, stop_result=result,
                 stopped_at=round(now(), 3))
        self.log("stopped", id=req["id"], result=result, save_evidence=evidence)

        if result == "timeout":
            # SIGKILLed process group (KillMode=mixed, TimeoutStopSec=180): the save may be torn
            # mid-write to _main.N.db2. Starting would load it and then overwrite the good backup
            # at the next periodic save. A human decides from here.
            return self.needs_admin(
                name, req, "the game had to be killed on stop (Result=timeout)",
                extra=f"The world may have been killed mid-save, so the server was NOT started "
                      f"again. Restore from `{(req.get('backup') or {}).get('path')}` if the world "
                      f"looks wrong. sha256 `{(req.get('backup') or {}).get('sha256')}`.")
        if not evidence["confirmed"] and not self.dry_run:
            # A clean exit without a fresh "World save (5/5) done" line means the last good save is
            # whatever was on disk -- intact, just older. Worth saying out loud, not worth an
            # indefinite outage, so we carry on and start the server.
            self.alert("The world save could not be confirmed",
                       "The game stopped cleanly but no fresh save line appeared in the journal. "
                       "Up to half an hour of building may be missing. Starting the server anyway; "
                       f"the backup is `{(req.get('backup') or {}).get('path')}`.", "warn")
        self.put(name, req, "stopped_verified")
        return True

    def do_start(self, name, req):
        self.put(name, req, "starting", start_requested_at=round(now(), 3),
                 verify_until=now() + START_VERIFY_BUDGET)
        facts = self.unit_facts()
        # `or self.dry_run`: in a rehearsal nothing was actually stopped, so the unit still looks
        # active and this guard would skip the start -- and the rehearsal report would then not
        # mention the start it would really have issued. The call is still only recorded, never run.
        if self.dry_run or facts.get("ActiveState") not in ("active", "activating"):
            self.ctl.start(UNIT)
        self.log("started", id=req["id"], invocation=self.unit_facts().get("InvocationID"))
        self.put(name, req, "verifying")
        return True

    def do_verify(self, name, req):
        """Liveness is a Steam A2S_INFO answer, not an exit code. ExecStartPre runs
        `steamcmd +app_update` on every start and TimeoutStartSec=900, so this waits patiently --
        and the deadline is persisted, so a killed tick keeps verifying instead of starting twice."""
        deadline = req.get("verify_until") or (now() + START_VERIFY_BUDGET)
        if req["state"] != "verifying":
            self.put(name, req, "verifying", verify_until=deadline)
        while now() < deadline:
            info = self.probe.info()
            if info.get("ok"):
                downtime = int(now() - (req.get("stop_requested_at") or now()))
                self.history["executed"].append(round(now()))
                self.put(name, req, "succeeded", downtime_s=downtime, verified_at=round(now(), 3),
                         a2s=info, why_not=None)
                self.save_history()
                self.log("succeeded", id=req["id"], downtime_s=downtime,
                         players_at_request=(req.get("players_at_request") or {}).get("count"),
                         players_at_exec=(req.get("players_at_exec") or {}).get("count"),
                         backup=(req.get("backup") or {}).get("path"),
                         backup_sha256=(req.get("backup") or {}).get("sha256"),
                         save_evidence=req.get("save_evidence"),
                         unwedged=bool(req.get("unwedge_used")),
                         approver=(req.get("approver") or {}).get("display"))
                self.post_countdown(name, req, title="The hall is open again", level="ok",
                                    extra=f"Back up after {dur(downtime)}.")
                return True
            facts = self.unit_facts()
            if facts.get("ActiveState") == "failed" and not req.get("reset_failed_used"):
                # Exactly once, recorded, never a loop.
                self.put(name, req, None, reset_failed_used=True)
                self.ctl.reset_failed(UNIT)
                self.ctl.start(UNIT)
                self.log("reset_failed_once", id=req["id"])
            elif facts.get("ActiveState") == "failed":
                return self.needs_admin(name, req, "the game failed to start twice")
            if self.dry_run and not info.get("ok"):
                # Nothing was actually stopped or started; do not burn 15 minutes rehearsing.
                self.put(name, req, "succeeded", downtime_s=0, dry_run_note="not verified: rehearsal")
                self.save_history()
                self.log("dry_run_finished", id=req["id"])
                self.post_countdown(name, req, title="The hall is open again", level="ok",
                                    extra="Rehearsal only: nothing was stopped or started.")
                return True
            time.sleep(5)
        return self.needs_admin(name, req, "the game did not answer on the Steam port in time")

    def needs_admin(self, name, req, why, extra=None):
        self.put(name, req, "failed_needs_admin", why_not=why)
        self.log("failed_needs_admin", id=req["id"], why=why,
                 backup=(req.get("backup") or {}).get("path"),
                 backup_sha256=(req.get("backup") or {}).get("sha256"),
                 save_evidence=req.get("save_evidence"), stop_result=req.get("stop_result"),
                 unwedged=bool(req.get("unwedge_used")), attempts=req.get("attempts"))
        self.alert("A restart needs a person", (extra + "\n\n" if extra else "")
                   + f"Reason: {why}. Nothing further will be attempted for this request.\n"
                   f"`journalctl -t valheim-restart -n 50` and "
                   f"`systemctl status {UNIT}` tell the rest.", "bad")
        return False

    def alert(self, title, text, level):
        """A separate message, not an edit: an operator alert must be loud and keep its own place
        in the channel."""
        self.hook.create(title, text, level)

    # ---- expiry and housekeeping -------------------------------------
    def expire(self, name, req):
        state = req.get("state")
        if state == "awaiting_approval" and now() > (req.get("expires_at") or 0):
            self.put(name, req, "expired_unapproved", why_not="nobody approved it within 15 minutes")
            self.inbox.unlink(req["id"] + ".json")
            return True
        if state == "approved" and now() - (req.get("approved_at") or now()) > APPROVED_WINDOW:
            self.put(name, req, "expired_approval",
                     why_not="the approval went stale before the restart could start")
            return True
        return False

    def sweep(self):
        live, done = set(), set()
        for name in self.requests.names():
            data, _ = self.requests.read(name)
            if not data:
                continue
            if data.get("state") not in TERMINAL_STATES:
                live.add(data.get("id"))
                continue
            done.add(data.get("id"))
            age = now() - (data.get("updated_at") or data.get("received_at") or now())
            if age > ARCHIVE_AFTER:
                self.requests.move_to(name, ARCHIVE_DIR)
                self.inbox.unlink(data.get("id", "") + ".json")
                self.log("archived", id=data.get("id"), state=data.get("state"))
        self.sweep_markers(live, done)

    def sweep_markers(self, live, done):
        """Clear the bot's "<id>.announce" markers the moment their request is terminal, and any
        orphan after an hour, so verdicts/ does not grow forever. Never parsed, only unlinked --
        root may unlink another user's file even in a 1730 sticky directory."""
        try:
            names = os.listdir(VERDICTS_DIR)
        except Exception:
            return
        for n in names:
            if not n.endswith(MARKER_SUFFIX):
                continue
            rid = n[:-len(MARKER_SUFFIX)]
            if rid in live:
                continue
            path = os.path.join(VERDICTS_DIR, n)
            try:
                orphan = rid not in done and now() - os.path.getmtime(path) > 3600
                if rid in done or orphan:
                    os.unlink(path)
                    self.log("marker_swept", id=rid, orphan=orphan)
            except Exception as e:
                self.log("marker_sweep_failed", file=n, error=str(e))

    # ---- outputs -----------------------------------------------------
    def csrf_token(self):
        secret = self.secret()
        if not secret:
            return None
        mac = hmac.new(secret, str(int(now() // CSRF_BUCKET)).encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def secret(self):
        try:
            with open(SECRET_PATH, "rb") as f:
                data = f.read().strip()
            if data:
                return data
        except FileNotFoundError:
            pass
        except Exception:
            return None
        # Create it once, here, rather than depending on install order. 0640 root:valheim-restartd.
        try:
            secret = base64.urlsafe_b64encode(os.urandom(32)).strip()
            os.makedirs(os.path.dirname(SECRET_PATH), exist_ok=True)
            fd = os.open(SECRET_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY | O_NOFOLLOW, 0o640)
            with os.fdopen(fd, "wb") as f:
                f.write(secret + b"\n")
            if grp is not None:
                try:
                    os.chown(SECRET_PATH, 0, grp.getgrnam(RESTARTD_USER).gr_gid)
                except Exception:
                    self.log("secret_chown_failed", path=SECRET_PATH)
            self.log("secret_created", path=SECRET_PATH)
            return secret
        except FileExistsError:
            try:                       # somebody won the race; use theirs
                with open(SECRET_PATH, "rb") as f:
                    return f.read().strip() or None
            except Exception:
                return None
        except Exception as e:
            self.log("secret_failed", error=str(e))
            return None

    def write_outputs(self, requests):
        active = [(n, d) for n, d in requests if d.get("state") in ACTIVE_STATES]
        current = None
        if active:
            n, d = active[0]
            current = {
                "id": d.get("id"), "state": d.get("state"),
                "reason": (d.get("claim") or {}).get("reason"),
                "nickname": (d.get("claim") or {}).get("nickname"),
                "created_at": int(d.get("received_at") or 0),
                "players_at_request": d.get("players_at_request") or {"count": None, "names": []},
                "restart_at": int(d["restart_at"]) if d.get("restart_at") else None,
                "approver": ({"display": (d.get("approver") or {}).get("display")}
                             if d.get("approver") else None),
                # True when systemd had the game parked and this restart had to clear it once.
                "unwedged": bool(d.get("unwedge_used")),
                "message_url": d.get("message_url"),
            }
        last = max(self.history["executed"]) if self.history["executed"] else None
        cooldown_until = int(last + MIN_BETWEEN_RESTARTS) if last and now() - last < MIN_BETWEEN_RESTARTS else None
        blockers = self.preflight()
        if active:
            blockers.insert(0, "a restart is already being handled")
        why_not = "; ".join(blockers) or None
        count, names, _ = self.players()

        state_doc = {
            "generated": int(now()),
            "csrf": self.csrf_token(),
            "accepting": not blockers,
            "why_not": why_not,
            "cooldown_until": cooldown_until,
            "current": current,
            "recent": self.history["recent"][:10],
        }
        # 0640 root:caddy: this file carries the CSRF token, so it must not be world-readable.
        save_json_atomic(WEB_STATE_PATH, state_doc, 0o640, CADDY_GROUP)

        gate = {
            "generated": int(now()),
            "accepting": not blockers,
            "why_not": why_not,
            "in_flight": (current or {}).get("id"),
            "cooldown_until": cooldown_until,
            "players_online": count,
            "executed_24h": len([t for t in self.history["executed"] if now() - t < 86400]),
            "rate_limited": bool(cooldown_until) or
                            len([t for t in self.history["executed"] if now() - t < 86400]) >= MAX_PER_DAY,
            "dry_run": self.dry_run,
        }
        save_json_atomic(GATE_PATH, gate, 0o644)

        collector = {k: v for k, v in state_doc.items() if k != "csrf"}
        collector["dry_run"] = self.dry_run
        save_json_atomic(COLLECTOR_PATH, collector, 0o644)

    # ---- one pass ----------------------------------------------------
    def tick(self):
        requests = self.load_requests()
        for name, req in list(requests):
            if req.get("state") == "pending_validation":
                others = [d for n, d in requests if d is not req and d.get("state") in ACTIVE_STATES]
                self.ingest(name, req, others)

        requests = self.load_requests()
        self.apply_verdicts(requests)

        requests = self.load_requests()
        for name, req in requests:
            if req.get("state") not in ACTIVE_STATES:
                continue
            if self.expire(name, req):
                continue
            state = req.get("state")
            try:
                if state == "approved":
                    blockers = self.preflight(check_lock=True)
                    if blockers:
                        self.put(name, req, None, why_not="; ".join(blockers))
                        self.log("approved_but_blocked", id=req["id"], blockers=blockers)
                    else:
                        self.begin_countdown(name, req)
                elif state == "counting_down":
                    self.tick_countdown(name, req)
                elif state in ("waiting_for_lock", "backing_up") or state in RESUMABLE:
                    self.run_sequence(name, req)
            except Exception as e:
                # Never fail the unit on a transient error, and never leave the phase a lie.
                self.log("tick_error", id=req.get("id"), state=state, error=f"{type(e).__name__}: {e}")

        self.sweep()
        self.save_history()
        self.write_outputs(self.load_requests())


def main(argv=None):
    ap = argparse.ArgumentParser(description="the Valheim restart caretaker")
    ap.add_argument("--dry-run", action="store_true",
                    help="do every check and every Discord post, but print the systemctl calls "
                         "instead of running them")
    ap.add_argument("--once", action="store_true", help="a single pass (the default; kept explicit "
                                                        "for tests and hand runs)")
    ap.add_argument("--echo-log", action="store_true", help="also print the jsonl log to stdout")
    args = ap.parse_args(argv)
    dry = args.dry_run or env("VR_DRY_RUN", "0") == "1"
    log = Log(echo=args.echo_log or dry)
    ex = Executor(log=log, dry_run=dry)
    if dry:
        print("[dry-run] no systemctl mutation will be executed this pass")
    try:
        ex.tick()
    except Exception as e:
        log("tick_fatal", error=f"{type(e).__name__}: {e}")
        return 0      # a transient failure must never fail the unit; the next tick is 15 s away
    if dry and ex.ctl.calls:
        print("[dry-run] systemctl calls that were skipped: "
              + "; ".join(" ".join(c) for c in ex.ctl.calls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
