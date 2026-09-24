# SelfEcho AI Community Edition v0.8.0 Release Notes

本文件描述 Community Edition v0.8.0 的准备内容。发布以 annotated tag 与 GitHub Release 为准；本文不声称 tag 或 Release 已经发布。

## Capture & Dashboard Simplification

- 重要性 / 紧急性（Importance / Urgency）退出 active 产品模型：AI 不再推断、写入或询问这两个 legacy 字段；用户表述的后果、约束、候选与时间压力作为事实上下文保留在 `extra_information`。已有事项上的人工确认值继续保留，历史数据不会被清除或改写。
- Current 仪表盘排序从 weighted priority score 改为确定性排序键：显式 Pin 优先，其后按 Deadline——已过期事项按截止日晚者在前，未到期事项按截止日早者在前（同一天内有时刻者先于纯日期），无截止日事项按创建时间新者在前，最终以 id 稳定决序。排序先于分页执行。
- 新增 persisted Pin：`personal_items.is_pinned` 由用户在 Detail 页通过 `PATCH /api/items/{id}` 的 `is_pinned` 布尔字段控制；仅 active 事项可修改（否则 409），与 `status` 不得在同一请求中同时修改（409），跨 complete / trash / restore 保留并在恢复 active 后重新生效。AI 输出不包含、也不能写 Pin。
- Dashboard 不再展示重要性 / 紧急性信号、快捷确认按钮或编辑字段；`priority_score` 恒为 `null`。

## Deadline Temporal Semantics

- Deadline 保持输入精度：纯日期是浮动日历日期（不加午夜、不加时区偏移），带时刻的 naive 值是本地墙钟约定，带偏移的值是绝对 instant。持久化值在读取与 API 返回时不变。
- 过期判定与排序使用用户 profile 时区：纯日期与用户本地今天比较；aware 值按 instant 比较（含 DST 重复小时）；naive 值按本地墙钟比较。等于当前时刻不算过期。
- AI 提取使用按用户 profile 时区计算的 `current_local_date` 做 relative-date grounding，不再使用服务器本地日期。
- 过期 Reminder 呈现按完整 24 小时周期显示「已过期 N 天」；不足一天仍使用分钟 / 小时表述。跨过本地午夜本身不累计天数。

## Reminder Invitation Retirement

- Capture 泛化提醒邀请移除：没有 Reminder 的 active 事项不再在 Capture 快速确认区显示「需要提醒吗？」邀请；`show_reminder_prompt` 响应字段保留且恒为 `false` 以兼容旧客户端。`reminder-prompt/dismiss` 端点仍可用，但不再有入口触发它。
- 明确的 `needs_confirmation` Reminder 流程、手动创建 / 修改 / 取消 Reminder 与应用内 due 回退全部保持不变。

## Lifecycle / Schema 8

- `personal_items` 新增 `is_pinned INTEGER NOT NULL DEFAULT 0 CHECK (is_pinned IN (0, 1))`；`CURRENT_SCHEMA_VERSION = 8`。
- 全新安装直接创建 schema v8；已有 v0.7 数据库必须显式迁移（见下）。

## Migration Requirement

- v0.8.0 使用 schema v8。已有 v0.7 数据库必须显式迁移：停止应用 → 已验证备份 → `python -m app.migrations.v008_item_pin --database data/selfecho.db --check-only` → 输入确认文字 `MIGRATE PUBLIC V7 TO V8` 执行 → 验证输出 → 重启。迁移在单一 transaction 中新增 Pin 列并验证行数守恒、历史事项全部未置顶、schema、foreign key 与 integrity。详见 [README](../README.md#upgrade-from-v070)。
- 全新安装直接创建 schema v8，历史事项一律以未置顶状态迁移。

## PWA / Static Revision

- 静态资源采用 `?v=0.8.0-community-ui-1` 版本化引用；Service Worker cache identity 为 `selfecho-ai-community-v0.8.0-ui-1`（opaque cache revision，不是产品版本号）。已安装的 v0.7.0 PWA 会在下一次访问时通过新 cache revision 获取本版本全部前端变化（Pin 控件、Deadline 呈现、priority UI 移除、过期 Reminder 文案）。
- Service Worker 既有行为边界不变：navigation network-first、静态资源 cache-first、`/api/` 与非 GET 请求绕过、离线 Shell 回退、push 与 notification click 处理、`skipWaiting` / `clients.claim`。

## Compatibility and Upgrade Facts

- API 契约保持向后兼容：`DashboardItem` / `PersonalItemPublic` 新增 `is_pinned` 字段（默认 `false`）；`PATCH` 新增可选 `is_pinned` 布尔字段。不发送新字段的旧客户端行为不变。
- legacy `importance` / `urgency` 数据库列与 API 字段保留：既有值继续返回，手动 PATCH 仍可带确认修改它们，但 AI 永远不再写入，排序不再使用它们。
- Provider 配置、Email、Voice、Push 的行为与配置边界在本版本无变化。
