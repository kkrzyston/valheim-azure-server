#!/usr/bin/env bash
# valheim-offsite-backup.sh
#
# Off-site backup of the configured world (WORLD_NAME, from
# /etc/valheim-server.env -- see backup.sh, which this script calls, for
# where that name is actually baked into the tar source path) plus
# admin/permitted/banned lists, to Azure Blob Storage, using the VM's
# system-assigned managed
# identity (no secrets on disk). Runs as root, via valheim-offsite.timer
# (daily 03:45 local) or by hand:
#   sudo /usr/local/sbin/valheim-offsite-backup.sh
#
# Never touches valheim.service, never restarts/reboots anything.
#
# Writes:
#   /var/log/valheim-offsite.log              - append-only run log
#   /var/lib/valheim-status/offsite.json       - status contract (0644),
#       merged into status.json by the collector as status.backup.offsite
#
# Also prunes /home/valheim/backups to the newest 10 .tgz files (off-site
# retention in the "worlds" container is 30 days via a lifecycle policy,
# so local disk does not need to hold as much).

set -uo pipefail

if [ -f /etc/valheim-server.env ]; then
  set -a; . /etc/valheim-server.env; set +a
fi
: "${OFFSITE_ACCOUNT:?set OFFSITE_ACCOUNT in /etc/valheim-server.env}"
OFFSITE_CONTAINER="worlds"
API_VERSION="2021-08-06"

BACKUP_DIR="/home/valheim/backups"
BACKUP_SCRIPT="/home/valheim/backup.sh"
DATA_DIR="/home/valheim/data"
LOG="/var/log/valheim-offsite.log"
STATE_DIR="/var/lib/valheim-status"
STATUS_FILE="$STATE_DIR/offsite.json"
LOCKFILE="/var/lock/valheim-world.lock"
RETENTION_DAYS=30
KEEP_LOCAL=10
TS="$(date +%Y%m%d-%H%M%S)"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

mkdir -p "$STATE_DIR"
touch "$LOG"

log() {
  printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S %Z')" "$*" >>"$LOG"
}

# Same tar -tzf + size checks valheim-autoupdate.sh runs on its pre-update
# backup, applied here to whichever archive is about to be uploaded as the
# canonical off-site copy.
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
    echo "only $entries entries (expected >= $min_entries); treating as truncated"
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

export OFFSITE_ACCOUNT OFFSITE_CONTAINER
export OFFSITE_NOW="$(date +%s)"
export OFFSITE_LAST_ERROR=""
export OFFSITE_UPLOAD_OK="0"
export OFFSITE_STATUS_FILE="$STATUS_FILE"
export OFFSITE_RETENTION_DAYS="$RETENTION_DAYS"
export OFFSITE_LIST_XML=""

log "=== offsite backup run starting ==="

# ---- 0. shared world lock (PLAN-v5) ----
# -w 900: this is the nightly backup, it must not skip -- give it a generous
# budget rather than a silent miss. Held for the rest of the script's life
# (fd 9 stays open until exit), so it covers backup.sh's tar of the live
# world dir below: that tar is what becomes the canonical off-site copy, and
# a tar that overlaps a world save would silently corrupt it. See
# valheim-autoupdate.sh for why this is fd-based rather than the
# self-re-exec one-liner (it would lose the ability to log a reason here).
if ! : >>"$LOCKFILE" 2>/dev/null; then
  export OFFSITE_LAST_ERROR="cannot access world lock file $LOCKFILE"
  log "ERROR: $OFFSITE_LAST_ERROR"
else
  exec 9>"$LOCKFILE"
  if ! flock -w 900 9; then
    export OFFSITE_LAST_ERROR="could not acquire world lock within 900s; backup skipped this run"
    log "ERROR: $OFFSITE_LAST_ERROR"
  else
    log "world lock acquired"
  fi
fi

# ---- 1. fresh world tarball via the existing backup script ----
if [ -n "$OFFSITE_LAST_ERROR" ]; then
  log "skipping backup.sh: $OFFSITE_LAST_ERROR"
  world_tgz_name=""
  world_tgz_path=""
else
  before_list=$(ls -1 "$BACKUP_DIR" 2>/dev/null | sort || true)
  backup_rc=0
  "$BACKUP_SCRIPT" >>"$LOG" 2>&1 || backup_rc=$?
  after_list=$(ls -1 "$BACKUP_DIR" 2>/dev/null | sort || true)
  world_tgz_name=$(comm -13 <(printf '%s\n' "$before_list") <(printf '%s\n' "$after_list") | grep '\.tgz$' | head -n1 || true)
  world_tgz_path="$BACKUP_DIR/$world_tgz_name"

  if [ "$backup_rc" -ne 0 ]; then
    export OFFSITE_LAST_ERROR="backup.sh exited $backup_rc"
    log "ERROR: $OFFSITE_LAST_ERROR"
  elif [ -z "$world_tgz_name" ] || [ ! -f "$world_tgz_path" ]; then
    export OFFSITE_LAST_ERROR="backup.sh did not produce a new .tgz"
    log "ERROR: $OFFSITE_LAST_ERROR"
  else
    # backup.sh may run with different effective ownership than valheim;
    # the backups dir is exposed read-only at /snapshots/ via a caddy ACL.
    chown valheim:valheim "$world_tgz_path" 2>>"$LOG" || true
    chmod 0644 "$world_tgz_path" 2>>"$LOG" || true
    log "fresh world tarball: $world_tgz_name ($(du -h "$world_tgz_path" 2>/dev/null | cut -f1))"

    verify_out=$(verify_tarball "$world_tgz_path" 102400 3)
    verify_rc=$?
    if [ "$verify_rc" -ne 0 ]; then
      export OFFSITE_LAST_ERROR="world tarball failed verification: $verify_out (left in place at $world_tgz_path for inspection, not uploaded)"
      log "ERROR: $OFFSITE_LAST_ERROR"
    else
      log "world tarball verified: $verify_out"
    fi
  fi
fi

# ---- 2. bundle admin/permitted/banned lists into a second small archive ----
lists_tgz_name="world-lists-$TS.tgz"
lists_tgz_path="$BACKUP_DIR/$lists_tgz_name"
if [ -z "$OFFSITE_LAST_ERROR" ]; then
  if tar -czf "$lists_tgz_path" -C "$DATA_DIR" adminlist.txt permittedlist.txt bannedlist.txt 2>>"$LOG"; then
    chown valheim:valheim "$lists_tgz_path"
    chmod 0644 "$lists_tgz_path"
    log "bundled admin lists: $lists_tgz_name"

    verify_out=$(verify_tarball "$lists_tgz_path" 1 1)
    verify_rc=$?
    if [ "$verify_rc" -ne 0 ]; then
      export OFFSITE_LAST_ERROR="lists tarball failed verification: $verify_out (left in place at $lists_tgz_path for inspection, not uploaded)"
      log "ERROR: $OFFSITE_LAST_ERROR"
    else
      log "lists tarball verified: $verify_out"
    fi
  else
    export OFFSITE_LAST_ERROR="failed to bundle admin/permitted/banned lists"
    log "ERROR: $OFFSITE_LAST_ERROR"
  fi
fi

# ---- 3. IMDS token for Azure Storage ----
token=""
if [ -z "$OFFSITE_LAST_ERROR" ]; then
  token_json=$(curl -s -m 10 -H "Metadata:true" \
    "http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fstorage.azure.com%2F")
  token=$(printf '%s' "$token_json" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('access_token', ''))
except Exception:
    print('')
")
  if [ -z "$token" ]; then
    export OFFSITE_LAST_ERROR="failed to obtain IMDS token"
    log "ERROR: $OFFSITE_LAST_ERROR ($token_json)"
  fi
fi

upload_blob() {
  # $1 = local file path, $2 = destination blob name
  local filepath="$1" blobname="$2" size resp_file
  size=$(stat -c%s "$filepath")
  resp_file="$WORKDIR/resp_$blobname"
  curl -s -o "$resp_file" -w '%{http_code}' \
    -X PUT \
    -H "Authorization: Bearer $token" \
    -H "x-ms-blob-type: BlockBlob" \
    -H "x-ms-version: $API_VERSION" \
    -H "Content-Length: $size" \
    --data-binary "@$filepath" \
    "https://${OFFSITE_ACCOUNT}.blob.core.windows.net/${OFFSITE_CONTAINER}/${blobname}"
}

# ---- 4. upload world tarball, then lists tarball ----
if [ -z "$OFFSITE_LAST_ERROR" ]; then
  code=$(upload_blob "$world_tgz_path" "$world_tgz_name")
  if [ "$code" = "201" ]; then
    log "uploaded $world_tgz_name (HTTP $code)"
  else
    export OFFSITE_LAST_ERROR="world upload HTTP $code: $(tr -d '\n' <"$WORKDIR/resp_$world_tgz_name" 2>/dev/null | head -c 300)"
    log "ERROR: $OFFSITE_LAST_ERROR"
  fi
fi

if [ -z "$OFFSITE_LAST_ERROR" ]; then
  code=$(upload_blob "$lists_tgz_path" "$lists_tgz_name")
  if [ "$code" = "201" ]; then
    log "uploaded $lists_tgz_name (HTTP $code)"
    export OFFSITE_UPLOAD_OK="1"
  else
    export OFFSITE_LAST_ERROR="lists upload HTTP $code: $(tr -d '\n' <"$WORKDIR/resp_$lists_tgz_name" 2>/dev/null | head -c 300)"
    log "ERROR: $OFFSITE_LAST_ERROR"
  fi
fi

# ---- 5. list the container (best effort, even after a failure) ----
if [ -n "$token" ]; then
  list_xml_file="$WORKDIR/list.xml"
  curl -s -m 20 \
    -H "Authorization: Bearer $token" \
    -H "x-ms-version: $API_VERSION" \
    "https://${OFFSITE_ACCOUNT}.blob.core.windows.net/${OFFSITE_CONTAINER}?restype=container&comp=list" \
    -o "$list_xml_file"
  export OFFSITE_LIST_XML="$list_xml_file"
fi

# ---- 6. write offsite.json (contract: last_attempt, last_success, last_error,
#          account, container, blobs, bytes, newest_blob, retention_days) ----
python3 <<'PYEOF'
import json, os
import xml.etree.ElementTree as ET
import email.utils as eu

status_file = os.environ["OFFSITE_STATUS_FILE"]
account = os.environ["OFFSITE_ACCOUNT"]
container = os.environ["OFFSITE_CONTAINER"]
now = int(os.environ["OFFSITE_NOW"])
last_error = os.environ.get("OFFSITE_LAST_ERROR") or None
upload_ok = os.environ.get("OFFSITE_UPLOAD_OK") == "1"
retention_days = int(os.environ["OFFSITE_RETENTION_DAYS"])
list_xml_file = os.environ.get("OFFSITE_LIST_XML", "")

prev = {}
try:
    with open(status_file) as f:
        prev = json.load(f)
except Exception:
    prev = {}

blobs = 0
total_bytes = 0
newest = None
newest_t = None
if list_xml_file and os.path.exists(list_xml_file):
    try:
        tree = ET.parse(list_xml_file)
        for b in tree.getroot().findall(".//Blob"):
            name = b.findtext("Name")
            props = b.find("Properties")
            size = int((props.findtext("Content-Length") or "0"))
            lm = props.findtext("Last-Modified")
            blobs += 1
            total_bytes += size
            if lm:
                t = eu.parsedate_to_datetime(lm)
                if newest_t is None or t > newest_t:
                    newest_t = t
                    newest = name
    except Exception as e:
        if last_error is None:
            last_error = "failed to parse container listing: %s" % e

result = {
    "last_attempt": now,
    "last_success": now if upload_ok else prev.get("last_success"),
    "last_error": last_error,
    "account": account,
    "container": container,
    "blobs": blobs,
    "bytes": total_bytes,
    "newest_blob": newest,
    "retention_days": retention_days,
}

tmp = status_file + ".tmp"
with open(tmp, "w") as f:
    json.dump(result, f, indent=2)
os.replace(tmp, status_file)
os.chmod(status_file, 0o644)
print(json.dumps(result, indent=2))
PYEOF

status_rc=$?
if [ "$status_rc" -ne 0 ]; then
  log "ERROR: failed to write $STATUS_FILE (python exit $status_rc)"
fi

# ---- 7. prune local backups dir to the newest 10 tarballs ----
mapfile -t all_tgz < <(ls -1t "$BACKUP_DIR"/*.tgz 2>/dev/null)
if [ "${#all_tgz[@]}" -gt "$KEEP_LOCAL" ]; then
  for f in "${all_tgz[@]:$KEEP_LOCAL}"; do
    rm -f "$f" && log "pruned local backup $f"
  done
fi

if [ -n "$OFFSITE_LAST_ERROR" ]; then
  log "=== run finished with error: $OFFSITE_LAST_ERROR ==="
  exit 1
fi
log "=== run finished OK ==="
exit 0
