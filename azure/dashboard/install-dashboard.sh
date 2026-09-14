#!/usr/bin/env bash
# install-dashboard.sh -- run on the VM as root from /home/azureuser/dashboard, with
# /etc/valheim-server.env already in place (copy it from ../valheim-server.env.example and set
# at least DASHBOARD_HOST first):
#   sudo bash install-dashboard.sh <viewer-password> <owner-password>
# viewer-password: user `viking`, opens the dashboard.
# owner-password:  user `owner`, opens /snapshots/ (world backup archives) -- nothing else does.
set -euo pipefail
PW="${1:?usage: install-dashboard.sh <viewer-password> <owner-password> (with DASHBOARD_HOST set in /etc/valheim-server.env)}"
OWNER_PW="${2:?usage: install-dashboard.sh <viewer-password> <owner-password> (with DASHBOARD_HOST set in /etc/valheim-server.env)}"
cd "$(dirname "$0")"
# DASHBOARD_HOST fills in the Caddyfile's __DASHBOARD_HOST__ token below, alongside __HASH__ /
# __OWNER_HASH__ -- sourced from the shared config so it is the same value the dashboard's own
# Python scripts use (SERVER_ADDRESS, etc.), not a separate CLI argument to keep in sync.
if [ -f /etc/valheim-server.env ]; then
  set -a; . /etc/valheim-server.env; set +a
fi
: "${DASHBOARD_HOST:?set DASHBOARD_HOST in /etc/valheim-server.env (see ../valheim-server.env.example) before running install-dashboard.sh}"
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
# Hermodr reads its own env now that it runs as valheim-bot, so the bot process never holds the
# Discord WEBHOOK credential that alert/medals/digest still use. Never overwritten once present.
test -f /etc/valheim-bot.env || install -o root -g valheim-bot -m 0640 valheim-bot.env /etc/valheim-bot.env
# nftables, not tcpdump: the collector learns peer addresses from the `peers` set in the
# `inet valheim_meter` table (installed by valheim-egress.service's ExecStartPre=), which
# replaced a once-a-minute AF_PACKET tap on the game's own receive path.
command -v nft >/dev/null || apt-get -o DPkg::Lock::Timeout=600 install -y nftables
install -m 0644 valheim-status.service valheim-status.timer /etc/systemd/system/
# Let the caddy user reach /home/valheim/backups (the snapshots listing) without opening
# /home/valheim to everyone: an ACL granting traversal of the home dir and read of backups only.
command -v setfacl >/dev/null || apt-get -o DPkg::Lock::Timeout=600 install -y acl
install -d -m 0755 -o valheim -g valheim /home/valheim/backups
setfacl -m u:caddy:x /home/valheim
setfacl -m u:caddy:rx /home/valheim/backups
# --- service users and the restart spool (PLAN-v5) -------------------------------------------
# Two unprivileged system accounts. Both keep a nologin shell: security-check.sh audits accounts
# that have a login shell, and these must never appear there.
id valheim-bot        >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin valheim-bot
id valheim-restartd   >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin valheim-restartd

# The spool IS the security model: one writer and one reader per directory, enforced by ownership.
# 1730 is rwx-wx--T -- the writer may create a file by name but can never list or read the
# directory back, and the sticky bit switches on the kernel's fs.protected_symlinks guard.
install -d -m 0755 -o root -g root             /var/lib/valheim-restart
install -d -m 1730 -o root -g valheim-restartd /var/lib/valheim-restart/requests
install -d -m 0750 -o root -g valheim-bot      /var/lib/valheim-restart/inbox
install -d -m 1730 -o root -g valheim-bot      /var/lib/valheim-restart/verdicts
install -d -m 0700 -o root -g root             /var/lib/valheim-restart/archive
install -d -m 0700 -o root -g root             /var/lib/valheim-restart/quarantine
# gate.json and secret.csrf are created by the executor on its first tick, with the right modes.

# Hermodr's `!lag` spool, same shape and same reason as requests/ above: 1730 lets valheim-bot
# create a report by name but never list or read the directory back, and root (the collector)
# drains it into events.jsonl once a minute. The bot cannot write events.jsonl itself -- the
# collector rewrites and renames that file every run, which would reset its owner and mode and
# could swallow an appended line outright.
install -d -m 1730 -o root -g valheim-bot /var/lib/valheim-status/lagreports

HASH=$(caddy hash-password --plaintext "$PW")
OWNER_HASH=$(caddy hash-password --plaintext "$OWNER_PW")
sed -e "s#__HASH__#$HASH#" -e "s#__OWNER_HASH__#$OWNER_HASH#" -e "s#__DASHBOARD_HOST__#$DASHBOARD_HOST#" Caddyfile > /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
install -m 0755 valheim-world-scan.py /usr/local/sbin/valheim-world-scan.py
install -m 0755 valheim-offsite-backup.sh /usr/local/sbin/valheim-offsite-backup.sh
install -m 0755 valheim-digest.py /usr/local/sbin/valheim-digest.py
install -m 0755 valheim-medals.py /usr/local/sbin/valheim-medals.py
install -m 0644 valheim-offsite.service valheim-offsite.timer /etc/systemd/system/
install -m 0644 valheim-digest.service valheim-digest.timer /etc/systemd/system/
install -m 0644 valheim-medals-daily.service valheim-medals-daily.timer /etc/systemd/system/
install -m 0644 valheim-medals-web.service valheim-medals-web.timer /etc/systemd/system/
# The restart feature. valheim-restart-exec.path is installed but deliberately NOT enabled --
# see the warning in its header; the 15 s timer is the guarantee, the path unit is convenience.
install -m 0755 valheim-restartd.py valheim-restart-exec.py /usr/local/sbin/
install -m 0644 valheim-restartd.service valheim-restart-exec.service \
                valheim-restart-exec.timer valheim-restart-exec.path /etc/systemd/system/
install -m 0644 manifest.webmanifest icon.svg icon-192.png icon-512.png /var/www/valheim/
# --- egress probe (lag investigation) ---------------------------------------------------------
# valheim-egress.service's ExecStartPre= runs the nft helper, so the `inet valheim_meter` table
# (counters + the `peers` set the collector now reads instead of running tcpdump) is rebuilt on
# every start and after a reboot. Keep this unit enabled: stopping it leaves the table in place,
# but a reboot with it disabled would take the dashboard's per-player ping column with it.
# The report script is offline and operator-run; it is installed only so it is on PATH.
install -m 0755 valheim-meter-nft.sh /usr/local/sbin/valheim-meter-nft.sh
install -m 0755 valheim-egress-probe.py /usr/local/sbin/valheim-egress-probe.py
install -m 0755 valheim-egress-report.py /usr/local/sbin/valheim-egress-report.py
install -m 0644 valheim-egress.service /etc/systemd/system/
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
systemctl enable --now valheim-offsite.timer valheim-digest.timer valheim-medals-daily.timer valheim-medals-web.timer
# The executor ships DISARMED: valheim-restart-exec.service carries Environment=VR_DRY_RUN=1, so
# it runs every check and posts to Discord but calls no systemctl. Remove that line and
# daemon-reload to arm it, after a rehearsal day. The timer also creates gate.json and
# secret.csrf on its first tick, which is why it starts before restartd.
systemctl enable --now valheim-restart-exec.timer
systemctl enable --now valheim-restartd.service
systemctl enable --now valheim-status.timer
# Started before the collector's first run, so the meter table (and its `peers` set) exists by the
# time valheim-status-collect.py first looks for it.
systemctl enable --now valheim-egress.service
systemctl start valheim-status.service
systemctl enable --now caddy
systemctl reload caddy
sleep 3
systemctl is-active caddy valheim-status.timer
ls -l /var/www/valheim
echo "dashboard installed (viewer login: viking, snapshots login: owner at /snapshots/)"
