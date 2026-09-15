#!/usr/bin/env python3
"""valheim-egress-report.py -- offline analysis of the 1 Hz egress samples. Run by hand.

THE HYPOTHESIS UNDER TEST (H): game egress is limited by Valheim's own per-peer send budget
(ZDOMan's m_dataPerSec), not by the VM, the kernel or the network. If H holds, adding players or
world activity does not buy more bytes -- it buys staler updates, which is what players describe
when they say it only stutters when everyone is in one place.

This script exists to be able to say NO. A plateau in a graph is not evidence of a ceiling: a
server that is simply never asked for more than 250 KB/s draws the same flat line. So it leads
with six refutation conditions and prints REFUTED the moment one fires:

  R1  any clean sample above the arithmetic ceiling (+5%). A budget you exceed is not a budget.
  R2  the plateau does not scale with player count. A per-PEER budget must be per-peer; a level
      that is the same for two players and for five is some other limit wearing its clothes.
  R3  players report lag while egress is well below the ceiling. Then the ceiling, real or not,
      is not what they are feeling, and raising it would fix nothing.
  R4  mean packet size inside plateaus is small. A saturated BYTE budget fills packets.
  R5  the socket send queue is non-empty. Then the kernel is holding bytes back.
  R6  A2S round trips spike while egress is below the ceiling.

AND A POSITIVE VERDICT IS ONLY EVER AS STRONG AS THE TESTS THAT ACTUALLY RAN. Every gate below
is conditional on its own evidence: with one player count there is no slope to measure, so R2 has
not passed -- it has not been attempted -- and the verdict is INCONCLUSIVE, not CONFIRMED. An
earlier version printed "they scale with player count" three lines above a caveat saying that
test could not run. That is the failure mode this whole report is supposed to guard against, so
it now refuses to print prose for a test it did not perform.

READ THIS BEFORE TRUSTING ANY VERDICT. The per-peer prior is weak and the arithmetic says so.
The 30-day maximum from the per-minute collector data is 276,425 B/s; 4.5 x 61440 = 276,480,
which is exactly 270 KiB/s -- the observed hard maximum sits within 55 bytes, 0.02%, of a round
GLOBAL number, and that is not what n x 61440 looks like for the three players who were online.
The operator has since measured observed maxima exceeding n x 61440 by 12-51% at n=1..4, which
refutes the per-peer model outright on that data. A single server-wide cap is currently the
better-supported story. R2 is the test that separates them -- a global cap produces a plateau
that does not scale with n -- which is exactly why a verdict reached without two or more player
counts is worthless, and why --budget is a parameter rather than a constant.

The strongest positive test is the matched-window comparison: raid windows against non-raid
windows at the same player count AND the same INBOUND rate. A raid is a large, involuntary,
externally-timed increase in world activity. If egress is indistinguishable across it, demand
rose and supply did not -- which is what supply-limited means.

Inputs (all read-only, none modified):
  /var/lib/valheim-status/egress-YYYY-MM-DD.jsonl   1 Hz samples from valheim-egress-probe.py
  /var/lib/valheim-status/samples.jsonl             per-minute collector samples (`pg` peer ping)
  /var/lib/valheim-status/events.jsonl              raid markers and `!lag` reports

  python3 valheim-egress-report.py                  full report over everything on disk
  python3 valheim-egress-report.py --days 7         only the last 7 days
  python3 valheim-egress-report.py --selftest       seventeen synthetic worlds with known answers

A NOTE ON WHAT THE BYTES ARE. nftables counters at the filter hooks count what the kernel sees at
layer 3: IP header + UDP header + payload, 28 bytes per packet. They do NOT include the 14-byte
Ethernet header. So --hdr defaults to 28, not the 42 you would use against a NIC counter. The
meter also keeps the Steam query port in its own counter, so master-server scrapes are not in
txb; if you are comparing against samples.jsonl's `tx` (a NIC counter) expect it to read higher
for both reasons.
"""
import argparse
import glob
import json
import math
import os
import random
import statistics
import sys
import time
from datetime import datetime

LIB = os.environ.get("EGRESS_DIR", "").strip() or "/var/lib/valheim-status"
BUDGET = 61440          # a figure from an older build than the one running here -- a parameter
HDR = 28                # IP(20) + UDP(8), which is what an nftables counter counts
DEFAULT_PAYLOAD = 1200  # only used when the data has no usable packet-size measurement


# ---------------------------------------------------------------- small stats, stdlib only
def mean(xs):
    return statistics.fmean(xs) if xs else float("nan")


def cv(xs):
    """Coefficient of variation. The flatness statistic: a saturated budget is very flat. The
    60-second collector data showed a loose +/-6% band, which is NOT flat enough to conclude
    anything -- a minute-scale average of a varying rate looks like that too."""
    if len(xs) < 2:
        return float("nan")
    m = mean(xs)
    return statistics.stdev(xs) / m if m else float("nan")


def pct(xs, p):
    if not xs:
        return float("nan")
    s = sorted(xs)
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] if lo == hi else s[lo] + (s[hi] - s[lo]) * (k - lo)


def perm_test(a, b, n_perm=2000, seed=1, cap=1500):
    """Two-sided permutation test on the difference of means. No scipy on this box, and a
    permutation test needs no distributional assumption anyway -- which matters, because
    per-second egress is emphatically not normal. Large p means the two windows are
    indistinguishable, which for the matched-window test is the interesting result."""
    if len(a) < 5 or len(b) < 5:
        return float("nan")
    rnd = random.Random(seed)
    a = rnd.sample(a, cap) if len(a) > cap else list(a)
    b = rnd.sample(b, cap) if len(b) > cap else list(b)
    obs = abs(mean(a) - mean(b))
    pool, na = a + b, len(a)
    hits = 0
    for _ in range(n_perm):
        rnd.shuffle(pool)
        if abs(mean(pool[:na]) - mean(pool[na:])) >= obs:
            hits += 1
    return (hits + 1) / (n_perm + 1)


def human(b):
    return f"{b/1000:,.1f} KB/s" if b == b else "n/a"


def dur(s):
    if s != s:
        return "n/a"
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s/60:.0f}m"
    if s < 2 * 86400:
        return f"{s/3600:.1f}h"
    return f"{s/86400:.1f}d"


# ---------------------------------------------------------------- the arithmetic ceiling
def predicted_ceiling(n, budget=BUDGET, payload=DEFAULT_PAYLOAD, hdr=HDR, k=0.0):
    """Counted bytes per second at the nftables hook, if every peer's ZDO budget were saturated:

        wire(n) = n*budget                      the ZDO payload itself
                + (n*budget / payload) * hdr    one header per packet needed to carry it
                + n*k                           per-peer traffic that is not ZDO data at all

    payload (S) and k are parameters, not magic numbers: S is measured from the data when the
    data has packet counts, and k is unknown without reading the game's source, so it defaults to
    0 -- which makes the predicted ceiling a LOWER bound and R1 correspondingly harder to fire.
    That is the conservative direction: it biases against the hypothesis, not toward it."""
    payload = payload if payload and payload > 0 else DEFAULT_PAYLOAD
    return n * budget + (n * budget / payload) * hdr + n * k


# ---------------------------------------------------------------- loading
def _usable(r):
    """A row is usable only if every field the analysis divides, sorts or compares by is a real
    number. A non-numeric `t` used to kill the whole report with a TypeError inside sort() --
    thirty days of data lost to a formatting problem at the last possible moment."""
    if not isinstance(r, dict):
        return False
    for key in ("t", "txb", "n"):
        v = r.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return False
    dt = r.get("dt", 1.0)
    if isinstance(dt, bool) or not isinstance(dt, (int, float)) or dt <= 0:
        return False
    return True


def normalize(rows):
    """Turn counter deltas into rates using the interval each row actually spans. Rows written
    before `dt` existed are treated as one second, which is what they claimed to be."""
    for r in rows:
        dt = r.get("dt", 1.0)
        r["_dt"] = dt
        r["_tx"] = r["txb"] / dt
        r["_rx"] = r.get("rxb", 0) / dt
        r["_pk"] = r.get("txp", 0) / dt
    return rows


def load_egress(lib, since=None):
    """(rows, skipped). The skip count is returned rather than swallowed: a file quietly dropping
    a third of its lines and a file that is fine look identical if nobody counts."""
    rows, skipped = [], 0
    for path in sorted(glob.glob(os.path.join(lib, "egress-*.jsonl"))):
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        skipped += 1
                        continue
                    if not _usable(r):
                        skipped += 1
                        continue
                    if since and r["t"] < since:
                        continue
                    rows.append(r)
        except OSError as e:
            print(f"  ! could not read {path}: {e}", file=sys.stderr)
    rows.sort(key=lambda r: r["t"])
    return normalize(rows), skipped


def load_jsonl(path, since=None):
    out, skipped = [], 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                if not isinstance(r, dict) or isinstance(r.get("t"), bool) \
                        or not isinstance(r.get("t"), (int, float)):
                    skipped += 1
                    continue
                if not since or r["t"] >= since:
                    out.append(r)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"  ! could not read {path}: {e}", file=sys.stderr)
    out.sort(key=lambda r: r["t"])
    return out, skipped


# ---------------------------------------------------------------- plateau detection
def plateaus(rows, frac=0.95, min_run=3, hi_pct=0.995):
    """Split samples by player count and mark the ones sitting at that count's own upper edge.

    A "plateau" means: at or above `frac` of the 99.5th percentile of egress for this player
    count (the 99.5th rather than the max, so one jittery second cannot define the edge), AND
    part of a run of at least `min_run` consecutive samples at that level. The run requirement is
    what separates a ceiling from a burst: a single high second is a spike, several in a row at
    the same level is a limit. Both are knobs and both are printed, because the definition of
    "plateau" does real work in the conclusion and should not be buried."""
    by_n, out = {}, {}
    for r in rows:
        by_n.setdefault(r["n"], []).append(r)
    for n, rs in by_n.items():
        if n <= 0:
            continue
        edge = pct([r["_tx"] for r in rs], hi_pct)
        thresh = edge * frac
        run, marked = [], []
        for r in rs:
            if r["_tx"] >= thresh:
                run.append(r)
            else:
                if len(run) >= min_run:
                    marked.extend(run)
                run = []
        if len(run) >= min_run:
            marked.extend(run)
        out[n] = {"all": rs, "edge": edge, "thresh": thresh, "plateau": marked}
    return out


def split_artifacts(rows, link_bps, mult, hi=0.999):
    """(clean, artifacts, bound_by_n). Separate instrument error from measurement.

    THIS IS NOT "DISCARD INCONVENIENT DATA". R1 exists to end the investigation on a sample above
    the ceiling, and that is correct science -- a budget you exceed is not a budget. But it is
    only correct when applied to a MEASUREMENT. A row reporting 15 MB/s on a host whose thirty-day
    observed maximum is 276 KB/s is not a clean overshoot of a hypothesis; it is a reading the
    machine cannot physically have produced, and promoting it to decisive evidence throws away ten
    hours of good data on the strength of one bad row. The probe now guards against writing such a
    row -- but "the other component guards against it" is exactly the reasoning that produced half
    the defects found in review, so the analysis guards too.

    Two independent bounds; a row is an artifact if it exceeds EITHER:

      * link capacity. Physically impossible, full stop.
      * `mult` times the 99.9th percentile for that player count. Absurd relative to everything
        else ever measured here. A percentile, so a handful of artifacts cannot inflate the bound
        meant to catch them -- and a GENUINE sustained overshoot raises that percentile itself, so
        it can never be caught by this bound. That asymmetry is the design: lone spikes are
        excluded, sustained excess is not.

    The second bound does the real work. A 15 MB/s row is well under a gigabit link, so the
    physical bound alone would miss it; it is ~50x the 99.9th percentile, so the relative one
    catches it. Both are needed.

    Nothing is dropped silently -- the caller prints the count, the bound and examples. Quietly
    deleting samples that disagree with the hypothesis would be a worse bias than the bug."""
    by_n = {}
    for r in rows:
        by_n.setdefault(r["n"], []).append(r)
    clean, arts, bound_by_n = [], [], {}
    for n, rs in by_n.items():
        edge = pct([r["_tx"] for r in rs], hi)
        bound = min(link_bps, mult * edge) if edge == edge and edge > 0 else link_bps
        bound_by_n[n] = bound
        for r in rs:
            (arts if r["_tx"] > bound else clean).append(r)
    clean.sort(key=lambda r: r["t"])
    arts.sort(key=lambda r: r["t"])
    return clean, arts, bound_by_n


def measured_payload(rows, hdr=HDR):
    """Mean payload bytes per packet, or None when there is nothing to measure -- which happens
    for real: build_record legitimately emits txp=0 for a silent second, and a whole window of
    those divides by zero. Every caller must handle the None; one that did not used to crash the
    report at the very end of a thirty-day run."""
    tot_b = sum(r["txb"] for r in rows if r.get("txp"))
    tot_p = sum(r["txp"] for r in rows if r.get("txp"))
    if not tot_p:
        return None
    return tot_b / tot_p - hdr


def fmt_payload(v):
    return "n/a" if v is None or v != v else f"{v:.0f}B"


# ---------------------------------------------------------------- the report
def analyse(rows, samples, events, args, skipped=0, out=print):
    fired = []          # refutation conditions that fired, with a one-line reason
    blocked = []        # missing evidence that a CONFIRMED verdict would otherwise have asserted
    caveats = []        # refutation conditions that could not be attempted -- weakens, not blocks
    #
    # The distinction matters. A CONFIRMED verdict states three things in prose: the plateaus are
    # flat, they scale with player count, and matched raids do not move them. Anything those three
    # claims rest on goes in `blocked` and forces INCONCLUSIVE. A refutation condition that simply
    # had nothing to chew on -- no lag reports, no packet counts -- did not pass, but a positive
    # verdict never claimed it did, so it belongs in `caveats` and is printed alongside.
    now = time.time()

    out("=" * 78)
    out("Valheim egress ceiling report")
    out("=" * 78)

    # ---- coverage: what evidence is actually here? --------------------------------------------
    # Answered FIRST and allowed to veto a positive verdict, because "three hours of month-old
    # data" and "thirty days of continuous data" produce identically confident-looking output
    # unless someone says how much there is.
    if not rows:
        out("No usable egress samples found. Has valheim-egress.service been running while")
        out("players were online?  systemctl status valheim-egress")
        out("")
        out("VERDICT: INCONCLUSIVE -- there is no data.")
        return "INCONCLUSIVE", [], ["no samples at all"]

    span = rows[-1]["t"] - rows[0]["t"]
    sampled = sum(r["_dt"] for r in rows)
    gaps = [rows[i + 1]["t"] - rows[i]["t"] for i in range(len(rows) - 1)]
    biggest = max(gaps) if gaps else 0.0
    newest_age = now - rows[-1]["t"]
    counts = sorted({r["n"] for r in rows if r["n"] > 0})
    out(f"requested        {('last %g days' % args.days) if args.days else 'everything on disk'}")
    out(f"wall-clock span  {dur(span)}  "
        f"({datetime.fromtimestamp(rows[0]['t']):%Y-%m-%d %H:%M} -> "
        f"{datetime.fromtimestamp(rows[-1]['t']):%Y-%m-%d %H:%M})")
    out(f"actually sampled {len(rows):,} rows, {dur(sampled)} of measured time "
        f"({sampled/span*100 if span else float('nan'):.1f}% of the span -- the probe only runs "
        f"while players are online, so this is expected to be well under 100%)")
    out(f"largest gap      {dur(biggest)}        newest sample {dur(newest_age)} old")
    out(f"player counts    {counts or 'none'}")
    if skipped:
        out(f"skipped lines    {skipped:,} unreadable or malformed "
            f"({skipped/(skipped+len(rows))*100:.1f}% of the file)")
    out("")

    if newest_age > args.max_stale * 3600:
        blocked.append(f"the newest sample is {dur(newest_age)} old (limit {args.max_stale:g}h) -- "
                       f"this is a report about the past, not about the server as it is now; "
                       f"check that valheim-egress.service is still running")
    if sampled < args.min_samples:
        blocked.append(f"only {dur(sampled)} of measured time (need {dur(args.min_samples)}); "
                       f"keep the probe running through more busy evenings")
    if skipped and skipped > args.max_skip * (skipped + len(rows)):
        blocked.append(f"{skipped/(skipped+len(rows)):.1%} of lines were unreadable (limit "
                       f"{args.max_skip:.0%}) -- fix that before trusting any of the rest")

    # Instrument error is separated from measurement BEFORE any statistic is computed, so that an
    # impossible row cannot set a percentile, define a plateau edge, or fire a refutation.
    link_bps = args.link_mbit * 1e6 / 8.0
    rows, artifacts, bound_by_n = split_artifacts(rows, link_bps, args.artifact_mult)
    out(f"discarded        {len(artifacts):,} sample(s) as physically implausible (above the "
        f"lesser of {human(link_bps)} link capacity and {args.artifact_mult:g}x the 99.9th "
        f"percentile for that player count)")
    for r in artifacts[:5]:
        out(f"                 {datetime.fromtimestamp(r['t']):%Y-%m-%d %H:%M:%S} n={r['n']} "
            f"{r['_tx']:,.0f} B/s over dt={r['_dt']:g}s -- the bound was "
            f"{bound_by_n.get(r['n'], float('nan')):,.0f} B/s")
    if len(artifacts) > 5:
        out(f"                 ...and {len(artifacts)-5:,} more")
    if artifacts and len(artifacts) > args.max_artifact * (len(rows) + len(artifacts)):
        blocked.append(f"{len(artifacts)/(len(rows)+len(artifacts)):.1%} of samples were physically "
                       f"implausible (limit {args.max_artifact:.1%}). That is an instrument fault, "
                       f"not a finding -- fix the probe before reading anything below")
    if not rows:
        out("")
        out("VERDICT: INCONCLUSIVE -- every sample was discarded as an artifact.")
        return "INCONCLUSIVE", [], ["every sample was an artifact"]
    out("")

    pay = args.payload if args.payload else measured_payload(rows, args.hdr)
    if pay is None:
        pay = DEFAULT_PAYLOAD
        blocked.append("no packet counts in the data, so packet size is assumed, not measured")
    out(f"assumed budget   {args.budget:,.0f} B/s per peer   headers {args.hdr:g} B/packet "
        f"(layer 3, as nftables counts)   k {args.k:g} B/s per peer")
    out(f"payload/packet   {pay:,.0f} B  "
        f"({'measured from the samples' if not args.payload else 'given on the command line'})")
    out("")

    pl = plateaus(rows, frac=args.plateau_frac, min_run=args.min_run)
    out(f"--- per-player-count distribution (plateau = >={args.plateau_frac:.0%} of the 99.5th "
        f"percentile, in runs of >={args.min_run}) ---")
    out(f"{'n':>3} {'samples':>8} {'plateau':>8} {'median':>12} {'p99.5':>12} {'plateau mean':>13} "
        f"{'CV':>7} {'per player':>11} {'pkt size':>9} {'predicted':>12}")
    per_player = {}
    for n in sorted(pl):
        d = pl[n]
        p = d["plateau"]
        pb = [r["_tx"] for r in p]
        pmean = mean(pb) if pb else float("nan")
        predicted = predicted_ceiling(n, args.budget, pay, args.hdr, args.k)
        if len(pb) >= args.min_plateau:
            per_player[n] = pmean / n
        out(f"{n:>3} {len(d['all']):>8,} {len(p):>8,} "
            f"{human(statistics.median([r['_tx'] for r in d['all']])):>12} "
            f"{human(d['edge']):>12} {human(pmean):>13} "
            f"{cv(pb)*100 if pb else float('nan'):>6.2f}% "
            f"{human(pmean/n) if pb else 'n/a':>11} "
            f"{fmt_payload(measured_payload(p, args.hdr)):>9} {human(predicted):>12}")
    out("")

    # ---- R1: anything above the ceiling at all -------------------------------------------------
    lim = 1.0 + args.r1_slack
    over = [r for r in rows if r["n"] > 0
            and r["_tx"] > predicted_ceiling(r["n"], args.budget, pay, args.hdr, args.k) * lim]
    worst = max((r["_tx"] / predicted_ceiling(r["n"], args.budget, pay, args.hdr, args.k)
                 for r in rows if r["n"] > 0), default=float("nan"))
    # The longest CONSECUTIVE overshoot is the statistic a lone artifact cannot manufacture:
    # producing it takes two or more bad rows back to back, and the probe re-baselines its
    # counters after any interval it could not trust, so it cannot emit two in a row.
    over_t = {r["t"] for r in over}
    run = best = 0
    for r in rows:
        run = run + 1 if r["t"] in over_t else 0
        best = max(best, run)
    out(f"R1  samples above the ceiling x{lim:.2f}: {len(over):,} of {len(rows):,}, longest "
        f"consecutive run {best} (fires at {args.r1_min} total, or {args.r1_run} consecutive)   "
        f"(the highest sample reached {worst*100:.1f}% of its predicted ceiling)")
    if len(over) >= args.r1_min or best >= args.r1_run:
        ex = max(over, key=lambda r: r["_tx"] / predicted_ceiling(r["n"], args.budget, pay, args.hdr, args.k))
        fired.append(f"R1: {len(over)} sample(s) exceeded the ceiling, longest run {best} "
                     f"consecutive, worst {ex['_tx']:,.0f} B/s at n={ex['n']} "
                     f"({datetime.fromtimestamp(ex['t']):%Y-%m-%d %H:%M:%S}). "
                     f"A budget you exceed is not a budget.")

    # ---- R2: does the plateau scale with n? ----------------------------------------------------
    spread = None
    if len(per_player) >= 2:
        lo, hi = min(per_player.values()), max(per_player.values())
        spread = (hi - lo) / hi
        out(f"R2  plateau per player across n={sorted(per_player)}: {human(lo)} .. {human(hi)}  "
            f"(spread {spread:.1%}, tolerance {args.r2_tol:.0%})")
        if spread > args.r2_tol:
            fired.append(f"R2: the plateau does not scale with player count -- per-player egress "
                         f"varies {spread:.0%} across n={sorted(per_player)}. A per-peer budget "
                         f"cannot do that; a single global cap does exactly that.")
    else:
        out(f"R2  NOT TESTABLE: needs >={args.min_plateau} plateau samples at two or more player "
            f"counts; only {sorted(per_player) or 'none'} qualify. One point has no slope.")
        blocked.append("R2 -- per-player scaling was never tested: fewer than two player counts "
                       "have enough plateau data, and one point has no slope. This is the test "
                       "that separates a per-peer budget from a single global cap, so without it "
                       "the two rival explanations are indistinguishable")

    # ---- R4: packet size inside plateaus -------------------------------------------------------
    allp = [r for n in pl for r in pl[n]["plateau"]]
    psz_all = measured_payload(allp, args.hdr) if allp else None
    if psz_all is None:
        out("R4  NOT TESTABLE: no packet counts inside plateaus")
        caveats.append("R4 -- no packet counts inside plateaus")
    else:
        out(f"R4  mean payload inside plateaus: {psz_all:.0f} B (floor {args.r4_min:g} B)")
        if psz_all < args.r4_min:
            fired.append(f"R4: mean payload inside plateaus is {psz_all:.0f} B, well short of an "
                         f"MTU. A saturated byte budget fills packets; this looks packet- or "
                         f"tick-limited.")

    # ---- R5: is the kernel holding bytes? ------------------------------------------------------
    sq = [r["sq"] for r in allp if isinstance(r.get("sq"), int) and not isinstance(r.get("sq"), bool)]
    if not sq:
        out("R5  NOT TESTABLE: no socket send-queue readings inside plateaus")
        caveats.append("R5 -- no socket send-queue readings inside plateaus")
    else:
        nz = [v for v in sq if v > 0]
        frac_nz = len(nz) / len(sq)
        out(f"R5  send queue inside plateaus: {len(nz):,}/{len(sq):,} samples non-zero "
            f"({frac_nz:.2%}), max {max(sq):,} B (tolerance {args.r5_frac:.1%})")
        # A tolerance rather than "any non-zero": a momentarily non-empty tx_queue is normal even
        # on an idle socket. Sustained backpressure is the claim, and that shows up as a fraction.
        # Note also that UDP tx_queue is very often structurally zero, so R5 NOT firing is weak
        # evidence -- it is here because a non-zero reading would be decisive, not because a zero
        # one proves much.
        if frac_nz > args.r5_frac:
            fired.append(f"R5: the socket send queue was non-empty in {frac_nz:.1%} of plateau "
                         f"samples (max {max(sq):,} B). The kernel is holding bytes back, so the "
                         f"application is not the binding constraint.")

    # ---- R6: does the server hurt while it is NOT at the ceiling? ------------------------------
    def frac_of_ceiling(r):
        return r["_tx"] / predicted_ceiling(r["n"], args.budget, pay, args.hdr, args.k)

    seen_rtt = [r for r in rows if r["n"] > 0 and ("rtt" in r or "rtt_to" in r)]
    censored = [r for r in seen_rtt if "rtt_to" in r]
    ok_rtt = [r for r in seen_rtt if isinstance(r.get("rtt"), (int, float))]
    if len(seen_rtt) < args.r6_min:
        out(f"R6  NOT TESTABLE: fewer than {args.r6_min} A2S round trips recorded")
        caveats.append("R6 -- too few A2S round trips recorded")
    elif len(censored) > args.r6_max_censored * len(seen_rtt):
        out(f"R6  NOT TESTABLE: {len(censored)}/{len(seen_rtt)} A2S probes timed out "
            f"({len(censored)/len(seen_rtt):.0%}, limit {args.r6_max_censored:.0%}) -- the latency "
            f"series is censored above the timeout bound and its median is meaningless")
        caveats.append(f"R6 -- {len(censored)/len(seen_rtt):.0%} of A2S probes timed out, so the "
                       f"latency distribution is censored and no threshold derived from its "
                       f"median would mean anything")
    else:
        med = statistics.median([r["rtt"] for r in ok_rtt]) if ok_rtt else 0.0
        # max(), not med*3: a median that rounds to 0.0 would collapse the threshold to zero and
        # make literally every sample a "spike".
        thresh = args.rtt_spike if args.rtt_spike else max(med * 3, med + 5.0, 2.0)
        # A timeout IS a spike -- it is a round trip longer than the bound. Counting it as one is
        # the whole reason the probe records rtt_to instead of dropping the sample.
        spikes = [r for r in ok_rtt if r["rtt"] > thresh] + censored
        low = [r for r in spikes if frac_of_ceiling(r) < args.low_frac]
        out(f"R6  A2S rtt median {med:.1f} ms, spikes >{thresh:.1f} ms (timeouts counted as "
            f"spikes): {len(spikes)} ({len(low)} while egress was below {args.low_frac:.0%} of "
            f"the ceiling)")
        if low and len(low) >= max(3, 0.5 * len(spikes)):
            fired.append(f"R6: {len(low)} of {len(spikes)} A2S latency spikes happened while "
                         f"egress was below {args.low_frac:.0%} of the ceiling. Something other "
                         f"than the send budget is hurting the server.")

    # ---- the matched-window test ---------------------------------------------------------------
    out("")
    out(f"--- matched windows: raid vs non-raid, same player count AND same inbound rate "
        f"({args.raid_window:g}s after onset) ---")
    raids = [e["t"] for e in events if e.get("kind") == "raid"]
    matched_ok, matched_tested = [], []
    if not raids:
        out("  no raid markers in events.jsonl for this window -- this test could not run.")
        blocked.append("the matched raid/non-raid comparison (no raids recorded)")
    else:
        def in_raid(t):
            return any(rt <= t < rt + args.raid_window for rt in raids)
        out(f"  {len(raids)} raid(s) in range")
        # MATCHED ON INBOUND, NOT ON EGRESS. Selecting the control set by egress would guarantee
        # the answer: it picks non-raid seconds that already have the same egress as the raid, so
        # the two sides are equal by construction whether or not anything is supply-limited.
        # Inbound is client-to-server traffic, which no server-side send budget constrains, so it
        # is an independent proxy for how much is going on.
        #
        # THAT CLAIM IS NOW ACTUALLY TESTED, WHICH IT WAS NOT WHEN IT WAS FIRST MADE. The original
        # commit asserted that the synthetic fixture had caught the circular version. It had not:
        # it only caught a half-swapped variant that died on a units mismatch (inbound is ~2 KB/s,
        # egress ~200 KB/s), which is a scale artifact and not the property at issue. The fully
        # self-consistent circular version -- target AND filter both on egress -- survived, because
        # in that fixture inbound and would-be egress were both exact linear functions of the same
        # per-second demand, so the two selection rules were indistinguishable by construction.
        # The `circular_trap` world in synth() exists to fix exactly that: demand and egress
        # genuinely decouple there, matching on inbound gives INCONCLUSIVE (the right answer) and
        # matching on egress gives CONFIRMED (the wrong one).
        for n in sorted(pl):
            rs = [r for r in pl[n]["all"] if "rxb" in r]
            raid_rows = [r for r in rs if in_raid(r["t"])]
            if len(raid_rows) < args.min_matched:
                out(f"  n={n}: only {len(raid_rows)} raid sample(s), need {args.min_matched} -- skipped")
                continue
            target = statistics.median([r["_rx"] for r in raid_rows])
            quiet_rows = [r for r in rs if not in_raid(r["t"])
                          and target and abs(r["_rx"] - target) <= args.match_tol * target]
            if len(quiet_rows) < args.min_matched:
                out(f"  n={n}: raid inbound {target:,.0f} B/s, but only {len(quiet_rows)} non-raid "
                    f"sample(s) within {args.match_tol:.0%} of it -- no comparable control, skipped")
                continue
            a, b = [r["_tx"] for r in raid_rows], [r["_tx"] for r in quiet_rows]
            ap, bp = [r["_pk"] for r in raid_rows], [r["_pk"] for r in quiet_rows]
            pb_ = perm_test(a, b, args.perms, seed=args.seed)
            pp_ = perm_test(ap, bp, args.perms, seed=args.seed)
            matched_tested.append(n)
            out(f"  n={n}: matched on inbound {target:,.0f} +/-{args.match_tol:.0%} B/s")
            out(f"       raid {human(mean(a))} ({len(a)}) vs control {human(mean(b))} ({len(b)})  "
                f"delta {(mean(a)-mean(b))/mean(b)*100:+.1f}%  p(bytes)={pb_:.3f}  p(packets)={pp_:.3f}")
            if pb_ == pb_ and pb_ > args.alpha:
                matched_ok.append(n)
                out(f"       -> indistinguishable at n={n}: demand rose, supply did not.")
            else:
                out(f"       -> raids DO move egress at n={n}. Whatever the plateau is, it is not "
                    f"a hard ceiling here.")
        if not matched_tested:
            blocked.append("the matched raid/non-raid comparison (no player count had enough of both)")

    # ---- R3 + the human signal -----------------------------------------------------------------
    out("")
    out("--- player lag reports ---")
    reports = [e for e in events if e.get("kind") == "lagreport"]
    if not reports:
        out("  none. Without them this report can show that a ceiling exists, but not that it is")
        out("  what anyone is feeling -- the question that actually matters. Ask players to use !lag.")
        caveats.append("R3 -- no !lag reports, so nothing says whether players felt any of this")
    else:
        by_t = {r["t"]: r for r in rows}
        times = sorted(by_t)
        pings = {s["t"]: s["pg"] for s in samples if isinstance(s.get("pg"), dict)}
        base_pg = [v for d in pings.values() for v in d.values() if isinstance(v, (int, float))]
        base_med = statistics.median(base_pg) if base_pg else float("nan")
        lows, usable = 0, 0
        for e in reports:
            near = [by_t[t] for t in times if abs(t - e["t"]) <= args.report_window]
            when = datetime.fromtimestamp(e["t"])
            if not near:
                out(f"  {when:%Y-%m-%d %H:%M} {e.get('name','?')}: no egress samples within "
                    f"{args.report_window:g}s (server empty, or the probe was not running)")
                continue
            usable += 1
            lvl = statistics.median([r["_tx"] for r in near])
            n = statistics.median([r["n"] for r in near])
            f = lvl / predicted_ceiling(n, args.budget, pay, args.hdr, args.k) if n else float("nan")
            npg = [v for t, d in pings.items() if abs(t - e["t"]) <= args.report_window
                   for v in d.values() if isinstance(v, (int, float))]
            pg_txt = (f"peer ping {statistics.median(npg):.0f} ms vs {base_med:.0f} ms baseline"
                      if npg else "no peer ping recorded")
            if f < args.low_frac:
                lows += 1
            out(f"  {when:%Y-%m-%d %H:%M} {e.get('name','?')}: egress {human(lvl)} = {f:.0%} of "
                f"ceiling at n={n:.0f}, {pg_txt}"
                + ("  <-- BELOW CEILING" if f < args.low_frac else ""))
        if usable:
            out(f"  {lows} of {usable} correlatable report(s) came while egress was below "
                f"{args.low_frac:.0%} of the ceiling")
            if lows >= max(1, 0.5 * usable):
                fired.append(f"R3: {lows} of {usable} lag reports happened while egress was below "
                             f"{args.low_frac:.0%} of the ceiling. Whatever players are feeling, "
                             f"it is not this ceiling -- raising it would not help them.")
        else:
            caveats.append("R3 -- no lag report could be matched to egress samples")

    # ---- verdict -------------------------------------------------------------------------------
    # Each gate is conditional on ITS OWN evidence. A positive verdict may only be reached when
    # every test it would then assert in prose actually ran and actually passed.
    cvs = {n: cv([r["_tx"] for r in pl[n]["plateau"]])
           for n in pl if len(pl[n]["plateau"]) >= args.min_plateau}
    flat = {n: v for n, v in cvs.items() if v == v and v <= args.cv_max}
    if not fired:
        if len(flat) < 2:
            blocked.append(
                f"a flat plateau at two or more player counts is required and "
                f"{sorted(flat) if flat else 'none'} qualified "
                + (f"(best CV {min(cvs.values()):.2%}, needs <={args.cv_max:.1%})" if cvs else
                   f"(no player count reached {args.min_plateau} plateau samples)"))
        if not matched_ok:
            blocked.append("the matched raid/non-raid comparison never came out indistinguishable, "
                           "so the plateau is not yet shown to be a supply limit rather than a "
                           "demand ceiling")

    out("")
    out("=" * 78)
    verdict = "REFUTED" if fired else ("INCONCLUSIVE" if blocked else "CONFIRMED")
    out(f"VERDICT: {verdict}")
    out(f"         on {dur(sampled)} of measured time, {len(rows):,} samples, player counts "
        f"{counts or 'none'}, {len(raids)} raid(s), {len(reports)} lag report(s)")
    out("=" * 78)
    if fired:
        out("Refuted by:")
        for f in fired:
            out("  * " + f)
        out("")
        out("The per-peer send budget is NOT the binding constraint on this data. The reason named")
        out("above is the thread to pull -- and note that a single global cap explains a plateau")
        out("that does not scale with n just as well, without any per-peer budget at all.")
    elif verdict == "CONFIRMED":
        # Only claims that were actually measured, with the measurement beside each.
        out(f"No refutation condition fired, and every test below actually ran:")
        out(f"  * plateaus at n={sorted(flat)} are flat to within {max(flat.values()):.2%} "
            f"(CV, limit {args.cv_max:.1%})")
        out(f"  * per-player plateau varies {spread:.1%} across n={sorted(per_player)} "
            f"(limit {args.r2_tol:.0%}), so it does scale with player count")
        out(f"  * raids at n={sorted(matched_ok)}, matched on inbound rate, do not move egress: "
            f"demand rose and supply did not")
        out("")
        out(f"That is consistent with a per-peer send budget near {args.budget:,.0f} B/s -- but")
        out("'consistent with' is not 'established'. A single global cap divided by the player")
        out("count can fit the same numbers whenever the player counts sampled are few or close")
        out("together, and on the operator's 30-day data the observed maxima exceed n x 61440 by")
        out("12-51%. Before planning around this: check the spread of player counts above is wide")
        out("enough to distinguish the two, confirm the constant against the running build rather")
        out("than an older one, and test whether it can be raised server-side at all.")
    else:
        out("Not enough evidence either way. Specifically:")
        for b in blocked:
            out("  * " + b)
    if caveats or (blocked and verdict == "REFUTED"):
        out("")
        out("Tests that could not run at all:")
        for b in caveats + (blocked if verdict == "REFUTED" else []):
            out("  * " + b)
    return verdict, fired, blocked + caveats


# ---------------------------------------------------------------- synthetic worlds for --selftest
def synth(kind, args, seed=7, hours=7):
    """Fake servers with known answers -- one per refutation condition, plus the ways a verdict
    can legitimately be INCONCLUSIVE. Each returns (rows, samples, events).

    `demand` is what the world is asking the server to send. It drives BOTH the inbound rate,
    which no server-side send budget constrains, and the would-be outbound rate. In most worlds
    those move together; in `circular_trap` they deliberately do not, which is what makes that
    world able to tell two selection rules apart.
    """
    budget, payload, hdr = args.budget, 1192.0, args.hdr
    rnd = random.Random(seed)
    t0 = time.time() - hours * 3600 - 60
    rows, events = [], []
    t = t0
    single = kind == "single_n"
    for block in range(hours * 6):
        n = 3 if single else [2, 3, 5][block % 3]
        raid = block % 7 == 3
        busy = raid or block % 5 != 0
        peak = block % 5 == 1
        if raid:
            events.append({"t": round(t, 1), "kind": "raid", "name": "army_goblin"})
        ceil_ = predicted_ceiling(n, budget, payload, hdr, 0.0)
        demand = 1.0 if raid else (0.85 if busy else 0.30)
        for s in range(600):
            d = demand * rnd.uniform(0.96, 1.04)
            want = ceil_ * d * 1.6          # at d>=0.63 the world wants more than the budget allows
            rxb = int(n * 900 * d)
            sq, rtt = 0, round(rnd.uniform(11.0, 13.0), 1)
            pay = payload
            if kind == "budget":
                txb = int(min(want, ceil_ * rnd.uniform(0.997, 1.0)))
            elif kind == "demand":
                txb = int(want)                                  # nothing caps it -> R1
            elif kind == "global":
                cap = predicted_ceiling(3, budget, payload, hdr, 0.0)   # one server-wide cap
                txb = int(min(want, cap * rnd.uniform(0.997, 1.0)))     # -> R2
            elif kind == "smallpkt":
                txb, pay = int(min(want, ceil_ * rnd.uniform(0.997, 1.0))), 300.0   # -> R4
            elif kind == "queued":
                txb = int(min(want, ceil_ * rnd.uniform(0.997, 1.0)))
                sq = 8192 if busy else 0                                # -> R5
            elif kind == "spiky":
                txb = int(min(want, ceil_ * rnd.uniform(0.997, 1.0)))
                if not busy and s % 25 == 0:
                    rtt = 400.0                                          # -> R6, at low egress
            elif kind == "noisy":
                txb = int(min(want, ceil_ * rnd.uniform(0.80, 1.0)))     # plateau far too loose
            elif kind == "bursty":
                # the ceiling is touched for one isolated second at a time -- a burst, not a
                # plateau. min_run must refuse to call these runs.
                txb = int(ceil_ * 0.999) if (busy and s % 4 == 0) else int(ceil_ * 0.40)
            elif kind == "raidmoves":
                # a clean plateau, but raids really do buy more bytes -- the matched test must
                # notice, and must not be satisfied by a p-value that is always 1.0
                txb = int(ceil_ * (0.95 if raid else 0.55) * rnd.uniform(0.997, 1.0))
            elif kind == "thin_raid" or kind == "artifact":
                txb = int(min(want, ceil_ * rnd.uniform(0.997, 1.0)))
            elif kind == "overshoot":
                # A GENUINE refutation: egress sits 30% above the modelled ceiling for minutes at
                # a time. Sustained, so the artifact filter cannot touch it (it raises the very
                # percentile that filter is derived from) and R1 must still fire. Without this
                # world, "stop R1 firing on one bad row" could be satisfied by breaking R1.
                txb = int(ceil_ * (1.30 if busy else 0.45) * rnd.uniform(0.99, 1.01))
            elif kind == "circular_trap":
                # Demand and egress DECOUPLE here, which is the point. Raids are entity churn
                # with little player input (low inbound, high egress); "peak" blocks are players
                # running about (high inbound, high egress); ordinary busy blocks share the
                # raid's inbound band at half the egress.
                #   matching on inbound (correct): control = busy blocks -> raids clearly move
                #     egress -> not indistinguishable -> INCONCLUSIVE.
                #   matching on egress (circular): control = peak blocks, equal by construction
                #     -> "indistinguishable" -> CONFIRMED. Wrong.
                if raid:
                    txb, rxb = int(ceil_ * 0.90 * rnd.uniform(0.997, 1.0)), int(n * 400)
                elif peak:
                    txb, rxb = int(ceil_ * 0.90 * rnd.uniform(0.997, 1.0)), int(n * 1600)
                elif busy:
                    txb, rxb = int(ceil_ * 0.45 * rnd.uniform(0.99, 1.0)), int(n * 400)
                else:
                    txb, rxb = int(ceil_ * 0.30 * rnd.uniform(0.99, 1.0)), int(n * 1000)
            else:
                txb = int(min(want, ceil_ * rnd.uniform(0.997, 1.0)))
            txp = max(1, round(txb / (pay + hdr)))
            rec = {"t": round(t, 1), "dt": 1.0, "txb": txb, "txp": txp, "rxb": rxb,
                   "rxp": max(1, round(rxb / (120 + hdr))), "sz": round(txb / txp, 1),
                   "n": n, "na": 1, "np": n, "sq": sq}
            if s % 5 == 0:
                rec["rtt"] = rtt
            rows.append(rec)
            t += 1
        if kind == "thin_raid" and raid:
            # only ten seconds of raid: too thin to compare, and min_matched must say so
            keep = [r for r in rows if not (r["t"] >= events[-1]["t"] + 10
                                            and r["t"] < events[-1]["t"] + args.raid_window)]
            rows = keep

    if kind == "laggy":
        # reports land in the quiet stretches, while egress is nowhere near the ceiling -> R3
        quiet = [r for r in rows
                 if r["txb"] < predicted_ceiling(r["n"], budget, payload, hdr, 0.0) * 0.5]
        for r in quiet[::max(1, len(quiet) // 6)][:6]:
            events.append({"t": r["t"], "kind": "lagreport", "name": "Bjorn"})
    elif kind != "empty":
        # A report is only a useful control if the SURROUNDING minute is at the ceiling -- that is
        # the window the report actually averages over. Placing one on a single high second in a
        # world of isolated bursts fired R3 for reasons that had nothing to do with R3.
        for i in range(120, len(rows) - 120, 60):
            win = rows[i - 60:i + 60]
            ceil_ = predicted_ceiling(win[0]["n"], budget, payload, hdr, 0.0)
            if all(r["n"] == win[0]["n"] for r in win) and                     statistics.median([r["txb"] for r in win]) > ceil_ * 0.85:
                events.append({"t": rows[i]["t"], "kind": "lagreport", "name": "Bjorn"})
                break
    if kind == "artifact":
        # One poisoned row in ten hours of clean multi-count data: a 60 s stall recorded as one
        # second. This is the exact shape that used to print REFUTED and abandon the hypothesis.
        # Note it is well UNDER a gigabit link -- only the distribution-relative bound catches it.
        rows[len(rows) // 2]["txb"] = 15_000_000
        rows[len(rows) // 2]["txp"] = 12000
    if kind == "stale":
        shift = 40 * 86400
        for r in rows:
            r["t"] -= shift
        for e in events:
            e["t"] -= shift
    if kind == "empty":
        rows, events = [], []
    samples = [{"t": int(t0 + 60 * i), "p": 3, "pg": {"Bjorn": 45, "Astrid": 47}}
               for i in range(hours * 60)]
    return normalize(rows), samples, events


# (world, expected verdict, refutation condition that must have fired, text the report must print)
WORLDS = [
    ("budget",        "CONFIRMED",    None, None),
    ("demand",        "REFUTED",      "R1", None),
    ("overshoot",     "REFUTED",      "R1", None),
    ("global",        "REFUTED",      "R2", None),
    ("laggy",         "REFUTED",      "R3", None),
    ("smallpkt",      "REFUTED",      "R4", None),
    ("queued",        "REFUTED",      "R5", None),
    ("spiky",         "REFUTED",      "R6", None),
    # One impossible row must NOT overturn ten hours of clean data -- and must be reported, not
    # quietly swallowed. Both halves are asserted: the verdict, and the line that says so.
    ("artifact",      "CONFIRMED",    None, "discarded        1 sample(s) as physically implausible"),
    ("single_n",      "INCONCLUSIVE", None, "one point has no slope"),
    ("empty",         "INCONCLUSIVE", None, None),
    ("noisy",         "INCONCLUSIVE", None, None),
    ("bursty",        "INCONCLUSIVE", None, None),
    ("stale",         "INCONCLUSIVE", None, None),
    ("raidmoves",     "INCONCLUSIVE", None, None),
    ("thin_raid",     "INCONCLUSIVE", None, None),
    ("circular_trap", "INCONCLUSIVE", None, None),
]


def selftest(verbose=False):
    """Hermetic: it builds its own argument namespace from the parser defaults rather than using
    whatever the operator typed, so `--selftest --budget 30000` cannot fail for reasons unrelated
    to any defect."""
    args = build_parser().parse_args([])
    ok = True
    for kind, want, want_rule, must_say in WORLDS:
        lines = []
        rows, samples, events = synth(kind, args)
        verdict, fired, blocked = analyse(rows, samples, events, args, out=lines.append)
        rules = [f.split(":")[0] for f in fired]
        said = must_say is None or any(must_say in l for l in lines)
        good = verdict == want and (want_rule is None or want_rule in rules) and said
        print(("  ok   " if good else "  FAIL ") + f"{kind:<14} -> {verdict}"
              + (f" [{', '.join(rules)}]" if rules else "")
              + (f"   (expected {want}" + (f" via {want_rule}" if want_rule else "")
                 + ("" if said else f"; missing output {must_say!r}") + ")"
                 if not good else ""))
        if not good or verbose:
            ok = ok and good
            print("\n".join("      " + l for l in lines))
    print("")
    print("selftest passed" if ok else "selftest FAILED")
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI
def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--dir", default=LIB, help="directory holding egress-*.jsonl / samples.jsonl / events.jsonl")
    ap.add_argument("--days", type=float, default=0, help="only consider the last N days (0 = everything)")
    ap.add_argument("--selftest", action="store_true", help="run the synthetic worlds with known answers")
    ap.add_argument("--verbose", action="store_true", help="with --selftest, print every report in full")
    # the model
    ap.add_argument("--budget", type=float, default=BUDGET, help="per-peer send budget, B/s")
    ap.add_argument("--payload", type=float, default=0, help="payload bytes/packet (0 = measure it from the data)")
    ap.add_argument("--hdr", type=float, default=HDR, help="header bytes/packet (28 = IP+UDP, as nftables counts; 42 adds Ethernet)")
    ap.add_argument("--k", type=float, default=0.0, help="non-ZDO per-peer traffic, B/s (0 keeps the ceiling a lower bound)")
    # how much evidence is enough
    ap.add_argument("--min-samples", type=float, default=6 * 3600, help="seconds of measured time before any verdict is offered")
    ap.add_argument("--max-stale", type=float, default=48, help="hours: refuse a verdict if the newest sample is older")
    ap.add_argument("--max-skip", type=float, default=0.02, help="fraction of unreadable lines that blocks a positive verdict")
    # plateau definition
    ap.add_argument("--plateau-frac", type=float, default=0.95, help="fraction of the p99.5 edge that counts as plateau")
    ap.add_argument("--min-run", type=int, default=3, help="consecutive samples needed to call it a plateau, not a burst")
    ap.add_argument("--min-plateau", type=int, default=1800, help="plateau samples needed before a player count is used")
    ap.add_argument("--cv-max", type=float, default=0.02, help="CV a plateau must be under to count as flat")
    # refutation thresholds
    ap.add_argument("--r1-slack", type=float, default=0.05, help="R1: fraction above the ceiling that still counts as noise")
    ap.add_argument("--r1-min", type=int, default=3, help="R1: over-ceiling samples anywhere that fire it (1 lets a single instrument artifact end the investigation)")
    ap.add_argument("--r1-run", type=int, default=2, help="R1: CONSECUTIVE over-ceiling samples that fire it -- sustained overshoot is a measurement, a lone spike is not")
    ap.add_argument("--link-mbit", type=float, default=1000, help="sanity bound on physically possible egress, megabits/s. Not a NIC spec: the observed maximum is ~2.2 Mbit/s, so this is ~450x headroom")
    ap.add_argument("--artifact-mult", type=float, default=10, help="a sample above this many times the 99.9th percentile for its player count is instrument error, not measurement")
    ap.add_argument("--max-artifact", type=float, default=0.01, help="artifact fraction above which the instrument, not the hypothesis, is the finding")
    ap.add_argument("--r2-tol", type=float, default=0.20, help="R2: allowed spread in per-player plateau across n")
    ap.add_argument("--r4-min", type=float, default=600, help="R4: minimum plausible mean payload, bytes")
    ap.add_argument("--r5-frac", type=float, default=0.01, help="R5: fraction of plateau samples with a non-empty send queue")
    ap.add_argument("--r6-min", type=int, default=30, help="R6: A2S samples needed before it is testable")
    ap.add_argument("--r6-max-censored", type=float, default=0.20, help="R6: timeout fraction above which the latency series is too censored to use")
    ap.add_argument("--rtt-spike", type=float, default=0, help="R6: ms that counts as a spike (0 = max(3x median, median+5ms, 2ms))")
    ap.add_argument("--low-frac", type=float, default=0.70, help="R3/R6: 'below the ceiling' threshold")
    # windows and tests
    ap.add_argument("--raid-window", type=float, default=300, help="seconds after a raid marker treated as a raid window")
    ap.add_argument("--report-window", type=float, default=60, help="seconds around a !lag report to average over")
    ap.add_argument("--min-matched", type=int, default=60, help="samples needed on each side of a matched comparison")
    ap.add_argument("--match-tol", type=float, default=0.25, help="how close a non-raid sample's INBOUND rate must be to the raid median to serve as its control")
    ap.add_argument("--alpha", type=float, default=0.05, help="p above which raid and control count as indistinguishable")
    ap.add_argument("--perms", type=int, default=2000, help="permutations in the matched-window test")
    ap.add_argument("--seed", type=int, default=1, help="seed, so the same data gives the same p-values")
    return ap


def main():
    a = build_parser().parse_args()
    if a.selftest:
        return selftest(verbose=a.verbose)
    since = time.time() - a.days * 86400 if a.days else None
    rows, skipped = load_egress(a.dir, since)
    samples, s_skip = load_jsonl(os.path.join(a.dir, "samples.jsonl"), since)
    events, e_skip = load_jsonl(os.path.join(a.dir, "events.jsonl"), since)
    if s_skip or e_skip:
        print(f"  ! skipped {s_skip} unreadable line(s) in samples.jsonl and {e_skip} in "
              f"events.jsonl", file=sys.stderr)
    analyse(rows, samples, events, a, skipped=skipped)
    # A refutation is a successful run of this program, not an error: exit 0 either way, so that
    # nobody wires this into a script that treats "the hypothesis was wrong" as a crash.
    return 0


if __name__ == "__main__":
    sys.exit(main())
