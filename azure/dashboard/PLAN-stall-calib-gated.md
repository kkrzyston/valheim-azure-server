# Player-gated `rq` stall calibration

Companion to the egress-ceiling investigation (`valheim-egress-probe.py`) and its one-off
calibration predecessor (`valheim-stall-calib.py`, previously ungated -- this replaces it in
place, same filename, same repo location). Read those first; this doc does not repeat their
design notes.

## The problem this fixes

`valheim-egress-probe.py` carries an `rq` field (UDP rx_queue depth on the game port) meant to
catch a main-loop stall directly: if Valheim's Unity main thread stalls on ZDO churn, it stops
draining its UDP socket and the kernel receive buffer fills. That is a much stronger signal than
`rtt` (A2S round trip), which the probe's own R6 has already left INCONCLUSIVE-leaning-BLIND after
a month of data. But `rq` only rises if packets *arrive* and the main loop is too stalled to drain
them -- and the one-off calibration run had 0 players online all day, so nothing could ever queue.
World saves (`PrepareSave: ZDOExtraData.PrepareSave done`, 151-159ms observed, every ~30 minutes
regardless of player count) are a known, timestamped main-loop stall -- but only useful as a
calibration event if something is sampling *while it happens*, and only informative about `rq`
specifically if someone is connected and sending when it fires. A one-off run that expires on a
timer and isn't running the next time players log on misses the opportunity entirely.

## What changed

`valheim-stall-calib.py` is now player-gated and self-stopping, matching
`valheim-egress-probe.py`'s conventions:

- **Gate**: reuses the exact same source and staleness handling as the egress probe --
  `status.json` (`STALLCALIB_STATUS`, same default path), `generated` timestamp, `players.count`,
  refused (treated as 0) past `STALLCALIB_STATUS_MAX_AGE` (180s default). Idle-polls at
  `STALLCALIB_IDLE_POLL_SEC` (10s) while `players == 0`; samples at 10 Hz only while `players > 0`.
- **Auto-stop**: counts world-save windows (`PrepareSave` journal lines) that occurred *while
  players were connected*, via a resumable `journalctl --after-cursor` poll every 30s (far cheaper
  than the 10 Hz /proc reads, and frequent enough relative to a ~30-minute save interval). Once
  `STALLCALIB_TARGET_WINDOWS` (default **5**, see justification below) such windows are captured,
  it sets `done: true` in a persisted state file and idles forever after -- surviving restarts and
  reboots, since the flag is read from disk on every start, not held only in memory.
- **Bounded retention**: day-partitioned JSONL (`stallcalib-YYYY-MM-DD.jsonl`), pruned on both age
  (`STALLCALIB_RETAIN_DAYS`, default 14) and total on-disk size (`STALLCALIB_MAX_MB`, default 60),
  oldest file first, enforced hourly regardless of gate state (prune() is only called from the
  main loop's hourly timer, not from every flush -- corrected here per PR#8 review item 3; the
  timer's initial state also guarantees an effectively-immediate first prune at startup) -- the
  class of defect this guards against is the one this session found and fixed in a sibling script
  (`valheim-relay-check.py`, unbounded persisted retention). The file actively being appended to
  this cycle is never evicted even if it alone exceeds the size cap. No personal data is recorded:
  queue depths, timings and player *counts* only, never addresses or names.
- **Drop accounting**: every dropped sample (`dt` outside sane bounds) and buffer overflow
  increments an in-memory counter, surfaced on every heartbeat log line -- never silently
  discarded, never fabricated.
- **A2S throttled down further**: starts at 1 Hz (`STALLCALIB_A2S_HZ_START`, vs. the predecessor's
  5 Hz) and still backs off by half on consecutive timeouts, floor 0.2 Hz. `rtt` is a secondary,
  corroborating signal here -- `rq` is the thing this run exists to calibrate, and `rtt` is
  already covered by the always-on egress probe's own `RTT_EVERY`-gated sampling. A2S is a real
  UDP round trip the game's query thread has to answer; the /proc reads are not.

## Why 10 Hz, measured, not assumed

The constraint: this VM is `Standard_D2as_v7`, 2 vCPU = **one physical core plus its SMT
sibling**, and Valheim's server loop is effectively single-threaded. This sampler runs exactly
when players are online -- the one moment added load is unacceptable, because an instrument that
perturbs the game corrupts its own measurement as well as harming real players. So the CPU cost
had to be measured, not estimated:

- **Idling (0 players), the deployed one-off predecessor**, over an 83-minute window (~5001s
  elapsed): 12s of process CPU time, i.e. **~0.24% of one core**. (`ps`/`/proc/<pid>/stat`
  utime+stime against elapsed wall time.)
- **Actively sampling at 10 Hz (rq/sq) with a simulated player online**, this version, measured
  with `/usr/bin/time -v` over a clean 40s run (`STALLCALIB_STATUS` pointed at a synthetic
  fresh `{"players":{"count":1}}` file so the gate opened without touching production
  `status.json`; 300 samples written, no drops, no journalctl errors): **0.08s user + 0.02s
  system = 0.10s CPU over 40s wall-clock, ~0.25% of one core.**

Both numbers land in the same place because the two /proc reads per tick dominate cost and are
"nearly free" as the brief anticipated; the A2S component barely registers at 0.2-1 Hz. 10 Hz is
therefore cheap enough to keep: dropping to 5 Hz (200ms period) would make the ~155ms save stall
span under one sample on average and risk missing it by phase alone, which would defeat the
purpose of running this at all. `systemd` additionally caps the unit at `CPUQuota=15%` as a hard
ceiling in case of an unforeseen pathology (a runaway journalctl loop, a stuck socket read),
several times the measured cost.

## Why 5 save windows

`valheim-egress-probe.py`'s own R6 analysis needed roughly 10+ windows to say anything about
`rtt` because that is a **mean-comparison problem**: is the *distribution* of round trips during a
stall shifted from baseline, which needs enough samples per side to separate signal from ordinary
jitter. `rq` is a different, and much easier, kind of question. Its baseline today is **flat
zero** -- 0 players online all day and not one non-zero `rq` reading recorded by the predecessor
run. Against a flat-zero baseline, a single non-zero excursion measured squarely inside a
timestamped `PrepareSave` window is already informative; the risk is not "not enough data to see a
shift in a noisy distribution," it's "was that one excursion a coincidental packet-arrival
artifact rather than the stall." Five independent windows -- five separate save ticks, each with
players connected and sampled at 10 Hz through it -- is enough to rule out a one-off timing fluke
while keeping the ask realistic: at one save every ~30 minutes, 5 windows is ~2.5 cumulative hours
of play, achievable within a handful of normal evening sessions rather than a month-long wait.

## Non-disturbance verification

Captured as the first action, before any change was made, and rechecked after deploying and
running the new service (idle, then a 40s gated-sampling burst against a simulated player):

| Check | Before | After | Verdict |
|---|---|---|---|
| `valheim.service` MainPID / ActiveEnterTimestamp / NRestarts | 865 / 2026-09-11 19:18:31 / 0 | 865 / 2026-09-11 19:18:31 / 0 | **PASS** -- untouched, never restarted |
| `valheim-egress.service` MainPID / ActiveEnterTimestamp / NRestarts, and its own drop count | 85757 / 2026-09-14 19:24:16 / 0, heartbeats show "no samples dropped" | 85757 / 2026-09-14 19:24:16 / 0, heartbeats still show "no samples dropped" through the deploy window | **PASS** -- untouched, no attributable drops |
| `inet valheim_meter` / `inet valheim_peermeter` nftables counters | meter counters non-zero and monotonically increasing; peermeter sets empty (0 players, nothing to count) | meter counters still increasing (not reset to zero); peermeter sets still empty, unaffected | **PASS** -- this script never calls `nft` at all; it only reads `/proc/net/udp` and `status.json`, both already covered by the unit's `ProtectSystem=strict` + `ReadWritePaths=/var/lib/valheim-status` |

The `valheim-stall-calib.service` game-service restart limit is deliberately never exercised: at
no point was `valheim.service` stopped, restarted, or reloaded.

## Systemd hardening

At least as strict as `valheim-egress.service`, and narrower where this unit's scope allows it to
be: `Nice=10`, `IOWeight=50`, `NoNewPrivileges`, `ProtectSystem=strict` with
`ReadWritePaths=/var/lib/valheim-status` only, `ProtectHome`, `PrivateTmp`,
`ProtectControlGroups`, `ProtectClock`, `ProtectHostname`, `RestrictRealtime`, `RestrictSUIDSGID`,
`RestrictNamespaces`, `LockPersonality`, plus a hard `MemoryMax=96M` / `TasksMax=16` /
`CPUQuota=15%` ceiling that `valheim-egress.service` does not carry (this unit never needs to call
`nft`, so it has no cold-boot `CAP_SYS_MODULE` requirement to work around, and its steady-state
cost is low enough that a hard CPU quota is a safe belt-and-braces rather than a risk of starving
a legitimate burst).

## Mutation test (item 5's real lock)

`--selftest` was deliberately broken (the size-bound eviction loop in `prune()` was short-circuited
to `while False and ...`) and re-run: both size-bound assertions failed as expected --

```
retention: size bound (THE lock this item exists for)
  FAIL oldest files are unlinked until under the MB cap  -- 5.0
  ok   the newest file(s) survive, not the oldest
  FAIL stallcalib-2026-01-01.jsonl (oldest) was the first evicted
...
selftest FAILED: oldest files are unlinked until under the MB cap, stallcalib-2026-01-01.jsonl (oldest) was the first evicted
```

The mutation was then reverted and `--selftest` passes clean (44 checks, both locally off-VM and
on the VM itself, no `nft`/game/root needed off-VM; `--selftest` is also run by the installer
before the unit is (re)started, so a bad deploy fails loudly instead of shipping unattended).

## Installation

Added to `install-dashboard.sh` alongside the egress-probe block: `install`s the script and unit,
runs `--selftest` before touching the running unit, `daemon-reload`s, and `enable --now`s it --
deliberately enabled, not merely installed, since the whole point is to catch the next play
session unattended. Idempotent: re-running the installer just re-installs the same files and
re-runs selftest; it never touches `stallcalib-state.json` or the sample data.

## Incident: idle-gap cursor sweep falsely marked calibration "done" (2026-09-15)

PR#8's fix only seeded `journal_cursor` on the *first-ever* gate-open (`journal_cursor is None`).
A session ran ~11:41-12:01 UTC and disconnected, leaving `journal_cursor` pointing at ~12:01. The
server then sat empty until 14:56:54 UTC (~3 hours). On that reconnect the gate opened, sampling
started -- but because a cursor already existed, nothing re-seeded it, so the very next journal
poll ran `journalctl --after-cursor <12:01 cursor>`, which swept the *entire empty-server gap*:
~6 saves at the ~30-min cadence, none of them with a player connected. All six were credited as
"save windows with players connected" in one poll, jumping `save_windows_captured` 3 -> 8 in a
single line and tripping the `done` state before the evening's real play session -- the one this
whole calibration exists to capture -- could run at all. Live evidence:

```
{"save_windows_captured": 8, "journal_cursor": "s=e330ec...", "done": true, "done_at": "2026-09-15T21:56:54+00:00"}
```
```
14:56:54  valheim-stall-calib: 1 player(s) online -- sampling at 10 Hz
14:56:54  valheim-stall-calib: captured 5 save window(s) with players connected (8/5 total)
14:56:54  valheim-stall-calib: calibration complete
```
all three lines in the same second, with zero new 10 Hz samples written in between -- the tell
that the "5 windows" it just claimed were never actually sampled through, they were swept from
journal history that predates the reconnect.

**Fix 1 -- re-seed the cursor on EVERY gate-open, not just the first.** `maybe_seed_journal_cursor`
(seed-if-missing) is now `reseed_journal_cursor_at_gate_open` (seed-unconditionally): every
players 0->N transition discards whatever cursor was left over from before and starts the journal
window fresh at the moment of reconnect. A save can only be credited if it happened after this
moment, which by construction excludes anything from an idle gap, no matter how long the gap was.
Locked by a selftest that reproduces this incident's exact shape (a stale cursor from "the previous
session," reseed asserted, then an idle-gap `PrepareSave` shown to be structurally unreachable
because `journalctl --after-cursor` is pointed at the fresh cursor) -- reverting the fix back to
seed-if-missing makes that lock fail immediately (see PR mutation-test evidence).

**Fix 2 -- repair state, re-arm.** The corrupted state (`save_windows_captured: 8, done: true`) was
replaced with a recount of only the windows that genuinely had a player connected and sampled
(from the raw `stallcalib-2026-09-15.jsonl`): three saves, 18:46:43 [205ms], 18:58:36 [146ms],
19:00:41 [215ms], all inside the single continuous sampling stint 18:42:25-19:01:29 UTC. `done` is
reset to `false` and the cursor is re-seeded at repair time so it cannot re-credit anything from
before the fix was deployed.

**Fix 3 -- require >= 2 concurrent players to COUNT a window (`STALLCALIB_MIN_PLAYERS_FOR_WINDOW`,
default 2, env-configurable).** The 1-player stall symptom under investigation (position blinking,
hit-registration mismatch) is reported only when several players are clustered together, and `rq`
is quantized at 2112 bytes (one packet) -- a single player's inbound rate is a much smaller signal
than several players' combined rate, so it is plausible that a ~150-200ms stall simply cannot push
one player's backlog past even a single quantum step. All three of the "valid" windows above are
1-player windows with `rq` flat at zero throughout -- consistent with *either* "no stall happened"
*or* "the stall happened but was too small at n=1 to register," and the existing data cannot tell
those two apart. A 1-player window is therefore weak evidence either way and must not retire the
calibration target: sampling still runs at n=1 (the data stays useful context, e.g. for the rq
excursion investigation below), but only a window where the minimum player count over the whole
poll interval was >= the threshold moves `save_windows_captured`. Every counted window's player
count is now recorded in `state["windows"]` for audit.

## Investigation: 18 non-zero `rq` excursions with no nearby save (2026-09-15)

In the existing capture (`stallcalib-2026-09-15.jsonl`, 11,443 samples, 18:42:25-19:01:29 UTC),
`rq` is non-zero in exactly 18 samples -- quantized at multiples of 2112 bytes (max 6336),
clustered 18:49:30-18:56:09 UTC with one straggler at 18:58:59 -- while flat zero through all
three real (1-player) save windows. All 18 occurred at `n=1`.

- **journal cross-reference:** the game's own journal is silent through the whole cluster except
  for its routine ~10-minute `Connections N ZDOS:... sent:... recv:...` heartbeat line at 18:49:04
  UTC (just before the cluster starts) -- no save, backup, spawn, or ZDO-burst log line falls
  inside the 18:49:30-18:56:09 window. **Could not determine** a journal-visible cause.
- **egress-probe cross-reference:** matching each excursion's timestamp to the nearest 1 Hz sample
  in `egress-2026-09-15.jsonl`, 5 of the 18 land within ~1-2s of an unusually large outbound burst
  (`txb` ~50-100 KB in one 1s sample vs. a ~1-1.3 KB baseline, average packet size near max MTU) --
  suggestive of a bulk ZDO/terrain sync send to the one connected client. The other 13 have no
  such burst at their nearest 1 Hz sample. This is a partial, unconfirmed correlation, not a
  proven cause: the egress probe's 1 Hz granularity is far coarser than the 100ms-resolution `rq`
  excursions, so exact causal alignment cannot be established, and it does not explain the
  majority of the 18.
- **Explicitly NOT concluding `rq` is "blind."** With only one player connected throughout this
  capture, the same "too little inbound rate to push past one quantum" limitation that motivates
  Fix 3 applies here too: these 18 excursions could be genuine (small) stall backlogs, or they
  could be ordinary single-quantum jitter in a low, mostly-zero baseline -- the data as collected
  cannot distinguish the two. Resolving this needs windows with >= 2 concurrent players, which is
  exactly what Fix 3 is designed to collect going forward.

**Verdict: could not determine** a definitive cause for the 18 `rq` excursions. Best lead is the
partial outbound-burst correlation above; the open question (genuine small stall vs. quantization
noise at low signal) is left for the >= 2-player data this fix now requires before counting a
window.
