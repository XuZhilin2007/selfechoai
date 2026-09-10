# SelfEcho AI

**English | [简体中文](README.zh-CN.md)**

SelfEcho AI helps you capture ideas quickly and uses AI to organize them into structured Personal Items, while always preserving the original input and leaving every final decision to you. AI extracts and organizes information; it does not decide what matters, what to plan, or what to do next.

**Status:** Community Edition 0.6.0 · Python 3.11+ · FastAPI · Web/PWA · Apache-2.0

## Community Edition

This public repository, [github.com/XuZhilin2007/selfechoai](https://github.com/XuZhilin2007/selfechoai), provides a self-hostable Community Edition. [selfechoai.com](https://selfechoai.com) is an independently operated Hosted Service. The two share the same product direction, but the public repository does not contain the hosted service's production database, credentials, server configuration, deployment runbooks, or private Git history.

## Why SelfEcho

Scattered thoughts often appear earlier than conventional tasks, and they carry concerns, constraints, candidate options, and information that is not yet settled. SelfEcho reliably saves the original text first, then lets AI organize that information into Personal Items you can revisit and correct. Its goal is not to plan your life for you, but to lower the cost of recording and re-understanding your personal context.

## Current Features

- Capture with raw input saved first
- DeepSeek/OpenAI AI structuring
- Personal Item dashboard, detail view, and lifecycle management
- One-time Reminders, created manually or extracted by AI from natural language and resolved deterministically in your timezone; ambiguous time expressions stay in a `needs_confirmation` state
- In-app due fallback, so Reminders remain usable without any notification setup
- Optional Web Push (disabled by default) with your own VAPID keys, browser subscription lifecycle, Service Worker delivery, and an embedded Reminder worker for scheduled multi-device delivery
- Optional Email Reminder (disabled by default) through operator-configured Tencent SES, with address ownership verification, an independent account-level channel, and an optional Test Email
- Optional Voice Capture (disabled by default): press-and-hold recording with upward cancel, transcription appended to the editable Capture Draft, and explicit retry/delete for failed segments
- Capture/Voice correctness fixes for concurrent discard actions and truthful failed-segment feedback
- Mobile-first Web/PWA interface
- Login, logout, server-side sessions, and CSRF protection
- Registration closed by default, with optional invite registration
- Multi-user data ownership isolation
- SQLite schema v6, with a required explicit v5→v6 migration for existing v0.5 databases

Planner, calendar integration, and autonomous agents are not implemented yet.

## Reminder Semantics

- AI only extracts reminder intent and a time expression; the application resolves the actual time deterministically using your timezone and your default reminder time.
- Ambiguous time expressions can remain unresolved in the `needs_confirmation` state until you confirm or edit them.
- Completing or trashing an item cancels its active Reminder; restoring the item does not revive the Reminder.
- Reminders are one-time. Recurring reminders are not supported.

## Web Push

Web Push is optional and disabled by default:

- It requires a browser that supports Push notifications and a secure context; use HTTPS for real deployments.
- The self-host operator generates their own VAPID key pair and configures it in `.env`.
- Scheduled Push and Email delivery require explicitly enabling the embedded Reminder worker; a single application instance is the supported topology.
- Without Push, Reminders and the in-app due fallback remain fully usable.
- Scheduled delivery is at-most-once: each notification is attempted at most once per subscribed device, and neither the provider accepting the message nor the device displaying it is guaranteed.

Web Push self-hosting has additional security and deployment considerations; see [Security Policy](SECURITY.md).

## Email Reminder

Email Reminder is an optional first-class Reminder channel beside Web Push. The channels are independent: a Push failure does not trigger Email, and an Email failure does not trigger Push. There is no hidden fallback between them. Depending on account settings and runtime availability, an account can use Push only, Email only, both, or neither (in-app only). These are account/runtime-level eligibility rules, not a per-Reminder channel field.

Scheduled external delivery requires `REMINDER_WORKER_ENABLED=true`. If the worker is disabled, due Reminders still transition and appear in the app when the user visits, but scheduled Push and Email are not delivered. Email configuration and Voice configuration are independent.

### Tencent SES setup

Community Edition v0.6.0 formally supports Tencent SES only. The self-host operator owns the Tencent Cloud account, SES activation, verified sender identity, credentials, templates, network access, costs, and compliance for their deployment. Prepare:

- a Tencent Cloud account with SES enabled;
- a verified sender identity;
- API credentials scoped according to your operating policy;
- one verification template and one normal Reminder template;
- optionally, a separate Test Email template.

Configure the following values in the untracked `.env`:

```text
EMAIL_REMINDER_PROVIDER_ENABLED=false
TENCENT_SES_REGION=ap-guangzhou
TENCENTCLOUD_SECRET_ID=
TENCENTCLOUD_SECRET_KEY=
TENCENT_SES_FROM_EMAIL_ADDRESS=
TENCENT_SES_VERIFICATION_TEMPLATE_ID=
TENCENT_SES_REMINDER_TEMPLATE_ID=
TENCENT_SES_TEST_TEMPLATE_ID=
TENCENT_SES_TIMEOUT_SECONDS=10
EMAIL_VERIFICATION_CODE_PEPPER=
```

`APP_ORIGIN` supplies the generic dashboard URL in normal Reminder Email, and `REMINDER_WORKER_ENABLED` controls scheduled Push/Email processing. Use the actual origin of your Community deployment; no Hosted Service domain is required.

With `EMAIL_REMINDER_PROVIDER_ENABLED=false` (the default), Email-specific values are not required or parsed, and startup does not require Tencent credentials, a sender, templates, a verification pepper, or Tencent network access. Authentication, Capture, in-app Reminders, separately configured Push, and separately configured Voice continue to work.

Setting `EMAIL_REMINDER_PROVIDER_ENABLED=true` fails closed unless the region and timeout are valid and the Secret ID, Secret Key, sender, verification template ID, Reminder template ID, and high-entropy verification pepper are configured. The Test Email template is optional: omitting it does not prevent normal Email Reminders, but the Test Email action is unavailable.

### Template contract and privacy

The verification template receives exactly:

```json
{"code":"123456"}
```

The normal Reminder template is deliberately generic and receives exactly:

```json
{"app_url":"https://your-selfecho.example/dashboard"}
```

It must not depend on a Personal Item title or body, `item_id`, `reminder_id`, an item-specific link, a Hosted Service domain, or private template IDs. The optional Test Email template receives an empty object:

```json
{}
```

For verification, Tencent receives the recipient Email address, verification code, and technical provider metadata. For a normal Reminder, Tencent receives the recipient address, generic Reminder subject/content, generic application/dashboard URL, and technical provider metadata. The normal Email does **not** send the Personal Item title or body, original Capture content, item ID, reminder ID, or an item-specific direct link.

The self-host database persists the current Reminder Email address, verification state and challenge metadata, a durable delivery ledger with destination snapshots, and provider message/status metadata. Raw verification codes are not persisted; the database stores an HMAC derived with `EMAIL_VERIFICATION_CODE_PEPPER`. Account deletion follows the application's existing data lifecycle; v0.6.0 does not claim a remove-address feature, automatic retention, automatic challenge cleanup, or a separate GDPR deletion subsystem.

### Verification and delivery semantics

- A Reminder Email address must be verified. Verification codes expire after 10 minutes, resend and confirmation attempts are rate-limited, and changing the address requires verification again. The login Email is never adopted automatically as the Reminder Email.
- External channel eligibility is captured the first time a Reminder enters the due lifecycle. If Email is disabled, unverified, unhealthy, or unavailable then, no Email delivery is created; enabling or verifying Email later does not backfill that already-due Reminder.
- A created Email delivery retains its destination snapshot. Before each actual send, the worker still revalidates the current address, verification, enabled state, destination health, user/item activity, and Reminder state. Changing the address does not redirect an existing delivery.
- Retriable pre-acceptance failures may be retried within the bounded delivery window; ambiguous outcomes become `unknown` instead of being sent again blindly. The worker performs bounded status reconciliation for accepted messages.
- Tencent accepting the API request means only that the request was submitted/accepted. It is not proof that the recipient received the message.
- Test Email goes only to the current verified, healthy address. It does not require normal Email Reminder to be enabled, but it does require an available provider and the optional Test Email template. Its “submitted” result likewise does not guarantee recipient delivery.

## Voice Capture

Voice Capture is optional and disabled by default (`VOICE_ASR_ENABLED=false`). With Voice disabled, text Capture, authentication, Reminders, and Web Push work normally; no Alibaba credential, ffmpeg, ffprobe, or Voice storage is required.

The flow preserves the existing Capture discipline:

- Press and hold to record; release to finish; slide upward while recording to cancel.
- A recording is capped at 60 seconds.
- The transcript is appended to the editable Capture Draft; you revise it yourself before Final Save.
- A failed Voice Segment can be retried or deleted explicitly; transcription is never retried automatically.
- Final Save stays blocked while a Voice Segment failure is unresolved.
- Saving the final edited text is what triggers AI Structuring. Successful transcription never triggers AI processing by itself.
- Original Audio is retained according to the Voice Capture lifecycle and remains associated with the saved input after Final Save.

Enabling Voice requires operator configuration:

- `VOICE_STORAGE_ROOT`: an absolute writable path outside the Git checkout. Voice Original Audio is stored there, outside SQLite. Protect and back up this storage consistently with the database. Disabling Voice later does not delete previously stored audio; keep existing Voice storage intact.
- `FFPROBE_PATH` and `FFMPEG_PATH`: paths to operator-installed media tools. SelfEcho does not bundle or redistribute ffmpeg/ffprobe binaries.
- `ALIBABA_ASR_API_URL` and `ALIBABA_API_KEY`: your own Alibaba DashScope endpoint and credential. The transcription model is fixed to `qwen-audio-3.0-asr-flash`.

When Voice is enabled, browser audio is sent from your self-hosted SelfEcho instance to the configured Alibaba ASR endpoint, which receives the audio required for transcription. This is a separate provider boundary and credential from AI Structuring, which still uses the configured DeepSeek/OpenAI-compatible provider after Final Save. Alibaba is not used for AI Structuring. Original Audio, transcripts, the database, Voice storage, backups, and provider credentials can all contain sensitive personal information and must be protected accordingly.

## Requirements

- Python 3.11 or later
- A local environment with SQLite support
- A modern browser
- Optional: your own DeepSeek or OpenAI API key; creating the first user and signing in do not require a provider key
- Optional, Email only: your own Tencent Cloud SES account, sender, credentials, templates, and verification pepper; not required while Email stays disabled
- Optional, Voice only: operator-installed ffmpeg and ffprobe plus your own Alibaba DashScope credential; not required while Voice stays disabled

## Quick Start

### Windows

```powershell
git clone https://github.com/XuZhilin2007/selfechoai.git
Set-Location selfechoai

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m app.bootstrap
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000> and sign in with the email and password you entered during bootstrap. The commands above, the browser URL, and `APP_ORIGIN` in `.env` must all consistently use `127.0.0.1`.

### macOS / Linux

```bash
git clone https://github.com/XuZhilin2007/selfechoai.git
cd selfechoai

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
cp .env.example .env
python -m app.bootstrap
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Then open <http://127.0.0.1:8000>.

## First User

The Community default configuration is:

```text
AUTH_REGISTRATION_MODE=closed
```

The first user must be created through a safe interactive command on a zero-user database:

```bash
python -m app.bootstrap
```

Bootstrap will:

- use the SQLite database configured via `APP_DATABASE_PATH` in `.env`;
- initialize schema v6 when the database does not exist yet;
- create one regular user only when the user count is 0;
- read and confirm the password through `getpass`;
- apply the same user constraints and Argon2id password hashing as the application;
- refuse to run once any user exists, with no `--force` option.

The bootstrap command does not print passwords, password hashes, session tokens, or CSRF tokens.

## Invite Registration

Invite registration is only for optional multi-user scenarios after the first user has been created. The project ships no default invite code.

Inside the activated virtual environment, generate a high-entropy invite code and its SHA-256 hash:

```bash
python -c "import secrets; from app.auth import hash_invite_code; code=secrets.token_urlsafe(32); print(f'Invite code: {code}'); print(f'Invite hash: {hash_invite_code(code)}')"
```

Write the generated hash into your local `.env`:

```text
AUTH_REGISTRATION_MODE=invite
AUTH_INVITE_CODE_HASH=<generated-sha256-hash>
```

After restarting the service, deliver the raw invite code to the invited user over a secure channel. Once registration is complete, set `AUTH_REGISTRATION_MODE=closed` again, clear the hash, and restart once more. Never commit the raw invite code, its hash, or your local `.env`.

## Local Cookie and Origin

The Community default configuration is:

```text
APP_ORIGIN=http://127.0.0.1:8000
AUTH_COOKIE_SECURE=false
```

Local HTTP uses the `selfecho_session` cookie. An HTTPS deployment must set `APP_ORIGIN` to the real HTTPS origin and set `AUTH_COOKIE_SECURE=true`; the server then uses `__Host-selfecho_session` with `Secure`, `HttpOnly`, `SameSite=Lax`, `Path=/`, and no `Domain`.

## AI Provider

DeepSeek is the default provider. Put your key into the untracked `.env`:

```text
AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_API_URL=https://api.deepseek.com
```

To use the OpenAI Responses API:

```text
AI_PROVIDER=openai
AI_API_URL=https://api.openai.com/v1/responses
AI_API_KEY=
AI_MODEL=
```

Depending on the configured provider, user input and related item context are sent to an external LLM provider such as DeepSeek or OpenAI. Reminder extraction is part of this data flow: text containing reminder intent or time expressions participates in the provider call. You are responsible for reviewing the provider's privacy policy, data retention rules, and API costs. Do not enable `AI_DEBUG_OUTPUT` in shared environments; debug output may contain personal input or model results.

Even without a provider key, you can still initialize the database, create a user, and sign in. Capture saves the raw input first, but AI structuring will report a missing configuration.

## Data

- Default database: `data/selfecho.db`
- SQLite data belongs to the current self-hosted instance
- Voice Original Audio (when Voice is enabled) is stored under `VOICE_STORAGE_ROOT`, outside SQLite; protect and back up that storage consistently with the database
- Reminder Email addresses, verification/challenge metadata, delivery destination snapshots, and provider status metadata are stored in SQLite; raw verification codes are not
- `.env`, `data/`, `*.db`, WAL/SHM files, and logs are ignored by Git
- The Community Edition ships no production data and no seeds derived from real data
- Empty databases are initialized directly to schema v6

Never commit databases, backups, logs, or screenshots containing personal content to Git.

## Upgrade from v0.5.0

A v0.5.0 database uses schema v5; v0.6.0 uses schema v6. **v0.6.0 does not migrate a v0.5 database automatically.** Starting the new runtime against schema v5 fails closed and asks the operator to migrate explicitly.

1. Stop the application/service and make sure no process is using the database.
2. Create and validate a recoverable backup of the database file and any WAL/SHM companions that exist for it.
3. Optionally run the migration's read-only preflight check against the actual configured database path:

```bash
python -m app.migrations.v006_email_reminders --database data/selfecho.db --check-only
```

4. Run the explicit v5→v6 migration:

```bash
python -m app.migrations.v006_email_reminders --database data/selfecho.db
```

5. At the prompt, enter the exact confirmation phrase `MIGRATE PUBLIC V5 TO V6`. The migration runs in one transaction and checks preserved row counts, schema structure, foreign keys, and SQLite integrity.
6. Restart the v0.6.0 application only after the migration reports success.
7. Confirm that startup accepts schema v6 and that existing data and the Account page load as expected.
8. Only then, optionally configure and enable Tencent SES Email Reminder.

A fresh v0.6.0 installation creates schema v6 directly and does not need to create or migrate schema v5 first. Older databases must still migrate sequentially: v4→v5 with `python -m app.migrations.v005_voice_capture`, then v5→v6 as above; schema v3 must first use `python -m app.migrations.v004_reminders` for v3→v4.

## Tests

```bash
python -m pytest
```

Tests cover Capture, raw input persistence, simulated DeepSeek/OpenAI responses, authentication, sessions, CSRF, invites, migrations, multi-user isolation, Reminders, temporal parsing, Web Push security and subscriptions, Email settings/verification/privacy/delivery contracts, Voice Capture contracts (draft lifecycle, transcription, storage, deletion ledger), PWA behavior, plus first-user bootstrap and local/production cookie handling. The JavaScript and Service Worker contract tests live in `tests/*.mjs` and run with the Node test runner.

## Current Limitations

- No password reset
- No recurring reminders, planner, or calendar integration
- No built-in rate limiting
- No formal admin console or role system
- Tencent SES is the only formally supported Email provider in v0.6.0; the Test Email action is unavailable without its optional dedicated template
- Provider acceptance is not guaranteed recipient delivery; ambiguous Email submission results remain `unknown` and status reconciliation is bounded
- No remove-address action, automatic Email data retention, automatic verification-challenge cleanup, or separate GDPR deletion subsystem
- Scheduled external delivery is not available without the Reminder worker; Web Push remains at-most-once and Email uses bounded retries only for explicit pre-acceptance retriable failures
- Single application instance and origin-root deployment; no official Docker image or binary distribution
- Voice Capture has limited real-device validation. Browser/device microphone, MediaRecorder, PWA, and native media-control behavior may vary. Native audio duration presentation may vary by browser before playback; this does not indicate that the persisted Original Audio or server-detected duration is invalid

## Architecture

```text
Vanilla JavaScript PWA (incl. Service Worker push handling)
          │ same origin
FastAPI + Uvicorn
          ├── SQLite (schema v6)
          ├── DeepSeek / OpenAI provider abstraction
          ├── optional Voice Capture → external Voice storage → Alibaba ASR
          └── optional embedded Reminder worker
                    ├── Web Push
                    └── generic Email Reminder → Tencent SES
```

The core product principles: original input must never be lost; unknown information stays unknown; AI only organizes information, and the user is always the final decision maker.

For full component, data-flow, authentication, multi-user isolation, Reminder channel/worker, PWA cache, and provider boundary details, see [Architecture](docs/ARCHITECTURE.md). Stable product boundaries are described in [Product Principles](docs/PRODUCT_PRINCIPLES.md). Release-candidate notes are in [Community Edition v0.6.0 Release Notes](docs/RELEASE_NOTES_v0.6.0.md).

## Security and Contributing

Please report vulnerabilities privately according to the [Security Policy](SECURITY.md). Do not disclose exploit details, secrets, or real user data in public issues. Contribution workflow, testing requirements, and scope constraints are described in the [Contributing Guide](CONTRIBUTING.md).

This is an early-stage personal community project with limited maintenance capacity and compatibility commitments. The roadmap reflects exploration directions only; please discuss larger features in an issue before submitting them.

## License

Copyright 2026 Xu Zhilin.

This project is released under the [Apache License 2.0](LICENSE); the copyright notice is also recorded in [NOTICE](NOTICE).
