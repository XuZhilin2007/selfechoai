# SelfEcho AI

**[English](README.md) | 简体中文**

SelfEcho AI 用于快速捕获想法，由 AI 帮助整理为结构化的 Personal Items，同时保留原始输入和用户的最终决策权。AI 负责提取与组织信息，不替用户决定重要性、计划或行动。

**Status:** Community Edition 0.4.0 · Python 3.11+ · FastAPI · Web/PWA · Apache-2.0

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
- 移动端优先的 Web/PWA 界面
- Login、Logout、服务端 Session 与 CSRF 防护
- 默认关闭注册和可选 Invite registration
- Multi-user 数据所有权隔离
- SQLite schema v4，以及保留的通用 v2→v3 migration 实现

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
- 定时 Push 投递需要显式启用嵌入式 Reminder worker；支持的单机拓扑是单应用实例。
- 不启用 Push 时，Reminder 和应用内 due 回退仍然完整可用。
- 定时投递是 at-most-once：每条通知对每台订阅设备最多进行一次自动投递尝试；系统不保证 Provider 接收，也不保证设备最终展示通知。

Web Push 自托管还有额外的安全与部署考量，见 [Security Policy](SECURITY.md)。

## Requirements

- Python 3.11 或更高版本
- 支持 SQLite 的本地环境
- 现代浏览器
- 可选：自己的 DeepSeek 或 OpenAI API Key；首次创建用户和登录不需要 Provider Key

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
- 在数据库不存在时初始化 schema v4；
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
- `.env`、`data/`、`*.db`、WAL/SHM 和日志均被 Git 忽略
- Community Edition 不附带 Production 数据或从真实数据生成的 seed
- 空数据库会直接初始化为 schema v4

不要把数据库、备份、日志或包含个人内容的截图提交到 Git。

## 从 v0.3.0 升级

v0.3.0 的数据库是 schema v3；v0.4.0 使用 schema v4。启动不会自动迁移——遇到不支持的旧版本会直接拒绝启动。

1. 停止应用，备份数据库文件及其 WAL/SHM 伴生文件。
2. 先用只读检查预览迁移：

```bash
python -m app.migrations.v004_reminders --database data/selfecho.db --check-only
```

3. 执行显式 v3→v4 迁移。正式迁移开始前需要输入确认文字：

```bash
python -m app.migrations.v004_reminders --database data/selfecho.db
```

4. 迁移成功完成后再启动 v0.4.0。

不存在直接 v2→v4 的升级路径；保留的 v2→v3 migration 只是历史实现。

## Tests

```bash
python -m pytest
```

测试覆盖 Capture、原始输入持久化、DeepSeek/OpenAI 模拟响应、Authentication、Session、CSRF、Invite、Migration、Multi-user isolation、Reminder、时间表达解析、Web Push 安全与订阅、PWA，以及 first-user bootstrap 和 local/production Cookie 行为。JavaScript 与 Service Worker 合同测试位于 `tests/*.mjs`，使用 Node 内置 test runner 运行。

## Current Limitations

- 没有 password reset
- 没有 email verification
- 不支持循环提醒，尚无 planner 或日历集成
- 没有内置 rate limiting
- 没有正式管理后台或角色系统
- 定时 Web Push 尽力而为：at-most-once，不保证送达
- 单应用实例与 origin-root 部署；没有官方 Docker 镜像或二进制分发
- Voice Capture 不属于 Public v0.4.0

## Architecture

```text
Vanilla JavaScript PWA (含 Service Worker push 处理)
          │ same origin
FastAPI + Uvicorn
          ├── SQLite (schema v4)
          ├── DeepSeek / OpenAI provider abstraction
          └── 可选嵌入式 Reminder worker → Web Push
```

核心产品原则是：原始输入不能丢失；未知信息保持未知；AI 只整理信息，用户始终是最终决策者。

更完整的组件、数据流、认证、多用户隔离、Reminder/Push/worker、PWA Cache 和 Provider 边界说明见 [Architecture](docs/ARCHITECTURE.md)，稳定产品边界见 [Product Principles](docs/PRODUCT_PRINCIPLES.md)。

## Security and Contributing

漏洞请按 [Security Policy](SECURITY.md) 私下报告，不要在公开 Issue 中披露利用细节、秘密或真实用户数据。贡献流程、测试要求和范围约束见 [Contributing Guide](CONTRIBUTING.md)。

这是一个早期的个人 Community Project，维护能力和兼容性承诺有限。Roadmap 仅代表探索方向；提交较大功能前请先通过 Issue 讨论。

## License

Copyright 2026 Xu Zhilin.

本项目依据 [Apache License 2.0](LICENSE) 发布；版权声明同时记录在 [NOTICE](NOTICE)。
