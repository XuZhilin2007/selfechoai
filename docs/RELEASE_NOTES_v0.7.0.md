# SelfEcho AI Community Edition v0.7.0 Release Notes

本文件描述 Community Edition v0.7.0 的准备内容。发布以 annotated tag 与 GitHub Release 为准；本文不声称 tag 或 Release 已经发布。

## Lifecycle / Schema 7

- `personal_items` 新增 `completed_at`、`trashed_at` 与 `status_before_trash`，并新增 `idx_personal_items_lifecycle` 与 `idx_personal_items_trash_retention` 索引；`CURRENT_SCHEMA_VERSION = 7`。
- Dashboard 演进为 Current / History / Trash 三个生命周期视图：完成写入真实 `completed_at`；移入回收站写入真实 `trashed_at` 并保留来源状态；恢复时回到已知来源，来源未知的 legacy 事项回到 Current。
- History 如实表达完成时间：没有真实完成时间的 legacy 完成事项显示「完成时间未知」，不会伪造时间戳。
- 永久删除仍只允许对回收站内事项执行。

## Trash Retention

- 回收站事项保留 30 天（`TRASH_RETENTION_DAYS = 30`）；嵌入式 retention worker 每小时分批清理过期事项（`TRASH_RETENTION_POLL_INTERVAL_SECONDS`、`TRASH_RETENTION_BATCH_SIZE`，上限 500）。
- purge 以数据库为先；Voice Original Audio 的物理删除沿用既有 deletion ledger，在提交后追溯执行。
- 两个新增运行时配置项已加入 `.env.example` 与配置校验。

## Bulk Selection and Pagination

- 事项列表支持长按（触屏/触控笔）进入多选、点击切换、全选本页，并提供键盘可达的选择入口与焦点连续性；路由/视图/页码变化会清空选择。
- 批量生命周期动作（完成、恢复到 Current、移入回收站、从回收站恢复、永久删除）单次最多 100 项，按原子事务执行；永久删除需要显式确认。
- Dashboard 分页大小为 100，与单次原子批量操作上限一致；响应包含 `page`、`page_size`、`total_items`、`total_pages`。

## Quiet Utility UI

- 全局界面收敛到以 typography、spacing、alignment 与内容层级为重的 Quiet Utility 基础；减少卡片堆叠、badge/button 过载与装饰性容器。
- 一个上下文只有一个 primary action；可逆生命周期操作（完成、恢复、移入回收站）不再添加多余确认，永久删除保持显式破坏性确认。
- 页面底色与主题色调整为 `#f6f4ed`；设计原则记录在 [UI/UX Foundation](UI_UX_FOUNDATION.md)。

## Capture / Reminder

- 新建 Reminder 默认日期从「明天」改为「今天」；快捷日期为今天 / 明天 / 后天 / 选日期。编辑已有 Reminder 仍加载真实保存的日期与时间，不添加过去时间猜测。
- Capture 页新增轻量 Upcoming Reminders 区块（约最近 3 条未到期 Reminder，空时隐藏）。
- TemporalParser 扩展确定性中文语法：中文数字小时、「中午」、「点钟」与 `HH:MM` / `HH：MM`；夏令时歧义或不存在的时间不猜测 fold。

## Providers

- DeepSeek 默认模型更新为 `deepseek-flash`；endpoint 保持 `https://api.deepseek.com`。`deepseek-chat` 与 `deepseek-reasoner` 仍是唯一 retired 别名。

## PWA / Static Revision

- 静态资源采用 `?v=0.7.0-community-ui-1` 版本化引用；Service Worker cache identity 为 `selfecho-ai-community-v0.7.0-ui-1`（opaque cache revision，不是产品版本号）。
- Service Worker 既有行为边界不变：navigation network-first、静态资源 cache-first、`/api/` 与非 GET 请求绕过、离线 Shell 回退、push 与 notification click 处理、`skipWaiting` / `clients.claim`。

## Migration Requirement

- v0.7.0 使用 schema v7。已有 v0.6 数据库必须显式迁移：停止应用 → 已验证备份 → `python -m app.migrations.v007_item_lifecycle --database data/selfecho.db --check-only` → 输入确认文字 `MIGRATE PUBLIC V6 TO V7` 执行 → 验证输出 → 重启。详见 [README](../README.md#upgrade-from-v060)。
- 全新安装直接创建 schema v7。legacy 回收站事项获得全新 30 天保留期；legacy 完成事项不写入虚构完成时间。

## Out of Scope

- Notes / Memo 生态（v0.8.0 方向）、Finance / Universal Capture（v0.9.0 方向）未包含在本版本。
- 长按阈值附近的 row highlight 边缘情况与 Capture 重要性/紧急性输入摩擦为已知 deferred debt，未在本版本处理。
