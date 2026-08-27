# SelfEcho AI Architecture

本文描述 Community Edition v0.3.0 的当前实现，不代表托管服务的基础设施设计。

## System Overview

~~~text
Vanilla JavaScript PWA
        │ same-origin HTTP + JSON
FastAPI application
        ├── Authentication / CSRF
        ├── Capture and Item APIs
        ├── Background AI processing
        ├── SQLite schema v3
        └── DeepSeek or OpenAI provider boundary
~~~

- app/main.py 组装 FastAPI、生命周期、API、静态文件和 PWA 路由。
- app/repository.py 处理 Personal Item 与原始输入持久化。
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
        └── failure: keep original input and record failure state
~~~

原始输入在 Provider 调用前提交。AI 失败不会删除输入；失败记录可重试。启动时，应用会恢复被中断的 processing 状态，并重新调度仍 pending 的输入。

AI 输出通过 Pydantic schema 验证。Provider 只负责提取和组织信息；重要性、计划与最终行动仍由用户确认。向已有事项补充输入或重新整理时，服务会使用该事项的输入历史。

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

schema v3 为 users、user_sessions、personal_items 和 item_inputs 建立显式所有权关系。业务 API 从已验证 Session 获取 current user，Repository 查询和修改同时使用记录 ID 与 user_id。跨用户访问按不存在处理，并由 API 与 Repository 测试覆盖。

这是应用层与数据库关系共同形成的隔离边界，不等同于面向不可信租户的完整托管平台安全认证。部署者仍需保护数据库文件、备份和主机权限。

## Database and Migration

- 默认数据库是 data/selfecho.db。
- SQLite 启用 foreign_keys、busy_timeout 和 WAL。
- 新空数据库直接初始化为 schema v3。
- 应用启动只验证已有数据库版本和必需结构，不执行隐式升级。
- app/migrations/v003_auth.py 保留通用的 v2 到 v3 显式迁移实现，生产迁移仍应先备份并独立演练。

数据库、WAL/SHM、备份和真实数据不属于公开发行物。

## PWA and Caching

FastAPI 同源提供 API、静态资源和单页应用入口。Service Worker 只缓存 App Shell：HTML、CSS、JavaScript、Manifest 和图标；以 /api/ 开头的请求明确绕过 Service Worker Cache。导航离线时只能回退到已缓存 Shell，并不表示业务数据支持离线同步。

登录用户的 Capture 草稿临时保存在 sessionStorage，并按用户区分。认证状态和私有事项不写入 Service Worker Cache。Service Worker 文件自身以 no-cache 响应，便于更新缓存版本。

## Provider and Privacy Boundary

DeepSeek 与 OpenAI Responses API 是可选 Provider。没有 Key 时，数据库初始化、Bootstrap 和登录仍可工作；Capture 会先保存原文，再把 AI 阶段标记为配置失败。

启用 Provider 后，用户原文及整理所需的事项上下文会离开本机并发送给所选第三方。部署者必须自行评估供应商的数据保留、地域、账户安全和费用。AI_DEBUG_OUTPUT 可能包含个人输入或模型结果，不应在共享或生产环境启用。

## Community Edition vs Hosted Service

- Community Edition：位于未来公开仓库 github.com/XuZhilin2007/selfechoai，可自行运行，包含应用代码、测试、示例配置和社区文档。
- Hosted service：selfechoai.com，由仓库所有者独立运营。

公开版本不包含托管服务的数据库、用户数据、API Key、服务器配置、Nginx、TLS、备份、日志、部署 Runbook 或私有 Git 历史。Community Edition 的许可证不构成托管服务可用性、安全或支持承诺。
