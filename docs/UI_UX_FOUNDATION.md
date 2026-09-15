# SelfEcho AI v0.7 UI/UX Foundation

- 文档状态：current v0.7.0 foundation
- 适用范围：v0.7.0 已实现界面；不是独立组件库或独立 release manifest

## 1. North Star

SelfEcho 是用户一天会多次打开的安静个人工具。界面服务于 `Capture → Structuring → Review → Continue`，不展示系统自身的复杂度，也不模仿项目管理、SaaS dashboard 或 AI chatbot。Capture 始终是最高优先级。

核心原则：内容优先于容器、层级优先于装饰、确定性优先于重复确认、可逆操作优先于打断、用户语言优先于 backend 语言。用户输入、上下文与最终决定权必须保留。

## 2. Visual hierarchy

- 以 typography、spacing、alignment 和轻分隔线组织内容；page title、section title、body、secondary/meta 与 supporting text 构成小而稳定的层级。
- 使用暖中性页面底色、深色正文和 muted 次要文字。SelfEcho Green 只用于主动作、活动导航、选择、焦点和有限正向反馈。
- 普通内容默认无阴影；阴影主要属于 dialog / sheet / overlay。圆角只使用少量一致值，不给每个对象添加边框或圆角。
- Surface 只在内容确实需要独立上下文或状态边界时使用。Capture 主输入是明确 surface；Current、History、Trash 和 Detail 的常规内容优先使用 section / row / divider。

## 3. Actions

一个上下文通常只有一个 primary action。Secondary action 更安静，text action 用于低权重操作，destructive action 只用于永久删除等真正不可逆结果。

- Capture Save 是明确 primary；Discard Draft 与其分离，并表达破坏性后果。
- Move to Trash 可恢复，不添加 destructive confirmation；Permanent Delete 与清空/永久删除本页使用红色和明确确认，Cancel 是安全默认。
- 所有按钮必须有可理解的 disabled 状态和足够触控尺寸；不要把同一组动作全部做成填充绿色按钮。

## 4. Navigation, sections, rows

顶层导航固定为 `记录 / 事项 / 账户`，使用单一轻量 selected indicator。移动端优先保证触达与 safe-area；桌面端使用合理 max-width 和行长。

- Current 是工作区，可以显示影响下一步行动的优先级、Reminder、needs-confirmation、processing、failure 与 selection 状态，但不把每个字段变成 chip/badge。
- History 比 Current 更轻，只表达 title 与真实可知的 completion 信息；未知 legacy 时间必须保持未知。
- Trash 是 secondary management surface，只表达 title、近似 retention 和 selection；页面本身不使用危险红，红色只属于永久删除。
- Upcoming Reminder 位于 Capture 主区域之后，最多约 3 条；整行进入 Detail，空时隐藏。
- Detail 使用连续内容层级，而不是堆叠独立 cards；生命周期动作集中在末端，不提供重复 mutation path。

## 5. Status, feedback, empty states

反馈只说明“发生了什么”或“下一步要做什么”。Pending、success、warning、error 和 empty state 使用一致、简短、可行动的用户语言；避免 provider、pipeline、数据库状态或技术错误码。错误必须如实表达并保留恢复入口，不把未知包装成成功。

Empty state 不使用插画、巨型空卡、激励文案或功能营销。Voice 保留 Original Audio、transcription、Retry、failure recovery 和语义上必要的 Delete，但不形成独立 AI assistant 视觉身份。

## 6. Dialogs

Dialog 顺序为 title、简短 context/consequence、actions。一个 clear primary；破坏性确认中 Cancel 是安全默认，destructive action 明确。可逆操作不新增确认。移动 sheet 和桌面 dialog 共享相同语义、focus 与 action hierarchy。

## 7. Responsive and accessibility

- 支持窄屏 mobile、standalone PWA 与 desktop；主要内容不得产生横向滚动，固定/粘性元素不得遮挡 safe-area。
- 使用原生 button/link/input 语义、可见 `:focus-visible`、自然 Tab 顺序、合理 heading hierarchy、必要且克制的 ARIA 与 live feedback。
- 选择模式保留长按、键盘与焦点连续性；touch target 不因视觉减重而缩小。
- 动效短、功能性且非必要；`prefers-reduced-motion` 必须关闭非必要 transition/animation。
- Public Login/Register 界面只表达 SelfEcho 产品品牌与 Community Edition 定位；自托管部署者自行决定是否添加其部署所需的本地合规信息。

## 8. Inheritance rule

未来功能先继承现有 token、actions、sections、rows、dialogs、navigation 和 accessibility patterns；只有已证明的真实需求无法由现有模式表达时才扩展。先证明复用，再抽象；不要为假设中的 Notes 或其他未来模块预建 enterprise design system。
