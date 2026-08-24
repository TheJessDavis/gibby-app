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
import json, os, re, urllib.request, urllib.parse, urllib.error, datetime, time, struct, uuid

HERE = os.path.dirname(os.path.abspath(__file__))

def load_config():
    cfg = {
        "live": os.environ.get("GIBBY_LIVE", "").lower() in ("1", "true", "yes"),
        "timezone": "America/New_York", "year": 2027,
        "eventbrite_token":  os.environ.get("EVENTBRITE_TOKEN", ""),
        "eventbrite_org_id": os.environ.get("EVENTBRITE_ORG_ID", ""),
        # Organizer profile the event posts under (The Gibby, not The Everett).
        # NOT a secret - this id appears in public Eventbrite URLs - so it is left
        # as a default deliberately: clearing it would silently post events under
        # the wrong organizer. Override with EVENTBRITE_ORGANIZER_ID if it changes.
        "eventbrite_organizer_id": os.environ.get("EVENTBRITE_ORGANIZER_ID", "76506239933"),
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

def _ok(id=None, status="", **extra):  return {"ok": True,  "id": id, "status": status, "error": "", **extra}
def _no(status, error=""):     return {"ok": False, "id": None, "status": status, "error": error}

def _get_bytes(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()

def _png_size(data):
    """(width, height) from a PNG's IHDR, or None if not a PNG we can read."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    return None

def _multipart_post(url, fields, file_field, filename, filedata, timeout=120):
    """POST multipart/form-data (used to push the graphic bytes to Eventbrite's
    upload endpoint). fields = ordinary form fields, then the file part last."""
    boundary = "----gibby" + uuid.uuid4().hex
    nl = b"\r\n"
    body = bytearray()
    for k, v in (fields or {}).items():
        body += b"--" + boundary.encode() + nl
        body += f'Content-Disposition: form-data; name="{k}"'.encode() + nl + nl
        body += str(v).encode() + nl
    body += b"--" + boundary.encode() + nl
    body += f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode() + nl
    body += b"Content-Type: application/octet-stream" + nl + nl
    body += filedata + nl
    body += b"--" + boundary.encode() + b"--" + nl
    req = urllib.request.Request(url, data=bytes(body), method="POST",
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def _poll(url, token, pick, tries=40, delay=2):
    """Poll a Canva async job until it succeeds; `pick(job)` extracts the result.
    Raises on failure or timeout."""
    for _ in range(tries):
        r = _req(url, method="GET", token=token)
        job = r.get("job") or r
        st = job.get("status")
        if st in ("success", "complete", "completed"): return pick(job)
        if st in ("failed", "error"): raise RuntimeError("Canva job failed: " + json.dumps(job.get("error", {})))
        time.sleep(delay)
    raise TimeoutError("Canva job timed out")

_MON = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1)}

def _season_year(month):
    """Slot labels carry no year; the season decides it. Dec 2026 season start
    means Dec is 2026 and Jan-May are 2027."""
    try:
        d = datetime.date.fromisoformat(os.environ.get("SEASON_START", "2026-09-01"))
        return d.year if month >= d.month else d.year + 1
    except ValueError:
        return 2027

def _iso_times(cls, cfg):
    """Best-effort ISO start/end from slot_date ('Sat, Jan 17') + slot_time
    ('10:00 AM – 10:30 AM'). Returns (start, end) or (None, None). Times are
    emitted with a 'Z'; a production build should convert ET->UTC properly."""
    if cls.get("start_utc") and cls.get("end_utc"):
        return cls["start_utc"], cls["end_utc"]
    try:
        date = (cls.get("slot_date") or "").split(", ")[-1].strip().split()  # ['Jan','17']
        mon, day = _MON[date[0]], int(date[1])
        # accept en dash, em dash or a plain hyphen: a hand-edited time should still publish
        # Students are told when the CLASS runs; the booked window around it is
        # the instructor's setup and cleanup time.
        span = cls.get("class_time") or cls.get("slot_time") or ""
        parts = [p.strip() for p in re.split(r"\s*[\u2013\u2014-]\s*", span.strip()) if p.strip()]
        def t(s):
            dt = datetime.datetime.strptime(s, "%I:%M %p")
            return datetime.datetime(_season_year(mon), mon, day, dt.hour, dt.minute)
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
        orgs = [{"id": o.get("id"), "name": o.get("name")} for o in res.get("organizations", [])]
        # Echo what the server is actually configured with, so a wrong or
        # mistyped EVENTBRITE_ORG_ID on the host is visible from the app.
        configured = (cfg.get("eventbrite_org_id") or "").strip()
        return {"ok": True, "organizations": orgs,
                "configured_org_id": cfg.get("eventbrite_org_id") or "",
                "org_id_matches": any(o["id"] == configured for o in orgs)}
    except urllib.error.HTTPError as e:
        try: body = e.read().decode()[:200]
        except Exception: body = ""
        return {"ok": False, "error": f"HTTP {e.code} {body}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ----------------------------------------------------------------- platforms ----
def render_canva(cls, cfg):
    """Make the class graphic from the Canva brand template and return a PNG URL.
    Full pipeline: autofill the template -> poll -> export as PNG -> poll -> URL.
    Returns _ok(design_id, ..., image_url=<png>) so Eventbrite can attach it.
    Dry-run and skip-safe."""
    if not (cfg["canva_token"] and cfg["canva_template_id"]):
        return _no("skipped: no Canva config")
    if not cfg["live"]:
        return _ok("canva-dryrun", "dry-run (would autofill template + export PNG)", image_url=None)
    tok = cfg["canva_token"]
    # 1) autofill the brand template with this class's data
    af = _req("https://api.canva.com/rest/v1/autofills", token=tok, json_body={
        "brand_template_id": cfg["canva_template_id"],
        "data": {
            "headline": {"type": "text", "text": cls.get("headline") or cls.get("title", "")},
            "subtitle": {"type": "text", "text": cls.get("subtitle", "")},
            "date":     {"type": "text", "text": f"{cls.get('slot_date','')} {cls.get('slot_time','')}".strip()},
            "ages":     {"type": "text", "text": cls.get("age_label") or cls.get("age_range", "")},
        }})
    af_id = (af.get("job") or {}).get("id")
    design_id = _poll(f"https://api.canva.com/rest/v1/autofills/{af_id}", tok,
                      lambda job: (((job.get("result") or {}).get("design") or {}).get("id")))
    if not design_id:
        return _no("autofill produced no design")
    # 2) export the finished design as a PNG
    ex = _req("https://api.canva.com/rest/v1/exports", token=tok,
              json_body={"design_id": design_id, "format": {"type": "png"}})
    ex_id = (ex.get("job") or {}).get("id")
    urls = _poll(f"https://api.canva.com/rest/v1/exports/{ex_id}", tok,
                 lambda job: job.get("urls") or [])
    if not urls:
        return _no("export produced no image")
    return _ok(design_id, "graphic exported", image_url=urls[0])

def _eventbrite_logo_id(image_url, cfg):
    """Upload a graphic to Eventbrite and return a media/logo id to set on an event.
    Three steps: ask for upload instructions, push the bytes to their storage,
    then confirm. Best-effort; the caller swallows failures."""
    tok = cfg["eventbrite_token"]
    img = _get_bytes(image_url)
    instr = _req("https://www.eventbriteapi.com/v3/media/upload/?type=image-event-logo",
                 method="GET", token=tok)
    _multipart_post(instr["upload_url"], instr.get("upload_data", {}),
                    instr["file_parameter_name"], "graphic.png", img)
    size = _png_size(img)
    body = {"upload_token": instr["upload_token"]}
    if size:
        body["crop_mask"] = {"top_left": {"x": 0, "y": 0}, "width": size[0], "height": size[1]}
    media = _req("https://www.eventbriteapi.com/v3/media/upload/", method="POST",
                 token=tok, json_body=body)
    return media.get("id")

def _event_description(cls):
    """The full Eventbrite description for a class: series dates, the ages phrase,
    a video-preview link when the video is app-hosted, then the instructor's FAQ.
    One builder shared by create and update so an edit can never drift."""
    desc = cls.get("description", "")
    try:
        sessions = json.loads(cls.get("session_dates") or "[]")
    except Exception:
        sessions = []
    if cls.get("is_series") and sessions:
        lines = "".join(f"<li>{s['date']} · {s['start']} – {s['end']}</li>" for s in sessions)
        desc = (f"<p><b>A {len(sessions)}-week course.</b> One ticket covers all "
                f"{len(sessions)} sessions:</p><ul>{lines}</ul>") + desc
    ages = cls.get("age_label") or cls.get("age_range") or ""
    if ages:
        desc = f"<p><b>{ages}</b></p>" + desc
    vurl0 = (cls.get("video") or "").strip()
    if vurl0 and "/media/" in vurl0:
        desc = f'<p>\U0001F3AC <a href="{vurl0}">Watch a video preview of this class</a></p>' + desc
    try:
        faq = json.loads(cls.get("faq") or "[]")
    except Exception:
        faq = []
    if faq:
        import html as _html
        rows = "".join(f"<p><b>{_html.escape(x.get('q',''))}</b><br>{_html.escape(x.get('a',''))}</p>"
                       for x in faq if x.get("q") and x.get("a"))
        if rows:
            desc = desc + f"<h3>Good to know</h3>{rows}"
    return desc

def update_eventbrite_details(cls, cfg):
    """Push a published class's CURRENT details onto its existing Eventbrite event:
    title, description, capacity, ticket price and sales cutoff. Same listing,
    same URL, nothing republished or duplicated."""
    if not (cfg["eventbrite_token"] and cfg["eventbrite_org_id"]):
        return "skipped: no Eventbrite config"
    try:
        ext = json.loads(cls.get("external_ids") or "{}")
    except Exception:
        ext = {}
    eid = ext.get("eventbrite_id")
    if not eid:
        return "skipped: never published to Eventbrite"
    if not cfg["live"]:
        return f"dry-run (would update event {eid})"
    _req(f"https://www.eventbriteapi.com/v3/events/{eid}/", token=cfg["eventbrite_token"],
         json_body={"event": {"name": {"html": cls["title"]},
                              "description": {"html": _event_description(cls)},
                              "capacity": cls.get("max_p")}})
    try:
        tc_body = {"cost": f"USD,{int(round((cls.get('ticket_price') or 0) * 100))}",
                   "quantity_total": cls.get("max_p")}
        close_days = int(cls.get("close_days") or 0)
        start, _end = _iso_times(cls, cfg)
        if close_days > 0 and start:
            tc_body["sales_end"] = (datetime.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
                                    - datetime.timedelta(days=close_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        tcs = _req(f"https://www.eventbriteapi.com/v3/events/{eid}/ticket_classes/",
                   method="GET", token=cfg["eventbrite_token"])
        for tc in (tcs.get("ticket_classes") or [])[:1]:
            _req(f"https://www.eventbriteapi.com/v3/events/{eid}/ticket_classes/{tc['id']}/",
                 token=cfg["eventbrite_token"], json_body={"ticket_class": tc_body})
    except Exception as e:
        print("[eventbrite] ticket update failed (event details updated):", e)
    return "updated"

def post_eventbrite(cls, cfg, image_url=None):
    if not (cfg["eventbrite_token"] and cfg["eventbrite_org_id"]):
        return _no("skipped: no Eventbrite config")
    start, end = _iso_times(cls, cfg)
    if not (start and end):
        return _no("needs valid start/end datetime on the class")
    if not cfg["live"]:
        return _ok("eb-dryrun", f"dry-run (would create + publish event {start}"
                                + (", with Canva graphic" if image_url else "") + ")")
    logo_id = None
    if image_url:                      # attach the Canva graphic; never let this block the event
        try: logo_id = _eventbrite_logo_id(image_url, cfg)
        except Exception as e: print("[eventbrite] logo upload failed (posting without it):", e)
    desc = _event_description(cls)
    event = {
        "name": {"html": cls["title"]},
        "description": {"html": desc},
        "start": {"timezone": cfg["timezone"], "utc": start},
        "end":   {"timezone": cfg["timezone"], "utc": end},
        "currency": "USD", "capacity": cls.get("max_p")}
    if cfg.get("eventbrite_organizer_id"):     # omit rather than send an empty id
        event["organizer_id"] = cfg["eventbrite_organizer_id"]
    if logo_id: event["logo_id"] = logo_id
    ev = _req(f"https://www.eventbriteapi.com/v3/organizations/{cfg['eventbrite_org_id']}/events/",
        token=cfg["eventbrite_token"], json_body={"event": event})
    eid = ev.get("id")
    ticket = {"name": "Admission", "quantity_total": cls.get("max_p"),
              "cost": f"USD,{int(round((cls.get('ticket_price') or 0) * 100))}"}
    # Registration cutoff. sales_end is a writable field on the ticket class, so
    # Eventbrite itself stops selling - the instructor gets a headcount that cannot
    # then change under them.
    close_days = int(cls.get("close_days") or 0)
    if close_days > 0:
        try:
            cutoff = datetime.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ") - datetime.timedelta(days=close_days)
            ticket["sales_end"] = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception as e:
            print("[eventbrite] could not set the registration cutoff:", e)
    _req(f"https://www.eventbriteapi.com/v3/events/{eid}/ticket_classes/", token=cfg["eventbrite_token"],
        json_body={"ticket_class": ticket})
    _req(f"https://www.eventbriteapi.com/v3/events/{eid}/publish/", token=cfg["eventbrite_token"], json_body={})
    video_ok = False
    vurl = (cls.get("video") or "").strip()
    if vurl and any(h in vurl for h in ("youtube.com", "youtu.be", "vimeo.com")):
        # A hosted link can embed as a real player. Never let it block the event.
        try:
            _req(f"https://www.eventbriteapi.com/v3/events/{eid}/structured_content/1/",
                 token=cfg["eventbrite_token"], json_body={
                     "access_type": "public", "publish": True,
                     "modules": [{"type": "video", "data": {"video": {"url": vurl}}}]})
            video_ok = True
        except Exception as e:
            print("[eventbrite] video attach failed (event is live without it):", e)
    return _ok(eid, "event published"
               + (" with poster attached" if logo_id else "")
               + (" and video embedded" if video_ok else ""))

def update_eventbrite_times(cls, cfg):
    """Move an already-published Eventbrite event to the class's current date and
    times, in place: same listing, same URL, no duplicate. Also shifts the ticket
    sales cutoff to match. Returns a plain-English result string."""
    if not (cfg["eventbrite_token"] and cfg["eventbrite_org_id"]):
        return "skipped: no Eventbrite config"
    try:
        ext = json.loads(cls.get("external_ids") or "{}")
    except Exception:
        ext = {}
    eid = ext.get("eventbrite_id")
    if not eid:
        return "skipped: never published to Eventbrite"
    start, end = _iso_times(cls, cfg)
    if not (start and end):
        return "failed: could not work out the new start/end times"
    if not cfg["live"]:
        return f"dry-run (would move event {eid} to {start})"
    _req(f"https://www.eventbriteapi.com/v3/events/{eid}/", token=cfg["eventbrite_token"],
         json_body={"event": {"start": {"timezone": cfg["timezone"], "utc": start},
                              "end":   {"timezone": cfg["timezone"], "utc": end}}})
    close_days = int(cls.get("close_days") or 0)
    if close_days > 0:
        try:
            cutoff = (datetime.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
                      - datetime.timedelta(days=close_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
            tcs = _req(f"https://www.eventbriteapi.com/v3/events/{eid}/ticket_classes/",
                       method="GET", token=cfg["eventbrite_token"])
            for tc in (tcs.get("ticket_classes") or [])[:1]:
                _req(f"https://www.eventbriteapi.com/v3/events/{eid}/ticket_classes/{tc['id']}/",
                     token=cfg["eventbrite_token"], json_body={"ticket_class": {"sales_end": cutoff}})
        except Exception as e:
            print("[eventbrite] could not move the sales cutoff (event itself moved):", e)
    return "updated"

MAX_ATTENDEE_PAGES = 200          # ~10k attendees at 50/page; a stop against a bad loop

def fetch_attendees(event_id, cfg, _req_fn=None):
    """Pull EVERY attendee for an Eventbrite event, following pagination.

    Eventbrite returns a `pagination` object alongside each page:
        {"has_more_items": true, "continuation": "<opaque token>", ...}
    Only the first page is returned unless the continuation token is passed back,
    so a naive single request silently under-reports the roster. Loop until
    has_more_items is false.

    Returns a list of raw attendee dicts. Raises on HTTP errors so the caller can
    decide (the retry queue treats them as retryable)."""
    req = _req_fn or _req
    token = cfg["eventbrite_token"]
    base = f"https://www.eventbriteapi.com/v3/events/{event_id}/attendees/"
    out, continuation, pages = [], None, 0
    while True:
        url = base + (f"?continuation={urllib.parse.quote(continuation)}" if continuation else "")
        res = req(url, method="GET", token=token) or {}
        out.extend(res.get("attendees") or [])
        pages += 1
        pg = res.get("pagination") or {}
        if not pg.get("has_more_items"):
            break
        continuation = pg.get("continuation")
        if not continuation:
            # has_more_items with no token to follow: stop rather than re-request
            # page one forever.
            print(f"[eventbrite] pagination claimed more items but sent no continuation "
                  f"after page {pages}; stopping with {len(out)} attendee(s)")
            break
        if pages >= MAX_ATTENDEE_PAGES:
            print(f"[eventbrite] stopped at the {MAX_ATTENDEE_PAGES}-page safety limit "
                  f"with {len(out)} attendee(s)")
            break
    print(f"[eventbrite] fetched {len(out)} attendee(s) for event {event_id} across {pages} page(s)")
    return out

def normalize_attendee(a):
    """Eventbrite attendee -> the fields the roster actually uses."""
    p = a.get("profile") or {}
    name = (p.get("name") or " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x)).strip()
    return {
        "external_id": str(a.get("id") or ""),
        "name": name or "(no name)",
        "email": (p.get("email") or "").strip(),
        "phone": (p.get("cell_phone") or p.get("home_phone") or p.get("work_phone") or "").strip(),
        # Eventbrite marks these separately; either means they are not coming.
        "refunded": bool(a.get("refunded") or a.get("cancelled")),
        # Eventbrite's attendee object carries a checked_in boolean, and each
        # barcode records whether it was scanned at the door. Either counts as
        # "this person actually turned up". Both stay false when the venue never
        # scans tickets, which the caller has to treat as "attendance unknown"
        # rather than "nobody came".
        "checked_in": bool(a.get("checked_in")) or any(
            (b or {}).get("status") == "used" for b in (a.get("barcodes") or [])),
    }

def sync_attendees(cls, cfg=None, _req_fn=None):
    """All attendees for a class, normalized. Returns None when there is nothing to
    sync (no Eventbrite id, no credentials, or not live)."""
    cfg = cfg or load_config()
    event_id = (cls.get("external_ids") or {}).get("eventbrite_id") if isinstance(cls.get("external_ids"), dict) else None
    event_id = event_id or cls.get("eventbrite_id")
    if not event_id: return None
    if not cfg.get("eventbrite_token"): return None
    if not cfg.get("live") and not _req_fn: return None
    return [normalize_attendee(a) for a in fetch_attendees(event_id, cfg, _req_fn)]

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

def publish(cls, cfg=None, image_url=None):
    """Run every platform, in order. The Canva graphic is attached to the Eventbrite
    event. Pass image_url to reuse an already-reviewed graphic (the normal path, since
    an admin approves the poster first); omit it to build one now. Returns an
    external_ids dict for storage, incl. a per-platform _results breakdown. Never raises."""
    cfg = cfg or load_config()
    if image_url:
        canva = _ok(None, "using the approved poster", image_url=image_url)
    else:
        canva = _safe(render_canva, cls, cfg)      # no reviewed graphic: build one now
        image_url = canva.get("image_url")
    results = {
        "canva":      canva,
        "eventbrite": _safe(post_eventbrite, cls, cfg, image_url),   # 2. post + attach graphic
        "facebook":   _safe(post_facebook, cls, cfg, None),
        "wix":        _safe(post_wix, cls, cfg),
        "descene":    _safe(post_descene, cls, cfg),
    }
    ext = {}
    if image_url: ext["canva_image_url"] = image_url
    for k, v in results.items():
        if v.get("id"):
            ext[k + "_id"] = v["id"]
        flag = "LIVE" if cfg["live"] else "dry-run"
        print(f"[publish:{k}] {flag} ok={v['ok']} {v.get('status','')}"
              + (f" id={v['id']}" if v.get('id') else "")
              + (f" ERROR={v['error']}" if v.get('error') else ""))
    ext["_results"] = results
    return ext
