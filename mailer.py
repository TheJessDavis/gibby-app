"""
Real email layer for Gibby Class Manager. Config-driven and DRY-RUN-SAFE.

Sends via SMTP (works with SendGrid, Postmark, Amazon SES, Gmail, etc. — any
provider gives you SMTP host/port/user/pass). Add the settings to config.json
and set "email_live": true to actually send. Until then every email is logged,
not sent, so the app is safe to run with no mail account.

All mail is from gibby@theeverett.org. Going live also requires SPF/DKIM/DMARC
records on theeverett.org, or messages will be marked as spam.
"""
import smtplib, ssl, json, os
from email.message import EmailMessage

HERE = os.path.dirname(os.path.abspath(__file__))

def load_email_config():
    cfg = {
        "email_live": False,
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

def send(to, subject, body, cfg=None):
    cfg = cfg or load_email_config()
    recips = [to] if isinstance(to, str) else list(to)
    recips = [r for r in recips if r and "@" in r]
    if not recips:
        print(f"[email] no valid recipient for {subject!r}"); return False
    if not (cfg["email_live"] and cfg["smtp_host"]):
        print(f"[email] DRY-RUN from={cfg['mail_from']} to={recips} subject={subject!r}")
        return True
    try:
        msg = EmailMessage()
        msg["From"] = cfg["mail_from"]; msg["To"] = ", ".join(recips); msg["Subject"] = subject
        msg.set_content(body)
        ctx = ssl.create_default_context()
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as s:
            s.starttls(context=ctx)
            if cfg["smtp_user"]:
                s.login(cfg["smtp_user"], cfg["smtp_pass"])
            s.send_message(msg)
        print(f"[email] SENT to={recips} subject={subject!r}")
        return True
    except Exception as e:
        print(f"[email] FAILED to={recips} subject={subject!r} error={e}")
        return False

# ------------------------------------------------------------- templates ----
def tmpl_approved(cls, instr):
    return (f"Your class is approved: {cls['title']}",
        f"Hi {instr['name'].split()[0]},\n\n"
        f"Your class \"{cls['title']}\" is approved and going live.\n\n"
        f"When: {cls.get('slot_date','')} {cls.get('slot_time','')}\n"
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
        f"When: {cls.get('slot_date','')} {cls.get('slot_time','')}\n"
        f"Where: The Gibby, {cls.get('room','')}\n\n"
        f"{cfg['logistics']}\n\nSee you there,\nThe Gibby")

def tmpl_followup(cls, cfg):
    return (f"Thanks for joining {cls['title']}!",
        f"Hello,\n\nThank you for coming to \"{cls['title']}\"! We hope you had a great time and made something you love.\n\n"
        f"If you enjoyed it, a quick Google review means the world to us: {cfg['google_review_url']}\n\n"
        f"And if you took any photos, we would love for you to share them.\n\nWith gratitude,\nThe Gibby")
