# SelfEcho AI Community Edition v0.6.0

These notes describe the Public repository's v0.6.0 release-candidate state. They do not claim a hosted deployment, Git tag, or GitHub Release.

## Highlights

- Adds optional Email Reminder beside Web Push, with independent account-level channel eligibility.
- Adds Reminder Email ownership verification and an optional Test Email action.
- Moves fresh installations to SQLite schema v6 and provides an explicit v5→v6 migration for existing v0.5 databases.
- Includes Capture/Voice correctness fixes and keeps the Service Worker cache namespace at `selfecho-ai-community-v0.6`.

## Email Reminder

Community v0.6.0 formally supports operator-configured Tencent SES only. Email is disabled by default, so a normal Community installation does not need Tencent credentials, templates, a sender, a verification pepper, or Tencent network access.

Push and Email are independent; neither is a fallback for the other. An account can use Push only, Email only, both, or neither/in-app only. Scheduled Push and Email require `REMINDER_WORKER_ENABLED=true`; the in-app due lifecycle remains usable without the worker.

Reminder Email addresses must be verified. The login Email is not adopted automatically, and changing the Reminder address requires verification again. The optional Test Email template is not required for normal Email Reminder; without it, only the Test Email action is unavailable. Provider acceptance means submitted/accepted, not recipient delivery.

Tencent SES template data contracts:

- Verification: `{"code":"123456"}`
- Normal Reminder: `{"app_url":"https://your-selfecho.example/dashboard"}`
- Test Email: `{}`

## Upgrade from v0.5.0

v0.6.0 does not automatically migrate a schema v5 database. Stop the application, create and validate a recoverable backup of the database and any WAL/SHM companions, then optionally run:

```bash
python -m app.migrations.v006_email_reminders --database data/selfecho.db --check-only
```

Run the migration with:

```bash
python -m app.migrations.v006_email_reminders --database data/selfecho.db
```

Enter the exact confirmation phrase `MIGRATE PUBLIC V5 TO V6`. Restart v0.6.0 only after migration succeeds, verify that startup accepts schema v6 and existing data loads, and only then enable Email if desired. A fresh v0.6.0 installation creates schema v6 directly.

## Privacy

Verification sends Tencent the recipient Email, verification code, and technical provider metadata. Normal Reminder Email is deliberately generic: it sends the recipient Email, generic subject/content, a generic application/dashboard URL, and technical provider metadata. It does not send the Personal Item title/body, original Capture content, item ID, reminder ID, or an item-specific link.

The self-host database stores Reminder Email settings, verification/challenge metadata, destination snapshots, and provider message/status metadata. Raw verification codes are not persisted.

## Fixes

- Prevents duplicate concurrent Capture Draft discard requests and restores the discard control after either success or failure.
- Uses truthful, bounded failed-segment messaging while preserving Original Audio and explicit retry/delete choices.

## Self-host Notes

- Tencent credentials, verified sender identity, templates, costs, network access, and Provider compliance remain the operator's responsibility.
- Enabling `EMAIL_REMINDER_PROVIDER_ENABLED=true` fails closed unless all required settings are valid; `TENCENT_SES_TEST_TEMPLATE_ID` alone is optional.
- `APP_ORIGIN` determines the generic Dashboard link. Use the real Community deployment origin rather than a Hosted Service default.
- Email, Push, Voice, and AI Provider configurations remain separate boundaries.

## Known Limitations

- Tencent SES is the only formally supported Email Provider in v0.6.0.
- Scheduled external delivery requires the single-instance embedded Reminder worker.
- Tencent accepting a send request is not a delivery guarantee; status reconciliation is bounded.
- There is no per-Reminder channel selector, remove-address action, automatic Email retention/challenge cleanup, or separate GDPR deletion subsystem.
- No recurring reminders, planner, calendar integration, official Docker image, or binary distribution.
