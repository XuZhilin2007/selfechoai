# Security Policy

## Supported Versions

SelfEcho AI Community Edition 目前仍处于早期阶段，仅维护当前 0.4.x 系列。

| Version | Supported |
| --- | --- |
| 0.4.x | Yes |
| < 0.4 | No |

## Reporting a Vulnerability

请不要在公开 Issue、Discussion、Pull Request、日志或截图中披露漏洞细节、API Key、Session、用户数据或可利用步骤。

公开仓库启用 GitHub Private Vulnerability Reporting 后，请通过仓库 Security 页面中的 **Report a vulnerability** 私下提交。若该入口暂不可用，请使用你与维护者之间已有的可信私下渠道，只发送最少必要信息；本项目不在文档中虚构尚未建立的安全邮箱。

报告建议包含：受影响版本、复现条件、预期与实际行为、影响范围，以及不含真实用户数据的最小复现。维护者确认可安全公开前，请保持细节私密。

## Self-hosted Security Scope

Community Edition 提供应用代码和本地运行默认值，不提供托管安全承诺。每个部署者负责自己的：

- 操作系统、反向代理、TLS、网络访问控制与安全更新；
- 数据库、备份、日志、文件权限和数据保留策略；
- APP_ORIGIN、HTTPS Cookie、注册模式和邀请代码配置；
- DeepSeek、OpenAI 或其他外部服务的账户、费用与隐私设置。

生产部署必须使用 HTTPS，并将 AUTH_COOKIE_SECURE 设为 true。不要公开 .env、数据库、WAL/SHM、备份、日志或真实截图。

如果 Key、邀请代码、Session 或数据库曾被意外披露，应立即撤销或轮换相关凭据，终止受影响 Session，检查日志与 Git 历史，并按实际风险通知受影响人员。仅从当前文件删除秘密并不能消除历史泄露。

## Web Push Security（Community self-hosting）

Community v0.4.0 的 Web Push 是可选功能，默认关闭。启用它意味着部署者接受以下安全责任与边界。

### VAPID

- VAPID private key 是秘密凭据：只保存在未跟踪的本地 `.env` 中，绝不提交到 Git，也不写入示例、日志或截图。
- 密钥对由 self-host operator 自行生成；应用启动时会校验公私钥配对与 subject 格式（mailto 或 HTTPS URI）。

### Subscription data

Push 订阅保存于自托管实例的 SQLite，包含 endpoint、p256dh 和 auth 材料。这些属于 operationally sensitive data：它们可以标识并触发对特定浏览器/设备的推送，也可能暴露所使用的推送服务。数据库文件和备份都应按敏感数据保护。

### Outbound defense

应用对出站 Web Push 请求实施了以下防御层：

- 仅接受 HTTPS endpoint；
- 结构化 URL 校验（长度、控制字符、userinfo、fragment 等）；
- 仅允许默认 443 端口策略；
- 所有 DNS 解析结果都必须满足公网/全局地址策略，直接 IP 字面量同样校验；
- 混合公私解析结果被整体拒绝；
- 重定向被禁用；
- 有限超时；
- 保留标准 TLS 证书校验；
- 禁用环境代理继承（不读取 HTTP(S)_PROXY 等环境变量）。

### Residual risks

上述防御不能等同于绝对安全：

- DNS 校验与实际建立网络连接之间仍存在残余的 DNS rebinding / TOCTOU 窗口；
- 已知某个精确高熵 Push endpoint 的请求者，可能通过跨用户碰撞行为获得有限的存在性信息。

建议把网络层出站限制（仅允许访问已知 Push provider 的地址段）作为 defense-in-depth。

### Deployment

Community v0.4.0 正式支持单应用实例与嵌入式 worker 的部署拓扑。在不受信任的多用户自托管场景中，管理员应额外考虑 host/container/network 层面的隔离与出站策略。
