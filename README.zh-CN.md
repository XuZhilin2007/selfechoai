# SelfEcho AI

**[English](README.md) | 简体中文**

SelfEcho AI 用于快速捕获想法，由 AI 帮助整理为结构化的 Personal Items，同时保留原始输入和用户的最终决策权。AI 负责提取与组织信息，不替用户决定重要性、计划或行动。

**Status:** Community Edition 0.6.0 · Python 3.11+ · FastAPI · Web/PWA · Apache-2.0

## Community Edition

本公开仓库 [github.com/XuZhilin2007/selfechoai](https://github.com/XuZhilin2007/selfechoai) 提供可自托管的 Community Edition。[selfechoai.com](https://selfechoai.com) 是独立运营的 Hosted Service。两者共享产品方向，但公开仓库不包含托管服务的生产数据库、密钥、服务器配置、部署 Runbook 或私有 Git 历史。

## Why SelfEcho

零散想法往往比传统任务更早出现，也包含顾虑、限制、候选方案和尚未确定的信息。SelfEcho 先可靠保存原文，再让 AI 把这些信息整理成可回看、可修正的 Personal Items。它的目标不是替用户制定人生计划，而是降低记录和重新理解个人上下文的成本。

## Current Features

- Capture 与原始输入先保存
- DeepSeek/OpenAI AI Structuring
- Personal Item Dashboard、Detail 与生命周期管理
- 一次性 Reminder：可手动创建，也可由 AI 从自然语言提取意图与时间表达，并按用户时区确定性解析；歧义时间表达会停留在 `needs_confirmation` 状态
- 应用内 due 回退，即使不配置任何通知，Reminder 依然可用
- 可选 Web Push（默认关闭）：使用自托管者自己的 VAPID 密钥、浏览器订阅生命周期、Service Worker 送达，以及用于定时投递的嵌入式 Reminder worker
- 可选 Email Reminder（默认关闭）：通过自托管者配置的 Tencent SES 发送，包含邮箱所有权验证、独立的账户级渠道与可选 Test Email
- 可选 Voice Capture（默认关闭）：按住录音、上滑取消，转写文本追加到可编辑的 Capture Draft，失败片段可显式重试或删除
- Capture/Voice correctness fixes：覆盖并发 discard 操作、上传 revision conflict 恢复与如实的失败片段提示
- 移动端优先的 Web/PWA 界面
- Login、Logout、服务端 Session 与 CSRF 防护
- 默认关闭注册和可选 Invite registration
- Multi-user 数据所有权隔离
- SQLite schema v6；已有 v0.5 数据库必须显式执行 v5→v6 迁移

Planner、日历集成和自治 Agent 尚未实现。

## Reminder 语义

- AI 只提取 Reminder 意图和时间表达；实际时间由应用按用户时区和默认提醒时间确定性解析。
- 歧义的时间表达可以保持 `needs_confirmation` 状态，等用户确认或修改，AI 不替用户猜。
- 完成或回收事项会取消其活跃 Reminder；恢复事项不会复活已取消的 Reminder。
- Reminder 都是一次性的，不支持循环提醒。

## Web Push

Web Push 可选且默认关闭：

- 需要浏览器支持 Push 通知并处于 secure context；正式部署请使用 HTTPS。
- VAPID 密钥对由自托管者自行生成并写入 `.env`。
- 定时 Push 与 Email 投递都需要显式启用嵌入式 Reminder worker；支持的单机拓扑是单应用实例。
- 不启用 Push 时，Reminder 和应用内 due 回退仍然完整可用。
- 定时投递是 at-most-once：每条通知对每台订阅设备最多进行一次自动投递尝试；系统不保证 Provider 接收，也不保证设备最终展示通知。

Web Push 自托管还有额外的安全与部署考量，见 [Security Policy](SECURITY.md)。

## Email Reminder

Email Reminder 是与 Web Push 并列的可选 first-class Reminder channel。两个渠道彼此独立：Push 失败不会触发 Email，Email 失败也不会触发 Push，不存在隐藏 fallback。根据账户设置与运行时可用性，一个账户可以是 Push only、Email only、Both，或 Neither / in-app only。这是账户与运行时级别的 eligibility，不是每条 Reminder 上可单独选择的字段。

外部定时投递必须设置 `REMINDER_WORKER_ENABLED=true`。worker 关闭时，Reminder 仍可在用户访问应用时进入 due 并显示于应用内，但不会执行定时 Push 或 Email 投递。Email 与 Voice 配置彼此独立。

### Tencent SES 配置

Community Edition v0.6.0 首发只正式支持 Tencent SES。自托管者自行负责 Tencent Cloud 账户、SES 开通、已验证 sender identity、凭据、模板、网络访问、费用与部署所需合规事项。需要准备：

- 已开通 SES 的 Tencent Cloud 账户；
- 已验证的发信身份；
- 按自身运维策略设置权限的 API 凭据；
- 一个 Verification template 和一个普通 Reminder template；
- 可选的独立 Test Email template。

在不受 Git 跟踪的 `.env` 中配置：

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

`APP_ORIGIN` 用于生成普通 Reminder Email 中的通用 Dashboard URL，`REMINDER_WORKER_ENABLED` 控制定时 Push/Email 处理。这里应使用 Community 自托管实例的实际 origin，不要求使用 Hosted Service 域名。

默认 `EMAIL_REMINDER_PROVIDER_ENABLED=false` 时，不要求也不会解析 Email 专用配置；正常启动不需要 Tencent 凭据、sender、模板、verification pepper 或 Tencent 网络访问。Authentication、Capture、应用内 Reminder、单独配置的 Push 与单独配置的 Voice 都可以继续工作。

主动设置 `EMAIL_REMINDER_PROVIDER_ENABLED=true` 后会 fail closed：region 与 timeout 必须有效，且 Secret ID、Secret Key、sender、Verification template ID、Reminder template ID 和高熵 verification pepper 都必须配置。Test Email template 是可选项；不配置不会阻止普通 Email Reminder，但 Test Email 操作会显示为不可用。

### Template contract 与隐私

Verification template 精确接收：

```json
{"code":"123456"}
```

普通 Reminder template 刻意保持 generic，只精确接收：

```json
{"app_url":"https://your-selfecho.example/dashboard"}
```

模板不得依赖 Personal Item title/body、`item_id`、`reminder_id`、事项专属链接、Hosted Service 域名或 Private template ID。可选 Test Email template 接收空对象：

```json
{}
```

验证邮件会向 Tencent 发送 recipient Email、verification code 与技术性 Provider metadata。普通 Reminder 会发送 recipient Email、通用 Reminder subject/content、通用应用/Dashboard URL 与技术性 Provider metadata。普通邮件**不会**发送 Personal Item title/body、原始 Capture 内容、item ID、reminder ID 或事项专属直达链接。

自托管数据库会保存当前 Reminder Email、验证状态与 challenge metadata、包含 destination snapshot 的 durable delivery ledger，以及 Provider message/status metadata。Raw verification code 不会持久化；数据库只保存使用 `EMAIL_VERIFICATION_CODE_PEPPER` 派生的 HMAC。账户删除遵循应用现有数据生命周期；v0.6.0 不声称已有 remove-address、自动 retention、自动 challenge cleanup 或独立 GDPR 删除子系统。

### 验证与投递语义

- Reminder Email 必须完成验证。验证码 10 分钟过期，重发与确认尝试受限流；更换地址后必须重新验证。Login Email 不会自动成为 Reminder Email。
- Reminder 第一次进入 due lifecycle 时记录外部渠道 eligibility。如果当时 Email 关闭、未验证、不健康或不可用，就不会创建 Email delivery；后来再开启或验证不会为该已 due Reminder backfill。
- 已创建的 Email delivery 保留 destination snapshot。真正发送前，worker 仍会重新核验当前地址、verification、enabled、destination health、用户/事项活跃状态与 Reminder 状态；后来更换地址不会把既有 delivery 重定向到新地址。
- 明确发生在 Provider 接受前的可重试失败，可在有限投递窗口内重试；结果有歧义时进入 `unknown`，不会盲目重发。worker 会对 accepted message 进行次数有限的 delivery-status reconciliation。
- Tencent 接受 API request 只表示请求已 submitted/accepted，不证明 recipient 已收到邮件。
- Test Email 只发送到当前已验证且健康的地址；它不要求普通 Email Reminder 已开启，但要求 Provider 可用且配置了可选 Test Email template。“已提交”同样不保证最终送达。

## Voice Capture

Voice Capture 可选且默认关闭（`VOICE_ASR_ENABLED=false`）。关闭 Voice 时，文本 Capture、认证、Reminder 和 Web Push 正常可用，也不需要 Alibaba Key、ffmpeg、ffprobe 或 Voice 存储。

流程延续现有 Capture 纪律：

- 按住开始录音；松开结束；录音过程中上滑取消。
- 单次录音最长 60 秒。
- 转写文本追加到可编辑的 Capture Draft，最终保存前由你自己修改。
- 失败的 Voice Segment 可显式重试或删除；转写不会自动重试。
- 存在未解决的 Voice 失败时，Final Save 会被阻止。
- 触发 AI Structuring 的是 Final Save 保存的最终编辑文本；转写成功本身不会触发 AI 处理。
- Original Audio 按 Voice Capture 生命周期保留，并在 Final Save 后继续与已保存输入关联。

启用 Voice 需要运维配置：

- `VOICE_STORAGE_ROOT`：Git checkout 之外的可写绝对路径。Voice Original Audio 存储在这里，不进入 SQLite。应像对待数据库一样保护和备份该存储；之后关闭 Voice 不会删除已存储的音频，已有 Voice 存储应保持原样。
- `FFPROBE_PATH` 与 `FFMPEG_PATH`：由运维自行安装的媒体工具路径。SelfEcho 不捆绑也不重新分发 ffmpeg/ffprobe 二进制。
- `ALIBABA_ASR_API_URL` 与 `ALIBABA_API_KEY`：你自己的 Alibaba DashScope 端点与凭据。转写模型固定为 `qwen-audio-3.0-asr-flash`。

启用 Voice 后，浏览器音频会从你自托管的 SelfEcho 实例发送到所配置的 Alibaba ASR 端点，Alibaba 接收转写所需的音频。这是与 AI Structuring 不同的 Provider 边界与凭据：AI Structuring 仍在 Final Save 后使用所配置的 DeepSeek/OpenAI 兼容 Provider，Alibaba 不用于 AI Structuring。Original Audio、转写文本、数据库、Voice 存储、备份和 Provider 凭据都可能包含敏感个人信息，应按敏感数据保护。

## Requirements

- Python 3.11 或更高版本
- 支持 SQLite 的本地环境
- 现代浏览器
- 可选：自己的 DeepSeek 或 OpenAI API Key；首次创建用户和登录不需要 Provider Key
- 可选，仅 Email 需要：自己的 Tencent Cloud SES 账户、sender、凭据、模板与 verification pepper；Email 保持关闭时不需要
- 可选，仅 Voice 需要：运维自行安装的 ffmpeg 与 ffprobe，以及自己的 Alibaba DashScope Key；Voice 保持关闭时不需要

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

打开 <http://127.0.0.1:8000>，使用 bootstrap 时输入的 email 和 password 登录。命令、浏览器地址、`.env` 中的 `APP_ORIGIN` 必须统一使用 `127.0.0.1`。

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

然后打开 <http://127.0.0.1:8000>。

## First User

Community 默认配置为：

```text
AUTH_REGISTRATION_MODE=closed
```

首次用户必须在 zero-user database 上通过安全交互命令创建：

```bash
python -m app.bootstrap
```

Bootstrap 会：

- 使用 `.env` 中 `APP_DATABASE_PATH` 指定的 SQLite 数据库；
- 在数据库不存在时初始化 schema v6；
- 只在 user count 为 0 时创建一个普通用户；
- 通过 `getpass` 读取并确认 password；
- 使用与应用相同的用户约束和 Argon2id password hashing；
- 在已有任何用户时拒绝执行，不提供 `--force`。

Bootstrap 命令不会在终端输出 password、password hash、Session token 或 CSRF token。

## Invite Registration

Invite registration 只用于 first user 创建完成后的可选多用户场景。项目不提供默认邀请码。

在已激活的虚拟环境中生成高熵 invite 及其 SHA-256 hash：

```bash
python -c "import secrets; from app.auth import hash_invite_code; code=secrets.token_urlsafe(32); print(f'Invite code: {code}'); print(f'Invite hash: {hash_invite_code(code)}')"
```

将生成的 hash 写入本地 `.env`：

```text
AUTH_REGISTRATION_MODE=invite
AUTH_INVITE_CODE_HASH=<generated-sha256-hash>
```

重启服务后，把 raw invite code 通过安全渠道发送给受邀用户。注册完成后建议恢复 `AUTH_REGISTRATION_MODE=closed`、清空 hash 并再次重启。不要提交 raw invite、hash 或本地 `.env`。

## Local Cookie and Origin

默认 Community 配置使用：

```text
APP_ORIGIN=http://127.0.0.1:8000
AUTH_COOKIE_SECURE=false
```

Local HTTP 使用 `selfecho_session`。HTTPS deployment 必须把 `APP_ORIGIN` 改为实际 HTTPS origin，并设置 `AUTH_COOKIE_SECURE=true`；服务端随后使用带 `Secure`、`HttpOnly`、`SameSite=Lax`、`Path=/`、无 `Domain` 的 `__Host-selfecho_session`。

## AI Provider

默认 Provider 是 DeepSeek。把自己的 Key 写入未跟踪的 `.env`：

```text
AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_API_URL=https://api.deepseek.com
```

使用 OpenAI Responses API 时：

```text
AI_PROVIDER=openai
AI_API_URL=https://api.openai.com/v1/responses
AI_API_KEY=
AI_MODEL=
```

根据所配置的 Provider，用户输入及相关事项上下文可能会发送给 DeepSeek 或 OpenAI 等外部第三方 LLM 服务。Reminder 提取属于这条数据流：包含提醒意图或时间表达的文本会参与 Provider 调用。使用者应自行确认供应商的隐私政策、数据保留规则和 API 费用。不要在共享环境中启用 `AI_DEBUG_OUTPUT`；调试输出可能包含个人输入或模型结果。

没有 Provider Key 时仍可初始化数据库、创建用户并登录；Capture 会先保存原始输入，但 AI 整理会报告配置缺失。

## Data

- 默认数据库：`data/selfecho.db`
- SQLite 数据属于当前 self-host instance
- Voice Original Audio（启用 Voice 时）存储在 `VOICE_STORAGE_ROOT` 下，不进入 SQLite；应与数据库一致地保护和备份该存储
- Reminder Email、验证/challenge metadata、delivery destination snapshot 与 Provider status metadata 保存在 SQLite；不保存 raw verification code
- `.env`、`data/`、`*.db`、WAL/SHM 和日志均被 Git 忽略
- Community Edition 不附带 Production 数据或从真实数据生成的 seed
- 空数据库会直接初始化为 schema v6

不要把数据库、备份、日志或包含个人内容的截图提交到 Git。

## 从 v0.5.0 升级

v0.5.0 数据库使用 schema v5，v0.6.0 使用 schema v6。**v0.6.0 不会自动迁移 v0.5 数据库。** 新 runtime 遇到 schema v5 会 fail closed，并要求 operator 显式迁移。

1. 停止 application/service，确认没有进程仍在使用数据库。
2. 为数据库文件以及实际存在的 WAL/SHM 伴生文件创建经过验证、可恢复的备份。
3. 可选：针对实际配置的数据库路径运行只读 preflight：

```bash
python -m app.migrations.v006_email_reminders --database data/selfecho.db --check-only
```

4. 执行显式 v5→v6 迁移：

```bash
python -m app.migrations.v006_email_reminders --database data/selfecho.db
```

5. 按提示输入精确确认文字 `MIGRATE PUBLIC V5 TO V6`。迁移在单一 transaction 内执行，并检查旧表行数守恒、schema 结构、foreign key 与 SQLite integrity。
6. 只有迁移报告成功后，才重启 v0.6.0 应用。
7. 确认应用启动接受 schema v6，已有数据与 Account 页面均可正常加载。
8. 完成上述步骤后，才按需配置并开启 Tencent SES Email Reminder。

全新 v0.6.0 安装会直接创建 schema v6，不需要先创建或迁移 schema v5。更旧数据库仍须顺序迁移：schema v4 先通过 `python -m app.migrations.v005_voice_capture` 升到 v5，再按上述步骤升到 v6；schema v3 还须先通过 `python -m app.migrations.v004_reminders` 升到 v4。

## Tests

```bash
python -m pytest
```

测试覆盖 Capture、原始输入持久化、DeepSeek/OpenAI 模拟响应、Authentication、Session、CSRF、Invite、Migration、Multi-user isolation、Reminder、时间表达解析、Web Push 安全与订阅、Email 设置/验证/隐私/投递合同、Voice Capture 合同（草稿生命周期、转写、存储、删除 ledger）、PWA，以及 first-user bootstrap 和 local/production Cookie 行为。JavaScript 与 Service Worker 合同测试位于 `tests/*.mjs`，使用 Node 内置 test runner 运行。

## Current Limitations

- 没有 password reset
- 不支持循环提醒，尚无 planner 或日历集成
- 没有内置 rate limiting
- 没有正式管理后台或角色系统
- v0.6.0 首发只正式支持 Tencent SES；未配置可选专用模板时 Test Email 操作不可用
- Provider acceptance 不等于 recipient delivery；Email 提交结果有歧义时保留为 `unknown`，delivery-status reconciliation 次数有限
- 尚无 remove-address、Email 数据自动 retention、verification challenge 自动 cleanup 或独立 GDPR 删除子系统
- Reminder worker 关闭时不提供外部定时投递；Web Push 仍是 at-most-once，Email 只对明确发生在接受前的可重试失败进行有限重试
- 单应用实例与 origin-root 部署；没有官方 Docker 镜像或二进制分发
- Voice Capture 的真机验证有限。浏览器/设备麦克风、MediaRecorder、PWA 与原生媒体控件行为可能存在差异。原生音频时长在播放前的展示可能因浏览器而异；这不表示已持久化的 Original Audio 或服务端检测到的时长无效

## Architecture

```text
Vanilla JavaScript PWA (含 Service Worker push 处理)
          │ same origin
FastAPI + Uvicorn
          ├── SQLite (schema v6)
          ├── DeepSeek / OpenAI provider abstraction
          ├── 可选 Voice Capture → 外部 Voice 存储 → Alibaba ASR
          └── 可选嵌入式 Reminder worker
                    ├── Web Push
                    └── generic Email Reminder → Tencent SES
```

核心产品原则是：原始输入不能丢失；未知信息保持未知；AI 只整理信息，用户始终是最终决策者。

更完整的组件、数据流、认证、多用户隔离、Reminder channel/worker、PWA Cache 和 Provider 边界说明见 [Architecture](docs/ARCHITECTURE.md)，稳定产品边界见 [Product Principles](docs/PRODUCT_PRINCIPLES.md)。Release Candidate 说明见 [Community Edition v0.6.0 Release Notes](docs/RELEASE_NOTES_v0.6.0.md)。

## Security and Contributing

漏洞请按 [Security Policy](SECURITY.md) 私下报告，不要在公开 Issue 中披露利用细节、秘密或真实用户数据。贡献流程、测试要求和范围约束见 [Contributing Guide](CONTRIBUTING.md)。

这是一个早期的个人 Community Project，维护能力和兼容性承诺有限。Roadmap 仅代表探索方向；提交较大功能前请先通过 Issue 讨论。

## License

Copyright 2026 Xu Zhilin.

本项目依据 [Apache License 2.0](LICENSE) 发布；版权声明同时记录在 [NOTICE](NOTICE)。
