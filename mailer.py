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

LOGO_URL = APP_URL + "/icon-192.png"

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
            out.append(f'<p style="margin:6px 0 18px"><a href="{mbtn.group(2)}" style="display:inline-block;background:#171512;color:#fff;text-decoration:none;'
                       f'padding:12px 20px;border-radius:999px;font-weight:700">{_h.escape(mbtn.group(1))}</a></p>')
            continue
        mb = _re.match(r"^(?:[•\-–]|\d+\.)\s+(.*)$", s)
        if mb and (raw.startswith(" ") or s.startswith("•") or s.startswith("- ")):
            flush_para(); lst.append(linkify(_h.escape(mb.group(1)))); continue
        if lst and raw.startswith("    "):        # continuation line of a bullet ("Register: …")
            lst[-1] += "<br>" + linkify(_h.escape(s)); continue
        flush_list(); para.append(linkify(_h.escape(s)))
    flush_para(); flush_list()
    return "".join(out)

def html_email(subject, body, from_name=None):
    import html as _h
    who = _h.escape(from_name or "The Gibby")
    return f"""<!doctype html><html><body style="margin:0;padding:0;background:#F5EFE3;font-family:'Helvetica Neue',Arial,'Segoe UI',sans-serif;color:#171512">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#F5EFE3"><tr><td align="center" style="padding:24px 12px">
<table role="presentation" width="600" cellspacing="0" cellpadding="0" style="max-width:600px;width:100%">
<tr><td style="padding:0 8px 14px"><table role="presentation" cellspacing="0" cellpadding="0"><tr>
<td style="vertical-align:middle;padding-right:12px"><img src="{LOGO_URL}" width="44" height="44" alt="" style="display:block;border-radius:12px"></td>
<td style="vertical-align:middle;font-family:Georgia,serif;font-size:22px;letter-spacing:.2px"><b>The Gibby</b><div style="font-family:'Helvetica Neue',Arial,sans-serif;font-size:12px;color:#6b665c;margin-top:2px">Gibby Center for the Arts · Middletown, DE</div></td>
</tr></table></td></tr>
<tr><td style="background:#FBF7EF;border:1px solid #E7DECB;border-radius:18px;padding:26px 28px;font-size:16px">
<h1 style="font-family:Georgia,serif;font-size:22px;line-height:1.3;margin:0 0 16px">{_h.escape(subject)}</h1>
{text_to_html(body)}
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

def send_via_bridge(recips, subject, body, cfg, attachments, reply_to=None, from_name=None):
    """Returns True when the bridge accepted the message; raises with the
    bridge's own reason otherwise (old script version, alias missing, quota)."""
    b = bridge_config()
    atts = [{"filename": fn, "mime": mime or "application/octet-stream",
             "b64": base64.b64encode(data).decode()} for fn, data, mime in (attachments or [])]
    # One message per recipient: students must never see each other's addresses.
    for one in recips:
        payload = {"key": b["key"], "action": "email", "to": [one], "subject": subject,
                   "body": body, "html": html_email(subject, body, from_name), "from": cfg["mail_from"],
                   "name": from_name or "The Gibby", "attachments": atts}
        if reply_to: payload["replyTo"] = reply_to
        req = urllib.request.Request(b["url"], data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "GibbyClassManager/1.0"})
        raw = None
        for attempt in (1, 2):      # Apps Script occasionally answers 404/5xx for one call; try twice
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    raw = r.read().decode("utf-8", "replace")
                break
            except urllib.error.HTTPError as e:
                if attempt == 2 or e.code not in (404, 429, 500, 502, 503, 504): raise
                print(f"[email] bridge answered {e.code}; retrying once"); time.sleep(3)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
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

def _copy(subject, body, recips, cfg, from_name=None):
    """One copy of an outgoing email to the Gibby's watch address."""
    if not COPY_TO or any(r.lower() == COPY_TO.lower() for r in recips) or subject.startswith("[Copy"):
        return
    try:
        send(COPY_TO, f"[Copy to {', '.join(recips)}] {subject}",
             f"(Sent{' as ' + from_name if from_name else ''} to {', '.join(recips)})\n\n{body}", cfg, copy=False)
    except Exception as e:
        print("[email] copy failed:", e)

def send(to, subject, body, cfg=None, attachments=None, reply_to=None, from_name=None, copy=True):
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
              + (f" attachments={[a[0] for a in attachments]}" if attachments else "")
              + (f" (+copy to {COPY_TO})" if copy and COPY_TO and not subject.startswith("[Copy") else ""))
        return True
    global LAST_ERROR, LAST_ROUTE
    errors = []
    if bridge_available():
        try:
            if send_via_bridge(recips, subject, body, cfg, attachments, reply_to, from_name):
                LAST_ERROR = ""; LAST_ROUTE = "bridge"
                print(f"[email] SENT via bridge to={recips} subject={subject!r}")
                if copy: _copy(subject, body, recips, cfg, from_name)
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
            msg.add_alternative(html_email(subject, body, from_name), subtype="html")
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
            if copy: _copy(subject, body, recips, cfg, from_name)
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
