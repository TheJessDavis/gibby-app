/**
 * Gibby Class Manager -> Google Calendar webhook.
 *
 * Why this exists: the everetttheatre.com Workspace blocks service account keys,
 * so the app cannot write to the calendar directly. This tiny script runs AS THE
 * PERSON WHO DEPLOYS IT (who already has write access to the Gibby calendar) and
 * accepts events from the app.
 *
 * One-time setup (about 3 minutes):
 *   1. Go to script.google.com while signed in as jdavis@everetttheatre.com
 *      and create a New project. Delete the sample code, paste this whole file.
 *   2. Change SHARED_KEY below to a long random phrase (anything, like a strong
 *      password). Save.
 *   3. Deploy -> New deployment -> type: Web app.
 *      Execute as: Me. Who has access: Anyone. Click Deploy and Authorize.
 *   4. Copy the web app URL it gives you.
 *   5. On Render, add two environment variables:
 *        GCAL_WEBHOOK_URL  = that URL
 *        GCAL_WEBHOOK_KEY  = the same phrase you put in SHARED_KEY
 *        GCAL_LIVE         = true
 *
 * After that, every published class lands on the Gibby calendar automatically,
 * one event per session for series courses.
 */

var SHARED_KEY = 'CHANGE-ME-to-a-long-random-phrase';
var CALENDAR_ID = 'everetttheatre.com_ck9si1lmol1aqpmdaqn075us7o@group.calendar.google.com';

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
