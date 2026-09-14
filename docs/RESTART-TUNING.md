# Restart-executor tuning reference

`valheim-restartd.py` and `valheim-restart-exec.py` (the Discord restart-approval pipeline —
see [`DISCORD-RESTART-APPROVAL.md`](DISCORD-RESTART-APPROVAL.md) for the feature itself) expose
roughly 47 environment-variable overrides between them. Every one of them has a working default
baked into the script, and the overwhelming majority of deployments never need to touch any of
them — they exist so paths can be relocated, timing can be rehearsed, and a handful of internal
ceilings can be adjusted without editing code. This file documents all of them so a reader
doesn't have to grep the source to find out they exist.

**Where they go.** Both scripts read plain `KEY=value` lines from an `EnvironmentFile`, loaded
by systemd (PID 1) before the unit's own sandboxing applies:

- `valheim-restartd.service` loads `EnvironmentFile=-/etc/valheim-alert.env` — so every
  `RESTARTD_*` override belongs there, **not** in `/etc/valheim-bot.env` (that file is
  Hermóðr's own Discord bot config and is never read by either restart script).
- `valheim-restart-exec.service` loads **two** files: `EnvironmentFile=-/etc/valheim-alert.env`
  and `EnvironmentFile=-/etc/valheim-server.env` — so every `VR_*` override, plus
  `DISCORD_WEBHOOK_URL`/`DASHBOARD_URL`, belongs in the former, and `SERVER_NAME`/`WORLD_NAME`
  in the latter (same as every other script on the box; see `.env.example`).

The leading `-` in `EnvironmentFile=-...` means "don't fail to start if the file is missing" —
both scripts run fine with none of this set, using their compiled-in defaults.

`azure/dashboard/valheim-alert.env` is the tracked template for `/etc/valheim-alert.env`, but it
intentionally does not list any of the ~47 variables below (see `.env.example`'s own "Advanced"
section for why: they're rarely-needed overrides, not required configuration). Add the specific
`RESTARTD_*`/`VR_*` lines you need directly to `/etc/valheim-alert.env` on the VM.

**Restart behavior after a change differs between the two units:**

- `valheim-restartd.service` is a long-running daemon (`Type=simple`). A change to
  `/etc/valheim-alert.env` has no effect until you run `sudo systemctl restart
  valheim-restartd`.
- `valheim-restart-exec.service` is `Type=oneshot`, invoked fresh every 15 s by
  `valheim-restart-exec.timer` (and on-demand by `valheim-restart-exec.path`). systemd reads
  `EnvironmentFile` again for every new invocation, so a change here is picked up automatically
  on the *next* tick — no explicit restart needed (though `sudo systemctl restart
  valheim-restart-exec` is harmless if you want it to take effect immediately).

**A handful of these are genuinely dangerous to change** — they can risk the world save, bypass
a safety check, or shorten the window that protects connected players. Each is called out
explicitly in its row below; read those before touching them.

---

## `valheim-restart-exec.py` (`VR_*`)

### Spool & state paths

All default to a subpath of `VR_LIB` unless set individually. `VR_REQUESTS`, `VR_SECRET`, and
`VR_GATE` are **shared state with `valheim-restartd.py`** (see that script's `RESTARTD_REQUESTS`,
`RESTARTD_SECRET`, `RESTARTD_GATE` below) — if you relocate one side without relocating the
other to match, the two processes silently stop seeing each other's files and the whole pipeline
stalls with no error.

| Variable | Default | Units | What it controls |
|---|---|---|---|
| `VR_LIB` | `/var/lib/valheim-restart` | path | Base directory; the default parent for every other spool/state path below that isn't set individually. |
| `VR_REQUESTS` | `{VR_LIB}/requests` | path | Where `restartd.py` writes new restart request files and this script reads/validates them (ownership checked against `VR_RESTARTD_UID`). **Must match `RESTARTD_REQUESTS`** or requests never reach the executor. |
| `VR_INBOX` | `{VR_LIB}/inbox` | path | Sanitized copy of a validated request, written for the Discord bot to read and post the approval message from. |
| `VR_VERDICTS` | `{VR_LIB}/verdicts` | path | Where the Discord bot writes approve/deny verdicts (ownership checked against `VR_BOT_UID`). |
| `VR_ARCHIVE` | `{VR_LIB}/archive` | path | Terminal request files older than `VR_ARCHIVE_AFTER` are moved here instead of deleted (kept for audit). |
| `VR_QUARANTINE` | `{VR_LIB}/quarantine` | path | Any spool file that fails ownership or shape validation is moved here rather than processed — a forensic holding pen, never auto-cleaned by this script. |
| `VR_HISTORY` | `{VR_LIB}/history.json` | path | Rolling record of executed-restart timestamps, used to enforce `VR_MIN_BETWEEN` and `VR_MAX_PER_DAY`. |
| `VR_GATE` | `{VR_LIB}/gate.json` | path | Heartbeat file this script writes every tick. `restartd.py` reads it (as `RESTARTD_GATE`) to refuse new requests if the executor looks dead. **Must match `RESTARTD_GATE`.** |
| `VR_SECRET` | `{VR_LIB}/secret.csrf` | path | The shared HMAC secret backing the CSRF token `restartd.py` validates on every POST; created here (mode 0640) if missing. **Must match `RESTARTD_SECRET`, and must stay unreadable to anyone but root/`valheim-restartd`** — relocating it somewhere group- or world-readable would let anyone forge a valid restart request. **Dangerous if relocated carelessly.** |
| `VR_WEB_STATE` | `/var/www/valheim/restart-state.json` | path | Public JSON the dashboard page polls for CSRF token, current request state, and countdown. Written 0640 `root:VR_CADDY_GROUP`. |
| `VR_STATUS` | `/var/www/valheim/status.json` | path | Where this script reads live player/server status (written once a minute by `valheim-status-collect.py`) to decide who's online before acting. Must match the collector's actual output path or every restart looks like it has stale status. |
| `VR_COLLECTOR_OUT` | `/var/lib/valheim-status/restart.json` | path | Where this script writes its own restart-outcome facts for the status collector to fold into the dashboard. |
| `VR_LOG` | `/var/log/valheim-restart.jsonl` | path | Append-only structured audit log: every state transition, the requester's claim, the approver's identity, player counts, backup path/sha256, and total downtime. |

### Safety-critical identity & unit paths

| Variable | Default | Units | What it controls |
|---|---|---|---|
| `VR_LOCK` | `/var/lock/valheim-world.lock` | path | Shared advisory lock file serializing this script against other jobs that touch the world (see `valheim-offsite-backup.sh`). **Dangerous if changed** without updating every other script that locks the same path — two "world jobs" (e.g. an off-site backup and a restart) running concurrently is exactly the corruption scenario the lock exists to prevent. |
| `VR_BACKUP_SH` | `/home/valheim/backup.sh` | path | The script this executor runs before stopping the world to take the safety snapshot the whole restart flow relies on. **Dangerous if repointed** at anything that isn't a real, working backup script — it removes the only safety net between a bad restart and a lost or corrupted save. |
| `VR_BACKUP_DIR` | `/home/valheim/backups` | path | Directory this script diffs (before vs. after running `VR_BACKUP_SH`) to confirm a *new* `.tgz` actually appeared. **Dangerous if it doesn't match where `VR_BACKUP_SH` actually writes** — the "backup succeeded" check would pass against stale or unrelated files while producing no real backup. |
| `VR_MIGRATED_MARKER` | `/home/valheim/data/.world-migrated` | path | The file whose *presence* is what lets `valheim.service` start at all (`ConditionPathExists=`). Per this script's own header comment, the marker is checked before anything is stopped and is **never re-created here on purpose** — it exists to guard against silently starting a brand-new world on top of a real one. **Extremely dangerous to repoint or to make this script (or anything else) auto-create it**: a missing marker makes `systemctl start` exit 0 while the unit goes inactive — a clean-looking shutdown that is actually a permanent, silently "successful" outage. |
| `VR_UNIT` | `valheim.service` | systemd unit name | Which unit this script stops, starts, and `reset-failed`s. **Dangerous if wrong** — every safety check in this script (marker check, sibling-unit check, start-limit avoidance) is keyed to this name; pointing it at the wrong unit either restarts nothing real or restarts something never verified. |
| `VR_SIBLING_UNITS` | `valheim-update.service,valheim-offsite.service` | comma-separated unit names | Precheck: refuses to restart while any listed unit is active. **Dangerous if cleared or narrowed** — it's the guard against restarting mid-update or mid-off-site-backup, both concurrent-world-access scenarios this system exists to avoid. |
| `VR_RESTARTD_USER` | `valheim-restartd` | OS username | The account whose UID incoming request files in `VR_REQUESTS` must be owned by, or they're quarantined. |
| `VR_BOT_USER` | `valheim-bot` | OS username | The account whose UID incoming verdict files in `VR_VERDICTS` must be owned by, or they're quarantined. |
| `VR_RESTARTD_UID` | resolved from `VR_RESTARTD_USER` via `pwd.getpwnam` (0 if the `pwd` module is unavailable, e.g. under the Windows test harness; -1 if the name can't be resolved, which quarantines everything) | integer UID (forced override) | Escapes the real `pwd` lookup for the request-spool ownership check — the actual security boundary between the internet-facing request path and the privileged executor. Intended for tests/non-POSIX environments only. **Dangerous in production**: set it wrong and you either quarantine every legitimate request or accept requests owned by an unintended UID. |
| `VR_BOT_UID` | same mechanism, for `VR_BOT_USER` | integer UID (forced override) | Same as `VR_RESTARTD_UID` above, but for verdict-file ownership. Same production danger. |
| `VR_CADDY_GROUP` | `caddy` | OS group name | Group `VR_WEB_STATE` (which carries the CSRF token) is written with, mode 0640. **Dangerous if broadened** — it would expose the CSRF token/state file to more local accounts than intended. |
| `VR_A2S_HOST` | `127.0.0.1` | hostname/IP | Host queried over the Steam A2S protocol to confirm the server is actually up and answering after a start. |
| `VR_A2S_PORT` | `2457` | port (int) | Port for the A2S query above. |
| `VR_DISCORD_GUILD_ID` | `""` (empty) | string (Discord snowflake) | Used only to build a clickable `discord.com/channels/...` deep link back to the approval message; cosmetic, not a security check (compare to Hermóðr's own `HERMODR_GUILD_ID`, which *is* used for authorization). |
| `VR_SYSLOG_TAG` | `valheim-restart` | string | The `logger -t <tag>` tag used when mirroring JSONL log lines into the systemd journal. Cosmetic. |
| `VR_SYSLOG` | `"1"` | boolean-ish (exact string `"1"` = on, anything else = off) | Whether log lines are also mirrored into the journal via `logger`. The JSONL file at `VR_LOG` is written either way. |

### Timing & windows

| Variable | Default | Units | What it controls |
|---|---|---|---|
| `VR_COUNTDOWN` | `300` | seconds | How long connected players are warned before a restart proceeds (visible milestones at 300/120/60/30/10 s — the milestone list itself is not independently configurable). **This is the window that protects players** named in this doc's intro — shortening it meaningfully cuts the warning time people online actually get before they're disconnected. |
| `VR_APPROVAL_WINDOW` | `900` | seconds | How long a pending request waits for a Discord approver before it expires (`awaiting_approval` → `expired_unapproved`). |
| `VR_APPROVED_WINDOW` | `300` | seconds | How long an approved request has to actually start executing before it expires (`approved` → `expired_approval`). |
| `VR_LOCK_BUDGET` | `600` | seconds | How long to wait for `VR_LOCK` to free up before giving up (`waiting_for_lock` → `aborted_lock_timeout`). |
| `VR_MIN_BETWEEN` | `1800` | seconds | Minimum gap enforced between two executed restarts. **Lowering this weakens a real safety rail** against rapid repeated restarts, though it is a soft check, not a hard block on the underlying systemd unit. |
| `VR_MAX_PER_DAY` | `3` | count | Max restarts allowed in a rolling 24 h window. **Dangerous if raised substantially** — it exists specifically to stay well under systemd's own `StartLimitBurst=5`/`StartLimitIntervalSec=600` on `valheim.service`; get close to that ceiling and systemd itself can park the unit in `failed`, which per this script's header requires a human `systemctl reset-failed` to clear (the auto-updater deliberately does not self-heal from that state). |
| `VR_STATUS_MAX_AGE` | `180` | seconds | How stale the dashboard's `status.json` (player-count evidence) is allowed to be before this script refuses to rely on it. |
| `VR_START_BUDGET` | `900` | seconds | How long to wait for the unit to verify started — sized generously because `ExecStartPre` runs `steamcmd +app_update` on every start. Shortening it risks treating a legitimately slow Steam update as a failed start. |
| `VR_STOP_WAIT` | `240` | seconds | How long to wait for a clean stop; sized as the unit's real `TimeoutStopSec=180` plus slack. **Dangerous if set below 180** (the real systemd timeout) — per this script's header, a stop that times out means the process group may have been SIGKILLed mid-write to the save file, so the script deliberately refuses to start again rather than risk loading (and then overwriting the backup with) a torn save. Setting `VR_STOP_WAIT` too low makes that safe abort trigger on stops that were actually still in progress. |
| `VR_MAX_ATTEMPTS` | `3` | count | How many times one request may re-enter the stop/start sequence before it's marked `failed_needs_admin` and left alone. **Dangerous if raised** — each attempt can stop and start `valheim.service`, so raising this raises how many restart cycles one stuck request can drive, again pushing toward systemd's own start-rate limit (see `VR_MAX_PER_DAY`). |
| `VR_ARCHIVE_AFTER` | `604800` (7 × 86400) | seconds | Age after which a terminal request file moves from the live spool into `VR_ARCHIVE`. Housekeeping only. |
| `VR_TIME_SHIFT` | `0` | seconds (float), added to wall clock | Exists purely so tests can rehearse a countdown without sleeping — every deadline in this script is absolute epoch time, so shifting the clock is enough. **Must stay `0` in production**; any other value desynchronizes every timing decision above (approval windows, countdowns, rate limiting) from real time. |

### Safety limits & debug

| Variable | Default | Units | What it controls |
|---|---|---|---|
| `VR_MAX_SPOOL_BYTES` | `8192` | bytes | Read ceiling for small spool files (verdicts, etc.). Exists specifically so an unbounded read can't become a memory-exhaustion bug. **Raise with caution** — that's the risk it closes off. |
| `VR_MAX_REQUEST_BYTES` | `32768` | bytes | Same idea, sized larger because request files accumulate phase history, backup facts, and evidence over their lifetime. Same caution as above. |
| `VR_DRY_RUN` | `"0"` | boolean-ish (exact string `"1"` = on) | Forces dry-run mode (every check and Discord post runs; `systemctl` calls are only printed) even without `--dry-run` on the command line. `valheim-restart-exec.service` ships with `Environment=VR_DRY_RUN=1` set directly in the unit, by design, as a "rehearse for a day before going live" gate — comment that line out (or `systemctl edit` it away) to arm the executor for real. **Common footgun**: if this is left at `1` in a supposedly-live deployment, the whole pipeline silently never restarts anything — not a corruption risk, but worth checking first if "approved" restarts never seem to happen. |

### Shared with other scripts (already documented in `.env.example`)

These aren't restart-specific, but this script reads them too, from the same `/etc/valheim-alert.env` / `/etc/valheim-server.env` files as everything else:

| Variable | Default | Units | What it controls |
|---|---|---|---|
| `DISCORD_WEBHOOK_URL` | `""` | URL | Webhook this script posts approval/status/outcome messages to (shared with `valheim-alert.py`). |
| `DASHBOARD_URL` | `""` | URL | Link included in Discord posts (shared with `valheim-alert.py`). |
| `SERVER_NAME` | `""` → `"Valheim Server"` | string | Display name used in Discord messages (shared, from `/etc/valheim-server.env`). |
| `WORLD_NAME` | `""` → `"Dedicated"` | string | Display name used in Discord messages (shared, from `/etc/valheim-server.env`). |

---

## `valheim-restartd.py` (`RESTARTD_*`)

| Variable | Default | Units | What it controls |
|---|---|---|---|
| `RESTARTD_LIB` | `/var/lib/valheim-restart` | path | Base directory; default parent for `RESTARTD_REQUESTS`/`RESTARTD_SECRET`/`RESTARTD_GATE` below if those aren't set individually. **Must stay in sync with `valheim-restart-exec.py`'s `VR_LIB`** (or the three paths below individually) — the two processes otherwise look at different spools and the pipeline silently stalls with no error on either side. |
| `RESTARTD_SOCKET` | `/run/valheim-restartd/http.sock` | path | The unix socket this daemon listens on for Caddy's reverse-proxied POSTs. Caddy's own config (`Caddyfile`) must point at the same path — move one without the other and every restart request gets a connection error. |
| `RESTARTD_SOCKET_GROUP` | `caddy` | OS group name | Group the socket is `chgrp`'d to at startup, giving it its final 0660 `valheim-restartd:caddy` permissions. **Dangerous if broadened** — this group membership is the entire reason only Caddy (and not, say, the `valheim` account running the internet-facing game process) can reach this daemon; widening it hands socket access to more local accounts than intended. |
| `RESTARTD_REQUESTS` | `{RESTARTD_LIB}/requests` | path | Where accepted, validated requests are written (directory mode 1730: create-by-name only, no listing, no reading back). **Must match `VR_REQUESTS`** on the executor side. |
| `RESTARTD_SECRET` | `{RESTARTD_LIB}/secret.csrf` | path | Where the shared CSRF HMAC secret is read from (this process only ever reads it; the executor creates and owns it). **Must match `VR_SECRET`.** If the two disagree, every request fails CSRF validation — fails closed, so not a corruption risk, but every legitimate request will also fail. |
| `RESTARTD_GATE` | `{RESTARTD_LIB}/gate.json` | path | Heartbeat file written by the executor; this daemon checks its age (a hardcoded 300 s ceiling, not itself overridable) and refuses new requests if the executor looks dead. **Must match `VR_GATE`.** |
| `RUNTIME_DIRECTORY` | `dirname(RESTARTD_SOCKET)` or `"."` | path | Normally supplied automatically by systemd (`RuntimeDirectory=valheim-restartd` in the unit) — not meant to be hand-set. Used only to place `accepted.json`, the scratch file backing the global rate limit (1/minute, 6/hour, shared across all requesters). This daemon runs under `ProtectSystem=strict` with `ReadWritePaths=` limited to its requests directory, so pointing this somewhere it can't actually write to would silently disable the rate limiter rather than error — **moderate risk**: it removes an abuse/DoS guard, not a world-safety one. |

---

*Generated from a full grep of both scripts for `os.environ.get`/`os.getenv`/`environ[` — see
`git log -- docs/RESTART-TUNING.md` if this drifts from the source; the source is always the
source of truth.*
