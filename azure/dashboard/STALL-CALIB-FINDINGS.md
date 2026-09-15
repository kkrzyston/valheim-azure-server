# Stall calibration: does rq or rtt actually see a world-save stall?

`valheim-egress-probe.py` samples two fields it claims are mod-free proxies for a Valheim
main-loop stall: `rq` (UDP rx_queue depth on the game port, from `/proc/net/udp`) and `rtt`
(A2S round trip to the local query port). Neither claim had been checked against a known stall.
`valheim-stall-calib.py` (a one-off, 10 Hz sampler, still running as of this writeup) exists to
check them against the one stall this box produces on a predictable, journal-timestamped
schedule: a world save (`PrepareSave: ZDOExtraData.PrepareSave done [NNNms]`), which blocks the
Unity main thread for a measured duration with nothing else required to reproduce it.

This is a status report on what the capture supports **right now**, not a final verdict — see
"What would settle this" below. The capture started 2026-09-15T16:33:07Z (09:33:07 PDT) and was
still running when this analysis was done; only one save event had occurred inside the window by
then.

## Setup

- Sampler: `valheim-stall-calib.py`, read on the VM, never modified or restarted for this
  analysis (read-only per the task's own constraint).
- Data: `/var/lib/valheim-status/stallcalib-2026-09-15.jsonl`, copied off the VM read-only,
  analyzed entirely offline with `valheim-stall-calib-analysis.py` (this directory).
- Ground truth: `sudo journalctl -u valheim`, re-derived directly rather than trusted from any
  prior list. Inside the capture window, exactly **one** `PrepareSave` line was found:

  ```
  09/15/2026 09:46:42 (16:46:42 UTC): PrepareSave: ZDOExtraData.PrepareSave done [155ms]
  ```

  The next scheduled save (~10:16:41 PDT) had not occurred by the time this analysis was run.
  Per the task brief, this analysis does not wait for it — it reports what one save event
  supports and says plainly where that ceiling is.

## 1. Schema and effective sample rate

`valheim-stall-calib.py` writes one JSON line per **kept** sample: `t` (wall clock ISO8601),
`mono` (monotonic seconds), `dt` (seconds since the previous kept sample), `rq`/`sq` (UDP
rx_queue/tx_queue depth on the game port, or `null` if unreadable), and `rtt_status`/`rtt_ms`
present only on the subset of ticks where an A2S query was actually sent that cycle (self-paced,
starting at 5 Hz, backing off on repeated timeouts — see the sampler's own docstring). A sample
whose `dt` falls outside `[0, 5]` seconds is dropped and logged, never fabricated or
interpolated; **0 such drops occurred in this capture.**

Measured directly from consecutive `mono` values (never trusting the nominal 10 Hz):

| | value |
|---|---|
| median dt | **100.1 ms** |
| mean dt | 100.1 ms |
| range | [100.0, 148.0] ms |
| gaps > 200ms | **0** of 12,842 intervals |

The sampler held its nominal 10 Hz rate essentially exactly for the full ~21-minute capture
window analyzed here (12,843 samples, 0 dropped, 0 large gaps) — including straight through the
155ms save stall, which makes sense: the stall blocks the *game's* main thread, not this
sampler's own process.

## 2. Alignment to the one captured save event

A ~155ms stall against a ~100ms sample period is covered by only **1–2 samples** — stated
explicitly per the task brief, not left implicit. Concretely, for this event, the stall's own
155ms duration contains exactly **1 sample** from this run; a wider ±2s window around it is used
below to also show shape (before/during/after), not to claim more resolution than the sampler has.

| field | baseline before (median, samples) | peak during ±2s window | samples inside the stall's own 155ms |
|---|---|---|---|
| `rq` | 0 (20) | **0** | 1 |
| `rtt_ms` | 29.6 ms (10) | **45.6 ms** | 1 |

`rq`'s ±2s window is all zeros — the full 20-sample sequence around the save never leaves 0.
`rtt_ms`'s ±2s window (`[15.9, 12.1, 8.2, 4.3, 0.4, 45.6, 40.2, 37.0, 33.2, 29.3]`) shows a rise
to 45.6ms in the sample immediately after t=0, but see the baseline test below before reading
that as a stall response.

## 3. Mandatory baseline test

A single elevated sample during a save proves nothing by itself — both fields wobble on their
own. The full non-save distribution (everything ≥10s from the one known save, to avoid
contaminating the baseline with the stall's own aftereffects) settles the question of whether an
excursion of that size is actually rare:

**`rq` — full-capture distribution (12,843 samples, ~21.4 minutes):**

`rq` was **exactly 0 in every single sample of the entire capture** — min=0, max=0, mean=0,
0 non-zero readings. The baseline test against the save-window peak (0) is therefore vacuous by
construction: 0 ≥ 0 everywhere, 12,643 "hits" out of 12,643 non-save samples. That is not
evidence the proxy is noisy — it is evidence the proxy never had anything to measure. See the
confound below.

**`rtt_ms` — full-capture distribution (6,422 samples with an A2S tick):**

| stat | value |
|---|---|
| min / max | 0.0 / 49.2 ms |
| mean / median | 24.7 / 24.8 ms |
| p95 / p99 | 46.7 / 48.6 ms |

The save-window peak of 45.6ms sits at roughly the 93rd percentile of the *entire* distribution —
not a rare event. Directly: **an rtt_ms value ≥ 45.6ms occurred 461 times across 6,322 non-save
samples, a rate of ~1,291/hour (≈7.3% of all samples).** A value this large shows up on its own,
unprompted by any save, several times a minute. One post-save sample landing in that range is not
distinguishable from coincidence.

## 4. Verdicts, per signal, independently

### `rq`: **INCONCLUSIVE (untested precondition)** — not simply BLIND

The mechanical result is flat: `rq` never rose above its own baseline in the one captured save
window, and it never moved at all anywhere in the 21-minute capture. Taken alone that would read
as a clean BLIND (a channel that had every opportunity to move and didn't). It does not, because
of a confound worth stating adversarially: **`journalctl -u valheim` shows 0 players connected
for the entire capture** (`Connections 0 ZDOS:365421 sent:0 recv:0` at both 09:39 and 09:49
PDT). `rq` is *rx_queue depth on the game port* — it can only show a backlog if packets are
arriving for the main thread to fall behind on. With zero connected players there is essentially
no inbound game-port traffic to begin with, stall or no stall, so `rq` reading 0 throughout is
exactly what an *untested* proxy looks like, not a *refuted* one. This run cannot distinguish "rq
doesn't respond to stalls" from "rq was never given anything to respond with."

### `rtt`: **INCONCLUSIVE**, leaning toward the BLIND/off-thread finding, not yet confirmed

Unlike `rq`, A2S query traffic does not depend on players being connected — the query port
answers server-info requests regardless. So the rtt test is not subject to the same precondition
gap. But with only one save event, and the one post-save sample that looked elevated (45.6ms)
sitting at the ~93rd percentile of the whole distribution (occurring at a ~1,291/hour baseline
rate), this analysis cannot separate a genuine main-loop-adjacent effect from A2S's ordinary
noise. The task brief's hypothesis — that a flat rtt through a known stall would mean A2S is
answered off the game thread and therefore useless as a tick-stall proxy — is **plausible and not
contradicted** by this data (nothing here shows a *reliable* rise tied to the stall), but N=1
cannot **confirm** it either. Calling this VALIDATED-flat (i.e., BLIND) on one event would be
exactly the kind of round-up-to-a-verdict this script's docstring and selftest are built to
prevent.

## What would settle this

1. **`rq`**: needs a capture window with players actually connected (even 1) during a save tick,
   so the queue has something to potentially back up. Zero-player captures cannot validate or
   refute this proxy — only rule out one failure mode (a queue that's never zero even at rest,
   which it also isn't).
2. **`rtt`**: needs several more save events (this run's own logic requires ≥3 before it will
   even consider a rate-based VALIDATED call) with rtt sampled continuously through each, so a
   proper "peak-during vs. matched-length windows away from saves" comparison has enough events
   to be a real test rather than a coincidence check. At one ~30-minute save cadence, roughly
   2 hours of capture gets 3–4 events — enough to move past INCONCLUSIVE in either direction.
3. Both: a formal significance test (e.g., a permutation test over save-vs.-non-save window
   peaks) once there are enough events to make one meaningful — with N=1 there is nothing to
   permute.

## Reproducing this analysis

```
python3 azure/dashboard/valheim-stall-calib-analysis.py --selftest
python3 azure/dashboard/valheim-stall-calib-analysis.py \
  --samples /path/to/stallcalib-YYYY-MM-DD.jsonl \
  --saves   /path/to/saves.tsv     # TAB-separated: ISO8601-UTC<TAB>duration_ms, one save per line
```

`--selftest` includes an inversion check: a synthetic case where the peak/baseline comparison is
deliberately deleted and shown to disagree with the real implementation's BLIND verdict, so the
test is a lock on the actual comparison direction, not a status-only assert.
