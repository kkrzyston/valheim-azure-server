#!/usr/bin/env bash
# verify-valheim.sh -- run:  sudo bash /home/azureuser/verify-valheim.sh
if [ -f /etc/valheim-server.env ]; then
  set -a; . /etc/valheim-server.env; set +a
fi
: "${WORLD_NAME:?set WORLD_NAME in /etc/valheim-server.env}"
SEED="uEA2lEA5YC"
W="/home/valheim/data/worlds_local/$WORLD_NAME"
LOG="journalctl -u valheim --no-pager --since=-2h"

echo "== 1. service (want: active)"
systemctl is-active valheim
systemctl status valheim --no-pager -l | head -n 12

echo; echo "== 2. UDP listeners (want :2456 and :2457)"
ss -ulnp | grep -E ':(2456|2457|2458)\s' || echo "!! nothing on 2456-2458 yet (re-run in 60s)"

echo; echo "== 3. key log lines"
$LOG | grep -iE "valheim version|load world|loadchunks|world generator|new world|game server connected|opened playfab|registered with join code|is active with|error|exception" | grep -v "referenced script" | tail -n 40

echo; echo "== 4. world load evidence in log (want: many zdos from 15 chunks; 0 = fresh world)"
load=$($LOG | grep "ZDOMan.LoadChunks - Starting" | tail -n 1)
if [ -n "$load" ]; then
  echo "$load" | sed 's/.*ZDOMan/ZDOMan/'
  if echo "$load" | grep -qE "load 0 zdos|from 0 Chunks"; then echo "!!  loaded ZERO zdos/chunks -> fresh world, STOP and investigate"; else echo "OK  migrated world data loaded"; fi
else
  echo "!!  no ZDOMan.LoadChunks line yet (server still starting?)"
fi
if $LOG | grep -qiE "missing world|creating new|new world"; then echo "!!  log mentions creating a NEW world -> STOP and investigate"; fi

echo; echo "== 5. on-disk world"
ls -la "$W" | grep -v '\.chunk$'; echo "file count: $(ls -1 "$W" | wc -l)  (19 on migration day)"
latest=$(ls -1 "$W"/_main.*.fwl2 2>/dev/null | sed -E 's/.*_main\.([0-9]+)\.fwl2/\1/' | sort -n | tail -n1)
MIG=$(cat /home/azureuser/migrated-save-number 2>/dev/null || echo 54)
echo "latest save number: ${latest:-none} (migrated copy was $MIG)"
if [ "${latest:-0}" = "$MIG" ]; then
  ( cd "$W" && sha256sum -c --quiet /home/azureuser/world-before.sha256 && echo "OK  all migrated files byte-identical to pre-start" )
else
  echo "OK  world has been saved by the server since migration; last save line:"
  $LOG | grep -E "World save \(5/5\) done" | tail -n 1 | sed 's/.*\]: /    /'
fi

echo; echo "== 6. every .fwl2 must carry the migrated seed"
for f in "$W"/*.fwl2; do if grep -aq "$SEED" "$f"; then echo "OK  $f"; else echo "!!  $f has a DIFFERENT seed -> fresh world generated"; fi; done
echo "-- world dirs (want '$WORLD_NAME' plus its _backup_ dirs only):"; ls -1 /home/valheim/data/worlds_local

echo; echo "== 7. Unity Player.log tail (fallback)"
tail -n 15 /home/valheim/.config/unity3d/IronGate/Valheim/Player.log 2>/dev/null || echo "(no Player.log)"
