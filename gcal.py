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
import os, re, json, datetime, urllib.request

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
        # Read-only fallback: the calendar's private iCal address. Needs no Google
        # Cloud project, no service account and no key file, which matters because
        # many Workspace organisations block service account keys outright.
        "ics_url": os.environ.get("GCAL_ICS_URL",""),
        # Write route that works under the org's locked-down Workspace: a Google
        # Apps Script web app (scripts/gcal-webhook.gs) deployed by someone with
        # write access to the calendar. It runs as THEM, so no service account or
        # OAuth client is ever needed. The key must match the one in the script.
        "webhook_url": os.environ.get("GCAL_WEBHOOK_URL",""),
        "webhook_key": os.environ.get("GCAL_WEBHOOK_KEY",""),
        # Last-resort read route: a busy-times snapshot file shipped with the app
        # (web/gibby-busy.ics, exported from the Gibby calendar). Lets slots appear
        # before any live feed is reachable; a configured GCAL_ICS_URL always wins.
        "ics_file": os.environ.get("GCAL_ICS_FILE",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "gibby-busy.ics")),
        # All tunable from the environment, because the defaults open every day
        # from 8am to 10pm, which is far more slots than a Saturday programme needs.
        "open_hour": int(os.environ.get("GCAL_OPEN_HOUR", "8") or 8),
        "close_hour": int(os.environ.get("GCAL_CLOSE_HOUR", "22") or 22),
        "slot_minutes": int(os.environ.get("GCAL_SLOT_MINUTES", "30") or 30),
        "horizon_days": int(os.environ.get("GCAL_HORIZON_DAYS", "56") or 56),
        # Mon=0 .. Sun=6. "5" is Saturdays only. Empty means every day.
        "days": [int(x) for x in os.environ.get("GCAL_DAYS","").replace(" ","").split(",") if x.isdigit()],
        # The booking season. Slots are generated for this whole window rather than
        # a rolling few weeks from today, so a December-to-May season is visible
        # the day booking opens. Overridable per season.
        "season_start": os.environ.get("GCAL_SEASON_START", os.environ.get("SEASON_START", "2026-09-01")),
        "season_end": os.environ.get("GCAL_SEASON_END", "2027-05-31"),
        # Which months hold classes: spring (Dec-May) plus fall (Oct-Nov). Summer
        # days inside the window are skipped so June-September never become slots.
        "season_months": {int(x) for x in os.environ.get("GCAL_SEASON_MONTHS",
                          "9,10,11,12,1,2,3,4,5").split(",") if x.strip().isdigit()},
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.isfile(path):
        try:
            cfg.update({k: v for k, v in json.load(open(path)).items() if k in cfg and v not in (None, "")})
        except Exception as e:
            print("[gcal:config] could not read config.json:", e)
    return cfg

def configured(cfg):
    """Can we read the calendar at all, by any route?"""
    return (bool(cfg.get("ics_url")) or os.path.isfile(cfg.get("ics_file") or "")
            or bool(cfg["calendar_id"] and cfg["service_account_json"]))

def can_write(cfg):
    """Writing classes back needs the API or the Apps Script webhook. The iCal
    feed (and the bundled snapshot) are read-only."""
    return bool(cfg.get("webhook_url")) or bool(cfg["calendar_id"] and cfg["service_account_json"])

def _service(cfg):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    info = json.loads(cfg["service_account_json"])
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"])
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def _season_window(cfg, today):
    """(first_day, last_day) to generate slots for. The season bounds when set,
    never reaching into the past."""
    try: a = datetime.date.fromisoformat(cfg.get("season_start") or "")
    except ValueError: a = today
    try: b = datetime.date.fromisoformat(cfg.get("season_end") or "")
    except ValueError: b = today + datetime.timedelta(days=cfg["horizon_days"])
    return max(a, today), b

def _fmt_date(d):  return f"{DOW[d.weekday()]}, {MONTHS[d.month-1]} {d.day}"
def _fmt_time(dt): return dt.strftime("%-I:%M %p") if os.name != "nt" else dt.strftime("%I:%M %p").lstrip("0")

def sync_slots(cfg):
    """Return open 30-min slots [{date,start,end}] across the horizon, based on the
    calendar's busy times. Roomless (single shared space). None if it can't read.
    Uses the API when a service account is available, otherwise the iCal feed."""
    if not configured(cfg): return None
    # Reading open times: the iCal feed (or bundled snapshot) is the route unless
    # a real service account exists. The write-only webhook must NOT divert this,
    # even though it makes can_write() true.
    if ((cfg.get("ics_url") or os.path.isfile(cfg.get("ics_file") or ""))
            and not (cfg["calendar_id"] and cfg["service_account_json"])):
        return sync_slots_ical(cfg)
    try:
        svc = _service(cfg)
        now = datetime.datetime.now(TZ)
        w0, w1 = _season_window(cfg, now.date())
        tmin = datetime.datetime(w0.year, w0.month, w0.day, tzinfo=TZ)
        tmax = datetime.datetime(w1.year, w1.month, w1.day, tzinfo=TZ) + datetime.timedelta(days=1)
        fb = svc.freebusy().query(body={
            "timeMin": tmin.isoformat(), "timeMax": tmax.isoformat(),
            "items": [{"id": cfg["calendar_id"]}]}).execute()
        raw = fb["calendars"][cfg["calendar_id"]]["busy"]
        busy = [(datetime.datetime.fromisoformat(b["start"]), datetime.datetime.fromisoformat(b["end"])) for b in raw]
    except Exception as e:
        print("[gcal] read error:", e); return None
    out, step = [], datetime.timedelta(minutes=cfg["slot_minutes"])
    for day in range((w1 - w0).days + 1):
        d = w0 + datetime.timedelta(days=day)
        if cfg.get("season_months") and d.month not in cfg["season_months"]: continue
        if cfg["days"] and d.weekday() not in cfg["days"]: continue
        t = datetime.datetime(d.year, d.month, d.day, cfg["open_hour"], 0, tzinfo=TZ)
        end_day = datetime.datetime(d.year, d.month, d.day, cfg["close_hour"], 0, tzinfo=TZ)
        while t + step <= end_day:
            s, e = t, t + step
            if s > now and not any(bs < e and s < be for bs, be in busy):
                out.append({"date": _fmt_date(s), "start": _fmt_time(s), "end": _fmt_time(e)})
            t = e
    return out

# --------------------------------------------------------------- iCal reading ----
def _ics_dt(value, params):
    """An ICS date-time -> naive local datetime. Handles 20270116T100000Z (UTC),
    20270116T100000 (floating or TZID) and 20270116 (all day)."""
    v = value.strip()
    try:
        if v.endswith("Z"):
            dt = datetime.datetime.strptime(v, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(TZ).replace(tzinfo=None)
        if "T" in v:
            return datetime.datetime.strptime(v, "%Y%m%dT%H%M%S")
        d = datetime.datetime.strptime(v, "%Y%m%d")
        return d
    except ValueError:
        return None

def _ics_events(text, horizon_start, horizon_end):
    """Busy intervals from an iCal feed. Unfolds wrapped lines, skips cancelled and
    transparent (free) events, and expands simple weekly/daily repeats, which is
    what a room calendar actually uses."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n ", "").replace("\n\t", "")
    busy = []
    for block in text.split("BEGIN:VEVENT")[1:]:
        block = block.split("END:VEVENT")[0]
        fields = {}
        for line in block.split("\n"):
            if ":" not in line: continue
            key, val = line.split(":", 1)
            name = key.split(";")[0].upper()
            params = {}
            for part in key.split(";")[1:]:
                if "=" in part:
                    k, v = part.split("=", 1); params[k.upper()] = v
            fields.setdefault(name, (val, params))
        if fields.get("STATUS", ("",{}))[0].upper() == "CANCELLED": continue
        if fields.get("TRANSP", ("",{}))[0].upper() == "TRANSPARENT": continue   # marked free
        if "DTSTART" not in fields: continue
        start = _ics_dt(*fields["DTSTART"])
        end = _ics_dt(*fields["DTEND"]) if "DTEND" in fields else None
        if not start: continue
        if not end: end = start + datetime.timedelta(hours=1)
        rule = fields.get("RRULE", ("",{}))[0]
        occurrences = [(start, end)]
        if rule:
            parts = dict(p.split("=", 1) for p in rule.split(";") if "=" in p)
            freq = parts.get("FREQ", "").upper()
            step = {"DAILY": 1, "WEEKLY": 7}.get(freq)
            if step:
                interval = int(parts.get("INTERVAL", 1) or 1) * step
                until = None
                if parts.get("UNTIL"):
                    until = _ics_dt(parts["UNTIL"], {})
                count = int(parts["COUNT"]) if parts.get("COUNT", "").isdigit() else None
                length = end - start
                cur, n = start, 1
                while cur <= horizon_end and n < 400:
                    cur = cur + datetime.timedelta(days=interval); n += 1
                    if until and cur > until: break
                    if count and n > count: break
                    occurrences.append((cur, cur + length))
        for s0, e0 in occurrences:
            if e0 >= horizon_start and s0 <= horizon_end:
                busy.append((s0, e0))
    return busy

# What the last sync actually read: 'live' (the feed URL), 'snapshot' (the
# bundled file), or None (nothing yet). Surfaced on /api/version so a wrong
# feed URL is visible instead of silently riding the snapshot forever.
LAST_SOURCE = None

def sync_slots_ical(cfg):
    """Open slots from the calendar's private iCal address. Same shape as
    sync_slots, so the rest of the app cannot tell which route was used."""
    global LAST_SOURCE
    text = None
    if cfg.get("ics_url"):
        try:
            req = urllib.request.Request(cfg["ics_url"], headers={"User-Agent": "GibbyClassManager/1.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                text = r.read().decode("utf-8", "replace")
            if "BEGIN:VCALENDAR" in text:
                LAST_SOURCE = "live"
            else:
                print("[gcal] the feed URL answered but not with a calendar; falling back")
                text = None
        except Exception as e:
            print("[gcal] could not read the iCal feed:", e)
    if text is None and os.path.isfile(cfg.get("ics_file") or ""):
        # Bundled busy-times snapshot: also the safety net when the live feed is down.
        try:
            text = open(cfg["ics_file"], encoding="utf-8").read()
            LAST_SOURCE = "snapshot"
            print("[gcal] using bundled snapshot", os.path.basename(cfg["ics_file"]))
        except Exception as e:
            print("[gcal] could not read the snapshot file:", e)
    if text is None:
        return None
    if "BEGIN:VCALENDAR" not in text:
        print("[gcal] that URL did not return a calendar feed"); return None

    now = datetime.datetime.now(TZ).replace(tzinfo=None)
    w0, w1 = _season_window(cfg, now.date())
    start_day = datetime.datetime(w0.year, w0.month, w0.day)
    horizon_end = datetime.datetime(w1.year, w1.month, w1.day, 23, 59)
    busy = _ics_events(text, start_day, horizon_end)

    out, step = [], datetime.timedelta(minutes=cfg["slot_minutes"])
    for day in range((w1 - w0).days + 1):
        d = w0 + datetime.timedelta(days=day)
        if cfg.get("season_months") and d.month not in cfg["season_months"]: continue
        if cfg["days"] and d.weekday() not in cfg["days"]: continue
        t = datetime.datetime(d.year, d.month, d.day, cfg["open_hour"], 0)
        close = datetime.datetime(d.year, d.month, d.day, cfg["close_hour"], 0)
        while t + step <= close:
            s0, e0 = t, t + step
            if s0 > now and not any(bs < e0 and s0 < be for bs, be in busy):
                out.append({"date": _fmt_date(s0), "start": _fmt_time(s0), "end": _fmt_time(e0)})
            t = e0
    print(f"[gcal] iCal feed: {len(busy)} busy period(s), {len(out)} open slot(s)")
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
        sessions = [{"date": cls.get("slot_date"), "time": cls.get("class_time") or cls.get("slot_time")}]
    n = len(sessions)

    if not can_write(cfg):
        print(f"[gcal] read-only (iCal feed): cannot add {n} event(s) for "
              f"'{cls.get('title')}'. A webhook or service account is needed to write."); return None
    if not cfg["gcal_live"]:
        print(f"[gcal] dry-run: would add {n} event(s) for '{cls.get('title')}'"); return None
    try:
        # slot labels carry no year; infer it from the season, as the server does
        try:
            sd = datetime.date.fromisoformat(os.environ.get("SEASON_START", "2026-09-01"))
        except ValueError:
            sd = datetime.date(2026, 12, 1)
        events = []
        for i, s in enumerate(sessions, start=1):
            md = (s.get("date") or "").split(", ")[-1].split()
            mon, day = MONTHS.index(md[0]) + 1, int(md[1])
            year = sd.year if mon >= sd.month else sd.year + 1
            span = s.get("time") or f"{s.get('start','')} \u2013 {s.get('end','')}"
            parts = [p.strip() for p in re.split(r"\s*[\u2013\u2014-]\s*", (span or "").strip()) if p.strip()]
            def t(x):
                dt = datetime.datetime.strptime(x, "%I:%M %p")
                return datetime.datetime(year, mon, day, dt.hour, dt.minute)
            start = t(parts[0])
            end = t(parts[1]) if len(parts) > 1 and parts[1] else start + datetime.timedelta(minutes=30)
            title = f"{cls['title']} ({i} of {n})" if n > 1 else cls["title"]
            desc = (f"Instructor: {cls.get('instructor_name','')}\nRoom: {cls.get('room','')}\n"
                    f"{cls.get('age_label') or cls.get('age_range','')}\nTicket: ${cls.get('ticket_price','')}"
                    + (f"\nSession {i} of {n}" if n > 1 else "") + f"\n\n{cls.get('description','')}")
            # Timezone-aware ISO strings: unambiguous for both the Apps Script
            # webhook (new Date(...) honours the offset) and the Calendar API.
            events.append({"title": title, "description": desc,
                           "location": f"The Gibby, {cls.get('room','')}",
                           "start": start.replace(tzinfo=TZ).isoformat(),
                           "end": end.replace(tzinfo=TZ).isoformat()})

        if cfg.get("webhook_url"):
            payload = json.dumps({"key": cfg.get("webhook_key",""), "action": "create",
                                  "events": events}).encode()
            req = urllib.request.Request(cfg["webhook_url"], data=payload,
                headers={"Content-Type": "application/json", "User-Agent": "GibbyClassManager/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.loads(r.read().decode("utf-8", "replace"))
            if not res.get("ok"):
                print("[gcal] webhook refused the events:", res.get("error", res)); return None
            ids = [str(x) for x in (res.get("ids") or [])]
            print(f"[gcal] created {len(ids)} event(s) for '{cls['title']}' via webhook")
            return ",".join(i for i in ids if i) or None

        svc = _service(cfg)
        ids = []
        for ev_body in events:
            ev = svc.events().insert(calendarId=cfg["calendar_id"], body={
                "summary": ev_body["title"],
                "description": ev_body["description"],
                "location": ev_body["location"],
                "start": {"dateTime": ev_body["start"]},
                "end":   {"dateTime": ev_body["end"]},
            }).execute()
            ids.append(ev.get("id"))
        print(f"[gcal] created {len(ids)} event(s) for '{cls['title']}'")
        return ",".join(i for i in ids if i) or None
    except Exception as e:
        print("[gcal] write error:", e); return None
