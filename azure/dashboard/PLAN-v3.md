# Dashboard v3 plan: everything that does not need a server mod

Owner: the Valheim server "1g49ye" / world "Vancouver Island", Azure VM `vm-valheim` (20.230.157.206).
Dashboard: https://valheim-1g49ye.westus2.cloudapp.azure.com (basic auth viking/hammerhead), source in this folder.
SSH from this PC: `ssh -o BatchMode=yes -i ~/.ssh/valheim_azure azureuser@20.230.157.206` (use `sudo` on the VM).
Rule from the owner: NEVER restart the game server or force an update. Nothing in this plan touches valheim.service.

## Existing pieces (do not rewrite, extend)
- `valheim-status-collect.py` -> `/usr/local/sbin/valheim-status-collect.py`, run every minute by `valheim-status.timer` (root).
  Reads the journal incrementally (cursor in `/var/lib/valheim-status/cursor`), keeps `state.json`, appends `samples.jsonl` (per minute, 30 days) and `events.jsonl` (30 days), writes `/var/www/valheim/status.json` and `history.json`.
- `valheim-alert.py` runs after it (Discord webhook, not configured yet). Leave it alone.
- `index.html` -> `/var/www/valheim/index.html`, vanilla JS, fetches `status.json` + `history.json` every 30 s via `base + 'status.json'`.
- `Caddyfile` -> `/etc/caddy/Caddyfile`, `install-dashboard.sh` installs everything (`sudo bash install-dashboard.sh <password>` in `/home/azureuser/dashboard`).
- Deploy = `scp` the file to `/home/azureuser/dashboard/` then `sudo install` to its destination; for the collector then `sudo systemctl start valheim-status.service` and check `journalctl -u valheim-status -n 20`.

## Facts learned by probing (trust these)
- Game process cmdline (`/proc/<MainPID>/cmdline`, NUL separated): `-name 1g49ye -port 2456 -world Vancouver Island -password hammerhead -modifier raids more -savedir /home/valheim/data -backups 4 -backupshort 7200 -backuplong 43200`. Defaults when absent: saveinterval 1800 s, public 0, crossplay off, max players 10. Never emit the password.
- `.fwl2` (newest `_main.N.fwl2` in the world dir): int32 length, int32 version(41), 7-bit-length-prefixed strings name, seedName, int64 seed, int64 uid, int32 worldGenVersion, then more. It also contains the text `preset combat_default:deathpenalty_default:resources_default:raids_more:portals_default` (regex it out for modifiers) and `eventrate 60`.
- `.db2` (newest `_main.N.db2`): bytes 0-3 int32 version (41), bytes 4-11 double netTime (seconds of world time; in-game day = floor(netTime/1800) + 1; currently ~96254 s = day 54). At byte offset 17 a gzip stream begins (`1f 8b 08`); decompress with `zlib.decompressobj(16 + zlib.MAX_WBITS)` (tolerates trailing data). Look for global keys `defeated_eikthyr`, `defeated_gdking` (Elder), `defeated_bonemass`, `defeated_dragon` (Moder), `defeated_goblinking` (Yagluth), `defeated_queen`, `defeated_fader` as plain ASCII in the decompressed bytes. If the decompressed block does not contain them, fall back to inference: raids `army_theelder` seen => Eikthyr+Elder defeated; `army_bonemass` => +Bonemass; `army_moder` => +Moder; `army_goblin` => +Yagluth; `army_seekers`/`army_gjall` => +Queen; ashlands raids => +Fader. Report `source: "keys"` or `"inferred"`.
- `*.chunk` files (same dir) hold ZDO data uncompressed. Counting little-endian uint32 occurrences of Valheim's stable hash of a prefab name gives a usable object count. Hash: `a=b=5381; for i in range(0,len(s),2): a=((a<<5)+a ^ ord(s[i])) & 0xffffffff; if i+1<len(s): b=((b<<5)+b ^ ord(s[i+1])) & 0xffffffff; return (a + b*1566083941) & 0xffffffff`. Verified counts now: portal_wood 2, bed 6, piece_workbench 12, Karve 2, Raft 2, piece_chest_wood 38, Player_tombstone 3, VikingShip 0. Only rescan when the newest `.fwl2` mtime changed (saves are every 30 min), it reads ~7 MB.
- Journal lines (MESSAGE field; timestamps from `__REALTIME_TIMESTAMP`):
  - death: `Got character ZDOID from <Name> : 0:0` (the existing code must NOT treat this as a join; currently it is skipped because the name is already online, keep that but also record a death).
  - raid: `Random event set:army_theelder` (name after the colon, may have no space). Labels: army_eikthyr "Eikthyr rallies the creatures of the forest", army_theelder "The forest is moving", army_bonemass "A foul smell from the swamp", army_moder "A cold wind blows from the mountains", army_goblin "The horde is attacking", army_seekers "They sought you out", army_gjall "What's that sound?", army_charred "The Ashlands are calling" (best effort), foresttrolls "The ground is shaking", blobs "You are being hunted", skeletons "Skeleton surprise", surtlings "There's a smell of sulfur in the air", wolves "You are being hunted", bats "You stirred the cauldron"; unknown names: show the raw name.
  - client version: `Network version check, their:40, mine:40`; their != mine => an outdated (or newer) client tried to join.
  - unknown Steam account: `Got connection SteamID <id>` where id is not in `state["steam_names"]` (names come from `Player history entry ...` lines printed at server start).
  - wrong password: `Peer <id> has wrong password` (already handled as kind "denied").
- Steam news: `https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/?appid=892970&count=10&maxlength=300` returns `appnews.newsitems[]` with `feedlabel`, `title`, `url`, `date` (epoch), `contents`. Keep only `feedlabel == "Community Announcements"` (official patch notes), top 5. Cache for an hour (`/var/lib/valheim-status/news.json`).
- Discord: `https://discord.com/api/v10/invites/QabVbWjMV?with_counts=true` returns `guild.name` ("existence is ARNAS"), `approximate_member_count` (64), `approximate_presence_count` (24 online). The server widget is disabled (403), so use these counts. Cache 10 minutes (`/var/lib/valheim-status/discord.json`). Invite link https://discord.gg/QabVbWjMV.
- NIC is `eth0`; `/sys/class/net/eth0/statistics/{rx,tx}_bytes`.
- Timers: `systemctl show valheim-update.timer -p NextElapseUSecRealtime --value` gives the next Steam check as a date string; convert to epoch (`date -d "<string>" +%s` works on the VM).
- Valheim's own rolling backups are directories `worlds_local/Vancouver Island_backup_auto-YYYYMMDD-HHMMSS`; next expected = newest + backup_short_s.
- Azure cost APIs return no rows for this credit subscription: the "Azure spend" idea is DROPPED.

## Data contract additions (collector writes, page reads)

`status.json` additions:
```
server.settings = {name, port, world, seed, max_players, public: bool, crossplay: bool, save_interval_s, backups, backup_short_s, backup_long_s,
                   modifiers: {combat, deathpenalty, resources, raids, portals}, password_protected: true}
server.uptime_30d_pct = float          # % of minute samples in the last 30 days where the server answered (sample field "o")
server.restarts = [{t, cause: "update"|"crash"|"manual"|"boot", version}]   # newest first, max 20. cause: "update" if autoupdate log has "updating" within 5 min before; "crash" if NRestarts grew; "boot" if machine uptime < 10 min at that time; else "manual"
server.next_update_check = epoch|null
server.next_backup = epoch|null
updates.history = [{t, from_build, to_build}]   # from /var/log/valheim-autoupdate.log "updating A -> B" lines, newest first
world.day = int|null
world.bosses = {source: "keys"|"inferred"|"unknown", eikthyr, elder, bonemass, moder, yagluth, queen, fader}  # bools
world.built = {portals, beds, workbenches, chests, karves, rafts, longships, tombstones, scanned_at}
world.growth_7d_bytes = int
world.raids = {count_7d, last: {t, name, label}|null, list: [{t, name, label, online: [names]}]}  # newest first, max 15
players.stats[] += {deaths, last_death: epoch|null, avg_ping_24h: ms|null, worst_ping_24h: ms|null}
players.together = [{a, b, seconds}]   # pairwise overlap of sessions over 30 days, a < b alphabetically, seconds > 0 only
players.heatmap = {busy: 7x24 floats 0..1, avg: 7x24 floats, weeks: n}   # index [weekday(Mon=0)][hour local time]; busy = fraction of samples with players > 0
players.records = {longest_session: {name, seconds, t}|null, biggest_gathering: {count, t}|null, longest_streak: {name, days, end_t}|null, most_deaths: {name, deaths}|null}
machine.net = {rx_bps, tx_bps}         # bytes per second over the last minute (delta of NIC counters / elapsed)
news = [{t, title, url}]
discord = {name, members, online, invite, fetched}
events kinds: existing join|leave|denied|start|update, plus death {name}, raid {name, label}, outdated {their, mine}, newcomer {steamid_suffix (last 4 digits only)}
```
`history.json` additions: every `day` and `week` entry gets `o` (1/0 online); `day` entries get `pg`: {name: ms} for players with a ping that minute. `samples.jsonl` entries gain `o`, `pg`, `rx`, `tx` (bytes counters), `w` (world bytes).

## Work split (three subagents, disjoint files)

### A. Collector (`valheim-status-collect.py`) -- everything in "Data contract additions"
Also: keep per-player death counts in `state["totals"][name]["deaths"]`; seed from existing events the first time like the session totals. Emit new events to the feed. Cache news/discord as described (network calls must have 5 s timeouts and never break the run). Restart causes need `state["restart_log"]`. World scan only when fwl2 mtime changed; store result in state. Keep runtime under 3 s normally (measure with `time`). Deploy to the VM, run it, check `journalctl -u valheim-status -n 30 --no-pager` for tracebacks, and print the new sections of status.json to prove them. Do not touch index.html, Caddyfile, alert script.

### B. Page (`index.html`) -- render everything above
Design already set: keep the palette, fonts and voice of the current page (charred timber, parchment, torch gold; Almendra display, Alegreya body; sentence-case, hall-keeper voice, "Existence is Pain"). Add, in this order after the existing facts strip:
1. Facts strip: add "uptime, 30 days" (%), and "day N in the world".
2. Under the tide chart: a second, shorter canvas "Ping to each Viking, last 24 hours" drawing one line per player from `history.day[].pg`, legend by name; hide when no ping data.
3. "Progress" band: the seven bosses as a row (Eikthyr, The Elder, Bonemass, Moder, Yagluth, The Queen, Fader) lit torch-gold when defeated, dim otherwise, with a one-line note "read from the world file" or "inferred from raids"; next to it the built counts (portals, beds, workbenches, chests, ships, tombstones).
4. "Raids" list (last 15: when, label, who was online) and "count this week".
5. "Vikings of the hall" table: add Deaths and Ping (avg / worst 24 h) columns; below it "Records" (four lines) and "Who plays together" as a small grid of names with hours in cells (only pairs > 0).
6. "When people are around" heat map: 7 rows (Mon..Sun) x 24 columns, cell shade = busy fraction, hover title shows "Tue 8 PM: someone here 60 % of weeks, avg 2.1". Note "N weeks of data".
7. Machine section: add network in/out (KB/s), world file growth this week.
8. "Restarts and updates": restarts list with cause (newest 10), update history, "next Steam check in X", "next world snapshot in X".
9. "How this world is set up": the settings as a dl (never a password; say "password protected").
10. Sidebar/aside additions: Discord card (name, online/members, invite link), "Patch notes" (5 items, external links), a "Notify me when someone joins" button using the Notification API (opt in, remembered in localStorage, fires when players.count rises between polls), and a "World snapshots" link to `/snapshots/` (owner only, separate login, see C).
Feed: render new event kinds (death "X died", raid "Raid: label", outdated "Someone tried to join with game version their vs ours", newcomer "A new Steam account (…1234) connected").
Keep the page one file, no libraries, responsive, `prefers-reduced-motion` respected. Test locally: write a `mock-status.json`/`mock-history.json` from the contract, serve the folder with `python -m http.server` and load with `?mock=1` support removed before deploy (or simply copy the mocks to `status.json`/`history.json` in a scratch folder). Deploy: scp to `/home/azureuser/dashboard/` and `sudo install -m 0644 ... /var/www/valheim/index.html`. Do not touch the collector.

### C. Hosting (`Caddyfile`, `install-dashboard.sh`, README section)
1. Serve `/home/valheim/backups/` at `/snapshots/` with directory listing (`file_server browse`), behind a SECOND basic_auth user `owner` whose password is passed to the installer as a 2nd argument (`install-dashboard.sh <viewer-pw> <owner-pw>`); the rest of the site keeps `viking`. Check that user `caddy` can read `/home/valheim/backups` (fix with group/ACL, not by opening /home/valheim world-wide beyond `o+x` traversal, and never touch `/home/valheim/data`). Generate a random 16-char owner password for this deployment with `openssl rand -base64 12`, print it in your final report.
2. Redeploy Caddy config (`caddy validate` then `systemctl reload caddy`), verify: 401 without auth on `/snapshots/`, 200 with owner creds, 401 with viking creds, and the main page still 200 with viking.
3. Update `README.md` (section "Dashboard") for the snapshots route, the second user, and the new contents list. Do not touch index.html or the collector.

## Integration (main agent, after A-C)
Reload the live page, screenshot every new section, fix contract mismatches, update memory notes.
