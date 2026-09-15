# Operations runbook

This is the original operator's day-to-day runbook for the deployment described in the
root [`README.md`](../README.md). It is kept here because it documents real operational
detail — architecture, Azure resource layout, backup mechanics, the auto-updater, Tailscale
access, the Hermóðr bot, and a running changelog — that a new deployer will want once the
server is up.

**If this is your first time here, read the root README first.** It explains what the
project is, the architecture, and walks through a fresh deployment end to end. This
document assumes that's done and covers operating an already-running server.

Every hostname, IP address, password, storage-account name, Steam/Discord/Azure identifier
and email address below is a placeholder in `<ANGLE_BRACKETS>` or an obviously-fake example
value — this runbook was written against one real deployment and every identifier from that
deployment has been scrubbed before publishing. Substitute your own values as you go.

## Connect

| | |
|---|---|
| Address | `<PUBLIC_IP>:2456` (static, survives restarts) |
| Password | `CHANGEME_SERVER_PASSWORD` |
| Server name | `<SERVER_NAME>` (shows in the Community list) |
| Crossplay | OFF by default (Steam PC only). No join codes. |

In Valheim: Start Game -> pick character -> Join Game -> Join IP -> `<PUBLIC_IP>:2456` -> password.

## Dashboard

| | |
|---|---|
| URL | `https://<DASHBOARD_HOST>` |
| Login | user `viking`, password `<DASHBOARD_VIEWER_PASSWORD>` (set separately from the game password — use a different value for each) |
| World snapshots | `https://<DASHBOARD_HOST>/snapshots/` — a listing of the `.tgz` archives in `/home/valheim/backups`. Needs the separate user `owner` (password chosen when the installer was run; the `viking` login is refused here, and `owner` opens nothing else) |

The dashboard title and color theme in `dashboard/index.html` are cosmetic — the original
deployment titled it after its own Discord community; change `<title>` and the CSS custom
properties at the top of the file to suit yours.

**Player ping** is measured from the VM: the collector sniffs a few seconds of traffic on
UDP 2456 to learn each connected player's address (never shown or stored beyond the
pairing), then ICMP-pings it. Players whose routers drop ping show "no reply". The
dedicated server itself does not expose in-game ping; a server-side mod (BepInEx) would be
the only way to get it exactly, and mods would break the hands-off auto-update, so this was
not done.

**Discord alerts** (`/usr/local/sbin/valheim-alert.py`, runs after every collector pass)
post to a webhook set in `/etc/valheim-alert.env` (`DISCORD_WEBHOOK_URL`): server down (2
consecutive failed checks) and back up; update waiting / installing / installed;
crash-restart by systemd; memory > 92 %, swap > 1.5 GB, disk > 90 %, world save > 5 s, CPU >
95 % for 5 min (each once, with a clear threshold). `ALERT_JOINS=1` also announces every
arrival and departure; set to 0 to quiet it. By design the bot never pings `@everyone`;
every post is silent. Setup: Discord channel > Edit channel > Integrations > Webhooks > New
Webhook > Copy URL, then on the VM `sudo nano /etc/valheim-alert.env`, paste it after
`DISCORD_WEBHOOK_URL=`. Log: `/var/log/valheim-alert.log`.

**Live status board:** the same webhook also carries a **live status board** — one message
that `valheim-alert.py` keeps editing every minute (hall open/closed, who's online with
session length and ping, in-game day, last raid, bosses defeated, next Steam check, 30-day
uptime; never `@everyone`; if the message is deleted it quietly creates a new one). Its id
lives in `/var/lib/valheim-status/alerts.json` under `board_message_id`. **Pin that message
in the channel** so it stays at the top — webhooks cannot pin it themselves. Also posted,
each once with a matching recovery/clear post where it makes sense: **raid** posts (who was
online), a **boss falling**, **milestones** (a Viking's hours/deaths/sessions crossing a
threshold, and a new name joining the roster — thresholds are seeded from whatever totals
already existed the first time the script saw them, so upgrading never fires a burst of old
milestones), **Azure scheduled-maintenance** notices, and an **off-site backup is behind**
warning (36 h+ since the last successful copy). A separate **weekly digest**
(`/usr/local/sbin/valheim-digest.py`, `valheim-digest.timer`, Sundays 18:00 local) posts one
summary embed of the past 7 days: hours logged in total and per Viking, deaths, raids,
bosses defeated, most people online at once, uptime, and updates installed; run it by hand
with `--dry-run` to print the embed JSON instead of posting. Every new post type should be
developed and verified against a local catcher on the VM (a plain `python3 http.server`-style
handler on `127.0.0.1:8765`) before ever touching the real webhook.

**Discord medals** (`/usr/local/sbin/valheim-medals.py`) compute a catalogue of roughly 27
"records" from the same on-disk data (the append-only event log, the per-minute samples,
`status.json`, and `valheim-alert.py`'s `boss_log`), grouped time & attendance, deaths &
survival, social & co-op, world & exploration, and a for-fun set — see
`dashboard/valheim-medals.py`'s `CATALOG` table for the full list, blurbs, and which of
day/week/all-time window each applies to. Delivered four ways, all from one shared
`compute()` so they can never disagree: a curated subset can interrupt the channel live when
an all-time record breaks (a third `ExecStart=` on `valheim-status.service`, so it runs
every minute right after the collector and the alert script — capped at 2 posts per run and
a 6 h cooldown per medal so a marathon session doesn't spam the channel); a daily digest post
at 09:00 local (`valheim-medals-daily.timer`) is skipped entirely on a quiet day; the same
table contributes a second embed to the Sunday `valheim-digest.py` message (imported
defensively, so a missing or broken medals script only costs that one embed, never the
weekly ledger); and a pinned Hall of Fame board is upserted right after the daily post, the
same edit-in-place pattern the live status board uses. Every medal requires at least two
eligible players and a sane minimum (30 min played, 2 deaths, etc.) before it is awarded —
one person playing alone gets no medals, and a day nobody played gets no post at all.
All-time medals are phrased relative to `players.tracking_since` rather than "all time",
since a fresh deployment is only ever a few days old at first. State lives in its own file,
`/var/lib/valheim-status/medals.json` — it never touches `alerts.json`, which
`valheim-alert.py` rewrites wholesale every minute. Run any mode by hand with `--dry-run` to
print the embed JSON instead of posting (`--daily`, `--weekly`, `--board`,
`--check-records`, `--seed`); `--seed` (run once at deploy) records every existing all-time
holder without posting, so turning this on for the first time doesn't dump the whole
catalogue into the channel at once. Log: `/var/log/valheim-medals.log`.

**Hermóðr (Discord Q&A bot)** (`/usr/local/sbin/valheim-bot.py`, `valheim-bot.service`) is a
long-running Discord gateway bot (discord.py, its own venv at `/opt/hermodr/venv`) that
answers questions in one Discord channel about the server, using the same on-disk data as
everything else above — `status.json`, the event log, and `valheim-medals.py`'s catalogue
(imported live, never a second copy of the logic). It responds only when @-mentioned or when
someone replies to one of its own messages, and only in the channel set by
`HERMODR_CHANNEL_ID` (or, as a fallback, resolved once at startup by exact name from
`HERMODR_CHANNEL_NAME`). Every question and every message it reads first passes a **channel
allowlist check** — before any parsing, before any AI call, before anything is logged — so
it is structurally incapable of answering (or leaking data) anywhere else; DMs are ignored
entirely. It can answer: who's online, in-game day, bosses defeated, uptime, last raid;
per-Viking hours/sessions/deaths/last-seen/longest-session/best-streak; the current holder of
all medals for day/week/all-time, and how each one is calculated (sourced live from
`valheim-medals.py`'s `CATALOG`, never a hand-copied list, so the explanations can't drift
from the real logic); world exploration %, structures built, tombstones, tamed animals; and
the tracking-start date, so it never claims "all-time" means longer ago than that. It has
**no tools** and cannot take any action — its only effect is the text reply it posts back to
the same channel, which is always sent with `allowed_mentions` set to none (so pings are
inert at the Discord API level) with a second-layer strip of any literal `@everyone`/`@here`
the model types anyway, capped at roughly 500 model tokens and truncated to Discord's
2000-char limit on a word boundary. A user's own message text is never folded into the
system prompt — it goes in as a separate untrusted `user` message, with the system prompt
stating plainly that any instructions embedded in it must be ignored. Per-user rate limit
(in-memory, resets on restart): 1 question per 10 s, 20 per hour; going over gets one brief
reply, then silence. The model call itself uses the VM's system-assigned managed identity
against an Azure AI Foundry endpoint (`HERMODR_AI_ENDPOINT`) — **no API key stored
anywhere**; to swap the model, edit only `HERMODR_AI_MODEL` in `/etc/valheim-alert.env` and
restart the service. Setup: create a Discord application + bot user at
<https://discord.com/developers/applications>, enable the **Message Content** privileged
intent, invite it to the server with the `bot` scope and `Send Messages`/`Read Message
History` permissions, then on the VM set `DISCORD_BOT_TOKEN` and `HERMODR_CHANNEL_ID` (or
`HERMODR_CHANNEL_NAME`) in `/etc/valheim-alert.env` and `sudo systemctl enable --now
valheim-bot`. The installer (`install-dashboard.sh`) creates the venv and installs the
script and unit but deliberately does **not** enable or start it, since a fresh deploy has
no token yet. Test locally with no bot token and no VM: `python3 valheim-bot.py --selftest`
builds and prints the context (no Discord, no AI call); `python3 valheim-bot.py --ask
"question"` builds the context and calls the AI endpoint once (only works on the VM, where
managed identity exists; fails with a clear message anywhere else). Log:
`/var/log/hermodr.log` (the bot token is never written to it, redacted defensively even from
tracebacks).

How it works on the VM: `valheim-status.timer` runs
`/usr/local/sbin/valheim-status-collect.py` every minute (reads the service journal
incrementally, queries the Steam port 2457, samples `/proc`) and writes
`/var/www/valheim/status.json` + `history.json`; Caddy serves `/var/www/valheim` with basic
auth and an automatic Let's Encrypt certificate, and serves `/home/valheim/backups` at
`/snapshots/` (directory listing) behind the second `owner` login. Caddy runs as user
`caddy`; the installer grants it an ACL for traversal of `/home/valheim` and read of
`/home/valheim/backups` only (`setfacl -m u:caddy:x /home/valheim; setfacl -m
u:caddy:rx /home/valheim/backups`), so `/home/valheim/data` stays private. State and 30 days
of samples/events live in `/var/lib/valheim-status/`. Cost: about 0.1 s CPU per minute, no
meaningful extra cloud charge.

| Task | Command (on the VM) |
|---|---|
| Collector health | `systemctl status valheim-status.timer valheim-status.service` |
| Run collector by hand | `sudo systemctl start valheim-status.service` |
| Egress probe health | `systemctl status valheim-egress`; `sudo valheim-meter-nft.sh show` — the counters climb while players are online and stand still when nobody is. The probe logs a heartbeat hourly, so if the journal is silent it is not running |
| Rebuild the meter table | `sudo valheim-meter-nft.sh ensure` (creates it only if missing or incomplete; `install` forces a rebuild and resets the counters) |
| One live egress sample | `sudo valheim-egress-probe.py --once` |
| Read the egress data | `sudo valheim-egress-report.py` (or `--days 7`) |
| Web server health | `systemctl status caddy`; `sudo journalctl -u caddy -n 50` |
| Test the Discord webhook | `sudo rm -f /var/lib/valheim-status/alerts.json` then wait a minute (only real conditions post; use a temporary bad status to test) or `curl -H 'Content-Type: application/json' -d '{"content":"test"}' "$URL"` |
| Change either password | `cd /home/azureuser/dashboard && sudo bash install-dashboard.sh <viewer-pw> <owner-pw>` (both are required every time; pass the unchanged one again). The installer also re-installs `index.html`, the collector and the alert script from that folder, so `sudo cp` the live copies (`/var/www/valheim/index.html`, `/usr/local/sbin/valheim-status-collect.py`, `/usr/local/sbin/valheim-alert.py`) into it first if they are newer than the folder. It never overwrites `/etc/valheim-alert.env`. |
| Change only the Caddy config after editing `dashboard/Caddyfile` | `scp` it up, then run the installer as above; it does `caddy validate` and `systemctl reload caddy` (no restart, no game-server impact) |
| Download a snapshot from this PC | `curl -u owner -O https://<DASHBOARD_HOST>/snapshots/<name>.tgz` (prompts for the owner password) or open `/snapshots/` in a browser |
| Redeploy the page after editing `dashboard/index.html` locally | `scp` it to `/home/azureuser/dashboard/` then `sudo install -m 0644 /home/azureuser/dashboard/index.html /var/www/valheim/index.html` |

Files: `dashboard/` in this folder (`index.html`, `valheim-status-collect.py`,
`valheim-alert.py`, `valheim-alert.env` template, the two systemd units
`valheim-status.service` + `valheim-status.timer`, `Caddyfile` with the `__HASH__` /
`__OWNER_HASH__` placeholders for the `viking` and `owner` logins, `valheim-digest.py`/
`.service`/`.timer`, `valheim-medals.py`/`valheim-medals-daily.service`/`.timer`,
`valheim-bot.py`/`valheim-bot.service` (Hermóðr, the Discord Q&A bot),
`install-dashboard.sh <viewer-pw> <owner-pw>`, and the dated `PLAN-v*.md` design notes). On
a rebuilt VM, copy that folder to `/home/azureuser/dashboard` and run the installer with
both passwords; the DNS label on the public IP and an NSG rule opening TCP 80/443 from the
internet must exist.

**The egress probe** (`valheim-egress.service`, `Type=simple`, always on) answers one question:
is game egress limited by the VM, or by Valheim itself? The server sits 98% idle with no UDP
buffer errors, no NIC drops and healthy per-player ping, yet egress plateaus around 240–276 KB/s
and has never once exceeded 276,425 B/s. The suspect is Valheim's own per-peer send budget
(ZDOMan's `m_dataPerSec`, historically 61440 B/s): if that is the limit, more players do not buy
more bytes, they buy staler updates — which is what players describe when they say it only
stutters when everyone is in one place.

Once-a-minute sampling cannot tell those apart, so the probe samples once a second. Its
measurement primitive is a self-contained nftables table, `inet valheim_meter`, installed by
`valheim-meter-nft.sh` from the unit's `ExecStartPre=`: two counters on the game ports and a
15-minute-timeout set of the addresses currently talking to the game port. The counter rules
carry no verdict and hook at priority 300, after all existing filtering, so they cannot change
what happens to a packet; the worst a bug there can do is produce a wrong number. That same
`peers` set replaced the once-a-minute `tcpdump` the collector used to run on the game's own
receive path, which is why `nftables` is now an installed package and `tcpdump` is not.

It writes `/var/lib/valheim-status/egress-YYYY-MM-DD.jsonl` — per interval: bytes and packets
both ways, **mean packet size**, player count, the game socket's `tx_queue`, and an A2S round trip
every fifth sample. Two of those fields are about the probe rather than the server, and both earn
their place: `dt` is the interval the row **actually** spans (a stalled loop would otherwise record
a 60-second gap as one second reading 15 MB/s — a single such row is enough to print a false
REFUTED), and `na`/`np` say how old the player count was and how many addresses were really
sending, because `status.json` can be a minute behind and a join it has not noticed makes a
saturated server look like it broke its own ceiling. Rows that fail either check are dropped
rather than written: a hole is honest, a fabricated rate is indistinguishable from a real one.
Whole days are unlinked after `EGRESS_RETAIN_DAYS` (35 by default — retention shorter than the
experiment destroys its early weeks). It only runs while players are online
(gated on the collector's `status.json`, so deciding whether to measure costs nothing), buffers in
memory and writes once every 15 s, and runs at `Nice=10`/`IOWeight=50` so the game always wins.

Players contribute the other half: **`!lag`** in the Hermóðr channel records one timestamped
report (one a minute, ten an hour, per person). Without it the data can show a ceiling exists but
not that it is what anyone is feeling. Hermóðr cannot write `events.jsonl` directly — the
collector rewrites that file wholesale every minute — so `!lag` drops a file into
`/var/lib/valheim-status/lagreports` (mode 1730, same one-writer pattern as the restart spool)
and the collector drains it on its next run.

`!lag` needs one thing to work that is easy to break: `valheim-bot.service` must list
`/var/lib/valheim-status/lagreports` under `ReadWritePaths=`. That directory sits inside a
`ReadOnlyPaths=` entry, so without the more specific grant every report fails with EROFS and the
bot says so to the player each time. If reports are not arriving, check that first.

`valheim-egress-report.py` reads all of it offline and prints **CONFIRMED / REFUTED /
INCONCLUSIVE**. It leads with a coverage block — how much data there actually is, how old the
newest sample is, which player counts are present — and refuses a positive verdict on thin or
stale evidence, because three hours of month-old data otherwise reads exactly like thirty days of
continuous data. **A CONFIRMED verdict needs at least two player counts**, each with at least
half an hour of plateau of its own: R2 (does the plateau scale with n?) is the test that separates
a per-peer budget from a single server-wide cap, and one point has no slope no matter how many
hours went into it.

It also reports the game socket's **receive-queue depth** (`rq`), which is read for free from the
same `/proc/net/udp` line as the send queue and is deliberately kept *outside* the
CONFIRMED/REFUTED ladder, because it is evidence about a different hypothesis. Valheim drains its
UDP socket from the Unity main thread, so a main loop stalling on ZDO churn — the thing that
happens when players cluster, and the thing no other part of this instrument can see without
BepInEx — stops calling `recvfrom` and the kernel's receive buffer fills. A rising `rq` during a
clustered fight would reframe the whole investigation toward tick duration. The asymmetry is
printed with the numbers and matters: non-zero `rq` is strong evidence of a stall, but `rq` at
zero is **weak** evidence against one, since Steam's networking layer may drain the socket on its
own thread and buffer internally.

It also separates instrument error from measurement before computing anything. A sample above
either the link capacity or ten times the 99.9th percentile for its player count is discarded as
an artifact -- a 15 MB/s reading on a host whose observed maximum is 276 KB/s is not a refutation,
it is a reading the machine cannot have produced, and R1 ending the investigation on one such row
would be a category error. The count, the bound and examples are always printed, never silently
swallowed, and above 1% artifacts the instrument rather than the hypothesis becomes the finding.
R1 correspondingly fires on three samples anywhere or two consecutive ones, since a sustained
overshoot is a measurement and a lone spike is not. It leads with six refutation conditions and stops at the first that fires: a
single second above the arithmetic ceiling, a plateau that does not scale with player count, lag
reports while egress is well below the ceiling, small packets inside plateaus, a non-empty socket
send queue, or A2S latency spikes at low egress. Its strongest positive test compares raid
windows against non-raid windows at the same player count *and the same inbound rate* — if demand
demonstrably rose and egress did not move, the sender is supply-limited. `--selftest` runs it
against two synthetic worlds with known answers. Once the question is settled, the probe unit can
simply be disabled — but leave the meter table installed (`valheim-meter-nft.sh install`), or the
dashboard loses its per-player ping column.

**Off-site backups** copy the world off the VM daily, independent of the local
`/home/valheim/backups` snapshots. `valheim-offsite.timer` (daily 03:45 local + up to 10 min
random delay, `Persistent=true` so a missed run fires at next boot) runs
`/usr/local/sbin/valheim-offsite-backup.sh` as root: it calls the existing `backup.sh` for a
fresh world `.tgz`, bundles `adminlist.txt`/`permittedlist.txt`/`bannedlist.txt` into a
second small `.tgz`, and uploads both straight from the VM to Azure Blob Storage using the
VM's system-assigned managed identity (no key or connection string stored anywhere) —
storage account `<OFFSITE_ACCOUNT>`, container `worlds`, Cool access tier, a lifecycle
rule deletes blobs older than 30 days automatically. Status (last attempt/success, error,
blob count and total size, newest blob) is written to
`/var/lib/valheim-status/offsite.json` and merged into the dashboard's `status.json` as
`backup.offsite`. Local `/home/valheim/backups` is pruned to the newest 10 `.tgz` files each
run (off-site now holds the 30-day history). Log: `/var/log/valheim-offsite.log`. To list
the off-site backups from this PC: `az storage blob list --account-name
<OFFSITE_ACCOUNT> --container-name worlds --auth-mode login -o table` (needs the
`Storage Blob Data Reader` role on the storage account, grantable with `az role assignment
create --assignee-object-id $(az ad signed-in-user show --query id -o tsv) --role "Storage
Blob Data Reader" --scope <storage account resource id>`). Run it by hand: `sudo
/usr/local/sbin/valheim-offsite-backup.sh`. Estimated extra cost: well under $0.05/month —
the compressed world is a few MB/day, so 30 days of Cool-tier storage is well under 200 MB;
write/list operations are a handful per day.

## Azure resources (example resource group `rg-valheim`, region West US 2)

| Resource | Example name | Notes |
|---|---|---|
| VM | `vm-valheim` | `Standard_D2as_v7`, 2 vCPU / 8 GB, Ubuntu 22.04 LTS |
| Public IP | `pip-valheim` | Standard SKU, static, with a DNS label for the dashboard hostname |
| NSG | `nsg-valheim` | UDP 2456-2458 from anywhere; TCP 80/443 from anywhere (dashboard). **No inbound SSH** — admin access is over Tailscale instead |
| OS disk | `osdisk-valheim` | 30 GB Standard SSD |
| Storage account | `<OFFSITE_ACCOUNT>` | Standard_LRS, StorageV2, Cool tier, container `worlds`, 30-day lifecycle for off-site world backups; VM managed identity has Storage Blob Data Contributor |

Subscription used in the original deployment: Visual Studio Enterprise (MSDN, roughly
$150/month credit, spending limit ON) — with a spending limit on, Azure stops the
subscription until the next month if the credit runs out rather than billing overage. A pay-
as-you-go subscription works too; just watch the bill or set a budget alert (see below).

Estimated cost, running 24x7: VM ~$66 + disk ~$2.40 + IP ~$3.65 = **about $72/month**.
Deallocated (stopped): about $6/month.

## SSH (over Tailscale — port 22 is closed to the internet)

The VM is reachable **only** over a Tailscale private network; there is no inbound SSH rule
in the NSG. A public `/32` allow-rule tied to a home IP was considered and rejected — on a
connection with a rotating or shared (CGNAT) egress address, a source-IP allow-rule is both
fragile and looser than it looks.

```bash
ssh -i ~/.ssh/<your_key> azureuser@<VM_TAILNET_IP>
scp -i ~/.ssh/<your_key> <file> azureuser@<VM_TAILNET_IP>:/home/azureuser/dashboard/
```

The tailnet address never changes, on any network, from any IP. Tailnet nodes in the
original deployment:

| Node | Notes |
|---|---|
| `vm-valheim` | `tailscaled` enabled at boot; `--accept-dns=false` so it cannot rewrite the VM's `/etc/resolv.conf` and disturb steamcmd, offsite backup, or Let's Encrypt |
| admin client | whichever machine you administer from; add it to your tailnet and note its IP |

**⚠ Node keys expire.** Check the expiry date for each device under
<https://login.tailscale.com/admin/machines> and disable key expiry for long-lived
infrastructure nodes (VM, always-on client) — an expired key silently drops the device off
the tailnet.

### Break-glass: getting in with no inbound port at all

Needs only `az login`, no SSH, no Tailscale:

```bash
az vm run-command invoke -g rg-valheim -n vm-valheim --command-id RunShellScript \
  --scripts "systemctl is-active valheim.service"
```

Use it to repair Tailscale (`sudo tailscale up --hostname=vm-valheim --accept-dns=false`)
or, as a last resort, to open a temporary public SSH rule scoped to your current IP:

```bash
az network nsg rule create -g rg-valheim --nsg-name nsg-valheim -n Allow-SSH-MyIP \
  --priority 110 --direction Inbound --access Allow --protocol Tcp \
  --source-address-prefixes "$(curl -s https://api.ipify.org)/32" \
  --destination-port-ranges 22 --description "SSH from home IP only"
```

The Azure Serial Console in the portal is a third way in and needs no networking at all.

## Operate (run on the VM)

| Task | Command |
|---|---|
| Status | `systemctl status valheim` |
| Live log | `sudo journalctl -u valheim -f` |
| Stop (saves world first) | `sudo systemctl stop valheim` |
| Start | `sudo systemctl start valheim` |
| Restart (also updates to the current Steam build) | `sudo systemctl restart valheim` |
| Force update + validate files | `sudo /home/valheim/update.sh` |
| Update now if Steam has a newer build (skips if players online) | `sudo /usr/local/sbin/valheim-autoupdate.sh` |
| Auto-update history | `sudo cat /var/log/valheim-autoupdate.log` or `sudo journalctl -t valheim-autoupdate` |
| Auto-update timers | `systemctl list-timers \| grep valheim` |
| Snapshot the world to a .tgz | `sudo /home/valheim/backup.sh` (lands in `/home/valheim/backups/`) |
| Full health check | `sudo bash /home/azureuser/verify-valheim.sh` |
| Status: memory trend, errors, players, disk | `sudo bash /home/azureuser/status-valheim.sh` |

Paths on the VM:
- Server binaries: `/home/valheim/server/`
- Save dir: `/home/valheim/data/` (world in `worlds_local/<WORLD_NAME>/`, Valheim's own
  rolling backups land next to it)
- Admin / ban / allow lists: `/home/valheim/data/adminlist.txt` (seed it with your own
  `Steam_<ADMIN_STEAM64_ID>`), `bannedlist.txt`, `permittedlist.txt`. Edit with sudo, then restart.
- Unit file (contains the game password): `/etc/systemd/system/valheim.service`
- First-boot setup log: `/var/log/valheim-setup.log`

## Pull a world backup down to this PC

```bash
ssh -i ~/.ssh/<your_key> azureuser@<VM_TAILNET_IP> \
  "sudo /home/valheim/backup.sh && sudo cp /home/valheim/backups/*.tgz /home/azureuser/ && sudo chown azureuser /home/azureuser/*.tgz"
```

```bash
scp -i ~/.ssh/<your_key> "azureuser@<VM_TAILNET_IP>:/home/azureuser/*.tgz" .
```

## Pause / resume billing (from this PC, Azure CLI)

```bash
az vm deallocate -g rg-valheim -n vm-valheim
```

```bash
az vm start -g rg-valheim -n vm-valheim
```

The IP is retained while deallocated. The service auto-starts on boot.

## Tear everything down

```bash
az group delete -n rg-valheim --yes --no-wait
```

## Optional: budget email alert

```bash
az consumption budget create --budget-name valheim-monthly --amount 100 --category cost \
  --time-grain monthly --start-date 2026-09-01 --end-date 2027-09-01 -g rg-valheim
```

(Adds a budget; configure the email notification in the portal under Cost Management >
Budgets.)

## Things to know

- **Migrating a world from another host:** stop the old server first. Two servers with the
  same name would both appear in the Community list and the worlds would diverge. Anything
  played on the old host after the copy was taken is not in the new copy — re-copy the
  `worlds_local/<WORLD_NAME>` folder from the old host if needed, stop the Azure service,
  replace the directory, start again.
- **Game updates are automatic.** `valheim-update.timer` runs `valheim-autoupdate.sh` every
  30 minutes: it asks Steam for the public build id, and if it differs from the installed
  one and nobody is connected, it snapshots the world to `/home/valheim/backups/`, then
  restarts the service (the restart's `ExecStartPre` pulls the new build). If players are
  online the update is deferred to the next check; it is never forced — there is no
  scheduled `--force` run, by design. Nothing happens when there is no update. Each check
  costs about 0.3 s of CPU and 70 KB of traffic. If friends see "incompatible version" while
  someone is still connected on the old build, wait for them to leave or run `sudo
  /usr/local/sbin/valheim-autoupdate.sh` by hand.
- **Crossplay is disabled by default** (no `-crossplay` flag). The server uses the Steam
  backend: game traffic is direct UDP to 2456, Steam query on 2457. Only Steam players can
  join. To re-enable, add `-crossplay` back to `ExecStart` in the unit file (and in
  `cloud-init.yaml`), then `sudo systemctl daemon-reload && sudo systemctl restart valheim`.
- **Azure CLI on a Windows admin machine:** if the Windows account broker fails in an
  automation shell, `az config set core.enable_broker_on_windows=false` works around it. Be
  aware that with `core.encrypt_token_cache=false` the login token sits unencrypted in
  `%USERPROFILE%\.azure\msal_token_cache.json`; run `az logout` if you'd rather not keep it,
  and prefer leaving the cache encrypted (the default) unless you hit the same broker issue.
- **Restart-executor tuning is optional.** `valheim-restartd.py`/`valheim-restart-exec.py`
  expose about 47 `RESTARTD_*`/`VR_*` environment-variable overrides (paths, timeouts, safety
  limits), every one with a working default — see [`RESTART-TUNING.md`](RESTART-TUNING.md).

## Files in this folder

- `cloud-init.yaml` — the whole VM bootstrap (packages, steamcmd, systemd unit, helper
  scripts). Re-creating the VM with this file reproduces the server minus the world.
- `dashboard/` — dashboard web server and scripts (`index.html`, `Caddyfile`,
  `valheim-status-collect.py`, `valheim-status.service`/`.timer`, `valheim-alert.py`,
  `valheim-alert.env` template, `valheim-world-scan.py`, `valheim-offsite-backup.sh`,
  `valheim-offsite.service`/`.timer`, `valheim-digest.py`,
  `valheim-digest.service`/`.timer`, `valheim-medals.py`,
  `valheim-medals-daily.service`/`.timer`, `valheim-egress-probe.py` +
  `valheim-egress.service` + `valheim-meter-nft.sh` + `valheim-egress-report.py` (the 1 Hz
  egress probe, its nftables meter table, and the offline analysis), PWA `manifest.webmanifest` +
  `icon.svg`/`icon-192.png`/`icon-512.png`, `install-dashboard.sh`, and the `PLAN-v*.md`
  design notes).
- `migrate-world.sh` — checksum-verified world placement; arms the service.
- `verify-valheim.sh` — health check (service, ports, log evidence, on-disk integrity).
- `set-crossplay.sh` — `sudo bash /home/azureuser/set-crossplay.sh on|off` on the VM toggles
  the `-crossplay` flag and restarts the server with evidence.
- `replace-world.sh` — swaps in a newer copy of the world. Extract the zip locally, `scp -r`
  the world folder plus a fresh `world.sha256` manifest to `/home/azureuser/`, then `sudo
  bash /home/azureuser/replace-world.sh`. It verifies checksums and seed, stops the server
  (which saves), archives the current world to `/home/valheim/backups/` and keeps it as
  `worlds_local/<WORLD_NAME>_replaced-<timestamp>`, installs the upload, restarts.
- `status-valheim.sh`, `security-check.sh` — operational and security audits, see Operate.
- `valheim-autoupdate.sh`, `units/` — the auto-update script and its two systemd units
  (installed to `/usr/local/sbin` and `/etc/systemd/system`; also embedded in
  `cloud-init.yaml`).
- `world.sha256` — SHA-256 manifest of a known-good set of world files, used by
  `migrate-world.sh` / `replace-world.sh` to verify a copy before installing it.
