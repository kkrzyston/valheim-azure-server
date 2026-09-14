#!/usr/bin/env bash
# migrate-world.sh -- run ONCE on the VM:  sudo bash /home/azureuser/migrate-world.sh
set -euo pipefail
if [ -f /etc/valheim-server.env ]; then
  set -a; . /etc/valheim-server.env; set +a
fi
: "${WORLD_NAME:?set WORLD_NAME in /etc/valheim-server.env}"
SRC="/home/azureuser/$WORLD_NAME"
DST_DIR="/home/valheim/data/worlds_local"
DST="$DST_DIR/$WORLD_NAME"
SEED="uEA2lEA5YC"

echo "== preflight"
if systemctl is-active --quiet valheim; then echo "ERROR: valheim.service is running; refusing"; exit 1; fi
if [ ! -f /home/valheim/.setup-done ]; then echo "ERROR: cloud-init setup not finished"; exit 1; fi
if [ ! -d "$SRC" ]; then echo "ERROR: $SRC missing (scp failed?)"; exit 1; fi
if [ -e "$DST" ]; then echo "ERROR: $DST already exists; refusing to overwrite"; exit 1; fi
if [ ! -f "$SRC/_main.54.fwl2" ]; then echo "ERROR: _main.54.fwl2 missing in upload"; exit 1; fi
if ! grep -aq "$SEED" "$SRC/_main.54.fwl2"; then echo "ERROR: seed $SEED not found in uploaded fwl2"; exit 1; fi
n=$(ls -1 "$SRC" | wc -l)
if [ "$n" -ne 19 ]; then echo "ERROR: expected 19 files, found $n"; ls -la "$SRC"; exit 1; fi

echo "== checksum verification against local manifest (all 19 must say OK)"
( cd "$SRC" && sha256sum -c /home/azureuser/world.sha256 )

echo "== move into place + ownership"
install -d -o valheim -g valheim -m 0755 "$DST_DIR"
mv "$SRC" "$DST"
chown -R valheim:valheim /home/valheim/data
chmod 755 "$DST"
chmod 644 "$DST"/*
( cd "$DST" && sha256sum * > /home/azureuser/world-before.sha256 )
ls -la "$DST"

echo "== arm the service (satisfies ConditionPathExists in valheim.service)"
touch /home/valheim/data/.world-migrated
chown valheim:valheim /home/valheim/data/.world-migrated
echo "== OK. World in place. Next: sudo systemctl start valheim"
