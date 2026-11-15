# Research: Agent 嵌套事务与管理审计

- Query: 阶段 E5a：梳理 Agent `TransactionSession`、SAVEPOINT、权限失效和管理员审计的事务边界，提出安全的接入与反例。
- Scope: mixed
- Date: 2026-11-09

## Findings

### 相关文件与已有契约

- `backend/app/api/v1/agent.py:24`：Agent HTTP 的 `mutation_db` 使用独立会话，捕获 `DBAPIError` 后交给 `rollback_conflict`。
- `backend/app/services/agent/transactions.py:10`：PostgreSQL Agent 会话是 `REPEATABLE READ`；`rollback_conflict` 识别 40001 并整体回滚。
- `backend/app/services/agent/tools/base.py:43`：`TransactionSession.commit()` 仅 flush、`rollback()` 为空；`Tool.preview` / `execute` 使用内部保存点。
- `backend/app/services/agent/service.py:322`：确认或自动执行提案时外层再建立保存点；效果快照、提案状态和业务修改由最终调用方提交。
- `backend/app/services/agent/runner.py:139`：后台的工具预览、消息、提案和自动执行通过同一 Agent 会话的多个明确 commit 分步推进。
- `backend/app/services/audit_writer.py:93`：`AuditOperation` 初始化、成功暂存和失败处理；只对已有明确采集动作的管理账号启用，作者操作默认不采集。
- `backend/app/core/deps.py:77`、`backend/app/services/authorization_service.py:191`：HTTP 的原始认证身份与 token_version 存于认证会话的 `db.info`，写屏障重验该快照。
- `backend/app/services/setting_ai_visibility.py:78`、`backend/app/services/project_access.py:64`：认证/User → 发送写门禁 → 项目父行的现有作者或协作写屏障。
- `backend/app/services/collaboration_service.py:63`、`backend/app/services/collaboration_audit.py:102`：协作审计已有回滚、连接失效、operation_id 查事实及重复取消保护，可借鉴协调思路；不能把协作审计表当管理员审计表。
- `backend/tests/test_audit_authorization_no_autoflush.py:134`：已有初始化 40001 安全转换和普通 viewer 嵌套回滚测试；目前不覆盖管理员协作者下审计可持久化的完整路径。
- `.trellis/spec/backend/agent.md:119`、`.trellis/spec/backend/admin-audit.md:59`、`.trellis/spec/backend/rbac-quotas.md:73`：分别约定代理事务、管理员审计及授权锁；`design.md:113`、`implement.md` 的 E5a 给出本任务新要求。

### 实际调用与缺口

1. HTTP 认证的 `get_current_user` 与 `mutation_db` 使用不同 AsyncSession。前者把 `AuthenticatedActor(user.id, decoded_version)` 放进**认证会话**（`backend/app/core/deps.py:77`）；后者只创建新的 RR 会话（`backend/app/api/v1/agent.py:24`），业务多处只传 `user.id`（例如 `agent.py:85`、`service.py:563`）。`actor_snapshot` 在新会话没有该 primitive 时返回整数，`require_authenticated` 只验证 active、不比较原请求令牌版本（`authorization_service.py:191`）。因此认证完成后、Agent 拿写屏障前发生密码重置/令牌撤销时，光重验数字 actor 不足；这是从代码得出的竞争风险，未在本研究中运行复现。后台任务本身没有 HTTP 会话，另按任务真实 actor、账号状态及任务的协作轮次校验，不能把最新 token 版本伪装为原请求凭据。
2. 写工具 `Tool.preview` 先锁项目，然后建立 SAVEPOINT，默认通过 `TransactionSession` 调业务并故意回滚（`base.py:163-211`）。`Tool.execute` 也建立保存点并在业务错误时回滚（`base.py:213-226`）。而 `execute_change` 在调用 `tool.execute` 时又建立外层 SAVEPOINT；工具把 `AppError` 转成 error dict 后，外层将其转为异常并回滚（`service.py:378-418`）。所以至少两个保存点可以依次撤销内层写入；不能只看最内层是否释放。
3. `AuditOperation.commit` 在业务操作和成功审计 flush 后调用代理的 `commit`，再将 `committed=True`（`audit_writer.py:232-247`）。在代理中这不是真实 commit。预览会回滚其暂存成功记录；执行时只有最外层真实事务成功才使业务与成功审计同时生效。若在该 flush 之后、最外层 commit 之前取消或抛错，不能把局部标志当作持久成功。
4. 管理员业务失败时，`AuditOperation.__aexit__` 先调用代理的空 `rollback`，再查询并插入失败审计，代理的 `commit` 再次只 flush（`audit_writer.py:256-299`）。失败记录仍属于内层保存点，将在工具异常退出或预览 finally 中消失；权限拒绝后查询还可能触发 autoflush，违反不得先写未经授权待写实体。失败审计在被回滚的内层数据库会话里看起来可查，不能作为持久证据。若 DB 状态已失败，该查询也可能直接出错；原生失败日志不能替代审计表事件。
5. `Tool.run/preview/execute` 会把一般 `AppError` 转为工具结果，仅 `VisibilityPolicyError` 会向上抛（`base.py:100-113,146-155,210-226`）。将撤权、降级或轮次失效实现为普通 403 并直接复用这条路径，可能仅让工具显示失败，后续轮次仍继续。`visibility_locked=True` 还会绕过 `_lock_write` 内复核（`base.py:157-161`）；传该标志前必须由提案服务持同一事务锁并重验当前 actor、`ai.use` 和原 invitation_id/revision。
6. `AuditOperation.__aenter__` 对数据库 40001 特别保留安全 SQLSTATE（`audit_writer.py:126-140`），但 `__aexit__` 的 `_propagate` 会将普通的非 `AppError` 转安全 500（`audit_writer.py:249-254`）。业务体或 flush 中发生的真实 40001 是否仍按 Agent 409 转换，目前没有同等证据，需补测并在确保原 SQL 和参数不出现在异常、输出、审计及日志的前提下处理。`transactions.rollback_conflict` 会以原异常作为 cause（`transactions.py:22-25`）；只有原异常已消毒时才可安全暴露/记录该链。
7. `begin_nested()` 会在创建保存点前 flush 当前会话待写对象；即使查询本身已 `no_autoflush`，也要保证进入保存点前已完成对应权限与父行屏障，不能把未经授权的业务对象提前放进外层。`project_lifecycle.lock_record` 的无值更新确保 SQLite 在 SAVEPOINT 外已有物理事务（`project_lifecycle.py:50-68`），不可删除或用 SQLite 的表面行为代替真实 PostgreSQL 行锁验证。
8. `decide_change` 会先 rollback 释放旧读事务，然后持会话锁重新获取数据（`service.py:549-585`）；`decide_turn` 逐项调用并可能逐项 commit（`service.py:588-619`），不能把整轮错误误说成回滚所有已成功项。后台 `runner._process_calls` 也在提案、每次自动执行及工具消息各自 commit（`runner.py:222-267`）。`_undo_changes` 对整轮撤销使用单外层事务并处理 `BaseException`（`service.py:622-656`），其语义不同。
9. MCP 外部提案先提交 `executing` 再发网，结果未知不自动重放（`service.py:437-507`）；远端结果可能已经发生，协作失效只能阻止后续受控写入并做受限收尾。将失败审计协调器添加到该路径时不得承诺回滚外部副作用或重调 MCP。

### 角色与所需事务事实

| 身份 | AI / Agent 判定 | 现有已纳入的管理员动作审计 |
|---|---|---|
| 作者 | 当前作者；旧任务无协作轮次仍兼容；每次受控发送和落库按原策略及账号状态检查。 | 本人作品 `AuditOperation` 默认关闭，不能因为代理而新采集普通本人创作。 |
| 普通 accepted editor | 由本人触发且持本轮 invitation_id/revision；锁后要仍是 editor。本人或授权作者模型使用规则独立检查。 | 非管理账号原本不采集管理审计；不能为满足测试无条件打开或伪造作者身份。 |
| 管理员兼 accepted editor | AI 必须来自有效 editor 身份，管理人工授权不能代替 `ai.use`；写入若使用管理员路径，原目标保护仍适用。 | 对属于现有采集清单的跨用户业务动作按真实 actor 记录；成功与实际业务同根事务，真正进入审计作用域的业务拒绝应保留安全失败事实。 |
| viewer、纯管理员、已撤销或旧轮次 | 均不能因人工能力触发/续跑 Agent；普通 viewer 可在被允许的私人历史范围读本人已完成内容。 | 401 不记管理员动作；尚未进入某个被采集业务动作时不能凭猜测伪造该动作审计。若已进入审计作用域后拒绝，按安全失败路径处理。 |

### 建议的最小改造顺序

1. **单独传原认证快照**：使 Agent mutation dependency 依赖同一个认证 `DbDep` / `CurrentUser`，复制其中经过校验且 actor ID 相同的 `AuthenticatedActor` 进入独立 Agent 会话 `info`；会话长期保存该 primitive，不能从当前数据库重新生成旧令牌的版本。确认 FastAPI 依赖复用与顺序，不改变后台任务的无会话授权模型。
2. **在已有 Agent 事务边界建立显式协调对象**：给 `TransactionSession` 明确标识此 scope 是 preview 或 execute，并共享一个外层持有的记录器。不要靠全局 ORM 事件、monkey-patch `AsyncSession.commit`、隐式线程/协程全局变量判定身份。记录器只持不可变、安全、白名单化的 operation_id/actor/目标快照、动作与错误类别，不保存 ORM 实例、正文、模型请求或原异常。
3. **区分 staged 与 confirmed**：若在代理中 `AuditOperation.commit`，成功审计仍在业务事务中 flush，但状态仅为 staged；外层真实 commit 确认后才视为成功。预览故意回滚必须丢弃其暂存成功，不能插入成功审计，也不能把预览当作业务已提交。后续投影/flush 失败与取消应让协调器知道先前 staged 操作尚未真正提交。
4. **失败延后到所有相关保存点退出之后**：`AuditOperation` 检测显式代理上下文后，不在内层做无效 rollback、查询或插入，而仅冻结安全失败快照并传播原业务错误或安全包装。协调器要穿透 `Tool.execute` 捕获 error dict 与 `execute_change` 的第二层保存点；不能在第一层结束后就在第二层中插入失败记录。对已确认业务拒绝，外层如果仍需提交合法状态，可以在所有保存点回滚后将失败记录加入可提交的根事务；如果根事务将回滚，则先结束/确认释放根事务与 SQLite/PG 锁，再以独立会话核实 operation_id 并安全记录。两条路径都需说明提交失败、回滚和消息状态的真实语义；不得为了审计提交外层未经授权写入。
5. **未知提交/取消统一核验**：commit await 返回异常或取消时，先在可防重复取消的受保护清理中回滚原会话；如不能确认连接上的事务已结束，先失效连接；仍无法确认时仅以独立会话读 operation_id，不补造失败。能确认事务结束后，先查是否已有成功，再按唯一 operation_id 插入安全失败；若独立失败写入本身确认丢失，再另用新会话查，不盲重放相反结果。可参照 `collaboration_service.py:63-106`，但应使用管理员审计模型、既有错误白名单和原业务异常协议；不要把该路径的 best-effort 能力描述为进程崩溃/全库故障下的必达保证。
6. **授权失效要成为不可吞掉的策略错误**：在当前已有的认证 → 发送写门禁 → 项目父行后重验 `ai.use`、真实 actor、原邀请 UUID/revision，区分普通工具参数错误与停止整个 Agent 的权限失效；对每次读/写工具、可见性 scope、确认及自动执行复查。锁后失败不能降级为继续模型轮次；授予管理员人工权限也不能补足被撤销的协作者 AI 身份。无权用户既不读私有会话，也不能透过错误得到 owner 私有配置。
7. **局部集成及归属**：`agent/tools/base.py`、`agent/transactions.py`、`audit_writer.py` 可以先以明确协议和专用单元测试形成独立切片；`agent/service.py` / `agent/runner.py` / `agent.py` 最终接线要和当前负责 Agent 隐私投影的代理串行协调，避免共享文件并行覆盖。Agent `start_task` 的服务端任务绑定、预览中作者模型 ID 的脱敏和真实 actor 派生任务是 E1–E3 的独立依赖，不能把事务修复冒充完整 Agent 授权。

### 必需的验证矩阵

- SQLite 和真实 PostgreSQL：作者、普通 editor、管理员+editor，分别覆盖成功 preview（零成功审计、零业务副作用）、实际成功（业务、AgentChange 与唯一成功审计同事务）、内层 400/403/409/500、锁后撤权及角色降级、旧邀请重新加入；无关外层合法 pending 不被内层回滚损坏。
- 注入两层 SAVEPOINT 间异常、内层成功后外层 flush/commit/投影异常、`CancelledError` 多次取消，核对根事务结束后每个已尝试的管理动作只有一个正确结果；不把模拟预览成功或被回滚的业务记成功。
- PostgreSQL 双连接：发送/任务锁先于撤权、撤权先提交后授权重验、令牌版本变更先于 Agent User 锁、`REPEATABLE READ` 快照冲突产生实际 SQLSTATE 40001；必要时根据数据库隔离事实整体 rollback 并返回安全 409，不能沿用旧快照重试写或重新绑定模型。SQLite 另验 busy 超时、PRAGMA 恢复、连接回池与物理外层事务。
- 插入审计失败、原业务 flush 失败、commit 前失败、commit 已成功但确认丢失、rollback/invalidate 不确定、独立失败写入成功但确认丢失；核对 operation_id 唯一与是否允许补记，不出现同操作相反结果或因审计故障提交业务。单独断言重试过程中不重复调用 MCP、不可撤销的既有结果不被误报回滚。
- 强制实际业务体抛带正文/密钥的 DBAPIError 和 40001，以及 `__aenter__` 与 `__aexit__` 两位置；HTTP、工具消息、SSE、审计表与 `caplog` 均不含 SQL、参数、密钥或原异常。验证普通业务错误保留约定状态；授权失效停止后续模型出站及工具执行。
- 严格复跑 `tests/test_agent_tools.py`、`tests/test_agent_api.py`、`tests/test_agent_runtime.py`、`tests/test_agent_effects.py`、`tests/test_agent_visibility.py`、`tests/test_agent_mcp_skills.py`、`tests/test_admin_audit_logs.py`、`tests/test_audit_authorization_no_autoflush.py` 和具体协作 PG 专用用例；相应外部服务用现有 FakeLLM，不发送真实业务数据。

### External references

- [SQLAlchemy 2.1 官方事务文档：Using SAVEPOINT](https://docs.sqlalchemy.org/en/21/orm/session_transaction.html#using-savepoint)：`begin_nested` 在保存点开始前无条件 flush 当前待写状态，即使关闭 autoflush；回滚保存点保留未在该保存点内变化的外层状态。本地 `uv run python -c` 查得 SQLAlchemy 2.1.6，`pyproject.toml` 声明 `sqlalchemy[asyncio]>=2.0.30`。
- [PostgreSQL 16 官方事务隔离文档](https://www.postgresql.org/docs/16/transaction-iso.html#XACT-REPEATABLE-READ)：Repeatable Read 的快照在事务首条非事务控制语句时建立；更新/锁定竞争事务已提交修改的旧快照行会发生序列化错误，须结束事务后重新评估。实际仓库竞争路径仍须由隔离 PostgreSQL 集成测试验证；不能把文档当作本项目已运行的并发证据。

## Caveats / Not Found

- 本研究只静态读取共享工作树，其他代理仍在并行编辑；上述文件与行号可能随集成移动。没有修改业务代码，也没有运行事务复现或测试。
- 现有 Agent 路径仍以严格作者 helper 为主；对 accepted editor 的表现是接入时的风险，不声称其当前已开放或已发生线上事故。
- 在设计中，“模拟预览失败是否作为管理动作审计”与现有“普通 AI 不自动采集”的边界应由实现按照已有显式审计范围界定：不能为了满足记录而扩大审计范围，但实际进入已采集业务动作后不得依赖将被回滚的 SAVEPOINT 保存失败事实。
- PostgreSQL `REPEATABLE READ` 中实际 40001 位置、网络分区时原连接终止是否可确认、取消时驱动行为都必须用真实测试核验；无法确认的结果应保留未知而不是创造失败事实。
