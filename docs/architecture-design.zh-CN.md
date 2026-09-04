# KiroCrew on AgentCore 架构设计

本文面向懂技术但不熟悉本项目的读者，客观描述整个系统的架构：组件分工、身份与
信任链、存储分层、备份与恢复的执行模型、以及多租户隔离。读完应能回答这几个
问题：请求怎么走？登录状态和文件存在哪里、由谁备份到哪里？一个用户为什么看
不到另一个用户的数据？

配图均在 `docs/` 下（SVG 源文件 + PNG）：

| 图 | 文件 | 内容 |
|---|---|---|
| 图 1 | `persistence-state-sync.png` | 组件与状态分层总览 |
| 图 2 | `auth-token-chain.png` | 身份与令牌链时序 |
| 图 3 | `checkpoint-data-path.png` | 备份数据通路时序 |
| 图 4 | `persistence-restore-flow.png` | 启动 / 恢复决策流程 |
| 图 5 | `persistence-checkpoint-flow.png` | checkpoint 提交步骤细节 |
| 图 6 | `kiro-login-persistence.png` | Kiro 登录态的持久与丢失路径 |

---

## 1. 系统概览

KiroCrew 本是一个本地应用：浏览器 SPA 通过 `127.0.0.1` 与本机 gateway 通信。
本项目把这套体验原样搬进云端：**每个用户一台 Amazon Bedrock AgentCore
microVM 沙盒**，上游 SPA 与 gateway 不做任何修改，浏览器里的回环调用被前端
shell 拦截、封装成协议信封，经 AgentCore 数据面送进沙盒内回放（图 1）。

组件与职责：

| 组件 | 形态 | 职责 |
|---|---|---|
| 前端 Shell + 悬浮面板 | 浏览器内 JS | Cognito 登录、沙盒启停按钮、拦截 SPA 的回环 HTTP/SSE/WebSocket |
| Amazon Cognito | 托管服务 | 用户身份，签发 JWT |
| Control API | API Gateway + **Control Lambda** | 沙盒生命周期（建档、租约、状态机、Start/Stop safely）、签发 binding token |
| AgentCore Runtime | 托管数据面 + microVM | 每次调用验 JWT，把请求路由到该会话专属的 microVM |
| 沙盒内运行时 | microVM 内进程 | 协议适配器（校验信封、路由策略）、supervisor（管 gateway 进程）、checkpoint/restore 引擎、kiro-cli 登录管理 |
| Persistence Broker | **Lambda**（不对浏览器暴露） | 备份/恢复的授权门卫：验 binding token、发预签名 URL、托管沙盒数据密钥、记账 |
| S3 / DynamoDB / KMS | 托管服务 | 加密 checkpoint 存储 / 沙盒状态与代数指针 / 签名密钥与每沙盒数据密钥 |

关键的面分离：**聊天与终端流量不经过任何 Lambda**——浏览器直连 AgentCore
数据面进入 microVM。Lambda 只出现在低频控制面动作（启停、授权、记账）里，
不在数据路径上。

## 2. 身份与信任链（图 2）

系统里有两层令牌，各证明一件事：

| 令牌 | 签发者 | 证明什么 | 有效期 | 谁验证 |
|---|---|---|---|---|
| Cognito JWT | Cognito（PKCE 登录） | **这个人是谁** | Cognito 配置 | API Gateway authorizer；AgentCore 数据面每次调用（CustomJWTAuthorizer） |
| binding token | Control Lambda，用 **KMS 非对称私钥**（RSASSA-PSS-SHA256）签名 | **这个沙盒、这个会话属于这个人**（载荷：sandboxId + runtimeSessionId + Cognito subject 哈希） | 30 分钟 | 沙盒内 adapter（KMS 验签 + subject 绑定核对）；Broker Lambda（存储操作前验签 + DynamoDB 会话核对） |

链条：用户 PKCE 登录拿到 JWT → 按 Start 时 Control Lambda 验完 JWT、把用户
身份与具体沙盒钉在一起签出 binding token → 之后每次协议调用带 JWT 过数据面、
信封里带 binding token 过 adapter → 备份时运行时拿 binding token 找 broker
换取存储授权。**租户隔离由传输层与密码学保证，而不是靠路由过滤**。

另有一个与 AWS 身份体系平行的第三身份：**Kiro CLI 的 Builder ID / SSO 登录**。
它是沙盒内 PTY 里跑的设备码流程，直接对 Kiro 服务，凭证落在沙盒文件系统里
（见 §4.4），随备份一起持久化。

## 3. 存储分层

一个沙盒的状态分布在五层（图 1），持久性依次增强：

| 层 | 位置 | 内容 | 生命周期 |
|---|---|---|---|
| 进程内存 | microVM | gateway/adapter 运行态 | 会话内 |
| 本地暂存 | microVM `/tmp/kirocrew-state` | gateway 的 SQLite WAL 工作集 | 会话内；checkpoint 时回写 workspace |
| **Session storage** | `/mnt/workspace` 挂载 | 用户全部文件（HOME、项目、登录库） | 托管同步盘（见下） |
| **S3 checkpoint** | 我们账户的 S3 桶 | 加密 chunk + manifest，保留最近两代 | **持久权威** |
| DynamoDB / KMS | 托管服务 | 状态机、代数指针 / 数据密钥 | 持久 |

### 3.1 Session storage：托管同步盘

`/mnt/workspace` 通过 AgentCore 的 `filesystemConfigurations.sessionStorage`
声明。它不是普通临时盘：写入会被平台异步复制到 AgentCore 服务侧的 S3，
会话 Stop/Resume、闲置回收后**自动原样恢复**，应用零操作。它的边界：

- **发布新 runtime 版本即清空**（每次发新镜像，所有会话的盘归零）；
- **14 天未调用即清空**；
- 每会话 **1 GB 上限**，不可调；
- 不支持跨会话文件锁（这正是 gateway SQLite 工作集要暂存到 `/tmp` 的原因）；
- Preview 功能，无持久化承诺。

### 3.2 两条 S3 链路，不要混淆

| | 托管复制（session storage 底层） | 我们的 checkpoint |
|---|---|---|
| 桶 | AgentCore 服务侧（`acr-storage-*`），不在我们账户 | 我们账户的 checkpoint 桶 |
| 谁执行 | 平台自动，应用无感 | 沙盒内 runtime 主动执行 |
| 加密 | 平台管理 | 每沙盒独立数据密钥，VM 内加密后才上传 |
| 存活 | 发版/14 天清空 | 直到用户删除（保留两代 + GC + 离线审计） |

**没有"从 session storage 复制到 S3"这个动作**：两条链路的源都是
`/mnt/workspace` 这同一份本地文件。托管复制换来免操作的秒级热恢复；
自有 checkpoint 才是真正属于部署者的持久副本，兜住发版清空、过期、
容量超限与平台规格变动。

## 4. 备份与恢复

### 4.1 备份在哪里执行（图 3）

**在每个用户自己的 microVM 里**，没有共享备份服务器。checkpoint 引擎读本地
`/mnt/workspace`、在 VM 内加密、经预签名 URL **直连上传 S3**。Broker Lambda
是门卫不是搬运工：数据不流经它，它只做三件事——验 binding token、派生 S3
key 并签发 ≤5 分钟的一次性 URL、在 DynamoDB 上记代数指针。因此沙盒内可以
**不持有任何 S3 凭证**。

### 4.2 触发点与丢失边界

| 触发 | 时机 | 保证 |
|---|---|---|
| Stop safely | 用户点按钮 | 零丢失（final checkpoint） |
| 登录 / 登出 | `kiro.authenticated` / logout 完成后台提交 | 凭证变更即时持久 |
| 周期 | `KIROCREW_CHECKPOINT_INTERVAL_SECONDS`（默认 300s，0 关闭），持久根元数据指纹变了才提交 | 任意回收路径丢失 ≤ 一个周期 |
| SIGTERM | 优雅关闭时限时尽力提交 | 优雅回收路径零丢失 |

提交流程细节见图 5：静默进程组 → 回写暂存态 → 确定性 manifest →
加密上传新 chunk（内容寻址去重）→ 上传 manifest → 条件提交代数 →
更新本地指针。任何一步失败，上一代仍是权威。后台提交可能 S3 成功而
DynamoDB 回执未落（进程恰好被回收）——恢复逻辑容忍 S3 超前于指针，
只有 S3 落后于指针才判定数据丢失。

### 4.3 启动时的恢复决策（图 4）

冷启动先看本地挂载：session storage 命中（指针与 S3 最新提交一致、版本
未变）则零拷贝直接用，秒级；否则从 S3 拉最新代——在隔离暂存树里逐 chunk
校验摘要后**原子换入**，最新代损坏自动回退一代，两代皆不可用才报
`PERSISTENCE_RESTORE_FAILED`。

### 4.4 都备份了什么

持久根：`home/.kiro`（含 gateway 全部状态：sessions、向量记忆库、apps、
skills、嵌入模型）、`home/.config`、`home/.local/share/kiro-cli`（**Kiro
登录凭证**，`data.sqlite3` 的 `auth_kv` 表，图 6）、`artifacts`、
`knowledge`、`memory`、`projects`、`user`。排除：`.aws`、`.ssh`、各类缓存、
日志、`.env*`、套接字等。完整契约见 `persistence.md`。

## 5. 多租户隔离

一个用户在自己的沙盒终端里可以跑任意代码，因此隔离不能依赖"约定"。
系统有四层防线：

1. **算力与磁盘**：每用户一台 Firecracker microVM；session storage
   官方语义 per-session isolated，文件系统层面互不可见。
2. **零凭证 + 门卫**：沙盒内没有 S3 凭证；一切存储操作凭 KMS 签名的
   binding token 找 broker 换一次性 URL，key 由 broker 派生、锁死本沙盒
   前缀。执行角色虽为整个 runtime 共享，但数据授权在 token 不在角色——
   即使用户代码直接调用 broker Lambda，手里也只有自己沙盒的 token。
3. **密码学**：每沙盒独立 256-bit 数据密钥，密文在 VM 内生成，KMS 解封
   绑定 sandboxId 加密上下文。即使桶权限配置失误，他人密文也不可读；
   去重只在沙盒内发生，相同明文在不同沙盒产生不相关密文，不泄露
   跨租户信息。
4. **特权面最小化**：能触达明文的只有沙盒本人和离线审计器 Lambda
   （验证保留代完整性）；拥有 KMS 权限的账户管理员属于部署者信任边界。

## 6. 设计取舍

- **为什么不用 EFS/EBS 简化持久化**：EBS 卷仅 Instances 计算类型可用，
  microVM 用不了；EFS/S3 Files 挂载是 runtime 级共享——所有用户会话看同
  一棵树，POSIX UID 是 access point 级别，对"沙盒内跑任意用户代码"的
  多租户模型不构成安全边界；要隔离就得每用户一个 runtime，发版和配额
  成本随用户数线性增长。单用户或小型互信部署可以走该路线并大幅删减
  本项目的持久化代码。
- **为什么 Lambda 只做门卫**：把"谁能写哪个 key、用哪把密钥"的决定权
  集中在一个可审计的小组件里，换来沙盒内零存储凭证；数据直连 S3 则让
  Lambda 时长与吞吐都不受备份体积影响。
- **与 aws-samples 的 OpenClaw-on-AgentCore 对比**：该样例用"启动恢复 +
  每 5 分钟保存 + SIGTERM 收尾"的裸文件 S3 同步，隔离靠沙盒内的
  STS 前缀限定凭证。本项目的触发点设计与其一致，但同步引擎更重也更强
  （原子代提交、逐块校验、密码学隔离），且沙盒内不落任何存储凭证。

## 7. 相关文档

- `persistence.md` —— 持久化契约（英文，规范性）
- `persistence-design.zh-CN.md` —— 登录态丢失问题的根因分析与改进方案演进
- `architecture.png` —— 上游项目原有的英文总架构图
