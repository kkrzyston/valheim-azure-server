#!/usr/bin/env python3
"""valheim-alert.py -- post Discord alerts from the dashboard's status.json.

Runs right after the collector, every minute (valheim-status.service). Reads DISCORD_WEBHOOK_URL,
ALERT_JOINS and DASHBOARD_URL from /etc/valheim-alert.env. Does nothing until a webhook URL is set.

Alerts (each sent once, with a recovery message where it makes sense):
  down        server not answering for 2 checks in a row (not during an update)  -> and "back up"
  updating    auto-updater began installing a new build                          -> and "updated to X"
  crash       systemd restarted the game after a crash
  unstable    memory nearly full, swap growing, disk nearly full, slow world saves, CPU pegged
  joins       (ALERT_JOINS=1) Vikings arriving and leaving (no mention)
  board       one message, edited every minute, showing live status (never @everyone)
  raid        an in-world raid event, with who was online
  boss        a boss flips from undefeated to defeated (torch colour, no mention)
  milestone   a Viking crosses an hours/deaths/sessions threshold, or a new name joins the roster
  azure       an Azure scheduled-maintenance event appears or clears
  offsite     the off-site backup has not succeeded in over 36 h, and its recovery

State (STATE file) also carries `boss_log` (list of {boss, t}), which valheim-digest.py reads
read-only to report bosses defeated in the past week. Milestone and boss thresholds are seeded
from whatever status.json already shows the first time each is seen, so pre-existing totals never
fire a burst of old milestones.
"""
import json, os, time, urllib.request, urllib.error

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
ALERT_JOINS = os.environ.get("ALERT_JOINS", "0").strip() == "1"
DASH = os.environ.get("DASHBOARD_URL", "").strip()
STATUS = "/var/www/valheim/status.json"
STATE = "/var/lib/valheim-status/alerts.json"
LOG = "/var/log/valheim-alert.log"

if not WEBHOOK:
    raise SystemExit(0)

try:
    status = json.load(open(STATUS))
except Exception:
    raise SystemExit(0)
try:
    st = json.load(open(STATE))
except Exception:
    st = {}
now = time.time()
s, u, m, w, p = status["server"], status["updates"], status["machine"], status["world"], status["players"]

COLORS = {"bad": 0xD0604A, "ok": 0x8FB877, "warn": 0xE0A44B, "info": 0x93A8AB, "torch": 0xE3A54A}

BOSS_NAMES = {
    "eikthyr": "Eikthyr", "elder": "The Elder", "bonemass": "Bonemass",
    "moder": "Moder", "yagluth": "Yagluth", "queen": "The Queen", "fader": "Fader",
}
HOUR_MILESTONES = [10, 25, 50, 100, 250, 500]
DEATH_MILESTONES = [10, 25, 50, 100]
SESSION_MILESTONES = [25, 50, 100]


def post(title, text, level="info", everyone=False):
    body = {"username": "Hermóðr", "embeds": [{"title": title, "description": text, "color": COLORS[level],
            "footer": {"text": "1g49ye on Vancouver Island"}}]}
    # Owner's rule (2026-09-11): never ping @everyone; posts arrive silently. The flag is kept
    # only so call sites stay readable; it is deliberately ignored.
    body["allowed_mentions"] = {"parse": []}
    if DASH:
        body["embeds"][0]["url"] = DASH
    req = urllib.request.Request(WEBHOOK, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "valheim-alert/1"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        ok = "sent"
    except Exception as e:
        ok = f"FAILED {e}"
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%F %T')} {ok}: {title} | {text}\n")


def dur(sec):
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600} h {sec % 3600 // 60} min"
    if sec >= 60:
        return f"{sec // 60} min"
    return f"{sec} s"


# ---- down / back up
online = s["online"]
updating = u.get("status") == "updating"
if not online and not updating:
    st["down_checks"] = st.get("down_checks", 0) + 1
    if st["down_checks"] == 2:
        st["down_since"] = now - 120
        why = "the game service has stopped" if not s["service_active"] else "the game is running but not answering"
        post("The server is down", f"Existence is pain: {why}. The dashboard will keep checking every minute, and this channel will hear when it is back.", "bad")
        st["down_alerted"] = True
elif online:
    if st.get("down_alerted"):
        post("The server is back", f"The hall is open again after about {dur(now - st.get('down_since', now))}. Version {s['version']}.", "ok")
    st["down_checks"] = 0
    st["down_alerted"] = False

# ---- updating / updated
last_line = u.get("last_line", "")
if last_line != st.get("last_update_line"):
    if updating:
        post("Updating the server", "Steam has a new Valheim build. Nobody is on, so it is installing now. Back within a couple of minutes.", "warn")
    elif u.get("status") == "pending" and st.get("last_update_status") != "pending":
        post("An update is waiting", f"Steam has a new Valheim build. It will install as soon as everyone has left the world ({p['count']} on right now). Update your game when you next launch it.", "info")
    elif u.get("status") == "error":
        post("Update check failed", f"The last auto-update run reported a problem:\n`{last_line[:300]}`", "warn")
    st["last_update_line"] = last_line
st["last_update_status"] = u.get("status")

last_evt = st.get("last_event_t", now - 300)
newest = last_evt
for e in sorted(status["events"], key=lambda e: e["t"]):
    if e["t"] <= last_evt:
        continue
    newest = max(newest, e["t"])
    if e["kind"] == "update":
        post("Server updated", f"{e['text']}. Make sure your game is updated in Steam before joining.", "ok")
    elif ALERT_JOINS and e["kind"] == "join":
        post(f"{e['name']} arrived", f"{p['count']} in the world now.", "info", everyone=True)
    elif ALERT_JOINS and e["kind"] == "leave":
        post(f"{e['name']} left", (f"after {dur(e['played'])}. " if e.get("played") else "") + f"{p['count']} in the world now.", "info", everyone=True)
    elif e["kind"] == "raid":
        raid_list = (w.get("raids") or {}).get("list", [])
        online_names = []
        for r in raid_list:
            if abs(r.get("t", -1e18) - e["t"]) < 5:
                online_names = r.get("online", [])
                break
        label = e.get("label", e.get("name", "a raid"))
        text = f"Vikings on hand: {', '.join(online_names)}." if online_names else "No one was there to see it."
        post(f"Raid: {label}", text, "warn")
st["last_event_t"] = newest

# ---- crash / restart by systemd
r = s.get("restarts_since_boot", 0)
if r > st.get("restarts", 0):
    post("The server crashed and restarted itself", f"Restart number {r} since the machine booted. If this keeps happening, check the dashboard's machine section.", "warn")
st["restarts"] = r

# ---- instability, each with a clear-condition so it alerts once
def flag(key, condition, clear, title, text, level="warn"):
    if condition and not st.get(key):
        post(title, text, level)
        st[key] = True
    elif clear and st.get(key):
        st[key] = False

mem_pct = 100 * m["mem_used"] / m["mem_total"]
flag("mem", mem_pct > 92, mem_pct < 85, "Memory is nearly full", f"{mem_pct:.0f}% of memory in use, game process {m['valheim_rss'] / 1e9:.1f} GB. The server may stutter or crash.")
flag("swap", m["swap_used"] > 1.5e9, m["swap_used"] < 0.5e9, "Server is swapping heavily", f"{m['swap_used'] / 1e9:.1f} GB of swap in use. Expect lag until it settles.")
disk_pct = 100 * m["disk_used"] / m["disk_total"]
flag("disk", disk_pct > 90, disk_pct < 80, "Disk is nearly full", f"{disk_pct:.0f}% used. World saves will fail if it fills up.", "bad")
save_ms = (w.get("last_save") or {}).get("ms", 0)
flag("save", save_ms > 5000, save_ms < 2000, "World saves are slow", f"The last save took {save_ms / 1000:.1f} s; players freeze during saves that long.")
cpu = m.get("cpu_pct")
st["cpu_hot"] = st.get("cpu_hot", 0) + 1 if (cpu or 0) > 95 else 0
flag("cpu", st["cpu_hot"] >= 5, st["cpu_hot"] == 0, "Processor pegged", "CPU has been above 95% for 5 minutes. Expect lag.")

# ---- boss falls (seed from current state on first sight, no burst of old news)
bosses_now = {k: v for k, v in (w.get("bosses") or {}).items() if k != "source"}
if "bosses" not in st:
    st["bosses"] = dict(bosses_now)
elif bosses_now:
    boss_log = st.setdefault("boss_log", [])
    for key, defeated in bosses_now.items():
        if defeated and not st["bosses"].get(key):
            display = BOSS_NAMES.get(key, key.title())
            post(f"{display} has fallen", f"{display} has been defeated on Vancouver Island. Existence is pain, but a little less of it now.", "torch", everyone=True)
            boss_log.append({"boss": display, "t": now})
            del boss_log[:-50]
    st["bosses"] = bosses_now

# ---- milestones: hours / deaths / sessions per Viking, and new names on the roster
stats = p.get("stats", [])
milestones = st.setdefault("milestones", {})
known_names = st.get("known_names")
if known_names is None:
    st["known_names"] = [row["name"] for row in stats]
else:
    for row in stats:
        if row["name"] not in known_names:
            known_names.append(row["name"])
            post("A new Viking arrives", f"The hall has {len(known_names)} Vikings now that {row['name']} has joined the roster.", "ok")

for row in stats:
    name = row["name"]
    hours = row.get("total", 0) / 3600.0
    deaths = row.get("deaths", 0)
    sessions = row.get("sessions", 0)
    prev = milestones.get(name)
    if prev is None:
        milestones[name] = {"hours": hours, "deaths": deaths, "sessions": sessions}
        continue
    for t in HOUR_MILESTONES:
        if prev["hours"] < t <= hours:
            post(f"{name} reaches {t} hours", f"{name} has spent {t}+ hours in Vancouver Island.", "ok")
    for t in DEATH_MILESTONES:
        if prev["deaths"] < t <= deaths:
            post(f"{name} has died {t} times", f"Existence is pain, and {name} knows it well: {t} deaths and counting.", "warn")
    for t in SESSION_MILESTONES:
        if prev["sessions"] < t <= sessions:
            post(f"{name} logs session {t}", f"{name} has logged in {t} times now.", "ok")
    milestones[name] = {"hours": hours, "deaths": deaths, "sessions": sessions}

# ---- Azure scheduled maintenance
maint = status.get("azure", {}).get("maintenance") or {}
events_list = maint.get("events") or []
posted_ids = st.setdefault("azure_events_posted", [])
current_ids = [ev.get("id") for ev in events_list]
for ev in events_list:
    eid = ev.get("id")
    if eid is not None and eid not in posted_ids:
        when = ev.get("not_before") or "an unspecified time"
        post("Azure maintenance scheduled", f"{ev.get('type', 'Maintenance')} planned for {when}. {ev.get('description', '')}".strip(), "warn")
        posted_ids.append(eid)
for eid in list(posted_ids):
    if eid not in current_ids:
        post("Azure maintenance cleared", "The scheduled maintenance event has cleared; no action needed.", "ok")
        posted_ids.remove(eid)

# ---- off-site backup missed / recovered
offsite = status.get("backup", {}).get("offsite")
if offsite:
    last_success = offsite.get("last_success")
    last_attempt = offsite.get("last_attempt")
    last_error = offsite.get("last_error")
    stale = (last_success is None and last_attempt is not None) or (last_success is not None and now - last_success > 36 * 3600)
    if stale and not st.get("offsite_alerted"):
        since = f" (last success {dur(now - last_success)} ago)" if last_success else " (no successful copy yet)"
        err = f" Last error: {last_error}" if last_error else ""
        post("Off-site backup is behind", f"The nightly copy to Azure Blob has not succeeded in over 36 hours{since}.{err}", "warn")
        st["offsite_alerted"] = True
    elif not stale and st.get("offsite_alerted"):
        post("Off-site backup is caught up", f"The nightly copy to Azure Blob succeeded again. {offsite.get('blobs', '?')} blobs, newest {offsite.get('newest_blob', 'unknown')}.", "ok")
        st["offsite_alerted"] = False

# ---- live status board: one message, edited every minute, never @everyone
def board_embed():
    bosses = w.get("bosses") or {}
    boss_count = sum(1 for k, v in bosses.items() if k != "source" and v)
    lines = []
    for o in p.get("online", []):
        length = dur(now - o["since"])
        if o.get("ping_ms") is not None:
            ping = f"{o['ping_ms']} ms"
        elif o.get("ping_checked"):
            ping = "no reply"
        else:
            ping = "checking…"
        lines.append(f"{o['name']} — {length}, {ping}")
    online_text = "\n".join(lines) if lines else "No one in the hall right now."
    raid_last = (w.get("raids") or {}).get("last")
    raid_text = f"{raid_last['label']} ({dur(now - raid_last['t'])} ago)" if raid_last else "none yet"
    nuc = s.get("next_update_check")
    nuc_text = dur(nuc - now) if nuc and nuc > now else ("due now" if nuc else "unknown")
    uptime = s.get("uptime_30d_pct")
    uptime_text = f"{uptime:.1f}%" if uptime is not None else "unknown"
    embed = {
        "title": "Vancouver Island, right now",
        "description": "The hall is **open**." if s.get("online") else "The hall is **closed**.",
        "color": COLORS["ok"] if s.get("online") else COLORS["bad"],
        "fields": [
            {"name": "Online", "value": online_text, "inline": False},
            {"name": "Day", "value": str(w.get("day", "unknown")), "inline": True},
            {"name": "Bosses", "value": f"{boss_count}/7", "inline": True},
            {"name": "Uptime, 30 days", "value": uptime_text, "inline": True},
            {"name": "Last raid", "value": raid_text, "inline": False},
            {"name": "Next Steam check", "value": nuc_text, "inline": True},
        ],
        "footer": {"text": f"updated {time.strftime('%-I:%M %p', time.localtime(now))}"},
    }
    if DASH:
        embed["url"] = DASH
    return embed


def discord_call(url, body, method):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "valheim-alert/1"}, method=method)
    return urllib.request.urlopen(req, timeout=10)


def update_board():
    body = {"username": "Hermóðr", "embeds": [board_embed()]}
    mid = st.get("board_message_id")
    if mid:
        try:
            discord_call(f"{WEBHOOK}/messages/{mid}", body, "PATCH").read()
            with open(LOG, "a") as f:
                f.write(f"{time.strftime('%F %T')} board edited: {mid}\n")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                with open(LOG, "a") as f:
                    f.write(f"{time.strftime('%F %T')} board PATCH FAILED {e}\n")
                return
            # message was deleted -- fall through and create a new one
        except Exception as e:
            with open(LOG, "a") as f:
                f.write(f"{time.strftime('%F %T')} board PATCH FAILED {e}\n")
            return
    try:
        resp = discord_call(f"{WEBHOOK}?wait=true", body, "POST")
        created = json.loads(resp.read())
        st["board_message_id"] = created["id"]
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%F %T')} board created: {created['id']}\n")
    except Exception as e:
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%F %T')} board CREATE FAILED {e}\n")


update_board()

with open(STATE, "w") as f:
    json.dump(st, f)
