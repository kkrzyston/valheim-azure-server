#!/usr/bin/env python3
"""valheim-digest.py -- weekly Discord digest from the dashboard's status.json/history.json.

Run by valheim-digest.timer, Sundays 18:00 local. Reads DISCORD_WEBHOOK_URL and DASHBOARD_URL
from /etc/valheim-alert.env (same file valheim-alert.py uses). Posts one embed summarising the
past 7 days: hours in the world (total and per Viking, from players.sessions_7d when the
collector emits it), sessions, deaths per Viking (from the events feed), raids, bosses defeated
(from the boss_log that valheim-alert.py appends to in its shared state file, read-only here),
most people online at once, uptime, and updates installed.

--dry-run prints the Discord message body as JSON instead of posting it.
"""
import json, os, sys, time, urllib.request

# Medals is a separate, independently-deployed script; import it defensively so a missing file
# or a bug in it never stops the weekly ledger from posting -- it only ever costs the second embed.
try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("valheim_medals", os.path.join(os.path.dirname(os.path.abspath(__file__)), "valheim-medals.py"))
    valheim_medals = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(valheim_medals)
except Exception:
    valheim_medals = None

WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DASH = os.environ.get("DASHBOARD_URL", "").strip()
# From /etc/valheim-server.env (see valheim-digest.service's EnvironmentFile=).
SERVER_NAME = os.environ.get("SERVER_NAME", "Valheim Server").strip() or "Valheim Server"
WORLD_NAME = os.environ.get("WORLD_NAME", "Dedicated").strip() or "Dedicated"
STATUS = "/var/www/valheim/status.json"
HISTORY = "/var/www/valheim/history.json"
STATE = "/var/lib/valheim-status/alerts.json"
LOG = "/var/log/valheim-digest.log"

DRY_RUN = "--dry-run" in sys.argv[1:]

WEEK_SECONDS = 7 * 86400


def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def dur(sec):
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    mnt = rem // 60
    if h:
        return f"{h} h {mnt} min" if mnt else f"{h} h"
    return f"{mnt} min"


def build_embed():
    status = load(STATUS, {})
    history = load(HISTORY, {})
    st = load(STATE, {})

    now = time.time()
    week_ago = now - WEEK_SECONDS

    s = status.get("server", {})
    p = status.get("players", {})
    w = status.get("world", {})
    u = status.get("updates", {})
    events = status.get("events", [])

    # ---- hours + sessions this week, per Viking (needs H1's players.sessions_7d)
    sessions_7d = p.get("sessions_7d")
    per_player_seconds = {}
    per_player_sessions = {}
    total_seconds = 0
    if sessions_7d:
        for sess in sessions_7d:
            name = sess.get("name", "someone")
            start = max(sess.get("start", now), week_ago)
            end = sess.get("end") or now
            secs = max(0, end - start)
            per_player_seconds[name] = per_player_seconds.get(name, 0) + secs
            per_player_sessions[name] = per_player_sessions.get(name, 0) + 1
            total_seconds += secs
        ordered = sorted(per_player_seconds.items(), key=lambda kv: -kv[1])
        hours_lines = [f"{name} — {dur(secs)} ({per_player_sessions[name]} session{'s' if per_player_sessions[name] != 1 else ''})" for name, secs in ordered]
        hours_text = f"{dur(total_seconds)} total\n" + "\n".join(hours_lines) if hours_lines else f"{dur(total_seconds)} total"
    else:
        hours_text = "not tracked yet (needs the v4 collector update)"

    # ---- deaths this week, from the events feed
    deaths_this_week = {}
    for e in events:
        if e.get("kind") == "death" and e.get("t", 0) >= week_ago:
            deaths_this_week[e.get("name", "someone")] = deaths_this_week.get(e.get("name", "someone"), 0) + 1
    deaths_text = ", ".join(f"{name}: {n}" for name, n in sorted(deaths_this_week.items(), key=lambda kv: -kv[1])) or "none this week"

    # ---- raids this week
    raids = w.get("raids") or {}
    raids_7d = raids.get("count_7d")
    if raids_7d is None:
        raids_7d = sum(1 for e in events if e.get("kind") == "raid" and e.get("t", 0) >= week_ago)
    last_raid = raids.get("last")
    raids_text = f"{raids_7d} this week"
    if last_raid:
        raids_text += f" — last: {last_raid.get('label', last_raid.get('name', '?'))}"

    # ---- bosses defeated this week (from valheim-alert.py's boss_log in the shared state file)
    boss_log = st.get("boss_log", [])
    bosses_week = [b["boss"] for b in boss_log if b.get("t", 0) >= week_ago]
    bosses_text = ", ".join(bosses_week) if bosses_week else "none this week"

    # ---- most people online at once this week (from history samples)
    max_online = None
    for series in ("week", "day"):
        for sample in history.get(series, []):
            if sample.get("t", 0) >= week_ago and "p" in sample:
                max_online = max(max_online or 0, sample["p"])
    most_text = f"{max_online} Viking{'s' if max_online != 1 else ''} at once" if max_online is not None else "unknown"

    # ---- uptime this week (fall back to the 30-day figure if there is no hourly history yet)
    o_samples = [smp.get("o") for smp in history.get("week", []) if smp.get("t", 0) >= week_ago and "o" in smp]
    if o_samples:
        uptime_text = f"{100.0 * sum(1 for o in o_samples if o) / len(o_samples):.1f}%"
    elif s.get("uptime_30d_pct") is not None:
        uptime_text = f"{s['uptime_30d_pct']:.1f}% (30-day figure, not enough history yet for a 7-day one)"
    else:
        uptime_text = "unknown"

    # ---- updates installed this week
    updates_week = [h for h in (u.get("history") or []) if h.get("t", 0) >= week_ago]
    updates_text = ", ".join(f"{h.get('from_build', '?')} → {h.get('to_build', '?')}" for h in updates_week) or "none this week"

    embed = {
        "title": f"This week on {WORLD_NAME}",
        "description": "The week's ledger, Vikings.",
        "color": 0xE3A54A,
        "fields": [
            {"name": "Hours logged", "value": hours_text, "inline": False},
            {"name": "Deaths", "value": deaths_text, "inline": False},
            {"name": "Raids", "value": raids_text, "inline": False},
            {"name": "Bosses defeated", "value": bosses_text, "inline": True},
            {"name": "Most at once", "value": most_text, "inline": True},
            {"name": "Uptime", "value": uptime_text, "inline": True},
            {"name": "Updates installed", "value": updates_text, "inline": False},
        ],
        "footer": {"text": f"{SERVER_NAME} on {WORLD_NAME} · weekly digest"},
    }
    if DASH:
        embed["url"] = DASH
    return embed


def medals_embed():
    """The week's medals as a second embed (Discord allows up to 10) -- kept separate from the
    7-field ledger above so neither one gets crowded. Returns None on any failure or if there is
    simply nothing to award this week; either way the ledger still posts."""
    if valheim_medals is None:
        return None
    try:
        return valheim_medals.weekly_embed()
    except Exception:
        return None


def main():
    embeds = [build_embed()]
    extra = medals_embed()
    if extra:
        embeds.append(extra)
    body = {"username": "Hermóðr", "embeds": embeds, "allowed_mentions": {"parse": []}}
    if DRY_RUN:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return
    if not WEBHOOK:
        raise SystemExit(0)
    req = urllib.request.Request(WEBHOOK, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "User-Agent": "valheim-digest/1"})
    try:
        urllib.request.urlopen(req, timeout=10).read()
        ok = "sent"
    except Exception as e:
        ok = f"FAILED {e}"
    with open(LOG, "a") as f:
        f.write(f"{time.strftime('%F %T')} {ok}: weekly digest\n")


if __name__ == "__main__":
    main()
