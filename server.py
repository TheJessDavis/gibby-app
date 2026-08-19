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
import http.server, socketserver, json, sqlite3, os, hashlib, secrets, urllib.parse, datetime, http.cookies, random, re
import integrations, mailer, gcal, threading, time

ROOT = os.path.dirname(os.path.abspath(__file__))

def _load_dotenv():
    """Read .env for local development. Real environment variables always win, so
    this never overrides what the host (Render) has set. Gitignored by design."""
    path = os.path.join(ROOT, ".env")
    if not os.path.isfile(path): return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and v and k not in os.environ:
                    os.environ[k] = v
        print("[config] loaded .env for local development")
    except Exception as e:
        print("[config] could not read .env:", e)

_load_dotenv()
DATA_DIR = os.environ.get("DATA_DIR", ROOT)   # point at a mounted volume in production so data persists
DB   = os.path.join(DATA_DIR, "gibby.db")
WEB  = os.path.join(ROOT, "web")
PORT = int(os.environ.get("PORT", "8000"))
# No weak default. If SEED_PASSWORD is not supplied we mint a random one for this
# run and print it once, so an unconfigured deploy can never ship accounts whose
# password is published in this repository.
SEED_PW = os.environ.get("SEED_PASSWORD") or ("gen-" + secrets.token_urlsafe(12))
SEED_PW_GENERATED = not os.environ.get("SEED_PASSWORD")
VERSION = "6.8-people-search"

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
    -- One row per (class, email type). The UNIQUE index is the real guarantee that
    -- an automated email cannot go out twice, even if two jobs race.
    CREATE TABLE IF NOT EXISTS email_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL,
      email_type TEXT NOT NULL, sent_at TEXT NOT NULL,
      recipients INTEGER DEFAULT 0, delivered INTEGER DEFAULT 0);
    CREATE UNIQUE INDEX IF NOT EXISTS email_log_once ON email_log(class_id, email_type);
    -- What the instructor thought, asked the day after the class. Private to The
    -- Gibby: it never goes to students. One row per class.
    CREATE TABLE IF NOT EXISTS class_feedback(
      id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL UNIQUE,
      instructor_id INTEGER, enrollment TEXT, materials TEXT, teach_again TEXT,
      notes TEXT, submitted_at TEXT);
    CREATE INDEX IF NOT EXISTS class_feedback_instructor ON class_feedback(instructor_id);
    -- Work-in-progress class proposals. A draft holds the FORM ONLY: it does not
    -- claim any slots, otherwise an abandoned draft would sit on a Saturday nobody
    -- else could book. Slots are still claimed at submit time.
    CREATE TABLE IF NOT EXISTS drafts(
      id INTEGER PRIMARY KEY AUTOINCREMENT, instructor_id INTEGER NOT NULL,
      title TEXT, payload TEXT, slot_ids TEXT, slot_date TEXT, slot_time TEXT, room TEXT,
      is_series INTEGER DEFAULT 0, session_count INTEGER DEFAULT 1,
      created TEXT, updated TEXT, deleted_at TEXT);
    CREATE INDEX IF NOT EXISTS drafts_instructor ON drafts(instructor_id, deleted_at);
    -- Sliding-window rate limiting. One row per accepted request; rows outside the
    -- window are pruned on read. Transient bookkeeping, so these ARE hard deleted
    -- (unlike slots/classes/users, which soft delete).
    CREATE TABLE IF NOT EXISTS rate_limit(
      id INTEGER PRIMARY KEY AUTOINCREMENT, bucket TEXT NOT NULL, ts REAL NOT NULL);
    CREATE INDEX IF NOT EXISTS rate_limit_bucket ON rate_limit(bucket, ts);
    -- Durable queue for outbound platform calls. A publish never fails in front of
    -- the admin: the job is queued, retried with backoff, and only flagged after it
    -- has genuinely given up. Survives restarts because it lives in the database.
    CREATE TABLE IF NOT EXISTS job_queue(
      id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL,
      platform TEXT NOT NULL, payload TEXT DEFAULT '{}',
      attempts INTEGER DEFAULT 0, next_run_at TEXT,
      status TEXT DEFAULT 'queued',      -- queued | running | done | skipped | failed
      last_error TEXT DEFAULT '', created TEXT, updated TEXT);
    CREATE INDEX IF NOT EXISTS job_queue_due ON job_queue(status, next_run_at);
    -- Browser-side render/action failures reported by the UI error boundaries.
    CREATE TABLE IF NOT EXISTS client_errors(
      id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, section TEXT, message TEXT,
      stack TEXT, path TEXT, email TEXT, role TEXT, agent TEXT);
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
    try: c.execute("ALTER TABLE users ADD COLUMN photo TEXT")
    except sqlite3.OperationalError: pass
    try: c.execute("ALTER TABLE users ADD COLUMN skills TEXT")
    except sqlite3.OperationalError: pass
    try: c.execute("ALTER TABLE users ADD COLUMN address TEXT")
    except sqlite3.OperationalError: pass
    try: c.execute("ALTER TABLE users ADD COLUMN must_change_pw INTEGER DEFAULT 0")
    except sqlite3.OperationalError: pass
    try: c.execute("ALTER TABLE slots ADD COLUMN source TEXT DEFAULT 'manual'")
    except sqlite3.OperationalError: pass
    for col in ("promoted","reminded","followed_up","low_alerted"):
        try: c.execute(f"ALTER TABLE classes ADD COLUMN {col} INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
    for col, typ in (("length","TEXT"),("pre_class","TEXT"),("own_materials","INTEGER DEFAULT 0"),
                     ("material_cost","REAL"),("needs_volunteer","INTEGER DEFAULT 0"),("waives_pay","INTEGER DEFAULT 0"),("slot_ids","TEXT"),
                     ("contract_status","TEXT"),("contract_text","TEXT"),("contract_name","TEXT"),
                     ("contract_address","TEXT"),("contract_signed_at","TEXT"),("contract_signature","TEXT"),
                     ("links","TEXT"),("reviewing_admin_id","INTEGER"),("review_started_at","TEXT"),
                     ("is_series","INTEGER DEFAULT 0"),("session_count","INTEGER DEFAULT 1"),
                     ("session_dates","TEXT"),("age_label","TEXT"),
                     ("close_days","INTEGER DEFAULT 0"),("poster","TEXT"),
                     ("pay_model","TEXT DEFAULT 'flat'"),("class_time","TEXT"),
                     ("headcount_sent","INTEGER DEFAULT 0"),
                     ("followup_note","TEXT"),("followup_status","TEXT"),
                     ("followup_requested_at","TEXT"),("followup_submitted_at","TEXT"),
                     ("publishing_in_progress","INTEGER DEFAULT 0")):
        try: c.execute(f"ALTER TABLE classes ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError: pass
    # Soft delete: nothing is ever removed from these three tables. A row with
    # deleted_at set is invisible to normal queries but recoverable from Archive.
    try: c.execute("ALTER TABLE registrations ADD COLUMN external_id TEXT")
    except sqlite3.OperationalError: pass
    try: c.execute("ALTER TABLE registrations ADD COLUMN checked_in INTEGER DEFAULT 0")
    except sqlite3.OperationalError: pass
    c.execute("""CREATE UNIQUE INDEX IF NOT EXISTS registrations_ext
                 ON registrations(class_id, external_id) WHERE external_id IS NOT NULL""")
    for tbl in ("slots", "classes", "users"):
        try: c.execute(f"ALTER TABLE {tbl} ADD COLUMN deleted_at TEXT")
        except sqlite3.OperationalError: pass
        c.execute(f"CREATE INDEX IF NOT EXISTS {tbl}_deleted_at ON {tbl}(deleted_at)")
    # Session lifetime + rotating refresh tokens.
    for col, typ in (("expires_at","TEXT"), ("refresh_token","TEXT"),
                     ("refresh_expires_at","TEXT"), ("revoked_at","TEXT"),
                     ("rotated_from","TEXT"), ("revoked_reason","TEXT"), ("csrf_token","TEXT")):
        try: c.execute(f"ALTER TABLE sessions ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError: pass
    c.execute("CREATE INDEX IF NOT EXISTS sessions_refresh ON sessions(refresh_token)")
    c.execute("CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id)")
    # ---- query indexes -------------------------------------------------------
    # Derived by running EXPLAIN QUERY PLAN over every query the app issues and
    # indexing what actually scanned. Composites lead with the selective column:
    # deleted_at alone is a poor index because nearly every row is NULL, so the
    # planner was picking it and still reading most of the table.
    for ddl in (
        # classes: filtered by status or instructor, ordered by created
        "CREATE INDEX IF NOT EXISTS classes_status      ON classes(status, deleted_at)",
        "CREATE INDEX IF NOT EXISTS classes_instructor  ON classes(instructor_id, deleted_at)",
        "CREATE INDEX IF NOT EXISTS classes_slot_date   ON classes(slot_date)",
        "CREATE INDEX IF NOT EXISTS classes_created     ON classes(created DESC)",
        # registrations: rosters and every enrolment count
        "CREATE INDEX IF NOT EXISTS registrations_class ON registrations(class_id, refunded)",
        # slots: availability, the series week-matcher, and calendar reconcile
        "CREATE INDEX IF NOT EXISTS slots_status        ON slots(status, deleted_at)",
        "CREATE INDEX IF NOT EXISTS slots_date          ON slots(date, start, end)",
        "CREATE INDEX IF NOT EXISTS slots_source        ON slots(source, status)",
        # audit log: the Audit tab filters, and decided_by() on every lost race
        "CREATE INDEX IF NOT EXISTS audit_log_class     ON audit_log(class_id, id DESC)",
        "CREATE INDEX IF NOT EXISTS audit_log_actor     ON audit_log(actor_id, id DESC)",
        # per-class job lookups (publish status, duplicate-publish guard)
        "CREATE INDEX IF NOT EXISTS job_queue_class     ON job_queue(class_id)",
        # lifecycle email recipients are selected by role
        "CREATE INDEX IF NOT EXISTS users_role          ON users(role, deleted_at)",
    ):
        try: c.execute(ddl)
        except sqlite3.OperationalError as e: print("[index]", e)
    # Any session created before expiry existed has no expires_at and would other-
    # wise live forever. Retire them so everyone re-authenticates once.
    c.execute("UPDATE sessions SET revoked_at=? WHERE expires_at IS NULL AND revoked_at IS NULL", (now(),))
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

def sync_registrations(class_id, _req_fn=None):
    """Pull the real roster from Eventbrite (all pages) and reconcile it into the
    registrations table. Idempotent: attendees are keyed on their Eventbrite id, so
    running this repeatedly updates rather than duplicates. Returns a summary, or
    None when there is nothing to sync."""
    c = db()
    row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL", (class_id,)).fetchone()
    if not row: c.close(); return None
    cls = dict(row)
    cls["external_ids"] = json.loads(cls.get("external_ids") or "{}")
    c.close()

    people = integrations.sync_attendees(cls, _req_fn=_req_fn)
    if people is None: return None

    c = db()
    added = updated = 0
    for a in people:
        ext = a["external_id"]
        existing = c.execute("SELECT id FROM registrations WHERE class_id=? AND external_id=?",
                             (class_id, ext)).fetchone() if ext else None
        if existing:
            c.execute("""UPDATE registrations SET name=?,email=?,phone=?,refunded=?,checked_in=? WHERE id=?""",
                      (a["name"], a["email"], a["phone"], 1 if a["refunded"] else 0,
                       1 if a.get("checked_in") else 0, existing["id"]))
            updated += 1
        else:
            c.execute("""INSERT INTO registrations(class_id,name,email,phone,refunded,checked_in,external_id,created)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (class_id, a["name"], a["email"], a["phone"],
                       1 if a["refunded"] else 0, 1 if a.get("checked_in") else 0, ext or None, now()))
            added += 1
    total = c.execute("SELECT COUNT(*) FROM registrations WHERE class_id=? AND refunded=0",
                      (class_id,)).fetchone()[0]
    c.commit(); c.close()
    print(f"[registrations] class #{class_id}: {added} added, {updated} updated, {total} attending")
    return {"added": added, "updated": updated, "attending": total, "fetched": len(people)}

def money_str(n):
    return f"${float(n or 0):,.2f}".replace(".00", "")

def class_finance(cls, enrolled):
    """What a class actually made.

    The pricing model: ticket = materials + (target pay / 0.6) / planned. Revenue
    minus materials is the pool; the instructor takes 60% of it and The Gibby 40%.
    If the INSTRUCTOR supplied the materials they are reimbursed, and that
    reimbursement is already inside the agreed instructor_pay - so The Gibby must
    not also be charged for those materials, or every such class reads as far less
    profitable than it was.

    pay_model decides which number is real, because The Gibby runs both deals:
      'flat'  - the agreed fee is paid regardless of attendance
      'split' - the instructor takes 60% of the pool at ACTUAL attendance
    actual_pay is the authoritative figure either way; formula_pay is kept so a
    flat-fee class still shows what the split would have given."""
    ticket  = float(cls.get("ticket_price") or 0)
    mat     = float(cls.get("material_cost") or 0)
    pay     = float(cls.get("instructor_pay") or 0)
    own     = bool(cls.get("own_materials"))
    planned = int(cls.get("max_p") or 0)
    model = (cls.get("pay_model") or "flat").strip() or "flat"
    revenue   = ticket * enrolled
    materials = mat * enrolled
    gibby_materials = 0.0 if own else materials      # see docstring
    pool = max(0.0, revenue - materials)
    formula = pool * 0.6 + (materials if own else 0.0)
    actual_pay = formula if model == "split" else pay
    net = revenue - actual_pay - gibby_materials
    return {
        "planned": planned, "enrolled": enrolled,
        "fill": (enrolled / planned) if planned else 0,
        "ticket_price": ticket, "revenue": round(revenue, 2),
        "pay_model": model,
        "instructor_pay": round(actual_pay, 2),
        "agreed_pay": round(pay, 2),
        "formula_pay": round(formula, 2),
        "materials": round(materials, 2),
        "materials_paid_by": "instructor (reimbursed)" if own else "The Gibby",
        "gibby_materials": round(gibby_materials, 2),
        "net": round(net, 2),
        "margin": (net / revenue) if revenue else 0,
    }

def followup_audience(c, class_id):
    """Who should get the after-class note, and do we actually know who attended?

    Decided per class from the data rather than from a setting, because The Gibby
    may scan tickets for one class and not the next:

      * If ANY registration is checked in, the door was scanned, so the check-in
        data is trustworthy. Write to those people only, and it is safe to say
        "thanks for coming".
      * If NOBODY is checked in, that means tickets were never scanned, not that
        nobody turned up. Write to everyone holding a ticket, but the copy must
        not assume they were there.

    Returns (emails, attendance_known)."""
    rows = [dict(r) for r in c.execute(
        "SELECT email, checked_in FROM registrations WHERE class_id=? AND refunded=0", (class_id,))]
    attended = [r["email"] for r in rows if r["checked_in"]]
    if attended:
        return attended, True
    return [r["email"] for r in rows if r["email"]], False

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
            # seeded accounts must set their own password at first sign-in
            c.execute("INSERT INTO users(name,email,role,pw_hash,pw_salt,must_change_pw) VALUES(?,?,?,?,?,1)",
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

# Outbound posting now runs through the retry queue below (queue_publish), which
# calls integrations.py one platform at a time so each can fail and retry alone.

# ---------------------------------------------------------------- sessions ----
# These are opaque, database-backed session tokens, NOT JWTs. That matters: every
# request looks the token up, so revoking a row takes effect on the very next
# request. There is no window during which a logged-out token still works, and no
# need for a separate blocklist to compensate for un-revocable tokens - revoked_at
# on the row IS the blocklist, and it doubles as an audit trail.
ACCESS_TTL  = 8 * 3600            # access token: 8 hours
REFRESH_TTL = 30 * 24 * 3600      # refresh token: 30 days, single use

def _ts(seconds_ahead=0):
    return (datetime.datetime.now() + datetime.timedelta(seconds=seconds_ahead)).isoformat(timespec="seconds")

def new_session(c, user_id, rotated_from=None):
    """Issue a fresh access + refresh pair plus a CSRF token bound to the session.
    Returns (access, refresh, csrf)."""
    access, refresh = secrets.token_hex(24), secrets.token_hex(32)
    csrf = secrets.token_urlsafe(32)
    c.execute("""INSERT INTO sessions(token,user_id,created,expires_at,refresh_token,
                                      refresh_expires_at,rotated_from,csrf_token)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (access, user_id, now(), _ts(ACCESS_TTL), refresh, _ts(REFRESH_TTL), rotated_from, csrf))
    return access, refresh, csrf

def session_cookies(access, refresh, secure=""):
    return [f"gibby_session={access}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ACCESS_TTL}{secure}",
            f"gibby_refresh={refresh}; Path=/; HttpOnly; SameSite=Lax; Max-Age={REFRESH_TTL}{secure}"]

CLEAR_COOKIES = ["gibby_session=; Path=/; Max-Age=0", "gibby_refresh=; Path=/; Max-Age=0"]

# Synchronizer-token CSRF. The token is generated per session and held server-side
# on the session row; the client echoes it in X-CSRF-Token on every state change.
#
# Two notes on the shape of this app:
#   * There are no HTML <form> elements - it is a SPA that sends JSON via fetch -
#     so there is nowhere to put a hidden field. The header is the mechanism.
#   * The token is deliberately NOT a cookie. It is handed to the page in an API
#     response body and kept in memory, so it cannot ride along automatically the
#     way a cookie would, which is the whole point.
#
# These routes are exempt because they run before a session exists (or authenticate
# by another means entirely):
CSRF_EXEMPT = {
    "/api/login",         # no session yet
    "/api/refresh",       # authenticated by the refresh cookie; issues the next CSRF token
    "/api/forgot",        # anonymous
    "/api/reset",         # authenticated by the emailed reset token
    "/api/client-error",  # error boundaries must be able to report before sign-in
}

def session_csrf(token):
    if not token: return None
    c = db()
    r = c.execute("""SELECT csrf_token FROM sessions
                     WHERE token=? AND revoked_at IS NULL AND expires_at > ?""",
                  (token, now())).fetchone()
    c.close()
    return r["csrf_token"] if r else None

def revoke_session(c, token=None, refresh=None, reason="revoked"):
    """reason matters: a token replayed after ROTATION is a theft signal, while one
    replayed after a normal logout is just a stale tab."""
    if token:   c.execute("UPDATE sessions SET revoked_at=?, revoked_reason=? WHERE token=? AND revoked_at IS NULL", (now(), reason, token))
    if refresh: c.execute("UPDATE sessions SET revoked_at=?, revoked_reason=? WHERE refresh_token=? AND revoked_at IS NULL", (now(), reason, refresh))

def revoke_all_for_user(c, user_id, why="", reason="revoked"):
    n = c.execute("UPDATE sessions SET revoked_at=?, revoked_reason=? WHERE user_id=? AND revoked_at IS NULL",
                  (now(), reason, user_id)).rowcount
    if n: print(f"[session] revoked {n} session(s) for user {user_id} {why}")
    return n

def prune_sessions():
    """Drop rows well past any possible use. Transient auth state, so a hard delete
    is right here (same reasoning as the rate-limit table)."""
    try:
        c = db()
        n = c.execute("DELETE FROM sessions WHERE refresh_expires_at IS NOT NULL AND refresh_expires_at < ?",
                      (_ts(-7 * 24 * 3600),)).rowcount
        c.commit(); c.close()
        if n: print(f"[session] pruned {n} long-expired session row(s)")
    except Exception as e:
        print("[session] prune failed:", e)

# ------------------------------------------------------------ rate limiting ----
# A true sliding window: every accepted request is logged with its timestamp and a
# caller is allowed through only if fewer than `limit` of their own requests fall
# inside the trailing window. That avoids the burst-at-the-boundary problem you get
# with fixed windows (10 at 10:59 plus 10 at 11:01).
#
# There is no Redis or cache layer in this app, so state lives in SQLite alongside
# everything else: it survives the restart that happens on every deploy, which an
# in-process dict would not.
RATE_LIMITS = {                 # name: (max requests, window seconds)
    "submit":  (5,  3600),      # class form submissions, per instructor
    "approve": (20, 3600),      # approval decisions, per admin
    "claim":   (10, 3600),      # slot claim attempts, per instructor
}
RATE_LABEL = {"submit": "class submissions", "approve": "approval decisions",
              "claim": "slot claims"}

def rate_check(name, user_id, count=True):
    """Returns (allowed, retry_after_seconds, remaining). Fails OPEN: if the limiter
    itself errors, the request is allowed rather than blocking real work."""
    limit, window = RATE_LIMITS[name]
    bucket = f"{name}:{user_id}"
    t = time.time()
    try:
        c = db()
        c.execute("DELETE FROM rate_limit WHERE bucket=? AND ts<?", (bucket, t - window))
        hits = [r[0] for r in c.execute(
            "SELECT ts FROM rate_limit WHERE bucket=? ORDER BY ts", (bucket,)).fetchall()]
        if len(hits) >= limit:
            retry_after = max(1, int(window - (t - hits[0])) + 1)
            c.commit(); c.close()
            print(f"[rate limit] {bucket}: {len(hits)}/{limit} in window, retry after {retry_after}s")
            return False, retry_after, 0
        if count:
            c.execute("INSERT INTO rate_limit(bucket,ts) VALUES(?,?)", (bucket, t))
        c.commit(); c.close()
        return True, 0, limit - len(hits) - (1 if count else 0)
    except Exception as e:
        print("[rate limit] check failed, allowing request:", e)
        return True, 0, limit

def _age_bounds(label):
    """'Ages 8–10' -> (8,10); 'Ages 21+' -> (21,200); 'All Ages' -> (0,200)."""
    s = (label or "").strip()
    if not s: return None
    if s.lower().startswith("all"): return (0, 200)
    nums = re.findall(r"\d+", s)
    if not nums: return None                      # legacy wording like 'Toddlers'
    if "+" in s: return (int(nums[0]), 200)
    if len(nums) >= 2: return (int(nums[0]), int(nums[1]))
    return (int(nums[0]), int(nums[0]))

def age_label(age_range):
    """Turn a multi-select into ONE readable phrase for the public listing.
    'Ages 5–7, Ages 8–10, Ages 11–14' -> 'Ages 5–14'. Non-touching picks stay
    separate: 'Ages 2–4, Ages 21+' -> 'Ages 2–4 & 21+'."""
    parts = [p.strip() for p in (age_range or "").split(",") if p.strip()]
    if not parts: return ""
    if any(p.lower().startswith("all") for p in parts): return "All ages"
    spans = sorted(b for b in (_age_bounds(p) for p in parts) if b)
    if not spans: return age_range                # unparseable: leave it alone
    merged = [list(spans[0])]
    for lo, hi in spans[1:]:
        if lo <= merged[-1][1] + 1: merged[-1][1] = max(merged[-1][1], hi)
        else: merged.append([lo, hi])
    out = [f"{lo}+" if hi >= 200 else (str(lo) if lo == hi else f"{lo}–{hi}") for lo, hi in merged]
    return "Ages " + " & ".join(out)

def audit(c, class_id, prev_status, new_status, actor_id):
    """Append one immutable entry recording a Class status change. Snapshots the
    class record AS IT STANDS NOW (after the change). Append-only: this is the ONLY
    place the app writes audit_log, and it never updates or deletes; DB triggers
    enforce that even against bugs. actor_id is None for automated (scheduler) changes."""
    r = c.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    snap = json.dumps({k: r[k] for k in r.keys()}) if r else "{}"
    c.execute("INSERT INTO audit_log(class_id,prev_status,new_status,actor_id,ts,snapshot) VALUES(?,?,?,?,?,?)",
              (class_id, prev_status, new_status, actor_id, now(), snap))

# ------------------------------------------------- decision concurrency ----
# SQLite has no SELECT ... FOR UPDATE (it has no row-level locks at all; it locks
# the whole database file). The equivalent guarantee here is two-part:
#   1. BEGIN IMMEDIATE takes the write lock at the START of the transaction, so a
#      second admin's decision cannot interleave between our read and our write.
#   2. The status change is a compare-and-swap: UPDATE ... WHERE status='pending'.
#      rowcount tells us whether WE made the change or someone beat us to it. That
#      CAS is the optimistic lock, and it holds even if the transaction is retried.
def begin_immediate(c):
    c.isolation_level = None          # take manual control of the transaction
    c.execute("BEGIN IMMEDIATE")

def decided_by(c, class_id, from_status="pending"):
    """Who moved this class out of `from_status`, per the immutable audit log.
    Pass 'graphic_review' to find who published it, not who approved it."""
    r = c.execute("""SELECT u.name AS name FROM audit_log a
                     LEFT JOIN users u ON u.id = a.actor_id
                     WHERE a.class_id=? AND a.prev_status=? AND a.new_status<>a.prev_status
                     ORDER BY a.id DESC LIMIT 1""", (class_id, from_status)).fetchone()
    return (r["name"] if r and r["name"] else None)

DECISION_WORD = {"graphic_review":"approved", "approved":"approved", "incomplete":"sent back",
                 "instructor_review":"edited", "cancelled":"cancelled"}

def lost_race_message(c, cls):
    """The friendly 'someone beat you to it' line for a class that is no longer pending."""
    who = decided_by(c, cls["id"])
    what = DECISION_WORD.get(cls["status"], "updated")
    return (f"This submission was just {what} by {who}."
            if who else f"This submission was just {what} by another admin.")

def merge_external(cid, updates):
    """Merge keys into a class's external_ids JSON without clobbering what's there."""
    c = db()
    row = c.execute("SELECT external_ids FROM classes WHERE id=?",(cid,)).fetchone()
    cur = json.loads((row["external_ids"] if row else "") or "{}")
    cur.update({k: v for k, v in updates.items() if v is not None})
    c.execute("UPDATE classes SET external_ids=? WHERE id=?",(json.dumps(cur), cid))
    c.commit(); c.close()

def start_graphic_review(c, cls, actor_id=None, spawn=True):
    """Admin approved the class: build the Canva graphic and HOLD for review.
    Nothing is posted publicly yet. The admin sees the poster, can adjust the
    headline/script line, and only then publishes. Pass spawn=False when calling
    inside a transaction and start the render yourself after the commit."""
    head = (cls.get("headline") or "").strip() or (cls.get("title") or "")
    c.execute("UPDATE classes SET status='graphic_review', headline=? WHERE id=?",(head, cls["id"]))
    audit(c, cls["id"], cls.get("status"), "graphic_review", actor_id)
    prepared = {**dict(cls), "headline": head, "status": "graphic_review"}
    if spawn:
        threading.Thread(target=render_graphic_async, args=(prepared,), daemon=True).start()
    return prepared

# ------------------------------------------------------- outbound job queue ----
# Every call to an outside service goes through here. Failures are retried on a
# backoff instead of surfacing to the admin; only a job that exhausts its retries
# is flagged on the dashboard. The class itself stays approved throughout.
BACKOFF = [60, 300, 900, 3600]        # 1 min, 5 min, 15 min, 1 hour
MAX_ATTEMPTS = len(BACKOFF) + 1       # first try + 4 retries
QUEUE_TICK = 20                       # seconds between sweeps
PLATFORM_LABEL = {"canva":"Canva", "eventbrite":"Eventbrite", "facebook":"Facebook",
                  "wix":"Wix", "descene":"Descene", "gcal":"Google Calendar"}

def refresh_publishing_flag(c, class_id):
    """publishing_in_progress simply means "this class still has outbound jobs in
    flight". Recomputed from the queue rather than tracked by hand, so it can never
    get stuck on after a crash or a restart."""
    n = c.execute("SELECT COUNT(*) FROM job_queue WHERE class_id=? AND status IN ('queued','running')",
                  (class_id,)).fetchone()[0]
    c.execute("UPDATE classes SET publishing_in_progress=? WHERE id=?", (1 if n else 0, class_id))
    return n

def enqueue(c, class_id, platform, payload=None, delay=0):
    when = (datetime.datetime.now() + datetime.timedelta(seconds=delay)).isoformat(timespec="seconds")
    c.execute("""INSERT INTO job_queue(class_id,platform,payload,attempts,next_run_at,status,created,updated)
                 VALUES(?,?,?,0,?,'queued',?,?)""",
              (class_id, platform, json.dumps(payload or {}), when, now(), now()))

def queue_publish(class_id, image_url=None, instructor_name=""):
    """Queue every outbound post for a newly published class."""
    c = db()
    for platform in ("eventbrite", "facebook", "wix", "descene", "gcal"):
        payload = {}
        if platform == "eventbrite": payload["image_url"] = image_url
        if platform == "gcal":       payload["instructor_name"] = instructor_name
        enqueue(c, class_id, platform, payload)
    refresh_publishing_flag(c, class_id)
    c.commit(); c.close()
    print(f"[queue] class #{class_id}: queued 5 publishing jobs")

def render_graphic_async(cls):
    """Queue the Canva poster build. The worker retries it on failure."""
    c = db(); enqueue(c, cls["id"], "canva", {}); refresh_publishing_flag(c, cls["id"])
    c.commit(); c.close()
    print(f"[queue] class #{cls['id']}: queued Canva graphic")

def _run_platform(platform, cls, payload):
    """Do the actual outside call. Returns (outcome, detail) where outcome is
    True (done), False (retryable failure) or None (nothing to do, e.g. no keys)."""
    cfg = integrations.load_config()
    try:
        if platform == "canva":
            res = integrations.render_canva(cls, cfg)
        elif platform == "eventbrite":
            res = integrations.post_eventbrite(cls, cfg, payload.get("image_url"))
        elif platform == "facebook":
            res = integrations.post_facebook(cls, cfg, None)
        elif platform == "wix":
            res = integrations.post_wix(cls, cfg)
        elif platform == "descene":
            res = integrations.post_descene(cls, cfg)
        elif platform == "gcal":
            gcfg = gcal.load_gcal_config()
            if not gcal.configured(gcfg): return None, "Google Calendar is not connected"
            eid = gcal.create_event({**cls, "instructor_name": payload.get("instructor_name","")}, gcfg)
            return (True, {"id": eid}) if eid else (None, "dry-run or nothing to add")
        else:
            return None, "unknown platform"
    except Exception as e:
        return False, str(e)          # network/HTTP blow-ups are retryable
    if res.get("ok"):
        return True, res
    status = str(res.get("status") or "")
    if status.startswith("skipped") or status.startswith("manual"):
        return None, status           # not configured / no API: nothing to retry
    return False, (res.get("error") or status or "unknown error")

def _record_success(class_id, platform, detail):
    if not isinstance(detail, dict): return
    ext = {}
    if platform == "canva":
        ext = {"canva_id": detail.get("id"), "canva_image_url": detail.get("image_url"),
               "canva_status": detail.get("status")}
    elif platform == "gcal":
        ext = {"gcal_event_id": detail.get("id")}
    elif detail.get("id"):
        ext = {platform + "_id": detail.get("id")}
    if ext: merge_external(class_id, ext)

def process_due_jobs():
    c = db()
    due = [dict(r) for r in c.execute(
        "SELECT * FROM job_queue WHERE status='queued' AND next_run_at<=? ORDER BY id LIMIT 25",
        (now(),)).fetchall()]
    c.close()
    for job in due:
        c = db()
        claimed = c.execute("UPDATE job_queue SET status='running', updated=? WHERE id=? AND status='queued'",
                            (now(), job["id"])).rowcount
        if claimed == 1:
            row = c.execute("SELECT * FROM classes WHERE id=?", (job["class_id"],)).fetchone()
            cls = dict(row) if row else None
        c.commit(); c.close()
        if claimed != 1: continue
        if not cls:
            c = db(); c.execute("UPDATE job_queue SET status='skipped', last_error='class no longer exists', updated=? WHERE id=?",
                                (now(), job["id"]))
            refresh_publishing_flag(c, job["class_id"]); c.commit(); c.close(); continue

        outcome, detail = _run_platform(job["platform"], cls, json.loads(job["payload"] or "{}"))
        label = PLATFORM_LABEL.get(job["platform"], job["platform"])
        attempts = job["attempts"] + 1
        c = db()
        if outcome is True:
            c.execute("UPDATE job_queue SET status='done', attempts=?, last_error='', updated=? WHERE id=?",
                      (attempts, now(), job["id"]))
            print(f"[queue] class #{cls['id']} {label}: done")
        elif outcome is None:
            c.execute("UPDATE job_queue SET status='skipped', attempts=?, last_error=?, updated=? WHERE id=?",
                      (attempts, str(detail)[:400], now(), job["id"]))
            print(f"[queue] class #{cls['id']} {label}: skipped ({detail})")
        elif attempts >= MAX_ATTEMPTS:
            c.execute("UPDATE job_queue SET status='failed', attempts=?, last_error=?, updated=? WHERE id=?",
                      (attempts, str(detail)[:400], now(), job["id"]))
            print(f"[queue] class #{cls['id']} {label}: FAILED after {attempts} attempts -> {detail}")
        else:
            delay = BACKOFF[min(attempts, len(BACKOFF)) - 1]
            nxt = (datetime.datetime.now() + datetime.timedelta(seconds=delay)).isoformat(timespec="seconds")
            c.execute("UPDATE job_queue SET status='queued', attempts=?, last_error=?, next_run_at=?, updated=? WHERE id=?",
                      (attempts, str(detail)[:400], nxt, now(), job["id"]))
            print(f"[queue] class #{cls['id']} {label}: attempt {attempts} failed ({detail}); retrying in {delay}s")
        refresh_publishing_flag(c, job["class_id"])   # clears once nothing is left in flight
        c.commit(); c.close()
        if outcome is True: _record_success(job["class_id"], job["platform"], detail)

def queue_worker():
    while True:
        try: process_due_jobs()
        except Exception as e: print("[queue] worker error:", e)
        time.sleep(QUEUE_TICK)

def publish_now(c, cls, actor_id=None, spawn=True):
    """Final step, after the admin has reviewed the graphic: post to Eventbrite with
    that graphic attached, add the class to the Google Calendar, email the instructor.
    Pass spawn=False inside a transaction and run the returned side effects after the
    commit, so no network work happens while holding the write lock."""
    instr = dict(c.execute("SELECT * FROM users WHERE id=?",(cls["instructor_id"],)).fetchone())
    c.execute("UPDATE classes SET status='approved' WHERE id=?",(cls["id"],))
    audit(c, cls["id"], cls.get("status"), "approved", actor_id)
    seed_registrations(c, cls)
    subj, body = mailer.tmpl_approved(cls, instr)
    # a hand-attached poster wins over the Canva render: an admin chose it on purpose
    img = cls.get("poster") or json.loads(cls.get("external_ids") or "{}").get("canva_image_url")
    side = {"to": instr["email"], "subject": subj, "body": body,
            "cls": dict(cls), "instructor_name": instr["name"], "image_url": img}
    if spawn: run_publish_side_effects(side)
    return side

def run_publish_side_effects(side):
    if not side: return
    mailer.send(side["to"], side["subject"], side["body"])
    threading.Thread(target=_publish_async,
                     args=(side["cls"], side["instructor_name"], side["image_url"]), daemon=True).start()

def _publish_async(cls, instructor_name, image_url=None):
    """Hand the outbound posting to the retry queue. Nothing is attempted inline, so
    a platform being down never surfaces as an error to the admin."""
    try:
        queue_publish(cls["id"], image_url=image_url, instructor_name=instructor_name)
    except Exception as e:
        print("[publish async] could not queue:", e)

def emails_for(c, where, args=()):
    # never mail a deactivated account
    joiner = "AND" if where.strip().upper().startswith("WHERE") else "WHERE"
    return [r[0] for r in c.execute(
        f"SELECT email FROM users {where} {joiner} deleted_at IS NULL", args).fetchall() if r[0]]

# --------------------------------------------------- lifecycle scheduler ----
_MON_NAMES = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
_MONS = {m:i for i,m in enumerate(_MON_NAMES, start=1)}
_DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]

# Slot labels carry no year, so the season decides one: months at or after the
# season-start month belong to the start year, earlier months to the next year
# (Dec -> 2026, Jan..May -> 2027 for a season starting 2026-12-01).
SEASON_START = os.environ.get("SEASON_START", "2026-12-01")

def _season_pivot():
    try:
        d = datetime.date.fromisoformat(SEASON_START)
        return d.month, d.year
    except ValueError:
        return 12, 2026

def season_label():
    """'SPRING 2027' for a season that starts 2026-12-01."""
    pm, py = _season_pivot()
    return f"SPRING {py + 1}"

def build_contract_text(cls, instructor_name):
    """The Master Instructor Contract, word for word from The Gibby's form, with
    the instructor, class, dates and pay filled in. Frozen at approval time so
    what was signed can never drift."""
    try:
        sessions = json.loads(cls.get("session_dates") or "[]")
    except Exception:
        sessions = []
    tm = cls.get("class_time") or cls.get("slot_time") or ""
    if len(sessions) > 1:
        when = (f"{len(sessions)} sessions, {sessions[0].get('date','')} through "
                f"{sessions[-1].get('date','')}, {tm} weekly")
    else:
        when = f"{cls.get('slot_date','')}, {tm}"
    if cls.get("waives_pay"):
        rate = "$0 (time donated by the instructor)"
    elif (cls.get("pay_model") or "flat") == "split":
        rate = "60% of ticket sales after material costs"
    else:
        rate = f"${cls.get('instructor_pay') or 0} (flat fee)"
    title = cls.get("title") or "the class"
    return f"""VISUAL ARTS INSTRUCTOR CONTRACT

The Everett Inc. contracts with {instructor_name} to be a Visual Arts Instructor at the Gilbert W. Perry Jr. Center for the Arts "The Gibby" located at 51 W. Main Street, Middletown, Delaware 19709 during The Gibby's {season_label()} ARTS programming for {title} taking place {when}.

All programming taught at The Gibby may not be duplicated at another organization, business, or community event within 15 miles of 51 W. Main Street, Middletown, Delaware for a period of 90 days before and after the schedule event, workshop, or class at The Gibby.

As an Instructor you commit to:

- Having all planning and preparation as needed for the start of each program
- Providing appropriate assistance and direction to the participant(s)
- Being punctual arriving at least 30 minutes before the start of each workshop
- Being accountable to The Gibby's Board of Director, Meghan Savage and/or the Director of Operations, Michelle Truban
- Obtain student rosters and ensure The Gibby has contacted participant or their parent/guardian one week prior to class beginning
- Check in with The Gibby Board Representative, or Director of Operations, to ensure you have everything you need for your class
- Greet students upon arrival
- For all participants under the age of 18 a contact name and number should be collected when dropping off on a sign-in sheet
- Communicate with The Gibby Board Representative, or Director of Operations, any student who does not attend the class
- Ensure all doors are locked, all lights are off, and the facility is left as you found it

If you are unable to instruct during a date and time previously agreed upon you will inform The Gibby Board Representative, or Director of Operations, immediately so that The Gibby can arrange coverage accordingly or postpone the event/class if needed.

In consideration of such service, The Everett Inc. agrees to pay you for your services at the rate of {rate} for {title} for this position. Breach of contract will result in ineligibility for future employment with The Everett Inc.

My typed name below will serve as my signature on file."""

def season_year(month):
    pm, py = _season_pivot()
    return py if month >= pm else py + 1

# Booking opens month by month: instructors see the season's first month, and on
# the LAST DAY of every month the next not-yet-shown month unlocks. The anchor is
# the month the rollout began; every month-end since then reveals one more month.
REVEAL_ANCHOR = os.environ.get("SLOT_REVEAL_ANCHOR", "2026-08")
_MON_FULL = ["January","February","March","April","May","June","July","August",
             "September","October","November","December"]

def _month_last_day(d):
    return (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)

def visible_month_count(today=None):
    t = today or datetime.date.today()
    try:
        ay, am = (int(x) for x in REVEAL_ANCHOR.split("-")[:2])
    except ValueError:
        ay, am = 2026, 8
    elapsed = max(0, (t.year * 12 + t.month) - (ay * 12 + am))
    if t == _month_last_day(t):
        elapsed += 1          # today IS a month-end: today's unlock counts
    return 1 + elapsed

def _season_key(m):
    return (season_year(m), m)

def _avail_months(c):
    rows = c.execute("SELECT DISTINCT date FROM slots WHERE status='available' AND deleted_at IS NULL").fetchall()
    return sorted({p.month for p in (parse_day(r[0]) for r in rows) if p}, key=_season_key)

def month_is_visible(c, month, today=None):
    months = _avail_months(c)
    return month in set(months[:visible_month_count(today)])

def parse_day(label, year=None):
    """'Sat, Jan 17' -> date(2027,1,17). Slot labels carry no year (see note below)."""
    try:
        p = (label or "").split(", ")[-1].split()
        mon = _MONS[p[0]]
        return datetime.date(year if year else season_year(mon), mon, int(p[1]))
    except Exception:
        return None

def day_label(d):
    """date -> 'Sat, Jan 17', the format slots are stored in."""
    return f"{_DOW[d.weekday()]}, {_MON_NAMES[d.month-1]} {d.day}"

def _class_date(cls, year=None):
    """First session. Used for the 48h reminder and the 1-week auto-cancel."""
    return parse_day(cls.get("slot_date"), year)

def _class_end_date(cls, year=None):
    """Last session. A 6-week course finishes weeks after it starts, so the
    day-after follow-up has to key off the END, not the first meeting."""
    try:
        sessions = json.loads(cls.get("session_dates") or "[]")
        if sessions:
            return parse_day(sessions[-1]["date"], year)
    except Exception:
        pass
    return _class_date(cls, year)

def find_series_sessions(c, first_ids, weeks, year=None):
    """Given the slot ids for the FIRST session, find the same weekday+time+room on
    following weeks. Weeks that are already taken are skipped and the search keeps
    going forward, so a 6-week course still gets 6 sessions around a busy Saturday.
    Returns (sessions, skipped) where each session is {date,start,end,slot_ids}."""
    rows = [dict(r) for r in c.execute(
        f"SELECT * FROM slots WHERE id IN ({','.join('?'*len(first_ids))}) AND deleted_at IS NULL", first_ids).fetchall()]
    if len(rows) != len(first_ids): return None, None
    rows.sort(key=lambda r: tmin(r["start"]))
    d0 = parse_day(rows[0]["date"], year)
    if not d0: return None, None
    room  = rows[0]["room"]
    times = [(r["start"], r["end"]) for r in rows]
    sessions = [{"date": rows[0]["date"], "start": rows[0]["start"], "end": rows[-1]["end"],
                 "slot_ids": [r["id"] for r in rows]}]
    skipped = []
    week = 1
    # look a good way past the requested run so skipped weeks can be made up
    while len(sessions) < weeks and week <= weeks * 3 + 8:
        d = d0 + datetime.timedelta(days=7*week); week += 1
        label = day_label(d)
        ids = []
        for (st, en) in times:
            r = c.execute("""SELECT id FROM slots WHERE date=? AND start=? AND end=?
                             AND status='available' AND deleted_at IS NULL AND (room=? OR room='')""",
                          (label, st, en, room)).fetchone()
            if not r: break
            ids.append(r["id"])
        if len(ids) == len(times):
            sessions.append({"date": label, "start": times[0][0], "end": times[-1][1], "slot_ids": ids})
        else:
            skipped.append((week, label))
    # Only report weeks skipped INSIDE the delivered run. Weeks looked at after the
    # last session found are not "skipped", we simply ran out of calendar.
    last_week = 0
    if len(sessions) > 1:
        last = parse_day(sessions[-1]["date"], year)
        last_week = round((last - d0).days / 7)
    skipped = [lbl for (w, lbl) in skipped if w < last_week]
    return sessions, skipped

# Every automated class email must declare which class statuses it is valid for, and
# whether the class must still be in the future. Nothing sends outside these rules.
EMAIL_RULES = {
    "reminder":          {"statuses": ("approved",),  "future_only": True,  "label": "48h reminder"},
    "followup":          {"statuses": ("approved",),  "future_only": False, "label": "day-after follow-up"},
    "cancel":            {"statuses": ("cancelled",), "future_only": False, "label": "cancellation notice"},
    "cancel_instructor": {"statuses": ("cancelled",), "future_only": False, "label": "instructor cancellation notice"},
    "low_alert":         {"statuses": ("approved",),  "future_only": True,  "label": "low-enrollment alert"},
    "followup_request":  {"statuses": ("approved",),  "future_only": False, "label": "follow-up writing request"},
    "headcount":         {"statuses": ("approved",),  "future_only": True,  "label": "confirmed headcount"},
}

def email_sent_at(c, class_id, email_type):
    r = c.execute("SELECT sent_at FROM email_log WHERE class_id=? AND email_type=?",
                  (class_id, email_type)).fetchone()
    return r["sent_at"] if r else None

def send_class_email(c, cls, email_type, recipients, subject, body, asof=None, cfg=None):
    """THE gate every automated class email goes through. Verifies:
      1. the class is in an expected status for this email type,
      2. this email type has not already been sent for this class,
      3. for reminders, the class date is still in the future.
    Claims the (class, type) slot in email_log BEFORE sending, so a race cannot
    produce two emails. Every suppression is logged with its reason.
    Returns (sent: bool, reason: str)."""
    rule = EMAIL_RULES[email_type]
    cid, title = cls["id"], cls.get("title", "")

    def suppressed(reason):
        print(f"[email SUPPRESSED] class #{cid} {title!r} type={email_type}: {reason}")
        return False, reason

    status = cls.get("status")
    if status not in rule["statuses"]:
        return suppressed(f"class status is {status!r}, expected {' or '.join(rule['statuses'])}")

    prev = email_sent_at(c, cid, email_type)
    if prev:
        return suppressed(f"already sent at {prev}")

    if rule["future_only"]:
        d, today = _class_date(cls), (asof or datetime.date.today())
        if d is None:
            return suppressed(f"could not read the class date from {cls.get('slot_date')!r}")
        if d < today:
            return suppressed(f"class date {d.isoformat()} has already passed")

    recips = sorted({r for r in (recipients or []) if r and "@" in r})
    if not recips:
        return suppressed("no valid recipients")

    # Claim first, then send: if another job already claimed it, we never send.
    try:
        c.execute("INSERT INTO email_log(class_id,email_type,sent_at,recipients) VALUES(?,?,?,?)",
                  (cid, email_type, now(), len(recips)))
    except sqlite3.IntegrityError:
        return suppressed("already sent (another job claimed it first)")

    delivered = mailer.send(recips, subject, body, cfg)
    c.execute("UPDATE email_log SET delivered=? WHERE class_id=? AND email_type=?",
              (1 if delivered else 0, cid, email_type))
    return True, f"{rule['label']} sent to {len(recips)}"

def run_scheduler(asof=None):
    """One daily tick. Fires the brief's lifecycle automations. Every send goes
    through send_class_email, which enforces status/duplicate/date guards and logs
    anything it suppresses. Returns the actions taken (for logging/UI)."""
    today = asof or datetime.date.today()
    c = db(); actions = []
    for r in c.execute("SELECT * FROM classes WHERE status IN ('approved','cancelled') AND deleted_at IS NULL").fetchall():
        cls = dict(r); d = _class_date(cls)
        if not d: continue
        days = (d - today).days                       # to the FIRST session
        end = _class_end_date(cls) or d
        end_days = (end - today).days                 # to the LAST session (series-aware)
        enrolled = enrollment(c, cls["id"])
        students = [x[0] for x in c.execute("SELECT email FROM registrations WHERE class_id=? AND refunded=0",(cls["id"],)).fetchall()]
        instr = c.execute("SELECT email FROM users WHERE id=?",(cls["instructor_id"],)).fetchone()
        cfg = mailer.load_email_config()
        if cls["status"] == "approved":
            if 7 < days <= 14 and enrolled < (cls["min_p"] or 0):
                sent, why = send_class_email(c, cls, "low_alert", emails_for(c,"WHERE role='admin'"),
                    f"Low enrollment: {cls['title']}",
                    f"\"{cls['title']}\" on {cls['slot_date']} has {enrolled}/{cls['min_p']} registered, two weeks out. Consider promoting it.",
                    today, cfg)
                if sent:
                    c.execute("UPDATE classes SET low_alerted=1 WHERE id=?",(cls["id"],))
                    actions.append(f"low-enroll alert: {cls['title']}")
            if days == 7 and enrolled < (cls["min_p"] or 0):
                c.execute("UPDATE classes SET status='cancelled' WHERE id=?",(cls["id"],))
                audit(c, cls["id"], cls.get("status"), "cancelled", None)   # automated: no actor
                c.execute("UPDATE registrations SET refunded=1 WHERE class_id=?",(cls["id"],))
                cancelled = {**cls, "status": "cancelled"}   # guards must see the NEW status
                subj, body = mailer.tmpl_cancel(cls)
                send_class_email(c, cancelled, "cancel", students, subj, body, today, cfg)
                send_class_email(c, cancelled, "cancel_instructor", [instr[0]] if instr else [],
                                 "Your class was auto-cancelled", body, today, cfg)
                actions.append(f"AUTO-CANCELLED (refunded {len(students)}): {cls['title']}"); continue
            close_days = int(cls.get("close_days") or 0)
            if close_days and days == close_days:
                # Registration has just closed. This number will not move now, which
                # is the whole point of the cutoff: shop for materials against it.
                instr_row = c.execute("SELECT name,email FROM users WHERE id=?",(cls["instructor_id"],)).fetchone()
                if instr_row:
                    first = (instr_row["name"] or "").split(" ")[0] or "there"
                    mat_line = (f"At {money_str(cls.get('material_cost'))} a head that is "
                                f"{money_str((cls.get('material_cost') or 0) * enrolled)} of materials.\n\n"
                                if cls.get("material_cost") else "")
                    if (cls.get("pay_model") or "flat") == "split":
                        mat_line += (f"At the 60% split, your pay for this class comes to "
                                     f"{money_str(class_finance(cls, enrolled)['instructor_pay'])}.\n\n")
                    sent, why = send_class_email(c, cls, "headcount", [instr_row["email"]],
                        f"Final numbers for {cls['title']}: {enrolled} booked",
                        f"Hi {first},\n\nRegistration for \"{cls['title']}\" has closed.\n\n"
                        f"You have {enrolled} student{'' if enrolled==1 else 's'} booked "
                        f"(room for {cls.get('max_p')}).\n\n{mat_line}"
                        f"That number is settled now, so you can shop with confidence.\n\n"
                        f"See you on {cls.get('slot_date','')}.\nThe Gibby", today, cfg)
                    if sent:
                        c.execute("UPDATE classes SET headcount_sent=1 WHERE id=?",(cls["id"],))
                        actions.append(f"final headcount to {instr_row['name']} ({enrolled}): {cls['title']}")
            if days == 2:
                subj, body = mailer.tmpl_reminder(cls, cfg)
                sent, why = send_class_email(c, cls, "reminder", students, subj, body, today, cfg)
                if sent:
                    c.execute("UPDATE classes SET reminded=1 WHERE id=?",(cls["id"],))
                    actions.append(f"48h reminder ({len(students)}): {cls['title']}")
                else: actions.append(f"reminder suppressed for {cls['title']}: {why}")
        if end_days == -1:      # the day after the LAST session, not the first
            # The follow-up is written by the instructor, approved by an admin and
            # only then sent. The scheduler just opens the task and nudges them.
            if not cls.get("followup_status"):
                c.execute("""UPDATE classes SET followup_status='awaiting_instructor',
                             followup_requested_at=? WHERE id=?""", (now(), cls["id"]))
            instr_row = c.execute("SELECT name,email FROM users WHERE id=?",(cls["instructor_id"],)).fetchone()
            if instr_row:
                first = (instr_row["name"] or "").split(" ")[0] or "there"
                sent, why = send_class_email(c, {**cls, "followup_status": "awaiting_instructor"},
                    "followup_request", [instr_row["email"]],
                    f"Write your note to students: {cls['title']}",
                    f"Hi {first},\n\n\"{cls['title']}\" has finished. When you have a moment, please "
                    f"write a short note to your students.\n\nOpen the app, go to My classes, and you "
                    f"will find it waiting under Follow-up notes. An admin reviews it before it goes "
                    f"out.\n\nWhile you are there, there are three quick questions about how the class "
                    f"went. Those are just for us, they are never sent to students, and they help us "
                    f"plan next season.\n\nThank you,\nThe Gibby", today, cfg)
                actions.append(f"asked {instr_row['name']} to write the follow-up: {cls['title']}"
                               if sent else f"follow-up request suppressed for {cls['title']}: {why}")
    c.commit(); c.close()
    if actions: print("[scheduler]", "; ".join(actions))
    return actions

def reconcile_calendar_slots(open_slots):
    """Make the slots table match the calendar's open times. Calendar slots that are
    no longer open are SOFT deleted (recoverable, and restored automatically if the
    time reopens). Claimed slots and manually-created ones are never touched."""
    c = db()
    existing = {(r["date"],r["start"],r["end"]): dict(r)
                for r in c.execute("SELECT * FROM slots WHERE source='calendar'")}
    want = {(s["date"],s["start"],s["end"]) for s in open_slots}
    added = removed = restored = 0
    for k, r in existing.items():
        if k not in want and r["status"] == "available" and not r["deleted_at"]:
            c.execute("UPDATE slots SET deleted_at=? WHERE id=?", (now(), r["id"])); removed += 1
    for s in open_slots:
        k = (s["date"], s["start"], s["end"])
        prev = existing.get(k)
        if prev is None:
            c.execute("INSERT INTO slots(date,start,end,room,status,source) VALUES(?,?,?,'','available','calendar')",
                      (s["date"],s["start"],s["end"])); added += 1
        elif prev["deleted_at"]:
            # the time reopened on the calendar: bring the original row back
            c.execute("UPDATE slots SET deleted_at=NULL WHERE id=?", (prev["id"],)); restored += 1
    c.commit(); c.close()
    return {"added": added, "removed": removed, "restored": restored, "open": len(open_slots)}

def sync_calendar():
    cfg = gcal.load_gcal_config()
    if not gcal.configured(cfg): return None
    slots = gcal.sync_slots(cfg)
    if slots is None: return None
    return reconcile_calendar_slots(slots)

def scheduler_loop():
    # Calendar slots refresh every 5 minutes so a change on the Gibby calendar
    # shows up almost immediately. The heavier lifecycle work (emails, Eventbrite
    # attendee sync) stays hourly; its day-based rules only fire once per class.
    SYNC_EVERY, LIFECYCLE_EVERY = 300, 3600
    last_lifecycle = 0
    while True:
        try:
            r = sync_calendar()
            if r and (r.get("added") or r.get("removed") or r.get("restored")):
                print("[gcal] sync", r)
        except Exception as e: print("[gcal] sync error:", e)
        if time.time() - last_lifecycle >= LIFECYCLE_EVERY - 5:
            last_lifecycle = time.time()
            try: run_scheduler()
            except Exception as e: print("[scheduler] error:", e)
            prune_sessions()          # tidy away long-dead auth rows
            try:
                c = db()
                live = [r["id"] for r in c.execute(
                    "SELECT id FROM classes WHERE status='approved' AND deleted_at IS NULL")]
                c.close()
                for cid in live:      # keeps enrolment honest for the low-enrollment rules
                    sync_registrations(cid)
            except Exception as e:
                print("[registrations] hourly sync error:", e)
        time.sleep(SYNC_EVERY)

# ------------------------------------------------------------- handler ----
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass  # quiet

    # -- helpers --
    def cookie(self, name):
        ck = http.cookies.SimpleCookie(self.headers.get("Cookie",""))
        return ck[name].value if name in ck else None

    def current_user(self):
        """Every request re-checks the token against the database, so expiry,
        logout and account removal all take effect immediately."""
        tok = self.cookie("gibby_session")
        if not tok: return None
        c = db()
        row = c.execute("""SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id
                           WHERE s.token=?
                             AND u.deleted_at IS NULL          -- archived account
                             AND s.revoked_at IS NULL          -- logged out / rotated
                             AND s.expires_at > ?              -- access token still live
                        """, (tok, now())).fetchone()
        c.close()
        return dict(row) if row else None

    def send_json(self, obj, code=200, cookie=None, headers=None):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body)))
        if cookie:
            for ck in ([cookie] if isinstance(cookie, str) else cookie):
                self.send_header("Set-Cookie", ck)
        for k, v in (headers or {}).items(): self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(body)

    def rate_limited(self, name, user_id):
        """Check one bucket. If the caller is over, send the 429 (with Retry-After)
        and return True so the handler can just `return`."""
        allowed, retry_after, remaining = rate_check(name, user_id)
        if allowed: return False
        limit, window = RATE_LIMITS[name]
        mins = max(1, round(retry_after / 60))
        self.send_json({"error": f"You have reached the limit of {limit} {RATE_LABEL[name]} per hour. "
                                 f"Please try again in about {mins} minute{'s' if mins != 1 else ''}.",
                        "retry_after": retry_after},
                       429, headers={"Retry-After": retry_after,
                                     "X-RateLimit-Limit": limit,
                                     "X-RateLimit-Remaining": 0})
        return True

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
        if not p.startswith("/api/"): return self.send_error(404)
        # Single choke point: every state-changing request is checked here, so a new
        # endpoint cannot forget to protect itself.
        if not self.csrf_ok(p): return
        return self.api_post(p)

    def csrf_ok(self, path):
        """Synchronizer token check. Sends the 403 itself and returns False on failure."""
        if path in CSRF_EXEMPT: return True
        cookie_tok = self.cookie("gibby_session")
        if not cookie_tok: return True          # unauthenticated: the handler's own 401 applies
        expected = session_csrf(cookie_tok)
        if not expected: return True            # dead session: again, let the 401 happen
        sent = self.headers.get("X-CSRF-Token") or ""
        if sent and secrets.compare_digest(sent, expected): return True
        print(f"[csrf] rejected {path} - {'missing' if not sent else 'mismatched'} token")
        self.send_json({"error":"Your session security token was missing or out of date. "
                                "Please refresh the page and try again.",
                        "csrf": True}, 403)
        return False

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
        if is_text:
            self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if is_text else ""))
        self.send_header("Content-Length",str(len(data)))
        self.end_headers(); self.wfile.write(data)

    # -- GET api --
    def api_get(self, p):
        if p == "/api/version":
            # open_slots is deliberately public: it says nothing beyond what the
            # published class listings do, and it lets a deploy be verified from
            # outside (did the calendar sync produce slots?) without credentials.
            c = db()
            n = c.execute("SELECT COUNT(*) FROM slots WHERE status='available' AND deleted_at IS NULL").fetchone()[0]
            c.close()
            return self.send_json({"version": VERSION, "open_slots": n,
                "calendar_source": gcal.LAST_SOURCE})
        if p == "/api/me":
            u = self.current_user()
            if not u: return self.send_json({"user": None, "season_start": SEASON_START})
            cq = db()
            n_contracts = cq.execute("""SELECT COUNT(*) FROM classes WHERE instructor_id=?
                AND contract_status='sent' AND deleted_at IS NULL""",(u["id"],)).fetchone()[0]
            cq.close()
            return self.send_json({"user": {"id":u["id"],"name":u["name"],"email":u["email"],
                "role":u["role"],"must_change_pw":u.get("must_change_pw",0),
                "photo":u.get("photo") or "", "skills":json.loads(u.get("skills") or "[]"),
                "address":u.get("address") or "",
                "contracts_to_sign":n_contracts},
                "season_start": SEASON_START,
                "csrf_token": session_csrf(self.cookie("gibby_session"))})
        if p == "/api/users":
            u = self.require("admin")
            if not u: return
            c = db()
            rows = [{"id":r["id"],"name":r["name"],"email":r["email"],"role":r["role"],
                     "pending":bool(r["must_change_pw"]), "photo":r["photo"] or "",
                     "skills":json.loads(r["skills"] or "[]"), "address":r["address"] or ""}
                    for r in c.execute("""SELECT id,name,email,role,must_change_pw,photo,skills,address FROM users
                                          WHERE deleted_at IS NULL ORDER BY role, name""").fetchall()]
            c.close(); return self.send_json({"users":rows})
        if p == "/api/slots":
            u = self.require()
            if not u: return
            c = db(); rows=[dict(r) for r in c.execute("SELECT * FROM slots WHERE status='available' AND deleted_at IS NULL ORDER BY id").fetchall()]; c.close()
            notice = None
            if u["role"] == "instructor":
                # Months unlock one at a time; hide the rest and say when the next opens.
                months = sorted({p.month for p in (parse_day(r["date"]) for r in rows) if p}, key=_season_key)
                show = set(months[:visible_month_count()])
                rows = [r for r in rows if (lambda p: p and p.month in show)(parse_day(r["date"]))]
                hidden = [m for m in months if m not in show]
                if hidden:
                    t = datetime.date.today()
                    unlock = _month_last_day(t)
                    if unlock == t:
                        unlock = _month_last_day(unlock + datetime.timedelta(days=1))
                    notice = (f"{_MON_FULL[hidden[0]-1]} dates open on {day_label(unlock)}. "
                              f"A new month opens on the last day of every month.")
            return self.send_json({"slots": rows, "notice": notice})
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
        if p.startswith("/api/classes/") and p.endswith("/publish-status"):
            # Per-platform state for the optimistic UI. One row per platform, latest wins.
            u = self.require("admin")
            if not u: return
            cid = int(p.split("/")[3]); c = db()
            row = c.execute("SELECT status, publishing_in_progress FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            jobs = {}
            for r in c.execute("SELECT * FROM job_queue WHERE class_id=? ORDER BY id",(cid,)):
                jobs[r["platform"]] = {"platform": r["platform"],
                    "label": PLATFORM_LABEL.get(r["platform"], r["platform"]),
                    "status": r["status"], "attempts": r["attempts"],
                    "error": r["last_error"] or "", "next_run_at": r["next_run_at"]}
            c.close()
            order = ["canva","eventbrite","facebook","wix","gcal","descene"]
            out = [jobs[k] for k in order if k in jobs] + [v for k,v in jobs.items() if k not in order]
            return self.send_json({"class_status": row["status"],
                                   "publishing": bool(row["publishing_in_progress"]),
                                   "platforms": out})
        if p == "/api/drafts":
            u = self.require("instructor")
            if not u: return
            c = db()
            rows = [dict(r) for r in c.execute("""
                SELECT id,title,slot_date,slot_time,room,is_series,session_count,created,updated
                FROM drafts WHERE instructor_id=? AND deleted_at IS NULL
                ORDER BY updated DESC""", (u["id"],))]
            c.close()
            return self.send_json({"drafts": rows})
        if p.startswith("/api/drafts/"):
            u = self.require("instructor")
            if not u: return
            try: did = int(p.split("/")[3])
            except ValueError: return self.send_json({"error":"not found"},404)
            c = db()
            row = c.execute("SELECT * FROM drafts WHERE id=? AND instructor_id=? AND deleted_at IS NULL",
                            (did, u["id"])).fetchone()
            if not row: c.close(); return self.send_json({"error":"That draft is no longer there."},404)
            d = dict(row)
            d["payload"] = json.loads(d["payload"] or "{}")
            ids = json.loads(d["slot_ids"] or "[]")
            # A draft never held the slots, so they may be gone by the time it reopens.
            still_free = True
            if ids:
                n = c.execute(f"""SELECT COUNT(*) FROM slots WHERE id IN ({','.join('?'*len(ids))})
                                  AND status='available' AND deleted_at IS NULL""", ids).fetchone()[0]
                still_free = (n == len(ids))
            c.close()
            d["slot_ids"] = ids
            d["slots_available"] = still_free
            return self.send_json({"draft": d})
        if p == "/api/report/money":
            u = self.require("admin")
            if not u: return
            # ?asof=YYYY-MM-DD looks at the season from a chosen date, which is how
            # you preview a term that has not finished yet.
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            today = datetime.date.today()
            if q.get("asof"):
                try: today = datetime.date.fromisoformat(q["asof"][0])
                except ValueError: pass
            c = db()
            rows = [dict(r) for r in c.execute("""
                SELECT cl.*, u.name AS instructor_name FROM classes cl
                LEFT JOIN users u ON u.id = cl.instructor_id
                WHERE cl.deleted_at IS NULL AND cl.status IN ('approved','cancelled')
                ORDER BY cl.slot_date""")]
            out = []
            for r in rows:
                enrolled = enrollment(c, r["id"])
                fin = class_finance(r, enrolled)
                end = _class_end_date(r)
                fin.update({"id": r["id"], "title": r["title"], "instructor": r["instructor_name"],
                            "slot_date": r["slot_date"], "room": r["room"], "status": r["status"],
                            "is_series": bool(r["is_series"]), "sessions": r["session_count"] or 1,
                            # only a class that has happened can be reported as fact
                            "actual": bool(end and end < today) or r["status"] == "cancelled"})
                out.append(fin)
            c.close()

            def rollup(key):
                g = {}
                # aggregate ONLY over classes that have run: projecting from partial
                # bookings would flatter an upcoming class into the league table
                for r in [x for x in out if x["actual"] and x["status"] != "cancelled"]:
                    k = r[key] or "unknown"
                    b = g.setdefault(k, {"name": k, "runs": 0, "revenue": 0.0, "pay": 0.0,
                                         "materials": 0.0, "net": 0.0, "seats": 0, "planned": 0})
                    b["runs"] += 1; b["revenue"] += r["revenue"]; b["pay"] += r["instructor_pay"]
                    b["materials"] += r["gibby_materials"]; b["net"] += r["net"]
                    b["seats"] += r["enrolled"]; b["planned"] += r["planned"]
                for b in g.values():
                    b["margin"] = (b["net"] / b["revenue"]) if b["revenue"] else 0
                    b["fill"] = (b["seats"] / b["planned"]) if b["planned"] else 0
                    b["net_per_run"] = b["net"] / b["runs"] if b["runs"] else 0
                    for k in ("revenue","pay","materials","net","net_per_run"): b[k] = round(b[k], 2)
                return sorted(g.values(), key=lambda x: -x["net_per_run"])

            done = [x for x in out if x["actual"] and x["status"] != "cancelled"]
            totals = {
                "classes": len(done),
                "revenue": round(sum(x["revenue"] for x in done), 2),
                "pay": round(sum(x["instructor_pay"] for x in done), 2),
                "materials": round(sum(x["gibby_materials"] for x in done), 2),
                "net": round(sum(x["net"] for x in done), 2),
                "seats": sum(x["enrolled"] for x in done),
                "planned": sum(x["planned"] for x in done),
            }
            totals["margin"] = (totals["net"] / totals["revenue"]) if totals["revenue"] else 0
            totals["fill"] = (totals["seats"] / totals["planned"]) if totals["planned"] else 0
            return self.send_json({"classes": out, "totals": totals,
                                   "by_class": rollup("title"), "by_instructor": rollup("instructor")})
        if p == "/api/feedback":
            # What instructors actually said, grouped by class title so a repeat of
            # the same class carries its history into the next season.
            u = self.require("admin")
            if not u: return
            c = db()
            rows = [dict(r) for r in c.execute("""
                SELECT f.*, cl.title, cl.slot_date, cl.max_p, cl.min_p, u.name AS instructor_name,
                       (SELECT COUNT(*) FROM registrations r
                        WHERE r.class_id=f.class_id AND r.refunded=0) AS enrolled
                FROM class_feedback f
                JOIN classes cl ON cl.id = f.class_id
                LEFT JOIN users u ON u.id = f.instructor_id
                WHERE cl.deleted_at IS NULL
                ORDER BY f.submitted_at DESC""")]
            c.close()
            by_title = {}
            for r in rows:
                t = by_title.setdefault(r["title"], {"title": r["title"], "runs": 0,
                        "again_yes": 0, "again_no": 0, "materials_short": 0,
                        "too_few": 0, "too_many": 0, "instructors": set()})
                t["runs"] += 1
                if r["teach_again"] == "yes": t["again_yes"] += 1
                if r["teach_again"] == "no":  t["again_no"]  += 1
                if r["materials"] == "short": t["materials_short"] += 1
                if r["enrollment"] == "too_few":  t["too_few"] += 1
                if r["enrollment"] == "too_many": t["too_many"] += 1
                if r["instructor_name"]: t["instructors"].add(r["instructor_name"])
            summary = []
            for t in by_title.values():
                t["instructors"] = sorted(t["instructors"])
                summary.append(t)
            summary.sort(key=lambda t: (-t["again_yes"], t["title"]))
            return self.send_json({"responses": rows, "by_title": summary})
        if p == "/api/admin/counts":
            # What is waiting on an admin, cheap enough to fetch on every nav render.
            u = self.require("admin")
            if not u: return
            c = db()
            q = lambda w: c.execute(f"SELECT COUNT(*) FROM classes WHERE deleted_at IS NULL AND {w}").fetchone()[0]
            needs = q("status='pending'") + q("status='graphic_review'") + q("followup_status='pending_admin'")
            failed = c.execute("SELECT COUNT(*) FROM job_queue WHERE status='failed'").fetchone()[0]
            c.close()
            return self.send_json({"needs_you": needs, "failed": failed})
        if p == "/api/classes/followup-review":
            u = self.require("admin")
            if not u: return
            out = self._classes("WHERE c.followup_status='pending_admin' ")
            c = db()
            for cl in out:      # tell the admin exactly who this will reach, and why
                who, known = followup_audience(c, cl["id"])
                cl["followup_recipients"] = len(who)
                cl["attendance_known"] = known
            c.close()
            return self.send_json({"classes": out})
        if p == "/api/classes/graphic-review":
            u = self.require("admin")
            if not u: return
            return self.send_json({"classes": self._classes("WHERE c.status='graphic_review' ")})
        if p == "/api/classes/mine":
            u = self.require("instructor")
            if not u: return
            return self.send_json({"classes": self._classes("WHERE c.instructor_id=? ", (u["id"],))})
        if p == "/api/classes/all":
            u = self.require("admin")
            if not u: return
            return self.send_json({"classes": self._classes("")})
        if p == "/api/archive":   # recovery view: everything currently soft-deleted
            u = self.require("admin")
            if not u: return
            c = db()
            classes = [dict(r) for r in c.execute("""
                SELECT c.id, c.title, c.slot_date, c.slot_time, c.room, c.status, c.deleted_at,
                       u.name AS instructor_name FROM classes c
                LEFT JOIN users u ON u.id=c.instructor_id
                WHERE c.deleted_at IS NOT NULL ORDER BY c.deleted_at DESC LIMIT 200""")]
            slots = [dict(r) for r in c.execute("""
                SELECT id, date, start, end, room, status, source, deleted_at FROM slots
                WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT 200""")]
            users = [dict(r) for r in c.execute("""
                SELECT id, name, email, role, deleted_at FROM users
                WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT 200""")]
            c.close()
            return self.send_json({"classes":classes, "slots":slots, "users":users})
        if p == "/api/instructors":   # active people, for the admin roster
            u = self.require("admin")
            if not u: return
            c = db()
            rows = [dict(r) for r in c.execute(
                "SELECT id,name,email,role FROM users WHERE deleted_at IS NULL ORDER BY role,name")]
            c.close()
            return self.send_json({"users": rows})
        if p == "/api/client-errors":   # what the UI error boundaries have caught
            u = self.require("admin")
            if not u: return
            c = db()
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM client_errors ORDER BY id DESC LIMIT 100").fetchall()]
            c.close()
            return self.send_json({"errors": rows})
        if p == "/api/email-log":   # read-only record of which automated emails went out
            u = self.require("admin")
            if not u: return
            c = db()
            rows = [dict(r) for r in c.execute("""
                SELECT e.*, c.title AS class_title, c.status AS class_status
                FROM email_log e LEFT JOIN classes c ON c.id = e.class_id
                ORDER BY e.id DESC LIMIT 500""").fetchall()]
            c.close()
            return self.send_json({"entries": rows})
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
                "publish_failures": self._publish_failures(),
                "pending": self._classes("WHERE c.status='pending' "),
                "graphic": self._classes("WHERE c.status='graphic_review' "),
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
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            if u["role"]=="instructor" and row["instructor_id"]!=u["id"]:
                c.close(); return self.send_json({"error":"forbidden"},403)
            regs=[dict(r) for r in c.execute("SELECT name,email,phone,refunded FROM registrations WHERE class_id=? ORDER BY id",(cid,)).fetchall()]
            c.close()
            return self.send_json({"registrations":regs})
        self.send_json({"error":"not found"},404)

    def _publish_failures(self):
        """Jobs that exhausted their retries. These are the dashboard alerts: the
        class is still approved, only this publishing step needs attention."""
        c = db()
        rows = [dict(r) for r in c.execute("""
            SELECT j.id, j.class_id, j.platform, j.attempts, j.last_error, j.updated,
                   cl.title AS class_title, cl.status AS class_status
            FROM job_queue j LEFT JOIN classes cl ON cl.id = j.class_id
            WHERE j.status='failed' ORDER BY j.updated DESC LIMIT 50""").fetchall()]
        c.close()
        for r in rows: r["platform_label"] = PLATFORM_LABEL.get(r["platform"], r["platform"])
        return rows

    def _classes(self, where, args=()):
        c = db()
        rows = c.execute(f"""SELECT c.*, u.name AS instructor_name, ra.name AS reviewing_admin_name,
                             f.enrollment AS fb_enrollment, f.materials AS fb_materials,
                             f.teach_again AS fb_teach_again, f.notes AS fb_notes,
                             f.submitted_at AS fb_submitted_at
                             FROM classes c
                             JOIN users u ON u.id=c.instructor_id
                             LEFT JOIN users ra ON ra.id=c.reviewing_admin_id
                             LEFT JOIN class_feedback f ON f.class_id=c.id
                             {where} {'AND' if where.strip() else 'WHERE'} c.deleted_at IS NULL
                             ORDER BY c.created DESC""", args).fetchall()
        out=[]
        for r in rows:
            d=dict(r); d["supplies"]=json.loads(d["supplies"] or "[]"); d["external_ids"]=json.loads(d["external_ids"] or "{}")
            d["sessions"]=json.loads(d.get("session_dates") or "[]")
            d["is_series"]=bool(d.get("is_series"))
            d["alcohol"]=bool(d["alcohol"]); d["promoted"]=bool(d.get("promoted"))
            d["enrolled"]=enrollment(c, d["id"])
            out.append(d)
        c.close()
        return out

    # -- POST api --
    def api_post(self, p):
        if p == "/api/login":
            b = self.read_json()
            c = db(); row = c.execute("SELECT * FROM users WHERE email=? AND deleted_at IS NULL",
                                      (b.get("email","").strip().lower(),)).fetchone()
            if not row:
                c.close(); return self.send_json({"error":"No account for that email."},401)
            h,_ = hash_pw(b.get("password",""), row["pw_salt"])
            if h != row["pw_hash"]:
                c.close(); return self.send_json({"error":"Wrong password."},401)
            access, refresh, csrf = new_session(c, row["id"])
            c.commit(); c.close()
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto","") == "https" else ""
            return self.send_json({"user":{"name":row["name"],"role":row["role"],"email":row["email"]},
                                   "expires_in": ACCESS_TTL, "csrf_token": csrf},
                                  cookie=session_cookies(access, refresh, secure))
        if p == "/api/refresh":
            # Single-use rotation: the presented refresh token is revoked and a brand
            # new access+refresh pair is issued.
            rtok = self.cookie("gibby_refresh")
            if not rtok: return self.send_json({"error":"no refresh token"},401, cookie=CLEAR_COOKIES)
            c = db()
            row = c.execute("""SELECT s.*, u.deleted_at AS user_deleted FROM sessions s
                               JOIN users u ON u.id = s.user_id WHERE s.refresh_token=?""",(rtok,)).fetchone()
            if not row or row["user_deleted"]:
                c.close(); return self.send_json({"error":"invalid refresh token"},401, cookie=CLEAR_COOKIES)
            if row["revoked_at"]:
                if row["revoked_reason"] == "rotated":
                    # A single-use token presented twice after it was already spent
                    # means it leaked. Kill every session this user has.
                    revoke_all_for_user(c, row["user_id"], "(refresh token reuse detected)", reason="reuse")
                    c.commit(); c.close()
                    print(f"[session] REFRESH REUSE for user {row['user_id']}: all sessions revoked")
                    return self.send_json({"error":"That sign-in was used somewhere else. For safety you have been signed out everywhere. Please sign in again."},
                                          401, cookie=CLEAR_COOKIES)
                # logged out, archived, or signed out everywhere: just refuse
                c.close()
                return self.send_json({"error":"Your session has ended. Please sign in again."},
                                      401, cookie=CLEAR_COOKIES)
            if row["refresh_expires_at"] and row["refresh_expires_at"] <= now():
                revoke_session(c, refresh=rtok, reason="expired"); c.commit(); c.close()
                return self.send_json({"error":"Your session has expired. Please sign in again."},
                                      401, cookie=CLEAR_COOKIES)
            revoke_session(c, token=row["token"], refresh=rtok, reason="rotated")   # old pair dies here
            access, refresh, csrf = new_session(c, row["user_id"], rotated_from=row["token"])
            urow = c.execute("SELECT name,role,email FROM users WHERE id=?",(row["user_id"],)).fetchone()
            c.commit(); c.close()
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto","") == "https" else ""
            return self.send_json({"ok":True, "user":dict(urow), "expires_in": ACCESS_TTL,
                                   "csrf_token": csrf},
                                  cookie=session_cookies(access, refresh, secure))
        if p == "/api/logout":
            # Revoke BOTH halves server-side. The next request with either token fails.
            access_tok, refresh_tok = self.cookie("gibby_session"), self.cookie("gibby_refresh")
            if access_tok or refresh_tok:
                c = db()
                if access_tok:  revoke_session(c, token=access_tok, reason="logout")
                if refresh_tok: revoke_session(c, refresh=refresh_tok, reason="logout")
                c.commit(); c.close()
            return self.send_json({"ok":True}, cookie=CLEAR_COOKIES)
        if p == "/api/logout-everywhere":
            u = self.current_user()
            if not u: return self.send_json({"error":"not signed in"},401)
            c = db(); n = revoke_all_for_user(c, u["id"], "(signed out everywhere)", reason="logout"); c.commit(); c.close()
            return self.send_json({"ok":True, "revoked":n}, cookie=CLEAR_COOKIES)
        if p.startswith("/api/archive/"):
            # Soft delete / restore. Nothing is ever removed from the database; a row
            # with deleted_at set simply stops appearing in normal queries.
            u = self.require("admin")
            if not u: return
            parts = p.split("/")            # /api/archive/{table}/{id}/{delete|restore}
            if len(parts) != 6: return self.send_json({"error":"not found"},404)
            table, rid, action = parts[3], parts[4], parts[5]
            if table not in ("slots","classes","users") or action not in ("delete","restore"):
                return self.send_json({"error":"not found"},404)
            try: rid = int(rid)
            except ValueError: return self.send_json({"error":"bad id"},400)
            if table == "users" and rid == u["id"]:
                return self.send_json({"error":"You cannot archive your own account."},400)
            c = db()
            row = c.execute(f"SELECT * FROM {table} WHERE id=?", (rid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            if action == "delete":
                c.execute(f"UPDATE {table} SET deleted_at=? WHERE id=? AND deleted_at IS NULL", (now(), rid))
                if table == "classes":
                    # give the slots back so the time can be rebooked
                    sids = json.loads(row["slot_ids"] or "[]")
                    if sids:
                        c.execute(f"UPDATE slots SET status='available' WHERE id IN ({','.join('?'*len(sids))})", sids)
                    audit(c, rid, row["status"], "archived", u["id"])
                if table == "users":
                    revoke_all_for_user(c, rid, "(account archived)", reason="archived")   # sign them out now
            else:
                c.execute(f"UPDATE {table} SET deleted_at=NULL WHERE id=?", (rid,))
                if table == "classes": audit(c, rid, "archived", row["status"], u["id"])
            c.commit(); c.close()
            print(f"[archive] {u['name']} {action}d {table} #{rid}")
            return self.send_json({"ok":True})
        if p.startswith("/api/jobs/") and p.endswith("/retry"):
            u = self.require("admin")
            if not u: return
            jid = int(p.split("/")[3]); c = db()
            got = c.execute("""UPDATE job_queue SET status='queued', attempts=0, next_run_at=?, updated=?
                               WHERE id=? AND status='failed'""", (now(), now(), jid)).rowcount
            c.commit(); c.close()
            if got != 1: return self.send_json({"error":"That job is not in a failed state."},409)
            print(f"[queue] job #{jid} manually re-queued by {u['name']}")
            return self.send_json({"ok":True})
        if p == "/api/jobs":       # full queue, for diagnosis
            u = self.require("admin")
            if not u: return
            c = db()
            rows = [dict(r) for r in c.execute("""
                SELECT j.*, cl.title AS class_title FROM job_queue j
                LEFT JOIN classes cl ON cl.id=j.class_id ORDER BY j.id DESC LIMIT 200""").fetchall()]
            c.close()
            for r in rows: r["platform_label"] = PLATFORM_LABEL.get(r["platform"], r["platform"])
            return self.send_json({"jobs": rows})
        if p == "/api/client-error":
            # Deliberately unauthenticated: boundaries must be able to report a
            # failure that happened before or during sign-in. Fields are truncated
            # and the table is capped so this cannot be used to fill the disk.
            b = self.read_json()
            def cut(k, n): return (str(b.get(k) or ""))[:n]
            c = db()
            c.execute("""INSERT INTO client_errors(at,section,message,stack,path,email,role,agent)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (now(), cut("section",80), cut("message",500), cut("stack",4000),
                       cut("path",200), cut("email",120), cut("role",20), cut("agent",200)))
            c.execute("""DELETE FROM client_errors WHERE id NOT IN
                         (SELECT id FROM client_errors ORDER BY id DESC LIMIT 500)""")
            c.commit(); c.close()
            print(f"[client error] {cut('section',80)}: {cut('message',300)} "
                  f"(user={cut('email',120) or 'anonymous'} path={cut('path',200)})")
            return self.send_json({"ok": True})
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
            # An account IS its email: it is the login, and where invites, approvals,
            # reminders and password resets go. Nothing gets created without a real one.
            if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
                return self.send_json({"error":"A real email address is required. It is how they sign in and where every notification goes."},400)
            if role not in ("instructor","admin"):
                return self.send_json({"error":"Role must be instructor or admin."},400)
            # No password supplied means "invite them": mint an unusable random one and
            # email a set-your-password link instead of an admin ever knowing a password.
            invited = not pw
            if invited: pw = secrets.token_urlsafe(16)
            mc = 1 if b.get("must_change_pw", True) else 0
            h, s = hash_pw(pw); c = db()
            if c.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone():
                # Re-adding a deactivated person revives their account rather than
                # failing on the unique email.
                if invited:   # re-invite must not clobber a password they already set
                    c.execute("UPDATE users SET name=?, role=?, deleted_at=NULL WHERE email=?",(name,role,email))
                else:
                    c.execute("""UPDATE users SET name=?, role=?, pw_hash=?, pw_salt=?, must_change_pw=?,
                                 deleted_at=NULL WHERE email=?""",(name,role,h,s,mc,email))
                action = "updated"
            else:
                c.execute("INSERT INTO users(name,email,role,pw_hash,pw_salt,must_change_pw) VALUES(?,?,?,?,?,?)",(name,email,role,h,s,mc))
                action = "created"
            if invited:
                row = c.execute("SELECT id FROM users WHERE email=?",(email,)).fetchone()
                tok = secrets.token_urlsafe(24)
                exp = (datetime.datetime.now()+datetime.timedelta(days=7)).isoformat()
                c.execute("INSERT INTO password_resets(token,user_id,expires) VALUES(?,?,?)",(tok,row["id"],exp))
                proto = self.headers.get("X-Forwarded-Proto","http"); host = self.headers.get("Host","localhost:8000")
                mailer.send(email, "You're set up on the Gibby Class Manager",
                    f"Hi {name.split()[0] if name != email else 'there'},\n\n"
                    f"An account has been created for you on the Gibby Class Manager, where you claim "
                    f"time slots and submit your classes.\n\n"
                    f"Set your password here (link valid for 7 days):\n\n{proto}://{host}/?reset={tok}\n\n"
                    f"After that, sign in any time with this email address.\n\nThe Gibby")
            c.commit(); c.close()
            return self.send_json({"ok":True,"action":action,"email":email,"role":role,"invited":invited})
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
            b = self.read_json()
            name = (b.get("name") or "").strip()
            if not name: return self.send_json({"error":"Name is required."},400)
            photo = b.get("photo")
            if photo is not None:
                photo = str(photo)
                if photo and not photo.startswith("data:image/"):
                    return self.send_json({"error":"That does not look like an image."},400)
                if len(photo) > 400_000:
                    return self.send_json({"error":"That photo is too large. Try a smaller one."},400)
            address = b.get("address")
            if address is not None: address = str(address).strip()[:200]
            skills = b.get("skills")
            if skills is not None:
                if not isinstance(skills, list):
                    return self.send_json({"error":"skills must be a list"},400)
                skills = [str(x).strip()[:40] for x in skills if str(x).strip()][:20]
            c = db()
            c.execute("UPDATE users SET name=? WHERE id=?",(name,u["id"]))
            if photo is not None:
                c.execute("UPDATE users SET photo=? WHERE id=?",(photo or None,u["id"]))
            if skills is not None:
                c.execute("UPDATE users SET skills=? WHERE id=?",(json.dumps(skills),u["id"]))
            if address is not None:
                c.execute("UPDATE users SET address=? WHERE id=?",(address,u["id"]))
            c.commit(); c.close()
            return self.send_json({"ok":True})
        mm = re.match(r"^/api/classes/(\d+)/sign-contract$", p)
        if mm:
            u = self.require("instructor")
            if not u: return
            b = self.read_json()
            name = (b.get("name") or "").strip()
            addr = (b.get("address") or "").strip()
            if not name: return self.send_json({"error":"Type your full name; it serves as your signature."},400)
            if not addr: return self.send_json({"error":"Your address is required on the contract."},400)
            sig = str(b.get("signature") or "")
            if not sig.startswith("data:image/"):
                return self.send_json({"error":"Please sign in the signature box too."},400)
            if len(sig) > 200_000:
                return self.send_json({"error":"That signature drawing is too large. Tap Clear and sign again."},400)
            c = db()
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(int(mm.group(1)),)).fetchone()
            if not row or row["instructor_id"] != u["id"]:
                c.close(); return self.send_json({"error":"That is not your class."},403)
            if row["contract_status"] == "signed":
                c.close(); return self.send_json({"error":"This contract is already signed."},400)
            if row["contract_status"] != "sent":
                c.close(); return self.send_json({"error":"There is no contract waiting on this class."},400)
            c.execute("""UPDATE classes SET contract_status='signed', contract_name=?,
                         contract_address=?, contract_signed_at=?, contract_signature=? WHERE id=?""",
                      (name[:120], addr[:200], now(), sig, row["id"]))
            # remember the address on their profile too, so next time it prefills
            if not (u.get("address") or "").strip():
                c.execute("UPDATE users SET address=? WHERE id=?",(addr[:200], u["id"]))
            c.commit(); c.close()
            print(f"[contract] signed: class #{row['id']} by {name}")
            first = (u.get("name") or "").split(" ")[0] or "there"
            mailer.send(u["email"], f"Your signed contract for {row['title']}",
                f"Hi {first},\n\nThank you! Your instructor contract for \"{row['title']}\" is signed "
                f"and on file at The Gibby. Here is your copy for your records.\n\n"
                f"Signed by: {name}\nAddress: {addr}\nDate: {now()[:10]}\n\n"
                f"{'-'*40}\n{row['contract_text'] or ''}\n{'-'*40}\n\nThe Gibby")
            return self.send_json({"ok":True})
        mm = re.match(r"^/api/users/(\d+)/ask-to-teach$", p)
        if mm:
            u = self.require("admin")
            if not u: return
            b = self.read_json(); msg = (b.get("message") or "").strip()
            c = db(); row = c.execute("SELECT * FROM users WHERE id=? AND deleted_at IS NULL",(int(mm.group(1)),)).fetchone()
            c.close()
            if not row: return self.send_json({"error":"No such person."},404)
            first = (row["name"] or "").split(" ")[0] or "there"
            proto = self.headers.get("X-Forwarded-Proto","http"); host = self.headers.get("Host","localhost:8000")
            body = (f"Hi {first},\n\n{u['name']} at The Gibby would love for you to teach a class.\n\n"
                    + (f"{msg}\n\n" if msg else "")
                    + f"If you're interested, log in and grab an open time slot:\n{proto}://{host}\n\nThe Gibby")
            sent = mailer.send(row["email"], "Would you teach a class at The Gibby?", body)
            return self.send_json({"ok":True, "delivered":bool(sent), "to":row["email"]})
        if p == "/api/forgot":
            email = (self.read_json().get("email","") or "").strip().lower()
            c = db(); row = c.execute("SELECT id FROM users WHERE email=? AND deleted_at IS NULL",(email,)).fetchone()
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
        if p == "/api/slots/generate":
            # A whole season in one click, since the calendar feed is blocked by the
            # organisation's Workspace policy. Idempotent: existing slots are skipped,
            # so re-running with wider hours only adds the new ones.
            u = self.require("admin")
            if not u: return
            b = self.read_json()
            try:
                first = datetime.date.fromisoformat((b.get("start_date") or "").strip())
            except ValueError:
                return self.send_json({"error":"Please pick the first date."},400)
            weeks = max(1, min(int(b.get("weeks") or 1), 30))
            open_h = max(6, min(int(b.get("open_hour") or 9), 22))
            close_h = max(open_h + 1, min(int(b.get("close_hour") or 17), 23))
            rooms = [r for r in (b.get("rooms") or []) if r in ("Large Room", "Studio")]
            if not rooms:
                return self.send_json({"error":"Pick at least one room."},400)
            c = db()
            existing = {(r["date"], r["start"], r["room"]) for r in
                        c.execute("SELECT date, start, room FROM slots WHERE deleted_at IS NULL")}
            added = skipped = 0
            for w in range(weeks):
                d = first + datetime.timedelta(days=7 * w)
                label = day_label(d)          # real weekday from the real date
                t = datetime.datetime(d.year, d.month, d.day, open_h, 0)
                close = datetime.datetime(d.year, d.month, d.day, close_h, 0)
                step = datetime.timedelta(minutes=30)
                while t + step <= close:
                    st = t.strftime("%I:%M %p").lstrip("0")
                    en = (t + step).strftime("%I:%M %p").lstrip("0")
                    for room in rooms:
                        if (label, st, room) in existing:
                            skipped += 1
                        else:
                            c.execute("INSERT INTO slots(date,start,end,room,status,source) "
                                      "VALUES(?,?,?,?,'available','manual')", (label, st, en, room))
                            existing.add((label, st, room)); added += 1
                    t += step
            c.commit(); c.close()
            last = first + datetime.timedelta(days=7 * (weeks - 1))
            print(f"[slots] {u['name']} generated {added} slot(s), {first} to {last}, rooms {rooms}")
            return self.send_json({"ok":True, "added":added, "skipped":skipped,
                                   "first":day_label(first), "last":day_label(last)})
        if p == "/api/slots":  # admin create
            u = self.require("admin");
            if not u: return
            b = self.read_json()
            c=db(); c.execute("INSERT INTO slots(date,start,end,room) VALUES(?,?,?,?)",
                (b.get("date"),b.get("start"),b.get("end"),b.get("room","Large Room")))
            c.commit(); c.close(); return self.send_json({"ok":True})
        if p == "/api/series-preview":
            # Show the instructor exactly which dates a run would land on, including
            # any weeks skipped because they were already booked, BEFORE submitting.
            u = self.require("instructor")
            if not u: return
            b = self.read_json()
            ids = [int(x) for x in (b.get("slot_ids") or [])]
            weeks = max(2, min(int(b.get("weeks") or 2), 26))
            if not ids: return self.send_json({"error":"Pick the first session first."},400)
            c = db(); sessions, skipped = find_series_sessions(c, ids, weeks); c.close()
            if sessions is None:
                return self.send_json({"error":"Those slots are no longer available."},400)
            return self.send_json({"ok":True, "sessions":sessions, "skipped":skipped,
                                   "requested":weeks, "found":len(sessions)})
        if p == "/api/drafts":
            # Save a work-in-progress form. No validation at all - the whole point is
            # to keep a half-finished proposal. Nothing is published or claimed.
            u = self.require("instructor")
            if not u: return
            b = self.read_json()
            payload = b.get("payload") or {}
            did = b.get("id")
            c = db()
            if did:
                own = c.execute("SELECT id FROM drafts WHERE id=? AND instructor_id=? AND deleted_at IS NULL",
                                (did, u["id"])).fetchone()
                if not own: c.close(); return self.send_json({"error":"That draft is no longer there."},404)
                c.execute("""UPDATE drafts SET title=?,payload=?,slot_ids=?,slot_date=?,slot_time=?,room=?,
                             is_series=?,session_count=?,updated=? WHERE id=?""",
                          ((payload.get("title") or "").strip()[:200], json.dumps(payload),
                           json.dumps(b.get("slot_ids") or []), b.get("slot_date"), b.get("slot_time"),
                           b.get("room"), 1 if b.get("is_series") else 0, b.get("session_count") or 1,
                           now(), did))
            else:
                n = c.execute("SELECT COUNT(*) FROM drafts WHERE instructor_id=? AND deleted_at IS NULL",
                              (u["id"],)).fetchone()[0]
                if n >= 20:
                    c.close(); return self.send_json({"error":"You already have 20 saved drafts. Delete one first."},400)
                c.execute("""INSERT INTO drafts(instructor_id,title,payload,slot_ids,slot_date,slot_time,room,
                             is_series,session_count,created,updated)
                             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                          (u["id"], (payload.get("title") or "").strip()[:200], json.dumps(payload),
                           json.dumps(b.get("slot_ids") or []), b.get("slot_date"), b.get("slot_time"),
                           b.get("room"), 1 if b.get("is_series") else 0, b.get("session_count") or 1,
                           now(), now()))
                did = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.commit(); c.close()
            return self.send_json({"ok":True, "id":did, "saved_at":now()})
        if p.startswith("/api/drafts/") and p.endswith("/delete"):
            u = self.require("instructor")
            if not u: return
            try: did = int(p.split("/")[3])
            except ValueError: return self.send_json({"error":"not found"},404)
            c = db()
            c.execute("UPDATE drafts SET deleted_at=? WHERE id=? AND instructor_id=? AND deleted_at IS NULL",
                      (now(), did, u["id"]))
            c.commit(); c.close()
            return self.send_json({"ok":True})
        if p == "/api/classes":  # instructor submit (this is also where slots get claimed)
            u = self.require("instructor")
            if not u: return
            if self.rate_limited("submit", u["id"]): return
            b = self.read_json()
            req = ["title","age_range","length","room"]
            miss=[k for k in req if not str(b.get(k,"")).strip()]
            if len((b.get("description") or "").split()) < 50: miss.append("description (at least 50 words)")
            if not b.get("photo"): miss.append("photo")
            def _num(k):
                try: return float(b.get(k))
                except (TypeError, ValueError): return None
            mx, mn = _num("max_p"), _num("min_p")
            if not (mx and mx > 0): miss.append("max_p")
            if not (mn and mn > 0): miss.append("min_p")
            if mx and mn and mn > mx: miss.append("min_p (cannot exceed max)")
            if not (_num("ticket_price") or 0) > 0: miss.append("ticket_price")
            waives = 1 if b.get("waives_pay") else 0
            # $0 pay is allowed only as a deliberate choice (donating their time);
            # otherwise a zero is almost always the calculator left untouched.
            if not waives and not (_num("instructor_pay") or 0) > 0: miss.append("instructor_pay")
            if waives and (_num("instructor_pay") or 0) < 0: miss.append("instructor_pay")
            mc = _num("material_cost")
            if mc is None or mc < 0: miss.append("material_cost")
            # The booked window includes setup and cleanup; the class itself must be
            # marked inside it, because students are told the CLASS time.
            cs, ce = (b.get("class_start") or "").strip(), (b.get("class_end") or "").strip()
            try:
                if not cs or not ce or tmin(cs) >= tmin(ce):
                    miss.append("class start and end time")
            except ValueError:
                miss.append("class start and end time")
            if miss: return self.send_json({"error":"Missing required fields","fields":miss},400)
            c=db()
            ids = b.get("slot_ids") or ([b["slot_id"]] if b.get("slot_id") else [])
            ids = [int(x) for x in ids]
            slot_date, slot_time, room = b.get("slot_date"), b.get("slot_time"), b.get("room")
            is_series = 1 if b.get("is_series") else 0
            weeks = max(2, min(int(b.get("session_count") or 2), 26)) if is_series else 1
            sessions = []
            if ids:
                # Slot claiming rides on this same endpoint, so it gets its own bucket.
                if self.rate_limited("claim", u["id"]): c.close(); return
                ph = ",".join("?"*len(ids))
                rows = [dict(r) for r in c.execute(f"SELECT * FROM slots WHERE id IN ({ph}) AND deleted_at IS NULL", ids).fetchall()]
                if len(rows) != len(ids):
                    c.close(); return self.send_json({"error":"One of those slots no longer exists."},400)
                # The FIRST session is always one day, one room, back-to-back. A series
                # then repeats that shape on later weeks.
                if len({r["date"] for r in rows}) != 1 or len({r["room"] for r in rows}) != 1:
                    c.close(); return self.send_json({"error":"Slots must be the same day and same room."},400)
                rows.sort(key=lambda r: tmin(r["start"]))
                for a, nxt in zip(rows, rows[1:]):
                    if tmin(a["end"]) != tmin(nxt["start"]):
                        c.close(); return self.send_json({"error":"Slots must be back-to-back (consecutive)."},400)
                if is_series:
                    sessions, _skipped = find_series_sessions(c, ids, weeks)
                    if not sessions:
                        c.close(); return self.send_json({"error":"Those slots are no longer available."},400)
                    if len(sessions) < 2:
                        c.close(); return self.send_json({"error":"Could not find enough open weeks for a series. Try a different start date."},400)
                    ids = [i for s in sessions for i in s["slot_ids"]]     # every slot across the run
                    ph = ",".join("?"*len(ids))
                else:
                    sessions = [{"date": rows[0]["date"], "start": rows[0]["start"],
                                 "end": rows[-1]["end"], "slot_ids": [r["id"] for r in rows]}]
                if tmin(cs) < tmin(rows[0]["start"]) or tmin(ce) > tmin(rows[-1]["end"]):
                    c.close(); return self.send_json({"error":"Class times must sit inside your booked window."},400)
                # The month must have unlocked (hiding it in the picker is not enough).
                # Only the FIRST session matters: a series may legitimately run on into
                # months that have not opened yet.
                if u["role"] == "instructor":
                    p0 = parse_day(rows[0]["date"])
                    if p0 and not month_is_visible(c, p0.month):
                        c.close(); return self.send_json({"error":"Those dates have not opened for booking yet. A new month opens on the last day of every month."},400)
                # RACE-SAFE: claim ALL slots for every session atomically; if any got
                # taken first, rowcount < N, we roll back (no commit) and reject.
                claimed = c.execute(f"UPDATE slots SET status='claimed' WHERE id IN ({ph}) AND status='available' AND deleted_at IS NULL", ids).rowcount
                if claimed != len(ids):
                    c.close(); return self.send_json({"error":"One of those slots was just claimed by someone else. Please reselect."},409)
                slot_date = rows[0]["date"]
                room = b.get("room") or rows[0]["room"]  # calendar slots are roomless; take room from the form
                slot_time = rows[0]["start"] + " – " + rows[-1]["end"]
                weeks = len(sessions)
            c.execute("""INSERT INTO classes(title,instructor_id,slot_date,slot_time,room,description,age_range,
                alcohol,max_p,min_p,ticket_price,instructor_pay,supplies,headline,subtitle,photo,
                length,pre_class,own_materials,material_cost,needs_volunteer,slot_ids,links,
                is_series,session_count,session_dates,age_label,close_days,status,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, ?,?,?,?,?,?,?, ?,?,?,?,?, 'pending', ?)""",
                (b.get("title"),u["id"],slot_date,slot_time,room,
                 b.get("description"),b.get("age_range"),1 if b.get("alcohol") else 0,
                 b.get("max_p"),b.get("min_p"),b.get("ticket_price"),b.get("instructor_pay"),
                 json.dumps(b.get("supplies",[])),b.get("headline",""),b.get("subtitle",""),b.get("photo"),
                 b.get("length",""),b.get("pre_class",""),1 if b.get("own_materials") else 0,
                 b.get("material_cost"),1 if b.get("needs_volunteer") else 0, json.dumps(ids), b.get("links",""),
                 is_series, weeks, json.dumps(sessions), age_label(b.get("age_range")),
                 max(0, min(int(b.get("close_days") or 0), 30)), now()))
            new_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            c.execute("UPDATE classes SET class_time=? WHERE id=?", (f"{cs} \u2013 {ce}", new_id))
            pm = b.get("pay_model")
            if waives:
                # Donated time: teaching pay is fixed at whatever remains (their own
                # materials reimbursement, or zero), never the split formula.
                c.execute("""UPDATE classes SET waives_pay=1, pay_model='flat',
                             instructor_pay=COALESCE(instructor_pay,0) WHERE id=?""", (new_id,))
            elif pm in ("flat", "split"):
                c.execute("UPDATE classes SET pay_model=? WHERE id=?", (pm, new_id))
            audit(c, c.execute("SELECT last_insert_rowid()").fetchone()[0], None, "pending", u["id"])
            if b.get("resubmit_of"):
                # A fixed version of a sent-back class: retire the old one so the
                # instructor's list does not show both.
                try: old = int(b["resubmit_of"])
                except (TypeError, ValueError): old = 0
                if old:
                    c.execute("""UPDATE classes SET deleted_at=? WHERE id=? AND instructor_id=?
                                 AND status='incomplete' AND deleted_at IS NULL""",(now(), old, u["id"]))
            if b.get("draft_id"):        # the proposal is submitted; retire its draft
                c.execute("UPDATE drafts SET deleted_at=? WHERE id=? AND instructor_id=?",
                          (now(), b["draft_id"], u["id"]))
            admins = emails_for(c, "WHERE role='admin'")
            c.commit(); c.close()
            when = (f"{weeks} sessions starting {slot_date}" if is_series else f"{slot_date} {slot_time}")
            mailer.send(admins, "New class submission",
                f"{u['name']} submitted \"{b.get('title')}\" for {when}. Review it in the app.")
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and p.endswith("/approve"):
            return self.decide(p, approve=True)
        if p.startswith("/api/classes/") and p.endswith("/incomplete"):
            return self.decide(p, approve=False)
        if p.startswith("/api/classes/") and p.endswith("/review-open"):
            # An admin opened this submission. Record who and when, so the queue can
            # warn the next admin before they collide. Advisory only: the real
            # protection is the compare-and-swap in decide().
            u = self.require("admin")
            if not u: return
            cid = int(p.split("/")[3]); c = db()
            row = c.execute("SELECT status, reviewing_admin_id, review_started_at FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            other = None
            if row["reviewing_admin_id"] and row["reviewing_admin_id"] != u["id"]:
                o = c.execute("SELECT name FROM users WHERE id=?",(row["reviewing_admin_id"],)).fetchone()
                other = {"name": o["name"] if o else "another admin", "since": row["review_started_at"]}
            # only claim a submission that is actually still awaiting a decision
            if row["status"] == "pending":
                c.execute("UPDATE classes SET reviewing_admin_id=?, review_started_at=? WHERE id=?",
                          (u["id"], now(), cid))
            c.commit(); c.close()
            return self.send_json({"ok":True, "status":row["status"], "also_reviewing":other})
        if p.startswith("/api/classes/") and p.endswith("/poster"):
            # Attach a poster made by hand. This is the fallback when Canva autofill
            # is not available, and it must be as good as the automatic path.
            u = self.require("admin")
            if not u: return
            cid = int(p.split("/")[3]); b = self.read_json(); c = db()
            row = c.execute("SELECT id FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            img = (b.get("image") or "").strip()
            if img and not img.startswith("data:image/"):
                c.close(); return self.send_json({"error":"That does not look like an image."},400)
            if len(img) > 8_000_000:      # ~6MB of actual image
                c.close(); return self.send_json({"error":"That image is too large. Please use one under 5MB."},400)
            c.execute("UPDATE classes SET poster=? WHERE id=?", (img or None, cid))
            c.commit(); c.close()
            print(f"[poster] class #{cid}: {'attached by ' + u['name'] if img else 'removed by ' + u['name']}")
            return self.send_json({"ok":True, "attached": bool(img)})
        if p.startswith("/api/classes/") and p.endswith("/graphic"):   # save poster text + rebuild it
            u = self.require("admin")
            if not u: return
            cid = p.split("/")[3]; b = self.read_json(); c = db()
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            head = (b.get("headline") or "").strip() or row["title"]
            sub  = (b.get("subtitle") or "").strip()
            c.execute("UPDATE classes SET headline=?, subtitle=? WHERE id=?",(head, sub, cid))
            cls = {**dict(row), "headline": head, "subtitle": sub}
            c.commit(); c.close()
            threading.Thread(target=render_graphic_async, args=(cls,), daemon=True).start()
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and p.endswith("/publish"):   # admin approved the graphic -> go live
            u = self.require("admin")
            if not u: return
            cid = int(p.split("/")[3]); c = db()
            begin_immediate(c)      # same lock as decide(): two admins cannot both publish
            try:
                row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
                if not row:
                    c.execute("ROLLBACK"); c.close(); return self.send_json({"error":"not found"},404)
                cls = dict(row)
                if cls["status"] != "graphic_review":
                    who = decided_by(c, cid, "graphic_review")   # the publisher, not the approver
                    c.execute("ROLLBACK"); c.close()
                    msg = (f"This submission was already published by {who}." if who and cls["status"]=="approved"
                           else "This class is not waiting on graphic review.")
                    return self.send_json({"error":msg, "status":cls["status"]}, 409)
                # Only a publish-phase job blocks a second publish. The Canva poster
                # render also sets publishing_in_progress, and that must NOT stop an
                # admin publishing a class whose poster is still building.
                busy = c.execute("""SELECT COUNT(*) FROM job_queue WHERE class_id=?
                                    AND platform<>'canva' AND status IN ('queued','running')""",
                                 (cid,)).fetchone()[0]
                if busy:
                    c.execute("ROLLBACK"); c.close()
                    return self.send_json({"error":"Publishing is already running for this class.",
                                           "status":cls["status"]}, 409)
                # CAS: only one publisher can move graphic_review -> approved
                if c.execute("UPDATE classes SET status='approved' WHERE id=? AND status='graphic_review'",
                             (cid,)).rowcount != 1:
                    c.execute("ROLLBACK"); c.close()
                    return self.send_json({"error":"This class was just published by another admin.","status":"approved"},409)
                pending_side_effects = publish_now(c, cls, u["id"], spawn=False)
                c.execute("COMMIT")
            except Exception:
                try: c.execute("ROLLBACK")
                except Exception: pass
                c.close(); raise
            c.close()
            run_publish_side_effects(pending_side_effects)
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and p.endswith("/feedback"):
            # Three questions, asked once, private to The Gibby.
            u = self.require("instructor")
            if not u: return
            cid = int(p.split("/")[3]); b = self.read_json(); c = db()
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            if u["role"] != "admin" and row["instructor_id"] != u["id"]:
                c.close(); return self.send_json({"error":"That is not your class."},403)
            valid = {"enrollment": {"too_few","about_right","too_many"},
                     "materials":  {"enough","short","too_much"},
                     "teach_again":{"yes","maybe","no"}}
            answers = {}
            for k, allowed in valid.items():
                v = (b.get(k) or "").strip()
                if v not in allowed:
                    c.close(); return self.send_json({"error":f"Please answer all three questions."},400)
                answers[k] = v
            c.execute("""INSERT INTO class_feedback(class_id,instructor_id,enrollment,materials,
                         teach_again,notes,submitted_at) VALUES(?,?,?,?,?,?,?)
                         ON CONFLICT(class_id) DO UPDATE SET enrollment=excluded.enrollment,
                         materials=excluded.materials, teach_again=excluded.teach_again,
                         notes=excluded.notes, submitted_at=excluded.submitted_at""",
                      (cid, row["instructor_id"], answers["enrollment"], answers["materials"],
                       answers["teach_again"], (b.get("notes") or "").strip()[:2000], now()))
            c.commit(); c.close()
            print(f"[feedback] class #{cid}: enrollment={answers['enrollment']} "
                  f"materials={answers['materials']} again={answers['teach_again']}")
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and p.endswith("/followup-note"):
            # Instructor writes (or re-writes) the note. save=true keeps a draft;
            # otherwise it goes to an admin for approval.
            u = self.require("instructor")
            if not u: return
            cid = int(p.split("/")[3]); b = self.read_json(); c = db()
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            if u["role"] != "admin" and row["instructor_id"] != u["id"]:
                c.close(); return self.send_json({"error":"That is not your class."},403)
            note = (b.get("note") or "").strip()
            if not b.get("save") and len(note) < 20:
                c.close(); return self.send_json({"error":"Please write a little more before sending it for approval."},400)
            status = "awaiting_instructor" if b.get("save") else "pending_admin"
            c.execute("""UPDATE classes SET followup_note=?, followup_status=?, followup_submitted_at=?
                         WHERE id=?""", (note, status, None if b.get("save") else now(), cid))
            admins = emails_for(c, "WHERE role='admin'") if not b.get("save") else []
            c.commit(); c.close()
            if admins:
                mailer.send(admins, f"Follow-up note to review: {row['title']}",
                    f"{u['name']} has written the after-class note for \"{row['title']}\".\n\n"
                    f"Review and send it from Approvals.")
            return self.send_json({"ok":True, "status":status})
        if p.startswith("/api/classes/") and p.endswith("/followup-return"):
            u = self.require("admin")
            if not u: return
            cid = int(p.split("/")[3]); b = self.read_json(); c = db()
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            c.execute("UPDATE classes SET followup_status='awaiting_instructor' WHERE id=?", (cid,))
            instr = c.execute("SELECT name,email FROM users WHERE id=?",(row["instructor_id"],)).fetchone()
            c.commit(); c.close()
            if instr:
                mailer.send(instr["email"], f"Please revise your note: {row['title']}",
                    f"Hi {(instr['name'] or '').split(' ')[0]},\n\nAn admin has asked for a change to your "
                    f"after-class note for \"{row['title']}\".\n\n"
                    f"{('Their note: ' + b.get('note')) if b.get('note') else ''}\n\nThanks,\nThe Gibby")
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and p.endswith("/followup-send"):
            # Admin approves. THIS is the only place students are mailed.
            u = self.require("admin")
            if not u: return
            cid = int(p.split("/")[3]); b = self.read_json(); c = db()
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            cls = dict(row)
            note = (b.get("note") or cls.get("followup_note") or "").strip()
            recipients, attended = followup_audience(c, cid)
            instr = c.execute("SELECT name FROM users WHERE id=?",(cls["instructor_id"],)).fetchone()
            cfg = mailer.load_email_config()
            subj, body = mailer.tmpl_followup(cls, cfg, attended=attended, note=note,
                                              instructor_name=(instr["name"] if instr else ""))
            sent, why = send_class_email(c, cls, "followup", recipients, subj, body, None, cfg)
            if sent:
                c.execute("""UPDATE classes SET followup_note=?, followup_status='sent', followed_up=1
                             WHERE id=?""", (note, cid))
            c.commit(); c.close()
            return self.send_json({"ok":sent, "reason":why, "sent_to":len(recipients) if sent else 0,
                                   "attendance_known":attended})
        if p.startswith("/api/classes/") and p.endswith("/sync-registrations"):
            u = self.require("admin")
            if not u: return
            cid = int(p.split("/")[3])
            r = sync_registrations(cid)
            if r is None:
                return self.send_json({"ok":False,
                    "error":"Nothing to sync: this class has no Eventbrite event yet, or Eventbrite is not connected."})
            return self.send_json({"ok":True, **r})
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
            cls = dict(c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone())
            students = [r[0] for r in c.execute("SELECT email FROM registrations WHERE class_id=?",(cid,)).fetchall()]
            instr = c.execute("SELECT email FROM users WHERE id=?",(cls["instructor_id"],)).fetchone()
            c.execute("UPDATE classes SET status='cancelled' WHERE id=?",(cid,))
            audit(c, cid, cls.get("status"), "cancelled", u["id"])
            c.execute("UPDATE registrations SET refunded=1 WHERE class_id=?",(cid,))
            print(f"[cancel] class #{cid} -> would refund all via Eventbrite")
            cancelled = {**cls, "status": "cancelled"}   # guards must see the NEW status
            subj, body = mailer.tmpl_cancel(cls)
            send_class_email(c, cancelled, "cancel", students, subj, body)
            send_class_email(c, cancelled, "cancel_instructor", [instr[0]] if instr else [],
                             "Your class was cancelled", body)
            c.commit(); c.close()
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and (p.endswith("/remind") or p.endswith("/followup")):
            u = self.require("admin")
            if not u: return
            cid = p.split("/")[3]; kind = p.rsplit("/",1)[1]; c=db()
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            cls = dict(row)
            students = [r[0] for r in c.execute("SELECT email FROM registrations WHERE class_id=? AND refunded=0",(cid,)).fetchall()]
            cfg = mailer.load_email_config()
            # Same guards as the automated job: right status, not already sent, still upcoming.
            etype = "reminder" if kind == "remind" else "followup"
            if kind == "remind":
                subj, body = mailer.tmpl_reminder(cls, cfg)
            else:
                # honour check-in data and the instructor's note here too
                students, attended = followup_audience(c, cid)
                subj, body = mailer.tmpl_followup(cls, cfg, attended=attended,
                                                  note=(cls.get("followup_note") or ""))
            sent, why = send_class_email(c, cls, etype, students, subj, body, None, cfg)
            c.commit(); c.close()
            return self.send_json({"ok":sent, "sent":len(students) if sent else 0, "reason":why})
        if p.startswith("/api/classes/") and p.endswith("/edit"):   # admin edits then returns for instructor approval
            u = self.require("admin")
            if not u: return
            cid = p.split("/")[3]; b = self.read_json()
            c = db(); row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            sets, vals = [], []
            for k in ("title","description","age_range","headline","subtitle"):
                if k in b: sets.append(f"{k}=?"); vals.append(b[k])
            if "age_range" in b:                       # keep the published phrase in sync
                sets.append("age_label=?"); vals.append(age_label(b["age_range"]))
            for k in ("max_p","min_p","ticket_price","instructor_pay","close_days"):
                if k in b: sets.append(f"{k}=?"); vals.append(b[k])
            if b.get("pay_model") in ("flat", "split"):
                sets.append("pay_model=?"); vals.append(b["pay_model"])
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
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
            if not row: c.close(); return self.send_json({"error":"not found"},404)
            cls = dict(row)
            if cls["instructor_id"] != u["id"] or cls["status"] != "instructor_review":
                c.close(); return self.send_json({"error":"not allowed"},403)
            start_graphic_review(c, cls, u["id"]); c.commit(); c.close()
            return self.send_json({"ok":True})
        if p.startswith("/api/classes/") and p.endswith("/instructor-decline"):   # instructor rejects admin edits -> back to admin
            u = self.require("instructor")
            if not u: return
            cid = p.split("/")[3]; b = self.read_json(); c = db()
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL",(cid,)).fetchone()
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
        """Approve or send back a pending submission, under a lock so that two admins
        deciding at the same moment cannot both win. Network work (emails, the Canva
        render) happens AFTER the commit so it never runs holding the write lock."""
        u = self.require("admin")
        if not u: return
        if self.rate_limited("approve", u["id"]): return
        cid = int(p.split("/")[3])
        b = self.read_json()
        target = "graphic_review" if approve else "incomplete"
        after_commit = None

        c = db()
        begin_immediate(c)                  # write lock for the whole decision
        try:
            row = c.execute("SELECT * FROM classes WHERE id=? AND deleted_at IS NULL", (cid,)).fetchone()
            if not row:
                c.execute("ROLLBACK"); c.close()
                return self.send_json({"error": "not found"}, 404)
            cls = dict(row)

            # Guard 1: is it still pending? Guard 2 (the CAS below): did we win?
            if cls["status"] != "pending":
                msg = lost_race_message(c, cls)
                c.execute("ROLLBACK"); c.close()
                print(f"[decision race] class #{cid}: {u['name']} was too late. {msg}")
                return self.send_json({"error": msg, "status": cls["status"]}, 409)

            # Compare-and-swap. Only one caller can turn 'pending' into the target.
            if c.execute("UPDATE classes SET status=? WHERE id=? AND status='pending'",
                         (target, cid)).rowcount != 1:
                fresh = dict(c.execute("SELECT * FROM classes WHERE id=?", (cid,)).fetchone())
                msg = lost_race_message(c, fresh)
                c.execute("ROLLBACK"); c.close()
                print(f"[decision race] class #{cid}: {u['name']} lost the compare-and-swap. {msg}")
                return self.send_json({"error": msg, "status": fresh["status"]}, 409)

            c.execute("UPDATE classes SET reviewing_admin_id=NULL, review_started_at=NULL WHERE id=?", (cid,))
            instr = dict(c.execute("SELECT * FROM users WHERE id=?", (cls["instructor_id"],)).fetchone())
            # (instr is loaded before the branch: both paths need it)
            if approve:
                prepared = start_graphic_review(c, cls, u["id"], spawn=False)
                ctext = build_contract_text(cls, instr["name"])
                c.execute("""UPDATE classes SET contract_status='sent', contract_text=? WHERE id=?""",
                          (ctext, cid))
                after_commit = ("render", prepared, instr, cls, ctext)
            else:
                c.execute("UPDATE classes SET admin_note=? WHERE id=?", (b.get("note",""), cid))
                audit(c, cid, "pending", "incomplete", u["id"])
                sids = json.loads(cls.get("slot_ids") or "[]")     # release the claimed slots
                if sids:
                    c.execute(f"UPDATE slots SET status='available' WHERE id IN ({','.join('?'*len(sids))})", sids)
                after_commit = ("email", mailer.tmpl_incomplete(cls, instr, b.get("note","")), instr["email"])
            c.execute("COMMIT")
        except Exception:
            try: c.execute("ROLLBACK")
            except Exception: pass
            c.close(); raise
        c.close()

        if after_commit and after_commit[0] == "render":
            threading.Thread(target=render_graphic_async, args=(after_commit[1],), daemon=True).start()
            # The contract goes out the moment the approval lands.
            _, _, instr2, cls2, ctext2 = after_commit
            first = (instr2.get("name") or "").split(" ")[0] or "there"
            proto2 = self.headers.get("X-Forwarded-Proto","http"); host2 = self.headers.get("Host","localhost:8000")
            mailer.send(instr2["email"], f"Action needed: sign your contract for {cls2['title']}",
                f"Hi {first},\n\nGreat news: \"{cls2['title']}\" has been approved!\n\n"
                f"One quick step before it goes live: read and sign your instructor contract in the app.\n\n"
                f"  1. Log in: {proto2}://{host2}\n"
                f"  2. Open My classes\n"
                f"  3. Tap Read and sign\n\n"
                f"It takes about a minute. Once you sign, we will email you a copy for your records.\n\nThe Gibby")
        elif after_commit and after_commit[0] == "email":
            (subj, body), to = after_commit[1], after_commit[2]
            mailer.send(to, subj, body)
        return self.send_json({"ok": True})

def now(): return datetime.datetime.now().isoformat(timespec="seconds")

class Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()   # daily lifecycle automations
    threading.Thread(target=queue_worker, daemon=True).start()     # outbound posting + retries
    print(f"Gibby Class Manager running:  http://localhost:{PORT}   (data: {DB})")
    print("Sign in as admin:      jess@theeverett.org")
    print("Sign in as instructor: christin.smiertka@theeverett.org  (first.last of any roster name)")
    if SEED_PW_GENERATED:
        print("\n  *** No SEED_PASSWORD set, so a random one was generated for any accounts")
        print(f"      seeded on this run:   {SEED_PW}")
        print("      It is shown only here. Everyone must change it at first sign-in.")
        print("      Set SEED_PASSWORD in the environment to choose it yourself. ***\n")
    Threaded(("0.0.0.0", PORT), H).serve_forever()
