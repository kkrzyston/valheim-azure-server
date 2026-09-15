#!/usr/bin/env python3
"""valheim-peermeter-read.py -- one-shot JSON reader for the `inet valheim_peermeter` nftables
table (installed by valheim-peermeter-nft.sh), which is a SEPARATE table from the one
valheim-egress-probe.py reads (`inet valheim_meter`). This script does not touch that probe or
its table; it is a standalone tool for the new per-peer byte accounting.

WHY THIS EXISTS: the aggregate counters in valheim_meter can only say how many total bytes left
the box. They cannot say whether the ~270 KiB/s ceiling is a budget shared across every player
or a budget each player gets on their own -- that requires bytes broken out BY peer, which is
what valheim_peermeter's peer_tx/peer_rx sets carry (one counter per source/destination address,
maintained in-kernel by nftables "meter" update rules; see valheim-peermeter-nft.sh for how and
why that mechanism was chosen).

Output is one JSON object per invocation, to stdout, matching the "drop bad samples rather than
fabricate" convention the egress probe uses: if `nft -j` fails, or its output does not parse, or
an element's fields are not the numbers they are supposed to be, that element (or the whole
sample) is left out and reported in an `errors` list -- never guessed at or zero-filled, because
a zero here would be indistinguishable from "this peer genuinely sent nothing."

Usage:
  valheim-peermeter-read.py                 print one JSON sample of current per-peer counters
  valheim-peermeter-read.py --pretty         same, human-readable indentation
  valheim-peermeter-read.py --selftest       parser + drop-rule tests against fixtures; no root,
                                              no real nft, no network

Sample shape:
  {
    "t": <unix time, float>,
    "table": "inet valheim_peermeter",
    "ok": true,
    "peers": {
      "<ipv4>": {"tx_bytes": N, "tx_packets": N, "rx_bytes": N, "rx_packets": N,
                 "tx_expires_s": N or null, "rx_expires_s": N or null}
    },
    "errors": []
  }

A peer address present in only peer_tx or only peer_rx (traffic flowing one direction right now,
or the other direction's element having already timed out) still gets an entry, with the missing
side's fields left null rather than 0 -- 0 would claim "definitely sent nothing," null says
"nothing observed in this sample," which is the honest claim for a value that ages out on its
own after VALHEIM_PEER_TIMEOUT.

Costs: one short-lived `nft -j list table` call, nothing else. No files written, no state kept
between invocations -- deltas across time are the caller's job (pipe consecutive samples to a
file, same pattern the egress probe's own external consumers already use).
"""
import argparse
import json
import subprocess
import sys
import time

NFT_TABLE_FAMILY = "inet"
NFT_TABLE_NAME = "valheim_peermeter"


def log(msg):
    print("valheim-peermeter-read: " + msg, file=sys.stderr, flush=True)


def _elem_val_and_expires(e):
    """A set element with `flags timeout` comes back from `nft -j` as
    {"elem": {"val": <addr>, "expires": <seconds-remaining>, "counter": {...}}}; without a
    timeout it would be a bare string with no counter. This table always has flags timeout, but
    handle the bare-string shape too rather than assume the ruleset can never change under us."""
    if isinstance(e, dict):
        inner = e.get("elem", e)
        if isinstance(inner, dict):
            return inner.get("val"), inner.get("expires"), inner.get("counter")
        return inner, None, None
    return e, None, None


def parse_table(blob, errors):
    """({addr: {"tx": (bytes,packets,expires) or None, "rx": (...) or None}}, ) from the parsed
    JSON of `nft -j list table inet valheim_peermeter`.

    Every element is validated before being trusted: val must be a string that looks like an
    IPv4 address, counter must carry integer bytes and packets. Anything else is recorded in
    `errors` and the element is skipped -- it does not become a zero, and it does not abort the
    whole sample (one malformed element must not hide every peer that parsed fine)."""
    by_addr = {}
    found_sets = set()
    for node in blob.get("nftables", []):
        if not isinstance(node, dict):
            continue
        s = node.get("set")
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if name not in ("peer_tx", "peer_rx"):
            continue
        found_sets.add(name)
        for e in s.get("elem", []) or []:
            val, expires, counter = _elem_val_and_expires(e)
            if not isinstance(val, str) or val.count(".") != 3:
                errors.append(f"{name}: element with a non-IPv4 val, skipped: {val!r}")
                continue
            if not isinstance(counter, dict):
                errors.append(f"{name}: element {val} has no counter data, skipped")
                continue
            try:
                b = int(counter["bytes"])
                p = int(counter["packets"])
            except (KeyError, TypeError, ValueError):
                errors.append(f"{name}: element {val} has a non-numeric counter, skipped")
                continue
            if b < 0 or p < 0:
                errors.append(f"{name}: element {val} has a negative counter, skipped")
                continue
            exp = None
            if expires is not None:
                try:
                    exp = float(expires)
                except (TypeError, ValueError):
                    exp = None
            rec = by_addr.setdefault(val, {"tx": None, "rx": None})
            key = "tx" if name == "peer_tx" else "rx"
            rec[key] = (b, p, exp)
    for want in ("peer_tx", "peer_rx"):
        if want not in found_sets:
            errors.append(f"set {want} not found in table output -- table may be missing or incomplete")
    return by_addr


def read_sample(nft_bin="nft"):
    errors = []
    try:
        out = subprocess.run(
            [nft_bin, "-j", "list", "table", NFT_TABLE_FAMILY, NFT_TABLE_NAME],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"t": time.time(), "table": f"{NFT_TABLE_FAMILY} {NFT_TABLE_NAME}",
                "ok": False, "peers": {}, "errors": [f"nft invocation failed: {exc}"]}
    if out.returncode != 0:
        return {"t": time.time(), "table": f"{NFT_TABLE_FAMILY} {NFT_TABLE_NAME}",
                "ok": False, "peers": {},
                "errors": [f"nft exited {out.returncode}: {out.stderr.strip()[:300]}"]}
    try:
        blob = json.loads(out.stdout)
    except json.JSONDecodeError as exc:
        return {"t": time.time(), "table": f"{NFT_TABLE_FAMILY} {NFT_TABLE_NAME}",
                "ok": False, "peers": {}, "errors": [f"nft -j produced unparseable JSON: {exc}"]}

    by_addr = parse_table(blob, errors)
    peers = {}
    for addr, rec in by_addr.items():
        tx = rec["tx"]
        rx = rec["rx"]
        peers[addr] = {
            "tx_bytes": tx[0] if tx else None,
            "tx_packets": tx[1] if tx else None,
            "tx_expires_s": tx[2] if tx else None,
            "rx_bytes": rx[0] if rx else None,
            "rx_packets": rx[1] if rx else None,
            "rx_expires_s": rx[2] if rx else None,
        }
    ok = not any("table may be missing" in e for e in errors)
    return {"t": time.time(), "table": f"{NFT_TABLE_FAMILY} {NFT_TABLE_NAME}",
            "ok": ok, "peers": peers, "errors": errors}


# ---------------------------------------------------------------- selftest (no root, no nft)
def _selftest():
    fails = 0

    def check(label, cond):
        nonlocal fails
        if cond:
            print(f"  ok   {label}")
        else:
            print(f"  FAIL {label}")
            fails += 1

    # 1. A clean sample with one address in both sets.
    blob = {"nftables": [
        {"set": {"name": "peer_tx", "elem": [
            {"elem": {"val": "203.0.113.9", "expires": 90, "counter": {"bytes": 5000, "packets": 40}}}
        ]}},
        {"set": {"name": "peer_rx", "elem": [
            {"elem": {"val": "203.0.113.9", "expires": 85, "counter": {"bytes": 1200, "packets": 30}}}
        ]}},
    ]}
    errors = []
    by_addr = parse_table(blob, errors)
    check("clean sample: no errors", errors == [])
    check("clean sample: one address", list(by_addr.keys()) == ["203.0.113.9"])
    check("clean sample: tx bytes correct", by_addr["203.0.113.9"]["tx"][0] == 5000)
    check("clean sample: rx packets correct", by_addr["203.0.113.9"]["rx"][1] == 30)

    # 2. One-sided traffic: address only in peer_tx (e.g. rx element already timed out).
    blob2 = {"nftables": [
        {"set": {"name": "peer_tx", "elem": [
            {"elem": {"val": "198.51.100.4", "expires": 10, "counter": {"bytes": 800, "packets": 6}}}
        ]}},
        {"set": {"name": "peer_rx", "elem": []}},
    ]}
    errors2 = []
    by_addr2 = parse_table(blob2, errors2)
    check("one-sided: no errors", errors2 == [])
    check("one-sided: rx is None, not 0", by_addr2["198.51.100.4"]["rx"] is None)

    # 3. Malformed element: non-numeric counter must be dropped, not crash, and must be reported.
    blob3 = {"nftables": [
        {"set": {"name": "peer_tx", "elem": [
            {"elem": {"val": "198.51.100.5", "expires": 10, "counter": {"bytes": "oops", "packets": 6}}},
            {"elem": {"val": "198.51.100.6", "expires": 10, "counter": {"bytes": 10, "packets": 1}}},
        ]}},
        {"set": {"name": "peer_rx", "elem": []}},
    ]}
    errors3 = []
    by_addr3 = parse_table(blob3, errors3)
    check("malformed: bad element dropped, not zero-filled", "198.51.100.5" not in by_addr3)
    check("malformed: sibling element still parsed", "198.51.100.6" in by_addr3)
    check("malformed: an error was recorded", any("non-numeric" in e for e in errors3))

    # 4. Non-IPv4 val (should not happen with type ipv4_addr, but must not crash if it does).
    blob4 = {"nftables": [
        {"set": {"name": "peer_tx", "elem": [{"elem": {"val": "not-an-ip", "counter": {"bytes": 1, "packets": 1}}}]}},
        {"set": {"name": "peer_rx", "elem": []}},
    ]}
    errors4 = []
    by_addr4 = parse_table(blob4, errors4)
    check("non-ipv4 val: dropped, not crashed", by_addr4 == {})
    check("non-ipv4 val: error recorded", any("non-IPv4" in e for e in errors4))

    # 5. Missing set entirely (table present but incomplete, e.g. mid-install) is flagged.
    blob5 = {"nftables": [{"set": {"name": "peer_tx", "elem": []}}]}
    errors5 = []
    parse_table(blob5, errors5)
    check("missing peer_rx set is reported", any("peer_rx not found" in e for e in errors5))

    print("")
    if fails == 0:
        print("selftest passed")
        return 0
    print(f"selftest FAILED ({fails})")
    return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pretty", action="store_true", help="indent the JSON output")
    ap.add_argument("--selftest", action="store_true", help="run offline parser tests and exit")
    ap.add_argument("--nft-bin", default="nft", help="path to nft (default: nft on PATH)")
    args = ap.parse_args()

    if args.selftest:
        sys.exit(_selftest())

    sample = read_sample(args.nft_bin)
    if sample["errors"]:
        for e in sample["errors"]:
            log(e)
    print(json.dumps(sample, indent=2 if args.pretty else None, sort_keys=True))
    sys.exit(0 if sample["ok"] else 1)


if __name__ == "__main__":
    main()
