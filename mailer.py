"""
Real email layer for Gibby Class Manager. Config-driven and DRY-RUN-SAFE.

Sends via SMTP (works with SendGrid, Postmark, Amazon SES, Gmail, etc. — any
provider gives you SMTP host/port/user/pass). Add the settings to config.json
and set "email_live": true to actually send. Until then every email is logged,
not sent, so the app is safe to run with no mail account.

All mail is from gibby@theeverett.org. Going live also requires SPF/DKIM/DMARC
records on theeverett.org, or messages will be marked as spam.
"""
import smtplib, ssl, json, os, base64, time, urllib.request, urllib.error
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))

def load_email_config():
    cfg = {
        "email_live": os.environ.get("EMAIL_LIVE", "").lower() in ("1", "true", "yes"),
        "smtp_host": os.environ.get("SMTP_HOST", ""),
        "smtp_port": int(os.environ.get("SMTP_PORT", "587") or 587),
        "smtp_user": os.environ.get("SMTP_USER", ""),
        "smtp_pass": os.environ.get("SMTP_PASS", ""),
        "mail_from": os.environ.get("MAIL_FROM", "gibby@theeverett.org"),
        # logistics text reused in the reminder email (brief: "what to bring, parking, arrival")
        "logistics": "Please arrive 10 minutes early. Free parking is available in the lot behind the building. "
                     "All materials are provided; just bring yourself and anything noted in the class description.",
        "google_review_url": "https://g.page/r/theeverett/review",
    }
    path = os.path.join(HERE, "config.json")
    if os.path.isfile(path):
        try:
            cfg.update({k: v for k, v in json.load(open(path)).items()
                        if k in cfg and v not in (None, "")})
        except Exception as e:
            print("[email:config] could not read config.json:", e)
    return cfg

APP_URL = os.environ.get("APP_URL", "https://gibby-app-ddjo.onrender.com")

LOGO_URL = os.environ.get("EMAIL_LOGO_URL", APP_URL + "/gibby-logo.jpg")   # the Gibby Center for the Arts logo

def text_to_html(body):
    """Turn the app's plain-text email into tidy HTML: paragraphs, bullet lists,
    links, and a button for any 'Label: https://…' line. The plain text stays
    as the alternative part, so nothing is lost for old mail apps."""
    import html as _h, re as _re
    url_re = _re.compile(r"(https?://[^\s<>\"']+)")
    def linkify(t):
        return url_re.sub(lambda mm: f'<a href="{mm.group(1)}" style="color:#2B4C7E">{mm.group(1)}</a>', t)
    out, para, lst = [], [], []
    def flush_para():
        if para:
            out.append('<p style="margin:0 0 14px;line-height:1.55">' + "<br>".join(para) + "</p>"); para.clear()
    def flush_list():
        if lst:
            out.append('<ul style="margin:0 0 14px 18px;padding:0;line-height:1.5">' + "".join(f"<li style='margin:0 0 6px'>{x}</li>" for x in lst) + "</ul>"); lst.clear()
    for raw in body.split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush_para(); flush_list(); continue
        if s in ("----", "---", "***"):
            flush_para(); flush_list(); out.append('<hr style="border:0;border-top:1px solid #E7DECB;margin:18px 0">'); continue
        mbtn = _re.match(r"^([A-Za-z][^:]{1,90}):\s*(https?://\S+)$", s)
        if mbtn and "•" not in s:
            flush_para(); flush_list()
            # The words before the colon become the button. A dangling "at" / "here"
            # / "to" reads as if the label were cut off, so trim it.
            label = _re.sub(r"\s+(at|here|to|from|via|on)$", "", mbtn.group(1).strip(), flags=_re.I).rstrip(",;")
            label = _re.sub(r"^(after that|then|now),?\s+", "", label, flags=_re.I)
            label = label[:1].upper() + label[1:]
            out.append(f'<p style="margin:6px 0 18px"><a href="{mbtn.group(2)}" style="display:inline-block;background:#171512;color:#fff;text-decoration:none;'
                       f'padding:12px 20px;border-radius:999px;font-weight:700">{_h.escape(label)}</a></p>')
            continue
        mb = _re.match(r"^(?:[•\-–]|\d+\.)\s+(.*)$", s)
        if mb and (raw.startswith(" ") or s.startswith("•") or s.startswith("- ")):
            flush_para(); lst.append(linkify(_h.escape(mb.group(1)))); continue
        if lst and raw.startswith("    "):        # continuation line of a bullet ("Register: …")
            lst[-1] += "<br>" + linkify(_h.escape(s)); continue
        flush_list(); para.append(linkify(_h.escape(s)))
    flush_para(); flush_list()
    return "".join(out)

def photo_grid(images, label="From class"):
    """Up to five photos, two per row, under the email text."""
    if not images: return ""
    cells = "".join(f'<td style="padding:4px;width:50%"><img src="{u}" alt="" width="270" style="display:block;width:100%;max-width:270px;height:auto;border-radius:12px;border:1px solid #E7DECB"></td>' for u in images[:5])
    rows, imgs = [], list(images[:5])
    while imgs:
        pair = imgs[:2]; imgs = imgs[2:]
        rows.append("<tr>" + "".join(f'<td style="padding:4px;width:50%;vertical-align:top"><img src="{u}" alt="" width="270" style="display:block;width:100%;max-width:270px;height:auto;border-radius:12px;border:1px solid #E7DECB"></td>' for u in pair) + ("<td></td>" if len(pair) == 1 else "") + "</tr>")
    return ('<p style="margin:18px 0 6px;font-size:13px;color:#6b665c;font-weight:700">' + label + '</p>'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0">' + "".join(rows) + "</table>")

def html_email(subject, body, from_name=None, images=None, banner=None):
    import html as _h
    who = _h.escape(from_name or "The Gibby")
    # banner: (label, background) pill above the title, e.g. ("Help wanted", "#EAF4E2")
    pill = ""
    if banner:
        pill = (f'<div style="display:inline-block;background:{banner[1]};border-radius:999px;padding:6px 14px;'
                f'font-size:13px;font-weight:700;margin:0 0 12px">{_h.escape(banner[0])}</div><br>')
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#F5EFE3;font-family:'Helvetica Neue',Arial,'Segoe UI',sans-serif;color:#171512">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#F5EFE3"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%">
<tr><td style="padding:0 8px 14px"><table role="presentation" cellspacing="0" cellpadding="0"><tr>
{('<td style="vertical-align:middle;padding-right:14px"><img src="' + LOGO_URL + '" width="64" height="64" alt="Gibby Center for the Arts" style="display:block;border-radius:14px;background:#fff"></td>') if LOGO_URL else ''}
<td style="vertical-align:middle;font-family:Georgia,serif;font-size:22px;letter-spacing:.2px"><b>The Gibby</b><div style="font-family:'Helvetica Neue',Arial,sans-serif;font-size:12px;color:#6b665c;margin-top:2px">Gibby Center for the Arts · Middletown, DE</div></td>
</tr></table></td></tr>
<tr><td style="background:#FBF7EF;border:1px solid #E7DECB;border-radius:18px;padding:26px 28px;font-size:16px">
{pill}<h1 style="font-family:Georgia,serif;font-size:22px;line-height:1.3;margin:0 0 16px">{_h.escape(subject)}</h1>
{text_to_html(body)}{photo_grid(images, "Pictures" if banner else "From class")}
</td></tr>
<tr><td style="padding:16px 10px 0;font-size:12px;color:#6b665c;line-height:1.5">Sent by {who} through the Gibby Class Manager · Gibby Center for the Arts, 51 W Main St, Middletown, DE<br>
<a href="{APP_URL}" style="color:#6b665c">{APP_URL}</a></td></tr>
</table></td></tr></table></body></html>"""

LAST_ERROR = ""   # the most recent send failure, so the app can show it; cleared on success
LAST_ROUTE = ""   # "bridge" or "smtp": which route carried the last delivered email

RUNTIME_BRIDGE = {}   # the mail bridge saved by an admin in the app (url, key); env wins

def bridge_config():
    """A dedicated Gibby Mail Bridge (Apps Script on the gibby@everetttheatre.com
    account, scripts/mail-bridge.gs) sends every email as that mailbox. Set it
    with MAIL_BRIDGE_URL / MAIL_BRIDGE_KEY or under Connections > Email. Without
    one, the calendar bridge is tried and explains what it needs.
    MAIL_VIA_BRIDGE=0 turns the whole route off."""
    url = os.environ.get("MAIL_BRIDGE_URL", "") or RUNTIME_BRIDGE.get("url", "")
    key = os.environ.get("MAIL_BRIDGE_KEY", "") or RUNTIME_BRIDGE.get("key", "")
    dedicated = bool(url)
    if not url:
        url = os.environ.get("GCAL_WEBHOOK_URL", ""); key = os.environ.get("GCAL_WEBHOOK_KEY", "")
    return {
        "url": url, "key": key, "dedicated": dedicated,
        "enabled": os.environ.get("MAIL_VIA_BRIDGE", "1").lower() not in ("0", "false", "no"),
    }

def bridge_available():
    b = bridge_config()
    return bool(b["enabled"] and b["url"])

def send_via_bridge(recips, subject, body, cfg, attachments, reply_to=None, from_name=None, images=None, banner=None):
    """Returns True when the bridge accepted the message; raises with the
    bridge's own reason otherwise (old script version, alias missing, quota)."""
    b = bridge_config()
    atts = [{"filename": fn, "mime": mime or "application/octet-stream",
             "b64": base64.b64encode(data).decode()} for fn, data, mime in (attachments or [])]
    # One message per recipient: students must never see each other's addresses.
    for one in recips:
        payload = {"key": b["key"], "action": "email", "to": [one], "subject": subject,
                   "body": body, "html": html_email(subject, body, from_name, images, banner), "from": cfg["mail_from"],
                   "name": from_name or "The Gibby", "attachments": atts}
        if reply_to: payload["replyTo"] = reply_to
        req = urllib.request.Request(b["url"], data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "GibbyClassManager/1.0"})
        raw = None
        for attempt in (1, 2):
            # Retry ONLY when the request never reached the script (a 404 from
            # script.google.com's front door). A timeout or a 5xx can mean Gmail
            # already sent the message; retrying those is how people got the
            # same email twice.
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    raw = r.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as e:
                if attempt == 2 or e.code != 404: raise
                print(f"[email] bridge answered 404 before running; retrying once"); time.sleep(3)
            except (TimeoutError, OSError) as e:
                if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
                    print(f"[email] bridge timed out; assuming it sent (no retry): {subject!r} -> {one}")
                    raw = json.dumps({"ok": True, "assumed": True}); break
                if attempt == 2: raise
                print(f"[email] bridge unreachable ({e}); retrying once"); time.sleep(3)
        _check_bridge_reply(raw)
    return True

def _check_bridge_reply(raw):
    try:
        res = json.loads(raw)
    except ValueError:
        # Apps Script answers with an HTML error page when the script needs a
        # permission it has not been granted yet; surface its one-line reason.
        import re as _re
        text = _re.sub(r"<[^>]+>", " ", raw)
        mm = _re.search(r"(Exception:|Authorization is required|Script function not found)[^<]{0,300}", text)
        raise RuntimeError("the bridge answered with an error page: "
                           + (" ".join(mm.group(0).split()) if mm else text.strip()[:200]))
    if res.get("ok"):
        return
    err = str(res.get("error") or res)
    if err == "unknown action":
        err = ("the Gibby Calendar Bridge script is an older version without the email action; "
               "paste the current scripts/gcal-webhook.gs and deploy a new version")
    raise RuntimeError(err)

COPY_TO = ""      # every outgoing email is copied here (set by the server from Connections > Email)

# ------------------------------------------------------------- limits ----
# Hard ceilings on what the app may send on its own. They live in a table in
# the app's database (LIMIT_DB, set by the server) so a restart or redeploy
# never resets them. Automated mail (scheduler, sweeps) is held to:
#   - the same subject to the same person at most once in SAME_SUBJECT_HOURS,
#   - AUTO_PER_HOUR / AUTO_PER_DAY messages in total (copies to the watch
#     address not counted).
# Mail a person triggered from the app (origin "user") keeps only the
# ten-minute double-tap dedupe above, so a deliberate re-send still works.
LIMIT_DB = ""
LIMITS = {"same_subject_hours": 24, "auto_per_hour": 40, "auto_per_day": 200}
_LIMIT_ALERTED_DAY = ""

def _ldb():
    import sqlite3
    c = sqlite3.connect(LIMIT_DB, timeout=10)
    c.execute("PRAGMA busy_timeout=8000")
    c.execute("""CREATE TABLE IF NOT EXISTS mail_sent(id INTEGER PRIMARY KEY, recipient TEXT, subj_hash TEXT,
                 subject TEXT, sent_at TEXT, origin TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS mail_sent_rs ON mail_sent(recipient, subj_hash, sent_at)")
    return c

def _subj_hash(subject):
    import hashlib
    return hashlib.sha1((subject or "").strip().lower().encode("utf-8", "replace")).hexdigest()

def _limit(recips, subject, origin):
    """Return the recipients this message may still go to under the limits."""
    if not LIMIT_DB or origin != "auto":
        return recips
    import datetime as _dt
    try:
        c = _ldb(); nowdt = _dt.datetime.now(); h = _subj_hash(subject)
        since = (nowdt - _dt.timedelta(hours=LIMITS["same_subject_hours"])).isoformat(timespec="seconds")
        allowed, blocked = [], []
        for r in recips:
            if c.execute("SELECT 1 FROM mail_sent WHERE recipient=? AND subj_hash=? AND sent_at>=? AND origin='auto'",
                         (r.lower(), h, since)).fetchone():
                blocked.append(r); continue
            allowed.append(r)
        if blocked:
            print(f"[email] LIMIT: {subject!r} already went to {blocked} in the last {LIMITS['same_subject_hours']}h; not sending again")
        is_copy = subject.startswith("[Copy") or subject.startswith("[Limit")
        if allowed and not is_copy:
            hour_ago = (nowdt - _dt.timedelta(hours=1)).isoformat(timespec="seconds")
            day_ago = (nowdt - _dt.timedelta(days=1)).isoformat(timespec="seconds")
            n_h = c.execute("SELECT COUNT(*) FROM mail_sent WHERE origin='auto' AND sent_at>=? AND subject NOT LIKE '[Copy%'", (hour_ago,)).fetchone()[0]
            n_d = c.execute("SELECT COUNT(*) FROM mail_sent WHERE origin='auto' AND sent_at>=? AND subject NOT LIKE '[Copy%'", (day_ago,)).fetchone()[0]
            if n_h + len(allowed) > LIMITS["auto_per_hour"] or n_d + len(allowed) > LIMITS["auto_per_day"]:
                print(f"[email] LIMIT: automated mail cap reached ({n_h} this hour, {n_d} today); not sending {subject!r} to {allowed}")
                blocked += allowed; allowed = []
                _limit_alert(n_h, n_d, subject)
        for r in blocked:
            c.execute("INSERT INTO mail_sent(recipient,subj_hash,subject,sent_at,origin) VALUES(?,?,?,?,'blocked')",
                      (r.lower(), h, (subject or "")[:200], nowdt.isoformat(timespec="seconds")))
        c.commit(); c.close()
        return allowed
    except Exception as e:
        print("[email] limiter error (sending anyway):", e)
        return recips

def _record_sent(recips, subject, origin):
    if not LIMIT_DB: return
    import datetime as _dt
    try:
        c = _ldb(); h = _subj_hash(subject); t = _dt.datetime.now().isoformat(timespec="seconds")
        c.executemany("INSERT INTO mail_sent(recipient,subj_hash,subject,sent_at,origin) VALUES(?,?,?,?,?)",
                      [(r.lower(), h, (subject or "")[:200], t, origin) for r in recips])
        c.execute("DELETE FROM mail_sent WHERE sent_at < ?", ((_dt.datetime.now() - _dt.timedelta(days=45)).isoformat(timespec="seconds"),))
        c.commit(); c.close()
    except Exception as e:
        print("[email] limiter record error:", e)

def _limit_alert(n_h, n_d, subject):
    """Tell the watch address once a day that the cap was hit."""
    global _LIMIT_ALERTED_DAY
    import datetime as _dt
    today = _dt.date.today().isoformat()
    if not COPY_TO or _LIMIT_ALERTED_DAY == today: return
    _LIMIT_ALERTED_DAY = today
    try:
        send(COPY_TO, "[Limit] The app paused its automated emails",
             f"The Gibby Class Manager hit its safety cap for automated email ({n_h} in the last hour, {n_d} in the last day; "
             f"the caps are {LIMITS['auto_per_hour']} an hour and {LIMITS['auto_per_day']} a day).\n\n"
             f"The message it held back was: {subject!r}. Anything else automated is held until the count drops. "
             f"Emails people send from the app are not affected.\n\nIf this is unexpected, something is looping; "
             f"see Email under More in the app for today's counts.", copy=False, origin="user")
    except Exception as e:
        print("[email] limit alert failed:", e)

def limit_stats():
    """For the Email settings page: what went out and what was held back."""
    out = {"caps": dict(LIMITS), "auto_last_hour": 0, "auto_today": 0, "blocked_today": 0, "user_today": 0}
    if not LIMIT_DB: return out
    import datetime as _dt
    try:
        c = _ldb(); nowdt = _dt.datetime.now()
        hour_ago = (nowdt - _dt.timedelta(hours=1)).isoformat(timespec="seconds")
        day_ago = (nowdt - _dt.timedelta(days=1)).isoformat(timespec="seconds")
        out["auto_last_hour"] = c.execute("SELECT COUNT(*) FROM mail_sent WHERE origin='auto' AND sent_at>=? AND subject NOT LIKE '[Copy%'", (hour_ago,)).fetchone()[0]
        out["auto_today"] = c.execute("SELECT COUNT(*) FROM mail_sent WHERE origin='auto' AND sent_at>=? AND subject NOT LIKE '[Copy%'", (day_ago,)).fetchone()[0]
        out["user_today"] = c.execute("SELECT COUNT(*) FROM mail_sent WHERE origin='user' AND sent_at>=? AND subject NOT LIKE '[Copy%'", (day_ago,)).fetchone()[0]
        out["blocked_today"] = c.execute("SELECT COUNT(*) FROM mail_sent WHERE origin='blocked' AND sent_at>=?", (day_ago,)).fetchone()[0]
        out["blocked"] = [{"to": r[0], "subject": r[1], "at": r[2]} for r in c.execute(
            "SELECT recipient, subject, sent_at FROM mail_sent WHERE origin='blocked' AND sent_at>=? ORDER BY id DESC LIMIT 20", (day_ago,)).fetchall()]
        c.close()
    except Exception as e:
        print("[email] limiter stats error:", e)
    return out

def _copy(subject, body, recips, cfg, from_name=None, origin="auto"):
    """One copy of an outgoing email to the Gibby's watch address."""
    if not COPY_TO or any(r.lower() == COPY_TO.lower() for r in recips) or subject.startswith("[Copy"):
        return
    try:
        send(COPY_TO, f"[Copy to {', '.join(recips)}] {subject}",
             f"(Sent{' as ' + from_name if from_name else ''} to {', '.join(recips)})\n\n{body}", cfg, copy=False, origin=origin)
    except Exception as e:
        print("[email] copy failed:", e)

_RECENT = {}      # (recipient, subject, body-hash) -> time sent; identical repeats inside the window are dropped
DEDUPE_SECONDS = 600

def _dedupe(recips, subject, body):
    """Two calls asking for the exact same email to the same person within ten
    minutes almost always mean a double tap or a repeated hook, never a real
    second message. Return the recipients that are genuinely new."""
    import hashlib
    now_ = time.time()
    for k, t in list(_RECENT.items()):
        if now_ - t > DEDUPE_SECONDS: _RECENT.pop(k, None)
    h = hashlib.sha1((subject + "\n" + body).encode("utf-8", "replace")).hexdigest()
    fresh = []
    for r in recips:
        k = (r.lower(), h)
        if k in _RECENT:
            print(f"[email] duplicate suppressed: {subject!r} -> {r} (sent {int(now_ - _RECENT[k])}s ago)")
            continue
        _RECENT[k] = now_; fresh.append(r)
    return fresh

import threading, queue
_tl = threading.local()          # _tl.defer = True inside an HTTP request: emails go to the queue
_queue = queue.Queue()

def defer_in_this_thread(on):
    """Request handlers turn this on: their emails are queued and sent by the
    worker below, so a tap answers in milliseconds instead of waiting on Gmail.
    The scheduler thread leaves it off and keeps sending synchronously."""
    _tl.defer = bool(on)

def _worker():
    while True:
        args, kwargs = _queue.get()
        try:
            _send_now(*args, **kwargs)
        except Exception as e:
            print("[email] queued send failed:", e)
        finally:
            _queue.task_done()

threading.Thread(target=_worker, daemon=True, name="email-worker").start()

def send(to, subject, body, cfg=None, attachments=None, reply_to=None, from_name=None, copy=True, images=None, wait=False, banner=None, origin=None):
    """attachments: list of (filename, bytes, mime) tuples, e.g. a contract PDF.
    Inside a request (see defer_in_this_thread) the message is queued and True is
    returned at once; pass wait=True when the caller needs the real outcome.
    origin: "user" for mail a person triggered, "auto" for the scheduler; worked
    out from the calling thread when not given (see the limits above)."""
    in_request = getattr(_tl, "defer", False)
    origin = origin or ("user" if in_request else "auto")
    if in_request and not wait:
        recips = [to] if isinstance(to, str) else list(to)
        recips = [r for r in recips if r and "@" in r]
        if not recips: return False
        fresh = _dedupe(recips, subject, body)
        if not fresh: return True
        _queue.put(((fresh, subject, body), dict(cfg=cfg, attachments=attachments, reply_to=reply_to, from_name=from_name, copy=copy, images=images, banner=banner, _deduped=True, origin=origin)))
        return True
    return _send_now(to, subject, body, cfg=cfg, attachments=attachments, reply_to=reply_to, from_name=from_name, copy=copy, images=images, banner=banner, origin=origin)

def _send_now(to, subject, body, cfg=None, attachments=None, reply_to=None, from_name=None, copy=True, images=None, _deduped=False, banner=None, origin="auto"):
    """attachments: list of (filename, bytes, mime) tuples, e.g. a contract PDF."""
    cfg = cfg or load_email_config()
    recips = [to] if isinstance(to, str) else list(to)
    recips = [r for r in recips if r and "@" in r]
    if not recips:
        print(f"[email] no valid recipient for {subject!r}"); return False
    if not _deduped:
        recips = _dedupe(recips, subject, body)
    if not recips:
        return True          # already sent moments ago; nothing more to do
    recips = _limit(recips, subject, origin)
    if not recips:
        return False         # held back by the limits above (logged there)
    # Every email links back to the app, so nobody has to hunt for the address.
    if APP_URL not in body:
        body = body.rstrip() + f"\n\nOpen the Gibby Class Manager: {APP_URL}"
    if not (cfg["email_live"] and (cfg["smtp_host"] or bridge_available())):
        print(f"[email] DRY-RUN from={cfg['mail_from']} to={recips} subject={subject!r}"
              + (f" attachments={[a[0] for a in attachments]}" if attachments else "")
              + (f" (+copy to {COPY_TO})" if copy and COPY_TO and not subject.startswith("[Copy") else ""))
        _record_sent(recips, subject, origin)
        return True
    global LAST_ERROR, LAST_ROUTE
    errors = []
    if bridge_available():
        try:
            if send_via_bridge(recips, subject, body, cfg, attachments, reply_to, from_name, images, banner):
                LAST_ERROR = ""; LAST_ROUTE = "bridge"
                print(f"[email] SENT via bridge to={recips} subject={subject!r}")
                _record_sent(recips, subject, origin)
                if copy: _copy(subject, body, recips, cfg, from_name, origin)
                return True
        except Exception as e:
            errors.append(f"Google bridge: {e}")
            print(f"[email] bridge could not send to={recips} subject={subject!r} error={e}")
    if cfg["smtp_host"]:
        try:
            msg = EmailMessage()
            from email.utils import formataddr
            msg["From"] = formataddr((from_name, cfg["mail_from"])) if from_name else cfg["mail_from"]
            if len(recips) == 1:
                msg["To"] = recips[0]
            else:   # never expose one student's address to another
                msg["To"] = cfg["mail_from"]; msg["Bcc"] = ", ".join(recips)
            if reply_to: msg["Reply-To"] = reply_to
            msg["Subject"] = subject
            msg.set_content(body)
            msg.add_alternative(html_email(subject, body, from_name, images, banner), subtype="html")
            for fn, data, mime in (attachments or []):
                mt, _, st = (mime or "application/octet-stream").partition("/")
                msg.add_attachment(data, maintype=mt, subtype=st or "octet-stream", filename=fn)
            ctx = ssl.create_default_context()
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as s:
                s.starttls(context=ctx)
                if cfg["smtp_user"]:
                    s.login(cfg["smtp_user"], cfg["smtp_pass"])
                s.send_message(msg)
            LAST_ERROR = ""; LAST_ROUTE = "smtp"
            print(f"[email] SENT via smtp to={recips} subject={subject!r}")
            _record_sent(recips, subject, origin)
            if copy: _copy(subject, body, recips, cfg, from_name, origin)
            return True
        except Exception as e:
            errors.append(f"SMTP: {type(e).__name__}: {e}")
            print(f"[email] smtp FAILED to={recips} subject={subject!r} error={e}")
    import datetime as _dt
    LAST_ERROR = (f"{_dt.datetime.now().strftime('%b %d %I:%M %p')} · to {', '.join(recips)} · \"{subject}\" · "
                  + (" | ".join(errors) or "no email route is configured"))
    return False

# ------------------------------------------------------------- templates ----
def tmpl_approved(cls, instr):
    return (f"Your class is approved: {cls['title']}",
        f"Hi {instr['name'].split()[0]},\n\n"
        f"Your class \"{cls['title']}\" is approved and going live.\n\n"
        f"When: {cls.get('slot_date','')} {cls.get('class_time') or cls.get('slot_time','')}\n"
        f"Where: The Gibby, {cls.get('room','')}\n"
        f"Ticket: ${cls.get('ticket_price','')}  |  Your pay: ${cls.get('instructor_pay','')}\n\n"
        f"It is now posted for registration. You will see enrollment as students sign up.\n\n"
        f"Thanks,\nThe Gibby")

def tmpl_incomplete(cls, instr, note):
    return (f"Changes needed: {cls['title']}",
        f"Hi {instr['name'].split()[0]},\n\n"
        f"Your submission \"{cls['title']}\" is not secured yet. An admin asked for a change:\n\n"
        f"  {note or 'Please review and resubmit.'}\n\n"
        f"The time slot is still open on a first-come basis. Please log back in, make the correction, and resubmit.\n\n"
        f"Thanks,\nThe Gibby")

def tmpl_cancel(cls):
    return (f"Class cancelled: {cls['title']}",
        f"Hello,\n\nUnfortunately \"{cls['title']}\" on {cls.get('slot_date','')} has been cancelled because it did "
        f"not reach the minimum enrollment. You will be refunded in full through Eventbrite automatically.\n\n"
        f"We would love to see you at another class soon. Thank you for your understanding.\n\nThe Gibby")

def tmpl_reminder(cls, cfg):
    return (f"See you soon: {cls['title']}",
        f"Hello,\n\nThis is a reminder for \"{cls['title']}\".\n\n"
        f"When: {cls.get('slot_date','')} {cls.get('class_time') or cls.get('slot_time','')}\n"
        f"Where: The Gibby, {cls.get('room','')}\n\n"
        f"{cfg['logistics']}\n\nSee you there,\nThe Gibby")

def tmpl_followup(cls, cfg, attended=True, note="", instructor_name=""):
    """After-class note. `attended` says whether we actually know these people were
    there (Eventbrite check-in was scanned). When we do not know, nothing in the
    copy may assume they came: asking a no-show how they enjoyed it, and to leave a
    review, is the fastest way to earn a complaint.

    `note` is the instructor's own message, which carries the email when present."""
    signoff = (f"\n\nWith gratitude,\n{instructor_name} and everyone at The Gibby"
               if instructor_name else "\n\nWith gratitude,\nThe Gibby")
    if attended:
        subject = f"Thanks for joining {cls['title']}!"
        opening = (f"Thank you for coming to \"{cls['title']}\"! We hope you had a great time "
                   f"and made something you love.")
        ask = (f"If you enjoyed it, a quick Google review means the world to us: "
               f"{cfg['google_review_url']}\n\nAnd if you took any photos, we would love for you "
               f"to share them.\n\nP.S. If the class made your week, reply with a sentence we can "
               f"share; a few words from a real student help more than any ad.")
    else:
        # Deliberately ambiguous: this list mixes people who came with people who
        # only held a ticket, and we cannot tell them apart.
        subject = f"About {cls['title']} at The Gibby"
        opening = (f"\"{cls['title']}\" has wrapped up. We hope you enjoyed the class, or that "
                   f"you are looking forward to catching a future one.")
        ask = (f"If you did join us and enjoyed it, a Google review means the world to us: "
               f"{cfg['google_review_url']}\n\nAnd if you took any photos, we would love to see "
               f"them.\n\nEither way, we would love to have you at the next one.")
    body = f"Hello,\n\n{opening}\n\n"
    if note.strip():
        body += note.strip() + "\n\n"
    return (subject, body + ask + signoff)
