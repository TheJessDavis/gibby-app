#!/usr/bin/env python3
"""
Gibby Class Manager - real backend (Python standard library + SQLite).
No external dependencies. Run:  python3 server.py   then open http://localhost:8000

This is the real foundation: real database, real logins with Admin/Instructor
roles, and a persistent workflow (slots -> instructor submission -> admin
approval). External posting (Eventbrite, Facebook, Wix, Canva, Descene) and
emails are behind a stubbed integration layer that logs what it *would* do,
until real account credentials are available.
"""
import http.server, socketserver, json, sqlite3, os, hashlib, secrets, urllib.parse, datetime, http.cookies, random
import integrations, mailer, gcal, threading, time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", ROOT)   # point at a mounted volume in production so data persists
DB   = os.path.join(DATA_DIR, "gibby.db")
WEB  = os.path.join(ROOT, "web")
PORT = int(os.environ.get("PORT", "8000"))
SEED_PW = os.environ.get("SEED_PASSWORD", "gibby123")   # override in production!
VERSION = "1.9-links"

# ---------------------------------------------------------------- database ----
def db():
    c = sqlite3.connect(DB, timeout=10)      # wait up to 10s for the write lock instead of erroring
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=8000")
    return c

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, role TEXT,
      pw_hash TEXT, pw_salt TEXT);
    CREATE TABLE IF NOT EXISTS sessions(
      token TEXT PRIMARY KEY, user_id INTEGER, created TEXT);
    CREATE TABLE IF NOT EXISTS slots(
      id INTEGER PRIMARY KEY, date TEXT, start TEXT, end TEXT, room TEXT,
      status TEXT DEFAULT 'available');
    CREATE TABLE IF NOT EXISTS templates(
      id INTEGER PRIMARY KEY, title TEXT, category TEXT, age TEXT,
      description TEXT, supplies TEXT, archived INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS classes(
      id INTEGER PRIMARY KEY, title TEXT, instructor_id INTEGER,
      slot_date TEXT, slot_time TEXT, room TEXT, description TEXT,
      age_range TEXT, alcohol INTEGER DEFAULT 0, max_p INTEGER, min_p INTEGER,
      ticket_price REAL, instructor_pay REAL, supplies TEXT,
      headline TEXT, subtitle TEXT, photo TEXT,
      status TEXT DEFAULT 'pending', admin_note TEXT DEFAULT '',
      external_ids TEXT DEFAULT '{}', created TEXT);
    CREATE TABLE IF NOT EXISTS registrations(
      id INTEGER PRIMARY KEY, class_id INTEGER, name TEXT, email TEXT, phone TEXT,
      refunded INTEGER DEFAULT 0, created TEXT);
    CREATE TABLE IF NOT EXISTS integrations(
      id TEXT PRIMARY KEY, name TEXT, method TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS password_resets(
      token TEXT PRIMARY KEY, user_id INTEGER, expires TEXT);
    CREATE TABLE IF NOT EXISTS audit_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER,
      prev_status TEXT, new_status TEXT, actor_id INTEGER,
      ts TEXT, snapshot TEXT);
    -- Immutable at the DB layer: any UPDATE or DELETE against a written row aborts.
    CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
      BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
      BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
    """)
    try: c.execute("ALTER TABLE users ADD COLUMN must_change_pw INTEGER DEFAULT 0")
    except sqlite3.OperationalError: pass
    try: c.execute("ALTER TABLE slots ADD COLUMN source TEXT DEFAULT 'manual'")
    except sqlite3.OperationalError: pass
    for col in ("promoted","reminded","followed_up","low_alerted"):
        try: c.execute(f"ALTER TABLE classes ADD COLUMN {col} INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
    for col, typ in (("length","TEXT"),("pre_class","TEXT"),("own_materials","INTEGER DEFAULT 0"),
                     ("material_cost","REAL"),("needs_volunteer","INTEGER DEFAULT 0"),("slot_ids","TEXT"),
                     ("links","TEXT")):
        try: c.execute(f"ALTER TABLE classes ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError: pass
    c.commit()
    seed(c)
    c.close()

FAKE_STUDENTS = [
    ("Priya Nair","priya.n@gmail.com","(302) 555-0143"),("Marcus Bell","mbell@outlook.com","(302) 555-0197"),
    ("Tara Whitfield","tara.w@gmail.com","(302) 555-0166"),("Devon Cho","devoncho@icloud.com","(302) 555-0111"),
    ("Amara Okafor","amara.o@gmail.com","(302) 555-0128"),("Ben Ray","benray@gmail.com","(302) 555-0159"),
    ("Sofia Marchetti","sofiam@outlook.com","(302) 555-0174"),("Jordan Pratt","jpratt@gmail.com","(302) 555-0182"),
    ("Hannah Wells","hwells@gmail.com","(302) 555-0120"),("Ravi Anand","ravi.a@gmail.com","(302) 555-0133"),
    ("Claire Dubois","claire.d@icloud.com","(302) 555-0148"),("Leo Park","leopark@gmail.com","(302) 555-0155"),
]
def seed_registrations(c, cls):
    """Simulate Eventbrite registration sync: seed a plausible headcount so the
    roster and enrollment views have real data. Some classes land below minimum."""
    mn, mx = (cls["min_p"] or 6), (cls["max_p"] or 10)
    mod = cls["id"] % 3                       # spread outcomes so the dashboard shows variety
    n = max(0, mn-2) if mod==0 else (min(mx, mn+1) if mod==1 else mx)   # low / just-over / full
    for name,email,phone in random.sample(FAKE_STUDENTS, min(n, len(FAKE_STUDENTS))):
        c.execute("INSERT INTO registrations(class_id,name,email,phone,created) VALUES(?,?,?,?,?)",
                  (cls["id"], name, email, phone, now()))

def enrollment(c, class_id):
    return c.execute("SELECT COUNT(*) FROM registrations WHERE class_id=? AND refunded=0",(class_id,)).fetchone()[0]

def tmin(s):
    dt = datetime.datetime.strptime(s.strip(), "%I:%M %p"); return dt.hour*60 + dt.minute

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 100000).hex()
    return h, salt

def seed(c):
    if c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        # one admin + the instructors from The Gibby's roster (default password below)
        admins = [("Jess Davis", "jess@theeverett.org", "admin")]
        instructors = ["Allison Medley","Christin Smiertka","Danielle Sims","Dawn Hunter",
            "Gabriel Hurd","Harlee Light","Heather Capezio","Jason Weaver","Jess Kille",
            "Katie Gorman","Paige Davis"]
        rows = admins + [(n, ".".join(n.lower().split())+"@theeverett.org", "instructor") for n in instructors]
        for name, email, role in rows:
            h, s = hash_pw(SEED_PW)
            c.execute("INSERT INTO users(name,email,role,pw_hash,pw_salt) VALUES(?,?,?,?,?)",
                      (name, email, role, h, s))
    if c.execute("SELECT COUNT(*) FROM templates").fetchone()[0] == 0:
        tpls = [
          ("Paint Your Pet","Painting","Ages 16+","Create a custom acrylic portrait of your pet.",[["Canvas",1,5],["Paint & brushes",1,3]]),
          ("Beaded Earrings","Jewelry","All Ages","Beginner-friendly beaded earrings.",[["Bead kit",1,3]]),
          ("Stained Glass Mini","Glass","Ages 16+","Design and build a small stained glass piece.",[["Glass & solder kit",1,30]]),
          ("Junk Journal Workshop","Paper & Mixed Media","All Ages","Make a one-signature journal from recycled papers.",[["Paper pack",1,6]]),
          ("Little Makers","Kids / Family","Toddlers","Sensory art play for toddlers and a grown-up.",[["Paint & paper",1,2.5]]),
        ]
        for t in tpls:
            c.execute("INSERT INTO templates(title,category,age,description,supplies) VALUES(?,?,?,?,?)",
                      (t[0],t[1],t[2],t[3],json.dumps(t[4])))
    if c.execute("SELECT COUNT(*) FROM slots").fetchone()[0] == 0:
        # a few open Saturday slots (30-min), Spring cycle
        base = [("Sat, Jan 17","10:00 AM","10:30 AM","Large Room"),
                ("Sat, Jan 17","10:30 AM","11:00 AM","Large Room"),
                ("Sat, Jan 17","6:00 PM","6:30 PM","Studio"),
                ("Sat, Jan 24","1:00 PM","1:30 PM","Large Room"),
                ("Sat, Jan 24","1:30 PM","2:00 PM","Studio"),
                ("Sat, Jan 31","10:00 AM","10:30 AM","Studio")]
        for d,st,en,rm in base:
            c.execute("INSERT INTO slots(date,start,end,room) VALUES(?,?,?,?)",(d,st,en,rm))
    if c.execute("SELECT COUNT(*) FROM integrations").fetchone()[0] == 0:
        integ = [
          ("eventbrite","Eventbrite","REST API","connected"),
          ("facebook","Facebook","Graph API","disconnected"),
          ("wix","Wix","Wix REST API","disconnected"),
          ("descene","Descene","Browser automation","attention"),
          ("canva","Canva","Canva API","connected"),
        ]
        for i in integ: c.execute("INSERT INTO integrations(id,name,method,status) VALUES(?,?,?,?)", i)
    c.commit()

# ------------------------------------------------ external posting ----
def publish_class(cls):
    """Real posting layer (integrations.py). Reads config.json for account keys;
    each platform is skipped if unconfigured, and nothing is actually sent unless
    config's "live" is true. Returns external IDs / per-platform results."""
    return integrations.publish(cls)

def audit(c, class_id, prev_status, new_status, actor_id):
    """Append one immutable entry recording a Class status change. Snapshots the
    class record AS IT STANDS NOW (after the change). Append-only: this is the ONLY
    place the app writes audit_log, and it never updates or deletes; DB triggers
    enforce that even against bugs. actor_id is None for automated (scheduler) changes."""
    r = c.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    snap = json.dumps({k: r[k] for k in r.keys()}) if r else "{}"
    c.execute("INSERT INTO audit_log(class_id,prev_status,new_status,actor_id,ts,snapshot) VALUES(?,?,?,?,?,?)",
              (class_id, prev_status, new_status, actor_id, now(), snap))

def approve_and_publish(c, cls, actor_id=None):
    """Shared approval: publish, sync registrations, email the instructor, mark
    approved. Used by admin approval and by instructor approval of admin edits."""
    instr = dict(c.execute("SELECT * FROM users WHERE id=?",(cls["instructor_id"],)).fetchone())
    ext = publish_class(cls)
    gid = gcal.create_event({**cls, "instructor_name": instr["name"]}, gcal.load_gcal_config())  # add to Google Calendar
    if gid: ext["gcal_event_id"] = gid
    c.execute("UPDATE classes SET status='approved', external_ids=? WHERE id=?",(json.dumps(ext),cls["id"]))
    audit(c, cls["id"], cls.get("status"), "approved", actor_id)
    seed_registrations(c, cls)
    subj, body = mailer.tmpl_approved(cls, instr); mailer.send(instr["email"], subj, body)

def emails_for(c, where, args=()):
    return [r[0] for r in c.execute(f"SELECT email FROM users {where}", args).fetchall() if r[0]]

# --------------------------------------------------- lifecycle scheduler ----
_MONS = {m:i for i,m in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], start=1)}
def _class_date(cls, year=2027):
    try:
        p = (cls["slot_date"] or "").split(", ")[-1].split()
        return datetime.date(year, _MONS[p[0]], int(p[1]))
    except Exception:
        return None

def run_scheduler(asof=None):
    """One daily tick. Fires the brief's lifecycle automations. Dedup flags on the
    class row prevent repeats. Returns a list of the actions taken (for logging/UI)."""
    today = asof or datetime.date.today()
    c = db(); actions = []
    for r in c.execute("SELECT * FROM classes WHERE status IN ('approved','cancelled')").fetchall():
        cls = dict(r); d = _class_date(cls)
        if not d: continue
        days = (d - today).days
        enrolled = enrollment(c, cls["id"])
        students = [x[0] for x in c.execute("SELECT email FROM registrations WHERE class_id=? AND refunded=0",(cls["id"],)).fetchall()]
        instr = c.execute("SELECT email FROM users WHERE id=?",(cls["instructor_id"],)).fetchone()
        cfg = mailer.load_email_config()
        if cls["status"] == "approved":
            if 7 < days <= 14 and enrolled < (cls["min_p"] or 0) and not cls.get("low_alerted"):
                mailer.send(emails_for(c,"WHERE role='admin'"), f"Low enrollment: {cls['title']}",
                    f"\"{cls['title']}\" on {cls['slot_date']} has {enrolled}/{cls['min_p']} registered, two weeks out. Consider promoting it.")
                c.execute("UPDATE classes SET low_alerted=1 WHERE id=?",(cls["id"],)); actions.append(f"low-enroll alert: {cls['title']}")
            if days == 7 and enrolled < (cls["min_p"] or 0):
                c.execute("UPDATE classes SET status='cancelled' WHERE id=?",(cls["id"],))
                audit(c, cls["id"], cls.get("status"), "cancelled", None)   # automated: no actor
                c.execute("UPDATE registrations SET refunded=1 WHERE class_id=?",(cls["id"],))
                subj, body = mailer.tmpl_cancel(cls); mailer.send(students, subj, body)
                if instr and instr[0]: mailer.send(instr[0], "Your class was auto-cancelled", body)
                actions.append(f"AUTO-CANCELLED (refunded {len(students)}): {cls['title']}"); continue
            if days == 2 and not cls.get("reminded"):
                subj, body = mailer.tmpl_reminder(cls, cfg); mailer.send(students, subj, body)
                c.execute("UPDATE classes SET reminded=1 WHERE id=?",(cls["id"],)); actions.append(f"48h reminder ({len(students)}): {cls['title']}")
        if days == -1 and not cls.get("followed_up") and cls["status"] != "cancelled":
            subj, body = mailer.tmpl_followup(cls, cfg); mailer.send(students, subj, body)
            c.execute("UPDATE classes SET followed_up=1 WHERE id=?",(cls["id"],)); actions.append(f"day-after follow-up ({len(students)}): {cls['title']}")
    c.commit(); c.close()
    if actions: print("[scheduler]", "; ".join(actions))
    return actions

def reconcile_calendar_slots(open_slots):
    """Make the slots table match the calendar's open times. Adds new open slots,
    removes calendar-sourced available slots that are no longer open; never touches
    claimed slots or manually-created ones."""
    c = db()
    existing = {(r["date"],r["start"],r["end"]): dict(r) for r in c.execute("SELECT * FROM slots WHERE source='calendar'")}
    want = {(s["date"],s["start"],s["end"]) for s in open_slots}
    added = removed = 0
    for k, r in existing.items():
        if k not in want and r["status"] == "available":
            c.execute("DELETE FROM slots WHERE id=?",(r["id"],)); removed += 1
    for s in open_slots:
        if (s["date"],s["start"],s["end"]) not in existing:
            c.execute("INSERT INTO slots(date,start,end,room,status,source) VALUES(?,?,?,'','available','calendar')",
                      (s["date"],s["start"],s["end"])); added += 1
    c.commit(); c.close()
    return {"added": added, "removed": removed, "open": len(open_slots)}

def sync_calendar():
    cfg = gcal.load_gcal_config()
    if not gcal.configured(cfg): return None
    slots = gcal.sync_slots(cfg)
    if slots is None: return None
    return reconcile_calendar_slots(slots)

def scheduler_loop():
    while True:
        try: run_scheduler()
        except Exception as e: print("[scheduler] error:", e)
        try:
            r = sync_calendar()
            if r: print("[gcal] sync", r)
        except Exception as e: print("[gcal] sync error:", e)
        time.sleep(3600)   # check hourly; day-based rules fire once per class

# ------------------------------------------------------------- handler ----
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # quiet

    # -- helpers --
    def current_user(self):
        ck = http.cookies.SimpleCookie(self.headers.get("Cookie",""))
        tok = ck["gibby_session"].value if "gibby_session" in ck else None
        if not tok: return None
        c = db()
        row = c.execute("""SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id
                           WHERE s.token=?""",(tok,)).fetchone()
        c.close()
        return dict(row) if row else None

    def send_json(self, obj, code=200, cookie=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body)))
        if cookie: self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length","0") or 0)
        if not n: return {}
        try: return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception: return {}

    def require(self, role=None):
        u = self.current_user()
        if not u:
            self.send_json({"error":"not signed in"},401); return None
        if role=="instructor" and u["role"] in ("instructor","admin"):
            return u   # admins can also act as instructors (submit their own classes)
        if role and u["role"]!=role:
            self.send_json({"error":"forbidden"},403); return None
        return u

    # -- routing --
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/api/"): return self.api_get(p)
        return self.static(p)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p.startswith("/api/"): return self.api_post(p)
        self.send_error(404)

    # -- static files --
    def static(self, p):
        if p in ("/",""): p = "/index.html"
        path = os.path.normpath(os.path.join(WEB, p.lstrip("/")))
        if not path.startswith(WEB) or not os.path.isfile(path):
            path = os.path.join(WEB, "index.html")  # SPA fallback
        ctype = {"html":"text/html","js":"application/javascript","css":"text/css",
                 "png":"image/png","gif":"image/gif","jpg":"image/jpeg","jpeg":"image/jpeg",
                 "svg":"image/svg+xml","webp":"image/webp","ico":"image/x-icon"}.get(path.rsplit(".",1)[-1],"application/octet-stream")
        with open(path,"rb") as f: data = f.read()
        self.send_response(200)
        is_text = ctype.startswith("text/") or "javascript" in ctype or "svg" in ctype
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if is_text else ""))
        self.send_header("Content-Length",str(len(data)))
        self.end_headers(); self.wfile.write(data)

    # -- GET api --
    def api_get(self, p):
        if p == "/api/version":
            return self.send_json({"version": VERSION})
        if p == "/api/me":
            u = self.current_user()
            if not u: return self.send_json({"user": None})
            return self.send_json({"user": {"id":u["id"],"name":u["name"],"email":u["email"],
                "role":u["role"],"must_change_pw":u.get("must_change_pw",0)}})
        if p == "/api/slots":
            u = self.require()
            if not u: return
            c = db(); rows=[dict(r) for r in c.execute("SELECT * FROM slots WHERE status='available' ORDER BY id").fetchall()]; c.close()
            return self.send_json({"slots":rows})
        if p in ("/api/templates","/api/templates/all"):
            u = self.require()
            if not u: return
            where = "" if (p.endswith("/all") and u["role"]=="admin") else "WHERE archived=0"
            c = db(); rows=[dict(r) for r in c.execute(f"SELECT * FROM templates {where} ORDER BY category,title").fetchall()]; c.close()
            for r in rows: r["supplies"]=json.loads(r["supplies"] or "[]")
            return self.send_json({"templates":rows})
        if p == "/api/classes/pending":
            u = self.require("admin");
            if not u: return
            return self.send_json({"classes": self._classes("WHERE c.status='pending' ")})
        if p == "/api/classes/mine":
            u = self.require("instructor")
            if not u: return
            return self.send_json({"classes": self._classes("WHERE c.instructor_id=? ", (u["id"],))})
        if p == "/api/classes/all":
            u = self.require("admin")
            if not u: return
            return self.send_json({"classes": self._classes("")})
        if p == "/api/audit":   # read-only; filter by ?class_id= or ?actor_id=
            u = self.require("admin")
            if not u: return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            where, args = "", []
            if q.get("class_id"):
                where, args = "WHERE a.class_id=?", [int(q["class_id"][0])]
            elif q.get("actor_id"):
                where, args = "WHERE a.actor_id=?", [int(q["actor_id"][0])]
            c = db()
            rows = [dict(r) for r in c.execute(f"""
                SELECT a.id, a.class_id, a.prev_status, a.new_status, a.actor_id, a.ts, a.snapshot,
                       c.title AS class_title, u.name AS actor_name, u.email AS actor_email
                FROM audit_log a
                LEFT JOIN classes c ON c.id = a.class_id
                LEFT JOIN users   u ON u.id = a.actor_id
                {where} ORDER BY a.id DESC LIMIT 500""", args).fetchall()]
            c.close()
            return self.send_json({"entries": rows})
        if p == "/api/dashboard":
            u = self.require("admin")
            if not u: return
            approved = self._classes("WHERE c.status='approved' ")
            return self.send_json({
                "pending": self._classes("WHERE c.status='pending' "),
                "returned": self._classes("WHERE c.status='incomplete' "),
                "low": [c for c in approved if not c.get("promoted") and c["enrolled"] < (c["min_p"] or 0)],
            })
        if p == "/api/integrations":
            u = self.require("admin")
            if not u: return
            c=db(); rows=[dict(r) for r in c.execute("SELECT * FROM integrations").fetchall()]; c.close()
            cfg = integrations.load_config(); conf = integrations.configured(cfg)
            for r in rows: r["configured"] = conf.get(r["id"], False)
            return self.send_json({"integrations": rows, "live": bool(cfg["live"])})
        if p.startswith("/api/class/") and p.endswith("/registrations"):
            u = self.current_user()
            if not u: return self.send_json({"error":"not signed in"},401)
            cid = p.split("/")[3]
            c = db()
            row = c.execute("SELECT * FROM classes WHERE id=?",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            if u["role"]=="instructor" and row["instructor_id"]!=u["id"]:
                c.close(); return self.send_json({"error":"forbidden"},403)
            regs=[dict(r) for r in c.execute("SELECT name,email,phone,refunded FROM registrations WHERE class_id=? ORDER BY id",(cid,)).fetchall()]
            c.close()
            return self.send_json({"registrations":regs})
        self.send_json({"error":"not found"},404)

    def _classes(self, where, args=()):
        c = db()
        rows = c.execute(f"""SELECT c.*, u.name AS instructor_name FROM classes c
                             JOIN users u ON u.id=c.instructor_id {where} ORDER BY c.created DESC""", args).fetchall()
        out=[]
        for r in rows:
            d=dict(r); d["supplies"]=json.loads(d["supplies"] or "[]"); d["external_ids"]=json.loads(d["external_ids"] or "{}")
            d["alcohol"]=bool(d["alcohol"]); d["promoted"]=bool(d.get("promoted"))
            d["enrolled"]=enrollment(c, d["id"])
            out.append(d)
        c.close()
        return out

    # -- POST api --
    def api_post(self, p):
        if p == "/api/login":
            b = self.read_json()
            c = db(); row = c.execute("SELECT * FROM users WHERE email=?",(b.get("email","").strip().lower(),)).fetchone()
            if not row:
                c.close(); return self.send_json({"error":"No account for that email."},401)
            h,_ = hash_pw(b.get("password",""), row["pw_salt"])
            if h != row["pw_hash"]:
                c.close(); return self.send_json({"error":"Wrong password."},401)
            tok = secrets.token_hex(24)
            c.execute("INSERT INTO sessions(token,user_id,created) VALUES(?,?,?)",(tok,row["id"],now()))
            c.commit(); c.close()
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto","") == "https" else ""
            ck = f"gibby_session={tok}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800{secure}"
            return self.send_json({"user":{"name":row["name"],"role":row["role"],"email":row["email"]}}, cookie=ck)
        if p == "/api/logout":
            ck = http.cookies.SimpleCookie(self.headers.get("Cookie",""))
            if "gibby_session" in ck:
                c=db(); c.execute("DELETE FROM sessions WHERE token=?",(ck["gibby_session"].value,)); c.commit(); c.close()
            return self.send_json({"ok":True}, cookie="gibby_session=; Path=/; Max-Age=0")
        if p == "/api/run-scheduler":
            u = self.require("admin")
            if not u: return
            b = self.read_json(); asof = None
            if b.get("asof"):
                try: asof = datetime.date.fromisoformat(b["asof"])
                except Exception: pass
            return self.send_json({"ok": True, "actions": run_scheduler(asof)})
        if p == "/api/users":  # admin create/update a user (idempotent by email)
            u = self.require("admin")
            if not u: return
            b = self.read_json()
            email = (b.get("email","") or "").strip().lower()
            name  = (b.get("name","") or "").strip() or email
            role  = b.get("role","instructor")
            pw    = b.get("password","")
            if not (email and pw):
                return self.send_json({"error":"email and password required"},400)
            mc = 1 if b.get("must_change_pw", True) else 0
            h, s = hash_pw(pw); c = db()
            if c.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone():
                c.execute("UPDATE users SET name=?, role=?, pw_hash=?, pw_salt=?, must_change_pw=? WHERE email=?",(name,role,h,s,mc,email))
                action = "updated"
            else:
                c.execute("INSERT INTO users(name,email,role,pw_hash,pw_salt,must_change_pw) VALUES(?,?,?,?,?,?)",(name,email,role,h,s,mc))
                action = "created"
            c.commit(); c.close()
            return self.send_json({"ok":True,"action":action,"email":email,"role":role})
        if p == "/api/change-password":
            u = self.current_user()
            if not u: return self.send_json({"error":"not signed in"},401)
            b = self.read_json(); npw = b.get("new_password",""); name = (b.get("name") or "").strip()
            if len(npw) < 8: return self.send_json({"error":"Use at least 8 characters."},400)
            h, s = hash_pw(npw); c = db()
            if name:
                c.execute("UPDATE users SET pw_hash=?, pw_salt=?, must_change_pw=0, name=? WHERE id=?",(h,s,name,u["id"]))
            else:
                c.execute("UPDATE users SET pw_hash=?, pw_salt=?, must_change_pw=0 WHERE id=?",(h,s,u["id"]))
            c.commit(); c.close()
            return self.send_json({"ok":True})
        if p == "/api/profile":
            u = self.current_user()
            if not u: return self.send_json({"error":"not signed in"},401)
            name = (self.read_json().get("name") or "").strip()
            if not name: return self.send_json({"error":"Name is required."},400)
            c = db(); c.execute("UPDATE users SET name=? WHERE id=?",(name,u["id"])); c.commit(); c.close()
            return self.send_json({"ok":True})
        if p == "/api/forgot":
            email = (self.read_json().get("email","") or "").strip().lower()
            c = db(); row = c.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone()
            if row:
                tok = secrets.token_urlsafe(24)
                exp = (datetime.datetime.now()+datetime.timedelta(hours=1)).isoformat()
                c.execute("INSERT INTO password_resets(token,user_id,expires) VALUES(?,?,?)",(tok,row["id"],exp))
                c.commit()
                proto = self.headers.get("X-Forwarded-Proto","http"); host = self.headers.get("Host","localhost:8000")
                link = f"{proto}://{host}/?reset={tok}"
                mailer.send(email, "Reset your Gibby Class Manager password",
                    f"We received a request to reset your password. Click the link below (valid for 1 hour):\n\n{link}\n\n"
                    f"If you did not request this, you can ignore this email.")
            c.close()
            return self.send_json({"ok":True})   # always success, so we never reveal which emails exist
        if p == "/api/reset":
            b = self.read_json(); tok = b.get("token",""); npw = b.get("new_password","")
            if len(npw) < 8: return self.send_json({"error":"Use at least 8 characters."},400)
            c = db(); row = c.execute("SELECT * FROM password_resets WHERE token=?",(tok,)).fetchone()
            if not row or row["expires"] < datetime.datetime.now().isoformat():
                c.close(); return self.send_json({"error":"This reset link is invalid or has expired."},400)
            h, s = hash_pw(npw)
            c.execute("UPDATE users SET pw_hash=?, pw_salt=?, must_change_pw=0 WHERE id=?",(h,s,row["user_id"]))
            c.execute("DELETE FROM password_resets WHERE token=?",(tok,)); c.commit(); c.close()
            return self.send_json({"ok":True})
        if p == "/api/test-email":
            u = self.require("admin")
            if not u: return
            to = (self.read_json().get("to") or u["email"]).strip()
            cfg = mailer.load_email_config()
            delivered = mailer.send(to, "Gibby Class Manager test email",
                "This is a test from your Gibby Class Manager. If you received this, email is working.", cfg)
            return self.send_json({"ok":True, "to":to, "from":cfg["mail_from"],
                "live": bool(cfg["email_live"] and cfg["smtp_host"]), "delivered": bool(delivered)})
        if p == "/api/test-eventbrite":
            u = self.require("admin")
            if not u: return
            return self.send_json(integrations.eventbrite_orgs(integrations.load_config()))
        if p == "/api/sync-calendar":
            u = self.require("admin")
            if not u: return
            if not gcal.configured(gcal.load_gcal_config()):
                return self.send_json({"ok":False,"error":"Google Calendar isn't connected yet."})
            r = sync_calendar()
            if r is None:
                return self.send_json({"ok":False,"error":"Could not read the calendar. Check the service account key and that the calendar is shared with it."})
            return self.send_json({"ok":True, **r})
        if p == "/api/slots":  # admin create
            u = self.require("admin");
            if not u: return
            b = self.read_json()
            c=db(); c.execute("INSERT INTO slots(date,start,end,room) VALUES(?,?,?,?)",
                (b.get("date"),b.get("start"),b.get("end"),b.get("room","Large Room")))
            c.commit(); c.close(); return self.send_json({"ok":True})
        if p == "/api/classes":  # instructor submit
            u = self.require("instructor")
            if not u: return
            b = self.read_json()
            req = ["title","description","age_range","length"]
            miss=[k for k in req if not str(b.get(k,"")).strip()]
            if not b.get("photo"): miss.append("photo")
            if miss: return self.send_json({"error":"Missing required fields","fields":miss},400)
            c=db()
            ids = b.get("slot_ids") or ([b["slot_id"]] if b.get("slot_id") else [])
            ids = [int(x) for x in ids]
            slot_date, slot_time, room = b.get("slot_date"), b.get("slot_time"), b.get("room")
            if ids:
                ph = ",".join("?"*len(ids))
                rows = [dict(r) for r in c.execute(f"SELECT * FROM slots WHERE id IN ({ph})", ids).fetchall()]
                if len(rows) != len(ids):
                    c.close(); return self.send_json({"error":"One of those slots no longer exists."},400)
                if len({r["date"] for r in rows}) != 1 or len({r["room"] for r in rows}) != 1:
                    c.close(); return self.send_json({"error":"Slots must be the same day and same room."},400)
                rows.sort(key=lambda r: tmin(r["start"]))
                for a, nxt in zip(rows, rows[1:]):
                    if tmin(a["end"]) != tmin(nxt["start"]):
                        c.close(); return self.send_json({"error":"Slots must be back-to-back (consecutive)."},400)
                # RACE-SAFE: claim ALL selected slots atomically; if any got taken
                # first, rowcount < N, we roll back (no commit) and reject.
                claimed = c.execute(f"UPDATE slots SET status='claimed' WHERE id IN ({ph}) AND status='available'", ids).rowcount
                if claimed != len(ids):
                    c.close(); return self.send_json({"error":"One of those slots was just claimed by someone else. Please reselect."},409)
                slot_date = rows[0]["date"]
                room = b.get("room") or rows[0]["room"]  # calendar slots are roomless; take room from the form
                slot_time = rows[0]["start"] + " – " + rows[-1]["end"]
            c.execute("""INSERT INTO classes(title,instructor_id,slot_date,slot_time,room,description,age_range,
                alcohol,max_p,min_p,ticket_price,instructor_pay,supplies,headline,subtitle,photo,
                length,pre_class,own_materials,material_cost,needs_volunteer,slot_ids,links,status,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?, 'pending', ?)""",
                (b.get("title"),u["id"],slot_date,slot_time,room,
                 b.get("description"),b.get("age_range"),1 if b.get("alcohol") else 0,
                 b.get("max_p"),b.get("min_p"),b.get("ticket_price"),b.get("instructor_pay"),
                 json.dumps(b.get("supplies",[])),b.get("headline",""),b.get("subtitle",""),b.get("photo"),
                 b.get("length",""),b.get("pre_class",""),1 if b.get("own_materials") else 0,
                 b.get("material_cost"),1 if b.get("needs_volunteer") else 0, json.dumps(ids), b.get("links",""), now()))
            audit(c, c.execute("SELECT last_insert_rowid()").fetchone()[0], None, "pending", u["id"])
            admins = emails_for(c, "WHERE role='admin'")
            c.commit(); c.close()
            mailer.send(admins, "New class submission",
                f"{u['name']} submitted \"{b.get('title')}\" for {b.get('slot_date','')} {b.get('slot_time','')}. Review it in the app.")
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and p.endswith("/approve"):
            return self.decide(p, approve=True)
        if p.startswith("/api/classes/") and p.endswith("/incomplete"):
            return self.decide(p, approve=False)
        if p.startswith("/api/classes/") and p.endswith("/promote"):
            u = self.require("admin")
            if not u: return
            cid = p.split("/")[3]; c=db()
            c.execute("UPDATE classes SET promoted=1 WHERE id=?",(cid,)); c.commit(); c.close()
            print(f"[promote] class #{cid} -> would re-share on Eventbrite/Facebook/Wix + send reminder")
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and p.endswith("/cancel"):
            u = self.require("admin")
            if not u: return
            cid = p.split("/")[3]; c=db()
            cls = dict(c.execute("SELECT * FROM classes WHERE id=?",(cid,)).fetchone())
            students = [r[0] for r in c.execute("SELECT email FROM registrations WHERE class_id=?",(cid,)).fetchall()]
            instr = c.execute("SELECT email FROM users WHERE id=?",(cls["instructor_id"],)).fetchone()
            c.execute("UPDATE classes SET status='cancelled' WHERE id=?",(cid,))
            audit(c, cid, cls.get("status"), "cancelled", u["id"])
            c.execute("UPDATE registrations SET refunded=1 WHERE class_id=?",(cid,)); c.commit(); c.close()
            print(f"[cancel] class #{cid} -> would refund all via Eventbrite")
            subj, body = mailer.tmpl_cancel(cls)
            mailer.send(students, subj, body)
            if instr and instr[0]: mailer.send(instr[0], "Your class was cancelled", body)
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and (p.endswith("/remind") or p.endswith("/followup")):
            u = self.require("admin")
            if not u: return
            cid = p.split("/")[3]; kind = p.rsplit("/",1)[1]; c=db()
            cls = dict(c.execute("SELECT * FROM classes WHERE id=?",(cid,)).fetchone())
            students = [r[0] for r in c.execute("SELECT email FROM registrations WHERE class_id=? AND refunded=0",(cid,)).fetchall()]
            c.close()
            cfg = mailer.load_email_config()
            subj, body = (mailer.tmpl_reminder(cls, cfg) if kind=="remind" else mailer.tmpl_followup(cls, cfg))
            mailer.send(students, subj, body)
            return self.send_json({"ok":True, "sent":len(students)})
        if p.startswith("/api/classes/") and p.endswith("/edit"):   # admin edits then returns for instructor approval
            u = self.require("admin")
            if not u: return
            cid = p.split("/")[3]; b = self.read_json()
            c = db(); row = c.execute("SELECT * FROM classes WHERE id=?",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            sets, vals = [], []
            for k in ("title","description","age_range","headline","subtitle"):
                if k in b: sets.append(f"{k}=?"); vals.append(b[k])
            for k in ("max_p","min_p","ticket_price","instructor_pay"):
                if k in b: sets.append(f"{k}=?"); vals.append(b[k])
            if "alcohol" in b: sets.append("alcohol=?"); vals.append(1 if b["alcohol"] else 0)
            sets += ["status=?","admin_note=?"]; vals += ["instructor_review", b.get("note","")]
            vals.append(cid)
            c.execute(f"UPDATE classes SET {','.join(sets)} WHERE id=?", vals)
            audit(c, cid, row["status"], "instructor_review", u["id"])
            instr = c.execute("SELECT email,name FROM users WHERE id=?",(row["instructor_id"],)).fetchone()
            c.commit(); c.close()
            if instr:
                mailer.send(instr[0], f"Please review changes to: {b.get('title', row['title'])}",
                    f"Hi {instr[1].split()[0]},\n\nAn admin made some edits to your class \"{b.get('title', row['title'])}\" "
                    f"and needs your approval before it goes live.\n\n{('Note: '+b.get('note')) if b.get('note') else ''}\n\n"
                    f"Please log in, review the changes, and approve.\n\nThanks,\nThe Gibby")
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and p.endswith("/instructor-approve"):   # instructor accepts admin edits -> publish
            u = self.require("instructor")
            if not u: return
            cid = p.split("/")[3]; c = db()
            row = c.execute("SELECT * FROM classes WHERE id=?",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            cls = dict(row)
            if cls["instructor_id"] != u["id"] or cls["status"] != "instructor_review":
                c.close(); return self.send_json({"error":"not allowed"},403)
            approve_and_publish(c, cls, u["id"]); c.commit(); c.close()
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and p.endswith("/instructor-decline"):   # instructor rejects admin edits -> back to admin
            u = self.require("instructor")
            if not u: return
            cid = p.split("/")[3]; b = self.read_json(); c = db()
            row = c.execute("SELECT * FROM classes WHERE id=?",(cid,)).fetchone()
            if not row or row["instructor_id"] != u["id"]:
                c.close(); return self.send_json({"error":"not allowed"},403)
            c.execute("UPDATE classes SET status='pending', admin_note=? WHERE id=?",(b.get("note",""),cid))
            audit(c, cid, row["status"], "pending", u["id"])
            admins = emails_for(c, "WHERE role='admin'"); c.commit(); c.close()
            mailer.send(admins, f"Instructor requested changes: {row['title']}",
                f"{u['name']} did not accept the edits to \"{row['title']}\".\n\nTheir note: {b.get('note','(none)')}\n\nIt is back in the approvals queue.")
            return self.send_json({"ok":True})
        if p.startswith("/api/integrations/") and (p.endswith("/connect") or p.endswith("/disconnect")):
            u = self.require("admin")
            if not u: return
            iid = p.split("/")[3]; status = "connected" if p.endswith("/connect") else "disconnected"
            c=db(); c.execute("UPDATE integrations SET status=? WHERE id=?",(status,iid)); c.commit(); c.close()
            print(f"[integration] {iid} -> {status}")
            return self.send_json({"ok":True})
        if p == "/api/templates":
            u = self.require("admin")
            if not u: return
            b = self.read_json()
            c=db(); c.execute("INSERT INTO templates(title,category,age,description,supplies) VALUES(?,?,?,?,?)",
                (b.get("title","").strip() or "Untitled", b.get("category","Uncategorized"),
                 b.get("age","All Ages"), b.get("description",""), json.dumps(b.get("supplies",[]))))
            c.commit(); c.close(); return self.send_json({"ok":True})
        if p.startswith("/api/templates/") and p.endswith("/archive"):
            u = self.require("admin")
            if not u: return
            tid = p.split("/")[3]; c=db()
            c.execute("UPDATE templates SET archived = 1 - archived WHERE id=?",(tid,)); c.commit(); c.close()
            return self.send_json({"ok":True})
        if p.startswith("/api/templates/"):
            u = self.require("admin")
            if not u: return
            tid = p.split("/")[3]; b=self.read_json(); c=db()
            c.execute("UPDATE templates SET title=?,category=?,age=?,description=?,supplies=? WHERE id=?",
                (b.get("title"),b.get("category"),b.get("age"),b.get("description"),json.dumps(b.get("supplies",[])),tid))
            c.commit(); c.close(); return self.send_json({"ok":True})
        self.send_json({"error":"not found"},404)

    def decide(self, p, approve):
        u = self.require("admin")
        if not u: return
        cid = p.split("/")[3]
        b = self.read_json()
        c=db(); row=c.execute("SELECT * FROM classes WHERE id=?",(cid,)).fetchone()
        if not row: c.close(); return self.send_json({"error":"not found"},404)
        cls=dict(row)
        instr=dict(c.execute("SELECT * FROM users WHERE id=?",(cls["instructor_id"],)).fetchone())
        if approve:
            approve_and_publish(c, cls, u["id"])
        else:
            c.execute("UPDATE classes SET status='incomplete', admin_note=? WHERE id=?",(b.get("note",""),cid))
            audit(c, cid, cls.get("status"), "incomplete", u["id"])
            # release all claimed slots back to available
            sids = json.loads(cls.get("slot_ids") or "[]")
            if sids:
                c.execute(f"UPDATE slots SET status='available' WHERE id IN ({','.join('?'*len(sids))})", sids)
            subj, body = mailer.tmpl_incomplete(cls, instr, b.get("note","")); mailer.send(instr["email"], subj, body)
        c.commit(); c.close()
        return self.send_json({"ok":True})

def now(): return datetime.datetime.now().isoformat(timespec="seconds")

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()   # daily lifecycle automations
    print(f"Gibby Class Manager running:  http://localhost:{PORT}   (data: {DB})")
    print("Sign in as admin:      jess@theeverett.org")
    print("Sign in as instructor: christin.smiertka@theeverett.org  (first.last of any roster name)")
    if SEED_PW == "gibby123":
        print("\n  *** SECURITY: users are seeded with the default password 'gibby123'.")
        print("      Before exposing this on the internet, set SEED_PASSWORD to something strong")
        print("      (and delete gibby.db so it re-seeds), or change each account's password. ***\n")
    Threaded(("0.0.0.0", PORT), H).serve_forever()
