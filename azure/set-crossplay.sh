#!/usr/bin/env bash
# set-crossplay.sh on|off  -- run on the VM:  sudo bash /home/azureuser/set-crossplay.sh off
# Edits the -crossplay flag in the systemd unit, restarts the server (graceful stop saves the world), then prints evidence.
set -euo pipefail
UNIT=/etc/systemd/system/valheim.service
mode="${1:-}"
case "$mode" in
  off) sed -i 's/ -crossplay//' "$UNIT" ;;
  on)  grep -q -- '-crossplay' "$UNIT" || sed -i 's/ -password "hammerhead"/ -password "hammerhead" -crossplay/' "$UNIT" ;;
  *) echo "usage: $0 on|off"; exit 2 ;;
esac
echo "== ExecStart now:"; grep '^ExecStart=' "$UNIT"
echo "== crossplay flag present (0 = off): $(grep -c -- '-crossplay' "$UNIT" || true)"
echo "== last player/session lines before restart:"
journalctl -u valheim --no-pager | grep -iE 'is active with|Got connection|Player joined' | tail -n 2 || true
systemctl daemon-reload
RESTART_AT=$(date '+%Y-%m-%d %H:%M:%S')
echo "== restarting at $RESTART_AT"
systemctl restart valheim
sleep 75
echo "== is-active: $(systemctl is-active valheim || true)"
echo "== stop/save lines:"
journalctl -u valheim --no-pager --since "$RESTART_AT" | grep -iE 'world saved|saving|shutdown' | head -n 4 || true
echo "== UDP listeners:"
ss -ulnp | grep -E ':(2456|2457|2458)\s' || echo "(none yet)"
echo "== startup lines since restart:"
journalctl -u valheim --no-pager --since "$RESTART_AT" | grep -iE 'valheim version|LoadChunks - Starting|game server connected|playfab|join code|is active with|public' | grep -viE 'referenced script|Steam Public' | head -n 12 || true
echo "== playfab mentions since restart (want 0 when off): $(journalctl -u valheim --no-pager --since "$RESTART_AT" | grep -ci playfab || true)"
