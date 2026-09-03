/**
 * Gibby Mail Bridge: sends Gibby Class Manager email AS gibby@everetttheatre.com.
 *
 * Deployed as a web app on the gibby@everetttheatre.com Google account
 * (Execute as: Me, Who has access: Anyone), so every message is sent by that
 * mailbox itself and lands in its Sent folder. No app password involved.
 * The app posts {key, action:'email', to:[...], subject, body, name, attachments:[{filename,mime,b64}]}.
 * Paste this whole file into a new Apps Script project, set SHARED_KEY, deploy,
 * then save the /exec URL and the key under Connections > Email in the app
 * (or MAIL_BRIDGE_URL / MAIL_BRIDGE_KEY on Render).
 */
var SHARED_KEY = 'amber-heron-heron-amber-3065aa98';

function doGet(e) {
  return reply({ ok: true, service: 'gibby-mail-bridge', quota: MailApp.getRemainingDailyQuota() });
}

function doPost(e) {
  var body;
  try { body = JSON.parse(e.postData.contents); }
  catch (err) { return reply({ error: 'not json' }); }
  if (!body.key || body.key !== SHARED_KEY) return reply({ error: 'wrong key' });
  if (body.action !== 'email') return reply({ error: 'unknown action' });
  var opts = { name: String(body.name || 'The Gibby') };
  if (body.replyTo) opts.replyTo = String(body.replyTo);
  if (body.attachments && body.attachments.length) {
    opts.attachments = body.attachments.map(function (a) {
      return Utilities.newBlob(Utilities.base64Decode(String(a.b64 || '')),
        String(a.mime || 'application/octet-stream'), String(a.filename || 'file'));
    });
  }
  var to = (body.to || []).join(',');
  if (!to) return reply({ error: 'no recipient' });
  GmailApp.sendEmail(to, String(body.subject || ''), String(body.body || ''), opts);
  return reply({ ok: true, remaining: MailApp.getRemainingDailyQuota() });
}

function reply(o) {
  return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON);
}

// Editor-run helper that only exists to trigger the Gmail consent prompt.
function authGmail() { Logger.log('sends left today: ' + MailApp.getRemainingDailyQuota()); }
