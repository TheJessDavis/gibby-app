# Gibby App Ownership and Continuity Checklist

This document is for tracking the operational handoff of the Gibby App repo and its live production environment.

Repository: https://github.com/thegibbycfta/gibby-app

## 1. Ownership and access transfer

- [ ] Confirm repository ownership is with the organization, not a personal account.
- [ ] Add at least 2–3 trusted maintainers with admin access.
- [ ] Enable GitHub branch protection on `main`.
- [ ] Require 2FA on all maintainers.
- [ ] Confirm who will be the final owner of the repo and deployment.
- [ ] Remove the original developer from org admin access when the handoff is complete.
- [ ] Create a backup fork or mirror under the organization account.
- [ ] Confirm who can approve production changes and deploy code.

## 2. Hosting and deployment

- [ ] Confirm the active hosting provider (`Render`, `Fly.io`, or another platform).
- [ ] Confirm the production app URL.
- [ ] Confirm the deployment account owner and who has access.
- [ ] Confirm the deployment method used by the app.
- [ ] Confirm whether the app is running from GitHub and whether deployment is automated.
- [ ] Confirm the app's production port and runtime behavior.
- [ ] Confirm database persistence setup.
- [ ] Confirm the persistent volume path (for example `/data`).
- [ ] Confirm whether the SQLite database survives restarts and redeploys.
- [ ] Confirm a backup strategy for the database and any uploaded files.
- [ ] Confirm rollback steps if the app fails after deployment.

## 3. Production secrets and environment variables

Check each of the following in the production deployment dashboard and confirm who owns each item.

Core variables:
- [ ] `PORT`
- [ ] `DATA_DIR`
- [ ] `SEED_PASSWORD`

Email variables:
- [ ] `EMAIL_LIVE`
- [ ] `SMTP_HOST`
- [ ] `SMTP_PORT`
- [ ] `SMTP_USER`
- [ ] `SMTP_PASS`
- [ ] `MAIL_FROM`

External posting variables:
- [ ] `GIBBY_LIVE`
- [ ] `EVENTBRITE_TOKEN`
- [ ] `EVENTBRITE_ORG_ID`
- [ ] `EVENTBRITE_ORGANIZER_ID`
- [ ] `FB_PAGE_ID`
- [ ] `FB_PAGE_TOKEN`
- [ ] `WIX_API_KEY`
- [ ] `WIX_SITE_ID`
- [ ] `CANVA_TOKEN`
- [ ] `CANVA_TEMPLATE_ID`

Google Calendar variables:
- [ ] `GCAL_LIVE`
- [ ] `GCAL_CALENDAR_ID`
- [ ] `GOOGLE_SERVICE_ACCOUNT_JSON`
- [ ] `GCAL_ICS_URL`
- [ ] `GCAL_DAYS`
- [ ] `GCAL_OPEN_HOUR`
- [ ] `GCAL_CLOSE_HOUR`

Other variables referenced in the repo:
- [ ] `APP_URL`
- [ ] `SEASON_START`
- [ ] `MAIL_BRIDGE_URL`
- [ ] `MAIL_BRIDGE_KEY`
- [ ] `GCAL_WEBHOOK_URL`
- [ ] `GCAL_WEBHOOK_KEY`

Notes:
- [ ] Confirm that no secret is hard-coded in the repo.
- [ ] Confirm that `config.json` is not committed or exposed.
- [ ] Confirm that production values are documented in one shared location.

## 4. Google Workspace and Google Apps Script continuity

This app relies on Google Apps Script bridges rather than direct Google API access.

- [ ] Identify the Google account that owns the calendar bridge.
- [ ] Identify the Google account that owns the mail bridge.
- [ ] Confirm the script URLs are still valid and active.
- [ ] Confirm the Google account still has access to the relevant calendar.
- [ ] Confirm the Gmail mailbox used to send messages still exists and is active.
- [ ] Confirm the correct OAuth permissions are still granted.
- [ ] Confirm any Google Drive folders used for contracts, paperwork, or class files still exist.
- [ ] Confirm the original developer is not the only person who can edit or redeploy the scripts.
- [ ] Confirm the script source in `scripts/` still matches the live deployed version.
- [ ] Confirm the bridge URLs are not stale or pointing to a dead Apps Script deployment.

## 5. External service ownership

Check these live systems and list the owner for each.

- [ ] Eventbrite account and token owner
- [ ] Facebook page owner and token owner
- [ ] Google Calendar owner / shared calendar owner
- [ ] Gmail or SMTP account owner
- [ ] Canva account owner
- [ ] DelawareScene publishing ownership (if used)
- [ ] Any admin or support mailbox tied to this app

## 6. Local run and smoke test

- [ ] Clone the repo locally.
- [ ] Confirm the repo boots with a local command.
- [ ] Confirm local startup command works (`python3 server.py` or equivalent).
- [ ] Set a local `SEED_PASSWORD` for known test accounts.
- [ ] Confirm the app launches locally at the expected address.
- [ ] Confirm seeded user accounts work as documented.
- [ ] Confirm dashboard loads with no fatal errors.
- [ ] Confirm admin login works.
- [ ] Confirm class list loads.
- [ ] Confirm a basic class flow works.
- [ ] Confirm email is in dry-run mode when external email keys are missing.
- [ ] Confirm posting is off when external live keys are missing.

## 7. Operational documentation and runbook

- [ ] Record the repository URL and ownership details.
- [ ] Record the production hosting provider.
- [ ] Record the production URL.
- [ ] Record the database location and backup policy.
- [ ] Record the named credentials and where they live.
- [ ] Record the Google bridge owner and script details.
- [ ] Record the admin email addresses and operational contacts.
- [ ] Record the local startup and troubleshooting steps.
- [ ] Record the rollback steps for an outage or bad deployment.
- [ ] Record who is allowed to approve live changes.

## 8. Business rules and protected workflows

This app has operational rules that should not be accidentally changed.

- [ ] Review `CLAUDE.md` and confirm the current business rules are still valid.
- [ ] Confirm any current policy changes are documented.
- [ ] Confirm no code or workflow changes undo critical safeguards.
- [ ] Confirm admin approval gates remain in place.
- [ ] Confirm email send rules are still correct.
- [ ] Confirm no live post goes out without the expected admin action.
- [ ] Confirm permission and review flows still match the current organization process.

## 9. Production validation checklist

After ownership transfer and deployment access is confirmed:

- [ ] App starts successfully in production.
- [ ] Admin login works.
- [ ] Dashboard loads.
- [ ] Class creation/editing works.
- [ ] Approval path works.
- [ ] Calendar integration works if enabled.
- [ ] Email sending works in the correct mode.
- [ ] Eventbrite integration works if enabled.
- [ ] Facebook posting works if enabled.
- [ ] No data is lost after restart or redeploy.
- [ ] Backup or restore path is documented and tested.

## 10. Risk log

Use this section to track unresolved issues or important items.

- [ ] Item 1:
- [ ] Item 2:
- [ ] Item 3:

## 11. Final handoff status

- [ ] Repository ownership transferred and confirmed.
- [ ] Production hosting access transferred and confirmed.
- [ ] Secrets and credentials inventoried and documented.
- [ ] Google Workspace and Apps Script access transferred and confirmed.
- [ ] Database persistence and backup confirmed.
- [ ] Local startup validated.
- [ ] Production smoke test passed.
- [ ] Runbook created and shared with the team.
- [ ] Handoff considered complete.

## Notes

This app is not just a codebase—it is a live operations application with external services, Google integrations, and a persistent database. The highest-risk areas are:

- Google account / Apps Script continuity
- production secrets and tokens
- database persistence
- deployment ownership
- live external integration access

The original developer leaving should be treated as a business continuity event, not just a repo cleanup task.

---

Last updated: [YYYY-MM-DD]
Owner / maintainer: [Name]

