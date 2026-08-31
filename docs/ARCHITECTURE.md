# SelfEcho AI Architecture

本文描述 Community Edition v0.4.0 的当前实现，不代表托管服务的基础设施设计。

## System Overview

~~~text
Vanilla JavaScript PWA（含 Service Worker push 处理）
        │ same-origin HTTP + JSON
FastAPI application
        ├── Authentication / CSRF
        ├── Capture / Item APIs
        ├── Reminder / Reminder-settings APIs
        ├── Push configuration / subscription APIs
        ├── Background AI processing
        ├── 可选嵌入式 Reminder worker（定时 due 扫描 + Web Push 投递）
        ├── SQLite schema v4
        └── DeepSeek or OpenAI provider boundary
~~~

- app/main.py 组装 FastAPI、生命周期、API、静态文件、PWA 路由和可选的嵌入式 Reminder worker。
- app/repository.py 处理 Personal Item 与原始输入持久化，并在事项完成/回收时取消活跃 Reminder。
- app/reminder_repository.py、app/reminder_routes.py 和 app/services/reminders.py 处理 Reminder 状态、生命周期与查询。
- app/services/temporal_parser.py 负责时间表达的确定性解析；app/time_utils.py 处理 IANA 时区校验与 UTC 序列化。
- app/push_routes.py 与 app/services/push_subscriptions.py 处理浏览器 Push 订阅生命周期。
- app/services/push_security.py 在出站前校验 Push endpoint；app/services/web_push.py 执行受限的出站 Web Push。
- app/services/reminder_delivery.py 是嵌入式 worker 的 due 扫描与投递逻辑。
- app/auth.py、app/auth_repository.py 和 app/auth_routes.py 处理认证、Session 和用户数据边界。
- app/services/processing.py 负责从已保存输入触发 AI 整理。
- app/services/ai.py 和 app/services/deepseek.py 隔离外部 LLM Provider。
- app/static/ 是无前端构建步骤的 Vanilla JavaScript PWA。

## Capture and AI Flow

~~~text
authenticated capture
        │
        ▼
commit original_text to SQLite
        │
        ├── immediately return 202
        ▼
background provider call
        │
        ├── success: create/update structured item
        │            └── reminder candidate → deterministic temporal parsing
        └── failure: keep original input and record failure state
~~~

原始输入在 Provider 调用前提交。AI 失败不会删除输入；失败记录可重试。启动时，应用会恢复被中断的 processing 状态，并重新调度仍 pending 的输入。

AI 输出通过 Pydantic schema 验证。Provider 只负责提取和组织信息；重要性、计划与最终行动仍由用户确认。向已有事项补充输入或重新整理时，服务会使用该事项的输入历史。

## Reminder Flow

~~~text
Capture / Manual action
        → LLM reminder candidate（仅在适用时）
        → deterministic temporal parsing（用户时区 + 默认提醒时间）
        → Reminder persistence
        → scheduled / needs_confirmation
        → due
        → in-app fallback / optional Web Push
~~~

- Reminder 是一次性的，没有循环提醒。
- AI 只产生 reminder intent 和 temporal expression；时区换算、确定性解析和 UTC 持久化都由应用完成。
- 无法解析或歧义的时间表达保持 `needs_confirmation`，不会由 AI 猜测补全。
- 用户可以手动创建、改期或取消 Reminder。
- 事项被完成或移入回收站时，其活跃 Reminder（`needs_confirmation` / `scheduled`）被取消；事项恢复到 active 不会复活已取消的 Reminder。
- 到期由两种途径呈现：应用访问时的惰性 due 转移与应用内展示；以及可选的定时 Web Push。

## Authentication and Request Protection

- 密码使用 argon2-cffi 的 Argon2 PasswordHasher 存储为不可逆哈希。
- Session Token 和 CSRF Token 使用安全随机值，数据库只保存其 SHA-256 哈希。
- Session 保存在 SQLite；Logout 会撤销服务端 Session。
- 修改型 API 要求有效 Session 与 X-CSRF-Token。
- Login、Register 和 Logout 检查浏览器 Origin；配置的 APP_ORIGIN 是允许值。
- 本地 HTTP 使用 selfecho_session；HTTPS 配置使用 Secure 的 __Host-selfecho_session。两者均为 HttpOnly、SameSite=Lax、Path=/ 且不设置 Domain。
- HTTPS Origin 配合非 Secure Cookie 会在配置加载时被拒绝。

注册默认关闭。空数据库的首位用户通过 python -m app.bootstrap 原子创建；数据库已有用户时命令拒绝继续。可选 Invite 模式只保存邀请代码的 SHA-256 摘要。

## Multi-user Isolation

schema v4 为 users、user_sessions、personal_items、item_inputs、reminders、push_subscriptions 和 reminder_deliveries 建立显式所有权关系。业务 API 从已验证 Session 获取 current user，Repository 查询和修改同时使用记录 ID 与 user_id；Reminder、Push 订阅和投递记录同样按 user_id 隔离，Push 订阅还绑定创建它的 Session。跨用户访问按不存在处理，并由 API 与 Repository 测试覆盖。

这是应用层与数据库关系共同形成的隔离边界，不等同于面向不可信租户的完整托管平台安全认证。部署者仍需保护数据库文件、备份和主机权限。

## Push Architecture

- Push 订阅按已认证 Session 创建，绑定 user 与 session；浏览器订阅生命周期由 PUT /api/push/subscriptions 等接口同步。
- 用户可以撤销当前设备或指定订阅；Logout 与服务端 Session 撤销会一并撤销对应会话创建的 Push 订阅。
- 出站推送使用 VAPID 签名。VAPID 密钥与 subject 来自本地 `.env` 配置，配置加载时进行密钥配对与 subject 格式校验。
- 出站 endpoint 安全层（app/services/push_security.py）：仅接受 HTTPS、仅 443 端口、结构化 URL 校验、所有解析地址都必须满足公网/全局地址策略（含 IPv4-mapped IPv6 展开）、混合公私解析结果被拒绝、重定向被禁用、有限超时、保留标准 TLS 校验、禁用环境代理继承。校验与实际建连之间仍存在 DNS rebinding / TOCTOU 残余窗口，详见 SECURITY.md。
- 推送载荷是通用内容并指向 /dashboard，不包含 Personal Item 标题、正文或其他用户标识。
- Service Worker 处理 push 与 notificationclick 事件；认证状态和私有事项不进入 Service Worker Cache。

## Reminder Worker

- worker 是嵌入在应用进程内的轮询循环，必须通过 `REMINDER_WORKER_ENABLED=true` 显式启用；支持的单机拓扑是单应用实例。
- 每轮 sweep：将超时的 `sending` 投递对账为 `unknown` → 认领到期的 scheduled Reminder（`due`）并按其活跃订阅扇出创建 `queued` 投递 → 将不再可用的目标终态化 → 逐条领取 `queued → sending` 并出站投递。
- 每次投递都记录在 reminder_deliveries ledger 中，含 provider_status；provider 拒绝且订阅已失效（如 410）时同时失效订阅记录。
- 语义是 at-most-once：`sent` / `failed` / `unknown` 均为终态，没有自动重试；`unknown` 表示 Provider 结果不确定。
- worker 未启用时，应用访问触发的惰性 due 转移和应用内 fallback 仍然可用；Web Push 不可用或未配置也不影响 Reminder 使用。

## Database and Migration

- 默认数据库是 data/selfecho.db。
- SQLite 启用 foreign_keys、busy_timeout 和 WAL。
- 新空数据库直接初始化为 schema v4。
- 应用启动只验证已有数据库版本和必需结构，不执行隐式升级；遇到不支持的旧版本会拒绝启动并提示显式迁移。
- app/migrations/v004_reminders.py 提供显式的 v3→v4 迁移：`--check-only` 只读预检；正式迁移要求停止使用且已备份的数据库、输入确认文字、单事务执行，并通过迁移后行数守恒、schema 结构、foreign key 和 integrity 检查。
- app/migrations/v003_auth.py 保留通用的 v2 到 v3 显式迁移实现，属于历史兼容层，不存在直接 v2→v4 路径。

数据库、WAL/SHM、备份和真实数据不属于公开发行物。

## PWA and Caching

FastAPI 同源提供 API、静态资源和单页应用入口。Service Worker 只缓存 App Shell：HTML、CSS、JavaScript、Manifest 和图标；以 /api/ 开头的请求明确绕过 Service Worker Cache。导航离线时只能回退到已缓存 Shell，并不表示业务数据支持离线同步。

登录用户的 Capture 草稿临时保存在 sessionStorage，并按用户区分。认证状态和私有事项不写入 Service Worker Cache。Service Worker 文件自身以 no-cache 响应，便于更新缓存版本。

## Provider and Privacy Boundary

DeepSeek 与 OpenAI Responses API 是可选 Provider。没有 Key 时，数据库初始化、Bootstrap 和登录仍可工作；Capture 会先保存原文，再把 AI 阶段标记为配置失败。

启用 Provider 后，用户原文及整理所需的事项上下文会离开本机并发送给所选第三方；包含提醒意图或时间表达的文本参与 Reminder 提取，同样属于这条数据流。部署者必须自行评估供应商的数据保留、地域、账户安全和费用。AI_DEBUG_OUTPUT 可能包含个人输入或模型结果，不应在共享或生产环境启用。

Web Push 推送载荷为通用内容，不含 Personal Item 标题或正文。Push 订阅的 endpoint、p256dh 与 auth 材料是运维敏感数据，存储在自托管实例的 SQLite 中；数据库备份应按敏感数据保护。

## Community Edition vs Hosted Service

- Community Edition：位于公开仓库 github.com/XuZhilin2007/selfechoai，可自行运行，包含应用代码、测试、示例配置和社区文档。
- Hosted service：selfechoai.com，由仓库所有者独立运营。

公开版本不包含托管服务的数据库、用户数据、API Key、服务器配置、Nginx、TLS、备份、日志、部署 Runbook 或私有 Git 历史。Community Edition 的许可证不构成托管服务可用性、安全或支持承诺。
