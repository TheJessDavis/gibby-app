"""
Real external-posting layer for Gibby Class Manager.

Config-driven and DRY-RUN-SAFE:
  * Fill in credentials via config.json (see config.example.json) or env vars.
  * Nothing is actually sent until you set  "live": true  in config.json.
  * If a platform's keys are missing, that platform is skipped (logged), and the
    rest still run. So the app is safe to run with no accounts at all.

When you have real credentials, this is where posting goes live: set the keys,
flip "live": true, and approvals will create the real Eventbrite event, Facebook
Page post, Wix event, and Canva graphic. (Descene has no API; see below.)

NOTE: endpoint paths/payloads follow each platform's current API shape but should
be verified against live docs before go-live. Real Eventbrite posting also needs
proper ISO start/end datetimes on the class (see _iso_times, a best-effort parser).
"""
import json, os, urllib.request, urllib.parse, urllib.error, datetime

HERE = os.path.dirname(os.path.abspath(__file__))

def load_config():
    cfg = {
        "live": os.environ.get("GIBBY_LIVE", "").lower() in ("1", "true", "yes"),
        "timezone": "America/New_York", "year": 2027,
        "eventbrite_token":  os.environ.get("EVENTBRITE_TOKEN", ""),
        "eventbrite_org_id": os.environ.get("EVENTBRITE_ORG_ID", ""),
        "fb_page_id":        os.environ.get("FB_PAGE_ID", ""),
        "fb_page_token":     os.environ.get("FB_PAGE_TOKEN", ""),
        "wix_api_key":       os.environ.get("WIX_API_KEY", ""),
        "wix_site_id":       os.environ.get("WIX_SITE_ID", ""),
        "canva_token":       os.environ.get("CANVA_TOKEN", ""),
        "canva_template_id": os.environ.get("CANVA_TEMPLATE_ID", ""),
    }
    path = os.path.join(HERE, "config.json")
    if os.path.isfile(path):
        try:
            cfg.update({k: v for k, v in json.load(open(path)).items() if v not in (None, "")})
        except Exception as e:
            print("[config] could not read config.json:", e)
    return cfg

def configured(cfg):
    return {
        "eventbrite": bool(cfg["eventbrite_token"] and cfg["eventbrite_org_id"]),
        "facebook":   bool(cfg["fb_page_id"] and cfg["fb_page_token"]),
        "wix":        bool(cfg["wix_api_key"] and cfg["wix_site_id"]),
        "canva":      bool(cfg["canva_token"] and cfg["canva_template_id"]),
        "descene":    False,  # no public API
    }

# ------------------------------------------------------------------ helpers ----
def _req(url, method="POST", token=None, json_body=None, form=None, headers=None):
    hdr = dict(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode(); hdr["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode(); hdr["Content-Type"] = "application/x-www-form-urlencoded"
    if token:
        hdr["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=hdr)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")

def _ok(id=None, status=""):   return {"ok": True,  "id": id, "status": status, "error": ""}
def _no(status, error=""):     return {"ok": False, "id": None, "status": status, "error": error}

_MON = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1)}

def _iso_times(cls, cfg):
    """Best-effort ISO start/end from slot_date ('Sat, Jan 17') + slot_time
    ('10:00 AM – 10:30 AM'). Returns (start, end) or (None, None). Times are
    emitted with a 'Z'; a production build should convert ET->UTC properly."""
    if cls.get("start_utc") and cls.get("end_utc"):
        return cls["start_utc"], cls["end_utc"]
    try:
        date = (cls.get("slot_date") or "").split(", ")[-1].strip().split()  # ['Jan','17']
        mon, day = _MON[date[0]], int(date[1])
        parts = [p.strip() for p in (cls.get("slot_time") or "").replace("—", "–").split("–")]
        def t(s):
            dt = datetime.datetime.strptime(s, "%I:%M %p")
            return datetime.datetime(cfg["year"], mon, day, dt.hour, dt.minute)
        start = t(parts[0])
        end = t(parts[1]) if len(parts) > 1 else start + datetime.timedelta(minutes=30)
        f = "%Y-%m-%dT%H:%M:%SZ"
        # convert US-Eastern local -> UTC so Eventbrite shows the right time
        su = start - datetime.timedelta(hours=_eastern_offset(start))
        eu = end - datetime.timedelta(hours=_eastern_offset(end))
        return su.strftime(f), eu.strftime(f)
    except Exception:
        return None, None

def _eastern_offset(dt):
    """US Eastern offset (hours, negative) for a naive local datetime. EDT (-4)
    from the 2nd Sunday of March 02:00 to the 1st Sunday of November 02:00, else EST (-5)."""
    y = dt.year
    second_sun_mar = [d for d in range(1, 15) if datetime.date(y, 3, d).weekday() == 6][1]
    first_sun_nov  = [d for d in range(1, 8)  if datetime.date(y, 11, d).weekday() == 6][0]
    start = datetime.datetime(y, 3, second_sun_mar, 2)
    end   = datetime.datetime(y, 11, first_sun_nov, 2)
    return -4 if start <= dt < end else -5

def eventbrite_orgs(cfg):
    """Read-only: verify the Eventbrite token and return the organization(s) so we
    can grab the org id. Creates nothing."""
    if not cfg["eventbrite_token"]:
        return {"ok": False, "error": "No EVENTBRITE_TOKEN set yet."}
    try:
        res = _req("https://www.eventbriteapi.com/v3/users/me/organizations/", method="GET",
                   token=cfg["eventbrite_token"])
        return {"ok": True, "organizations": [{"id": o.get("id"), "name": o.get("name")}
                                              for o in res.get("organizations", [])]}
    except urllib.error.HTTPError as e:
        try: body = e.read().decode()[:200]
        except Exception: body = ""
        return {"ok": False, "error": f"HTTP {e.code} {body}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ----------------------------------------------------------------- platforms ----
def post_canva(cls, cfg):
    if not (cfg["canva_token"] and cfg["canva_template_id"]):
        return _no("skipped: no Canva config")
    if not cfg["live"]:
        return _ok("canva-dryrun", "dry-run (would autofill + export the poster)")
    job = _req("https://api.canva.com/rest/v1/autofills", token=cfg["canva_token"], json_body={
        "brand_template_id": cfg["canva_template_id"],
        "data": {
            "headline": {"type": "text", "text": cls.get("headline") or cls.get("title", "")},
            "subtitle": {"type": "text", "text": cls.get("subtitle", "")},
            "date":     {"type": "text", "text": f"{cls.get('slot_date','')} {cls.get('slot_time','')}".strip()},
            "ages":     {"type": "text", "text": cls.get("age_range", "")},
        }})
    # A production build polls the job, then calls /v1/exports to get the PNG URL.
    return _ok(((job.get("job") or {}).get("id")) or "submitted", "autofill submitted")

def post_eventbrite(cls, cfg):
    if not (cfg["eventbrite_token"] and cfg["eventbrite_org_id"]):
        return _no("skipped: no Eventbrite config")
    start, end = _iso_times(cls, cfg)
    if not (start and end):
        return _no("needs valid start/end datetime on the class")
    if not cfg["live"]:
        return _ok("eb-dryrun", f"dry-run (would create + publish event {start})")
    ev = _req(f"https://www.eventbriteapi.com/v3/organizations/{cfg['eventbrite_org_id']}/events/",
        token=cfg["eventbrite_token"], json_body={"event": {
            "name": {"html": cls["title"]},
            "description": {"html": cls.get("description", "")},
            "start": {"timezone": cfg["timezone"], "utc": start},
            "end":   {"timezone": cfg["timezone"], "utc": end},
            "currency": "USD", "capacity": cls.get("max_p")}})
    eid = ev.get("id")
    _req(f"https://www.eventbriteapi.com/v3/events/{eid}/ticket_classes/", token=cfg["eventbrite_token"],
        json_body={"ticket_class": {"name": "Admission", "quantity_total": cls.get("max_p"),
            "cost": f"USD,{int(round((cls.get('ticket_price') or 0) * 100))}"}})
    _req(f"https://www.eventbriteapi.com/v3/events/{eid}/publish/", token=cfg["eventbrite_token"], json_body={})
    return _ok(eid, "event published")

def post_facebook(cls, cfg, link=None):
    # Facebook removed Event creation from the Graph API, so this posts to the Page.
    if not (cfg["fb_page_id"] and cfg["fb_page_token"]):
        return _no("skipped: no Facebook config")
    if not cfg["live"]:
        return _ok("fb-dryrun", "dry-run (Page post, not an Event)")
    msg = f"{cls['title']} — {cls.get('slot_date','')} {cls.get('slot_time','')}\n\n{cls.get('description','')}"
    if link: msg += f"\n\nSign up: {link}"
    res = _req(f"https://graph.facebook.com/v20.0/{cfg['fb_page_id']}/feed",
        form={"message": msg, "access_token": cfg["fb_page_token"]})
    return _ok(res.get("id"), "posted to Page")

def post_wix(cls, cfg):
    if not (cfg["wix_api_key"] and cfg["wix_site_id"]):
        return _no("skipped: no Wix config")
    if not cfg["live"]:
        return _ok("wix-dryrun", "dry-run (would create Wix event)")
    res = _req("https://www.wixapis.com/events/v1/events",
        headers={"Authorization": cfg["wix_api_key"], "wix-site-id": cfg["wix_site_id"]},
        json_body={"event": {"title": cls["title"], "description": cls.get("description", "")}})
    return _ok((res.get("event") or {}).get("id"), "event created")

def post_descene(cls, cfg):
    # No public API. Real posting requires headless-browser automation (Playwright),
    # which is not available in the standard library. Flagged as manual for now.
    return _no("manual: Descene has no API (needs browser automation, not in stdlib)")

# ------------------------------------------------------------------- publish ----
def _safe(fn, *a):
    try:
        return fn(*a)
    except urllib.error.HTTPError as e:
        try: body = e.read().decode()[:300]
        except Exception: body = ""
        return _no("http error", f"{e.code} {body}")
    except Exception as e:
        return _no("error", str(e))

def publish(cls, cfg=None):
    """Run every platform. Returns an external_ids dict for storage, including a
    per-platform _results breakdown. Never raises; failures are captured."""
    cfg = cfg or load_config()
    results = {
        "canva":      _safe(post_canva, cls, cfg),
        "eventbrite": _safe(post_eventbrite, cls, cfg),
        "facebook":   _safe(post_facebook, cls, cfg, None),
        "wix":        _safe(post_wix, cls, cfg),
        "descene":    _safe(post_descene, cls, cfg),
    }
    ext = {}
    for k, v in results.items():
        if v.get("id"):
            ext[k + "_id"] = v["id"]
        flag = "LIVE" if cfg["live"] else "dry-run"
        print(f"[publish:{k}] {flag} ok={v['ok']} {v.get('status','')}"
              + (f" id={v['id']}" if v.get('id') else "")
              + (f" ERROR={v['error']}" if v.get('error') else ""))
    ext["_results"] = results
    return ext
