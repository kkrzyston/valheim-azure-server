#!/usr/bin/env python3
"""valheim-egress-report.py -- offline analysis of the 1 Hz egress samples. Run by hand.

THE HYPOTHESIS UNDER TEST (H): game egress is limited by Valheim's own per-peer send budget
(ZDOMan's m_dataPerSec, historically 61440 B/s), not by the VM, the kernel or the network. If H
holds, adding players or world activity does not buy more bytes -- it buys staler updates, which
is what players describe when they say it only stutters when everyone is in one place.

This script exists to be able to say NO. A plateau in a graph is not evidence of a ceiling: a
server that is simply never asked for more than 250 KB/s also produces a flat line. So the report
leads with six refutation conditions, and prints REFUTED the moment one fires:

  R1  any clean 1-second sample above the arithmetic ceiling (+5%). A budget you exceed is not a
      budget. This is the cheapest and most decisive test in the whole file.
  R2  the plateau does not scale with player count. A per-PEER budget must be per-peer; a level
      that is the same for two players and for five is some other limit wearing its clothes.
  R3  players report lag while egress is well below the ceiling. Then the ceiling, real or not,
      is not what they are feeling, and fixing it fixes nothing.
  R4  mean packet size inside plateaus is small. A saturated BYTE budget fills packets; small
      packets at a fixed byte rate mean the limit is packets, syscalls or a tick, not bytes.
  R5  the socket send queue is non-empty. Then the kernel is holding bytes back and the
      application is not the binding constraint.
  R6  A2S round trips spike while egress is below the ceiling. Then the server is struggling for
      some reason that has nothing to do with how much it is sending.

And one positive test that is worth more than all the plateau statistics: the matched-window
comparison. Compare raid windows against non-raid windows AT THE SAME PLAYER COUNT. A raid is a
large, involuntary, externally-timed increase in world activity. If egress and packet rate are
statistically indistinguishable across that, demand went up and supply did not move -- which is
what supply-limited means, and is very hard to explain any other way.

Inputs (all read-only, none modified):
  /var/lib/valheim-status/egress-YYYY-MM-DD.jsonl   1 Hz samples from valheim-egress-probe.py
  /var/lib/valheim-status/samples.jsonl             per-minute collector samples (`pg` per-peer ping)
  /var/lib/valheim-status/events.jsonl              raid markers and `!lag` reports

  python3 valheim-egress-report.py                  full report over everything on disk
  python3 valheim-egress-report.py --days 7         only the last 7 days
  python3 valheim-egress-report.py --selftest       run against two synthetic worlds -- one
                                                    genuinely budget-limited, one demand-limited
                                                    -- and check the verdict comes out right.

READ THIS BEFORE TRUSTING A "CONFIRMED". The prior is already shaky, and in an interesting way.
The 30-day maximum from the per-minute collector data is 276,425 B/s. 4.5 x 61440 = 276,480, and
276,480 B/s is exactly 270 KiB/s. The observed hard maximum is within 55 bytes -- 0.02% -- of a
round 270 KiB/s. That is not what n x 61440 looks like for the three players who were online; it
is what a single GLOBAL cap looks like. If the real limit is one server-wide 270 KiB/s budget
rather than a per-peer one, the per-peer hypothesis is wrong even though every plateau graph will
look identical. R2 is the test that separates them: a global cap produces a plateau that does NOT
scale with player count, and R2 fires. Run with several distinct player counts before concluding
anything, and treat --budget as the parameter it is rather than as a known constant -- 61440 is a
figure from an older build, and this server runs l-1.0.12.

A NOTE ON WHAT THE BYTES ARE. nftables counters at the filter hooks count what the kernel sees at
layer 3: IP header + UDP header + payload, 28 bytes of header per packet. They do NOT include the
14-byte Ethernet header. So --hdr defaults to 28, not the 42 you would use against a NIC counter.
Getting this backwards inflates the predicted ceiling by about 1% -- small, but the whole question
is whether a measured value sits above or below a computed one, so it is not a rounding detail.
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
BUDGET = 61440          # ZDOMan m_dataPerSec, bytes per second per peer
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
    per-second egress is emphatically not normal. Returns a p-value; large p means the two
    windows are indistinguishable, which for the matched-window test is the interesting result."""
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


# ---------------------------------------------------------------- the arithmetic ceiling
def predicted_ceiling(n, budget=BUDGET, payload=DEFAULT_PAYLOAD, hdr=HDR, k=0.0):
    """Counted bytes per second at the nftables hook, if every peer's ZDO budget were saturated:

        wire(n) = n*budget                      the ZDO payload itself
                + (n*budget / payload) * hdr    one header per packet needed to carry it
                + n*k                           per-peer traffic that is not ZDO data at all
                                                (acks, RPCs, pings, the routed-RPC chatter)

    payload (S) and k are parameters, not magic numbers: S is measured from the data when the
    data has packet counts, and k is unknown without reading the game's source, so it defaults to
    0 -- which makes the predicted ceiling a LOWER bound and R1 correspondingly harder to fire.
    That is the conservative direction: it biases against the hypothesis, not toward it."""
    payload = payload if payload and payload > 0 else DEFAULT_PAYLOAD
    return n * budget + (n * budget / payload) * hdr + n * k


# ---------------------------------------------------------------- loading
def load_egress(lib, since=None):
    rows = []
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
                        continue        # one torn line at the tail of a file being written
                    if not isinstance(r, dict) or "t" not in r or "txb" not in r or "n" not in r:
                        continue
                    if since and r["t"] < since:
                        continue
                    rows.append(r)
        except OSError as e:
            print(f"  ! could not read {path}: {e}", file=sys.stderr)
    rows.sort(key=lambda r: r["t"])
    return rows


def load_jsonl(path, since=None):
    out = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if isinstance(r, dict) and (not since or r.get("t", 0) >= since):
                    out.append(r)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"  ! could not read {path}: {e}", file=sys.stderr)
    out.sort(key=lambda r: r.get("t", 0))
    return out


# ---------------------------------------------------------------- plateau detection
def plateaus(rows, frac=0.95, min_run=3, hi_pct=0.995):
    """Split samples by player count and mark the ones sitting at that count's own upper edge.

    A "plateau" here means: at or above `frac` of the 99.5th percentile of egress for this player
    count (the 99.5th rather than the max, so one jittery second cannot define the edge), AND
    part of a run of at least `min_run` consecutive seconds at that level. The run requirement is
    what separates a ceiling from a burst: a single high second is a spike, several in a row at
    the same level is a limit. Both are knobs, and both are printed in the report, because the
    definition of "plateau" is doing real work in the conclusion and should not be buried."""
    by_n, out = {}, {}
    for r in rows:
        by_n.setdefault(r["n"], []).append(r)
    for n, rs in by_n.items():
        if n <= 0:
            continue
        edge = pct([r["txb"] for r in rs], hi_pct)
        thresh = edge * frac
        run, marked = [], []
        for r in rs:
            if r["txb"] >= thresh:
                run.append(r)
            else:
                if len(run) >= min_run:
                    marked.extend(run)
                run = []
        if len(run) >= min_run:
            marked.extend(run)
        out[n] = {"all": rs, "edge": edge, "thresh": thresh, "plateau": marked}
    return out


def measured_payload(rows, hdr=HDR):
    """Mean payload bytes per packet, from the samples themselves."""
    tot_b = sum(r["txb"] for r in rows if r.get("txp"))
    tot_p = sum(r["txp"] for r in rows if r.get("txp"))
    if not tot_p:
        return None
    return tot_b / tot_p - hdr


# ---------------------------------------------------------------- the report
def analyse(rows, samples, events, args, out=print):
    fired = []          # refutation conditions that fired, with a one-line reason
    blocked = []        # tests that could not run at all, which is why a verdict can be INCONCLUSIVE

    if not rows:
        out("No egress samples found. Has valheim-egress.service been running while players were online?")
        return "INCONCLUSIVE", ["no data"], ["every test"]

    span = rows[-1]["t"] - rows[0]["t"]
    out("=" * 78)
    out("Valheim egress ceiling report")
    out("=" * 78)
    out(f"samples          {len(rows):,} at 1 Hz over {span/3600:.1f} h "
        f"({datetime.fromtimestamp(rows[0]['t']):%Y-%m-%d %H:%M} -> {datetime.fromtimestamp(rows[-1]['t']):%Y-%m-%d %H:%M})")
    pay = args.payload if args.payload else measured_payload(rows, args.hdr)
    if pay is None:
        pay = DEFAULT_PAYLOAD
        blocked.append("no packet counts in the data, so packet size is assumed, not measured")
    out(f"assumed budget   {args.budget:,} B/s per peer   headers {args.hdr} B/packet "
        f"(layer 3, as nftables counts)   k {args.k:g} B/s per peer")
    out(f"payload/packet   {pay:,.0f} B  ({'measured from the samples' if not args.payload else 'given on the command line'})")
    out("")

    pl = plateaus(rows, frac=args.plateau_frac, min_run=args.min_run)
    out(f"--- per-player-count distribution (plateau = >={args.plateau_frac:.0%} of the 99.5th "
        f"percentile, in runs of >={args.min_run}s) ---")
    out(f"{'n':>3} {'samples':>8} {'plateau':>8} {'median':>12} {'p99.5':>12} {'plateau mean':>13} "
        f"{'CV':>7} {'per player':>11} {'pkt size':>9} {'predicted':>12}")
    per_player = {}
    for n in sorted(pl):
        d = pl[n]
        p = d["plateau"]
        allb = [r["txb"] for r in d["all"]]
        pb = [r["txb"] for r in p]
        pmean = mean(pb) if pb else float("nan")
        psz = measured_payload(p, args.hdr) if p else float("nan")
        predicted = predicted_ceiling(n, args.budget, pay, args.hdr, args.k)
        if pb and len(pb) >= args.min_plateau:
            per_player[n] = pmean / n
        out(f"{n:>3} {len(d['all']):>8,} {len(p):>8,} {human(statistics.median(allb)):>12} "
            f"{human(d['edge']):>12} {human(pmean):>13} {cv(pb)*100 if pb else float('nan'):>6.2f}% "
            f"{human(pmean/n) if pb else 'n/a':>11} {psz:>8.0f}B {human(predicted):>12}")
    out("")

    # ---- R1: anything above the ceiling at all -------------------------------------------------
    lim = 1.0 + args.r1_slack
    over = [r for r in rows if r["n"] > 0
            and r["txb"] > predicted_ceiling(r["n"], args.budget, pay, args.hdr, args.k) * lim]
    worst = max((r["txb"] / predicted_ceiling(r["n"], args.budget, pay, args.hdr, args.k) for r in rows if r["n"] > 0),
                default=float("nan"))
    out(f"R1  samples above the ceiling x{lim:.2f}: {len(over):,} of {len(rows):,}   "
        f"(highest observed second reached {worst*100:.1f}% of its predicted ceiling)")
    if len(over) >= args.r1_min:
        ex = over[0]
        fired.append(f"R1: {len(over)} sample(s) exceeded the ceiling, e.g. {ex['txb']:,} B/s at n={ex['n']} "
                     f"({datetime.fromtimestamp(ex['t']):%Y-%m-%d %H:%M:%S}). A budget you exceed is not a budget.")

    # ---- R2: does the plateau scale with n? ----------------------------------------------------
    if len(per_player) >= 2:
        lo, hi = min(per_player.values()), max(per_player.values())
        spread = (hi - lo) / hi
        out(f"R2  plateau per player across n={sorted(per_player)}: {human(lo)} .. {human(hi)}  "
            f"(spread {spread:.1%}, tolerance {args.r2_tol:.0%})")
        if spread > args.r2_tol:
            fired.append(f"R2: the plateau does not scale with player count -- per-player egress varies "
                         f"{spread:.0%} across n={sorted(per_player)}. A per-peer budget cannot do that.")
    else:
        out(f"R2  not testable: need >={args.min_plateau} plateau samples at two or more player counts, "
            f"have {sorted(per_player) or 'none'}")
        blocked.append("R2 (only one player count has enough plateau data)")

    # ---- R4: packet size inside plateaus -------------------------------------------------------
    allp = [r for n in pl for r in pl[n]["plateau"]]
    psz_all = measured_payload(allp, args.hdr) if allp else None
    if psz_all is None:
        out("R4  not testable: no packet counts inside plateaus")
        blocked.append("R4 (no packet counts)")
    else:
        out(f"R4  mean payload inside plateaus: {psz_all:.0f} B (floor {args.r4_min} B)")
        if psz_all < args.r4_min:
            fired.append(f"R4: mean payload inside plateaus is {psz_all:.0f} B, well short of an MTU. "
                         f"A saturated byte budget fills packets; this looks packet- or tick-limited.")

    # ---- R5: is the kernel holding bytes? ------------------------------------------------------
    sq = [r["sq"] for r in allp if isinstance(r.get("sq"), int)]
    if not sq:
        out("R5  not testable: no socket send-queue readings inside plateaus")
        blocked.append("R5 (no sq readings)")
    else:
        nz = [v for v in sq if v > 0]
        frac_nz = len(nz) / len(sq)
        out(f"R5  send queue inside plateaus: {len(nz):,}/{len(sq):,} samples non-zero ({frac_nz:.2%}), "
            f"max {max(sq):,} B (tolerance {args.r5_frac:.1%})")
        # A tolerance rather than "any non-zero": a momentarily non-empty tx_queue is normal even on
        # an idle socket. Sustained backpressure is the claim being tested, and that shows up as a
        # fraction, not a single sample.
        if frac_nz > args.r5_frac:
            fired.append(f"R5: the socket send queue was non-empty in {frac_nz:.1%} of plateau samples "
                         f"(max {max(sq):,} B). The kernel is holding bytes back, so the application is "
                         f"not the binding constraint.")

    # ---- R6: does the server hurt while it is NOT at the ceiling? ------------------------------
    rtts = [(r["t"], r["rtt"], r["txb"] / predicted_ceiling(r["n"], args.budget, pay, args.hdr, args.k))
            for r in rows if isinstance(r.get("rtt"), (int, float)) and r["n"] > 0]
    if len(rtts) < 30:
        out("R6  not testable: fewer than 30 A2S round trips recorded")
        blocked.append("R6 (too few A2S samples)")
    else:
        med = statistics.median([v for _, v, _ in rtts])
        thresh = args.rtt_spike if args.rtt_spike else med * 3
        spikes = [(t, v, f) for t, v, f in rtts if v > thresh]
        low = [s for s in spikes if s[2] < args.low_frac]
        out(f"R6  A2S rtt median {med:.1f} ms, spikes >{thresh:.1f} ms: {len(spikes)} "
            f"({len(low)} of them while egress was below {args.low_frac:.0%} of the ceiling)")
        if low and len(low) >= max(3, 0.5 * len(spikes)):
            fired.append(f"R6: {len(low)} of {len(spikes)} A2S latency spikes happened while egress was "
                         f"below {args.low_frac:.0%} of the ceiling. Something else is hurting the server.")

    # ---- the matched-window test: raid vs non-raid, same n -------------------------------------
    out("")
    out(f"--- matched windows: raid vs non-raid at the same player count ({args.raid_window}s after onset) ---")
    raids = [e["t"] for e in events if e.get("kind") == "raid" and isinstance(e.get("t"), (int, float))]
    matched_ok, matched_any = [], False
    if not raids:
        out("  no raid markers in events.jsonl for this window -- this test could not run.")
        blocked.append("matched-window raid comparison (no raids recorded)")
    else:
        def in_raid(t):
            return any(rt <= t < rt + args.raid_window for rt in raids)
        out(f"  {len(raids)} raid(s) in range")
        # MATCHED ON INBOUND, NOT JUST ON n. Comparing raid seconds against every other second at
        # the same player count is not a matched comparison -- it is a raid against an empty
        # forest, and those differ even under a perfectly saturated budget, because nobody was
        # asking for anything during the lull. The comparison set has to be seconds of comparable
        # DEMAND, and demand has to be measured by something that is not the quantity under test.
        # rxb is exactly that: client-to-server traffic is not subject to the server's send
        # budget, so it is an independent proxy for how much is going on. Selecting the control
        # set by egress instead would be circular and would guarantee the answer we are hoping for.
        for n in sorted(pl):
            rs = [r for r in pl[n]["all"] if "rxb" in r]
            raid_rows = [r for r in rs if in_raid(r["t"])]
            if len(raid_rows) < args.min_matched:
                out(f"  n={n}: only {len(raid_rows)}s of raid -- skipped")
                continue
            target = statistics.median([r["rxb"] for r in raid_rows])
            quiet_rows = [r for r in rs if not in_raid(r["t"])
                          and target and abs(r["rxb"] - target) <= args.match_tol * target]
            a, ap = [r["txb"] for r in raid_rows], [r["txp"] for r in raid_rows if "txp" in r]
            b, bp = [r["txb"] for r in quiet_rows], [r["txp"] for r in quiet_rows if "txp" in r]
            if len(b) < args.min_matched:
                out(f"  n={n}: raid inbound {target:,.0f} B/s, but only {len(b)}s of non-raid time "
                    f"within {args.match_tol:.0%} of it -- no comparable control, skipped")
                continue
            out(f"  n={n}: matched on inbound {target:,.0f} +/-{args.match_tol:.0%} B/s")
            matched_any = True
            pb_ = perm_test(a, b, args.perms, seed=args.seed)
            pp_ = perm_test(ap, bp, args.perms, seed=args.seed) if ap and bp else float("nan")
            out(f"  n={n}: raid {human(mean(a))} ({len(a)}s) vs quiet {human(mean(b))} ({len(b)}s)  "
                f"delta {(mean(a)-mean(b))/mean(b)*100:+.1f}%  p(bytes)={pb_:.3f}  p(packets)={pp_:.3f}")
            if pb_ == pb_ and pb_ > args.alpha:
                matched_ok.append(n)
                out(f"       -> indistinguishable at n={n}: demand rose, supply did not. That is the "
                    f"signature of a supply-limited sender.")
            else:
                out(f"       -> raids DO move egress at n={n}. Whatever the plateau is, it is not a hard ceiling here.")
        if not matched_any:
            blocked.append("matched-window raid comparison (no player count had enough of both)")

    # ---- R3 + the human signal -----------------------------------------------------------------
    out("")
    out("--- player lag reports ---")
    reports = [e for e in events if e.get("kind") == "lagreport" and isinstance(e.get("t"), (int, float))]
    if not reports:
        out("  none. Without them this report can show that a ceiling exists, but not that it is what")
        out("  anyone is feeling -- which is the question that actually matters. Ask players to use !lag.")
        blocked.append("R3 (no !lag reports)")
    else:
        by_t = {r["t"]: r for r in rows}
        times = sorted(by_t)
        pings = {s["t"]: s["pg"] for s in samples if isinstance(s.get("pg"), dict)}
        base_pg = [v for d in pings.values() for v in d.values() if isinstance(v, (int, float))]
        base_med = statistics.median(base_pg) if base_pg else float("nan")
        lows = 0
        for e in reports:
            near = [by_t[t] for t in times if abs(t - e["t"]) <= args.report_window]
            if not near:
                out(f"  {datetime.fromtimestamp(e['t']):%Y-%m-%d %H:%M} {e.get('name','?')}: "
                    f"no egress samples within {args.report_window}s (server empty, or probe not running)")
                continue
            lvl = statistics.median([r["txb"] for r in near])
            n = statistics.median([r["n"] for r in near])
            f = lvl / predicted_ceiling(n, args.budget, pay, args.hdr, args.k) if n else float("nan")
            npg = [v for t, d in pings.items() if abs(t - e["t"]) <= args.report_window
                   for v in d.values() if isinstance(v, (int, float))]
            pg_txt = (f"peer ping {statistics.median(npg):.0f} ms vs {base_med:.0f} ms baseline"
                      if npg else "no peer ping recorded")
            flag = "  <-- BELOW CEILING" if f < args.low_frac else ""
            if f < args.low_frac:
                lows += 1
            out(f"  {datetime.fromtimestamp(e['t']):%Y-%m-%d %H:%M} {e.get('name','?')}: "
                f"egress {human(lvl)} = {f:.0%} of ceiling at n={n:.0f}, {pg_txt}{flag}")
        usable = [e for e in reports if any(abs(t - e["t"]) <= args.report_window for t in times)]
        if usable:
            out(f"  {lows} of {len(usable)} correlatable report(s) came while egress was below "
                f"{args.low_frac:.0%} of the ceiling")
            if lows >= max(1, 0.5 * len(usable)):
                fired.append(f"R3: {lows} of {len(usable)} lag reports happened while egress was below "
                             f"{args.low_frac:.0%} of the ceiling. Whatever players are feeling, it is not "
                             f"this ceiling -- raising it would not help them.")
        else:
            blocked.append("R3 (no lag report could be matched to egress samples)")

    # ---- verdict -------------------------------------------------------------------------------
    plateau_total = sum(len(pl[n]["plateau"]) for n in pl)
    cvs = {n: cv([r["txb"] for r in pl[n]["plateau"]]) for n in pl if len(pl[n]["plateau"]) >= args.min_plateau}
    flat = {n: v for n, v in cvs.items() if v <= args.cv_max}
    out("")
    out("=" * 78)
    if fired:
        verdict = "REFUTED"
    elif plateau_total < args.min_plateau:
        verdict = "INCONCLUSIVE"
        blocked.append(f"only {plateau_total} plateau samples (need {args.min_plateau}); "
                       f"keep the probe running through more busy evenings")
    elif not flat:
        verdict = "INCONCLUSIVE"
        blocked.append(f"no player count has a plateau flatter than CV {args.cv_max:.1%} "
                       f"(best {min(cvs.values()):.2%}); a saturated budget is flatter than this")
    elif not matched_ok:
        verdict = "INCONCLUSIVE"
        blocked.append("the matched raid/non-raid comparison never came out indistinguishable, so the "
                       "plateau is not yet shown to be a supply limit rather than a demand ceiling")
    else:
        verdict = "CONFIRMED"
    out(f"VERDICT: {verdict}")
    out("=" * 78)
    if fired:
        out("Refuted by:")
        for f in fired:
            out("  * " + f)
        out("")
        out("The per-peer send budget is NOT the binding constraint. Do not spend another day on it;")
        out("the reason named above is the thread to pull.")
    elif verdict == "CONFIRMED":
        out(f"No refutation condition fired. Plateaus at n={sorted(flat)} are flat to within "
            f"{max(flat.values()):.2%}, they scale with player count, and raids at matched player")
        out(f"counts ({sorted(matched_ok)}) do not move egress. Demand rose, supply did not.")
        out("Consistent with a per-peer application send budget of about "
            f"{args.budget:,} B/s. Next: confirm against the game's own ZDOMan rate, and test whether")
        out("raising it is even possible server-side before planning around it.")
    else:
        out("Not enough evidence either way. Specifically:")
        for b in blocked:
            out("  * " + b)
    if blocked and verdict != "INCONCLUSIVE":
        out("")
        out("Caveats -- these tests could not run:")
        for b in blocked:
            out("  * " + b)
    return verdict, fired, blocked


# ---------------------------------------------------------------- synthetic worlds for --selftest
def synth(kind, seed=7, hours=3, budget=BUDGET, payload=1192, hdr=HDR):
    """Two fake servers with known answers.

    'budget'  -- a genuinely supply-limited server: busy periods pin at exactly the arithmetic
                 ceiling with only measurement jitter, raids do not move it, queues stay empty.
    'demand'  -- a server that simply is not asked for much, and bursts well past the budget when
                 it is. Same plateau-shaped graph at a glance; must come out REFUTED.
    """
    rnd = random.Random(seed)
    t0 = time.time() - hours * 3600
    rows, events = [], []
    t = t0
    for block in range(hours * 6):          # ten-minute blocks
        n = [2, 3, 5][block % 3]            # deterministic, so every n sees a raid and a control
        raid = block % 7 == 3
        busy = raid or block % 5 != 0
        if raid:
            events.append({"t": round(t, 1), "kind": "raid", "name": "army_goblin"})
        ceil_ = predicted_ceiling(n, budget, payload, hdr, 0.0)
        # `demand` is what the world is asking the server to send, independent of what it can.
        # It drives BOTH the inbound rate (which no send budget constrains) and the would-be
        # outbound rate -- which is the whole point: the two worlds below differ only in whether
        # anything stops the outbound side from following it.
        demand = 1.0 if raid else (0.85 if busy else 0.30)
        for s in range(600):
            d = demand * rnd.uniform(0.96, 1.04)
            want = ceil_ * d * 1.6          # at d>=0.63 the world wants more than the budget allows
            txb = int(min(want, ceil_ * rnd.uniform(0.997, 1.0))) if kind == "budget" else int(want)
            txp = max(1, round(txb / (payload + hdr)))
            rxb = int(n * 900 * d)
            rec = {"t": round(t, 1), "txb": txb, "txp": txp, "rxb": rxb,
                   "rxp": max(1, round(rxb / (120 + hdr))),
                   "sz": round(txb / txp, 1), "n": n, "sq": 0}
            if s % 5 == 0:
                rec["rtt"] = round(rnd.uniform(11.0, 13.0), 1)
            rows.append(rec)
            t += 1
    # one lag report in the middle of a busy stretch, so R3 must not fire on the budget world
    busy_rows = [r for r in rows if r["txb"] > predicted_ceiling(r["n"], budget, payload, hdr, 0.0) * 0.9]
    if busy_rows:
        events.append({"t": busy_rows[len(busy_rows) // 2]["t"], "kind": "lagreport", "name": "Bjorn"})
    samples = [{"t": int(t0 + 60 * i), "p": 3, "pg": {"Bjorn": 45, "Astrid": 47}} for i in range(hours * 60)]
    return rows, samples, events


def selftest(args):
    ok = True
    for kind, want in (("budget", "CONFIRMED"), ("demand", "REFUTED")):
        lines = []
        rows, samples, events = synth(kind)
        verdict, fired, blocked = analyse(rows, samples, events, args, out=lines.append)
        got = "ok" if verdict == want else "FAIL"
        print(f"  {got}   synthetic '{kind}' world -> {verdict} (expected {want})")
        if verdict != want:
            ok = False
            print("\n".join("      " + l for l in lines))
        elif fired:
            print("        fired: " + "; ".join(f.split(":")[0] for f in fired))
    print("")
    print("selftest passed" if ok else "selftest FAILED")
    return 0 if ok else 1


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0],
                                 formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--dir", default=LIB, help="directory holding egress-*.jsonl / samples.jsonl / events.jsonl")
    ap.add_argument("--days", type=float, default=0, help="only consider the last N days (0 = everything)")
    ap.add_argument("--selftest", action="store_true", help="run against two synthetic worlds with known answers")
    # the model
    ap.add_argument("--budget", type=float, default=BUDGET, help="per-peer send budget, B/s")
    ap.add_argument("--payload", type=float, default=0, help="payload bytes/packet (0 = measure it from the data)")
    ap.add_argument("--hdr", type=float, default=HDR, help="header bytes/packet (28 = IP+UDP, as nftables counts; 42 adds Ethernet)")
    ap.add_argument("--k", type=float, default=0.0, help="non-ZDO per-peer traffic, B/s (0 keeps the ceiling a lower bound)")
    # plateau definition
    ap.add_argument("--plateau-frac", type=float, default=0.95, help="fraction of the p99.5 edge that counts as plateau")
    ap.add_argument("--min-run", type=int, default=3, help="consecutive seconds needed to call it a plateau, not a burst")
    ap.add_argument("--min-plateau", type=int, default=120, help="plateau samples needed before a player count is used")
    ap.add_argument("--cv-max", type=float, default=0.02, help="CV a plateau must be under to count as flat")
    # refutation thresholds
    ap.add_argument("--r1-slack", type=float, default=0.05, help="R1: fraction above the ceiling that still counts as noise")
    ap.add_argument("--r1-min", type=int, default=1, help="R1: how many over-ceiling samples fire it")
    ap.add_argument("--r2-tol", type=float, default=0.20, help="R2: allowed spread in per-player plateau across n")
    ap.add_argument("--r4-min", type=float, default=600, help="R4: minimum plausible mean payload, bytes")
    ap.add_argument("--r5-frac", type=float, default=0.01, help="R5: fraction of plateau samples with a non-empty send queue")
    ap.add_argument("--rtt-spike", type=float, default=0, help="R6: ms that counts as a spike (0 = 3x the median)")
    ap.add_argument("--low-frac", type=float, default=0.70, help="R3/R6: 'below the ceiling' threshold")
    # windows and tests
    ap.add_argument("--raid-window", type=float, default=300, help="seconds after a raid marker treated as a raid window")
    ap.add_argument("--report-window", type=float, default=60, help="seconds around a !lag report to average over")
    ap.add_argument("--min-matched", type=int, default=60, help="seconds needed on each side of a matched comparison")
    ap.add_argument("--match-tol", type=float, default=0.25,
                    help="how close a non-raid second's INBOUND rate must be to the raid median to serve as its control")
    ap.add_argument("--alpha", type=float, default=0.05, help="p above which raid and quiet count as indistinguishable")
    ap.add_argument("--perms", type=int, default=2000, help="permutations in the matched-window test")
    ap.add_argument("--seed", type=int, default=1, help="seed, so the same data gives the same p-values")
    a = ap.parse_args()

    if a.selftest:
        return selftest(a)
    since = time.time() - a.days * 86400 if a.days else None
    rows = load_egress(a.dir, since)
    samples = load_jsonl(os.path.join(a.dir, "samples.jsonl"), since)
    events = load_jsonl(os.path.join(a.dir, "events.jsonl"), since)
    verdict, _, _ = analyse(rows, samples, events, a)
    return 0 if verdict != "REFUTED" else 0    # a refutation is a successful run, not an error


if __name__ == "__main__":
    sys.exit(main())
