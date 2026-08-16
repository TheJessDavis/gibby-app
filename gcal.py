"""
Google Calendar two-way sync for Gibby Class Manager.

Reads free/busy from ONE shared Gibby calendar and turns the open time into
available 30-minute slots; writes a full event back to the calendar when a class
is approved. Uses a service account (a robot Google account you share the
calendar with).

Config (env vars, or config.json):
  GOOGLE_SERVICE_ACCOUNT_JSON  the entire service-account JSON key (one line)
  GCAL_CALENDAR_ID             the calendar's ID (looks like an email)
  GCAL_LIVE                    "true" to actually read/write; otherwise dry-run

Does nothing until configured. The google libraries are imported lazily so the
rest of the app runs even if they aren't installed.
"""
import os, json, datetime

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/New_York")
except Exception:
    TZ = datetime.timezone(datetime.timedelta(hours=-5))  # fallback EST

MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

def load_gcal_config():
    cfg = {
        "gcal_live": os.environ.get("GCAL_LIVE","").lower() in ("1","true","yes"),
        "calendar_id": os.environ.get("GCAL_CALENDAR_ID",""),
        "service_account_json": os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON",""),
        "open_hour": 8, "close_hour": 22,   # 8 AM - 10 PM
        "slot_minutes": 30,
        "horizon_days": 56,                 # look 8 weeks ahead
        "days": [],                         # [] = every day; e.g. [5] = Saturdays only (Mon=0..Sun=6)
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.isfile(path):
        try:
            cfg.update({k: v for k, v in json.load(open(path)).items() if k in cfg and v not in (None, "")})
        except Exception as e:
            print("[gcal:config] could not read config.json:", e)
    return cfg

def configured(cfg):
    return bool(cfg["calendar_id"] and cfg["service_account_json"])

def _service(cfg):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(cfg["service_account_json"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def _fmt_date(d):  return f"{DOW[d.weekday()]}, {MONTHS[d.month-1]} {d.day}"
def _fmt_time(dt): return dt.strftime("%-I:%M %p") if os.name != "nt" else dt.strftime("%I:%M %p").lstrip("0")

def sync_slots(cfg):
    """Return open 30-min slots [{date,start,end}] across the horizon, based on the
    calendar's busy times. Roomless (single shared space). None if it can't read."""
    if not configured(cfg): return None
    try:
        svc = _service(cfg)
        now = datetime.datetime.now(TZ)
        tmin = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tmax = tmin + datetime.timedelta(days=cfg["horizon_days"])
        fb = svc.freebusy().query(body={
            "timeMin": tmin.isoformat(), "timeMax": tmax.isoformat(),
            "items": [{"id": cfg["calendar_id"]}]}).execute()
        raw = fb["calendars"][cfg["calendar_id"]]["busy"]
        busy = [(datetime.datetime.fromisoformat(b["start"]), datetime.datetime.fromisoformat(b["end"])) for b in raw]
    except Exception as e:
        print("[gcal] read error:", e); return None
    out, step = [], datetime.timedelta(minutes=cfg["slot_minutes"])
    for day in range(cfg["horizon_days"]):
        d = (tmin + datetime.timedelta(days=day)).date()
        if cfg["days"] and d.weekday() not in cfg["days"]: continue
        t = datetime.datetime(d.year, d.month, d.day, cfg["open_hour"], 0, tzinfo=TZ)
        end_day = datetime.datetime(d.year, d.month, d.day, cfg["close_hour"], 0, tzinfo=TZ)
        while t + step <= end_day:
            s, e = t, t + step
            if s > now and not any(bs < e and s < be for bs, be in busy):
                out.append({"date": _fmt_date(s), "start": _fmt_time(s), "end": _fmt_time(e)})
            t = e
    return out

def create_event(cls, cfg):
    """Write a class onto the calendar with full details. A series gets ONE calendar
    event per session, so every week blocks the room. Returns the event id (comma
    separated for a series), or None (dry-run / not configured / error)."""
    # A series books every session; a one-day class books just the one.
    try:
        sessions = json.loads(cls.get("session_dates") or "[]")
    except Exception:
        sessions = []
    if not sessions:
        sessions = [{"date": cls.get("slot_date"), "time": cls.get("slot_time")}]
    n = len(sessions)

    if not (configured(cfg) and cfg["gcal_live"]):
        print(f"[gcal] dry-run: would add {n} event(s) for '{cls.get('title')}'"); return None
    try:
        svc = _service(cfg)
        year = datetime.datetime.now(TZ).year
        ids = []
        for i, s in enumerate(sessions, start=1):
            md = (s.get("date") or "").split(", ")[-1].split()
            mon, day = MONTHS.index(md[0]) + 1, int(md[1])
            span = s.get("time") or f"{s.get('start','')} – {s.get('end','')}"
            parts = [p.strip() for p in span.replace("—", "–").split("–")]
            def t(x):
                dt = datetime.datetime.strptime(x, "%I:%M %p")
                return datetime.datetime(year, mon, day, dt.hour, dt.minute)
            start = t(parts[0])
            end = t(parts[1]) if len(parts) > 1 and parts[1] else start + datetime.timedelta(minutes=30)
            title = f"{cls['title']} ({i} of {n})" if n > 1 else cls["title"]
            desc = (f"Instructor: {cls.get('instructor_name','')}\nRoom: {cls.get('room','')}\n"
                    f"{cls.get('age_label') or cls.get('age_range','')}\nTicket: ${cls.get('ticket_price','')}"
                    + (f"\nSession {i} of {n}" if n > 1 else "") + f"\n\n{cls.get('description','')}")
            ev = svc.events().insert(calendarId=cfg["calendar_id"], body={
                "summary": title,
                "description": desc,
                "location": f"The Gibby — {cls.get('room','')}",
                "start": {"dateTime": start.isoformat(), "timeZone": "America/New_York"},
                "end":   {"dateTime": end.isoformat(),   "timeZone": "America/New_York"},
            }).execute()
            ids.append(ev.get("id"))
        print(f"[gcal] created {len(ids)} event(s) for '{cls['title']}'")
        return ",".join(i for i in ids if i) or None
    except Exception as e:
        print("[gcal] write error:", e); return None
