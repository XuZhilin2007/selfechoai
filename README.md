# SelfEcho AI

**English | [简体中文](README.zh-CN.md)**

SelfEcho AI helps you capture ideas quickly and uses AI to organize them into structured Personal Items, while always preserving the original input and leaving every final decision to you. AI extracts and organizes information; it does not decide what matters, what to plan, or what to do next.

**Status:** Community Edition 0.5.0 · Python 3.11+ · FastAPI · Web/PWA · Apache-2.0

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
- Optional Voice Capture (disabled by default): press-and-hold recording with upward cancel, transcription appended to the editable Capture Draft, and explicit retry/delete for failed segments
- Mobile-first Web/PWA interface
- Login, logout, server-side sessions, and CSRF protection
- Registration closed by default, with optional invite registration
- Multi-user data ownership isolation
- SQLite schema v5, with preserved explicit migration implementations

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
- Scheduled Push delivery requires explicitly enabling the embedded Reminder worker; a single application instance is the supported topology.
- Without Push, Reminders and the in-app due fallback remain fully usable.
- Scheduled delivery is at-most-once: each notification is attempted at most once per subscribed device, and neither the provider accepting the message nor the device displaying it is guaranteed.

Web Push self-hosting has additional security and deployment considerations; see [Security Policy](SECURITY.md).

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
- initialize schema v5 when the database does not exist yet;
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
- `.env`, `data/`, `*.db`, WAL/SHM files, and logs are ignored by Git
- The Community Edition ships no production data and no seeds derived from real data
- Empty databases are initialized directly to schema v5

Never commit databases, backups, logs, or screenshots containing personal content to Git.

## Upgrade from v0.4.0

A v0.4.0 database uses schema v4; v0.5.0 uses schema v5. Startup does not migrate automatically — it refuses to start on an unsupported schema version.

1. Stop the application and back up the database file together with its WAL/SHM companions.
2. Preview the migration with a read-only check:

```bash
python -m app.migrations.v005_voice_capture --database data/selfecho.db --check-only
```

3. Run the explicit v4→v5 migration. It asks for a typed confirmation before it starts:

```bash
python -m app.migrations.v005_voice_capture --database data/selfecho.db
```

The confirmation phrase is `MIGRATE V4 TO V5`.

4. Start v0.5.0 only after the migration has completed successfully.

Databases still on schema v3 (v0.3.0) must migrate sequentially: v3→v4 with `python -m app.migrations.v004_reminders`, then v4→v5 as above. There is no direct v3→v5 path, and the preserved v2→v3 migration remains a historical implementation detail.

## Tests

```bash
python -m pytest
```

Tests cover Capture, raw input persistence, simulated DeepSeek/OpenAI responses, authentication, sessions, CSRF, invites, migrations, multi-user isolation, Reminders, temporal parsing, Web Push security and subscriptions, Voice Capture contracts (draft lifecycle, transcription, storage, deletion ledger), PWA behavior, plus first-user bootstrap and local/production cookie handling. The JavaScript and Service Worker contract tests live in `tests/*.mjs` and run with the Node test runner.

## Current Limitations

- No password reset
- No email verification
- No recurring reminders, planner, or calendar integration
- No built-in rate limiting
- No formal admin console or role system
- Scheduled Web Push is best-effort: at-most-once, no guaranteed delivery
- Single application instance and origin-root deployment; no official Docker image or binary distribution
- Voice Capture has limited real-device validation. Browser/device microphone, MediaRecorder, PWA, and native media-control behavior may vary. Native audio duration presentation may vary by browser before playback; this does not indicate that the persisted Original Audio or server-detected duration is invalid

## Architecture

```text
Vanilla JavaScript PWA (incl. Service Worker push handling)
          │ same origin
FastAPI + Uvicorn
          ├── SQLite (schema v5)
          ├── DeepSeek / OpenAI provider abstraction
          ├── optional Voice Capture → external Voice storage → Alibaba ASR
          └── optional embedded Reminder worker → Web Push
```

The core product principles: original input must never be lost; unknown information stays unknown; AI only organizes information, and the user is always the final decision maker.

For full component, data-flow, authentication, multi-user isolation, Reminder/Push/worker, PWA cache, and provider boundary details, see [Architecture](docs/ARCHITECTURE.md). Stable product boundaries are described in [Product Principles](docs/PRODUCT_PRINCIPLES.md).

## Security and Contributing

Please report vulnerabilities privately according to the [Security Policy](SECURITY.md). Do not disclose exploit details, secrets, or real user data in public issues. Contribution workflow, testing requirements, and scope constraints are described in the [Contributing Guide](CONTRIBUTING.md).

This is an early-stage personal community project with limited maintenance capacity and compatibility commitments. The roadmap reflects exploration directions only; please discuss larger features in an issue before submitting them.

## License

Copyright 2026 Xu Zhilin.

This project is released under the [Apache License 2.0](LICENSE); the copyright notice is also recorded in [NOTICE](NOTICE).
