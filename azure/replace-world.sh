#!/usr/bin/env bash
# replace-world.sh -- run on the VM:  sudo bash /home/azureuser/replace-world.sh
# Replaces the live world (WORLD_NAME, from /etc/valheim-server.env) with the copy uploaded to
# /home/azureuser/$WORLD_NAME.
# Steps: verify upload against /home/azureuser/world.sha256 -> graceful stop (saves) -> archive current world -> install -> start.
set -euo pipefail
if [ -f /etc/valheim-server.env ]; then
  set -a; . /etc/valheim-server.env; set +a
fi
: "${WORLD_NAME:?set WORLD_NAME in /etc/valheim-server.env}"
SRC="/home/azureuser/$WORLD_NAME"
DST_DIR="/home/valheim/data/worlds_local"
DST="$DST_DIR/$WORLD_NAME"
SEED="uEA2lEA5YC"
MANIFEST=/home/azureuser/world.sha256
TS=$(date +%Y%m%d-%H%M%S)

echo "== preflight: uploaded copy"
[ -d "$SRC" ] || { echo "ERROR: $SRC missing (scp failed?)"; exit 1; }
[ -f "$MANIFEST" ] || { echo "ERROR: $MANIFEST missing"; exit 1; }
expected=$(grep -cve '^\s*$' "$MANIFEST"); n=$(ls -1 "$SRC" | wc -l)
[ "$n" -eq "$expected" ] || { echo "ERROR: manifest lists $expected files, upload has $n"; ls -la "$SRC"; exit 1; }
fwl=$(ls -1 "$SRC"/_main.*.fwl2 | head -n 1); [ -n "$fwl" ] || { echo "ERROR: no _main.*.fwl2 in upload"; exit 1; }
grep -aq "$SEED" "$fwl" || { echo "ERROR: seed $SEED not found in $fwl (wrong world?)"; exit 1; }
( cd "$SRC" && sha256sum -c --quiet "$MANIFEST" ) && echo "OK  $n files match manifest"
newsave=$(basename "$fwl" | sed -E 's/_main\.([0-9]+)\.fwl2/\1/')
echo "uploaded copy is save number $newsave"

echo "== stop server (graceful; saves the current world first)"
if systemctl is-active --quiet valheim; then systemctl stop valheim; echo "stopped"; else echo "was not running"; fi
systemctl is-active --quiet valheim && { echo "ERROR: service still active"; exit 1; }

echo "== archive current world"
if [ -d "$DST" ]; then
  cursave=$(ls -1 "$DST"/_main.*.fwl2 2>/dev/null | head -n 1 | sed -E 's/.*_main\.([0-9]+)\.fwl2/\1/')
  mkdir -p /home/valheim/backups
  tar -czf "/home/valheim/backups/world-pre-replace-$TS.tgz" -C "$DST_DIR" "$WORLD_NAME"
  mv "$DST" "$DST_DIR/${WORLD_NAME}_replaced-$TS"
  chown -R valheim:valheim /home/valheim/backups
  echo "current world (save ${cursave:-?}) archived: /home/valheim/backups/world-pre-replace-$TS.tgz"
  echo "and kept on disk as: $DST_DIR/${WORLD_NAME}_replaced-$TS"
else
  echo "no current world dir; nothing to archive"
fi

echo "== install uploaded world (save $newsave)"
mv "$SRC" "$DST"
chown -R valheim:valheim /home/valheim/data
chmod 755 "$DST"; chmod 644 "$DST"/*
( cd "$DST" && sha256sum * > /home/azureuser/world-before.sha256 )
echo "$newsave" > /home/azureuser/migrated-save-number
ls -la "$DST" | grep -v '\.chunk$'
touch /home/valheim/data/.world-migrated; chown valheim:valheim /home/valheim/data/.world-migrated

echo "== start server"
START_AT=$(date '+%Y-%m-%d %H:%M:%S')
systemctl start valheim
sleep 75
echo "is-active: $(systemctl is-active valheim)"
journalctl -u valheim --no-pager --since "$START_AT" | grep -E 'Valheim version|LoadChunks - Starting|Game server connected|Registering lobby' | sed 's/.*\]: /  /'
echo "== OK. World replaced with save $newsave."
