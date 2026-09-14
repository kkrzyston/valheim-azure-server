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
BACKUP_DIR=/home/valheim/backups
LOGFILE=/var/log/valheim-autoupdate.log
LOCKFILE=/var/lock/valheim-world.lock
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

# Authoritative player check: the collector's status.json, refreshed every
# 60s by an A2S_INFO query to 127.0.0.1:2457 (valheim-status-collect.py) --
# this is the real live count, unlike the journal signal below. Fails
# CLOSED: any problem reading it reports "unknown", never "0". Path is
# overridable via VALHEIM_STATUS_FILE so this can be unit-tested off-box.
players_check() {
  local status_file="${VALHEIM_STATUS_FILE:-/var/www/valheim/status.json}"
  local freshness_s=180
  local py
  py=$(python3 - "$status_file" "$freshness_s" <<'PYEOF'
import json, sys, time

path, freshness = sys.argv[1], int(sys.argv[2])


def unknown(reason):
    print("unknown|%s|" % reason)
    sys.exit(0)


try:
    with open(path) as f:
        data = json.load(f)
except FileNotFoundError:
    unknown("status.json missing")
except Exception as e:
    unknown("status.json unreadable (%s)" % type(e).__name__)

try:
    generated = int(data["generated"])
    count = int(data["players"]["count"])
    names = [str(p.get("name", "?")) for p in data["players"].get("online", [])]
except Exception as e:
    unknown("status.json missing expected fields (%s)" % type(e).__name__)

age = int(time.time() - generated)
if age > freshness or age < -5:
    unknown("status.json is %ds old (stale budget %ds)" % (age, freshness))

print("%d|status.json, %ds old|%s" % (count, age, ",".join(names)))
PYEOF
)
  local kind reason names
  IFS='|' read -r kind reason names <<<"$py"

  # Secondary signal only: the server logs " Connections N ZDOS:..." roughly
  # every 10 minutes, so this can be up to ~10 min stale -- it must never be
  # used to turn "unknown" into a number, and never used alone. It only ever
  # pushes the reported count UP (more cautious) when it disagrees with the
  # authoritative status.json reading.
  local inv jn=0
  inv=$(systemctl show valheim -p InvocationID --value 2>/dev/null || true)
  if [ -n "$inv" ]; then
    jn=$(journalctl --no-pager "_SYSTEMD_INVOCATION_ID=$inv" 2>/dev/null \
          | grep -oE 'Connections [0-9]+' | tail -n 1 | grep -oE '[0-9]+' || true)
    [ -n "$jn" ] || jn=0
  fi

  if [ "$kind" = "unknown" ]; then
    echo "unknown|$reason (journal secondary signal: $jn)|"
    return
  fi

  if [ "$jn" -gt "$kind" ] 2>/dev/null; then
    echo "$jn|$reason, but journal (up to ~10 min stale) last saw $jn -- taking the higher, more cautious count|$names"
  else
    echo "$kind|$reason|$names"
  fi
}

# Same tar -tzf + size checks used for the off-site copy in
# valheim-offsite-backup.sh, tuned for the world tarball backup.sh makes.
verify_tarball() {
  # $1 = path, $2 = minimum plausible size in bytes, $3 = minimum entry count.
  local f="$1" min_size="$2" min_entries="$3" listing rc entries size
  listing=$(tar -tzf "$f" 2>&1)
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "tar -tzf failed (exit $rc): $(printf '%s\n' "$listing" | tail -n1)"
    return 1
  fi
  entries=$(printf '%s\n' "$listing" | grep -c .)
  if [ "$entries" -lt "$min_entries" ]; then
    echo "only $entries entries (expected >= $min_entries; the world dir normally has ~19); treating as truncated"
    return 1
  fi
  size=$(stat -c%s "$f" 2>/dev/null || echo 0)
  if [ "$size" -lt "$min_size" ]; then
    echo "only $size bytes (expected >= $min_size); treating as truncated"
    return 1
  fi
  echo "$entries entries, $size bytes"
  return 0
}

# Shared world lock (PLAN-v5): autoupdate, the off-site backup, and the
# restart executor all serialize around world saves/stops so nobody's tar
# or stop/start ever overlaps another's. -w 60: short budget, this timer
# retries for free again in 30 min, so a lock miss is never an emergency.
#
# fd-based (exec 9>lock; flock -w N 9) rather than PLAN-v5's literal
# "exec env ... flock ... "$0" "$@"" self-re-invocation one-liner: that
# form replaces this process image via exec, so if flock times out it exits
# silently with no way to run our own log() call -- which is exactly what
# "log and exit 0" below requires. The fd form holds the lock for the rest
# of this script's life (until the process exits and fd 9 closes) with no
# re-exec, no argv/$0 re-resolution, and no loss of our logging on timeout.
if ! : >>"$LOCKFILE" 2>/dev/null; then
  log "could not access world lock file $LOCKFILE; deferring, timer retries in 30 min"
  exit 0
fi
exec 9>"$LOCKFILE"
if ! flock -w 60 9; then
  log "could not acquire world lock within 60s (another world-touching job is running); deferring, timer retries in 30 min"
  exit 0
fi

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

if [ "$FORCE" = 1 ]; then
  log "forced update requested ($cur -> $new); skipping player check"
else
  result=$(players_check)
  IFS='|' read -r n why names <<<"$result"
  if [ "$n" = "unknown" ]; then
    log "deferring update ($cur -> $new): player count is unknown ($why); treating as players may be online"
    exit 0
  elif [ "$n" != "0" ]; then
    log "deferring update ($cur -> $new): $n player(s) online${names:+ ($names)} [$why]"
    exit 0
  else
    log "no players online [$why]; proceeding with update ($cur -> $new)"
  fi
fi

log "backing up before update ($cur -> $new)"
before_ts=$(date +%s)
backup_rc=0
/home/valheim/backup.sh >>"$LOGFILE" 2>&1 || backup_rc=$?
if [ "$backup_rc" -ne 0 ]; then
  log "ERROR: backup.sh exited $backup_rc; aborting update, service left untouched"
  exit 1
fi

tgz=$(ls -1t "$BACKUP_DIR"/*.tgz 2>/dev/null | head -n1)
if [ -z "${tgz:-}" ] || [ ! -f "$tgz" ]; then
  log "ERROR: no tarball found in $BACKUP_DIR after backup.sh; aborting update, service left untouched"
  exit 1
fi
tgz_mtime=$(stat -c%Y "$tgz" 2>/dev/null || echo 0)
if [ "$tgz_mtime" -lt "$before_ts" ]; then
  log "ERROR: newest tarball $tgz predates this run (mtime $tgz_mtime < $before_ts); backup.sh did not produce a fresh snapshot; aborting update, service left untouched"
  exit 1
fi

verify_msg=$(verify_tarball "$tgz" 102400 3)
verify_rc=$?
if [ "$verify_rc" -ne 0 ]; then
  log "ERROR: backup verification failed for $tgz: $verify_msg; aborting update, service left untouched"
  exit 1
fi
sha=$(sha256sum "$tgz" 2>/dev/null | awk '{print $1}')
log "backup verified: $tgz ($verify_msg, sha256 $sha)"

log "updating $cur -> $new (players checked, force: $FORCE)"
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
