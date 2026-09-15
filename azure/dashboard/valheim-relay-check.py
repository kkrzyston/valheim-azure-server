#!/usr/bin/env python3
"""valheim-relay-check.py -- one-time capture: is the next peer that talks to the game port a
real client address, or a Valve/Steam Datagram Relay (SDR) address?

WHY: crossplay is OFF (no -crossplay in the unit's ExecStart), so clients are EXPECTED to
connect directly rather than through SDR. But the game log only ever records
`Got connection SteamID <id>` -- no IP -- and the dashboard's ip_of map is empty while the
server is idle, so "probably direct" has never actually been checked against a real address.
This script closes that gap the cheap way: watch the peer address nftables already tracks (this
box's `inet valheim_peermeter` table, from valheim-peermeter-nft.sh -- a table this same agent's
work installed, so no dependency on any other agent's table or script), and the moment a NEW
address shows up, classify it against Valve's published network ranges.

Classification is a heuristic, not an authority, and says so in its own output:
  * a small built-in table of Valve Corporation / Steam Datacenter CIDR ranges (AS32590 and the
    classic Steam matchmaking/relay blocks that have been publicly documented for years -- see
    KNOWN_VALVE_RANGES below for the list and its sourcing). A hit here is strong evidence of
    SDR or another Valve-operated hop, reported as `category=likely_relay_or_valve` with
    `confidence=medium_high` (the table is community-sourced, not Valve's live authoritative
    list, so even a hit stops short of "confidence=definitive").
  * a MISS against that table is NOT evidence of anything -- the table is small, community
    sourced, and admittedly possibly stale, so a real SDR relay outside these five blocks is a
    live possibility. A miss is reported as `category=no_known_relay_range_hit`, a fact about
    the table rather than a verdict on the peer, with `confidence=low`. Earlier revisions of
    this script called that branch `likely_direct_client`, which a downstream consumer reading
    only `category` (not this docstring) could and did misread as "confirmed direct" -- exactly
    the confusion `confidence` and the rename now guard against.
  * reverse DNS as a corroborating signal only (no external service, no API key -- just the
    resolver already configured on the box). A PTR that resolves is informative; one that
    doesn't (very common for residential/ISP addresses) is NOT evidence of anything and is
    reported as `null`, never as "not a relay."
  * RFC1918/loopback/link-local ranges are flagged as `private` with `confidence=definitive` --
    seeing one here would mean a NAT or routing misconfiguration, not a relay-vs-direct answer,
    and is reported as its own category rather than silently folded into "direct."
Nothing here is a network call to a third party: no external API, no telemetry leaves the box
beyond the DNS lookup the box already makes routinely.

Modes:
  valheim-relay-check.py                  wait for the next NEW peer address, classify it, print
                                           one JSON line, exit 0. (This is the "one-time capture"
                                           the brief asks for.)
  valheim-relay-check.py --continuous     same, but keep running and print one line per new peer
                                           instead of exiting after the first.
  valheim-relay-check.py --timeout N      give up after N seconds with no new peer (default: no
                                           timeout -- wait indefinitely, since nobody is online
                                           right now and the whole point is to catch the NEXT
                                           join whenever it happens).
  valheim-relay-check.py --classify IP    classify one address with no nft, no root, no polling
                                           loop -- for testing the classifier itself in isolation.
  valheim-relay-check.py --selftest       offline tests: classifier against known fixtures, and
                                           the new-peer-detection logic exercised through the
                                           real run() loop against a fake poll source. No root,
                                           no real nft, no network.

Output file and retention: each captured line is ALSO appended to RELAYCHECK_OUT (default
/var/lib/valheim-status/relay-check.jsonl) if that directory exists and is writable, in addition
to stdout -- so a capture that happens unattended (e.g. under `nohup ... &` while waiting for
someone to log in, potentially for hours) is not lost. `--continuous` is explicitly designed to
run unattended for long stretches, so this file is bounded on EVERY write, not by a separate
cleanup job that could be skipped or forgotten:
  * `--retain-days N` (default 7) drops any persisted record older than N days. Seven days is
    conservative for a debugging capture that exists to answer "is the game port reachable
    directly" -- long enough to cover a single investigation across a weekend, short enough that
    an unattended `--continuous` run does not quietly accumulate weeks of player address history.
  * `--max-lines N` (default 500) additionally caps the file to its N most recent surviving
    records, oldest dropped first, regardless of age. At roughly one line per new peer this is
    already a generous number of distinct joins for a tool meant to answer one question, and it
    bounds worst-case file growth even if `--retain-days` were set very high.
  * Both bounds are enforced by rewriting the file (prune, then append, then truncate to
    `--max-lines`) every time a new record is about to be written, BEFORE the write happens --
    so the bound holds even if the process is killed and restarted arbitrarily many times; there
    is no separate "cleanup pass" that could be skipped.
  * The persisted record's `ip` field is NOT the full client address -- see `_mask_ip_for_disk`
    below. The full address is still what gets classified and printed to stdout for the operator
    watching in real time (this script's whole purpose is determining direct-vs-relay for that
    one moment), but nothing about answering that question requires keeping the exact host
    address around afterwards: the classification (`category`, `valve_range_hit`, `confidence`)
    is already computed and persisted, and a /24 (IPv4) or /48 (IPv6) network prefix is enough to
    show which range a peer came from on later review without retaining an individual player's
    exact address. That file lives under /var/lib on the VM, is never committed to the
    repository, and this script's own --selftest and any committed example output use RFC 5737
    documentation-range addresses (203.0.113.0/24 etc.), never a real one.
"""
import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time

# Built-in, offline, no external calls. Sourced from Valve's long-publicly-documented network
# footprint (AS32590 "Valve Corporation") -- these are the classic Steam matchmaking / content /
# relay blocks that have been referenced in community and vendor firewall documentation for
# Steam multiplayer services for years. This is a best-effort allowlist, not Valve's own live
# SDR relay list (which Valve exposes per-app via an authenticated Steamworks API this box has
# no key for) -- a MISS here does not prove "not a relay," it proves "not in this static table."
# That asymmetry is why the output field is named `valve_range_hit`, a fact about this table,
# rather than something that reads like a verdict, and why a miss carries category
# `no_known_relay_range_hit` (not "direct") plus `confidence=low`.
KNOWN_VALVE_RANGES = [
    ipaddress.ip_network("155.133.224.0/19"),   # Valve / Steam (matchmaking, relay)
    ipaddress.ip_network("162.254.192.0/21"),   # Valve / Steam
    ipaddress.ip_network("208.64.200.0/22"),    # Valve / Steam
    ipaddress.ip_network("208.78.164.0/22"),    # Valve / Steam
    ipaddress.ip_network("205.196.6.0/24"),     # Valve / Steam
]

NFT_TABLE_FAMILY = "inet"
NFT_TABLE_NAME = "valheim_peermeter"

DEFAULT_RETAIN_DAYS = 7
DEFAULT_MAX_LINES = 500


def log(msg):
    print("valheim-relay-check: " + msg, file=sys.stderr, flush=True)


def classify(addr_str):
    """One classification record for addr_str. Never raises on a malformed address -- returns
    an `error` field instead, because this runs unattended and a single bad sample must not
    kill the wait for every future one."""
    rec = {"ip": addr_str}
    try:
        ip = ipaddress.ip_address(addr_str)
    except ValueError as exc:
        rec["error"] = f"not a valid IP address: {exc}"
        return rec

    if ip.is_private or ip.is_loopback or ip.is_link_local:
        rec["category"] = "private"
        rec["valve_range_hit"] = False
        rec["confidence"] = "definitive"
    else:
        hit = next((str(n) for n in KNOWN_VALVE_RANGES if ip in n), None)
        rec["valve_range_hit"] = hit is not None
        rec["valve_range"] = hit
        if hit is not None:
            rec["category"] = "likely_relay_or_valve"
            rec["confidence"] = "medium_high"  # community-sourced table, not Valve's live list
        else:
            # NOT "likely_direct_client" -- a miss against a small, admittedly-possibly-stale
            # table is not evidence of directness. See module docstring.
            rec["category"] = "no_known_relay_range_hit"
            rec["confidence"] = "low"

    try:
        host, _, _ = socket.gethostbyaddr(addr_str)
        rec["ptr"] = host
    except (socket.herror, socket.gaierror, OSError):
        rec["ptr"] = None  # absence is not evidence either way -- see module docstring

    return rec


def _mask_ip_for_disk(addr_str):
    """Network-prefix form of addr_str for persistence: /24 for IPv4, /48 for IPv6. Enough to
    see which range a peer came from on later review without keeping an individual player's
    exact address around in a file with no expiry other than the retention bound below.
    Malformed input (already reported via `error` by classify()) passes through unchanged --
    there is no address to mask, and the record's `error` field already flags it."""
    try:
        ip = ipaddress.ip_address(addr_str)
    except ValueError:
        return addr_str
    prefix = 24 if ip.version == 4 else 48
    net = ipaddress.ip_network(f"{addr_str}/{prefix}", strict=False)
    return str(net)


def _read_peer_rx_addrs(nft_bin):
    """Current addresses in valheim_peermeter's peer_rx set, or None on any read failure
    (nft missing, not root, table not there, unparseable JSON) -- caller treats None as
    "try again next poll," never as "the peer set is empty."""
    try:
        out = subprocess.run(
            [nft_bin, "-j", "list", "set", NFT_TABLE_FAMILY, NFT_TABLE_NAME, "peer_rx"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"nft invocation failed: {exc}")
        return None
    if out.returncode != 0:
        log(f"nft exited {out.returncode}: {out.stderr.strip()[:200]}")
        return None
    try:
        blob = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        log(f"nft -j produced unparseable JSON: {exc}")
        return None

    addrs = set()
    for node in blob.get("nftables", []):
        s = node.get("set") if isinstance(node, dict) else None
        if not isinstance(s, dict) or s.get("name") != "peer_rx":
            continue
        for e in s.get("elem", []) or []:
            inner = e.get("elem", e) if isinstance(e, dict) else e
            val = inner.get("val") if isinstance(inner, dict) else inner
            if isinstance(val, str):
                addrs.add(val)
    return addrs


def _prune_records(records, retain_days, max_lines, now=None):
    """Bound a list of parsed JSONL records by age (retain_days, using each record's own `t`
    timestamp -- a record with no usable `t` is treated as expired, never kept indefinitely) and
    by count (max_lines, oldest dropped first). Pure function so the bound is independently
    testable from the file I/O around it."""
    if now is None:
        now = time.time()
    cutoff = now - (retain_days * 86400)
    kept = [r for r in records if isinstance(r.get("t"), (int, float)) and r["t"] >= cutoff]
    kept.sort(key=lambda r: r["t"])
    if max_lines is not None and len(kept) > max_lines:
        kept = kept[-max_lines:]
    return kept


def _write_bounded(rec, out_path, retain_days, max_lines):
    """Append rec to out_path, enforcing retention on EVERY write (not a separate cleanup pass)
    so the bound holds even across kill/restart: read whatever is already there, drop anything
    expired or beyond the count cap, add the new record (with its disk-safe masked ip), then
    rewrite the file in one shot. Best-effort: any failure here is logged, never raised -- a
    write hiccup must not kill the wait for the next join."""
    disk_rec = dict(rec)
    if "ip" in disk_rec:
        disk_rec["ip"] = _mask_ip_for_disk(disk_rec["ip"])

    existing = []
    try:
        if os.path.exists(out_path):
            with open(out_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        existing.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue  # a corrupt old line is dropped, not fatal
    except OSError as exc:
        log(f"could not read {out_path} for retention pruning: {exc} (will still try to write)")

    kept = _prune_records(existing, retain_days, max_lines, now=rec.get("t"))
    kept.append(disk_rec)
    kept = _prune_records(kept, retain_days, max_lines, now=rec.get("t"))

    try:
        tmp_path = out_path + ".tmp"
        with open(tmp_path, "w") as f:
            for r in kept:
                f.write(json.dumps(r, sort_keys=True) + "\n")
        os.replace(tmp_path, out_path)
    except OSError as exc:
        log(f"could not write {out_path}: {exc} (record was still printed to stdout)")


def _emit(rec, out_path, retain_days=DEFAULT_RETAIN_DAYS, max_lines=DEFAULT_MAX_LINES):
    line = json.dumps(rec, sort_keys=True)
    print(line, flush=True)
    if out_path:
        _write_bounded(rec, out_path, retain_days, max_lines)


def run(nft_bin, poll_sec, timeout_sec, continuous, out_path,
        retain_days=DEFAULT_RETAIN_DAYS, max_lines=DEFAULT_MAX_LINES,
        read_fn=None, capture=None):
    """Main capture loop. `read_fn`, if given, replaces the live `_read_peer_rx_addrs(nft_bin)`
    call -- this is what lets --selftest exercise this exact function (diff, timeout, emit) with
    a fake, deterministic sequence of peer sets instead of re-implementing the diff separately.
    `capture`, if given, is a list that also receives every emitted record, so a test can assert
    on what was actually produced without parsing stdout or touching a real file."""
    if read_fn is None:
        read_fn = lambda: _read_peer_rx_addrs(nft_bin)

    seen = read_fn()
    if seen is None:
        log("could not read the peer set on startup -- is this running as root, and is "
            "valheim-peermeter-nft.sh installed? Waiting and retrying rather than giving up, "
            "since a transient nft hiccup should not abort a capture that might wait hours "
            "for the next join.")
        seen = set()
    log(f"watching for new peers ({len(seen)} already present at startup, which are NOT "
        f"reported -- only NEW joins after this process started count as \"the next join\")")

    start = time.monotonic()
    captured_any = False
    while True:
        if timeout_sec is not None and (time.monotonic() - start) >= timeout_sec:
            log(f"timeout ({timeout_sec}s) with no new peer observed")
            return 0 if captured_any else 2
        cur = read_fn()
        if cur is not None:
            new = cur - seen
            for addr in sorted(new):
                rec = classify(addr)
                rec["t"] = time.time()
                rec["source"] = "peer_rx (inet valheim_peermeter)"
                log(f"new peer observed: category={rec.get('category')} "
                    f"valve_range_hit={rec.get('valve_range_hit')}")
                _emit(rec, out_path, retain_days, max_lines)
                if capture is not None:
                    capture.append(rec)
                captured_any = True
            seen |= cur
            if new and not continuous:
                return 0
        time.sleep(poll_sec)


def _selftest():
    fails = 0

    def check(label, cond):
        nonlocal fails
        print(("  ok   " if cond else "  FAIL ") + label)
        if not cond:
            fails += 1

    # Classifier: a well-known, globally-routable, definitely-not-Valve address (Cloudflare's
    # public resolver) exercises the "no_known_relay_range_hit" path. NOTE: an RFC 5737
    # documentation-range address (203.0.113.0/24 etc.) looks tempting for a fixture here but is
    # wrong -- Python's ipaddress module classifies those as `is_private` (they are IANA
    # "reserved for documentation", which the stdlib folds into the private bucket), so they
    # would exercise the `private` branch instead of the one this case is meant to test. Those
    # ranges are still exactly right for stdout examples and for the persisted file precisely
    # because they can never appear in a real client's address either.
    r = classify("1.1.1.1")
    check("public non-Valve address classifies as no_known_relay_range_hit", r.get("category") == "no_known_relay_range_hit")
    check("public non-Valve address is not a valve range hit", r.get("valve_range_hit") is False)
    check("a miss carries low confidence, not a direct-client verdict", r.get("confidence") == "low")

    r2 = classify("155.133.230.10")  # inside the first KNOWN_VALVE_RANGES block
    check("address inside a known Valve block hits valve_range_hit", r2.get("valve_range_hit") is True)
    check("category reflects the hit", r2.get("category") == "likely_relay_or_valve")
    check("a hit carries higher (but not definitive) confidence", r2.get("confidence") == "medium_high")

    r3 = classify("10.0.0.4")
    check("RFC1918 address classifies as private, not direct or relay", r3.get("category") == "private")
    check("private classification is definitive", r3.get("confidence") == "definitive")

    r4 = classify("not-an-ip")
    check("garbage input reports an error instead of raising", "error" in r4)

    # IP masking for disk persistence: /24 for IPv4, /48 for IPv6, so an on-disk record never
    # carries an individual player's exact address.
    check("IPv4 masked to /24 for disk", _mask_ip_for_disk("203.0.113.42") == "203.0.113.0/24")
    check("IPv6 masked to /48 for disk", _mask_ip_for_disk("2001:db8:1234:5678::42") == "2001:db8:1234::/48")

    # Bounded retention: age cutoff and max-line cap, exercised as a pure function against
    # synthetic records (no real file needed for this part).
    now = 1_000_000.0
    day = 86400.0
    old = [{"t": now - 10 * day, "ip": "x"}]  # older than the 7-day default -> pruned
    fresh = [{"t": now - 1 * day, "ip": "y"}]
    pruned = _prune_records(old + fresh, retain_days=7, max_lines=500, now=now)
    check("records older than retain_days are dropped", pruned == fresh)

    many = [{"t": now - i, "ip": str(i)} for i in range(10)]
    capped = _prune_records(many, retain_days=7, max_lines=3, now=now)
    check("max_lines keeps only the most recent N records", len(capped) == 3)
    check("max_lines keeps the newest, not the oldest", {r["ip"] for r in capped} == {"0", "1", "2"})

    # Retention bound holds across a simulated kill/restart: _write_bounded is called fresh each
    # time (as it would be by a freshly-started process), reading whatever the previous process
    # left on disk, so the cap is enforced on every write rather than by a separate job.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out_path = os.path.join(td, "relay-check.jsonl")
        for i in range(5):
            rec = {"ip": f"203.0.113.{i}", "t": now + i, "category": "no_known_relay_range_hit"}
            _write_bounded(rec, out_path, retain_days=7, max_lines=3)
        with open(out_path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        check("bound holds across repeated independent writes (simulated restarts)", len(lines) == 3)
        check("persisted ip is masked, not the exact address", all("/24" in l["ip"] for l in lines))

    # New-peer detection, exercised through the REAL run() loop (not a re-implementation of the
    # diff): a fake read_fn feeds run() a scripted sequence of peer sets, and `capture` collects
    # whatever run() actually emits, so this test would fail if run()'s diff/emit logic broke.
    seq = [
        {"203.0.113.1"},                       # startup snapshot: nothing "new" yet
        {"203.0.113.1", "203.0.113.2"},        # one new peer appears
        {"203.0.113.1", "203.0.113.2"},        # unchanged -- must not double-report
    ]
    it = iter(seq)

    def fake_read():
        try:
            return next(it)
        except StopIteration:
            return seq[-1]

    captured = []
    rc = run(nft_bin="unused", poll_sec=0, timeout_sec=0.05, continuous=True,
             out_path=None, read_fn=fake_read, capture=captured)
    check("run() exits 0 once it has captured at least one new peer before timing out", rc == 0)
    check("run() reports exactly one new-peer record via the real loop", len(captured) == 1)
    check("the reported record is the actually-new address", captured and captured[0]["ip"] == "203.0.113.2")

    print("")
    if fails == 0:
        print("selftest passed")
        return 0
    print(f"selftest FAILED ({fails})")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--continuous", action="store_true", help="keep watching after the first new peer")
    ap.add_argument("--timeout", type=float, default=None, help="give up after N seconds with no new peer")
    ap.add_argument("--poll", type=float, default=2.0, help="poll interval in seconds (default 2)")
    ap.add_argument("--classify", metavar="IP", help="classify one address and exit; no nft, no polling")
    ap.add_argument("--out", default="/var/lib/valheim-status/relay-check.jsonl",
                     help="append captures here too, best-effort (default: %(default)s)")
    ap.add_argument("--retain-days", type=float, default=DEFAULT_RETAIN_DAYS,
                     help="drop persisted records older than this many days, enforced on every "
                          "write (default: %(default)s)")
    ap.add_argument("--max-lines", type=int, default=DEFAULT_MAX_LINES,
                     help="cap persisted records to the most recent N, enforced on every write "
                          "(default: %(default)s)")
    ap.add_argument("--nft-bin", default="nft")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(_selftest())
    if args.classify:
        print(json.dumps(classify(args.classify), indent=2, sort_keys=True))
        sys.exit(0)

    sys.exit(run(args.nft_bin, args.poll, args.timeout, args.continuous, args.out,
                  args.retain_days, args.max_lines))


if __name__ == "__main__":
    main()
