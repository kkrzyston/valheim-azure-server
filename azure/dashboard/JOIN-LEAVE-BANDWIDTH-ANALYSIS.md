# Does a join/leave settle GLOBAL vs PER-PEER? No -- and here is why not

**Script:** `valheim-jointransition-report.py` (read-only, run by hand: `python3
valheim-jointransition-report.py samples.jsonl`). **Data:** a copy of
`/var/lib/valheim-status/samples.jsonl`, the 60-second dashboard collector's own history file
(`valheim-status-collect.py`), read-only, nothing on the VM touched.

## The question this was supposed to settle

A prior analysis (`valheim-egress-report.py`) found a hard outbound ceiling on this server: the
observed maximum sits within 0.02% of 270 KiB/s (4.5 x 61440), and observed maxima exceeded
`n x 61440` at every player count 1-4 by 12-51%. That refutes a strict per-peer 61440 B/s ZDOMan
budget outright. What's left is GLOBAL (one server-wide byte budget, shared across everyone) vs.
PER-PEER (a per-connection budget, just not 61440). That prior session said the two could only be
told apart by varying group size, and warned that a group that always plays at the same size
leaves the verdict INCONCLUSIVE forever.

The idea here: we don't need an arranged session. Whatever this deployment's history already
contains includes moments where a player joined or left mid-session. At the instant of a join,
GLOBAL predicts per-player throughput **drops** (same budget, more ways to split it) while
PER-PEER predicts it stays **flat** while total throughput steps up. Mirror image on a leave.
That's a within-session natural experiment on data that already exists.

## Correction to the premise: this is not 30 days of data

`RETAIN_SECONDS` in the collector is `30 * 86400`, and the collector does keep up to 30 days --
but the server was deployed 2026-09-10 (`JOURNAL_EPOCH = "2026-09-10 13:40"` in the collector
script itself), so it has not been running for 30 days. The actual file, as copied down for this
analysis:

```
path:   /var/lib/valheim-status/samples.jsonl
size:   497,952 bytes
lines:  5,129 (5,008 with rx/tx present -- the fields were added partway through, at line 122)
span:   2026-09-11 22:24:22 UTC -> 2026-09-15 16:30:58 UTC  ==  3.67 days
```

Schema (one line, raw, from a stretch with rx/tx present):

```json
{"t":1789172901,"p":3,"c":3.1,"m":25.4,"o":1,"rx":6109201962,"tx":1304591478,"w":6857564}
```

`t` unix seconds, `p` players online, `c` CPU %, `m` memory %, `o` server-active flag, `rx`/`tx`
**cumulative** eth0 byte counters (need diffing between samples to get a rate -- this script does
that the same way the collector's own live `net_bps` calculation does), `w` world file size in
bytes (not a rate). A `pg` field (per-player ping, keyed by character name) appears on some lines
and is never read by this script -- there is nothing here to anonymize because player identity is
never touched.

This does not invalidate the "30-day" ceiling finding -- the max-observed-rate claim just describes
fewer distinct days than advertised, and more data will keep accumulating. It matters here because
it also bounds the transition inventory below.

## Transition inventory

76 total player-count transitions in 3.67 days:

| from -> to | count | | from -> to | count |
|---|---|---|---|---|
| 0->1 | 10 | | 3->2 | 11 |
| 1->0 | 9  | | 3->4 | 7  |
| 1->2 | 6  | | 4->3 | 7  |
| 2->0 | 1  | | 4->5 | 4  |
| 2->1 | 5  | | 5->4 | 4  |
| 2->3 | 11 | | 3->0 | 1  |

The 4<->5 pairs -- the only counts that could show a per-connection budget stacking meaningfully
above the 61440 figure -- have **4 samples each**. Anything computed from them alone would be
noise dressed up as a finding.

## Measurement method

For each transition: a 90-second settle gap is excluded on each side (letting TCP-ish/ZDO
connection setup and any burst catch-up settle out), then up to 240 seconds of clean rate samples
are aggregated on each side. "Clean" means: the sample's own diff interval is fully inside the
window (no bridging across the transition), **and** its player count matches what that side
expects. That second condition matters a lot here -- several transitions in this data are only
about a minute apart (a whole group leaving or joining in a burst of single-player steps), and
without it, a transition's "after" window could silently include samples from a *second*,
unrelated transition. Adding that guard dropped usable transitions from 75/76 to **49/76**; the
other 27 simply don't have a clean answer and are printed as `[SKIP: insufficient clean samples]`
rather than guessed at.

Of those 49, a transition only counts as **active** (load-bearing) if inbound (rx) demand --
players' traffic *to* the server, not subject to the server's own send budget under either
hypothesis, used here purely as a proxy for "were people actually doing anything" -- is above a
2 KB/s floor on *both* sides. A join during idle standing-around tells us nothing about a budget
that was never being asked for more than it gives out at rest. **34 of 49** clean transitions
were active.

## What the numbers say

Across those 34 active transitions:

- `corr(demand change, total-tx change) = 0.31` -- positive, i.e. some of the swing in total
  egress around a transition really is just "people started doing more stuff," not redivision of
  a fixed budget. Not overwhelming (this is a noisy, 60-second-granularity measurement), but real
  enough that raw before/after deltas can't be trusted at face value.
- Only **3 of 34** active transitions have demand held roughly flat (`|d_rx| <= 20 KB/s`) on both
  sides -- the minimum needed to look at a join/leave without the demand confound doing the
  talking:

  | transition | d_total_tx | d_perpeer_tx | d_rx (demand) |
  |---|---|---|---|
  | join 3->4 | +48.0 KB/s | +6.1 KB/s | +7.1 KB/s |
  | join 4->5 | +34.4 KB/s | -0.1 KB/s | +2.9 KB/s |
  | leave 5->4 | -47.4 KB/s | -3.6 KB/s | +8.2 KB/s |

  Three points, mixed sign on `d_perpeer_tx`, is not a pattern -- it's three points.

- The global maximum tx rate anywhere in this file is **269.9 KB/s (99.98% of the assumed 270
  KB/s ceiling)**, confirming the prior session's ceiling finding still holds. But that maximum
  occurred during a **sustained 4-player stretch with no join or leave nearby** -- the nearest
  transitions are more than 900 seconds away on either side, outside this script's settle+window.
  Checking all 34 active transitions against that ceiling: **zero** have either side within 85%
  of it (i.e. within about 229.5 KB/s).

## Verdict: INCONCLUSIVE

Not "the data is too noisy to say" -- something more specific: **the two hypotheses only make
different predictions when the server is actually saturated**, and every usable, load-bearing
join/leave in this file happened well under the ceiling. Below the ceiling, GLOBAL and PER-PEER
both predict the same thing -- total egress tracks demand, nobody is fighting anybody else for
bytes -- so a transition down there is not evidence for either story, no matter how many of them
you collect. The one stretch that did approach the ceiling (269.9 KB/s at p=4) had no player-count
change anywhere near it.

This confirms the prior session's warning was correctly cautious, and sharpens it: it's not
"group size never varies" that blocks a verdict here (group size *did* vary, 76 times) -- it's
that a join/leave has never yet happened *while the server was already maxed out*. The needed
event is specific: a join or a leave recorded while total egress is already sitting near 270 KB/s,
i.e. with several players already generating heavy simultaneous load. Two more things would help
get there:

1. **The 1 Hz egress probe** (`valheim-egress-probe.py`), once it has real samples -- its
   settling window can be seconds instead of the 90+240 s this 60-second collector data forces,
   and the demand control (matched on inbound rate) can be far tighter.
2. **A deliberately loaded transition** -- not necessarily an "arranged session" in the R2 sense
   (varying group size across whole sessions), just watching for, or nudging toward, a join/leave
   that happens to land while several players are already in a raid or a boss fight.

Until then: GLOBAL remains the better-supported story from the *ceiling arithmetic* alone (per
the prior analysis), but this join/leave method has not added evidence either way, and should not
be cited as having done so.
