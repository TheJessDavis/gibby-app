# Gibby Class Manager

Class-lifecycle app for The Gibby (Gibby Center for the Arts, Middletown DE, part of The Everett).
Instructors book studio time, submit classes, sign contracts, and follow up with students.
Admins approve, and the app publishes to Eventbrite, Facebook, the website, Google Calendar and
DelawareScene, then runs the emails, paperwork, orders and money on its own.

Live: https://gibby-app-ddjo.onrender.com (Render, Docker, persistent disk at /data).
Owner: Jess Davis (jdavis@theeverett.org). Emails go out as gibby@everetttheatre.com.

## Stack

Pure Python 3 standard library, no packages to install. SQLite. One HTML file for the whole UI.

| File | What it is |
|---|---|
| `server.py` | HTTP server, every API endpoint, the hourly scheduler, schema and migrations (in `init_db`) |
| `web/index.html` | The entire front end: CSS, markup and JS in one file, phone-first |
| `mailer.py` | Email: Gmail Apps Script bridge first, SMTP fallback, branded HTML, dedupe, background queue |
| `integrations.py` | Eventbrite, Facebook, Canva, DelawareScene, discount codes |
| `gcal.py` | Google Calendar bridge (open slots in, approved classes out) |
| `pdfgen.py` | Contract PDFs |
| `scripts/gcal-webhook.gs`, `scripts/mail-bridge.gs` | Mirrors of the two Google Apps Scripts (see Bridges) |
| `web/site-embed.js` | Script the website loads to list classes |
| `DEPLOY.md` | Hosting and environment variables |

## Run it locally

```bash
SEED_PASSWORD=localtest123 python3 server.py
```

Then open http://127.0.0.1:8000. Local accounts: admin `jess@theeverett.org`, instructors
`allison.medley@theeverett.org`, `christin.smiertka@theeverett.org`, `danielle.sims@theeverett.org`,
all with the seed password. Class 1 has 9 registrations, class 3 has 0. Without bridge keys, email is
a DRY-RUN that prints to the console, and posting is off.

## Deploy

```bash
sed -i '' 's/^VERSION = .*/VERSION = "X.Y.Z-short-name"/' server.py
python3 -m py_compile server.py
git add -A && git commit -m "..." && git push
```

Render builds on push; poll `GET /api/version` until it reports the new string (about 60 to 90 s).
Every release so far has bumped VERSION and followed this exact pattern. Before committing, remove any
helper scripts you dropped into the repo. A pre-commit hook blocks anything that looks like a secret;
mark false positives with `# pragma: allowlist secret`.

## Admin API for checks

```bash
curl -s -c jar -H 'Content-Type: application/json' -d '{"email":"...","password":"..."}' https://gibby-app-ddjo.onrender.com/api/login
```

The reply carries `csrf_token`; send it as `X-CSRF-Token` with the cookie jar on every POST.
Useful reads: `/api/dashboard`, `/api/classes/all`, `/api/report/money`, `/api/email-log`,
`/api/client-errors` (what the UI error boundaries caught), `/api/audit?class_id=N`.

## Bridges (Google Apps Scripts)

The app cannot use Google APIs directly, so two Apps Scripts act as bridges. The `.gs` files in
`scripts/` are mirrors; the deployed code lives in the Google accounts.

- **Gibby Calendar Bridge** on jdavis@everetttheatre.com: calendar events, Drive filing (contracts,
  class photos, paperwork, backups), the Materials and Treasurer spreadsheets. Currently Version 19.
  The `sheet` action takes `name` and `tab`; the `photo` action takes `root` (top folder).
- **Gibby Mail Bridge** on gibby@everetttheatre.com: sends every email as that mailbox. Version 2.

To change one: edit in the Apps Script editor, save, Deploy > Manage deployments > pencil > New
version > Deploy. Only the account owner can grant a new OAuth scope. Apps Script answers 404 now and
then for one call; the app retries only when the request never reached the script, because retrying
after a timeout is how people once got the same email five times.

## Environment variables

See DEPLOY.md. Keys live in Render's environment, never in the repo. Names the code reads:
EVENTBRITE_TOKEN, EVENTBRITE_ORG_ID, EVENTBRITE_ORGANIZER_ID, EVENTBRITE_VENUE_ID, FB_PAGE_ID,
FB_PAGE_TOKEN, FB_TOKEN_EXPIRES, GCAL_WEBHOOK_URL, GCAL_WEBHOOK_KEY, GCAL_ICS_URL, MAIL_BRIDGE_URL,
MAIL_BRIDGE_KEY, MAIL_FROM, SMTP_*, CANVA_*, SEED_PASSWORD, DATA_DIR, APP_URL, SEASON_START.
Some settings are also editable in the app under More > Email and More > Integrations and are stored
in the `meta` table (thank-you settings, deadline reminders, supply ordering address, mail bridge).

## Rules the owner has set (do not undo)

- Nothing publishes to Facebook or goes to students without an admin pressing the button. Photos
  become drafts admins approve.
- Every email the app sends is copied to Jess (`copy_all` setting). Instructors get a copy of
  emails they send. Thank-you emails are copied one per student.
- Emails come from gibby@everetttheatre.com. Instructor emails set Reply-To to the instructor.
- The instructor questionnaire always shows and is never required.
- Materials from the class form compile into the Gibby Materials spreadsheet, not into order
  requests to admins.
- Class requests post quietly unless the admin chooses "Post it and email everyone".
- No em dashes anywhere in copy.
- Never handle passwords or payment details for the owner.
- Background checks are paused (meta `bg_paused`, default on) until The Gibby settles the process:
  no requests, reminders or instructor cards for that kind. Untick "Paused" under People to resume.
- No "Final numbers" headcount email at the registration cutoff, and no "A week out and not
  full" promote nudge (both removed Sep 21, 2026). The under-minimum decision email stays.

## Interface conventions (Sep 2026 calm-down pass)

- Colour means status only: `.card.needs` / `cls-back` / `cls-review` pink (needs you), `cls-pending` /
  `.waiting` yellow (waiting on someone), `cls-approved` / `.done` green, `cls-off` / `.past` grey;
  every other card is white. No rotating pastels.
- `alert()` is a toast (`toast()`); `confirm()` stays for irreversible actions. No emoji on `.btn`
  buttons; emoji live on the tab bar, section headers and list rows only.
- Section headers are `#tab > label` in Summer Fresh. Explainer paragraphs (`h2 + p.sub`,
  `label + p.sub`) show once per screen then fold behind a "?" (`foldHints`, localStorage `hint:*`).
- Long lists are `details.clsrow` rows that open to the full card (My classes, Requests, People,
  Learn, Opportunities). Instructor extras live in the "Ask The Gibby" menu on My classes. Admin
  More is grouped: Classes, Money, Reach, Records. Money has three tabs (`MONEY_TAB`).
- Six tabs is the phone limit for instructors; put new things inside existing screens.
- Retired screens (Sep 22, 2026), code kept but unlinked: Slots (`adminSlots`, the manual slot
  fallback if the calendar bridge dies), Calendar view, Templates (`adminTemplates`; the class form's
  template picker is gone, "copy a past class" stays), Calendar vs app (`calreview`) and Eventbrite
  import (`ebimport`), both still reachable by `renderAdmin('...')`. Deleted: the old per-class
  reimbursement endpoints and the `/embed` iframe page. "Worth running again" flags now show on All
  classes and the class card instead of a Templates queue.

## How the main flows work

- **Booking**: `slots` table from the Gibby calendar. Fall months are open; later months unlock at
  month end, or early via `open_through` under Email settings. A series claims the same window on
  later weeks (`find_series_sessions`); instructors can untick dates. Resubmitting a sent-back class
  frees its own slots first.
- **Pricing**: instructor enters what they hope to make per student; ticket = materials + that / 0.6.
  Supply links (url, price, qty, name) are required unless they buy their own. Donation-based makes
  an Eventbrite donation ticket.
- **Admin "Edit this class"** (`openLiveEdit` / `update-live`): every field, plus the class's own
  start and end inside the booked window (pushed to Eventbrite via `update_eventbrite_times`, the
  calendar is recreated, the website reads the DB) and all three images (landscape poster to
  Eventbrite, portrait poster and class photo to the website via `/class-photo/`). Moving the date
  or the booked window is the reschedule flow.
- **Approval**: pending -> graphic_review -> approved (`publish_now`) which posts everywhere and
  sends the contract. Send back = incomplete. Admin "Save changes" on the edit form is a quiet
  save; "Send to instructor" needs their approval.
- **After class** (hourly scheduler, `run_scheduler`): day +1 asks the instructor for a note and
  up to five photos; day +2 thank-you to students with note, photos, review links and a personal
  one-use Eventbrite discount code (`thanks_codes`, org-wide, 30 days); day +3 asks for marketing
  photos and video. Open studios and empty classes are exempt (`followup_exempt`). Instructors can
  decline the note.
- **Opportunities** (`class_requests.kind` teach / help / sell / design): teach is claimed and
  pre-fills a proposal; the others take a raised hand (`request_interest`), design takes a full
  proposal with photos and a supply list; admins confirm or decline. A Help card is created
  automatically when an approved class asked for a volunteer.
- **Collab** (`class_requests.kind='collab'`, posted by instructors at `/api/requests/collab`): an event
  idea with about (description), vision (notes), a date picked from open calendar dates (when_text,
  room). Other artists raise a hand; the proposer (created_by) confirms or declines at the interest
  endpoints, which allow the creator for this kind. Admins see it under Requests > Collab.
- **Paperwork**: W-9, background check, phone, lockbox contract. Admins request per person; instructors
  complete in the app; files go to Drive under Gibby Paperwork; reminder after 3 days. The lockbox
  contract (`LOCKBOX_CONTRACT_DEFAULT`, editable in meta) is signed with a typed name into the
  `lockbox` table. The app never holds the code: each signature emails Michelle Truban (meta
  `lockbox_to`, set under People > Lockbox) the instructor's name and email, and she sends the code.
- **Email limits** (`mailer.LIMITS`, table `mail_sent` in the app database): automated mail may go
  to the same person with the same subject once per 24 h, and at most 40 an hour / 200 a day in
  total; past that it stops and the watch address gets one "[Limit]" email. Mail a person triggers
  (origin "user", worked out from the request thread) is not capped. Counts show under More > Email.
- **Scheduler safety**: `send_class_email` commits its email_log claim at once, and `run_scheduler`
  handles each class in its own try/commit (`_one`). Errors land in meta `scheduler_errors`, read
  with `GET /api/admin/scheduler-errors`. (Before this, one crash rolled back every claim and the
  same alerts went out hourly.)
- **Help cards**: `ensure_help_card` runs at approval and every scheduler tick for approved classes
  with `needs_volunteer`, so any class asking for an assistant appears under Opportunities > Help.
- **Art for All donation add-on**: wanted on every Eventbrite event, but the API cannot create
  add-ons, and a donation ticket type counts toward capacity (it inflated every event to +500 and
  was removed on Sep 19). Only Eventbrite's own UI can add a true add-on; do not retry via tickets.
- **Thank-yous** go to everyone who registered; check-in scans never narrow the list.
- **Meet Our Teaching Artists** (approved by Marketing, Lou Booker, Sep 20 2026): `users.headshot`
  (square, min 600px; `headshot_by` artist|gibby, admins can upload Marketing's standardized one),
  `users.bio` built from three fixed prompts in `bio_parts` (40 to 80 words in all). Public feed
  `/embed/instructors.json` includes each artist's upcoming approved classes (Eventbrite links tagged
  `aff=site-artists`), images at `/headshot/{id}.jpg`, preview at `/embed/instructors`.
  `site-embed.js` fills a Squarespace Code block containing `<div id="gibby-instructors"></div>`.
  Marketing review (Sep 22): the site shows only `pub_bio` / `pub_headshot_web`; any bio or headshot
  change sets `web_review='pending'` and emails `web_review_to()` (default Lou Booker,
  lbooker@theeverett.org) once; admins approve or ask for a change under More > Reach > Website
  reviews (`/api/admin/web-review/{id}/approve|changes`). Marketing's own upload publishes at once.
  `users.pronouns` show with the name. The page intro is meta `artists_intro` (Email settings).
- **Reimbursements** (Reimburse tab, `reimb_requests` + `reimb_files`): the Everett's Expense
  Reimbursement & Check Request filled in the app. Class picker limited to the instructor's own
  classes, dated lines by category (`REIMB_CATEGORIES`), receipts required (photo or PDF), delivery
  choice. On submit: form PDF via `pdfgen.contract_pdf`, PDF and receipts filed under "Gibby
  Reimbursements" on Drive, emailed with attachments to `reimb_to()` (default Tina Johnson
  tjohnson@theeverett.org and Michelle Truban; editable under More > Email), copy to the
  instructor. Admins see the list under Money and tick Paid. The old per-class "Get paid back for
  supplies" flow (`reimbursements` table) is retired; its route answers 410.
- **Incident reports** (`incident_reports`): The Everett 2026 Incident Report filled in the app from
  "Report an incident" on My classes. PDF via `pdfgen.contract_pdf` to Drive under "Gibby Incident
  Reports", emailed with the PDF to `incident_to()` (default Michelle Truban mtruban@theeverett.org and
  Seth Cosans scosans@everetttheatre.com; editable under More > Email), copy to the reporter. Admins
  read them under More > Incidents.
- **Ticket sources**: Eventbrite `aff=` codes are translated by `channel_meaning`. "Share a tracked
  link" on a class card tags links by channel (`gibby-fb`, `gibby-ig`, ...).

## Testing

There is no test suite. Changes are verified by running the server locally and driving the real
pages headlessly with Chrome DevTools Protocol (a stdlib client, `cdp.py`, is easy to rewrite:
start Chrome with `--headless=new --remote-debugging-port=9777`, open `/json/new`, speak the
websocket protocol, `Runtime.evaluate` and `Page.captureScreenshot`). Log in through the real form
(`#le`, `#lp`, `doLogin()`), stub `window.alert` and `window.confirm`, then call page functions
directly. Clean up any rows you seed in `gibby.db`. The local submit rate limit is 12 per hour per
instructor; switch test users when it trips.
