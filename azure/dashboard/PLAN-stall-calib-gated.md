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
