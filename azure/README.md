# Valheim server "1g49ye" on Azure

Deployed 2026-09-10. World "Vancouver Island" was migrated from the home server copy taken 9/10 11:52 AM.

## Connect

| | |
|---|---|
| Address | `20.230.157.206:2456` (static, survives restarts) |
| Password | `hammerhead` |
| Server name | `1g49ye` (shows in the Community list) |
| Crossplay | OFF (Steam PC only, changed 2026-09-10 14:10). No join codes. |

In Valheim: Start Game -> pick character -> Join Game -> Join IP -> `20.230.157.206:2456` -> password.

## Dashboard

| | |
|---|---|
| URL | https://valheim-1g49ye.westus2.cloudapp.azure.com |
| Login | user `viking`, password `hammerhead` (same as the game password) |
| World snapshots | https://valheim-1g49ye.westus2.cloudapp.azure.com/snapshots/ -- a listing of the `.tgz` archives in `/home/valheim/backups`. Needs the separate user `owner` (password printed when the installer was run; the `viking` login is refused here, and `owner` opens nothing else) |

Themed "Existence is Pain" (the group's Discord). v3 (2026-09-11 evening, see `dashboard/PLAN-v3.md` for the data contract) added: real launch settings read from the running process and world file, 30-day uptime, in-game day, boss progression and built-object counts read from the world save (gzip block in `_main.N.db2`, prefab-hash counts over `*.chunk`), raids, deaths, per-Viking ping history, records, who-plays-together matrix, weekday/hour heat map, network rates, world growth, restart causes, update history, next check/snapshot countdowns, Steam patch notes, Discord member/online counts, browser join notifications, and an owner-only `/snapshots/` download route. Shows whether the server is up, who is on with session length and their ping, a 24 h / 7 d player chart, game version and update status, world save info, machine load, a join/leave/update timeline, a per-Viking table (this session, average session, session count, total time, last seen), a Discord link, and the viewer's own ping to the server. Refreshes itself every 30 s.

**Player ping** is measured from the VM: the collector sniffs 3 s of traffic on UDP 2456 to learn each connected player's address (never shown or stored beyond the pairing), then ICMP-pings it. Players whose routers drop ping show "no reply". The dedicated server itself does not expose in-game ping; a server-side mod (BepInEx) would be the only way to get it exactly, and mods would break the hands-off auto-update, so this was not done.

**Discord alerts** (`/usr/local/sbin/valheim-alert.py`, runs after every collector pass) post to a webhook set in `/etc/valheim-alert.env`: server down (2 consecutive failed checks) and back up; update waiting / installing / installed; crash-restart by systemd; memory > 92 %, swap > 1.5 GB, disk > 90 %, world save > 5 s, CPU > 95 % for 5 min (each once, with a clear threshold). `ALERT_JOINS=1` (on since 2026-09-11 17:50, per the owner) also announces every arrival and departure; set to 0 to quiet it. Since 2026-09-11 19:35 the bot never pings @everyone (owner's rule); every post is silent. Setup: Discord channel > Edit channel > Integrations > Webhooks > New Webhook > Copy URL, then on the VM `sudo nano /etc/valheim-alert.env`, paste it after `DISCORD_WEBHOOK_URL=`. Log: `/var/log/valheim-alert.log`.

**Discord, v4 additions (2026-09-11 evening):** the same webhook now also carries a **live status board** -- one message that `valheim-alert.py` keeps editing every minute (title "Vancouver Island, right now": hall open/closed, who's online with session length and ping, in-game day, last raid, bosses defeated, next Steam check, 30-day uptime; never @everyone; if the message is deleted it quietly creates a new one). Its id lives in `/var/lib/valheim-status/alerts.json` under `board_message_id`. **Pin that message in the channel** so it stays at the top -- webhooks cannot pin it themselves. Also new, each posted once with a matching recovery/clear post where it makes sense: **raid** posts (who was online), a **boss falling**, **milestones** (a Viking's hours/deaths/sessions crossing a threshold, and a new name joining the roster -- thresholds are seeded from whatever totals already existed the first time the script saw them, so upgrading never fires a burst of old milestones), **Azure scheduled-maintenance** notices, and an **off-site backup is behind** warning (36 h+ since the last successful copy). A separate **weekly digest** (`/usr/local/sbin/valheim-digest.py`, `valheim-digest.timer`, Sundays 18:00 local) posts one summary embed of the past 7 days: hours logged in total and per Viking, deaths, raids, bosses defeated, most people online at once, uptime, and updates installed; run it by hand with `--dry-run` to print the embed JSON instead of posting. Every new post type was developed and verified against a local catcher on the VM (a `python3 http.server`-style handler on `127.0.0.1:8765`) before ever touching the real webhook.

**Discord medals** (`/usr/local/sbin/valheim-medals.py`, added 2026-09-14) compute a catalogue of ~27 "records" from the same on-disk data (the append-only event log, the per-minute samples, `status.json`, and `valheim-alert.py`'s `boss_log`), grouped time & attendance, deaths & survival, social & co-op, world & exploration, and a for-fun set (Tourist Trap, The Yo-Yo, Lag Lord, and friends) -- see `dashboard/valheim-medals.py`'s `CATALOG` table for the full list, blurbs, and which of day/week/all-time window each applies to. Delivered four ways, all from one shared `compute()` so they can never disagree: a curated ~8-medal subset can interrupt the channel live when an all-time record breaks (a third `ExecStart=` on `valheim-status.service`, so it runs every minute right after the collector and the alert script -- capped at 2 posts per run and a 6 h cooldown per medal so a marathon session doesn't spam the channel); **"Yesterday on Vancouver Island"** posts daily at 09:00 local (`valheim-medals-daily.timer`) and is skipped entirely on a quiet day; the same table contributes a second embed to the Sunday `valheim-digest.py` message (imported defensively, so a missing or broken medals script only costs that one embed, never the weekly ledger); and a pinned **Hall of Fame** board is upserted right after the daily post, the same edit-in-place pattern the live status board uses. Every medal requires at least two eligible players and a sane minimum (30 min played, 2 deaths, etc.) before it is awarded -- one person playing alone gets no medals, and a day nobody played gets no post at all. All-time medals are always phrased "since 10 September" (from `players.tracking_since`), never "all time", since the server is only a few days old. State lives in its own file, `/var/lib/valheim-status/medals.json` -- it never touches `alerts.json`, which `valheim-alert.py` rewrites wholesale every minute. Run any mode by hand with `--dry-run` to print the embed JSON instead of posting (`--daily`, `--weekly`, `--board`, `--check-records`, `--seed`); `--seed` (run once at deploy) records every existing all-time holder without posting, so turning this on for the first time doesn't dump the whole catalogue into the channel at once. Log: `/var/log/valheim-medals.log`.

**Hermodr (Discord Q&A bot)** (`/usr/local/sbin/valheim-bot.py`, `valheim-bot.service`, added 2026-09-14) is a long-running Discord gateway bot (discord.py, its own venv at `/opt/hermodr/venv`) that answers questions in one Discord channel about the server, using the same on-disk data as everything else above -- `status.json`, the event log, and `valheim-medals.py`'s catalogue (imported live, never a second copy of the logic). It responds only when @-mentioned or when someone replies to one of its own messages, and only in the channel set by `HERMODR_CHANNEL_ID` (or, as a fallback, resolved once at startup by exact name from `HERMODR_CHANNEL_NAME`). Every question and every message it reads first passes a **channel allowlist check** -- before any parsing, before any AI call, before anything is logged -- so it is structurally incapable of answering (or leaking data) anywhere else; DMs are ignored entirely. It can answer: who's online, in-game day, bosses defeated, uptime, last raid; per-Viking hours/sessions/deaths/last-seen/longest-session/best-streak; the current holder of all ~27 medals for day/week/all-time, and how each one is calculated (sourced live from `valheim-medals.py`'s `CATALOG`, never a hand-copied list, so the explanations can't drift from the real logic); world exploration %, structures built, tombstones, tamed animals; and the tracking-start date, so it never claims "all-time" means longer ago than that. It has **no tools** and cannot take any action -- its only effect is the text reply it posts back to the same channel, which is always sent with `allowed_mentions` set to none (so pings are inert at the Discord API level) with a second-layer strip of any literal `@everyone`/`@here` the model types anyway, capped at ~500 model tokens and truncated to Discord's 2000-char limit on a word boundary. A user's own message text is never folded into the system prompt -- it goes in as a separate untrusted `user` message, with the system prompt stating plainly that any instructions embedded in it must be ignored. Per-user rate limit (in-memory, resets on restart): 1 question per 10 s, 20 per hour; going over gets one brief reply, then silence. The model call itself uses the VM's system-assigned managed identity against an Azure AI Foundry endpoint (`HERMODR_AI_ENDPOINT`, default the `ai-valheim` project) -- **no API key stored anywhere**; **to swap the model, edit only `HERMODR_AI_MODEL`** in `/etc/valheim-alert.env` (default `hermodr-llm`) and restart the service. Setup: create a Discord application + bot user at <https://discord.com/developers/applications>, enable the **Message Content** privileged intent, invite it to the server with the `bot` scope and `Send Messages`/`Read Message History` permissions, then on the VM set `DISCORD_BOT_TOKEN` and `HERMODR_CHANNEL_ID` (or `HERMODR_CHANNEL_NAME`) in `/etc/valheim-alert.env` and `sudo systemctl enable --now valheim-bot`. The installer (`install-dashboard.sh`) creates the venv and installs the script and unit but deliberately does **not** enable or start it, since a fresh deploy has no token yet. Test locally with no bot token and no VM: `python3 valheim-bot.py --selftest` builds and prints the context (no Discord, no AI call); `python3 valheim-bot.py --ask "question"` builds the context and calls the AI endpoint once (only works on the VM, where managed identity exists; fails with a clear message anywhere else). Log: `/var/log/hermodr.log` (the bot token is never written to it, redacted defensively even from tracebacks).

How it works on the VM: `valheim-status.timer` runs `/usr/local/sbin/valheim-status-collect.py` every minute (reads the service journal incrementally, queries the Steam port 2457, samples /proc) and writes `/var/www/valheim/status.json` + `history.json`; Caddy serves `/var/www/valheim` with basic auth and an automatic Let's Encrypt certificate, and serves `/home/valheim/backups` at `/snapshots/` (directory listing) behind the second `owner` login. Caddy runs as user `caddy`; the installer grants it an ACL for traversal of `/home/valheim` and read of `/home/valheim/backups` only (`setfacl -m u:caddy:x /home/valheim; setfacl -m u:caddy:rx /home/valheim/backups`), so `/home/valheim/data` stays private. State and 30 days of samples/events live in `/var/lib/valheim-status/`. Cost: about 0.1 s CPU per minute, no extra Azure charge.

| Task | Command (on the VM) |
|---|---|
| Collector health | `systemctl status valheim-status.timer valheim-status.service` |
| Run collector by hand | `sudo systemctl start valheim-status.service` |
| Web server health | `systemctl status caddy`; `sudo journalctl -u caddy -n 50` |
| Test the Discord webhook | `sudo rm -f /var/lib/valheim-status/alerts.json` then wait a minute (only real conditions post; use a temporary bad status to test) or `curl -H 'Content-Type: application/json' -d '{"content":"test"}' "$URL"` |
| Change either password | `cd /home/azureuser/dashboard && sudo bash install-dashboard.sh <viewer-pw> <owner-pw>` (both are required every time; pass the unchanged one again). The installer also re-installs `index.html`, the collector and the alert script from that folder, so `sudo cp` the live copies (`/var/www/valheim/index.html`, `/usr/local/sbin/valheim-status-collect.py`, `/usr/local/sbin/valheim-alert.py`) into it first if they are newer than the folder. It never overwrites `/etc/valheim-alert.env`. |
| Change only the Caddy config after editing `dashboard/Caddyfile` | `scp` it up, then run the installer as above; it does `caddy validate` and `systemctl reload caddy` (no restart, no game-server impact) |
| Download a snapshot from this PC | `curl -u owner -O https://valheim-1g49ye.westus2.cloudapp.azure.com/snapshots/<name>.tgz` (prompts for the owner password) or open `/snapshots/` in a browser |
| Redeploy the page after editing `dashboard/index.html` locally | `scp` it to `/home/azureuser/dashboard/` then `sudo install -m 0644 /home/azureuser/dashboard/index.html /var/www/valheim/index.html` |

Files: `dashboard/` in this folder (`index.html`, `valheim-status-collect.py`, `valheim-alert.py`, `valheim-alert.env` template, the two systemd units `valheim-status.service` + `valheim-status.timer`, `Caddyfile` with the `__HASH__` / `__OWNER_HASH__` placeholders for the `viking` and `owner` logins, `valheim-digest.py`/`.service`/`.timer`, `valheim-medals.py`/`valheim-medals-daily.service`/`.timer`, `valheim-bot.py`/`valheim-bot.service` (Hermodr, the Discord Q&A bot), `install-dashboard.sh <viewer-pw> <owner-pw>`, and `PLAN-v3.md` with the design notes). On a rebuilt VM, copy that folder to `/home/azureuser/dashboard` and run the installer with both passwords; the DNS label `valheim-1g49ye` on `pip-valheim` and NSG rule `Allow-Dashboard-HTTPS` (TCP 80, 443 from Internet, priority 120) must exist.

**Off-site backups** (added 2026-09-11, see `dashboard/PLAN-v4.md` task S1) copy the world off the VM daily, independent of the local `/home/valheim/backups` snapshots. `valheim-offsite.timer` (daily 03:45 local + up to 10 min random delay, `Persistent=true` so a missed run fires at next boot) runs `/usr/local/sbin/valheim-offsite-backup.sh` as root: it calls the existing `backup.sh` for a fresh world `.tgz`, bundles `adminlist.txt`/`permittedlist.txt`/`bannedlist.txt` into a second small `.tgz`, and uploads both straight from the VM to Azure Blob Storage using the VM's system-assigned managed identity (no key or connection string stored anywhere) — storage account `stvalheimz09j56`, container `worlds`, Cool access tier, a lifecycle rule deletes blobs older than 30 days automatically. Status (last attempt/success, error, blob count and total size, newest blob) is written to `/var/lib/valheim-status/offsite.json` and merged into the dashboard's `status.json` as `backup.offsite`. Local `/home/valheim/backups` is pruned to the newest 10 `.tgz` files each run (off-site now holds the 30-day history). Log: `/var/log/valheim-offsite.log`. To list the off-site backups from this PC: `az storage blob list --account-name stvalheimz09j56 --container-name worlds --auth-mode login -o table` (needs the `Storage Blob Data Reader` role on the storage account, granted to `kkrzyston@gmail.com`'s signed-in user on 2026-09-11; `az role assignment create --assignee-object-id $(az ad signed-in-user show --query id -o tsv) --role "Storage Blob Data Reader" --scope <storage account resource id>` grants it to anyone else who needs it). Run it by hand: `sudo /usr/local/sbin/valheim-offsite-backup.sh`. Estimated extra cost: well under $0.05/month (the compressed world is about 4 MB/day, so 30 days of Cool-tier storage is roughly 120-150 MB; write/list operations are a handful per day) -- negligible against the $150/month MSDN credit.

## Azure resources (all in resource group `rg-valheim`, region West US 2)

| Resource | Name | Notes |
|---|---|---|
| VM | `vm-valheim` | `Standard_D2as_v7`, 2 vCPU / 8 GB, Ubuntu 22.04 LTS |
| Public IP | `pip-valheim` | 20.230.157.206, Standard SKU, static, DNS label `valheim-1g49ye.westus2.cloudapp.azure.com` |
| NSG | `nsg-valheim` | UDP 2456-2458 from anywhere; TCP 80/443 from anywhere (dashboard). **No inbound SSH** — port 22 closed 2026-09-14, admin access is over Tailscale |
| OS disk | `osdisk-valheim` | 30 GB Standard SSD |
| Storage account | `stvalheimz09j56` | Standard_LRS, StorageV2, Cool tier, container `worlds`, 30-day lifecycle for off-site world backups; VM managed identity has Storage Blob Data Contributor |

Subscription: Visual Studio Enterprise (MSDN, $150/month credit, spending limit ON). If the month's credit runs out, Azure stops the subscription until the next month; it never bills overage.

Estimated cost, running 24x7: VM ~$66 + disk ~$2.40 + IP ~$3.65 = **about $72/month**. Deallocated (stopped): about $6/month.

## SSH (over Tailscale — port 22 is closed to the internet)

Since 2026-09-14 the VM is reachable **only** over a Tailscale private network. The
public SSH rule (`Allow-SSH-MyIP`) was deleted because the home connection is Starlink:
the IP rotates constantly, and its CGNAT egress address is shared with other Starlink
customers, so a `/32` allow-rule was both fragile and looser than it looked.

```bash
ssh -i ~/.ssh/valheim_azure azureuser@100.67.101.52     # vm-valheim on the tailnet
scp -i ~/.ssh/valheim_azure <file> azureuser@100.67.101.52:/home/azureuser/dashboard/
```

The tailnet address never changes, on any network, from any IP. Tailnet nodes:

| Node | Tailnet IP | Notes |
|---|---|---|
| `vm-valheim` | `100.67.101.52` | `tailscaled` enabled at boot; `--accept-dns=false` so it cannot rewrite the VM's `/etc/resolv.conf` and disturb steamcmd, offsite backup, or Let's Encrypt |
| `msi` | `100.123.176.75` | Windows client, service set to Automatic |

**⚠ Node keys expire.** `vm-valheim`'s key expires **2027-03-13**. When it does, the VM
silently drops off the tailnet. Disable expiry at
<https://login.tailscale.com/admin/machines> → `vm-valheim` → ⋯ → *Disable key expiry*.

### Break-glass: getting in with no inbound port at all

Verified working before port 22 was closed. Needs only an `az login`, no SSH, no Tailscale:

```bash
az vm run-command invoke -g rg-valheim -n vm-valheim --command-id RunShellScript   --scripts "systemctl is-active valheim.service"
```

Use it to repair Tailscale (`sudo tailscale up --hostname=vm-valheim --accept-dns=false`)
or, as a last resort, to restore the old public SSH rule:

```bash
az network nsg rule create -g rg-valheim --nsg-name nsg-valheim -n Allow-SSH-MyIP   --priority 110 --direction Inbound --access Allow --protocol Tcp   --source-address-prefixes "$(curl -s https://api.ipify.org)/32"   --destination-port-ranges 22 --description "SSH from home IP only"
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
- Save dir: `/home/valheim/data/` (world in `worlds_local/Vancouver Island/`, Valheim's own rolling backups land next to it)
- Admin / ban / allow lists: `/home/valheim/data/adminlist.txt` (seeded with `Steam_76561198185349471`), `bannedlist.txt`, `permittedlist.txt`. Edit with sudo, then restart.
- Unit file (contains the password): `/etc/systemd/system/valheim.service`
- First-boot setup log: `/var/log/valheim-setup.log`

## Pull a world backup down to this PC

```bash
ssh -i ~/.ssh/valheim_azure azureuser@100.67.101.52 "sudo /home/valheim/backup.sh && sudo cp /home/valheim/backups/*.tgz /home/azureuser/ && sudo chown azureuser /home/azureuser/*.tgz"
```

```bash
scp -i ~/.ssh/valheim_azure "azureuser@100.67.101.52:/home/azureuser/*.tgz" .
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

## Optional: budget email alert at $100

```bash
az consumption budget create --budget-name valheim-monthly --amount 100 --category cost --time-grain monthly --start-date 2026-09-01 --end-date 2027-09-01 --resource-group rg-valheim
```

(Adds a budget; configure the email notification in the portal under Cost Management > Budgets.)

## Things to know

- **Old home server:** stop it. Two servers named `1g49ye` would both appear in the Community list and the worlds would diverge. Anything played on the old box after 9/10 11:52 AM is not in this copy; re-copy `worlds_local\Vancouver Island` from the old host if needed, stop the Azure service, replace the directory, start again.
- **Game updates are automatic** (since 2026-09-11). `valheim-update.timer` runs `valheim-autoupdate.sh` every 30 minutes: it asks Steam for the public build id, and if it differs from the installed one and nobody is connected, it snapshots the world to `/home/valheim/backups/`, then restarts the service (the restart's `ExecStartPre` pulls the new build). If players are online the update is deferred to the next check; it is never forced (no scheduled `--force`, by the owner's rule). Nothing happens when there is no update. Each check costs about 0.3 s of CPU and 70 KB of traffic, which is free on this VM's billing. If friends see "incompatible version" while someone is still connected on the old build, wait for them to leave or run `sudo /usr/local/sbin/valheim-autoupdate.sh` by hand.
- **Crossplay is disabled** (no `-crossplay` flag). The server uses the Steam backend: game traffic is direct UDP to 2456, Steam query on 2457. Only Steam players can join. To re-enable, add `-crossplay` back to `ExecStart` in the unit file (and in `cloud-init.yaml`), then `sudo systemctl daemon-reload && sudo systemctl restart valheim`.
- **Azure CLI on this PC:** it is configured with `core.enable_broker_on_windows=false` and `core.encrypt_token_cache=false` because the Windows account broker failed in the automation shell. The login token sits unencrypted in `%USERPROFILE%\.azure\msal_token_cache.json`; run `az logout` if you'd rather not keep it.

## Files in this folder

- `cloud-init.yaml` — the whole VM bootstrap (packages, steamcmd, systemd unit, helper scripts). Re-creating the VM with this file reproduces the server minus the world.
- `dashboard/` — dashboard web server and scripts (`index.html`, `Caddyfile`, `valheim-status-collect.py`, `valheim-status.service`/`.timer`, `valheim-alert.py`, `valheim-alert.env` template, `valheim-world-scan.py`, `valheim-offsite-backup.sh`, `valheim-offsite.service`/`.timer`, `valheim-digest.py`, `valheim-digest.service`/`.timer`, `valheim-medals.py`, `valheim-medals-daily.service`/`.timer`, PWA `manifest.webmanifest` + `icon.svg`/`icon-192.png`/`icon-512.png`, `install-dashboard.sh`, `PLAN-v3.md`, `PLAN-v4.md`).
- `migrate-world.sh` — checksum-verified world placement; arms the service.
- `verify-valheim.sh` — health check (service, ports, log evidence, on-disk integrity).
- `set-crossplay.sh` — `sudo bash /home/azureuser/set-crossplay.sh on|off` on the VM toggles the `-crossplay` flag and restarts the server with evidence. Currently OFF.
- `replace-world.sh` — swaps in a newer copy of the world. Extract the zip locally, `scp -r` the `Vancouver Island` folder plus a fresh `world.sha256` manifest to `/home/azureuser/`, then `sudo bash /home/azureuser/replace-world.sh`. It verifies checksums and seed, stops the server (which saves), archives the current world to `/home/valheim/backups/` and keeps it as `worlds_local/Vancouver Island_replaced-<timestamp>`, installs the upload, restarts.
- `status-valheim.sh`, `security-check.sh` — operational and security audits, see Operate.
- `valheim-autoupdate.sh`, `units/` — the auto-update script and its two systemd units (installed to `/usr/local/sbin` and `/etc/systemd/system`; also embedded in `cloud-init.yaml`).

## World history

| When | What |
|---|---|
| 2026-09-10 13:40 | Migrated from home server copy taken 11:52 AM (save 54) |
| 2026-09-11 15:30 | Dashboard added at https://valheim-1g49ye.westus2.cloudapp.azure.com (Caddy + collector timer, TCP 80/443 opened). |
| 2026-09-11 15:05 | Server updated 1.0.7 -> 1.0.12 (Steam build 25185644 -> 25253791) by the new auto-updater; pre-update snapshot `vancouver-island-20260911-150520.tgz`. Auto-update timer enabled; the 04:30 forced window was removed the same day (never force). |
| 2026-09-10 14:26 | Replaced with home server copy taken 2:23 PM (save 61). The Azure-side world from 13:40 to 14:26, which had about 10 minutes of play by three people, is archived at `/home/valheim/backups/vancouver-island-pre-replace-20260910-142653.tgz` and kept on disk as `worlds_local/Vancouver Island_replaced-20260910-142653`. |
- `world.sha256` — SHA-256 manifest of the 19 migrated world files.
