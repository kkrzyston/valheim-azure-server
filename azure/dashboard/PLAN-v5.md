# Dashboard v5 plan: achievements page, site navigation, and a player-triggered restart

Read `PLAN-v3.md` then `PLAN-v4.md` first for the environment, SSH, deploy conventions and the
existing data contract. Everything there still holds **except the restart rule, amended below.**

## The amended hard rule

PLAN-v4 said: *NEVER stop/restart/touch `valheim.service`.* That rule is now carved out in
exactly one place and nowhere else:

> **`valheim-restart-exec.py`, running as root from `valheim-restart-exec.timer`, is the only
> thing in this system permitted to stop or start `valheim.service`** — and only for a request
> that a Discord role holder approved, under every rail in task R3. The auto-updater's existing
> `systemctl restart` stays as it is.
>
> Nothing else may touch the service. No agent, no script, no bot, no dashboard code path, no
> sudoers rule. `--force` on the auto-updater is still never scheduled. If you are not
> `valheim-restart-exec.py`, the old rule applies to you unchanged.

Do not "fix" the restart feature by removing it because PLAN-v4 forbade restarts. This file is
newer.

## Hard rules for every agent

- **Only edit the files your task owns.** Other agents are editing other files right now. The
  ownership table below is exhaustive; if you need a file you do not own, say so in your report
  instead of editing it.
- **Never touch the live VM.** No `ssh`, no `scp`, no `systemctl`, no deploys. This round is
  local authoring only; the owner deploys. State your verification commands; do not run them
  against the server.
- Never commit or write a real credential. `valheim-alert.env` and `valheim-bot.env` are
  TEMPLATES with every value empty. Real secrets live only in `/etc/` on the VM, mode 0600.
- LF line endings (`.gitattributes` enforces this). Run `python3 -m py_compile` on every Python
  file and `bash -n` on every shell script you touch, before you report done.
- Keep the collector's total runtime under ~3.5 s (`collect_ms` in status.json). Nothing new
  goes on the 60-second path unless the task says so.
- Match the existing voice. The page talks like a saga, warmly and in second person: "The
  hall", "Vikings in the world", "Not recorded yet." No jargon, no dashboard-speak. Server
  scripts are terse and comment *why*, not *what*.
- Preserve the defensive conventions: the page's per-section `safe()` isolation, every section
  degrading to a readable note rather than blanking, atomic `tmp` + `os.replace` for every file
  write, and scripts that never fail their systemd unit on a transient error.
- Report: what you changed and why, the verification commands with their real output, pasted
  excerpts of any new JSON, and anything you could not do or had to assume.

## Two honest limits, to be reflected in copy and comments

1. **The requester cannot be authenticated.** `viking`/`hammerhead` is one shared password,
   identical to the in-game password and printed in the README. The approver may be the same
   human who filed the request. This is *accountable* authorization — one named person owns
   each restart — **not** two-person control. Never describe it as four-eyes, and say plainly
   in the Discord embed that the requester is unauthenticated.
2. **Players in-game see no warning at all.** Vanilla Valheim has no RCON and this server runs
   no mods on purpose. The countdown reaches Discord and the dashboard only. Say so in the UI
   and the README; never imply otherwise.

## File ownership

| Task | Owns | Must not touch |
|---|---|---|
| **R1** bug fixes + shared lock | `azure/valheim-autoupdate.sh`, `azure/dashboard/valheim-offsite-backup.sh`, and **only** the embedded autoupdate copy inside `azure/cloud-init.yaml` | anything else in cloud-init.yaml |
| **R2** bot: de-root + approval | `azure/dashboard/valheim-bot.service`, `azure/dashboard/valheim-bot.py`, `azure/dashboard/valheim-alert.env`, new `azure/dashboard/valheim-bot.env` | every other file |
| **M1** medals web feed | `azure/dashboard/valheim-medals.py`, new `valheim-medals-web.service`, new `valheim-medals-web.timer` | every other file |
| **R3** restartd + executor | new `azure/dashboard/valheim-restartd.py`, `valheim-restart-exec.py`, `valheim-restartd.service`, `valheim-restart-exec.service`, `valheim-restart-exec.timer`, `valheim-restart-exec.path` | every existing file |
| **W1** the page | `azure/dashboard/index.html` | every other file |
| **I1** integration (orchestrator) | `Caddyfile`, `install-dashboard.sh`, `cloud-init.yaml` (users/dirs/units), `security-check.sh`, `README.md`, `valheim-status-collect.py` | the files above |

## Data contract: `/var/www/valheim/medals.json` (task M1 writes, W1 renders)

Replaces today's `{generated, all_time}`, which nothing has ever fetched. `all_time` stays at
the top level for compatibility. Every value under `windows` is the medals engine's own
`finish()` result, so **the page renders the engine's strings and duplicates no medal logic.**

```
{ generated: int, tracking_since: int, cost_ms: int, day_label: "13 September",
  catalog: [ {key, emoji, name, category, verb, howto, windows: [...], live: bool} ],   # all 27
  windows: { day:  {<key>: {names: [...], value: n, display: "...",
                            runners_up: [{name, value, display}]}},
             week: {...}, all: {...} },
  all_time: {<key>: {names, value}} }                                     # unchanged, kept
```

`category` is new and must be added to every `CATALOG` entry: one of
`time`, `death`, `social`, `world`, `fun` — the five groupings that exist today only as source
comments. A medal missing from a window simply is not in that window's object.

## Data contract: `/var/www/valheim/restart-state.json` (task R3 writes, W1 renders)

Mode **0640 root:caddy** — it carries the CSRF token, so it must not be world-readable.
Served by the existing `file_server` behind the `viking` login.

```
{ generated: int,
  csrf: "<base64url>",                  # HMAC(secret, floor(now/300)); page echoes it back
  accepting: bool, why_not: "..."|null, # false => disable the button and show why
  cooldown_until: int|null,
  current: null | { id, state, reason, nickname, created_at,
                    players_at_request: {count, names: [...]},
                    restart_at: int|null,          # absolute epoch deadline, not a duration
                    approver: {display}|null,      # never the raw Discord id
                    message_url: "..."|null },
  recent: [ {id, state, reason, at, approver: {display}|null, downtime_s: int|null} ] }  # <=10
```

## The restart request POST (task R3 serves, W1 sends)

`POST /api/restart/request` — the only write path into this system, ever.

Required by the server, all four, or it is rejected: `Content-Type: application/json`, header
`X-Valheim-Restart: 1`, `Sec-Fetch-Site` absent or `same-origin`, and a current `csrf` value.
Body, strict — unknown keys rejected, 2 KB cap enforced by Caddy:

```
{ csrf: "...", reason: "<enum>", nickname: "<=24 chars, [A-Za-z0-9 _.-] only>", ack_players: bool }
```

`reason` is an **enum, never free text**: `not_responding`, `cannot_join`, `lag`,
`stuck_after_update`, `other`. `ack_players` must be `true` when anyone is online.

Responses: `202` `{id}` accepted; `409` `{error, why}` when a request is in flight or the
cooldown is active; `429` rate limited; `400` malformed; `403` a CSRF or header check failed.
**The client never influences a filename** — the server generates `uuid4()` itself.

## Countdown

No wait when the server is empty (still posts a heads-up). **300 s when anyone is online**,
with milestones at T−300/120/60/30/10. It is a persisted absolute deadline, never a sleep, so a
killed executor resumes correctly. The webhook posts and edits it — not the bot — so a
crash-looping bot cannot stall a countdown already in flight.

## Rate limits

At least 30 min between executed restarts, at most 3 per rolling 24 h, exactly one request in
flight. `valheim.service` has `StartLimitBurst=5` / `StartLimitIntervalSec=600`; exceeding that
parks the unit in `failed` until `systemctl reset-failed`, and the auto-updater then sees
`is-active` false and deliberately does nothing — an indefinite outage. Stay far below it.

## The spool: one writer and one reader per directory

```
browser -> Caddy (viking auth, 2KB) -> restartd (no network at all) -> requests/
requests/ -> executor validates + sanitizes -> inbox/ -> bot posts, human approves -> verdicts/
verdicts/ -> executor (checks st_uid == bot) -> lock, backup, stop, confirm save, start, A2S
```

| Path | Owner:Group | Mode | Writer | Reader |
|---|---|---|---|---|
| `/var/lib/valheim-restart/` | `root:root` | `0755` | root | traverse |
| `.../requests/` | `root:valheim-restartd` | `1730` | restartd, create only | root |
| `.../inbox/` | `root:valheim-bot` | `0750` | root | bot |
| `.../verdicts/` | `root:valheim-bot` | `1730` | bot, create only | root |
| `.../archive/`, `.../quarantine/` | `root:root` | `0700` | root | root |
| `.../gate.json` | `root:root` | `0644` | root | restartd |
| `.../secret.csrf` | `root:valheim-restartd` | `0640` | root, once | restartd |
| `/run/valheim-restartd/http.sock` | `valheim-restartd:caddy` | `0660` | restartd | caddy only |
| `/var/www/valheim/restart-state.json` | `root:caddy` | `0640` | root | caddy only |
| `/var/lib/valheim-status/restart.json` | `root:root` | `0644` | root | collector |
| `/var/log/valheim-restart.jsonl` | `root:adm` | `0640` | root | root/adm |
| `/var/lock/valheim-world.lock` | `root:root` | `0644` | the four root scripts | — |

`1730` is `rwx-wx--T`: the writer can create a file by name but cannot list or read the
directory. restartd never needs to, so it does not get to. The sticky bit activates the kernel's
`fs.protected_symlinks` guard.

**Every executor read of a spool file**: `os.open(..., O_RDONLY|O_NOFOLLOW)` with `dir_fd=`
against a directory fd opened once, then `fstat` asserting `S_ISREG`, the expected `st_uid`,
`st_nlink == 1`, and a bounded `st_size`. Anything else goes to `quarantine/` and is never
parsed.

## Request state machine (the executor is the only writer after creation)

`pending_validation` -> `awaiting_approval` -> `approved` -> `counting_down` ->
`waiting_for_lock` -> `backing_up` -> `stopping` -> `stopped_verified` -> `starting` ->
`verifying` -> `succeeded`

Terminal: `rejected_invalid`, `failed_precheck`, `denied`, `expired_unapproved` (15 min),
`expired_approval` (5 min), `aborted`, `aborted_lock_timeout`, `failed_needs_admin`.

`failed_needs_admin` alerts loudly and **never touches systemd again for that request.**
Resume-safe entry points: `stopping`, `stopped_verified`, `starting`, `verifying`.
`attempts >= 3` becomes `failed_needs_admin`. Terminal requests move to `archive/` after 7 days.

## Shared lock

`/var/lock/valheim-world.lock`, `root:root 0644` — all holders are root, so it adds no
permission boundary. One line at the top of each shell script:

```bash
[ "${VALHEIM_WORLD_LOCKED:-}" ] || exec env VALHEIM_WORLD_LOCKED=1 \
    flock -w 60 /var/lock/valheim-world.lock "$0" "$@"
```

Wait budgets differ and the difference is the point:
- `valheim-autoupdate.sh`: `-w 60`, and on failure **log and `exit 0`** — it retries free in 30 min.
- `valheim-offsite-backup.sh`: `-w 900` — it must not skip the nightly backup.
- executor: **non-blocking**, state `waiting_for_lock`, retry next tick, 10 min budget then abort.

## Page routes (task W1)

Six hash routes, one fetch cycle, renderers run only for the visible view — the map and chart
canvases are the expensive part of every 30 s tick and mostly will not be visible.

| Route | Holds |
|---|---|
| `#/` **Now** | headline, who is online with session length and ping, join info, version and update state, short activity feed, your ping, **and the restart affordance when the server is down** |
| `#/world` | map, bosses, what has been built, raids, world facts |
| `#/vikings` | roster, 7-day timeline, together, heatmap, calendars |
| `#/trends` | 24 h / 7 d player chart, per-player ping chart |
| `#/hall` | Medals / Hall of Fame tabs |
| `#/server` | machine, updates, restarts, next events, settings, off-site, snapshots, notifications, news, **the Restart control** |

Unknown hash falls back to Now. Deep links work. Nav scrolls horizontally under 520 px, never a
hamburger. `aria-current="page"` on the active link; tabs reuse the existing `aria-pressed`
pattern from the 24 h / 7 d range buttons. Document title and the staleness footer stay global.
