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

  if (body.action === 'contract') {
    var folder;
    var it = DriveApp.getFoldersByName('Gibby Contracts');
    folder = it.hasNext() ? it.next() : DriveApp.createFolder('Gibby Contracts');
    var f = folder.createFile(String(body.filename || 'contract.html'),
                              String(body.html || ''), 'text/html');
    return reply({ ok: true, id: f.getId(), link: f.getUrl() });
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
