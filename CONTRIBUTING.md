# Contributing to SelfEcho AI

感谢你关注 SelfEcho AI Community Edition。项目目前是一个小规模、早期的个人开源项目，贡献应优先改善真实使用问题，并保持现有 MVP 边界。

## Contribution Flow

1. Fork 仓库，从最新默认分支创建范围清晰的分支。
2. 在本地安装开发依赖并运行测试。
3. 只提交与目标直接相关的最小变更。
4. 提交 Pull Request，说明问题、行为变化、验证方式和隐私影响。

较大的功能、数据模型变化或产品方向调整，请先创建 Issue 讨论。Roadmap 中的想法不代表已经实现，也不代表已经承诺交付。

## Local Setup

~~~bash
python -m venv .venv
python -m pip install -e ".[dev]"
cp .env.example .env
python -m app.bootstrap
python -m pytest
~~~

Windows PowerShell 可用 Copy-Item .env.example .env，并通过 .venv\Scripts\Activate.ps1 激活环境。

## Scope and Quality

- 保持 Capture、AI Structuring、事项管理、Reminder、可选 Voice Capture 与 PWA 的当前 MVP 范围。
- 不要把所有 Personal Item 强制转成传统 Todo。
- 原始输入必须先保存；未知信息不能由 AI 猜测补全。
- 涉及 Authentication、Session、CSRF 或 Repository 查询时，必须补充或更新安全测试与多用户隔离测试。
- Reminder 变更必须保持 per-user ownership，并补充或更新所有权/隔离测试。
- 数据库 schema 或迁移变更必须携带迁移与回归测试。
- Voice 变更必须保持 per-user 所有权隔离与 VOICE_STORAGE_ROOT 路径 containment；测试只使用合成媒体 fixture，不得提交真实个人录音或真实 Alibaba 凭据；保留失败显式可恢复（Retry/Delete、不自动重试）与删除 lifecycle 语义；麦克风资源与前端录音状态必须正确清理。
- Push 变更必须保持 Session 与用户隔离；订阅数据不能跨用户或跨 Session 可见。
- 出站 endpoint / 安全逻辑变更必须附带 SSRF 回归测试。
- 前端 Push 或 Service Worker 变更需要运行 JS/SW 合同测试（Node 内置 test runner）。
- 前端保持 mobile-first，并验证窄屏、触控和 PWA 基本流程。
- Provider 实现不得在普通日志中记录原始用户文本、完整模型输入/输出、API Key 或 Session。
- 生产服务器、Nginx、TLS、真实域名和私有运维配置不属于 Community Edition 的贡献范围。

## Test Data and Secrets

只使用合成测试数据。不要提交真实用户文本、数据库、备份、日志、Cookie、邀请代码、API Key、.env 或由真实数据生成的截图。示例中的邮箱、Token 和 Provider 响应必须明显是虚构值。Push 相关 fixture 只能使用合成的 endpoint 与 VAPID 材料，不得出现真实推送端点、真实密钥或个人数据。Voice 相关测试只使用合成的媒体 fixture 与合成凭据，不得提交真实个人录音或真实 Alibaba Key。

提交 Pull Request 前至少运行：

~~~bash
python -m pytest
~~~

修改前端 Push 或 Service Worker 时，还需运行对应的合同测试，例如：

~~~bash
node --test tests/test_frontend_push.mjs tests/test_service_worker_push.mjs
~~~

如果修改打包、静态资源或启动流程，还应执行 wheel 构建、全新环境安装和本地启动检查。
