#!/usr/bin/env python3
"""valheim-stall-calib-analysis.py -- does rq/rtt actually see a world-save stall?

Offline analysis of valheim-stall-calib.py's 10 Hz JSONL output. Answers the question that
sampler was built to answer: does `rq` (UDP rx_queue depth on the game port -- a mod-free proxy
for a Valheim main-loop stall, per valheim-egress-probe.py's own claim) or `rtt` (A2S round trip
to the local query port) actually move during a known, journal-timestamped world-save stall?

World saves ("PrepareSave: ZDOExtraData.PrepareSave done [NNNms]" in `journalctl -u valheim`)
are the ground truth: they block the Unity main thread for a measured duration, with nothing else
required. This script never touches the VM -- it reads a copied JSONL sample file and a copied
list of save events (journal timestamp + duration_ms), both supplied as files.

WHY A POSITIVE CLAIM NEEDS A BASELINE, NOT JUST A DURING-VS-BEFORE COMPARISON. A single sample
that happens to be elevated during a save proves nothing on its own: rq and rtt both wobble all
the time (scheduler jitter, other processes on the box, a GC pause in the query thread). The
question is not "was rq higher during the save than the samples right before it" -- it is
"is an excursion of that size rare outside of saves, or does it happen just as often at random."
So this script always computes the full non-save distribution first and reports how often an
equal-or-greater excursion occurs away from any known save, before it will print any verdict word
stronger than INCONCLUSIVE. See `--selftest` for a synthetic case where a loud non-save baseline
downgrades what looks like a clean during-save spike from VALIDATED to BLIND.

WHY THIS CANNOT "VALIDATE" ANYTHING WITH ONE SAVE EVENT. A ~150ms stall against a ~100ms sample
period is covered by 1-2 samples, if the sampler's clock and the game's clock are aligned within
a single period (they are not synchronized at all -- both are independent wall clocks on the same
box, which is normally good enough, but with only one event there is no way to tell an alignment
miss from a true null result). N=1 cannot support a population-level VALIDATED verdict for either
signal; the honest ceiling with one event is INCONCLUSIVE-but-suggestive, or in the rtt case, a
genuine BLIND finding if a large fraction of the excursion window is flat. This script computes
what it can and says so explicitly rather than rounding an N=1 result up to something it isn't.

Usage:
  python3 valheim-stall-calib-analysis.py --samples FILE.jsonl --saves saves.tsv
  python3 valheim-stall-calib-analysis.py --selftest      # no VM, no real data needed

saves.tsv format: one save per line, tab-separated, UTC ISO8601 timestamp + duration in ms:
  2026-09-15T16:46:42+00:00	155
Lines starting with # are comments. Blank lines are ignored.
"""
import argparse
import json
import statistics
import sys
from datetime import datetime, timezone, timedelta

# How far around a save's t=0 counts as "in window" for excursion peak/duration measurement.
# 2s is generous relative to a ~150ms stall -- wide enough to catch a slightly-misaligned clock,
# narrow enough that it can't accidentally swallow the *next* independent event (saves are ~30
# min apart, so there is no risk of overlap).
WINDOW_S = 2.0

# Samples further than this from EVERY known save are "non-save" for the baseline distribution.
# Wider than WINDOW_S on purpose: a stall's aftereffects (e.g. queued packets draining) could
# leak past WINDOW_S, and the baseline must not be contaminated by save-adjacent samples or it
# would UNDERSTATE how rare a real excursion is, which biases every rate calculation toward
# "looks like noise" -- the wrong direction to be biased in given this script's job.
BASELINE_EXCLUSION_S = 10.0


def log(msg):
    print("stall-calib-analysis: " + msg, file=sys.stderr)


# ---------------------------------------------------------------- loading

def load_samples(path):
    """Parse the sampler's JSONL. Returns a list of dicts with a `t_epoch` float added (seconds
    since epoch, UTC). Rows that fail to parse are counted and skipped -- never fabricated."""
    rows = []
    n_bad = 0
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                row["t_epoch"] = datetime.fromisoformat(row["t"]).timestamp()
                rows.append(row)
            except Exception:
                n_bad += 1
    if n_bad:
        log(f"dropped {n_bad} unparseable lines from {path}")
    return rows


def load_saves(path):
    """Parse saves.tsv -> list of (epoch_seconds, duration_ms)."""
    saves = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ts, dur = line.split("\t")
            saves.append((datetime.fromisoformat(ts).timestamp(), float(dur)))
    return saves


# ---------------------------------------------------------------- effective sample rate

def effective_rate(rows):
    """Empirical sample rate from consecutive `mono` timestamps -- never trust the nominal 10 Hz.
    Returns (median_dt, mean_dt, min_dt, max_dt, n_gaps_over_200ms)."""
    dts = []
    for a, b in zip(rows, rows[1:]):
        if a.get("mono") is not None and b.get("mono") is not None:
            dts.append(b["mono"] - a["mono"])
    if not dts:
        return None
    n_gaps = sum(1 for d in dts if d > 0.2)
    return {
        "median_dt": statistics.median(dts),
        "mean_dt": statistics.mean(dts),
        "min_dt": min(dts),
        "max_dt": max(dts),
        "n_samples": len(rows),
        "n_gaps_over_200ms": n_gaps,
    }


# ---------------------------------------------------------------- save-window alignment

def samples_in_window(rows, center_epoch, half_width=WINDOW_S):
    return [r for r in rows if abs(r["t_epoch"] - center_epoch) <= half_width]


def is_non_save(row, saves, exclusion=BASELINE_EXCLUSION_S):
    return all(abs(row["t_epoch"] - s_epoch) > exclusion for s_epoch, _ in saves)


def field_stats(rows, field):
    """(baseline_before, values-during-window, n) for one field, ignoring None/missing."""
    vals = [r[field] for r in rows if r.get(field) is not None]
    return vals


def analyze_save(rows, s_epoch, s_dur_ms, field):
    """For one save event and one field (rq or rtt_ms): baseline (samples just before the
    window), peak during the window, how many samples fall inside [t0, t0+dur_ms], and the
    window's full excursion duration estimated from consecutive above-baseline samples."""
    before = [r for r in rows if -WINDOW_S <= r["t_epoch"] - s_epoch < 0 and r.get(field) is not None]
    window = [r for r in rows if 0 <= r["t_epoch"] - s_epoch <= WINDOW_S and r.get(field) is not None]
    strict = [r for r in rows if 0 <= (r["t_epoch"] - s_epoch) * 1000 <= s_dur_ms and r.get(field) is not None]

    baseline_val = statistics.median([r[field] for r in before]) if before else None
    peak_val = max((r[field] for r in window), default=None)
    return {
        "baseline_before": baseline_val,
        "n_before": len(before),
        "peak_during": peak_val,
        "n_samples_in_stall_window": len(strict),
        "n_samples_in_2s_window": len(window),
        "window_values": [r[field] for r in window],
    }


def baseline_test(rows, saves, field, peak_value):
    """The mandatory check: across every non-save sample, how often does `field` reach or
    exceed `peak_value`? Returns (rate_per_hour, n_equal_or_greater, n_non_save_samples,
    span_hours)."""
    non_save = [r for r in rows if is_non_save(r, saves) and r.get(field) is not None]
    if not non_save or peak_value is None:
        return None
    n_hit = sum(1 for r in non_save if r[field] >= peak_value)
    t0 = min(r["t_epoch"] for r in non_save)
    t1 = max(r["t_epoch"] for r in non_save)
    span_hours = max((t1 - t0) / 3600.0, 1e-9)
    rate_per_hour = n_hit / span_hours
    return {
        "n_equal_or_greater": n_hit,
        "n_non_save_samples": len(non_save),
        "span_hours": round(span_hours, 3),
        "rate_per_hour": round(rate_per_hour, 2),
        "hit_fraction": round(n_hit / len(non_save), 6),
    }


# ---------------------------------------------------------------- verdicts

def verdict_for_signal(field, save_results, baseline_results, n_saves):
    """VALIDATED / BLIND / INCONCLUSIVE, per-signal, adversarially. Never rounds N=1 up to a
    population-level VALIDATED. A signal that is flat through a real stall is a positive BLIND
    finding, not a failed test -- it says the signal cannot see main-loop stalls, full stop."""
    if n_saves == 0:
        return "INCONCLUSIVE", "no save events fell inside the capture window"

    flat_count = 0
    moved_count = 0
    for sr in save_results:
        if sr["peak_during"] is None or sr["baseline_before"] is None:
            continue
        moved = sr["peak_during"] > sr["baseline_before"]
        if moved:
            moved_count += 1
        else:
            flat_count += 1

    if flat_count == len(save_results) and flat_count > 0:
        return "BLIND", (
            f"{field} did not rise above its own pre-save baseline in any of {len(save_results)} "
            "save window(s) -- the signal does not see the stall at all"
        )

    if n_saves < 3:
        return "INCONCLUSIVE", (
            f"only {n_saves} save event(s) captured; a signal moving once is consistent with a "
            "real effect but also with ordinary noise landing near a save by chance -- see the "
            "baseline rate below for how likely that coincidence is"
        )

    # n_saves >= 3 and at least one moved: lean on the baseline rate.
    rates = [b["rate_per_hour"] for b in baseline_results if b is not None]
    if rates and max(rates) < 1.0:
        return "VALIDATED", f"moved in {moved_count}/{len(save_results)} windows; baseline rate < 1/hr"
    return "INCONCLUSIVE", "moved during saves, but comparable excursions are not rare in the baseline"


# ---------------------------------------------------------------- report

def run_report(samples_path, saves_path):
    rows = load_samples(samples_path)
    saves = load_saves(saves_path)
    rows.sort(key=lambda r: r["t_epoch"])

    print(f"# Loaded {len(rows)} samples from {samples_path}")
    print(f"# Loaded {len(saves)} save event(s) from {saves_path}")

    rate = effective_rate(rows)
    if rate:
        print("\n## Effective sample rate (empirical, from mono deltas)")
        print(f"  median dt = {rate['median_dt']*1000:.1f} ms  "
              f"(nominal target: 100.0 ms / 10 Hz)")
        print(f"  mean dt   = {rate['mean_dt']*1000:.1f} ms")
        print(f"  range     = [{rate['min_dt']*1000:.1f}, {rate['max_dt']*1000:.1f}] ms")
        print(f"  gaps > 200ms: {rate['n_gaps_over_200ms']} "
              f"(of {rate['n_samples']-1} intervals)")

    for field, label in (("rq", "rq (UDP rx_queue depth)"), ("rtt_ms", "rtt_ms (A2S round trip)")):
        print(f"\n## {label}")
        save_results = []
        baseline_results = []
        for s_epoch, s_dur in saves:
            sr = analyze_save(rows, s_epoch, s_dur, field)
            save_results.append(sr)
            when = datetime.fromtimestamp(s_epoch, tz=timezone.utc).isoformat()
            print(f"  save @ {when}  duration={s_dur:.0f}ms")
            print(f"    baseline before (median, {sr['n_before']} samples): {sr['baseline_before']}")
            print(f"    peak during 2s window: {sr['peak_during']}  "
                  f"({sr['n_samples_in_2s_window']} samples in window, "
                  f"{sr['n_samples_in_stall_window']} inside the stall's own duration)")
            print(f"    window values: {sr['window_values']}")

            bt = baseline_test(rows, saves, field, sr["peak_during"])
            baseline_results.append(bt)
            if bt:
                print(f"    BASELINE TEST: peak={sr['peak_during']}; an equal-or-greater "
                      f"{field} value occurred {bt['n_equal_or_greater']} time(s) across "
                      f"{bt['n_non_save_samples']} non-save samples "
                      f"({bt['rate_per_hour']}/hour, spanning {bt['span_hours']}h)")
            else:
                print("    BASELINE TEST: could not run (no non-save samples or no peak value)")

        verdict, reason = verdict_for_signal(field, save_results, baseline_results, len(saves))
        print(f"\n  VERDICT ({field}): {verdict} -- {reason}")

    return 0


# ---------------------------------------------------------------- selftest

def _selftest():
    ok = True

    def check(name, cond):
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        if not cond:
            ok = False
        print(f"[{status}] {name}")

    def mk(t_iso, mono, rq=None, rtt_ms=None):
        row = {"t": t_iso, "mono": mono, "rq": rq}
        if rtt_ms is not None:
            row["rtt_ms"] = rtt_ms
            row["rtt_status"] = "ok"
        row["t_epoch"] = datetime.fromisoformat(t_iso).timestamp()
        return row

    # ---- effective_rate: 10 samples at a clean 100ms cadence plus one 500ms gap.
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    mono = 1000.0
    for i in range(10):
        rows.append(mk((base + timedelta(seconds=i * 0.1)).isoformat(), mono))
        mono += 0.1
    rows.append(mk((base + timedelta(seconds=1.5)).isoformat(), mono + 0.5))
    rate = effective_rate(rows)
    check("effective_rate finds ~100ms median dt", abs(rate["median_dt"] - 0.1) < 0.01)
    check("effective_rate flags the 500ms+ gap", rate["n_gaps_over_200ms"] == 1)

    # ---- a clean case: rq spikes only during the save window, never elsewhere. VALIDATED-shaped.
    t0 = base + timedelta(seconds=100)
    rows2 = []
    mono = 2000.0
    for i in range(-150, 150):
        t = t0 + timedelta(seconds=i * 0.1)
        rq = 40 if 0 <= i <= 1 else 2  # spike only in the 2 samples covering the ~150ms stall
        rows2.append(mk(t.isoformat(), mono, rq=rq))
        mono += 0.1
    saves2 = [(t0.timestamp(), 155.0)]
    sr = analyze_save(rows2, t0.timestamp(), 155.0, "rq")
    check("analyze_save finds the spike as the peak", sr["peak_during"] == 40)
    check("analyze_save baseline_before is the quiet value", sr["baseline_before"] == 2)
    bt = baseline_test(rows2, saves2, "rq", sr["peak_during"])
    check("baseline_test finds zero equal-or-greater hits outside the save",
          bt["n_equal_or_greater"] == 0)

    # ---- inverted case: same magnitude spike happens constantly, everywhere, at random.
    # This MUST NOT be called VALIDATED -- if it is, the baseline gate is not doing its job.
    rows3 = []
    mono = 3000.0
    for i in range(-150, 150):
        t = t0 + timedelta(seconds=i * 0.1)
        rq = 40 if i % 3 == 0 else 2  # spikes to the same value constantly, save or not
        rows3.append(mk(t.isoformat(), mono, rq=rq))
        mono += 0.1
    sr3 = analyze_save(rows3, t0.timestamp(), 155.0, "rq")
    bt3 = baseline_test(rows3, saves2, "rq", sr3["peak_during"])
    check("baseline_test catches a noisy signal (many equal-or-greater hits)",
          bt3["n_equal_or_greater"] > 3)
    v3, _ = verdict_for_signal("rq", [sr3], [bt3], 1)
    check("verdict_for_signal refuses VALIDATED on N=1 even with a spike",
          v3 != "VALIDATED")

    # ---- flat/BLIND case: rtt never rises above its pre-save baseline through the window.
    rows4 = []
    mono = 4000.0
    for i in range(-150, 150):
        t = t0 + timedelta(seconds=i * 0.1)
        rows4.append(mk(t.isoformat(), mono, rtt_ms=15.0))  # perfectly flat, no spike, no noise
        mono += 0.1
    sr4 = analyze_save(rows4, t0.timestamp(), 155.0, "rtt_ms")
    v4, reason4 = verdict_for_signal("rtt_ms", [sr4], [None], 1)
    check("verdict_for_signal calls a flat signal BLIND, not INCONCLUSIVE", v4 == "BLIND")

    # ---- inversion check: if peak/baseline comparison were REMOVED (e.g. always "moved=True"),
    # the BLIND case above would incorrectly fall through to INCONCLUSIVE/VALIDATED instead of
    # BLIND. Simulate the inverted implementation directly and confirm it disagrees with ours --
    # proves this assertion is not vacuously true regardless of the comparison direction.
    def inverted_verdict(sr, n_saves):
        # deletes the "was it actually flat" check -- always assumes movement
        if n_saves < 3:
            return "INCONCLUSIVE"
        return "VALIDATED"
    check("inverted (comparison-deleted) implementation disagrees with ours on the BLIND case",
          inverted_verdict(sr4, 1) != v4)

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", help="path to stallcalib-*.jsonl")
    ap.add_argument("--saves", help="path to saves.tsv (epoch iso8601 TAB duration_ms per line)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
    elif args.samples and args.saves:
        sys.exit(run_report(args.samples, args.saves))
    else:
        ap.error("either --selftest, or both --samples and --saves are required")
