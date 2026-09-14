#!/usr/bin/env bash
# install-dashboard.sh -- run on the VM as root from /home/azureuser/dashboard:
#   sudo bash install-dashboard.sh <viewer-password> <owner-password>
# viewer-password: user `viking`, opens the dashboard.
# owner-password:  user `owner`, opens /snapshots/ (world backup archives) -- nothing else does.
set -euo pipefail
PW="${1:?usage: install-dashboard.sh <viewer-password> <owner-password>}"
OWNER_PW="${2:?usage: install-dashboard.sh <viewer-password> <owner-password>}"
cd "$(dirname "$0")"
export DEBIAN_FRONTEND=noninteractive
if ! command -v caddy >/dev/null; then
  apt-get -o DPkg::Lock::Timeout=600 install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get -o DPkg::Lock::Timeout=600 update
  apt-get -o DPkg::Lock::Timeout=600 install -y caddy
fi
install -d -m 0755 /var/www/valheim /var/lib/valheim-status
install -m 0644 index.html /var/www/valheim/index.html
printf 'ok\n' > /var/www/valheim/ping.txt; chmod 644 /var/www/valheim/ping.txt
install -m 0755 valheim-status-collect.py /usr/local/sbin/valheim-status-collect.py
install -m 0755 valheim-alert.py /usr/local/sbin/valheim-alert.py
test -f /etc/valheim-alert.env || install -m 0600 valheim-alert.env /etc/valheim-alert.env
command -v tcpdump >/dev/null || apt-get -o DPkg::Lock::Timeout=600 install -y tcpdump
install -m 0644 valheim-status.service valheim-status.timer /etc/systemd/system/
# Let the caddy user reach /home/valheim/backups (the snapshots listing) without opening
# /home/valheim to everyone: an ACL granting traversal of the home dir and read of backups only.
command -v setfacl >/dev/null || apt-get -o DPkg::Lock::Timeout=600 install -y acl
install -d -m 0755 -o valheim -g valheim /home/valheim/backups
setfacl -m u:caddy:x /home/valheim
setfacl -m u:caddy:rx /home/valheim/backups
HASH=$(caddy hash-password --plaintext "$PW")
OWNER_HASH=$(caddy hash-password --plaintext "$OWNER_PW")
sed -e "s#__HASH__#$HASH#" -e "s#__OWNER_HASH__#$OWNER_HASH#" Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
install -m 0755 valheim-world-scan.py /usr/local/sbin/valheim-world-scan.py
install -m 0755 valheim-offsite-backup.sh /usr/local/sbin/valheim-offsite-backup.sh
install -m 0755 valheim-digest.py /usr/local/sbin/valheim-digest.py
install -m 0755 valheim-medals.py /usr/local/sbin/valheim-medals.py
install -m 0644 valheim-offsite.service valheim-offsite.timer /etc/systemd/system/
install -m 0644 valheim-digest.service valheim-digest.timer /etc/systemd/system/
install -m 0644 valheim-medals-daily.service valheim-medals-daily.timer /etc/systemd/system/
install -m 0644 manifest.webmanifest icon.svg icon-192.png icon-512.png /var/www/valheim/
# Hermodr (Discord Q&A bot) -- its own venv so discord.py never touches the system Python used by
# the collector/alert/medals scripts above. Installed but NOT started: it needs DISCORD_BOT_TOKEN
# (and HERMODR_CHANNEL_ID or HERMODR_CHANNEL_NAME) in /etc/valheim-alert.env first, and there is no
# token yet on a fresh deploy. Idempotent: re-running this only upgrades the venv/script/unit.
command -v python3 >/dev/null && python3 -c "import venv" 2>/dev/null || apt-get -o DPkg::Lock::Timeout=600 install -y python3-venv
install -d -m 0755 /opt/hermodr
[ -x /opt/hermodr/venv/bin/python ] || python3 -m venv /opt/hermodr/venv
/opt/hermodr/venv/bin/pip install --upgrade pip >/dev/null
/opt/hermodr/venv/bin/pip install --upgrade discord.py
install -m 0755 valheim-bot.py /usr/local/sbin/valheim-bot.py
install -m 0644 valheim-bot.service /etc/systemd/system/valheim-bot.service
systemctl daemon-reload
systemctl enable --now valheim-offsite.timer valheim-digest.timer valheim-medals-daily.timer
systemctl enable --now valheim-status.timer
systemctl start valheim-status.service
systemctl enable --now caddy
systemctl reload caddy
sleep 3
systemctl is-active caddy valheim-status.timer
ls -l /var/www/valheim
echo "dashboard installed (viewer login: viking, snapshots login: owner at /snapshots/)"
