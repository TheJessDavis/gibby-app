"""
Real email layer for Gibby Class Manager. Config-driven and DRY-RUN-SAFE.

Sends via SMTP (works with SendGrid, Postmark, Amazon SES, Gmail, etc. — any
provider gives you SMTP host/port/user/pass). Add the settings to config.json
and set "email_live": true to actually send. Until then every email is logged,
not sent, so the app is safe to run with no mail account.

All mail is from gibby@theeverett.org. Going live also requires SPF/DKIM/DMARC
records on theeverett.org, or messages will be marked as spam.
"""
import smtplib, ssl, json, os, base64, urllib.request
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

LAST_ERROR = ""   # the most recent send failure, so the app can show it; cleared on success
LAST_ROUTE = ""   # "bridge" or "smtp": which route carried the last delivered email

def bridge_config():
    """The Gibby Calendar Bridge (Apps Script on the Everett Google account) can
    also send mail, as the mailbox that deployed it or any of its Send-mail-as
    aliases. No app password involved. MAIL_VIA_BRIDGE=0 turns the route off."""
    return {
        "url": os.environ.get("GCAL_WEBHOOK_URL", ""),
        "key": os.environ.get("GCAL_WEBHOOK_KEY", ""),
        "enabled": os.environ.get("MAIL_VIA_BRIDGE", "1").lower() not in ("0", "false", "no"),
    }

def bridge_available():
    b = bridge_config()
    return bool(b["enabled"] and b["url"])

def send_via_bridge(recips, subject, body, cfg, attachments):
    """Returns True when the bridge accepted the message; raises with the
    bridge's own reason otherwise (old script version, alias missing, quota)."""
    b = bridge_config()
    payload = {"key": b["key"], "action": "email", "to": recips, "subject": subject,
               "body": body, "from": cfg["mail_from"], "name": "The Gibby",
               "attachments": [{"filename": fn, "mime": mime or "application/octet-stream",
                                "b64": base64.b64encode(data).decode()}
                               for fn, data, mime in (attachments or [])]}
    req = urllib.request.Request(b["url"], data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "GibbyClassManager/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode("utf-8", "replace")
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
        return True
    err = str(res.get("error") or res)
    if err == "unknown action":
        err = ("the Gibby Calendar Bridge script is an older version without the email action; "
               "paste the current scripts/gcal-webhook.gs and deploy a new version")
    raise RuntimeError(err)

def send(to, subject, body, cfg=None, attachments=None):
    """attachments: list of (filename, bytes, mime) tuples, e.g. a contract PDF."""
    cfg = cfg or load_email_config()
    recips = [to] if isinstance(to, str) else list(to)
    recips = [r for r in recips if r and "@" in r]
    if not recips:
        print(f"[email] no valid recipient for {subject!r}"); return False
    # Every email links back to the app, so nobody has to hunt for the address.
    if APP_URL not in body:
        body = body.rstrip() + f"\n\nOpen the Gibby Class Manager: {APP_URL}"
    if not (cfg["email_live"] and (cfg["smtp_host"] or bridge_available())):
        print(f"[email] DRY-RUN from={cfg['mail_from']} to={recips} subject={subject!r}"
              + (f" attachments={[a[0] for a in attachments]}" if attachments else ""))
        return True
    global LAST_ERROR, LAST_ROUTE
    errors = []
    if bridge_available():
        try:
            if send_via_bridge(recips, subject, body, cfg, attachments):
                LAST_ERROR = ""; LAST_ROUTE = "bridge"
                print(f"[email] SENT via bridge to={recips} subject={subject!r}")
                return True
        except Exception as e:
            errors.append(f"Google bridge: {e}")
            print(f"[email] bridge could not send to={recips} subject={subject!r} error={e}")
    if cfg["smtp_host"]:
        try:
            msg = EmailMessage()
            msg["From"] = cfg["mail_from"]; msg["To"] = ", ".join(recips); msg["Subject"] = subject
            msg.set_content(body)
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
            return True
        except Exception as e:
            errors.append(f"SMTP: {type(e).__name__}: {e}")
            print(f"[email] smtp FAILED to={recips} subject={subject!r} error={e}")
    LAST_ERROR = " | ".join(errors) or "no email route is configured"
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
