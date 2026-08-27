# Security Policy

## Supported Versions

SelfEcho AI Community Edition 目前仍处于早期阶段，仅维护当前 0.3.x 系列。

| Version | Supported |
| --- | --- |
| 0.3.x | Yes |
| < 0.3 | No |

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
