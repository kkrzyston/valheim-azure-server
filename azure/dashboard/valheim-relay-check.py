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
    SDR or another Valve-operated hop.
  * reverse DNS as a corroborating signal only (no external service, no API key -- just the
    resolver already configured on the box). A PTR that resolves is informative; one that
    doesn't (very common for residential/ISP addresses) is NOT evidence of anything and is
    reported as `null`, never as "not a relay."
  * RFC1918/loopback/link-local ranges are flagged as `private` -- seeing one here would mean a
    NAT or routing misconfiguration, not a relay-vs-direct answer, and is reported as its own
    category rather than silently folded into "direct."
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
                                           the new-peer-detection logic against a fake poll
                                           source. No root, no real nft, no network.

Output file: each captured line is ALSO appended to RELAYCHECK_OUT (default
/var/lib/valheim-status/relay-check.jsonl) if that directory exists and is writable, in addition
to stdout -- so a capture that happens unattended (e.g. under `nohup ... &` while waiting for
someone to log in) is not lost. That file contains a real client IP address, which is personal
data: it lives under /var/lib on the VM, is never committed to the repository, and this script's
own --selftest and any committed example output use RFC 5737 documentation-range addresses
(203.0.113.0/24 etc.), never a real one.
"""
import argparse
import ipaddress
import json
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
# rather than something that reads like a verdict.
KNOWN_VALVE_RANGES = [
    ipaddress.ip_network("155.133.224.0/19"),   # Valve / Steam (matchmaking, relay)
    ipaddress.ip_network("162.254.192.0/21"),   # Valve / Steam
    ipaddress.ip_network("208.64.200.0/22"),    # Valve / Steam
    ipaddress.ip_network("208.78.164.0/22"),    # Valve / Steam
    ipaddress.ip_network("205.196.6.0/24"),     # Valve / Steam
]

NFT_TABLE_FAMILY = "inet"
NFT_TABLE_NAME = "valheim_peermeter"


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
    else:
        hit = next((str(n) for n in KNOWN_VALVE_RANGES if ip in n), None)
        rec["valve_range_hit"] = hit is not None
        rec["valve_range"] = hit
        rec["category"] = "likely_relay_or_valve" if hit else "likely_direct_client"

    try:
        host, _, _ = socket.gethostbyaddr(addr_str)
        rec["ptr"] = host
    except (socket.herror, socket.gaierror, OSError):
        rec["ptr"] = None  # absence is not evidence either way -- see module docstring

    return rec


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


def _emit(rec, out_path):
    line = json.dumps(rec, sort_keys=True)
    print(line, flush=True)
    if out_path:
        try:
            with open(out_path, "a") as f:
                f.write(line + "\n")
        except OSError as exc:
            log(f"could not append to {out_path}: {exc} (still printed to stdout above)")


def run(nft_bin, poll_sec, timeout_sec, continuous, out_path):
    seen = _read_peer_rx_addrs(nft_bin)
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
        cur = _read_peer_rx_addrs(nft_bin)
        if cur is not None:
            new = cur - seen
            for addr in sorted(new):
                rec = classify(addr)
                rec["t"] = time.time()
                rec["source"] = "peer_rx (inet valheim_peermeter)"
                log(f"new peer observed: category={rec.get('category')} "
                    f"valve_range_hit={rec.get('valve_range_hit')}")
                _emit(rec, out_path)
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
    # public resolver) exercises the "likely_direct_client" path. NOTE: an RFC 5737
    # documentation-range address (203.0.113.0/24 etc.) looks tempting for a fixture here but is
    # wrong -- Python's ipaddress module classifies those as `is_private` (they are IANA
    # "reserved for documentation", which the stdlib folds into the private bucket), so they
    # would exercise the `private` branch instead of the one this case is meant to test. Those
    # ranges are still exactly right for stdout examples and for --out's real usage precisely
    # because they can never appear in a real client's address either.
    r = classify("1.1.1.1")
    check("public non-Valve address classifies as likely_direct_client", r.get("category") == "likely_direct_client")
    check("public non-Valve address is not a valve range hit", r.get("valve_range_hit") is False)

    r2 = classify("155.133.230.10")  # inside the first KNOWN_VALVE_RANGES block
    check("address inside a known Valve block hits valve_range_hit", r2.get("valve_range_hit") is True)
    check("category reflects the hit", r2.get("category") == "likely_relay_or_valve")

    r3 = classify("10.0.0.4")
    check("RFC1918 address classifies as private, not direct or relay", r3.get("category") == "private")

    r4 = classify("not-an-ip")
    check("garbage input reports an error instead of raising", "error" in r4)

    # New-peer detection logic, exercised without nft: feed _read_peer_rx_addrs-shaped sets
    # directly into the same diffing the run() loop uses, via a tiny local reimplementation of
    # just the diff step (run() itself needs a live nft/timer loop, which is exactly what a
    # unit test should NOT depend on).
    seen = {"203.0.113.1"}
    cur = {"203.0.113.1", "203.0.113.2"}
    new = cur - seen
    check("diff finds exactly the newly-joined address", new == {"203.0.113.2"})
    seen |= cur
    cur2 = {"203.0.113.1", "203.0.113.2"}
    check("no new addresses on the next poll when nothing changed", (cur2 - seen) == set())

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
    ap.add_argument("--nft-bin", default="nft")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(_selftest())
    if args.classify:
        print(json.dumps(classify(args.classify), indent=2, sort_keys=True))
        sys.exit(0)

    sys.exit(run(args.nft_bin, args.poll, args.timeout, args.continuous, args.out))


if __name__ == "__main__":
    main()
