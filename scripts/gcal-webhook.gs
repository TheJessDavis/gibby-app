/**
 * Gibby Class Manager <-> Google Calendar bridge.
 *
 * Why this exists: the everetttheatre.com Workspace blocks service account keys,
 * the calendar's secret iCal address is hidden, and only the calendar's owner
 * can make it public. This script runs AS THE PERSON WHO DEPLOYS IT (who has
 * access to the Gibby calendar) and does both directions:
 *
 *   GET  ?key=...   -> the calendar's busy times as an iCal feed (no titles),
 *                      so the app can post open slots that stay current.
 *   POST {key,...}  -> creates (or deletes) events, so approved classes land
 *                      on the calendar automatically.
 *
 * One-time setup (about 3 minutes):
 *   1. Go to script.google.com while signed in as the Everett account and
 *      create a New project. Delete the sample code, paste this whole file.
 *   2. Change SHARED_KEY below to a long random phrase. Save.
 *   3. Deploy -> New deployment -> type: Web app.
 *      Execute as: Me. Who has access: Anyone. Click Deploy and Authorize.
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
var FEED_START = new Date('2026-11-01T00:00:00-05:00');
var FEED_END = new Date('2027-12-31T23:59:59-05:00');

function doGet(e) {
  if (!e || !e.parameter || e.parameter.key !== SHARED_KEY) {
    return ContentService.createTextOutput('missing or wrong key');
  }
  var cal = CalendarApp.getCalendarById(CALENDAR_ID);
  if (!cal) return ContentService.createTextOutput('calendar not found');
  var events = cal.getEvents(FEED_START, FEED_END);
  var lines = ['BEGIN:VCALENDAR', 'VERSION:2.0',
               'PRODID:-//Gibby Class Manager//live busy feed//EN'];
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
      lines.push('DTSTART:' +
        Utilities.formatDate(ev.getStartTime(), 'UTC', "yyyyMMdd'T'HHmmss'Z'"));
      lines.push('DTEND:' +
        Utilities.formatDate(ev.getEndTime(), 'UTC', "yyyyMMdd'T'HHmmss'Z'"));
    }
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

  var cal = CalendarApp.getCalendarById(CALENDAR_ID);
  if (!cal) return reply({ error: 'calendar not found or no access' });

  if (body.action === 'create') {
    var ids = [];
    (body.events || []).forEach(function (ev) {
      var created = cal.createEvent(
        String(ev.title || 'Gibby class'),
        new Date(ev.start), new Date(ev.end),
        { description: String(ev.description || ''), location: String(ev.location || '') });
      ids.push(created.getId());
    });
    return reply({ ok: true, ids: ids });
  }

  if (body.action === 'delete') {
    var removed = 0;
    (body.ids || []).forEach(function (id) {
      try { cal.getEventById(id).deleteEvent(); removed++; } catch (err) {}
    });
    return reply({ ok: true, removed: removed });
  }

  return reply({ error: 'unknown action' });
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}
