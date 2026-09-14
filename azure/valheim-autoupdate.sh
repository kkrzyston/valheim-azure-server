#!/usr/bin/env bash
# valheim-autoupdate.sh -- keep the Valheim dedicated server on Steam's current public build.
#
#   sudo /usr/local/sbin/valheim-autoupdate.sh          # update only if nobody is connected
#   sudo /usr/local/sbin/valheim-autoupdate.sh --force  # manual only: update even with players online
#
# Run by valheim-update.timer every 30 min. Nothing ever schedules --force (owner's rule: never force an update).
# Logs to the journal (unit tag "valheim-autoupdate") and /var/log/valheim-autoupdate.log.
set -uo pipefail

APPID=896660
SERVER=/home/valheim/server
MANIFEST="$SERVER/steamapps/appmanifest_${APPID}.acf"
STEAMCMD=/usr/games/steamcmd
LOGFILE=/var/log/valheim-autoupdate.log
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

log() { echo "$(date '+%F %T') $*" | tee -a "$LOGFILE" | logger -t valheim-autoupdate; }

installed_build() { grep -oE '"buildid"\s+"[0-9]+"' "$MANIFEST" 2>/dev/null | grep -oE '[0-9]+'; }

remote_build() {
  # app_info_print sometimes returns nothing on the first call after a cache refresh; try up to 3 times.
  for _ in 1 2 3; do
    local out
    out=$(sudo -u valheim -H "$STEAMCMD" +login anonymous +app_info_update 1 +app_info_print "$APPID" +quit 2>/dev/null \
      | awk '/"branches"/{b=1} b && /"public"/{p=1} p && /"buildid"/{gsub(/"/,"",$2); print $2; exit}')
    if [[ "$out" =~ ^[0-9]+$ ]]; then echo "$out"; return 0; fi
    sleep 10
  done
  return 1
}

players() {
  # The server logs " Connections N ZDOS:..." every 10 minutes; take the newest line since the current start.
  local inv
  inv=$(systemctl show valheim -p InvocationID --value)
  journalctl --no-pager "_SYSTEMD_INVOCATION_ID=$inv" 2>/dev/null \
    | grep -oE 'Connections [0-9]+' | tail -n 1 | grep -oE '[0-9]+' || echo 0
}

if ! systemctl is-active --quiet valheim; then
  log "valheim.service is not active; leaving it alone (systemctl start runs the Steam update itself)"
  exit 0
fi

cur=$(installed_build)
new=$(remote_build) || { log "could not read Steam's public build id; will retry next run"; exit 0; }

if [ "$cur" = "$new" ]; then
  log "up to date (build $cur)"
  exit 0
fi

n=$(players)
if [ "$n" != "0" ] && [ "$FORCE" = 0 ]; then
  log "update available ($cur -> $new) but $n player(s) connected; deferring"
  exit 0
fi

log "updating $cur -> $new (players: $n, force: $FORCE)"
/home/valheim/backup.sh >>"$LOGFILE" 2>&1 || log "warning: backup.sh failed, continuing"
# 'stop' sends SIGINT so the server saves the world; 'start' runs steamcmd app_update via ExecStartPre.
if ! systemctl restart valheim; then
  log "ERROR: systemctl restart valheim failed: $(systemctl status valheim --no-pager -l | tail -n 5)"
  exit 1
fi
sleep 20
after=$(installed_build)
if [ "$after" = "$new" ]; then
  log "updated OK; now on build $after ($(systemctl is-active valheim))"
else
  log "ERROR: restart done but installed build is $after, expected $new"
  exit 1
fi
