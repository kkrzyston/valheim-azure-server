#!/usr/bin/env python3
"""valheim-medals.py -- Discord "medals" for the configured world, computed from the dashboard's
own on-disk data. Library + CLI: every delivery channel calls the same compute() so they can
never disagree.

    compute(window, now) -> {medal_key: {winners, value, display, runners_up}}
    window in {"day", "week", "all"}

Inputs, all read-only (never written by anything else that matters to us):
  /var/lib/valheim-status/events.jsonl    append-only join/leave/death/raid/... log, never trimmed
  /var/lib/valheim-status/samples.jsonl   per-minute {t,p,c,m,o,rx,tx,w,pg}, 30 days
  /var/www/valheim/status.json            current snapshot (tombstones, raids, tracking_since, ...)
  /var/lib/valheim-status/alerts.json     valheim-alert.py's state -- read-only, boss_log lives here

Our own state is a separate file, /var/lib/valheim-status/medals.json (mode 0644). We never touch
alerts.json -- valheim-alert.py rewrites that wholesale every minute and would clobber us.

CLI modes:
  --check-records   recompute the curated live subset, diff against medals.json, post at most
                     2 newly-broken records (a third `ExecStart=` on valheim-status.service, 60s)
  --daily           "Yesterday on {world}" -- skips entirely if nobody played
  --weekly          print/post the week's medals embed (production use is `import valheim_medals`
                     from valheim-digest.py and call weekly_embed() directly)
  --board           upsert the pinned all-time hall of fame message
  --seed            record every all-time holder without posting (run once at deploy)
  --web             write the full day/week/all medals.json for the dashboard's Hall page
                    (pure file writer, never posts to Discord; run every 10 min)
  --dry-run         print embed/JSON instead of posting or writing, for any of the above

All paths take an env override so this can run against fixtures without touching /var:
  MEDALS_STATUS, MEDALS_EVENTS, MEDALS_SAMPLES, MEDALS_STATE, MEDALS_ALERTS, MEDALS_WEB_OUT, MEDALS_LOG

Never @everyone: every body we send carries allowed_mentions: {"parse": []}. Never print the
webhook URL. Every all-time string says "since {tracking_since}" -- never "all time" -- because
tracking is only a few days deep.
"""
import json, os, sys, time, math, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo("America/Los_Angeles")
except Exception:
    LOCAL_TZ = None

STATUS = os.environ.get("MEDALS_STATUS", "/var/www/valheim/status.json")
EVENTS = os.environ.get("MEDALS_EVENTS", "/var/lib/valheim-status/events.jsonl")
SAMPLES = os.environ.get("MEDALS_SAMPLES", "/var/lib/valheim-status/samples.jsonl")
STATE = os.environ.get("MEDALS_STATE", "/var/lib/valheim-status/medals.json")
ALERTS = os.environ.get("MEDALS_ALERTS", "/var/lib/valheim-status/alerts.json")
WEB_OUT = os.environ.get("MEDALS_WEB_OUT", "/var/www/valheim/medals.json")
LOG = os.environ.get("MEDALS_LOG", "/var/log/valheim-medals.log")
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
DASH = os.environ.get("DASHBOARD_URL", "").strip()
# From /etc/valheim-server.env (see the medals services' EnvironmentFile=).
SERVER_NAME = os.environ.get("SERVER_NAME", "Valheim Server").strip() or "Valheim Server"
WORLD_NAME = os.environ.get("WORLD_NAME", "Dedicated").strip() or "Dedicated"

DAY_S = 86400
WEEK_S = 7 * DAY_S
COOLDOWN_S = 6 * 3600
MAX_LIVE_POSTS = 2

COLORS = {"bad": 0xD0604A, "ok": 0x8FB877, "warn": 0xE0A44B, "info": 0x93A8AB, "torch": 0xE3A54A, "gold": 0xD4AF37}
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


# ---------------------------------------------------------------- small helpers (house style)
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_jsonl(path):
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def save(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o644)
    except Exception:
        pass


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%F %T')} {msg}\n")
    except Exception:
        pass


def dur(sec):
    """Mirrors valheim-alert.py/valheim-digest.py's dur(), extended with seconds so the short
    punny medals (shortest session, fastest death) read as "45 s" instead of "0 min"."""
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec} s"
    h, rem = divmod(sec, 3600)
    mnt = rem // 60
    if h:
        return f"{h} h {mnt} min" if mnt else f"{h} h"
    return f"{mnt} min"


def local_date(ts):
    if LOCAL_TZ:
        return datetime.fromtimestamp(ts, LOCAL_TZ).date()
    return datetime.fromtimestamp(ts, timezone.utc).date()


def local_epoch_of_date(d):
    dt = datetime(d.year, d.month, d.day)
    if LOCAL_TZ:
        return dt.replace(tzinfo=LOCAL_TZ).timestamp()
    return dt.replace(tzinfo=timezone.utc).timestamp()


def tod_seconds(t):
    """Seconds since local midnight -- the player's clock time-of-day."""
    if LOCAL_TZ:
        d = datetime.fromtimestamp(t, LOCAL_TZ)
    else:
        d = datetime.fromtimestamp(t, timezone.utc)
    return d.hour * 3600 + d.minute * 60 + d.second


def fmt_clock(secs):
    secs = int(secs) % DAY_S
    h, m = divmod(secs // 60, 60)
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ampm}"


def fmt_date(d):
    return f"{MONTHS[d.month - 1]} {d.day}"


def fmt_date_full(d):
    return f"{WEEKDAYS[d.weekday()]}, {MONTHS[d.month - 1]} {d.day}"


# Mirrors valheim-status-collect.py's hardcoded players.tracking_since (both must move together
# if tracking is ever reset); also what since_date_str()'s own fallback string below means.
TRACKING_SINCE_FALLBACK = 1789080000


def since_date_str(status):
    """"since 10 September" -- never "all time"; tracking is only ~4 days deep."""
    ts = (status.get("players") or {}).get("tracking_since")
    if not ts:
        return "since 10 September"
    d = local_date(ts)
    return f"since {d.day} {MONTHS[d.month - 1]}"


def tie_join(names):
    names = list(names)
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def clamp_desc(s, limit=3900):
    if len(s) <= limit:
        return s
    return s[:limit - 20].rsplit("\n", 1)[0] + "\n… (truncated)"


# ---------------------------------------------------------------- sessions, reused everywhere
# Same reconstruction valheim-status-collect.py already does (L818-829): walk join/leave pairs
# into (name, start, end) intervals, closing anything still open at `now`. We rebuild it here
# rather than importing collect.py, which runs its whole pipeline as a side effect of import.
def reconstruct_sessions(events):
    sessions = []
    open_since = {}
    for e in sorted(events, key=lambda e: e.get("t", 0)):
        name = e.get("name")
        kind = e.get("kind")
        if not name or kind not in ("join", "leave"):
            continue
        if kind == "join":
            if name in open_since:  # a join without a leave: close the old one here
                sessions.append((name, open_since[name], e["t"]))
            open_since[name] = e["t"]
        elif kind == "leave" and name in open_since:
            sessions.append((name, open_since.pop(name), e["t"]))
    for name, start in open_since.items():
        sessions.append((name, start, None))  # still online
    return sessions


def clip_sessions(sessions, wstart, wend, now):
    out = []
    for name, start, end in sessions:
        e = now if end is None else end
        e = min(e, wend)
        s = max(start, wstart)
        if e > s:
            out.append((name, s, e))
    return out


def online_at(sessions, t, now):
    return sorted({n for n, s, e in sessions if s <= t <= (now if e is None else e)})


def seconds_per_player(sessions):
    d = {}
    for name, s, e in sessions:
        d[name] = d.get(name, 0) + (e - s)
    return d


def sessions_per_player(sessions):
    d = {}
    for name, s, e in sessions:
        d.setdefault(name, []).append((s, e))
    return d


def deaths_per_player(events):
    d = {}
    for e in events:
        if e.get("kind") == "death":
            n = e.get("name", "someone")
            d[n] = d.get(n, 0) + 1
    return d


def played_between(sessions, name, t0, t1):
    total = 0
    for n, s, e in sessions:
        if n != name:
            continue
        a, b = max(s, t0), min(e, t1)
        if b > a:
            total += b - a
    return total


def pairwise_together(sessions):
    together = {}
    for i, (n1, a1, b1) in enumerate(sessions):
        for n2, a2, b2 in sessions[i + 1:]:
            if n1 == n2:
                continue
            ov = min(b1, b2) - max(a1, a2)
            if ov > 0:
                key = tuple(sorted((n1, n2)))
                together[key] = together.get(key, 0) + ov
    return together


def overlap_with_nightly_window(s, e, start_h=0, end_h=5):
    """seconds of [s, e) that fall between local start_h:00 and end_h:00, any number of days"""
    total = 0.0
    cur = s
    while cur < e:
        d = local_date(cur)
        midnight = local_epoch_of_date(d)
        night_a, night_b = midnight + start_h * 3600, midnight + end_h * 3600
        a, b = max(cur, night_a), min(e, night_b)
        if b > a:
            total += b - a
        cur = midnight + DAY_S
    return total


# ---------------------------------------------------------------- winner picking (ties honoured)
def pick(d, higher=True):
    """d: name -> value. Returns (winners[list, sorted], value, all_items) or None if fewer than
    two eligible players -- the anti-spam minimum-sample rule, applied uniformly."""
    items = [(n, v) for n, v in d.items() if v is not None]
    if len(items) < 2:
        return None
    best_val = max(v for _, v in items) if higher else min(v for _, v in items)
    winners = sorted(n for n, v in items if v == best_val)
    return winners, best_val, items


def runners_up(items, winners, higher=True, fmt=str, limit=3):
    rest = sorted([(n, v) for n, v in items if n not in winners], key=lambda kv: -kv[1] if higher else kv[1])
    return [{"name": n, "value": v, "display": fmt(v)} for n, v in rest[:limit]]


def finish(winners, value, display, items, higher=True, fmt=str, limit=3):
    return {"winners": winners, "value": value, "display": display,
            "runners_up": runners_up(items, winners, higher, fmt, limit)}


# ---------------------------------------------------------------- window bounds + context
def window_bounds(window, now, tracking_since):
    if window == "all":
        return tracking_since, now
    if window == "week":
        return now - WEEK_S, now
    if window == "day":
        today = local_date(now)
        y = today - timedelta(days=1)
        start = local_epoch_of_date(y)
        return start, start + DAY_S
    raise ValueError(f"unknown window {window!r}")


def build_context(events, samples, status, window, now):
    tracking_since = (status.get("players") or {}).get("tracking_since") or now
    wstart, wend = window_bounds(window, now, tracking_since)
    all_sessions = reconstruct_sessions(events)
    sessions = clip_sessions(all_sessions, wstart, wend, now)
    closed = [(n, s, e) for n, s, e in all_sessions if e is not None]
    closed_sessions = clip_sessions(closed, wstart, wend, now)
    wevents = [e for e in events if wstart <= e.get("t", 0) < wend]
    # Raw (unclipped) join/leave instants that actually happened inside the window -- distinct
    # from `sessions`/`closed_sessions` above, whose ends get clamped to wstart/wend for anyone
    # who was already connected across the boundary. Using the clamped versions for "earliest
    # login" etc. would report a fake login at exactly local midnight; these don't.
    joins_in_window = [(n, s) for n, s, e in all_sessions if wstart <= s < wend]
    closes_in_window = [(n, e) for n, s, e in all_sessions if e is not None and wstart <= e < wend]
    return {
        "window": window, "wstart": wstart, "wend": wend, "now": now,
        "sessions": sessions, "closed_sessions": closed_sessions, "all_sessions": all_sessions,
        "joins_in_window": joins_in_window, "closes_in_window": closes_in_window,
        "events": wevents, "all_events": events, "samples": samples, "status": status,
    }


# ================================================================== medal implementations
# Time & attendance --------------------------------------------------------------------
def m_longhouse_dweller(ctx):
    p = pick(seconds_per_player(ctx["sessions"]), higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 1800:
        return None
    return finish(winners, val, dur(val), items, True, dur)


def m_ironman(ctx):
    best = {}
    for name, s, e in ctx["sessions"]:
        length = e - s
        if length > best.get(name, -1):
            best[name] = length
    p = pick(best, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 1800:
        return None
    return finish(winners, val, dur(val), items, True, dur)


def m_first_light(ctx):
    best = {}
    for name, s in ctx["joins_in_window"]:
        tod = tod_seconds(s)
        if name not in best or tod < best[name]:
            best[name] = tod
    p = pick(best, higher=False)
    if not p:
        return None
    winners, val, items = p
    return finish(winners, val, fmt_clock(val), items, False, fmt_clock)


def m_night_watch(ctx):
    best = {}
    for name, e in ctx["closes_in_window"]:
        tod = tod_seconds(e)
        if name not in best or tod > best[name]:
            best[name] = tod
    p = pick(best, higher=True)
    if not p:
        return None
    winners, val, items = p
    return finish(winners, val, fmt_clock(val), items, True, fmt_clock)


def m_the_regular(ctx):
    if ctx["window"] != "all":
        return None
    streaks = {}
    for name, s, e in ctx["sessions"]:
        days = streaks.setdefault(name, set())
        d = local_date(s)
        d1 = local_date(e)
        while d <= d1:
            days.add(d)
            d += timedelta(days=1)
    best = {}
    for name, days in streaks.items():
        ds = sorted(days)
        run, longest = 1, 1
        for i in range(1, len(ds)):
            run = run + 1 if (ds[i] - ds[i - 1]).days == 1 else 1
            longest = max(longest, run)
        best[name] = longest
    p = pick(best, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 2:
        return None
    return finish(winners, val, f"{val} days running", items, True, lambda v: f"{v}d")


def m_hall_opener(ctx):
    if ctx["window"] not in ("week", "all"):
        return None
    first_by_day = {}
    for name, s in ctx["joins_in_window"]:
        d = local_date(s)
        if d not in first_by_day or s < first_by_day[d][1]:
            first_by_day[d] = (name, s)
    counts = {}
    for name, _ in first_by_day.values():
        counts[name] = counts.get(name, 0) + 1
    p = pick(counts, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 2:
        return None
    return finish(winners, val, f"{val} days", items, True, lambda v: f"{v}d")


def m_last_torch_out(ctx):
    if ctx["window"] not in ("week", "all"):
        return None
    last_by_day = {}
    for name, e in ctx["closes_in_window"]:
        d = local_date(e)
        if d not in last_by_day or e > last_by_day[d][1]:
            last_by_day[d] = (name, e)
    counts = {}
    for name, _ in last_by_day.values():
        counts[name] = counts.get(name, 0) + 1
    p = pick(counts, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 2:
        return None
    return finish(winners, val, f"{val} days", items, True, lambda v: f"{v}d")


# Deaths & survival ----------------------------------------------------------------------
def m_frequent_flyer(ctx):
    p = pick(deaths_per_player(ctx["events"]), higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 2:
        return None
    return finish(winners, val, f"{val} deaths", items, True, lambda v: f"{v}")


def m_the_unkillable(ctx):
    deaths = {}
    for e in ctx["events"]:
        if e.get("kind") == "death":
            deaths.setdefault(e["name"], []).append(e["t"])
    best = {}
    for name, ts in deaths.items():
        ts = sorted(ts)
        if len(ts) < 2:
            continue
        gaps = [played_between(ctx["sessions"], name, ts[i], ts[i + 1]) for i in range(len(ts) - 1)]
        if gaps:
            best[name] = max(gaps)
    p = pick(best, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 1800:
        return None
    return finish(winners, val, dur(val), items, True, dur)


def m_red_wedding(ctx):
    deaths_by_name = {}
    for e in ctx["events"]:
        if e.get("kind") == "death":
            deaths_by_name.setdefault(e["name"], []).append(e["t"])
    best = {}
    for name, s, e in ctx["sessions"]:
        ts = deaths_by_name.get(name, [])
        cnt = sum(1 for t in ts if s <= t <= e)
        if cnt > best.get(name, 0):
            best[name] = cnt
    p = pick(best, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 2:
        return None
    return finish(winners, val, f"{val} in one sitting", items, True, lambda v: f"{v}")


def m_eager_for_valhalla(ctx):
    deaths_by_name = {}
    for e in ctx["events"]:
        if e.get("kind") == "death":
            deaths_by_name.setdefault(e["name"], []).append(e["t"])
    best = {}
    for name, s, e in ctx["sessions"]:
        ts = sorted(t for t in deaths_by_name.get(name, []) if s <= t <= e)
        if ts:
            gap = ts[0] - s
            if name not in best or gap < best[name]:
                best[name] = gap
    p = pick(best, higher=False)
    if not p:
        return None
    winners, val, items = p
    return finish(winners, val, dur(val), items, False, dur)


def m_statistically_unlucky(ctx):
    secs = seconds_per_player(ctx["sessions"])
    deaths = deaths_per_player(ctx["events"])
    rate = {}
    for name, s in secs.items():
        if s >= 2 * 3600:
            rate[name] = deaths.get(name, 0) / (s / 3600.0)
    p = pick(rate, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val <= 0:
        return None
    return finish(winners, val, f"{val:.2f} deaths/h", items, True, lambda v: f"{v:.2f}/h")


# Social & co-op ---------------------------------------------------------------------------
def m_shield_brothers(ctx):
    together = pairwise_together(ctx["sessions"])
    if not together:
        return None
    best_val = max(together.values())
    if best_val < 1800:
        return None
    pairs = sorted(k for k, v in together.items() if v == best_val)
    winners = list(pairs[0])  # a genuine tie between pairs is vanishingly unlikely; keep the first
    runners = sorted(((k, v) for k, v in together.items() if k != pairs[0]), key=lambda kv: -kv[1])[:3]
    return {"winners": winners, "value": best_val, "display": dur(best_val),
            "runners_up": [{"name": " & ".join(k), "value": v, "display": dur(v)} for k, v in runners]}


def m_full_hall(ctx):
    samples = [s for s in ctx["samples"] if ctx["wstart"] <= s.get("t", 0) < ctx["wend"]]
    if not samples:
        return None
    best = max(samples, key=lambda s: s.get("p", 0) or 0)
    if (best.get("p") or 0) < 2:
        return None
    names = online_at(ctx["all_sessions"], best["t"], ctx["now"])
    if len(names) < 2:
        return None
    # the A2S count is authoritative but the log may not have surfaced every
    # character name yet, so only name them when the two actually agree
    exact = len(names) == (best.get("p") or 0)
    return {"winners": names if exact else [], "value": best["p"],
            "display": f"{best['p']} Vikings at once", "runners_up": []}


def m_the_hermit(ctx):
    sessions = ctx["sessions"]
    if not sessions:
        return None
    pts = []
    for name, s, e in sessions:
        pts.append((s, 1, name))
        pts.append((e, -1, name))
    pts.sort(key=lambda x: x[0])
    active = {}
    alone = {}
    prev_t = None
    for t, delta, name in pts:
        if prev_t is not None and t > prev_t and len(active) == 1:
            only = next(iter(active))
            alone[only] = alone.get(only, 0) + (t - prev_t)
        if delta == 1:
            active[name] = active.get(name, 0) + 1
        else:
            active[name] = active.get(name, 0) - 1
            if active[name] <= 0:
                active.pop(name, None)
        prev_t = t
    p = pick(alone, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 1800:
        return None
    return finish(winners, val, dur(val), items, True, dur)


def m_the_wingman(ctx):
    joins = ctx["joins_in_window"]
    counts = {}
    for name, s in joins:
        near = any(name2 != name and abs(s2 - s) <= 300 for name2, s2 in joins)
        if near:
            counts[name] = counts.get(name, 0) + 1
    p = pick(counts, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 2:
        return None
    return finish(winners, val, f"{val} times", items, True, lambda v: f"{v}")


# World & exploration ------------------------------------------------------------------------
def m_far_from_home(ctx):
    if ctx["window"] != "all":
        return None
    tombs = ((ctx["status"].get("world") or {}).get("extra") or {}).get("tombstones") or []
    best = {}
    for tb in tombs:
        owner = tb.get("owner")
        if not owner:
            continue
        distv = math.hypot(tb.get("x", 0), tb.get("z", 0))
        if owner not in best or distv > best[owner]:
            best[owner] = distv
    p = pick(best, higher=True)
    if not p:
        return None
    winners, val, items = p
    return finish(winners, val, f"{val:.0f} m from spawn", items, True, lambda v: f"{v:.0f} m")


def m_raid_magnet(ctx):
    raids = [e for e in ctx["events"] if e.get("kind") == "raid"]
    if not raids:
        return None
    raid_list = ((ctx["status"].get("world") or {}).get("raids") or {}).get("list") or []
    counts = {}
    for e in raids:
        names = None
        for r in raid_list:
            if abs(r.get("t", -1e18) - e["t"]) < 5:
                names = r.get("online", [])
                break
        if names is None:
            names = online_at(ctx["all_sessions"], e["t"], ctx["now"])
        for n in names:
            counts[n] = counts.get(n, 0) + 1
    p = pick(counts, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 2:
        return None
    return finish(winners, val, f"{val} raids", items, True, lambda v: f"{v}")


def m_stood_at_the_fall(ctx):
    alerts_state = load(ALERTS, {})
    boss_log = alerts_state.get("boss_log") or []
    in_window = [b for b in boss_log if ctx["wstart"] <= b.get("t", 0) < ctx["wend"]]
    if not in_window:
        return None
    counts = {}
    for b in in_window:
        for n in online_at(ctx["all_sessions"], b["t"], ctx["now"]):
            counts[n] = counts.get(n, 0) + 1
    p = pick(counts, higher=True)
    if not p:
        return None
    winners, val, items = p
    return finish(winners, val, f"{val} boss fall{'s' if val != 1 else ''} witnessed", items, True, lambda v: f"{v}")


# For fun / punny -----------------------------------------------------------------------------
def m_tourist_trap(ctx):
    if ctx["window"] != "all":
        return None
    tombs = ((ctx["status"].get("world") or {}).get("extra") or {}).get("tombstones") or []
    by_owner = {}
    for tb in tombs:
        by_owner.setdefault(tb.get("owner"), []).append(tb)
    best = {}
    for owner, ts in by_owner.items():
        if not owner or len(ts) < 2:
            continue
        best[owner] = min(math.hypot(a["x"] - b["x"], a["z"] - b["z"]) for i, a in enumerate(ts) for b in ts[i + 1:])
    p = pick(best, higher=False)
    if not p:
        return None
    winners, val, items = p
    return finish(winners, val, f"{val:.0f} m apart", items, False, lambda v: f"{v:.0f} m")


def m_the_yoyo(ctx):
    if ctx["window"] != "day":
        return None
    counts = {}
    for name, s in ctx["joins_in_window"]:
        counts[name] = counts.get(name, 0) + 1
    p = pick(counts, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 3:
        return None
    return finish(winners, val, f"{val} sessions in a day", items, True, lambda v: f"{v}")


def m_lag_lord(ctx):
    samples = [s for s in ctx["samples"] if ctx["wstart"] <= s.get("t", 0) < ctx["wend"] and s.get("pg")]
    sums, cnts = {}, {}
    for s in samples:
        for n, ms in s["pg"].items():
            if ms is None:
                continue
            sums[n] = sums.get(n, 0) + ms
            cnts[n] = cnts.get(n, 0) + 1
    avg = {n: sums[n] / cnts[n] for n in sums if cnts[n] >= 5}
    p = pick(avg, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 80:
        return None
    return finish(winners, val, f"{val:.0f} ms avg", items, True, lambda v: f"{v:.0f} ms")


def m_blink(ctx):
    best = {}
    for name, s, e in ctx["closed_sessions"]:
        length = e - s
        if name not in best or length < best[name]:
            best[name] = length
    p = pick(best, higher=False)
    if not p:
        return None
    winners, val, items = p
    if val > 300:
        return None
    return finish(winners, val, dur(val), items, False, dur)


def m_3am_club(ctx):
    totals = {}
    for name, s, e in ctx["sessions"]:
        totals[name] = totals.get(name, 0) + overlap_with_nightly_window(s, e)
    p = pick(totals, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 1800:
        return None
    return finish(winners, val, dur(val), items, True, dur)


def m_clockwork_viking(ctx):
    if ctx["window"] not in ("week", "all"):
        return None
    by_name = {}
    for name, s, e in ctx["sessions"]:
        by_name.setdefault(name, []).append(tod_seconds(s))
    var = {}
    for name, tods in by_name.items():
        if len(tods) < 3:
            continue
        mean = sum(tods) / len(tods)
        var[name] = sum((t - mean) ** 2 for t in tods) / len(tods)
    p = pick(var, higher=False)
    if not p:
        return None
    winners, val, items = p
    return finish(winners, val, f"±{dur(math.sqrt(val))}", items, False, lambda v: f"±{dur(math.sqrt(v))}")


def m_wrong_place_wrong_time(ctx):
    raids = [e for e in ctx["events"] if e.get("kind") == "raid"]
    if not raids:
        return None
    raid_list = ((ctx["status"].get("world") or {}).get("raids") or {}).get("list") or []
    counts = {}
    for e in raids:
        names = None
        for r in raid_list:
            if abs(r.get("t", -1e18) - e["t"]) < 5:
                names = r.get("online", [])
                break
        if names is None:
            names = online_at(ctx["all_sessions"], e["t"], ctx["now"])
        if len(names) == 1:
            counts[names[0]] = counts.get(names[0], 0) + 1
    p = pick(counts, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 2:
        return None
    return finish(winners, val, f"{val} solo raids", items, True, lambda v: f"{v}")


def m_overachiever(ctx, other_results):
    counts = {}
    for key, res in other_results.items():
        if not res:
            continue
        for n in res.get("winners", []):
            counts[n] = counts.get(n, 0) + 1
    p = pick(counts, higher=True)
    if not p:
        return None
    winners, val, items = p
    if val < 2:
        return None
    return finish(winners, val, f"{val} medals", items, True, lambda v: f"{v}")


# ---------------------------------------------------------------- rendering
def pluralize_verb(verb):
    """Cheap singular->plural conjugation for the handful of present-tense verb phrases in the
    catalogue (past-tense ones like "died"/"logged"/"pulled" need no change for a tied group)."""
    for a, b in (("has ", "have "), ("was ", "were "), ("keeps ", "keep "), ("logs ", "log ")):
        if verb.startswith(a):
            return b + verb[len(a):]
    return verb


def render_default(entry, res):
    names, value, verb = res["winners"], res["display"], entry["verb"]
    if len(names) == 1:
        return f"{entry['emoji']} **{entry['name']}** — {names[0]} {verb}: {value}."
    who = tie_join(names)
    return f"{entry['emoji']} **{entry['name']}** — {who} are tied — {pluralize_verb(verb)}: {value}."


def render_shield_brothers(entry, res):
    a, b = res["winners"]
    return f"{entry['emoji']} **{entry['name']}** — {a} and {b} {entry['verb']}: {res['display']}."


def render_full_hall(entry, res):
    if not res["winners"]:
        return f"{entry['emoji']} **{entry['name']}** — the hall was at its fullest: {res['display']}."
    who = tie_join(res["winners"])
    return f"{entry['emoji']} **{entry['name']}** — {who} were all in the hall at once: {res['display']}."


def render(entry, res):
    fn = entry.get("render", render_default)
    return fn(entry, res)


# ================================================================== the catalogue
# One declarative table -- add a medal by adding one entry. `windows` says which of
# day/week/all it can be awarded in; `live` marks the curated ~8-medal subset that
# --check-records is allowed to interrupt the channel for; `higher` says which direction
# counts as "a new record" for that live subset.
CATALOG = [
    # -- time & attendance
    dict(key="longhouse_dweller", emoji="\U0001FA93", name="The Longhouse Dweller", category="time",
         windows=("day", "week", "all"), live=True, higher=True, fn=m_longhouse_dweller,
         verb="has logged the most hours",
         howto="Most total seconds played across all sessions in the window."),
    dict(key="ironman", emoji="⏳", name="Ironman", category="time",
         windows=("day", "week", "all"), live=True, higher=True, fn=m_ironman,
         verb="pulled the longest unbroken session",
         howto="Longest single unbroken session in the window; min 30 minutes."),
    dict(key="first_light", emoji="\U0001F305", name="First Light", category="time",
         windows=("day", "week", "all"), live=True, higher=False, fn=m_first_light,
         verb="logged in earliest",
         howto="Earliest local login time-of-day among logins in the window."),
    dict(key="night_watch", emoji="\U0001F319", name="Night Watch", category="time",
         windows=("day", "week", "all"), live=True, higher=True, fn=m_night_watch,
         verb="logged out latest",
         howto="Latest local logout time-of-day among logouts in the window."),
    dict(key="the_regular", emoji="\U0001F501", name="The Regular", category="time",
         windows=("all",), live=True, higher=True, fn=m_the_regular,
         verb="kept the longest daily streak",
         howto="All-time only. Longest run of consecutive calendar days played; min 2 days."),
    dict(key="hall_opener", emoji="\U0001F6AA", name="Hall Opener", category="time",
         windows=("week", "all"), live=False, higher=True, fn=m_hall_opener,
         verb="was first in most often",
         howto="Week/all-time. Most days being first to join that day; min 2."),
    dict(key="last_torch_out", emoji="\U0001F512", name="Last Torch Out", category="time",
         windows=("week", "all"), live=False, higher=True, fn=m_last_torch_out,
         verb="was last out most often",
         howto="Week/all-time. Most days being last to leave that day; min 2."),
    # -- deaths & survival
    dict(key="frequent_flyer", emoji="\U0001F480", name="Valhalla Frequent Flyer", category="death",
         windows=("day", "week", "all"), live=True, higher=True, fn=m_frequent_flyer,
         verb="has died the most",
         howto="Most deaths recorded in the window; min 2."),
    dict(key="the_unkillable", emoji="\U0001F6E1️", name="The Unkillable", category="death",
         windows=("day", "week", "all"), live=True, higher=True, fn=m_the_unkillable,
         verb="went the longest between deaths",
         howto="Longest actual played-time gap between two of your own deaths; min 30 minutes."),
    dict(key="red_wedding", emoji="\U0001FA78", name="Red Wedding", category="death",
         windows=("day", "week", "all"), live=False, higher=True, fn=m_red_wedding,
         verb="died the most in one sitting",
         howto="Most deaths suffered in a single session; min 2."),
    dict(key="eager_for_valhalla", emoji="⚰️", name="Eager for Valhalla", category="death",
         windows=("day", "week", "all"), live=False, higher=False, fn=m_eager_for_valhalla,
         verb="died fastest after logging in",
         howto="Shortest time from login to that session's first death."),
    dict(key="statistically_unlucky", emoji="\U0001F4CA", name="Statistically Unlucky", category="death",
         windows=("day", "week", "all"), live=False, higher=True, fn=m_statistically_unlucky,
         verb="has the worst deaths per hour",
         howto="Worst deaths-per-hour rate; only players with 2+ hours played count."),
    # -- social & co-op
    dict(key="shield_brothers", emoji="\U0001F91D", name="Shield Brothers", category="social",
         windows=("day", "week", "all"), live=False, higher=True, fn=m_shield_brothers,
         verb="have spent the most time playing together",
         howto="Pair of players with the most overlapping online time together; min 30 minutes.",
         render=render_shield_brothers),
    dict(key="full_hall", emoji="\U0001F389", name="Full Hall", category="social",
         windows=("day", "week", "all"), live=True, higher=True, fn=m_full_hall,
         verb="",
         howto="Most players online at the same instant, from per-minute samples; min 2. Names shown only when the sample count matches exactly.",
         render=render_full_hall),
    dict(key="the_hermit", emoji="\U0001F9CD", name="The Hermit", category="social",
         windows=("day", "week", "all"), live=False, higher=True, fn=m_the_hermit,
         verb="spent the most hours playing alone",
         howto="Most time spent online while no one else was; min 30 minutes."),
    dict(key="the_wingman", emoji="\U0001F37B", name="The Wingman", category="social",
         windows=("day", "week", "all"), live=False, higher=True, fn=m_the_wingman,
         verb="keeps logging in right after someone else",
         howto="Most logins landing within 5 minutes of someone else's login; min 2."),
    # -- world & exploration
    dict(key="far_from_home", emoji="\U0001F5FA️", name="Far From Home", category="world",
         windows=("all",), live=False, higher=True, fn=m_far_from_home,
         verb="has a tombstone furthest from spawn",
         howto="All-time only. Whoever has a tombstone furthest (straight-line) from spawn."),
    dict(key="raid_magnet", emoji="\U0001F329️", name="Raid Magnet", category="world",
         windows=("day", "week", "all"), live=False, higher=True, fn=m_raid_magnet,
         verb="was present for the most raids",
         howto="Present online for the most raid events in the window; min 2."),
    dict(key="stood_at_the_fall", emoji="\U0001F43A", name="Stood at the Fall", category="world",
         windows=("day", "week", "all"), live=False, higher=True, fn=m_stood_at_the_fall,
         verb="was online for the most boss falls",
         howto="Online for the most boss-defeat moments (from the boss log)."),
    # -- for fun / punny
    dict(key="tourist_trap", emoji="\U0001FAA6", name="Tourist Trap", category="fun",
         windows=("all",), live=False, higher=False, fn=m_tourist_trap,
         verb="left two tombstones closest together",
         howto="All-time only. Your own two tombstones that ended up closest together; needs 2+."),
    dict(key="the_yoyo", emoji="\U0001F3A3", name="The Yo-Yo", category="fun",
         windows=("day",), live=False, higher=True, fn=m_the_yoyo,
         verb="joined and left the most in one day",
         howto="Day window only. Most join events in a single day; min 3."),
    dict(key="lag_lord", emoji="\U0001F4F6", name="Lag Lord", category="fun",
         windows=("day", "week", "all"), live=False, higher=True, fn=m_lag_lord,
         verb="has the worst average ping",
         howto="Worst average ping across samples, min 5 samples; at least 80 ms."),
    dict(key="blink", emoji="\U0001F4A8", name="Blink and You'll Miss It", category="fun",
         windows=("day", "week", "all"), live=False, higher=False, fn=m_blink,
         verb="had the shortest session",
         howto="Shortest completed session (join to leave), 5 minutes or less."),
    dict(key="the_3am_club", emoji="\U0001F550", name="The 3 AM Club", category="fun",
         windows=("day", "week", "all"), live=False, higher=True, fn=m_3am_club,
         verb="spent the most hours between midnight and 5 AM",
         howto="Most hours played between local midnight and 5 AM; min 30 minutes."),
    dict(key="clockwork_viking", emoji="⏰", name="Clockwork Viking", category="fun",
         windows=("week", "all"), live=False, higher=False, fn=m_clockwork_viking,
         verb="logs in at the steadiest time of day",
         howto="Week/all-time. Steadiest daily login time-of-day (lowest variance); 3+ sessions needed."),
    dict(key="wrong_place_wrong_time", emoji="\U0001F9F2", name="Wrong Place, Wrong Time", category="fun",
         windows=("day", "week", "all"), live=False, higher=True, fn=m_wrong_place_wrong_time,
         verb="faced the most raids alone",
         howto="Most raids faced while completely alone on the server; min 2."),
    dict(key="overachiever", emoji="\U0001F3C6", name="Overachiever", category="fun",
         windows=("day", "week", "all"), live=False, higher=True, fn=None,
         verb="won the most other medals",
         howto="Meta-medal: won the most other medals in this same window; min 2."),
]
CATALOG_BY_KEY = {e["key"]: e for e in CATALOG}
LIVE_KEYS = [e["key"] for e in CATALOG if e["live"]]
NEEDS_SAMPLES = {"full_hall", "lag_lord"}

CATEGORIES = ("time", "death", "social", "world", "fun")


def _validate_catalog():
    """Guards the "add a medal by adding one entry" property: every entry must have a category
    from the fixed set (used for grouping on the web page) and a unique key."""
    seen = set()
    for e in CATALOG:
        if e["key"] in seen:
            raise ValueError(f"duplicate CATALOG key {e['key']!r}")
        seen.add(e["key"])
        if e.get("category") not in CATEGORIES:
            raise ValueError(f"CATALOG entry {e['key']!r} has no valid category (got {e.get('category')!r})")


_validate_catalog()


# ---------------------------------------------------------------- compute()
def compute(window, now=None, keys=None):
    now = now if now is not None else time.time()
    status = load(STATUS, {})
    events = load_jsonl(EVENTS)
    entries = [e for e in CATALOG if window in e["windows"] and (keys is None or e["key"] in keys)]
    need_samples = any(e["key"] in NEEDS_SAMPLES for e in entries)
    samples = load_jsonl(SAMPLES) if need_samples else []
    ctx = build_context(events, samples, status, window, now)

    results = {}
    overachiever_entry = None
    for e in entries:
        if e["key"] == "overachiever":
            overachiever_entry = e
            continue
        try:
            res = e["fn"](ctx)
        except Exception as exc:
            log(f"medal {e['key']} raised {exc!r}")
            res = None
        if res:
            results[e["key"]] = res
    if overachiever_entry:
        try:
            res = m_overachiever(ctx, results)
        except Exception as exc:
            log(f"medal overachiever raised {exc!r}")
            res = None
        if res:
            results[overachiever_entry["key"]] = res
    return results, ctx


def sig(res):
    return {"names": list(res["winners"]), "value": res["value"]}


def broken(entry, prev, res):
    if not prev:
        return False
    pv, nv = prev.get("value"), res.get("value")
    if pv is None or nv is None:
        return False
    return nv > pv if entry["higher"] else nv < pv


# ---------------------------------------------------------------- embeds
def ordered_lines(results):
    return [render(e, results[e["key"]]) for e in CATALOG if e["key"] in results]


def daily_embed(now=None):
    now = now if now is not None else time.time()
    results, ctx = compute("day", now=now)
    if not results:
        return None
    y = local_date(ctx["wstart"])
    status = ctx["status"]
    desc = f"{fmt_date_full(y)}:\n\n" + "\n".join(ordered_lines(results))
    embed = {"title": f"Yesterday on {WORLD_NAME}", "description": clamp_desc(desc),
             "color": COLORS["ok"], "footer": {"text": f"{SERVER_NAME} on {WORLD_NAME} · daily medals"}}
    if DASH:
        embed["url"] = DASH
    return embed


def weekly_embed(now=None):
    """Called by valheim-digest.py for the Sunday message's second embed. Returns None (never
    raises outward on a normal 'nothing qualified' result) if there is nothing to show."""
    now = now if now is not None else time.time()
    results, ctx = compute("week", now=now)
    if not results:
        return None
    desc = "\n".join(ordered_lines(results))
    return {"title": "Medals this week", "description": clamp_desc(desc), "color": COLORS["gold"],
            "footer": {"text": f"{SERVER_NAME} on {WORLD_NAME} · medals"}}


def board_embed():
    results, ctx = compute("all")
    status = ctx["status"]
    fields = []
    for e in CATALOG:
        res = results.get(e["key"])
        if not res:
            continue
        who = tie_join(res["winners"])
        fields.append({"name": f"{e['emoji']} {e['name']}", "value": f"{who} — {res['display']}", "inline": True})
    fields = fields[:24]
    embed = {"title": f"{WORLD_NAME} Hall of Fame",
             "description": f"All-time records, {since_date_str(status)}.",
             "color": COLORS["gold"], "fields": fields,
             "footer": {"text": f"{SERVER_NAME} on {WORLD_NAME} · medals"}}
    if DASH:
        embed["url"] = DASH
    return embed


def record_embed(entry, res, status):
    line = render(entry, res)
    return {"title": f"New record: {entry['name']}",
            "description": f"{line}\n\nA new all-time high on {WORLD_NAME}, {since_date_str(status)}.",
            "color": COLORS["gold"], "footer": {"text": f"{SERVER_NAME} on {WORLD_NAME} · medals"}}


# ---------------------------------------------------------------- Discord I/O (mirrors valheim-alert.py)
def discord_call(url, body, method):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json",
                                 "User-Agent": "valheim-medals/1"}, method=method)
    return urllib.request.urlopen(req, timeout=10)


def post_embeds(embeds):
    if not WEBHOOK or not embeds:
        return
    body = {"username": "Hermóðr", "embeds": embeds, "allowed_mentions": {"parse": []}}
    try:
        discord_call(WEBHOOK, body, "POST").read()
        log(f"sent: {len(embeds)} embed(s)")
    except Exception as e:
        log(f"FAILED {e!r}")


def update_board(embed, st):
    body = {"username": "Hermóðr", "embeds": [embed], "allowed_mentions": {"parse": []}}
    mid = st.get("board_message_id")
    if mid:
        try:
            discord_call(f"{WEBHOOK}/messages/{mid}", body, "PATCH").read()
            log(f"board edited: {mid}")
            return
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log(f"board PATCH FAILED {e}")
                return
            # message was deleted -- fall through and create a new one
        except Exception as e:
            log(f"board PATCH FAILED {e}")
            return
    try:
        resp = discord_call(f"{WEBHOOK}?wait=true", body, "POST")
        created = json.loads(resp.read())
        st["board_message_id"] = created["id"]
        log(f"board created: {created['id']}")
    except Exception as e:
        log(f"board CREATE FAILED {e}")


# ---------------------------------------------------------------- CLI modes
def cmd_seed(dry):
    results, ctx = compute("all")
    st = load(STATE, {})
    st["all_time"] = {k: sig(v) for k, v in results.items()}
    st["seeded"] = True
    if dry:
        print(json.dumps(st["all_time"], indent=2, ensure_ascii=False))
        return
    save(STATE, st)
    print(f"seeded {len(st['all_time'])} all-time records, posted nothing")


def cmd_check_records(dry):
    t0 = time.time()
    try:
        st = load(STATE, {})
        results, ctx = compute("all", now=t0, keys=LIVE_KEYS)
        all_time = st.setdefault("all_time", {})
        cooldowns = st.setdefault("cooldowns", {})

        if not st.get("seeded"):
            # never seeded (e.g. --seed was skipped) -- seed the live subset silently, post nothing
            for key, res in results.items():
                all_time.setdefault(key, sig(res))
            st["seeded"] = True
            if not dry:
                save(STATE, st)
            log(f"check-records: auto-seeded ({int((time.time() - t0) * 1000)} ms)")
            return

        to_post = []
        for key, res in results.items():
            prev = all_time.get(key)
            if prev is None:
                all_time[key] = sig(res)  # first sight of this key -- seed silently, no burst
                continue
            if broken(CATALOG_BY_KEY[key], prev, res):
                to_post.append((key, res))

        posted, embeds = 0, []
        status = ctx["status"]
        for key, res in to_post:
            if posted >= MAX_LIVE_POSTS:
                break
            if t0 - cooldowns.get(key, 0) < COOLDOWN_S:
                continue
            entry = CATALOG_BY_KEY[key]
            embeds.append(record_embed(entry, res, status))
            all_time[key] = sig(res)
            cooldowns[key] = t0
            posted += 1

        if dry:
            print(json.dumps({"would_post": embeds, "elapsed_ms": int((time.time() - t0) * 1000)},
                              indent=2, ensure_ascii=False))
            return
        if embeds:
            post_embeds(embeds)
        save(STATE, st)
        log(f"check-records: posted {posted} ({int((time.time() - t0) * 1000)} ms)")
    except Exception as e:
        # non-negotiable: this runs every 60s inside valheim-status.service and must never fail the unit
        log(f"check-records ERROR {e!r}")


def cmd_daily(dry):
    # one post per local day: Persistent=true replays a missed timer after a reboot,
    # and a manual run must not double up on what the timer already sent
    today = str(local_date(time.time()))
    if not dry:
        st = load(STATE, {})
        if st.get("last_daily_date") == today:
            log("daily: already posted today, skipped")
            return
    embed = daily_embed()
    if embed is None:
        if dry:
            print(json.dumps({"skipped": "no medals -- quiet day"}, indent=2))
        else:
            log("daily: skipped (no medals)")
        return
    body = {"username": "Hermóðr", "embeds": [embed], "allowed_mentions": {"parse": []}}
    if dry:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return
    post_embeds([embed])
    st = load(STATE, {})
    st["last_daily_date"] = today
    save(STATE, st)


def cmd_weekly(dry):
    embed = weekly_embed()
    if embed is None:
        if dry:
            print(json.dumps({"skipped": "no medals this week"}, indent=2))
        else:
            log("weekly: skipped (no medals)")
        return
    body = {"username": "Hermóðr", "embeds": [embed], "allowed_mentions": {"parse": []}}
    if dry:
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return
    post_embeds([embed])


def cmd_board(dry):
    embed = board_embed()
    if dry:
        print(json.dumps({"username": "Hermóðr", "embeds": [embed], "allowed_mentions": {"parse": []}},
                          indent=2, ensure_ascii=False))
        return
    if not WEBHOOK:
        return
    st = load(STATE, {})
    update_board(embed, st)
    save(STATE, st)
    # keep the web feed fresh on the same daily cadence; cmd_web() never raises outward
    cmd_web(dry=False)


def web_payload(now):
    """The full /var/www/valheim/medals.json payload (see PLAN-v5's data contract). Computes all
    three windows itself and emits the engine's own display/runners_up verbatim -- the page must
    duplicate no medal logic. `now` is threaded through so tests can pin it."""
    windows = {}
    all_time = {}
    status = {}
    for w in ("day", "week", "all"):
        results, ctx = compute(w, now=now)
        windows[w] = {
            key: {"names": list(res["winners"]), "value": res["value"], "display": res["display"],
                  "runners_up": res["runners_up"]}
            for key, res in results.items()
        }
        if w == "all":
            all_time = {k: sig(v) for k, v in results.items()}
            status = ctx["status"]

    tracking_since = (status.get("players") or {}).get("tracking_since") or TRACKING_SINCE_FALLBACK
    day_start, _ = window_bounds("day", now, tracking_since)
    return {
        "generated": int(now),
        "tracking_since": int(tracking_since),
        "day_label": fmt_date_full(local_date(day_start)),
        "catalog": [
            {"key": e["key"], "emoji": e["emoji"], "name": e["name"], "category": e["category"],
             "verb": e["verb"], "howto": e["howto"], "windows": list(e["windows"]), "live": e["live"]}
            for e in CATALOG
        ],
        "windows": windows,
        "all_time": all_time,  # unchanged shape, kept for compatibility
    }


def cmd_web(dry):
    t0 = time.time()
    try:
        payload = web_payload(t0)
        payload["cost_ms"] = int((time.time() - t0) * 1000)
        if dry:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
            return
        save(WEB_OUT, payload)
        log(f"web: wrote {WEB_OUT} ({payload['cost_ms']} ms, {len(payload['catalog'])} medals)")
    except Exception as e:
        # non-negotiable: this runs on its own 10-min timer and must never fail the unit
        log(f"web ERROR {e!r}")


def main():
    argv = sys.argv[1:]
    dry = "--dry-run" in argv
    modes = [
        ("--seed", cmd_seed),
        ("--check-records", cmd_check_records),
        ("--daily", cmd_daily),
        ("--weekly", cmd_weekly),
        ("--board", cmd_board),
        ("--web", cmd_web),
    ]
    ran = False
    for flag, fn in modes:
        if flag in argv:
            fn(dry)
            ran = True
    if not ran:
        print("usage: valheim-medals.py --seed|--check-records|--daily|--weekly|--board|--web [--dry-run]"
              " (--daily and --board may combine)", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
