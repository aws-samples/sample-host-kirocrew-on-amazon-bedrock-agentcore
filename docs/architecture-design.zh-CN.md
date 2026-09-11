# KiroCrew on AgentCore 架构设计

> **状态更新（2026-09-07）**：本文最初按"managed session storage 作为加速层"
> 的设计撰写。该层已于 2026-09-06 彻底移除（见 §3.1），`/mnt/workspace`
> 现为临时容器盘，S3 checkpoint 是唯一持久层。正文与配图（图 1 状态分层、
> 图 4 恢复流程、图 6 登录态持久化）已于同日按现状重绘；图 2/3/5 描述的
> 令牌链、备份数据通路与 checkpoint 提交步骤不受该变更影响。

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
| binding token | Control Lambda，用 **KMS 非对称私钥**（RSASSA-PSS-SHA256）签名 | **这个沙盒、这个会话属于这个人**（载荷：sandboxId + runtimeSessionId + Cognito subject 哈希） | 30 分钟 | 沙盒内 adapter（KMS 验签 + subject 绑定核对）；Broker Lambda（每次操作前验签 + DynamoDB 会话核对） |
| runtime-session token | Broker Lambda，在容器赢得 `acquireInit` 时用同一把密钥签发 | **这个容器正合法地服务这个沙盒的这个会话**（载荷同上，type=runtime-session） | ≤ 平台会话生命周期（`runtime_max_lifetime_seconds`） | 仅 Broker Lambda 接受；adapter 不接受。运行时用它做租约心跳、生命周期更新与后台 checkpoint，浏览器离开 30 分钟后仍可继续 |

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
| **Workspace（容器盘）** | `/mnt/workspace` | 用户全部文件（HOME、项目、登录库） | **临时**：会话结束即消失，只靠下一行的 checkpoint 持久（见下） |
| **S3 checkpoint** | 我们账户的 S3 桶 | 加密 chunk + manifest，保留最近两代 | **持久权威** |
| DynamoDB / KMS | 托管服务 | 状态机、代数指针 / 数据密钥 | 持久 |

### 3.1 Workspace：临时容器盘（不再使用 managed session storage）

`/mnt/workspace` 现在就是 microVM 容器盘上的一个普通目录，**没有**声明
AgentCore 的 `filesystemConfigurations.sessionStorage`。这是 2026-09-06 的
明确决定（commit `16da25b`），并由 `tests/unit/test_terraform_security.py`
的守护测试锁死（runtime 模板中不得出现 `FilesystemConfigurations` /
`SessionStorage`）。原因是托管 session storage 在实际运行中反复引发事故：

- 每会话 **1 GB 硬上限**且不可调，嵌入模型一度占掉三分之二，导致 kiro-cli
  登录因 ENOSPC 失败；
- **14 天未调用即清空**、发布新 runtime 版本即清空，恢复路径反而多出一条
  "挂载存在但内容过期/不完整"的分支，restore 校验因此出过回归；
- Preview 功能，无持久化承诺，却在设计上诱使人把它当持久层。

去掉它之后模型是单一的：**容器盘是唯一的工作副本，S3 checkpoint 是唯一的
持久层**。每次冷启动都从 S3 最新一代恢复（小工作区秒级），运行期间由
§4.2 的多触发点 checkpoint 把丢失窗口压到一个周期以内。runtime 启动时会
打印容器盘实际容量（日志行 `Workspace disk at ...`），排查空间问题看它。

### 3.2 只有一条 S3 链路

历史版本里这里区分"托管复制"与"自有 checkpoint"两条链路；现在只剩后者：
沙盒内 runtime 主动执行、每沙盒独立数据密钥、VM 内加密后直连上传我们账户
的 checkpoint 桶，保留两代 + GC。文中及配图凡出现 "managed session
storage / 托管同步盘 / 热恢复命中" 的位置，一律按"无此层"理解。

## 4. 备份与恢复

### 4.1 备份在哪里执行（图 3）

**在每个用户自己的 microVM 里**，没有共享备份服务器。checkpoint 引擎读本地
`/mnt/workspace`、在 VM 内加密、经预签名 URL **直连上传 S3**。Broker Lambda
是门卫不是搬运工：数据不流经它，它只做三件事——验 token、派生 S3
key 并签发 ≤5 分钟的一次性 URL、在 DynamoDB 上记代数指针与沙盒生命周期。
沙盒记录的每一次读写（`readRecord`、`lease`、`acquireInit`、`heartbeatInit`、
`heartbeatLease`、`healReady`、`markReady`、`markError`）都是 Broker 端定义的
窄操作，调用方只能传经校验的值，不能传表达式；microVM 的执行角色因此不持有
任何 DynamoDB、S3 或数据密钥 KMS 权限——沙盒内所有进程（含用户终端）共享这个
角色，它拿到手里不能越出本沙盒。因此沙盒内可以
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

冷启动时容器盘总是空的，因此**每次都从 S3 拉最新代**——在隔离暂存树里
逐 chunk 校验摘要后**原子换入**，最新代损坏自动回退一代，两代皆不可用
才报 `PERSISTENCE_RESTORE_FAILED`。同一会话内的热恢复（Stop/Resume）
沿用本地盘，不重复拉取。restore 会跳过被现行策略排除、但出现在旧
manifest 里的条目（例如曾被纳入的嵌入模型目录），不会因此整体失败。

### 4.4 都备份了什么

持久根：`home/.kiro`（含 gateway 全部状态：sessions、向量记忆库、apps、
skills、嵌入模型）、`home/.config`、`home/.local/share/kiro-cli`（**Kiro
登录凭证**，`data.sqlite3` 的 `auth_kv` 表，图 6）、`artifacts`、
`knowledge`、`memory`、`projects`、`user`。排除：`.aws`、`.ssh`、各类缓存、
日志、`.env*`、套接字等。完整契约见 `persistence.md`。

## 5. 多租户隔离

一个用户在自己的沙盒终端里可以跑任意代码，因此隔离不能依赖"约定"。
系统有四层防线：

1. **算力与磁盘**：每用户一台 Firecracker microVM，容器盘随 microVM
   隔离，文件系统层面互不可见。
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
