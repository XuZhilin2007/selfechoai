# SelfEcho Public Community Edition v0.8.1 Release Notes

本文件描述 Public Community Edition v0.8.1 的交付内容。发布以 annotated tag 与 GitHub Release 为准；本文不声称 tag 或 Release 已经发布。

## Snapshot positioning

v0.8.1 是当前计划中**最后一次 Private → Public 的 planned product selective sync**。此后本仓库继续作为独立的 self-hosted Community / Showcase snapshot 存在：

- 不承诺与 Hosted SelfEcho 的 feature / version parity；
- 不自动同步未来的 Private product features；
- 仍可接受 security fixes、严重 correctness fixes、必要的 compatibility / dependency maintenance，以及 Community-specific 的文档与修复；
- Hosted SelfEcho 是独立运营的服务，不会停留在 v0.8.1，也与本仓库不共享 implementation、deployment 或 schema history。

本版本不是 SelfEcho 整体停止开发，也不代表本仓库被放弃。

## Community Admission Foundation

新增四类进程内、内存有界的 admission 保护，在昂贵工作发生前尽早拒绝，并给出显式重试信息（`Retry-After`），不静默丢失用户输入：

- **认证 admission**：按来源地址（ASGI server 身份，不读转发头）、按账号、全站限制注册与登录尝试，在密码哈希与任何数据库写入之前拒绝。
- **AI / ASR provider admission**：按时间窗口与在途并发限制 Provider 调用，并限制发往 Provider 的 AI 请求体大小；被拒绝的输入以其原始形式保留。
- **持久存储 admission**：写入前按真实文件系统预留空闲空间（含 SQLite WAL allowance），限制 Voice 录音并发与 `extra_information` 增长；拒绝不修改或丢弃已保存内容，等量/缩小编辑保持可用。
- **Email 发送 admission**：按收件人与全站限制对外邮件，本地 challenge 状态变化前预留额度，且只在 Provider 边界计数。

默认值为有限的社区回退值（非任何 Hosted capacity policy），可通过 `AUTH_*`、`AI_ADMISSION_*`/`ASR_ADMISSION_*`、`STORAGE_*`、`EMAIL_SEND_*`/`EMAIL_VERIFICATION_RECIPIENT_*` 配置。详见 [README](../README.md#admission-and-storage-protection)。

## Schema v9 / Voice persistent state

- `voice_segments` 状态机新增持久化状态 `transcribed`：Provider 整段转写已完成并完整存储，但尚未进入 Capture Draft；`transcribed` 不等于 Draft acceptance，schema CHECK 保证无完整持久 final 不得进入接受流程，并记录 Streaming Voice 模型标识。`CURRENT_SCHEMA_VERSION = 9`。
- Draft acceptance 是独立、持久、幂等的状态迁移；启动时自动恢复未完成的 acceptance（无需 ASR）。
- 新增显式 v8→v9 迁移：`python -m app.migrations.v009_voice_segment_state`（`--check-only` 只读预检；确认文字 `MIGRATE PUBLIC V8 TO V9`）。迁移在单一 transaction 中重建 `voice_segments`，逐行保留数据、自增高位水印与既有索引，并验证数据守恒、schema、foreign key 与 integrity。详见 [README](../README.md#upgrade-from-v080)。
- 全新安装直接创建 schema v9。

## Streaming Voice

完整的可选实时语音闭环：录音 → durable Original Audio + 实时流式转写 → 持久化 `transcribed` → Draft acceptance → 既有 Draft / Final Save 流程。

- 浏览器经 AudioWorklet 采集 16 kHz mono PCM16，经同源 WebSocket 发送到 Provider 进行 duplex 识别；MediaRecorder 并行独立录制需要保留的 Original Audio。
- 句级实时文本只是预览，永远不会自动成为 Draft 文字；整段 final 必须完整持久化后才能进入 Draft；用户仍须显式执行 Final Save 才进入 Item 与 AI 流程。
- Cancel 终止 attempt 权威性，迟到的 Provider 事件无法复活已取消 attempt。Cancel 不能撤回已经发送给 Provider 的音频——产品文案与隐私边界均按此事实表述。
- Original Audio 上传与 Provider 会话完全独立：Provider / 网络 / admission 失败不会丢弃已安全保存的录音；失败录音保持显式可重试（batch ASR）或可删除。Provider 失败时预留的存储容量通过有界 handoff 移交给迟到的上传。
- Streaming 与 batch ASR 共享同一套 admission 保护；已开始的 streaming attempt 失败不会自动静默回退到 batch。
- 对 batch 有效但不支持 streaming 的配置（如共享 `dashscope.aliyuncs.com` 端点）继续使用既有非流式录制路径：应用将 streaming 标记为不可用，行为与 v0.8.0 一致。
- Voice 关闭的实例无需任何 Streaming 配置；流式端点要求 workspace 专用 HTTPS 地址。

## Dashboard Pin shortcut

Dashboard Current 行新增快捷 Pin 控件（`PATCH /api/items/{id}` 的既有 `is_pinned` 字段）：多选模式自动隐藏，pending / 失败状态有明确呈现，失败不锁死按钮，跨页签/路由切换有上下文防护。不改变 Pin 排序、Detail 控制、生命周期与既有交互。

## Registration modes: closed / invite / open

- `AUTH_REGISTRATION_MODE` 支持 `closed`（默认）、`invite`、`open`。`open` 取消邀请码要求并直接进入登录态，仅供面向自助注册的实例显式开启。
- 新增未认证 `GET /api/auth/config`，只返回真实注册模式；注册 / 登录页按真实 mode 呈现：closed 不再提供必然失败的注册表单。
- 认证 admission 限制在每种模式同等生效；首用户 bootstrap 流程不受影响。不包含 email verification 注册、password reset 或任何新 identity system。

## Capability-aware Web Push UI

前端先建立 server capability（`/api/push/config` 的 `available` / VAPID key），再解释浏览器状态，从而正确区分：server 不可用/关闭、浏览器不支持、permission denied、可用但未订阅、已订阅。server capability 未建立时不再误导用户发起 browser permission 请求。Push 的 SSRF / redirect / proxy / VAPID 安全契约与 v0.8.0 完全一致，无任何变化。

## PWA / Static Revision

- 静态资源采用 `?v=0.8.1-stream-1` 版本化引用；Service Worker cache identity 为 `selfecho-ai-community-v0.8.1-stream-1`（opaque cache revision，不是产品版本号）。`voice-pcm-worklet.js` 进入 precache shell。
- Service Worker 既有行为边界不变：navigation network-first、静态资源 cache-first、`/api/` 与非 GET 请求绕过、离线 Shell 回退、push 与 notification click 处理。

## Compatibility and Upgrade Facts

- `CaptureDraftResponse` 新增 `streaming_voice_available: bool`（默认 `false`）；`VoiceSegmentPublic.transcription_status` 可能出现新值 `transcribed`。
- `RegisterRequest.invite_code` 变为可选（open 模式可省略；invite 模式仍必填）。
- 依赖新增显式声明 `websockets>=14,<18`（此前经 `uvicorn[standard]` 传递）。
- 既有 batch Voice、Email privacy、Push 安全、Cookie 行为、历史迁移与 bootstrap 契约均无回归。

## Known limitations

- Streaming Voice 的真实浏览器（AudioWorklet / MediaRecorder 端到端）与真实 Provider 的 wss 联调、真实 HTTPS + 反向代理下的 WebSocket 部署验证，仍属 operator 侧 real-environment acceptance，不在本仓库测试覆盖内。
