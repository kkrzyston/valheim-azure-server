# Per-peer byte accounting + relay-vs-direct determination

Companion to the egress-ceiling investigation (`valheim-egress-probe.py`, `valheim-meter-nft.sh`
-- read those first; this plan does not repeat their design notes). This work is additive and
does not modify either of those files or their table (`inet valheim_meter`).

## The question this answers

The existing probe established a hard ~270 KiB/s egress ceiling, but its counters are
**aggregate** -- six un-keyed totals summing all traffic on the game port. That leaves one
question genuinely undecided: is the ceiling a budget shared across every connected player, or a
budget each peer gets independently? Distinguishing those from the aggregate alone requires
catching different player counts online at once, which may never happen if the group always
plays at the same size. Per-peer counters answer it directly at whatever size shows up, without
waiting for a lucky night.

## What was built

**`valheim-peermeter-nft.sh`** installs a new, separate nftables table, `inet valheim_peermeter`,
alongside (never touching) `inet valheim_meter`. Two dynamic sets with per-element counters (an
nftables "meter"):

- `peer_tx` -- bytes/packets **sent to** each peer, keyed by `ip daddr`, updated in the
  `output`/game-port chain. This is the number that matters: the ceiling is on egress.
- `peer_rx` -- bytes/packets **received from** each peer, keyed by `ip saddr`, updated in the
  `input`/game-port chain.

Same conventions as `valheim-meter-nft.sh`: `ensure`/`install`/`show`/`remove`/`selftest`
subcommands, ports and timeouts from the environment, IPv4-only (matching `valheim_meter`'s own
`peers` set), `policy accept` with no verdict rule anywhere in the table, and the same
unquoted-heredoc-body hazard documented and guarded by `selftest`.

**Per-element counter support was verified empirically, not assumed.** Before this table was
designed, a scratch table (`inet scratch_test`, deleted immediately after) was built on the live
box and fed synthetic UDP traffic from a bound local socket. The resulting per-address elements'
byte counts matched IP+UDP header arithmetic exactly (e.g. a 100-byte payload plus a 10-byte
payload to the same destination produced `packets 2 bytes 166` -- 100+28 header, plus 10+28).
`nft --version` on the box is 1.0.2 ("Lester Gooch"); kernel is `6.8.0-1065-azure`. Conclusion:
per-element counters are fully supported, no fallback mechanism was needed.

**`valheim-peermeter-read.py`** is a standalone, one-shot JSON reader for the new table (`nft -j
list table inet valheim_peermeter`, parsed and validated). It is deliberately **not** wired into
`valheim-egress-probe.py` -- that file is owned by other work in flight this session. Output
shape and drop-on-malformed-input behavior are documented in the script's own module docstring;
`--selftest` covers the parser against fixtures (clean sample, one-sided traffic, malformed
counter, non-IPv4 val, missing set) with no root and no real `nft`.

### How this should be integrated later

`valheim-egress-probe.py` already samples once a second and already reads one nftables table via
`nft -j`. The natural integration is a second `nft -j list table inet valheim_peermeter` call
folded into the same 1 Hz loop (or an independent lower-frequency poll, since per-peer drift
matters less than the 1-second aggregate ceiling), added as a `peers: {ip: {...}}` field on each
row. That is a change to a file this agent does not own and was left undone on purpose --
`valheim-peermeter-read.py` exists so the shape of that integration is already decided and
tested, not so this agent could make it.

**`valheim-relay-check.py`** is a one-time (or `--continuous`) capture: it watches
`valheim_peermeter`'s `peer_rx` set for the next address that has never been seen before, and
classifies it against a small built-in table of Valve/Steam network ranges (AS32590 and the
long-documented Steam matchmaking/relay CIDR blocks), a reverse-DNS lookup as a corroborating
signal only, and an RFC1918/loopback check. It answers, for the next real join, whether the
observed source address looks like a direct client connection or a Valve-operated hop -- crossplay
is off (no `-crossplay` in the unit's `ExecStart`), so direct was expected, but the game log
(`Got connection SteamID <id>`, no IP) and an idle `ip_of` map meant that had never actually been
checked against a real address.

**Validated end-to-end without a real player**, since nobody was online: the capture was started
on the VM (`--timeout 60`, backgrounded), then a synthetic UDP packet was sent to the VM's public
game port from the operator's own machine over the network (not loopback, not spoofed -- a real
socket send from outside the VM, so it traverses `eth0` as genuine ingress the same way a real
client's packet would). The capture correctly detected the new `peer_rx` element within one poll
interval, classified it (`likely_direct_client`, PTR resolved to a real ISP hostname, no Valve
range hit), and wrote the JSON record. The test output file (which necessarily contained a real,
non-Valve client IP address -- the operator's own) was deleted from the VM immediately after and
was never committed; this repo and this script's own `--selftest`/examples only ever use RFC 5737
documentation-range addresses or Cloudflare's well-known `1.1.1.1`.

## Non-disturbance verification

All three checks below compare a baseline captured **before any change was made** against the
same measurement taken after every task in this plan was complete.

| Check | Before | After | Verdict |
|---|---|---|---|
| `valheim_meter` counters not reset | `game_tx/rx 0/0`, `query_tx 15960/1398756`, `query_rx 16101/879345`, `game_tx6/rx6 0/0` | `game_tx 0/0` (unchanged), `game_rx` up by exactly 1 packet / 60 bytes (the synthetic relay-check test packet sent in the step above -- expected, not a reset), `query_tx`/`query_rx` grew monotonically from ordinary A2S query traffic, `game_tx6/rx6` unchanged at `0/0` | **PASS** -- every counter is monotonic; nothing was zeroed |
| `valheim-egress.service` undisturbed | active, `MainPID=85757`, started `Mon 2026-09-14 19:24:16 PDT` | still active, **same** `MainPID=85757`, **same** `ActiveEnterTimestamp`, `NRestarts=0`; heartbeat log through the whole work window reads `heartbeat: idle, server empty; 0 samples written, 0 buffered; no samples dropped` | **PASS** -- process never restarted, no dropped samples attributable to this work |
| `valheim.service` (game) never restarted | `ActiveEnterTimestamp=Fri 2026-09-11 19:18:31 PDT`, `MainPID=865` | **identical** `ActiveEnterTimestamp` and `MainPID` | **PASS** -- the game service was never touched, per the ownership fence |

## Ownership

This work owns `valheim-peermeter-nft.sh`, `valheim-peermeter-read.py`, `valheim-relay-check.py`,
and the new `inet valheim_peermeter` table. It does not modify `valheim-meter-nft.sh`,
`valheim-egress-probe.py`, `inet valheim_meter`, `valheim.service`, `valheim-bot`,
`valheim-status`, or `valheim-restartd`.
