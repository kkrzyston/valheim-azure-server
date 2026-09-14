#!/usr/bin/env python3
"""valheim-restartd.py -- accept one kind of POST from Caddy and write one file. Nothing else.

Runs as the system user `valheim-restartd` (no shell, no privileges, no network) from
valheim-restartd.service. The whole job:

    POST /api/restart/request  ->  validate  ->  /var/lib/valheim-restart/requests/<uuid4>.json  ->  202 {id}

It cannot execute anything, cannot reach the network, cannot read the directory it writes into
(mode 1730: create-by-name only) and cannot read any world data. The component that is reachable
from the internet is therefore the component that can do the least. Everything dangerous lives in
valheim-restart-exec.py, which runs as root from a timer and never listens on anything.

Why a unix socket and not a localhost port
    A port on 127.0.0.1 is reachable by *every* local account, including `valheim` -- the account
    that runs the internet-facing game process, i.e. the one most likely to be compromised. The
    socket is 0660 valheim-restartd:caddy, so only Caddy can speak to it. This is also what lets
    the unit keep PrivateNetwork=yes: see the loud comment in valheim-restartd.service.

Why the four header checks (see PLAN-v5, "The restart request POST")
    The dashboard is behind HTTP basic auth, which is *not* a cookie: SameSite does not apply and
    a browser will happily attach cached credentials to a cross-site form POST. So:
      * Content-Type: application/json and a custom X-Valheim-Restart header cannot be produced by
        a plain <form> submission, and force a CORS preflight that we deliberately never answer
        (OPTIONS gets a bare 405 with no Access-Control-Allow-* headers, so the preflight fails).
      * Sec-Fetch-Site is set by the browser itself and cannot be forged by page script. Absent is
        allowed on purpose: a non-browser client (curl) has no cached credentials to be tricked
        out of, so it is not the threat this check exists for.
      * Origin is only a secondary signal and is never used to reject: the site sends
        Referrer-Policy: no-referrer, which can legitimately produce `Origin: null`.
    Plus a CSRF token the attacker's page cannot read (it is served inside restart-state.json,
    behind the same login, and the browser will not hand a cross-origin reader its body).

CSRF token: base64url(HMAC-SHA256(secret, decimal(floor(now/300)))), current or previous bucket,
compared in constant time. Stateless on purpose -- a stored single-use nonce would need a write
path per page load, which is a DoS amplifier pointed at our own disk.

Everything read-only that the page needs (including the token) is the static restart-state.json
that the executor writes. There is deliberately no GET route here, and no state endpoint.
"""
import base64
import hashlib
import hmac
import http.server
import json
import os
import re
import socket
import socketserver
import sys
import threading
import time
import uuid

LIB = os.environ.get("RESTARTD_LIB", "/var/lib/valheim-restart")
SOCKET_PATH = os.environ.get("RESTARTD_SOCKET", "/run/valheim-restartd/http.sock")
SOCKET_GROUP = os.environ.get("RESTARTD_SOCKET_GROUP", "caddy")
REQUESTS_DIR = os.environ.get("RESTARTD_REQUESTS", os.path.join(LIB, "requests"))
SECRET_PATH = os.environ.get("RESTARTD_SECRET", os.path.join(LIB, "secret.csrf"))
GATE_PATH = os.environ.get("RESTARTD_GATE", os.path.join(LIB, "gate.json"))
# systemd exports RUNTIME_DIRECTORY for RuntimeDirectory=; the rate-limit scratch file lives there
# because it is the only place this user may write to *and* read back from.
RUNTIME_DIR = os.environ.get("RUNTIME_DIRECTORY", os.path.dirname(SOCKET_PATH) or ".")
RATE_PATH = os.path.join(RUNTIME_DIR, "accepted.json")

ROUTE = "/api/restart/request"
MAX_BODY = 2048             # Caddy caps at 2 KB too; never rely on the proxy alone
GATE_MAX_AGE = 300          # gate.json older than this means the executor is not running: fail closed
CSRF_BUCKET = 300
REASONS = ("not_responding", "cannot_join", "lag", "stuck_after_update", "other")
BODY_KEYS = {"csrf", "reason", "nickname", "ack_players"}
# {0,24}: an empty nickname is allowed and means "no name given". See the strip() in
# evaluate() -- whitespace-only is normalised to "" rather than stored as spaces.
NICK_RE = re.compile(r"^[A-Za-z0-9 _.\-]{0,24}$")
UA_MAX = 200

# Global, not per-IP: an attacker picks their source addresses, and there is exactly one real user
# population (four people). Per-IP limits would only make the abuse case cheaper.
LIMIT_MINUTE = (1, 60)
LIMIT_HOUR = (6, 3600)

O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)   # 0 on Windows, where this only ever runs under tests
O_BINARY = getattr(os, "O_BINARY", 0)


def log(msg):
    # stderr -> the journal. Never log a token, a body, or an unvalidated string.
    sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------- CSRF
_secret_cache = {"mtime": None, "value": None}


def read_secret():
    """Read the shared secret, cached on mtime. Missing secret means the caretaker has not run
    yet; every request then fails the CSRF check, which is the correct fail-closed answer."""
    try:
        st = os.stat(SECRET_PATH)
        if _secret_cache["mtime"] != st.st_mtime_ns:
            with open(SECRET_PATH, "rb") as f:
                _secret_cache["value"] = f.read().strip()
            _secret_cache["mtime"] = st.st_mtime_ns
        return _secret_cache["value"] or None
    except Exception:
        return None


def csrf_for(secret, bucket):
    mac = hmac.new(secret, str(int(bucket)).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")


def csrf_ok(secret, token, now):
    if not secret or not isinstance(token, str) or not token:
        return False
    bucket = int(now // CSRF_BUCKET)
    ok = False
    for b in (bucket, bucket - 1):        # accept the previous bucket so a page loaded 4 min ago works
        ok |= hmac.compare_digest(token, csrf_for(secret, b))
    return bool(ok)


# ---------------------------------------------------------------- gate (written by the executor)
def read_gate(now):
    """The executor regenerates gate.json every tick. It is how we refuse the common abuse case
    *before* touching the disk. Unreadable, unparsable or stale all mean 'not accepting'."""
    try:
        with open(GATE_PATH, "rb") as f:
            gate = json.loads(f.read(8192))
        if not isinstance(gate, dict):
            raise ValueError("not an object")
    except Exception:
        return {"accepting": False, "why_not": "the caretaker is not running", "stale": True}
    gen = gate.get("generated")
    if not isinstance(gen, (int, float)) or now - gen > GATE_MAX_AGE:
        gate = dict(gate)
        gate["accepting"] = False
        gate["why_not"] = "the caretaker is not running"
        gate["stale"] = True
    return gate


# ---------------------------------------------------------------- rate limit
class RateLimiter:
    """Accepted-POST timestamps, in memory and mirrored into the runtime dir so a plain restart of
    this daemon does not hand an attacker a fresh budget. It is *not* the durable rail: the
    executor's gate.json (30 min between restarts, 3 per 24 h, one in flight) is, and it survives
    reboots. This limiter only keeps junk off the disk."""

    def __init__(self, path=RATE_PATH):
        self.path = path
        self.lock = threading.Lock()
        self.hits = []
        try:
            with open(self.path) as f:
                self.hits = [float(x) for x in json.load(f) if isinstance(x, (int, float))]
        except Exception:
            self.hits = []

    def _persist(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.hits[-32:], f)
            os.replace(tmp, self.path)
        except Exception:
            pass       # a transient failure here must never fail a request

    def check_and_reserve(self, now):
        """Returns None when allowed (and counts the hit), else a human reason."""
        with self.lock:
            self.hits = [t for t in self.hits if now - t < LIMIT_HOUR[1]]
            for limit, window in (LIMIT_MINUTE, LIMIT_HOUR):
                if sum(1 for t in self.hits if now - t < window) >= limit:
                    return ("one request a minute is plenty" if window == 60
                            else "too many requests in the last hour")
            self.hits.append(now)
            self._persist()
            return None

    def release(self, now):
        # Only called when the write failed after the reservation, so a disk error does not eat
        # the user's one-per-minute budget.
        with self.lock:
            try:
                self.hits.remove(now)
            except ValueError:
                pass


LIMITER = None      # created in main(); the pure validator below never needs it


# ---------------------------------------------------------------- validation (pure, so it is testable)
def clean_text(value, limit):
    """Printable ASCII/latin only, control characters dropped, hard length cap. Used for the audit
    record's user-agent and nothing else. Never echoed back to a client."""
    if not isinstance(value, str):
        return ""
    out = "".join(ch for ch in value if ch.isprintable())
    return out[:limit]


def evaluate(get_header, body, gate, now, secret):
    """The whole request policy, as one pure function: headers in, (status, payload, checks, claim)
    out. The HTTP handler below is a thin shell around this so the CSRF/header matrix can be tested
    without a socket. Nothing here touches the disk.

    Status codes are exactly PLAN-v5's: 403 a header or CSRF check failed, 400 malformed,
    409 in flight or cooling down, 429 rate limited, 202 accepted.
    """
    ctype = (get_header("Content-Type") or "").split(";")[0].strip().lower()
    if ctype != "application/json":
        return 403, {"error": "forbidden", "why": "this endpoint takes application/json only"}, None, None

    if (get_header("X-Valheim-Restart") or "").strip() != "1":
        return 403, {"error": "forbidden", "why": "missing the X-Valheim-Restart header"}, None, None

    sfs = (get_header("Sec-Fetch-Site") or "").strip().lower()
    # Absent is allowed: only browsers send it, and only browsers carry credentials we did not ask
    # for. Anything a browser sends that is not same-origin is a cross-site attempt.
    if sfs and sfs != "same-origin":
        return 403, {"error": "forbidden", "why": "cross-site requests are not accepted"}, None, None

    # Origin is a secondary signal only, and deliberately never a rejection: Referrer-Policy:
    # no-referrer can make a legitimate browser send `Origin: null`.
    origin = clean_text(get_header("Origin") or "", 120)

    if body is None or len(body) > MAX_BODY:
        return 400, {"error": "bad_request", "why": "the request body is too large"}, None, None
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return 400, {"error": "bad_request", "why": "the request body is not valid JSON"}, None, None
    if not isinstance(payload, dict):
        return 400, {"error": "bad_request", "why": "the request body must be an object"}, None, None

    extra = set(payload) - BODY_KEYS
    missing = BODY_KEYS - set(payload)
    if extra or missing:
        # Deliberately does not name the offending keys: never echo unvalidated input back.
        return 400, {"error": "bad_request", "why": "the request body has the wrong fields"}, None, None

    reason = payload["reason"]
    if not isinstance(reason, str) or reason not in REASONS:
        return 400, {"error": "bad_request", "why": "that is not one of the five reasons"}, None, None
    nickname = payload["nickname"]
    if not isinstance(nickname, str):
        return 400, {"error": "bad_request",
                     "why": "a name is up to 24 letters, digits, spaces, dots, dashes or underscores"}, None, None
    # Strip first, then allow zero length: leaving the field blank (or typing only spaces) records
    # "" -- the truth, that no name was given -- instead of a placeholder that a human reading the
    # audit log could mistake for a name somebody actually typed. The page sends "" for a blank
    # field; the wording shown to people is chosen downstream, never invented here.
    nickname = nickname.strip()
    if not NICK_RE.match(nickname):
        return 400, {"error": "bad_request",
                     "why": "a name is up to 24 letters, digits, spaces, dots, dashes or underscores"}, None, None
    ack = payload["ack_players"]
    if not isinstance(ack, bool):      # a real bool: 1/"true"/"yes" are all rejected
        return 400, {"error": "bad_request", "why": "ack_players must be true or false"}, None, None

    if not csrf_ok(secret, payload["csrf"], now):
        return 403, {"error": "forbidden", "why": "this page is stale; reload it and try again"}, None, None

    # The player count comes from the gate, which root wrote; this daemon has no way to see the
    # game itself (no network, no world files) and must not guess.
    online = gate.get("players_online")
    if isinstance(online, int) and online > 0 and not ack:
        return 400, {"error": "bad_request", "why": "someone is in the world; tick the box first"}, None, None

    if not gate.get("accepting", False):
        why = gate.get("why_not") or "the hall is not taking requests right now"
        code = 429 if gate.get("rate_limited") else 409
        return code, {"error": "unavailable", "why": why}, None, None

    checks = {
        "csrf_ok": True,
        "sec_fetch_site": sfs or None,
        "content_type_ok": True,
        "custom_header_ok": True,
        "origin": origin or None,
    }
    claim = {"nickname": nickname, "reason": reason, "ack_players": ack}
    return 202, None, checks, claim


# ---------------------------------------------------------------- the one write
def write_request(req_id, record):
    """Create requests/<uuid>.json. The client never influences the name: uuid4() is generated
    here. Written as <uuid>.json.part then renamed, so the executor (which sweeps *.json only)
    can never read a half-written file; the rename is permitted in a 1730 sticky directory because
    we own the file. O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW so a pre-planted name or symlink fails
    instead of being followed."""
    final = os.path.join(REQUESTS_DIR, req_id + ".json")
    part = final + ".part"
    fd = os.open(part, os.O_CREAT | os.O_EXCL | os.O_WRONLY | O_NOFOLLOW | O_BINARY, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json.dumps(record, separators=(",", ":")).encode())
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(part)
        except Exception:
            pass
        raise
    os.rename(part, final)
    # No directory fsync: opening this directory needs read permission, which this user does not
    # have by design. A request lost to a power cut is a request the user simply files again.


# ---------------------------------------------------------------- HTTP
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "valheim-restartd/1"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # --- plumbing
    def address_string(self):
        # AF_UNIX gives client_address == '', which the base class would index into and crash on.
        return "unix"

    def log_message(self, fmt, *args):
        log("restartd " + (fmt % args))

    def log_error(self, fmt, *args):
        log("restartd error " + (fmt % args))

    def _send(self, code, payload):
        body = json.dumps(payload, separators=(",", ":")).encode() if payload is not None else b"{}"
        self.close_connection = True      # never keep-alive: a body we chose not to drain would
        self.send_response(code)          # otherwise desync the next request on this connection
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _405(self):
        # Drain a small body first: a client that sent one (a stray form POST, a probe) would
        # otherwise see its write reset instead of our answer.
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if 0 < length <= MAX_BODY:
                self.rfile.read(length)
        except Exception:
            pass
        self.send_response(405)
        self.send_header("Allow", "POST")
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    # Exactly one route exists. Everything else, every method, is 405 -- including OPTIONS, which
    # is what makes the CORS preflight fail and keeps a cross-site POST from ever being sent.
    do_GET = do_HEAD = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = lambda self: self._405()

    def do_POST(self):
        try:
            self._post()
        except Exception as e:
            log(f"restartd internal error: {type(e).__name__}")
            try:
                self._send(500, {"error": "internal"})
            except Exception:
                pass

    def _post(self):
        if self.path.split("?")[0] != ROUTE:
            return self._405()

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._send(400, {"error": "bad_request", "why": "the request body is not valid JSON"})
        if length > MAX_BODY:
            # Do not read it. Refusing early is the point of having our own cap.
            return self._send(400, {"error": "bad_request", "why": "the request body is too large"})
        body = self.rfile.read(length) if length > 0 else b""

        now = time.time()
        gate = read_gate(now)
        status, payload, checks, claim = evaluate(self.headers.get, body, gate, now, read_secret())
        if status != 202:
            log(f"restartd refused {status}")
            return self._send(status, payload)

        reserved = None
        if LIMITER is not None:
            reason = LIMITER.check_and_reserve(now)
            if reason:
                log("restartd refused 429")
                return self._send(429, {"error": "rate_limited", "why": reason})
            reserved = now

        req_id = str(uuid.uuid4())
        record = {
            "schema": 1,
            "id": req_id,
            "state": "pending_validation",
            "received_at": now,
            "source": {
                # X-Real-IP only. Caddy *appends* to a client-supplied X-Forwarded-For, so XFF is
                # attacker-controlled; task I1 strips it, but this does not depend on that.
                "remote_ip": clean_text(self.headers.get("X-Real-IP") or "", 64) or "unknown",
                "user_agent": clean_text(self.headers.get("User-Agent") or "", UA_MAX),
                "auth_realm": "viking",
                # Deliberate and permanent: the dashboard password is shared with the whole server
                # (it is the in-game password, printed in the README), so nothing downstream --
                # code or human -- may ever mistake the nickname below for an identity.
                "authenticated_identity": None,
            },
            "claim": claim,
            "checks": checks,
        }
        try:
            write_request(req_id, record)
        except Exception as e:
            if reserved is not None and LIMITER is not None:
                LIMITER.release(reserved)
            log(f"restartd could not spool a request: {type(e).__name__}")
            return self._send(500, {"error": "internal"})

        log(f"restartd accepted {req_id} reason={claim['reason']}")
        self._send(202, {"id": req_id})


if hasattr(socket, "AF_UNIX"):
    class UnixHTTPServer(socketserver.ThreadingUnixStreamServer):
        """The cleanest stdlib way to speak HTTP over AF_UNIX: http.server.HTTPServer is only
        TCPServer plus a couple of address niceties, and BaseHTTPRequestHandler itself needs
        nothing but a stream socket with rfile/wfile. So pairing the handler with
        ThreadingUnixStreamServer is the whole adaptation -- no third-party code, no subclassing of
        HTTPServer, no AF_INET anywhere in this process."""

        daemon_threads = True
        request_queue_size = 16

        def server_bind(self):
            # systemd's RuntimeDirectory= is cleaned on stop, but a SIGKILLed daemon can leave the
            # socket behind; unlink only if it really is a socket we own.
            try:
                import stat as _stat
                if _stat.S_ISSOCK(os.lstat(self.server_address).st_mode):
                    os.unlink(self.server_address)
            except FileNotFoundError:
                pass
            old = os.umask(0o177)         # the socket is born 0600, widened to 0660 below
            try:
                super().server_bind()
            finally:
                os.umask(old)
            harden_socket(self.server_address)

        def handle_error(self, request, client_address):
            log("restartd connection error")

    def harden_socket(path):
        """0660 valheim-restartd:caddy -- only Caddy may speak to us. The group has to be set at
        runtime because systemd's RuntimeDirectory= always uses the unit's own User/Group; we keep
        Group=valheim-restartd (so the 0640 secret and the 1730 requests dir stay ours) and add
        caddy as a supplementary group purely so this chgrp is permitted."""
        import grp
        gid = -1
        try:
            gid = grp.getgrnam(SOCKET_GROUP).gr_gid
        except Exception:
            log(f"restartd WARNING: no group {SOCKET_GROUP}; leaving the socket group alone")
        rundir = os.path.dirname(path)
        for target, mode in ((rundir, 0o750), (path, 0o660)):
            try:
                if gid != -1:
                    os.chown(target, -1, gid)
                os.chmod(target, mode)
            except Exception as e:
                # Loud: if this fails Caddy cannot reach us and the button is dead.
                log(f"restartd WARNING: could not set {target} to {oct(mode)} {SOCKET_GROUP}: {e}")


def main(argv):
    global LIMITER
    if "--check" in argv:
        # Syntax/serve-ability check for the deploy: no socket, no side effects.
        log("restartd --check: ok")
        return 0
    if not hasattr(socket, "AF_UNIX"):
        log("restartd: this platform has no AF_UNIX; the daemon is Linux-only")
        return 1
    if not os.path.isdir(REQUESTS_DIR):
        # Do not create it: its 1730 root:valheim-restartd ownership is the installer's job, and a
        # directory we made ourselves would be the wrong one.
        log(f"restartd: {REQUESTS_DIR} is missing; the installer must create it")
        return 1
    LIMITER = RateLimiter()
    os.umask(0o077)
    server = UnixHTTPServer(SOCKET_PATH, Handler)
    log(f"restartd listening on {SOCKET_PATH} (route {ROUTE}, {MAX_BODY} B cap)")
    try:
        server.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
            os.unlink(SOCKET_PATH)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
