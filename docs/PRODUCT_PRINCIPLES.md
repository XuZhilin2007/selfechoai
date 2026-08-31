# SelfEcho AI Product Principles

这些原则是 Community Edition 当前产品行为的稳定边界。

## Preserve First

- 用户输入不能丢失。Capture 必须先保存原始文本，再调用 AI。
- 原始输入是可追溯事实，不因整理成功、失败或重新处理而被覆盖。
- AI 失败应留下清晰状态并允许重试，而不是让用户误以为记录成功后又消失。

## AI Organizes; the User Decides

- AI 用于提取、归类和组织，不替用户做最终决定。
- 不根据缺失信息猜测重要性、紧迫性、截止时间或行动。
- 未知信息保持 unknown，用户可以稍后补充或修改。
- 事项排序是辅助视图，不是自动决策或自治执行。

## Preserve Human Context

结构化结果应保留用户明确表达的顾虑、限制、候选方案、权衡、决策背景和个人理由。Personal Item 可以是想法、记录、购买比较、项目或决策，不必被强制改写成传统 Todo。

## Privacy and Ownership

- 每条输入、事项和 Session 都必须有明确的用户边界。
- 默认不公开注册；自托管者显式决定首位用户和后续注册方式。
- Provider 调用只发送完成当前整理所需的内容，日志不应记录原始文本、完整模型输出或秘密。
- 数据库、备份、日志和截图都可能含个人信息，不能作为公开示例。

## Current Release Boundary

v0.4.0 提供 Capture、AI Structuring、事项生命周期管理、多用户认证隔离、Web/PWA 使用体验，以及作为当前辅助能力的一次性 Reminder。Reminder 可以手动创建，也可以由 AI 提取意图与时间表达后经应用确定性解析生成；歧义时间可以保持 `needs_confirmation` 状态，而不必被强行确定。Personal Item 不会被强制变成传统 Todo，Reminder 只辅助用户在合适的时间回看与行动。完成或回收事项会取消其活跃 Reminder；恢复事项不会复活已取消的 Reminder。可选 Web Push 与嵌入式 Reminder worker 只影响提醒的送达方式，不改变上述语义。

Flexible Planning Assistant、日历集成和自治 Agent 仍是未来方向，Voice Capture 不属于 Public v0.4.0；这些不应在文档中描述为已完成功能。
