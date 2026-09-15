# Stall calibration: does rq or rtt actually see a world-save stall?

`valheim-egress-probe.py` samples two fields it claims are mod-free proxies for a Valheim
main-loop stall: `rq` (UDP rx_queue depth on the game port, from `/proc/net/udp`) and `rtt`
(A2S round trip to the local query port). Neither claim had been checked against a known stall.
`valheim-stall-calib.py` (a one-off, 10 Hz sampler) exists to check them against the one stall
this box produces on a predictable, journal-timestamped schedule: a world save (`PrepareSave:
ZDOExtraData.PrepareSave done [NNNms]`), which blocks the Unity main thread for a measured
duration with nothing else required to reproduce it.

**Updated 2026-09-15 ~10:48 PDT: all three scheduled save windows for this capture have now
occurred (N=3, up from N=1).** The capture started 2026-09-15T16:33:07Z (09:33:07 PDT) and was
still running when this update was done, at 45,320 samples spanning ~78 minutes.

## Setup

- Sampler: `valheim-stall-calib.py`, read on the VM, never modified or restarted for this
  analysis (read-only per the task's own constraint).
- Data: `/var/lib/valheim-status/stallcalib-2026-09-15.jsonl`, re-copied fresh off the VM for
  this update (the earlier N=1 partial copy was discarded, not reused), analyzed entirely
  offline with `valheim-stall-calib-analysis.py` (this directory).
- Ground truth: `sudo journalctl -u valheim`, re-derived directly rather than trusted from any
  prior list. Inside the capture window, three `PrepareSave` lines were found:

  ```
  09/15/2026 09:46:42 (16:46:42 UTC)  PrepareSave: ZDOExtraData.PrepareSave done [155ms]
  09/15/2026 10:16:42 (17:16:42 UTC)  PrepareSave: ZDOExtraData.PrepareSave done [159ms]
  09/15/2026 10:46:42 (17:46:42 UTC)  PrepareSave: ZDOExtraData.PrepareSave done [151ms]
  ```

  A fourth line, `World auto backup saved [25ms]`, also fired at 10:46:42 — the backup tick
  riding the same save cycle. It is not used as a calibration event here: the sampler's own
  docstring picks `PrepareSave` as the trigger because it is the more frequent, reliably-present
  event, and mixing a 25ms backup-write stall into the same analysis would blur what "the stall"
  means. All three `PrepareSave` events are used; none were excluded.

## 1. Schema and effective sample rate

`valheim-stall-calib.py` writes one JSON line per **kept** sample: `t` (wall clock ISO8601),
`mono` (monotonic seconds), `dt` (seconds since the previous kept sample), `rq`/`sq` (UDP
rx_queue/tx_queue depth on the game port, or `null` if unreadable), and `rtt_status`/`rtt_ms`
present only on the subset of ticks where an A2S query was actually sent that cycle (self-paced,
starting at 5 Hz, backing off on repeated timeouts — see the sampler's own docstring). A sample
whose `dt` falls outside `[0, 5]` seconds is dropped and logged, never fabricated or
interpolated; **0 such drops occurred in this capture.**

Measured directly from consecutive `mono` values (never trusting the nominal 10 Hz), now over
the full ~78-minute capture:

| | value |
|---|---|
| median dt | **100.1 ms** |
| mean dt | 100.1 ms |
| range | [100.0, 189.3] ms |
| gaps > 200ms | **0** of 45,319 intervals |

The sampler held its nominal 10 Hz rate essentially exactly for the entire ~78-minute capture
(45,320 samples, 0 dropped, 0 large gaps) — including straight through all three save stalls,
which makes sense: the stall blocks the *game's* main thread, not this sampler's own process.

## 2. Per-window alignment (N=3)

A ~150–160ms stall against a ~100ms sample period is covered by only **1–2 samples** — true for
all three events below, not just the first. A wider ±2s window is shown for shape
(before/during/after), not to claim more resolution than the sampler has.

| save (UTC) | duration | field | baseline before (median) | peak during ±2s | samples inside the stall's own duration |
|---|---|---|---|---|---|
| 16:46:42 | 155ms | `rq` | 0 (n=20) | **0** | 1 |
| 16:46:42 | 155ms | `rtt_ms` | 29.6ms (n=10) | **45.6ms** | 1 |
| 17:16:42 | 159ms | `rq` | 0 (n=19) | **0** | 2 |
| 17:16:42 | 159ms | `rtt_ms` | 33.2ms (n=9) | **47.4ms** | 1 |
| 17:46:42 | 151ms | `rq` | 0 (n=20) | **0** | 1 |
| 17:46:42 | 151ms | `rtt_ms` | 19.15ms (n=10) | **39.0ms** | 1 |

`rq` is flat at 0 in all three windows — every one of the 20-sample ±2s sequences around all
three saves stays at exactly 0, no exceptions.

`rtt_ms` shows a rise above its own pre-save baseline in **all three** windows (45.6 vs 29.6ms;
47.4 vs 33.2ms; 39.0 vs 19.15ms). That is a more consistent pattern than the N=1 result alone
could show — but window shape is not consistent: in windows 1 and 2 the rise lands a few samples
*after* t=0, while in window 3 the highest value is the very first in-window sample (`t=0`
itself, `39.0`) and the rest of the window trends *down*. Three different shapes, not one
repeating signature. That inconsistency, plus the baseline test below, is why this does not
settle the question by itself — see §4.

## 3. Mandatory baseline test, and the significance test N=1 couldn't run

A single elevated sample during a save proves nothing by itself — both fields wobble on their
own. The full non-save distribution (everything ≥10s from any of the three known saves, so the
baseline is never contaminated by a stall's own aftereffects) settles the question of whether an
excursion of that size is actually rare — and now, with ~78 minutes of baseline, there is enough
data to also ask the sharper question: **how surprising is it that all 3 windows moved, given how
often excursions like these happen anyway?**

**`rq` — full-capture distribution (45,320 samples, ~78 minutes):**

`rq` was **exactly 0 in every single sample of the entire 78-minute capture** — min=0, max=0,
mean=0, 0 non-zero readings anywhere, save window or not. As with the N=1 result, the baseline
test against a peak of 0 is vacuous by construction (0 ≥ 0 everywhere: 44,721 "hits" out of
44,721 non-save samples, nominally "35,483.74/hour"). A larger N does not change this — see the
confound in §4.

**`rtt_ms` — full-capture distribution and per-window baseline rate:**

| save (UTC) | peak | non-save exceedance rate | P(≥1 excursion this size in a random 2s window, by chance) |
|---|---|---|---|
| 16:46:42 | 45.6ms | 1,307.63/hour (1,648/22,361 samples) | **0.5164** |
| 17:16:42 | 47.4ms | 669.68/hour (844/22,361 samples) | **0.3107** |
| 17:46:42 | 39.0ms | 3,712.62/hour (4,679/22,361 samples) | **0.8729** |

The per-window significance test (Poisson, using each window's own non-save exceedance rate as
the background hazard) says the probability of an excursion at least that large landing in a
random 2-second window *by chance alone* is 31–87%, depending on the window. These are not small
numbers — a coin flip or worse. None of the three individual excursions is a surprise on its own.

**Pooled:** 3/3 windows moved above their own pre-save baseline. Summing each window's own
by-chance probability gives **1.70 windows expected to show a comparable excursion purely by
chance**, against **3 observed**. Observed exceeds expected, but not by the margin this script's
own verdict logic requires to call that more-than-chance (it requires observed to at least
double the expected count, or exceed it by at least one full window, before calling VALIDATED —
see `pooled_significance()`/`verdict_for_signal()` in the script). 3 vs. an expectation of 1.70
is consistent with "got a little lucky, three times, in a data set where a comparable excursion
already happens over a thousand times an hour" — it does not clear the bar for a real detection.

## 4. Verdicts, per signal, independently (N=3)

### `rq`: **INCONCLUSIVE (untested precondition)** — still not VALIDATED, still not BLIND

The mechanical result is unchanged from N=1 and is now stronger in the same direction: `rq` never
rose above its own baseline in any of the three captured save windows, and it never moved at all
anywhere across 45,320 samples over 78 minutes. Taken alone that reads as a clean, well-powered
BLIND (a channel that had 45,320 opportunities to move and took none of them).

It is not called BLIND, for the same reason as before, now reconfirmed at N=3: **`journalctl -u
valheim` shows 0 players connected for the entire 78-minute capture**
(`Connections 0 ZDOS:365421 sent:0 recv:0` at nine consecutive ~10-minute checkpoints, 09:39
through 10:39 PDT). `rq` is *rx_queue depth on the game port* — it can only show a backlog if
packets are arriving for the main thread to fall behind on. With zero connected players there is
no inbound game-port traffic to begin with, stall or no stall, so `rq` reading 0 throughout is
exactly what an *untested* proxy looks like. **A larger sample size does not fix a precondition
that was never met** — 45,320 samples of "no packets ever arrived, so nothing ever queued" is not
more evidence than 12,843 samples of the same thing. This run still cannot distinguish "rq
doesn't respond to stalls" from "rq was never given anything to respond with," and no amount of
additional zero-player capture time will change that. The fix is a capture with at least one
connected player, not a longer one.

### `rtt`: **INCONCLUSIVE**, and now leaning further toward the BLIND / off-thread finding

With N=3, all three windows show a nominal rise — more consistent-looking than N=1 alone. But the
mandatory baseline and pooled significance test both say this is not distinguishable from noise:
per-window by-chance probabilities of 31–87%, and a pooled observed-vs-expected of 3 vs. 1.70,
which is short of what this script's own bar requires to call it more than chance. The window
shapes are also inconsistent with each other (§2) — a real fixed-latency response to a fixed-
duration stall would be expected to look similar each time; a post-t0 rise, another post-t0 rise,
and a pre-t0 peak that decays are not the same signature repeating three times.

The task brief's hypothesis — that a flat rtt through a known stall would mean A2S is answered
off the game thread and is therefore useless as a tick-stall proxy — is **not contradicted by
this data, and is now better supported than at N=1**: three independent stalls, none of them
producing an excursion large enough to clear its own local noise floor, is the shape a genuinely
blind signal would have. This analysis stops short of calling it BLIND outright only because the
task's own verdict criteria (§ script) require the *baseline_before → peak* comparison to show no
rise at all for a BLIND call, and technically all three windows did show a nominal rise — just
one indistinguishable from chance. Practically: **if this is meant to answer "can rtt see a
main-loop stall," the honest current answer is no, not yet demonstrated, and the trend across
three independent events points toward it never being demonstrated with this sampling
scheme.**

## What would settle this

1. **`rq`**: needs a capture window with players actually connected (even one) during a save
   tick, so the queue has something to potentially back up. No amount of additional zero-player
   capture time settles this — the precondition, not the sample size, is the blocker.
2. **`rtt`**: the N≥3 bar this script requires before even attempting a rate-based VALIDATED
   verdict has now been met, and the answer it gives is INCONCLUSIVE, not VALIDATED. Settling
   this in either direction needs enough additional save events that the pooled
   observed-vs-expected gap becomes unambiguous — at the current baseline rate (~700–3,700/hour
   depending on the window), that likely means several more hours of capture (10+ save events)
   before the pooled test has the power to separate "real effect, diluted by noise" from "no
   effect, noise looks like a trend in small samples." A tighter per-window search radius than
   ±2s (the stall itself is only 1-2 samples wide) would also sharpen the test, at the cost of
   needing very precise clock alignment between the sampler and the game process.
3. Both: consider a longer-baseline, cleaner significance test (e.g. a full permutation test
   over many resampled 2s windows rather than the closed-form Poisson approximation used here)
   once there is enough data for the extra rigor to matter.

## Reproducing this analysis

```
python3 azure/dashboard/valheim-stall-calib-analysis.py --selftest
python3 azure/dashboard/valheim-stall-calib-analysis.py \
  --samples /path/to/stallcalib-YYYY-MM-DD.jsonl \
  --saves   /path/to/saves.tsv     # TAB-separated: ISO8601-UTC<TAB>duration_ms, one save per line
```

`--selftest` (17 checks) includes two inversion checks: one for the flat/BLIND comparison (a
synthetic case where the peak/baseline comparison is deleted disagrees with the real
implementation), and one for the pooled significance path (a synthetic case where the
expected-by-chance comparison is deleted would call a noise-explained result VALIDATED; the real
implementation disagrees). Neither is a status-only assert — both simulate the specific deleted
comparison and confirm the verdict flips.
