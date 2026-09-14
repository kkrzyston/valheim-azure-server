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
