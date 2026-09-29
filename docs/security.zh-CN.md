# 安全模型

[English](security.md) | **简体中文**

本文说明这个示例保护了什么、有意没有保护什么，以及在更严格的场景下该如何加固。对照框架是
[AWS Agent 安全框架](https://aws.amazon.com/cn/blogs/china/agent-security-framework-design-model-amazon/)
（运行时隔离、零常驻凭证、确定性工具授权、出网控制、可审计），每一项差距都直接写明。

> 这是示例代码，请结合 [README](../README.zh-CN.md) 中"不用于生产"的说明阅读。

## 这套部署是什么

每个用户在自己的 AgentCore microVM 里拿到**完整的 KiroCrew 开发工作台**（对话、终端、代码执行、
MCP）。这决定了威胁模型：

- **用户在自己的沙箱内是受信的。** Agent 能做的事，用户的终端同样能做。本示例防守的边界是
  **用户与用户之间**，以及沙箱与部署共享数据之间。
- 信任级别与笔记本上运行 KiroCrew 相同，但隔离更强：用每用户 microVM 取代本机，且不常驻任何共享
  存储的访问权限。
- 它**不是**受限的业务 Agent。那类 Agent 需要默认拒绝出网和逐工具授权，见[加固档位](#加固档位)。

## 已落实的控制

| 控制 | 实现方式 | 位置 |
|---|---|---|
| 每用户一个 microVM | 每个 Cognito subject 对应独立的 AgentCore 会话 | `infrastructure/functions/control/` |
| 每次调用校验身份 | AgentCore `customJWTAuthorizer` 校验 Cognito JWT | `infrastructure/modules/runtime-microvm/` |
| 用户 ↔ 沙箱 ↔ 会话绑定 | KMS 签名的绑定令牌（30 分钟，租约存活期间自动续期），Adapter 与 Broker 都会与 Cognito subject 比对 | `adapter/.../transport.py`、`persistence/lambda_handler.py` |
| 沙箱内无数据权限 | 执行角色只能验签、调用 Broker、写自身日志和指标，以及（可选）只读访问一个 Agent Registry。DynamoDB、S3 和 KMS 数据密钥只能经 Broker 访问 | `infrastructure/modules/runtime-common/main.tf` |
| 经 Broker 的短期存储访问 | Broker 校验绑定、确认记录仍指向该会话后，返回 SigV4 签名的预签名 URL | `infrastructure/functions/persistence/` |
| 加密检查点 | 每沙箱独立 KMS 数据密钥、VM 内加密、按代提交、恢复失败可回退 | [persistence.md](persistence.md) |
| Gateway 凭证不出 VM | 固定路由策略拒绝 `/api/token` 和 `/api/shutdown`，两端都有契约测试 | `contracts/kirocrew/0.3.0-route-allowlist.json` |
| 注册门禁 | 注册和登录经 auth Lambda，强制邮箱域名白名单 | `infrastructure/functions/auth/` |
| 无人值守唤醒不存密钥 | EventBridge Scheduler → 只有 IAM 角色的 Waker Lambda → 单独的 IAM 鉴权 Runtime 端点；Adapter 只在该端点接受调度调用 | [design-scheduled-jobs.md](design-scheduled-jobs.md) |
| 访问 Registry 不用用户凭证 | stdio SigV4 MCP 桥用执行角色签名，拒绝非 `https` 地址，权限限定为单个 Registry ARN 的四个只读动作 | `runtime/.../sigv4_mcp_proxy.py`、[operations.md](operations.md) |
| 租户隔离负向测试 | subject 不匹配、缺 JWT、伪造调度声明都会被拒绝 | `tests/unit/test_adapter_transport.py` |

## 凭证清单

"沙箱不持有 AWS 数据面权限"成立；"Agent 没有任何长期凭证"**不成立**：用户的 Kiro 登录态就在沙箱里。

| 凭证 | 持有者 | Agent/终端能否读取 | 是否进检查点 | 有效期 | 撤销方式 |
|---|---|---|---|---|---|
| Cognito access / refresh token | 浏览器 | 否 | 否 | 按 Cognito 配置 | Cognito 登出 / revoke |
| 绑定令牌 | 浏览器，随请求 | 仅传输中可见 | 否 | 30 分钟 | 过期、会话轮换 |
| Runtime-session 令牌 | Runtime 进程 | 是（同一 VM） | 否 | 平台会话寿命 | 过期 |
| 执行角色 STS 凭证 | VM 内所有进程 | 是 | 否 | 短期 | 修改角色策略 |
| 预签名 S3 URL | 持久化引擎 | 短暂可见 | 否 | 分钟级 | 过期 |
| Kiro Builder ID / SSO 令牌 | `~/.local/share/kiro-cli` | **是** | **是**，加密 | Kiro 管理 | `Sign out of Kiro` |
| 用户自行添加的 MCP / 第三方令牌 | `~/.kiro`、`~/.config`（视工具而定） | **是** | **是**，加密 | 由提供方管理 | 在提供方撤销 |

执行角色**由同一部署的所有沙箱共用**。今天这是安全的，因为它没有数据权限，Registry 也只有只读动作；
一旦往这个角色上加下游业务权限，就不再安全。

## 已知差距与边界

1. **登录是密码方式，不是 PKCE。** 悬浮面板把邮箱和密码发给 auth Lambda，由它调用 Cognito
   `ADMIN_USER_PASSWORD_AUTH`。用户池允许可选 MFA，但 MFA challenge 会被拒绝
   （`CHALLENGE_NOT_SUPPORTED`）。Control API 校验的是通用 scope `aws.cognito.signin.user.admin`，
   而不是已定义的 `kirocrew.control/invoke`。
2. **公网出口。** Runtime 使用 `NetworkMode = "PUBLIC"`。开发工作台需要联网（Kiro 服务、Git、包仓库、
   MCP），但这也意味着被提示注入的命令可以访问任意目标。
3. **没有确定性的逐工具授权。** 除被拒绝的路由外，KiroCrew 路由全部隧穿；工具审批依靠 KiroCrew 自身的
   应用层。Agent Registry **只负责发现**，不授予任何权限。
4. **第三方令牌与 Kiro 登录态一起落盘。** 用户在沙箱内登录的任何服务，都会随工作区进入检查点。
5. **检查点保留策略尚未生效。** `RetentionManager` 没有生产调用方（容器侧删除被有意禁止），定时触发的
   `audit` Broker 操作目前是空实现。在 Broker 侧清理实现之前，旧代数会持续累积。
6. **平台访问日志包含调用载荷。** Prompt 和文件内容可能出现在 `/aws/vendedlogs/bedrock-agentcore/...`
   中。Kiro 输出按正则脱敏（`redact_kiro_output`），这是尽力而为，不是保证。

## 加固档位

以下两档是规划，尚未实现。

**开发沙箱**（本示例，面向受信工程师）：

- 保留互联网出口，但阻止实例元数据、链路本地地址和内部网段，并记录 DNS/HTTP 目标。
- 登录改为 Authorization Code + PKCE 或企业 IdP，让部署不接触密码；启用 MFA；要求专用
  `kirocrew.control/invoke` scope。
- 第三方凭证从检查点移到凭证提供方（例如 AgentCore Identity），不再以文件形式放在
  `~/.kiro` / `~/.config`。
- 在 Broker 侧实现保留清理和真正的完整性审计。

**受限 Agent**（发邮件、CRM、GitHub 写操作、AWS 变更等业务动作）：

- VPC 网络模式，出口默认拒绝，只放行白名单（Kiro、Bedrock、Gateway、批准的仓库与软件源）。
- 高风险工具统一经 AgentCore Gateway，按用户、工具、资源、参数执行策略（Cedar）判定；删除、外发类动作
  加人工确认。下游系统仍负责业务状态校验。
- 执行角色保持最小；下游权限放在 Gateway Target 和凭证提供方一侧，不叠加到共享角色。
- 应用日志、AgentCore Observability trace 和 CloudTrail 用同一请求 ID 串联；敏感字段与 Prompt 正文
  从设计上不进日志，而不是依赖脱敏。

## 建议补充的测试

现有测试已覆盖绑定不匹配和伪造调度声明。扩大使用前建议补充：会话轮换后复用旧绑定令牌；在沙箱内尝试读取
其他租户的检查点对象；要求 Agent 上传 `~/.local/share/kiro-cli` 的提示注入用例；访问元数据与内部地址；
以及删除账号后 Cognito、DynamoDB、S3 对象版本和日志的清理。
