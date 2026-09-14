#!/usr/bin/env bash
# status-valheim.sh -- run on the VM:  sudo bash /home/azureuser/status-valheim.sh
# Service health, memory trend (3 samples over 30s), errors since last start, players, disk, OOM/restart events.
PID=$(systemctl show valheim -p MainPID --value)
START=$(systemctl show valheim -p ActiveEnterTimestamp --value)
echo "== service"
echo "state: $(systemctl is-active valheim)   pid: $PID   since: $START"
echo "restarts (NRestarts, auto-restarts after failure): $(systemctl show valheim -p NRestarts --value)"
echo "result of last run: $(systemctl show valheim -p Result --value)"

echo; echo "== memory trend (RSS of valheim_server, 3 samples 15s apart)"
for i in 1 2 3; do
  rss=$(awk '/VmRSS/ {print $2}' /proc/$PID/status 2>/dev/null)
  cg=$(systemctl show valheim -p MemoryCurrent --value)
  printf '  %s  RSS %6.0f MB   cgroup %6.0f MB   threads %s\n' "$(date +%H:%M:%S)" "$((rss/1024))" "$((cg/1024/1024))" "$(awk '/Threads/ {print $2}' /proc/$PID/status)"
  [ "$i" -lt 3 ] && sleep 15
done
echo "  system:"; free -m | awk 'NR==1{print "   "$0} NR==2{print "   "$0} NR==3{print "   "$0}'

echo; echo "== cpu"
echo "  load avg (1/5/15 min, 2 vCPU): $(cut -d' ' -f1-3 /proc/loadavg)"
top -b -n 1 -p "$PID" | tail -n 1 | awk '{printf "  valheim_server now: %s%% CPU, %s%% MEM\n", $9, $10}'
echo "  cumulative CPU time: $(ps -o etime=,time= -p "$PID" | awk '{print "uptime "$1", cpu "$2}')"

echo; echo "== disk"
df -h / | awk 'NR==2{printf "  root: %s used of %s (%s)\n", $3, $2, $5}'
du -sh /home/valheim/data/worlds_local 2>/dev/null | awk '{print "  worlds_local: "$1}'

echo; echo "== OOM / kernel kills / segfaults (want none)"
journalctl -k --no-pager --since "$START" | grep -iE 'oom|killed process|segfault' | tail -n 5 || true
journalctl -u valheim --no-pager --since "$START" | grep -iE 'oom|out of memory|segfault|core dumped|Main process exited|Failed with result' | tail -n 5 || true
echo "  (end of kill checks)"

echo; echo "== errors / warnings since start, excluding known-benign Unity noise"
journalctl -u valheim --no-pager --since "$START" \
  | grep -iE 'error|exception|fail|warn|timeout|refused|denied' \
  | grep -viE 'referenced script|Missing audio clip|S_API FAIL\] Tried to access Steam interface SteamNetworkingUtils004|ProbeSize' \
  | sed 's/.*\]: //' | sort | uniq -c | sort -rn | head -n 15
echo "  (end of error scan)"

echo; echo "== steam networking / auth"
journalctl -u valheim --no-pager --since "$START" | grep -iE 'Authentication:|Steam game server|Registering lobby|Game server connected|Session' | sed 's/.*\]: //' | tail -n 6

echo; echo "== players & connections"
journalctl -u valheim --no-pager --since "$START" | grep -iE 'Got connection|Got handshake|Got character ZDOID|Player joined|Player left|disconnected|Closing socket|New peer' | sed 's/.*\]: //' | tail -n 8 || true
conn=$(journalctl -u valheim --no-pager --since "$START" | grep -c 'Got connection SteamID'); disc=$(journalctl -u valheim --no-pager --since "$START" | grep -ciE 'Closing socket|Player disconnected')
echo "  connections since start: $conn   disconnects: $disc   => roughly $((conn-disc)) online now"
echo "  saves since start: $(journalctl -u valheim --no-pager --since "$START" | grep -c 'World save (5/5) done')"
journalctl -u valheim --no-pager --since "$START" | grep 'World save (5/5) done' | tail -n 1 | sed 's/.*\]: /  last: /'

echo; echo "== listeners (want 2456 game + 2457 query)"
ss -ulnp | grep -E ':(2456|2457|2458)\s' | awk '{print "  "$4"  "$6}'
