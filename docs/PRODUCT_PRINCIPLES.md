# SelfEcho AI Product Principles

这些原则是 Community Edition 当前产品行为的稳定边界。

## Preserve First

- 用户输入不能丢失。Capture 必须先保存原始文本，再调用 AI。
- 原始输入是可追溯事实，不因整理成功、失败或重新处理而被覆盖。
- AI 失败应留下清晰状态并允许重试，而不是让用户误以为记录成功后又消失。

## AI Organizes; the User Decides

- AI 用于提取、归类和组织，不替用户做最终决定。
- Legacy Importance / Urgency 已退出 active 产品模型：AI 不推断、不写入、不询问这两个字段；用户表述的后果、约束与时间压力作为事实上下文保留在 `extra_information`。
- 不根据缺失信息猜测截止时间或行动。
- 未知信息保持 unknown，用户可以稍后补充或修改。
- 事项排序是辅助视图，不是自动决策或自治执行。

## Preserve Human Context

结构化结果应保留用户明确表达的顾虑、限制、候选方案、权衡、决策背景和个人理由。Personal Item 可以是想法、记录、购买比较、项目或决策，不必被强制改写成传统 Todo。

## Privacy and Ownership

- 每条输入、事项和 Session 都必须有明确的用户边界。
- 默认不公开注册；自托管者显式决定首位用户和后续注册方式。
- Provider 调用只发送完成当前整理所需的内容，日志不应记录原始文本、完整模型输出或秘密。
- 外部 Reminder 渠道只发送完成投递所需的最少数据；通用 Email Reminder 不发送 Personal Item title/body、原始 Capture、item ID 或 reminder ID。
- Self-host operator 自行拥有并管理 Tencent SES、Web Push、AI 与 Voice 等外部集成的账户、凭据、配置、费用和 Provider 隐私边界。
- Provider acceptance 与 recipient delivery 是不同事实；结果不确定时保留 `unknown`，不把不确定性包装成“已送达”。
- 数据库、备份、日志和截图都可能含个人信息，不能作为公开示例。

## Current Release Boundary

v0.8.0 提供 Capture、AI Structuring、Current / History / Trash 事项生命周期、30 天回收站保留、批量生命周期操作（单次最多 100 项）、Dashboard 分页、多用户认证隔离、Quiet Utility Web/PWA 使用体验，以及作为当前辅助能力的一次性 Reminder。Current 排序由用户显式 Pin 与 Deadline 确定性决定（app/priority.py），不再使用 weighted priority score；legacy Importance / Urgency 退出 active 模型，AI 不再推断或写入。Reminder 可以手动创建，也可以由 AI 提取意图与时间表达后经应用确定性解析生成；新建 Reminder 默认「今天」，歧义时间可以保持 `needs_confirmation` 状态，而不必被强行确定；没有 Reminder 的事项不再显示泛化提醒邀请。Personal Item 不会被强制变成传统 Todo，Reminder 只辅助用户在合适的时间回看与行动。完成或回收事项会取消其活跃 Reminder；恢复事项不会复活已取消的 Reminder。History 如实表达完成时间：legacy 完成事项没有真实完成时间时保持未知，不会伪造。

可选 Web Push 与 Email Reminder 是彼此独立、显式的外部渠道，不存在隐藏 fallback；渠道 eligibility 在 Reminder 第一次进入 due lifecycle 时确定，不是 AI 决策，也不是每条 Reminder 的隐式选择。Email 默认关闭，只在用户设置并验证独立 Reminder Email、自托管者配置 Tencent SES、且 Reminder worker 已启用时参与定时投递。任何外部渠道不可用都不改变应用内 due Reminder 的存在与展示。

可选 Voice Capture（默认关闭）延续同一原则：先捕获，保留原始用户输入，AI 只辅助而不决定。按住录音、上滑取消；转写文本只追加进可编辑的 Capture Draft，最终内容在 Final Save 前由用户修改确认；失败的转写保持显式状态，可重试或删除，原始录音不会因此静默丢失，转写成功也不会替用户触发 AI 整理。

Flexible Planning Assistant、日历集成和自治 Agent 仍是未来方向；这些不应在文档中描述为已完成功能。
