# Dashboard v4 plan: second tier (off-site backup, Discord live board, deeper world data, timeline, polish)

Read `PLAN-v3.md` first for the environment, SSH, deploy conventions and the existing data contract. Everything there still holds.
Hard rules for every agent:
- NEVER stop/restart/touch `valheim.service`, never run steamcmd, never force an update, never edit the NSG rules that exist, never reboot the VM. Four people are usually playing.
- Only edit the files your task owns (listed per task). Other agents are editing the other files at the same time.
- Deploy = scp to `/home/azureuser/dashboard/` then `sudo install` to the destination. Collector/alert changes take effect on the next minute (`sudo systemctl start valheim-status.service` runs them now; check `sudo journalctl -u valheim-status -n 30 --no-pager` for tracebacks).
- Line endings LF. Python syntax check before scp. Keep the collector's total runtime under ~3.5 s (`collect_ms` in status.json).
- Report: exact verification commands and their output, pasted excerpts of the new JSON, and anything you could not do.

Current live facts: dashboard https://valheim-1g49ye.westus2.cloudapp.azure.com (viking/hammerhead; `/snapshots/` owner-only). Discord webhook is set in `/etc/valheim-alert.env` (`DISCORD_WEBHOOK_URL`, `ALERT_JOINS=1`, `DASHBOARD_URL`); the env file is root-only, read it with sudo on the VM, never paste the URL into reports or files. VM: Ubuntu 22.04, Python 3.10, root cron via systemd timers, `eth0`, timezone America/Los_Angeles. Azure: subscription be0a6b66-1dce-4568-9c77-47dbbc5937b1 (MSDN credit, spending cap), RG `rg-valheim`, region westus2, VM `vm-valheim`; az CLI on this PC at `/c/Program Files/Microsoft SDKs/Azure/CLI2/wbin/az` (add to PATH in Git Bash), already logged in.

## Data contract additions (collector merges side files; page renders)

Side files under `/var/lib/valheim-status/` are produced by other scripts and merged by the collector into status.json verbatim if present:
```
world_extra.json  -> status.world.extra      (task D1 writes; H1 merges)
offsite.json      -> status.backup.offsite   (task S1 writes; H1 merges)
```
Collector-native additions (task H1):
```
azure.maintenance = {fetched, incarnation, events: [{id, type, status, not_before, description, resources}]}   # from IMDS scheduled events
machine.os_updates = {pending, security, reboot_required, checked}                                            # from update-notifier files
players.sessions_7d = [{name, start, end|null}]     # end null = still online; from events, last 7 days, chronological
players.calendar = {name: {"YYYY-MM-DD": seconds}}  # local dates, last 365 days, seconds played that day (split sessions at midnight)
events retained forever (samples still 30 days); records/together become all-time
```
D1 `world_extra.json`:
```
{scanned_at, fwl_mtime,
 explored: {zones_generated, zones_total, pct}|null,
 tombstones: [{owner, x, z}],            # owner names from the tombstone ZDO's string data; unnamed -> "unknown"
 wards: int, tamed: {boar, wolf, lox, hen, asksvin}|null,
 basemap: {extent: 10000, points: [[x, z, kind]...]}   # kind: "build" | "portal" | "bed" | "ship" | "tomb" | "ward"; downsample builds to a 16 m grid; cap 4000 points
 notes: "human-readable caveats"}
```
S1 `offsite.json`:
```
{last_attempt, last_success, last_error|null, account, container, blobs: int, bytes: int, newest_blob, retention_days: 30}
```
Alert/Discord additions (task S2), all read from status.json:
- live status board message (edited every minute), raid posts, boss posts, milestone posts, Azure maintenance post, off-site backup missed post, weekly digest (Sunday 18:00 local).

## Tasks

### D1 (Opus): deep world-file parser -> `valheim-world-scan.py` (new file, owns it alone)
Goal: produce `world_extra.json` per the contract. Installed at `/usr/local/sbin/valheim-world-scan.py`; the collector (H1) runs it with a 60 s timeout whenever the newest `.fwl2` mtime changes, so it may take a few seconds; it must be safe to run any time (read-only, opens files read-only, never writes into the world dir). World dir: `/home/valheim/data/worlds_local/Vancouver Island` (root can read). Facts known (see PLAN-v3): `.db2` = int32 version(41) + double netTime, then a gzip stream (magic `1f8b08` at byte 16) which decompresses to ~217 KB containing global keys such as `defeated_eikthyr` as plain strings; `*.chunk` files hold ZDOs uncompressed; the stable-hash-count trick already works (function in `valheim-status-collect.py`, copy it). Approach for positions: find occurrences of a known prefab hash (e.g. `portal_wood`, there are exactly 2) in a chunk, dump the surrounding 64 bytes, and look for three little-endian float32 (x, y, z) with |x|,|z| < 10500 and -100 < y < 1000 at a fixed offset relative to the hash; confirm the offset on `bed` (6) and `piece_workbench` (12), then generalise. Tombstone owner: `Player_tombstone` ZDO stores the owner name as a length-prefixed UTF-8 string in its string properties, near the hash. Explored zones: in the decompressed .db2 block, the ZoneSystem section stores generated zones as int32 count followed by count (int32 x, int32 y) pairs with |x|,|y| <= ~160, followed by the global keys count and strings; locate it by searching backwards from the `defeated_` strings. `zones_total` = number of 64 m zones inside the 10 km world radius (about 76,000; compute it). Tamed animals: only if you can find the `tamed` boolean reliably; otherwise `null` with a note. Builds for the basemap: count/plot every ZDO whose prefab hash is in a list of common player-built pieces (wood_wall, wood_floor, wood_beam, wood_pole, wood_roof_45, wood_door, stone_wall_1x1, stone_wall_2x1, stone_wall_4x2, stone_floor_2x2, stone_stair, stone_arch, piece_workbench, forge, piece_chest_wood, bed, portal_wood, guard_stone, fermenter, smelter, charcoal_kiln, windmill, piece_stonecutter, piece_cauldron, piece_artisanstation, piece_blackmarble_*, darkwood_*, piece_sharpstakes, wood_stack, etc.; build a list of ~60 names, hash them). Validate every number you emit with a sanity check and put caveats into `notes`. Deploy the script and run it by hand (`sudo python3 /usr/local/sbin/valheim-world-scan.py`), paste the JSON summary (not the point list) in your report, and measure runtime. Do not edit any other file.

### S1 (Sonnet): off-site backup to Azure Blob -> new files `valheim-offsite-backup.sh`, `valheim-offsite.service`, `valheim-offsite.timer`; Azure resources
1. From this PC with az: create a storage account in `rg-valheim` (name like `stvalheim` + 6 random lowercase alnum chars; Standard_LRS, kind StorageV2, access tier Cool, `--allow-blob-public-access false`, min TLS 1.2), a container `worlds`, and a lifecycle management rule that deletes blobs older than 30 days. Enable a system-assigned managed identity on `vm-valheim` (`az vm identity assign`, this does not reboot the VM) and grant it `Storage Blob Data Contributor` on the storage account.
2. On the VM, `valheim-offsite-backup.sh` (root): runs the existing `/home/valheim/backup.sh` to make a fresh `.tgz` of the world (it tars the live world dir; that is fine, Valheim's own files are consistent between saves), also bundles `/home/valheim/data/adminlist.txt permittedlist.txt bannedlist.txt` into the same archive or a second small one, then uploads with plain curl: get a token from IMDS (`curl -s -H Metadata:true "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https://storage.azure.com/"`), then `PUT https://<acct>.blob.core.windows.net/worlds/<name>.tgz` with headers `Authorization: Bearer`, `x-ms-blob-type: BlockBlob`, `x-ms-version: 2021-08-06`. Then list the container (`?restype=container&comp=list`) to fill `offsite.json` (contract above) at `/var/lib/valheim-status/offsite.json` (0644). Log to `/var/log/valheim-offsite.log`. Also prune local `/home/valheim/backups` to the newest 10 tarballs.
3. Timer: daily at 03:45 local, `Persistent=true`, plus `RandomizedDelaySec=10min`. Run it once now by hand and prove the blob exists (`az storage blob list` from the PC with `--auth-mode login`).
4. Report: account name, monthly cost estimate, the offsite.json produced, and add a "Off-site backups" paragraph to `README.md` under the Dashboard section (you own README for this task; append only, do not restructure).
Do not edit the collector, alert script, page, or Caddy.

### S2 (Sonnet): Discord upgrades -> owns `valheim-alert.py`, new `valheim-digest.py`, `valheim-digest.service/.timer`, `valheim-alert.env` (template only; on the VM edit `/etc/valheim-alert.env` with sudo sed, never print it)
Read the current `valheim-alert.py` fully. Add, all reading `status.json` (fields per contracts v3 and v4; guard for missing fields since other agents are deploying in parallel):
1. **Live status board**: one Discord message the alerter keeps editing every minute. Create it once with `POST <webhook>?wait=true` (returns the message id), store `board_message_id` in `/var/lib/valheim-status/alerts.json`, then `PATCH <webhook>/messages/<id>` each run. Content: an embed titled "Vancouver Island, right now" with: hall open/closed, who is online with session length and ping, day N, last raid, bosses defeated N/7, next Steam check, uptime %, "updated <time>" footer. Never mention @everyone in the board. If the PATCH returns 404 (message deleted), create a new one. Tell the user in your report to pin the message (webhooks cannot pin).
2. **Raid posts**: event kind `raid` -> "Raid: <label>" with who was online (from `world.raids.list[0].online` if it matches the event time), no @everyone.
3. **Boss posts**: compare `world.bosses` with the last stored copy; a boss flipping to true -> "<Boss> has fallen" in torch colour, with @everyone (the owner wants everyone pinged for big news). Boss display names: Eikthyr, The Elder, Bonemass, Moder, Yagluth, The Queen, Fader.
4. **Milestones**: from `players.stats[]`: total hours crossing 10/25/50/100/250/500, deaths crossing 10/25/50/100, sessions crossing 25/50/100 -> one post each, remembered in state so it never repeats. Also "The hall has N Vikings" the first time a new name appears.
5. **Azure maintenance**: `azure.maintenance.events` non-empty -> post once per event id (warn colour) with type, not_before and description; post an all-clear when it disappears.
6. **Off-site backup**: if `backup.offsite.last_success` is older than 36 h (or missing while `last_attempt` exists) -> post once until it recovers; on recovery post ok.
7. **Weekly digest** `valheim-digest.py` + timer Sunday 18:00 local (`Persistent=true`): hours in the world this week (total and per Viking), sessions, deaths per Viking, raids, bosses defeated this week, most people at once, uptime %, updates installed, and a link to the dashboard. Post as one embed. Provide `--dry-run` that prints the JSON body instead of posting; run it once with `--dry-run` for the report. Do NOT post the digest for real now.
Test every new post type against a local catcher (pattern: `python3 -m http.server`-style handler on 127.0.0.1:8765 printing POST/PATCH bodies; the previous session did this) before enabling with the real webhook; the only real posts allowed during your work are the initial board message and its edits. Update the README Dashboard section with a short "Discord" paragraph (append only).

### S3 (Sonnet): page -> owns `index.html`, new `manifest.webmanifest`, `icon.svg` (and `icon-192.png`/`icon-512.png` if you can generate them with Python; otherwise SVG only)
Read `index.html` fully (it is ~900 lines, keep its palette, fonts, voice and helper functions). Add:
1. **Session timeline** band under the tide chart: last 7 days, one row per Viking, bars for `players.sessions_7d`, day gridlines, hover title "Tue 7:12 PM to 9:40 PM (2 h 28 min)", current session runs to the right edge with the torch colour.
2. **Calendar** in the "Vikings of the hall" area: per Viking a 52 x 7 contribution-style grid from `players.calendar` (last 365 days, shade by hours, title per cell); collapsible per Viking, default collapsed except the top 2 by total time.
3. **World map**: canvas scatter of `world.extra.basemap.points` (square, extent 10000 m centred on 0,0; kinds coloured: build ash, portal torch, bed moss, ship parchment, tomb blood, ward dim torch), with the 10 km world circle drawn faintly and spawn at the centre; legend; "N zones explored (X %)" from `world.extra.explored`; tombstone owners listed under it ("Brunhilde's tombstone at 340 m east, 120 m south"); wards and tamed counts in the built grid.
4. **Off-site backups** in the "For the owner" area: last successful copy, blob count and size, storage account name, error if any.
5. **Azure maintenance** line in "Restarts and updates": "No Azure maintenance scheduled" or the event with its time; **OS updates** line in the machine dl ("N updates pending, M security; reboot required" / "up to date").
6. **PWA**: `manifest.webmanifest` (name "Existence is Pain", short_name "Valheim", theme_color #1a1512, background #1a1512, display standalone, icons) linked from `<head>` with `<link rel="manifest">`, `<meta name="theme-color">`, `<link rel="apple-touch-icon">`; `icon.svg` a simple torch-gold rune-like mark on soot. No service worker (the page must always be live).
7. **Steam join link** next to the copy button: `steam://run/892970//+connect%2020.230.157.206:2456` labelled "Open in Steam (may just launch the game)"; keep the copy button as the primary.
8. Records and together matrix notes say "all time" once the collector's retention change lands (guard: if `players.records_window` is "all" show "all time", else "30 days"; H1 will emit `players.records_window`).
Every new section must degrade quietly when its data is missing. Test with mocks in your scratchpad like before, plus on the live site, desktop and mobile (375 px) via the Browser pane, console clean. Deploy `index.html`, `manifest.webmanifest`, `icon*.{svg,png}` with `sudo install -m 0644` into `/var/www/valheim/`.

### H1 (Haiku): collector small additions -> owns `valheim-status-collect.py` only
Read the file fully first (it is long; the v3 sections are marked). Add:
1. **Merge side files**: after the world scan section, if `/var/lib/valheim-status/world_extra.json` exists load it into `status["world"]["extra"]`; if `/var/lib/valheim-status/offsite.json` exists load it into `status["backup"] = {"offsite": ...}`. Wrap in try/except; missing files mean the key is absent.
2. **Run the deep world scan**: when the newest `.fwl2` mtime differs from `state["world_extra_mtime"]` and `/usr/local/sbin/valheim-world-scan.py` exists, run `subprocess.run(["python3", "/usr/local/sbin/valheim-world-scan.py"], timeout=60)` in try/except, then set `state["world_extra_mtime"]`. (The script is being written by another agent; until it exists this is a no-op.)
3. **Azure maintenance**: GET `http://169.254.169.254/metadata/scheduledevents?api-version=2020-07-01` with header `Metadata: true`, 5 s timeout, cache the result in state for 5 minutes; map to the contract (`azure.maintenance`). On any failure keep the last cached value and add `"error": "<msg>"`.
4. **OS updates**: parse `/var/lib/update-notifier/updates-available` (lines like "N updates can be applied immediately." and "M of these updates are standard security updates.") and `os.path.exists("/var/run/reboot-required")` into `machine.os_updates`.
5. **Retention**: events are no longer trimmed (keep `RETAIN_SECONDS` for samples only); the `together`, `records` computations use all events; emit `players.records_window = "all"`.
6. **Sessions and calendar**: the file already reconstructs sessions from events for `together` (look for that code and reuse it). Emit `players.sessions_7d` (sessions overlapping the last 7 days, `end` null when still online) and `players.calendar` (per name, per local date YYYY-MM-DD, seconds; split sessions at local midnight; last 365 days). Use `local_dt()` already defined for local time.
Keep runtime under 3.5 s. Deploy, run, paste the new JSON sections and `collect_ms`.

## Integration (main session)
After all five report: check collect_ms, journal for tracebacks, alert log, page sections on desktop and mobile, README coherence, memory notes.
