# KiroCrew on AgentCore 文件持久化设计与 Kiro CLI 登录态丢失分析

本文回答两个问题：

1. **为什么沙盒重启后 Kiro CLI 需要重新登录**（根因分析）。
2. **沙盒环境如何与 AgentCore managed session storage、S3 等各层状态同步、更新与恢复**（现状梳理 + 改进方案设计）。

配图（SVG 源文件与 PNG 均在 `docs/` 下）：

| 图 | 文件 | 内容 |
|---|---|---|
| 图 1 | `persistence-state-sync.png` | 状态分层与同步总体架构 |
| 图 2 | `persistence-restore-flow.png` | 启动 / 恢复决策流程 |
| 图 3 | `persistence-checkpoint-flow.png` | checkpoint 提交流程 |
| 图 4 | `kiro-login-persistence.png` | Kiro 登录态持久化时序（含丢失路径） |

---

## 1. 登录态丢失的根因

Kiro CLI 把 Builder ID / SSO 登录凭证存在
`~/.local/share/kiro-cli/data.sqlite3` 的 `auth_kv` 表里
（两行：`kirocli:odic:device-registration` 与 `kirocli:odic:token`，
SQLite `journal_mode=delete`）。沙盒内 `HOME=/mnt/workspace/home`，
所以该文件位于 `/mnt/workspace/home/.local/share/kiro-cli/data.sqlite3`。

登录态丢失有三层原因，按影响排序：

### 1.1 历史缺陷：登录库目录不在持久化根内（已修复，需重新部署）

`PersistencePolicy._roots` 最初只包含 `home/.kiro`、`home/.config`、
`projects`、`user`。`home/.local/share/kiro-cli` **不在其中**，
checkpoint 从不捕获登录库，每次从 S3 restore 必然回到未登录状态。

commit `5c7c067`（"Survive a swallowed readiness line and persist the Kiro
CLI sign-in"）已把该目录加入持久化根。**但如果线上运行的镜像构建于该
commit 之前，症状会原样存在。** 处置：用当前代码重新
`make image-publish` 并 `make infra-deploy` 切换到新镜像。

### 1.2 结构性缺口：checkpoint 只有一个触发点

生产代码中 `CheckpointEngine.checkpoint()` 只在
`sandbox.prepare_stop`（悬浮面板的 **Stop safely** 按钮）被调用
（`runtime/src/kirocrew_agentcore_runtime/aws_runtime.py`，
`ProductionRuntimeBackend.execute`）。以下场景**都不会产生 checkpoint**：

- 沙盒闲置超时（默认 15 分钟）被 AgentCore 缩容回收；
- 用户直接关掉浏览器；
- 运行时崩溃或收到 SIGTERM；
- AgentCore 会话到达最大生命周期被回收。

只要用户登录之后没有按过 Stop safely，登录写入就只存在于
managed session storage（加速层，非持久承诺）。一旦下次启动触发
S3 restore（见 1.3），`RestoreEngine._install` 会**整树替换** workspace
（manifest 之外的一切都被删除），登录态回滚到登录之前的那一代。

### 1.3 restore 的触发条件放大了 1.2 的窗口

启动时只要满足任一条件就从 S3 restore（`restore.py::_restore_reasons`）：

- 挂载为空 / `.agentcore/persistence.json` 缺失（managed storage 过期）；
- 本地代数指针 ≠ 最新提交代数；
- `runtimeVersion` 变化（**每次发布新镜像都会触发**）；
- 本地完整性抽样失败；
- managed session 过期标记。

即发布一次新镜像，所有用户下次启动都会整树回滚到各自最后一次
"Stop safely" 的状态——之后发生的登录、令牌刷新全部丢失。

### 1.4 文档承诺的 30 秒 RPO 实际未接线

`docs/persistence.md` 承诺"首个未提交持久变更后 30 秒为恢复点目标，
超限则沙盒转只读"。支撑机制（`DirtyJournal`、`InotifyDirtyWatcher`、
`CheckpointEngine.enforce_rpo/assert_writable`）代码齐全，但**生产路径
没有任何调用方**——`InotifyDirtyWatcher` 与 `assert_writable` 只出现在
单元测试里。运行期没有人往 journal 写事件，也没有人检查 RPO，
所以既没有周期性 checkpoint，也没有只读降级，实际 RPO = "距上次手动
Stop safely 的全部时长"。

---

## 2. 现状：状态分层与同步机制（图 1）

### 2.1 状态存放点

| 层 | 位置 | 内容 | 生命周期 / 持久性 |
|---|---|---|---|
| microVM 内存 | 沙盒进程 | gateway/adapter 运行态、dashboard token | 会话内，回收即失 |
| 本地暂存 | `/tmp/kirocrew-state/crew` | gateway SQLite 工作集（WAL 需要，仅当 workspace 不支持 WAL 时启用） | 会话内；checkpoint 前由 `StagedStateFlusher` 镜像回 workspace |
| workspace 挂载 | `/mnt/workspace` | `home/.kiro`、`home/.config`、`home/.local/share/kiro-cli`、`projects`、`user` + `.agentcore/`（运行时元数据，不入 checkpoint） | AgentCore managed session storage：**加速层**，跨热启动保留，会过期，无持久承诺 |
| S3 checkpoint | `snapshots/<sandboxId>/…` | 内容寻址加密 chunk + 加密 manifest，保留最近两代 | **持久权威**；KMS 每沙盒数据密钥，AES-256-GCM |
| DynamoDB | sandbox 表 | 沙盒状态机、租约、`lastCheckpointGeneration` 指针、init 属主 | 持久（控制面元数据） |
| KMS | 每部署一把 key | 每沙盒 256-bit 数据密钥（不入 manifest） | 持久 |

### 2.2 持久化根与排除规则（`PersistencePolicy`）

包含：`home/.kiro`、`home/.config`、`home/.local/share/kiro-cli`、
`projects`、`user`。
排除：`.agentcore`、`.aws`、`.ssh`、各类缓存（`.cache`、`node_modules`、
`__pycache__`、`.git/objects` 等）、`.env*`、`*.log/.pid/.sock/.tmp/~`、
FIFO / 设备文件；symlink 只记录目标不跟随。

### 2.3 同步机制一览

- **WAL 暂存镜像**：workspace 挂载不支持 SQLite WAL 时，supervisor 把
  `home/.kiro/crew` 镜像到本地盘作为活动数据目录；checkpoint 静默期内
  由 `StagedStateFlusher.flush()` 先把暂存态同步回 workspace 再落盘。
- **checkpoint（图 3）**：暂停 KiroCrew 进程组 → 回写暂存态 → `os.sync`
  → 按 policy 构建确定性 manifest → 加密上传新 chunk（内容寻址去重）
  → 加密上传 manifest → 条件提交代数 → 更新本地指针
  `.agentcore/persistence.json` → 清 journal。任何一步失败则上一代仍为权威。
- **restore（图 2）**：读 DynamoDB 元数据 → 抢 init 租约（心跳续期）→
  无提交代数则（新沙盒）空初始化 /（老沙盒）失败 → 有代数则按 1.3 的
  条件判定；需要 restore 时在 `.agentcore/restore-stage` 下重建隔离暂存树、
  校验每个 chunk 与整文件摘要、原子换入（失败自动回滚）；最新代损坏则
  向前回退一代；两代都不行报 `PERSISTENCE_RESTORE_FAILED`。
- **凭证边界**：persistence broker 自行推导 S3 key，预签名 URL ≤15 分钟，
  强制 KMS encryption-context 含已验证的 sandbox ID；运行时拿不到桶前缀、
  不能 list。绑定令牌 30 分钟。
- **GC 与审计**：保留最近两代；引用感知 GC 先标记、过宽限期、复算可达性
  再清扫；离线审计器定期解密两份 manifest 验证所有 chunk 存在。

---

## 3. 改进方案设计

目标：**用户可感知的持久状态（尤其是登录态）在任何回收路径下都不丢**，
同时保持"加密 checkpoint 是唯一持久权威"的安全模型不变。

### P0（运维，立即）：重新发布运行时镜像

确认线上镜像 ≥ `5c7c067`。否则登录库根本不入 checkpoint，
后续所有改进无从谈起。验证方法：停止前登录 → Stop safely → 重启 →
`kiro-cli whoami` 应保持登录。

### P1（小改动，收益最大）：登录成功即触发一次 checkpoint

在 `ProductionRuntimeBackend.execute` 的 `kiro.login.start` 流程里，
当产出 `kiro.authenticated` 事件后异步提交一次非 final checkpoint
（复用 `_checkpoint_lock`，不暂停用户操作感知）。由于 chunk 内容寻址
去重，这次增量只上传 `data.sqlite3` 等少量新 chunk，秒级完成。
同理可在 `kiro.logout` 后触发，保证登出也持久。

### P2（补齐承诺）：接线 dirty journal，周期性增量 checkpoint

把已有但闲置的机制接入生产：

1. runtime 启动后创建 `InotifyDirtyWatcher(workspace, journal)`，
   后台任务循环 `poll_once`；
2. 后台调度器检查 `journal.snapshot().first_dirty_at`，
   距今 ≥ RPO（30s，可配）且不在其他 checkpoint 中时提交下一代；
3. 提交失败时按现有 `enforce_rpo` 语义转只读，
   与 `docs/persistence.md` 的承诺一致。

配套：保留两代不变（GC/审计不动）；对高频写入目录
（`projects` 下构建产物）可加去抖（如脏后静默 5s 再提交）避免抖动。

### P3（兜底）：SIGTERM / 会话回收时尽力 checkpoint

入口是 `tini` + aiohttp：注册 `on_shutdown`（SIGTERM 触发）执行一次
带短超时（如 10s）的 final checkpoint。AgentCore 回收 microVM 前若给
grace period，就能把"闲置回收"路径也覆盖住。拿不到 grace 的硬回收
由 P2 的周期 checkpoint 保证损失 ≤ RPO。

### P4（可选优化）：凭证类"热根"更紧的 RPO

`home/.local/share/kiro-cli` 与 `home/.kiro` 体积小、价值高
（登录态、令牌刷新、会话记录）。可在 P2 调度器里给这些根单独更短的
去抖窗口（脏即提交），项目文件用标准 30s。实现上只是调度策略差异，
manifest/chunk 机制完全复用。

### 方案对比

| 方案 | 改动量 | 覆盖场景 | 残余风险 |
|---|---|---|---|
| P0 重新部署 | 无代码 | 修复"从不捕获" | 1.2 的窗口仍在 |
| P1 登录后即存 | ~30 行 | 登录/登出后任何回收 | 令牌刷新、其他文件仍有窗口 |
| P2 周期增量 | 中 | 一切写入，RPO=30s | 最后 30s 内的写入 |
| P3 SIGTERM 兜底 | 小 | 优雅回收路径 | 硬杀无效 |
| P4 热根策略 | 小（依赖 P2） | 凭证近零丢失 | — |

**推荐组合：P0 + P1 立即做；P2 + P3 作为持久化契约的正式补齐；
P4 视运行成本再定。**

---

## 4. 验证清单

- 单元：`PersistencePolicy` 包含 `home/.local/share/kiro-cli`（已有）；
  新增 P1/P2 触发路径的检查点提交测试（复用 `InMemoryCheckpointStore`）。
- 端到端（部署栈）：登录 → 不按 Stop safely → 等待闲置回收 + managed
  storage 过期（或强制新镜像触发 restore）→ 重启 → `kiro-cli whoami`
  仍为登录态。
- 审计：离线 auditor 对新代数验证通过，GC 不误清仍被引用的 chunk。
