/**
 * Gibby Class Manager <-> Google Calendar + Drive bridge.
 *
 * This is the readable mirror of the script deployed as "Gibby Calendar Bridge"
 * on the jdavis@everetttheatre.com account (the live copy is minified onto one
 * line; keep this file in sync when the live one changes).
 *
 * Why this exists: the everetttheatre.com Workspace blocks service account keys,
 * the calendar's secret iCal address is hidden, and only the calendar's owner
 * can make it public. This script runs AS THE PERSON WHO DEPLOYS IT (who has
 * access to the Gibby calendar) and does three jobs:
 *
 *   GET  ?key=...   -> the calendar's busy times as an iCal feed (no titles),
 *                      each event tagged X-ROOM so the app knows which room
 *                      it blocks. So the app can post open slots that stay current.
 *   POST {key,action:'create'|'delete'} -> creates or deletes calendar events,
 *                      so approved classes land on the calendar automatically.
 *   POST {key,action:'contract'}        -> files a signed-contract HTML doc in
 *                      the "Gibby Contracts" folder on Drive.
 *   POST {key,action:'email'}           -> sends app email as gibby@everetttheatre.com
 *                      (a Send-mail-as alias of the deploying mailbox).
 *
 * One-time setup (about 3 minutes):
 *   1. Go to script.google.com while signed in as the Everett account and
 *      create a New project. Delete the sample code, paste this whole file.
 *   2. Change SHARED_KEY below to a long random phrase. Save.
 *   3. Deploy -> New deployment -> type: Web app.
 *      Execute as: Me. Who has access: Anyone. Click Deploy and Authorize.
 *      IMPORTANT: on the consent screen make sure the FULL Drive permission
 *      ("See, edit, create, and delete all of your Google Drive files") is
 *      granted. Google's granular consent can silently grant only read-only
 *      Drive, and then the contract action fails with "You do not have
 *      permission to call DriveApp.createFolder" for web calls while editor
 *      runs of read-only helpers still work. If that happens, run authDrive2
 *      below from the editor: it forces a consent prompt for the full scope.
 *   4. Copy the web app URL it gives you (ends in /exec).
 *   5. On Render, set:
 *        GCAL_ICS_URL      = <that URL>?key=<your phrase>
 *        GCAL_WEBHOOK_URL  = <that URL>
 *        GCAL_WEBHOOK_KEY  = <your phrase>
 *        GCAL_LIVE         = true
 */

var SHARED_KEY = 'CHANGE-ME-to-a-long-random-phrase';
var CALENDAR_ID = 'everetttheatre.com_ck9si1lmol1aqpmdaqn075us7o@group.calendar.google.com';
// How far the busy feed looks: the whole season plus a little slack.
var FEED_START = new Date('2026-08-01T00:00:00-04:00');
var FEED_END = new Date('2027-06-30T23:59:59-04:00');

function fmtu(d) {
  var s = Utilities.formatDate(d, 'UTC', 'yyyyMMddHHmmss');
  return s.slice(0, 8) + 'T' + s.slice(8) + 'Z';
}

// Which room an event blocks, read from its title/location. Blank means both.
function roomOf(ev) {
  var t = (ev.getTitle() + ' ' + (ev.getLocation() || '')).toLowerCase();
  if (t.indexOf('studio') > -1) return 'Studio';
  if (t.indexOf('large') > -1 || t.indexOf('stained') > -1) return 'Large Room';
  if (t.indexOf('blocked') > -1 || t.indexOf('board') > -1 || t.indexOf('closed') > -1 ||
      t.indexOf('live stage') > -1 || t.indexOf('private') > -1 || t.indexOf('rental') > -1 ||
      t.indexOf('hire') > -1 || t.indexOf('whole') > -1) return '';
  return 'Studio';
}

function doGet(e) {
  if (!e || !e.parameter || e.parameter.key !== SHARED_KEY) {
    return ContentService.createTextOutput('missing or wrong key');
  }
  var cal = CalendarApp.getCalendarById(CALENDAR_ID);
  if (!cal) return ContentService.createTextOutput('calendar not found');
  var events = cal.getEvents(FEED_START, FEED_END);
  var lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Gibby//busy//EN'];
  for (var i = 0; i < events.length; i++) {
    var ev = events[i];
    lines.push('BEGIN:VEVENT');
    lines.push('UID:busy-' + i + '@gibby-live');
    if (ev.isAllDayEvent()) {
      lines.push('DTSTART;VALUE=DATE:' +
        Utilities.formatDate(ev.getAllDayStartDate(), 'America/New_York', 'yyyyMMdd'));
      lines.push('DTEND;VALUE=DATE:' +
        Utilities.formatDate(ev.getAllDayEndDate(), 'America/New_York', 'yyyyMMdd'));
    } else {
      lines.push('DTSTART:' + fmtu(ev.getStartTime()));
      lines.push('DTEND:' + fmtu(ev.getEndTime()));
    }
    var rm = roomOf(ev);
    if (rm) lines.push('X-ROOM:' + rm);
    // Busy times only. Titles and details stay private on purpose.
    lines.push('SUMMARY:Busy');
    lines.push('END:VEVENT');
  }
  lines.push('END:VCALENDAR');
  return ContentService.createTextOutput(lines.join('\r\n'))
    .setMimeType(ContentService.MimeType.ICAL);
}

function doPost(e) {
  var body;
  try { body = JSON.parse(e.postData.contents); }
  catch (err) { return reply({ error: 'not json' }); }

  if (!body.key || body.key !== SHARED_KEY) return reply({ error: 'wrong key' });

  if (body.action === 'sheet') {
    // Rewrite the master worksheet wholesale: one row per class, idempotent.
    var ss;
    var fs = DriveApp.getFilesByName('Gibby Classes Master Sheet');
    ss = fs.hasNext() ? SpreadsheetApp.open(fs.next())
                      : SpreadsheetApp.create('Gibby Classes Master Sheet');
    var sh = ss.getSheets()[0];
    sh.clearContents();
    var data = [body.headers].concat(body.rows || []);
    sh.getRange(1, 1, data.length, body.headers.length).setValues(data);
    return reply({ ok: true, link: ss.getUrl(), rows: (body.rows || []).length });
  }

  if (body.action === 'contract') {
    var folder;
    var it = DriveApp.getFoldersByName('Gibby Contracts');
    folder = it.hasNext() ? it.next() : DriveApp.createFolder('Gibby Contracts');
    var f = folder.createFile(String(body.filename || 'contract.html'),
                              String(body.html || ''), 'text/html');
    return reply({ ok: true, id: f.getId(), link: f.getUrl() });
  }

  if (body.action === 'list') {
    // Read-only: every event in a window, WITH titles, for the admin's
    // calendar-vs-app review. Only the app (with the key) can ask for this.
    var cal2 = CalendarApp.getCalendarById(CALENDAR_ID);
    if (!cal2) return reply({ error: 'calendar not found' });
    var f = new Date(body.from || FEED_START), t = new Date(body.to || FEED_END);
    var evs = cal2.getEvents(f, t).map(function (ev) {
      return { id: ev.getId(), title: ev.getTitle(), allDay: ev.isAllDayEvent(),
        start: Utilities.formatDate(ev.getStartTime(), 'America/New_York', "yyyy-MM-dd'T'HH:mm"),
        end: Utilities.formatDate(ev.getEndTime(), 'America/New_York', "yyyy-MM-dd'T'HH:mm"),
        location: ev.getLocation() || '' };
    });
    return reply({ ok: true, events: evs });
  }

  if (body.action === 'photo') {
    // After-class photos: Gibby Class Photos / <class title> / <file>.
    var root;
    var rit = DriveApp.getFoldersByName('Gibby Class Photos');
    root = rit.hasNext() ? rit.next() : DriveApp.createFolder('Gibby Class Photos');
    var sub = root, subName = String(body.folder || '').trim();
    if (subName) {
      var sit = root.getFoldersByName(subName);
      sub = sit.hasNext() ? sit.next() : root.createFolder(subName);
    }
    var blob = Utilities.newBlob(Utilities.base64Decode(String(body.b64 || '')),
      String(body.mime || 'image/jpeg'), String(body.filename || 'photo.jpg'));
    var pf = sub.createFile(blob);
    return reply({ ok: true, id: pf.getId(), link: pf.getUrl(), folder: sub.getUrl() });
  }

  if (body.action === 'email') {
    // Send app email as the mailbox that deployed this script, or as one of its
    // Send-mail-as aliases (gibby@everetttheatre.com). No app password needed.
    var me = Session.getEffectiveUser().getEmail();
    var from = String(body.from || '');
    var opts = { name: String(body.name || 'The Gibby') };
    if (from && from.toLowerCase() !== me.toLowerCase()) {
      var aliases = GmailApp.getAliases().map(function (a) { return a.toLowerCase(); });
      if (aliases.indexOf(from.toLowerCase()) < 0) {
        return reply({ error: from + ' is not a Send-mail-as address on ' + me +
          '. In Gmail for ' + me + ' go to Settings > Accounts > Send mail as, add it, then try again.' });
      }
      opts.from = from;
    }
    if (body.attachments && body.attachments.length) {
      opts.attachments = body.attachments.map(function (a) {
        return Utilities.newBlob(Utilities.base64Decode(String(a.b64 || '')),
          String(a.mime || 'application/octet-stream'), String(a.filename || 'file'));
      });
    }
    var to = (body.to || []).join(',');
    if (!to) return reply({ error: 'no recipient' });
    GmailApp.sendEmail(to, String(body.subject || ''), String(body.body || ''), opts);
    return reply({ ok: true, from: opts.from || me, remaining: MailApp.getRemainingDailyQuota() });
  }

  var cal = CalendarApp.getCalendarById(CALENDAR_ID);
  if (!cal) return reply({ error: 'calendar not found' });

  if (body.action === 'create') {
    var ids = [];
    (body.events || []).forEach(function (ev) {
      var c = cal.createEvent(
        String(ev.title || 'Gibby class'),
        new Date(ev.start), new Date(ev.end),
        { description: String(ev.description || ''), location: String(ev.location || '') });
      ids.push(c.getId());
    });
    return reply({ ok: true, ids: ids });
  }

  if (body.action === 'delete') {
    var n = 0;
    (body.ids || []).forEach(function (id) {
      try { cal.getEventById(id).deleteEvent(); n++; } catch (err) {}
    });
    return reply({ ok: true, removed: n });
  }

  return reply({ error: 'unknown action' });
}

function reply(o) {
  return ContentService.createTextOutput(JSON.stringify(o))
    .setMimeType(ContentService.MimeType.JSON);
}

// Editor-run helpers that exist only to trigger Google's consent prompts.
// authDrive asks for (at least) read access; authDrive2 forces the FULL Drive
// scope the contract action needs. Harmless to leave in place.
function authDrive() { Logger.log(DriveApp.getRootFolder().getName()); }
function authDrive2() {
  var f = DriveApp.createFolder('AUTH TEST safe to delete');
  Logger.log(f.getId());
  f.setTrashed(true);
}
// Forces the consent prompt for the Gmail scope the email action needs.
function authGmail() { Logger.log(GmailApp.getAliases().join(', ') || '(no aliases)'); }
// Forces the consent prompt for the Sheets WRITE scope the sheet action needs.
function authSheets() {
  var t = SpreadsheetApp.create('AUTH TEST sheet');
  Logger.log(t.getId());
  DriveApp.getFileById(t.getId()).setTrashed(true);
}
