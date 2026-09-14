#!/usr/bin/env bash
# security-check.sh -- run on the VM:  sudo bash /home/azureuser/security-check.sh
echo "== sshd effective settings (want: passwordauthentication no, permitrootlogin no, pubkeyauthentication yes)"
sshd -T 2>/dev/null | grep -iE '^(passwordauthentication|permitrootlogin|pubkeyauthentication|kbdinteractiveauthentication|challengeresponseauthentication) ' | sed 's/^/  /'

echo; echo "== everything listening on non-loopback addresses (want: only ssh 22, valheim 2456/2457)"
ss -tulnpH | grep -vE '127\.0\.0\.1|\[::1\]' | awk '{printf "  %-5s %-22s %s\n", $1, $5, $7}'

echo; echo "== valheim runs as (want: valheim, not root)"
ps -o user=,pid=,comm= -p "$(systemctl show valheim -p MainPID --value)" | sed 's/^/  /'

echo; echo "== accounts with a login shell"
awk -F: '$7 !~ /nologin|false|sync|halt|shutdown/ {print "  "$1"  shell="$7}' /etc/passwd
echo "  accounts with a password set: $(awk -F: '$2 !~ /^[!*]/ {print $1}' /etc/shadow | tr '\n' ' ')(blank = none, key-only)"
echo "  sudoers for azureuser: $(grep -rhs azureuser /etc/sudoers /etc/sudoers.d | tr '\n' ' ')"

echo; echo "== automatic security updates"
echo "  unattended-upgrades: $(systemctl is-enabled unattended-upgrades 2>/dev/null || echo not-installed) / $(systemctl is-active unattended-upgrades 2>/dev/null)"
grep -hE 'Unattended-Upgrade|Update-Package-Lists' /etc/apt/apt.conf.d/20auto-upgrades 2>/dev/null | sed 's/^/  /'

echo; echo "== host firewall (NSG in Azure is the real control; ufw is expected inactive)"
echo "  $(ufw status | head -n 1)"

echo; echo "== Valheim access lists (/home/valheim/data)"
for f in adminlist bannedlist permittedlist; do
  printf '  %-14s ' "$f.txt:"
  if [ -f /home/valheim/data/$f.txt ]; then c=$(grep -cve '^\s*$' /home/valheim/data/$f.txt); echo "$c entries: $(grep -ve '^\s*$' /home/valheim/data/$f.txt | tr '\n' ' ')"; else echo "(absent)"; fi
done
echo "  note: permittedlist.txt, if present, allows ONLY the listed IDs and blocks everyone else"

echo; echo "== unit file permissions (contains the game password)"
ls -l /etc/systemd/system/valheim.service | sed 's/^/  /'

echo; echo "== SSH auth attempts since boot"
echo "  accepted logins: $(journalctl -u ssh --no-pager -b | grep -c 'Accepted publickey')"
echo "  failed/invalid attempts: $(journalctl -u ssh --no-pager -b | grep -ciE 'invalid user|failed password|authentication failure|Connection closed by authenticating')"
journalctl -u ssh --no-pager -b | grep -iE 'invalid user|failed password' | tail -n 3 | sed 's/.*\]: /  /'

echo; echo "== game connections since service start (SteamID -> should all be friends)"
journalctl -u valheim --no-pager --since "$(systemctl show valheim -p ActiveEnterTimestamp --value)" | grep -oE 'Got connection SteamID [0-9]+' | sort | uniq -c | sed 's/^/  /'

echo; echo "== restart feature: privilege boundaries (PLAN-v5). Absent = not deployed yet, which is fine."
if [ ! -d /var/lib/valheim-restart ]; then
  echo "  /var/lib/valheim-restart does not exist -- restart feature not deployed; skipping."
else
  # The whole design rests on filesystem ownership, so these modes ARE the security model.
  # Wanted, from PLAN-v5:
  #   requests/  root:valheim-restartd 1730   (restartd may create a file, but not list or read)
  #   inbox/     root:valheim-bot      0750   (root writes, bot reads)
  #   verdicts/  root:valheim-bot      1730   (bot may create, only root reads)
  want() { # dir wanted_mode wanted_owner
    got=$(stat -c '%a %U:%G' "$1" 2>/dev/null)
    if [ "$got" = "$2 $3" ]; then echo "  ok      $1  ($got)"; else echo "  MISMATCH $1  want '$2 $3', got '${got:-absent}'"; fi
  }
  want /var/lib/valheim-restart          755  root:root
  want /var/lib/valheim-restart/requests 1730 root:valheim-restartd
  want /var/lib/valheim-restart/inbox    750  root:valheim-bot
  want /var/lib/valheim-restart/verdicts 1730 root:valheim-bot
  want /var/lib/valheim-restart/archive  700  root:root
  want /var/lib/valheim-restart/quarantine 700 root:root
  want /var/lib/valheim-restart/secret.csrf 640 root:valheim-restartd

  # Carries the CSRF token, so it must not be world-readable even though it sits in the
  # webroot. Caddy reads it as group 'caddy'.
  want /var/www/valheim/restart-state.json 640 root:caddy

  # The socket, not a TCP port: a 127.0.0.1 port would be reachable by every local account,
  # including the 'valheim' user whose process faces the internet.
  printf '  %-8s %s\n' "socket" "$(stat -c '%a %U:%G %F' /run/valheim-restartd/http.sock 2>/dev/null || echo 'absent (restartd not running?)')"
  echo "  restartd listening on TCP anywhere (want: nothing): $(ss -tlnpH 2>/dev/null | grep -c valheim-restartd)"

  echo "  -- service users (want: neither is uid 0)"
  for u in valheim-restartd valheim-bot; do
    if id "$u" >/dev/null 2>&1; then
      echo "    $u uid=$(id -u "$u") shell=$(getent passwd "$u" | cut -d: -f7)"
    else
      echo "    $u  MISSING"
    fi
  done
  for unit in valheim-restartd valheim-bot; do
    echo "    $unit.service User=$(systemctl show "$unit" -p User --value 2>/dev/null)  (want: $unit)"
  done

  echo "  -- the executor must be the ONLY thing able to touch valheim.service"
  echo "    sudoers mentioning systemctl or valheim (want: nothing): $(grep -rhsE 'systemctl|valheim' /etc/sudoers /etc/sudoers.d 2>/dev/null | grep -v '^#' | tr '\n' ' ')"
  echo "    polkit rules for systemd (want: nothing): $(ls -1 /etc/polkit-1/rules.d/ 2>/dev/null | tr '\n' ' ')"
  echo "    setuid files under /usr/local (want: nothing): $(find /usr/local -perm -4000 -type f 2>/dev/null | tr '\n' ' ')"

  echo "  -- kernel guards the sticky spool dirs rely on (want: both 1)"
  echo "    fs.protected_symlinks=$(sysctl -n fs.protected_symlinks 2>/dev/null) fs.protected_hardlinks=$(sysctl -n fs.protected_hardlinks 2>/dev/null)"

  echo "  -- anything quarantined? (a spool file that failed validation; should normally be empty)"
  echo "    $(ls -1 /var/lib/valheim-restart/quarantine 2>/dev/null | wc -l) file(s)"

  echo "  -- kill switch state"
  echo "    valheim-restartd: $(systemctl is-enabled valheim-restartd 2>/dev/null)/$(systemctl is-active valheim-restartd 2>/dev/null)"
  echo "    valheim-restart-exec.timer: $(systemctl is-enabled valheim-restart-exec.timer 2>/dev/null)/$(systemctl is-active valheim-restart-exec.timer 2>/dev/null)"
  echo "    accepting new requests: $(python3 -c "import json;print(json.load(open('/var/lib/valheim-restart/gate.json')).get('accepting'))" 2>/dev/null || echo 'unknown')"

  echo "  -- last 3 restart audit entries"
  tail -n 3 /var/log/valheim-restart.jsonl 2>/dev/null | sed 's/^/    /' || echo "    (no audit log yet)"
fi
