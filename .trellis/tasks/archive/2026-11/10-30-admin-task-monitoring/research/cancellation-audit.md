# Research: 管理员取消请求、真实收尾与同事务审计

- Query: 如何在不改造任务调度体系的前提下，可靠受理管理员跨用户取消，并保留审计、执行身份、原子保存和项目排空契约？
- Scope: internal；仅代码及规范研究，不修改产品代码，不启动任务。
- Date: 2026-10-31

## Findings

### 1. 文件与已验证模式

| 文件 | 作用与证据 |
| --- | --- |
| `backend/app/services/task_service.py:265-274` | `refresh_same_task` 必须同时绑定整数 ID 和 `statistics_key`，旧对象不能刷新为复用同 ID 的新任务。 |
| `backend/app/services/task_service.py:331-405` | 共用取消入口已抽取，但 queued / paused 先提交终态、再独立 `on_abort`；running 在独立执行体收尾。 |
| `backend/app/services/task_engine/engine.py:200-237,268-293` | 调度扫描和真正领取是两个边界；只在扫描过滤取消标记不足以阻止已选中的任务领取。 |
| `backend/app/services/task_engine/engine.py:295-349` | late done / paused 已落库时保留真实成功或暂停结果，不强改 cancelled。 |
| `backend/app/services/task_engine/engine.py:399-472` | `_finalize` 在独立会话执行 `on_abort`、任务终态及统计提交；当前 abort 失败仍可能写终态。 |
| `backend/app/services/task_engine/engine.py:485-496` | `cancel_running` 按 UUID 核对执行体，只 cancel 一次，等待最多 10 秒；返回不证明已收尾。 |
| `backend/app/services/task_engine/engine.py:498-521` | `cleanup_scope` / `project_work` 覆盖请求侧清理；新后台取消 drain 也必须登记。 |
| `backend/app/services/task_service.py:480-546` | 项目删除排空会重试 abort、等待执行链，使用统一 10 秒窗口；不能只数 OPEN 或只看 cancelled。 |
| `backend/app/services/generation_service.py:380-425` | `_save_chapter_result` 只 shield 有界数据库保存；提交前取消完整回滚，提交已开始则保存获胜。 |
| `backend/app/services/audit_writer.py:82-105,168-195,205-244` | 成功审计与业务同一次提交；未知异常安全转换；提交确认丢失不另写相反失败；本人作品默认不采集。 |
| `backend/app/schemas/audit.py:8-44,129-151` | 审计 action / target / summary 均有界；目前没有 task 类型与自由文本原因。 |
| `backend/app/models/audit.py:21` | `operation_id` 唯一，可将同一次受理与审计绑定，不需要新长期任务历史表。 |
| `backend/app/models/entities.py:138,588-641` | 活动资源部分唯一索引只覆盖 queued / running；Task 继承 `updated_at`，不能把它当纯进度或排队起点。 |
| `backend/app/services/task_engine/handlers/chapter.py:134-146` | abort 只按章节 ID 与 generating 状态还原，不核对哪个新任务拥有该状态。 |
| `backend/app/services/kb_index_service.py:390-402` | 文档 abort 同样可能修改后来进入 pending / indexing 的文档。 |
| `backend/app/services/batch_service.py:387-399,496,628-633` | 批量 abort 从 Task 断点恢复当前章；真实审核暂停前 current 已清空。 |

### 2. 不能成立的“最少改动”方案

1. **外套 `AuditOperation` 后调用现有 `_cancel_loaded_task`，末尾再 `audit.commit()`**：queued / paused 已在 `task_service.py:348,369` 提交；running 已由 `_finalize` 独立提交。审计失败不能撤回取消，不能宣称同事务。
2. **先信号取消，再补“受理”审计**：不可回滚副作用已先发生，审计失败时放行了未留痕的管理员动作。
3. **只持内存请求**：HTTP 断开或进程在审计后、调用 helper 前退出，queued / paused 没有可恢复意图，可能继续执行。
4. **先记 accepted 审计而不写可恢复标记**：只实现“先留痕、尽力尝试”。可以如实描述为尽力执行，但不能承诺可靠受理，不推荐用于本任务。
5. **看到取消已结束就清标记，或只扫描 OPEN**：现有 queued / paused 可能已经 cancelled，但 `on_abort` 尚未成功，重启会漏掉资源收尾。
6. **终态后无限重试旧 abort**：幂等只针对同一资源代次，并不等于对新任务安全。旧任务已释放活动索引后，新任务可能占用相同章节；旧 abort 会误还原新任务状态。

### 3. 推荐最小契约：持久化“受理”，而非冒称“已取消”

成功审计的业务对象改为明确的 **取消请求受理**：

- action 使用 `task.cancel_request`，target type 使用 `task`。
- 接受事务：再次核实当前管理员权限、捕获真实 actor / owner / 作品 / 任务身份、安全原因；条件写入 Task 取消意图；同事务写受理成功审计。
- 只有受理事务明确提交后才启动或唤醒 drain；响应 `202`，文案“取消请求已受理，任务正在收尾”，绝不固定提示“取消成功”。
- 真实 Task 状态仍为 queued / running / paused，另有 `cancellation_pending` 投影；最终状态由原引擎规则决定，可能是 cancelled，也可能是赢得竞争的 succeeded / paused，重启时可能是 interrupted。
- 同事务保证只涵盖 **持久化受理意图 + 受理成功审计**。信号投递、handler 清理与最终任务状态不是与接受请求跨事务原子提交；不能在文档中模糊这一界限。
- 不另建长期任务明细或复制 input / output / checkpoint / 上游错误。

#### 最低持久化数据

Task 新增可空字段，旧记录全部为空，无业务内容回填：

- `admin_cancel_operation_id: UUID string | null`：也绑定受理审计 `operation_id`；非空表示尚未完成该请求的收尾。
- `admin_cancel_requested_at: UTC datetime | null`：展示受理时间、排查请求状态，不复用 `updated_at`。
- 建议同时保存 `admin_cancel_requested_status: queued|running|paused|null`：记录接受事务真正命中的原状态，便于恢复及辨别原 paused 与 late paused；不能从用户请求体相信该值。

三个字段作为一组写入、作为一组清空；Task 身份始终是 `(id, statistics_key)`，操作更新再附加原 operation_id。SQLite 不为增加检查约束重建 tasks 父表；迁移新增可空列即可，组内一致性由服务校验及迁移测试约束。操作 ID 索引用于 pending 扫描可加，但不需要修改活动资源部分唯一索引，前提是下面的“持原状态直到安全收尾”不变量完整实现。

严格说，如果**每一个** finalize / recover 分支都能在原状态提交中原子清空取消标记，`requested_status` 不是逻辑必需：不会留下“late paused 已提交但标记尚未清”的持久化组合。保存它是小而明确的防御性元数据；**它不能代替原子清标记和资源屏障**。仅在外层 drain 事后清标记会重新引入崩溃歧义。

无需在 Task 保存原因或 actor 副本；受理审计保存这些快照。执行取消不能依赖审计记录仍可读，因为管理员可能通过既有审计清理显式删掉历史。

### 4. 执行与清标记的必要不变量

#### 4.1 受理事务与调度竞争

1. 受理条件包含 `Task.id`、`statistics_key`、真实 OPEN 状态和无 pending 意图；状态刚改变则重读原 UUID 后重新决定接受或 409。
2. 调度扫描 **和** `_claim_and_run` 条件 UPDATE 都必须拒绝有 pending 标记的 queued；不能仅靠内存集合或只在扫描过滤。
3. 已领取运行的任务走 running 分支，不假装仍在队列；接受前已经开始的模型调用属于原任务工作，不由管理员创建新任务。
4. 普通 owner 取消遇 pending 标记时委托 / 等待同一个 drain；resume / finish 不能覆盖 pending。不得在另一个 helper 中重复 abort 或清掉标记。

#### 4.2 queued / 原 paused：先安全清理，再提交 cancelled

管理员 pending 路径不能继续直接使用现有“先终态再 abort”的顺序：

1. 保持 queued / paused，释放请求侧事务，保留 pending 领取屏障；queued 保留活动资源键与批量范围锁。
2. 同一 UUID / operation_id 的单一后台 drain，整个清理阶段登记 `engine.cleanup_scope(task)`。
3. 幂等完成必要 handler 清理；不能在等待独立清理会话时持有它需要的请求数据库锁。
4. 最后一次条件 UPDATE 同时提交 cancelled、统一结束时刻、独立任务统计和清空全部 pending 字段；只在原 UUID + operation_id + 原状态命中时写统计。
5. abort 或最终保存失败：保留原 OPEN 状态、意图及资源屏障，给固定安全“取消收尾未完成”信息；后台兜底可再次尝试同一清理，不写虚假终态。

paused 本来不占活动资源键，不能为了取消重新占用或回滚其他新任务的资源。真实可暂停的当前 handlers 中 agent / book.import abort 为 no-op，batch 暂停前已清空 current；实现与测试必须保持这一前提。不能把任意遗留 paused 的不可信 current 当作可随意还原的资源所有权。

#### 4.3 running：沿用一次 cancel 与原子保存，清标记进入原 finalize 事务

1. drain 独立于 HTTP 请求生存，使用独立会话；请求取消不能连带取消引擎执行体或正在写数据库的清理协程。
2. 走 `cancel_running(task_id, statistics_key=...)`，不二次 `.cancel()`，不在等待其退出时持 Task / Project 请求事务锁。
3. 10 秒只是本次等待窗口。仍 running 时继续展示 pending，不能声称资源已释放；引擎最终会在后台收尾。
4. `_finalize` 读最新同 UUID 行，在其真实状态 + 统计提交中清 pending 标记；late done 保留 succeeded，late paused 保留 paused，不再对其运行旧 abort。
5. 对 admin pending 的 abort 失败，必须保留原 running 状态及 marker，不能按现有通用分支写 cancelled 后释放资源键。已退出执行体可作为“待安全收尾的孤立 running”由相同 drain 修复；不重新执行模型任务。
6. 状态事务保存失败时同样保留原 running + marker；若提交其实成功但确认丢失，按原 UUID 重读事实，不能再次写统计或把 succeeded 误作需 abort。
7. 默认普通用户与项目删除的旧路径保持原语义；新安全分支以存在管理员 pending 意图为条件，不扩大所有任务行为。

### 5. 重启、重复请求、清理和删除

- 新 drain 只处理 `(statistics_key, operation_id)` 绑定的任务，进程内 single-flight；启动及调度兜底唤醒 drain，不在 scheduler 主循环内串行等待所有取消收尾。
- 恢复扫描应覆盖所有带 marker 的状态，以暴露不一致数据；正常新实现不应产生“真实终态 + 未清 marker”。遇到这种异常组合，不能盲目 abort 已完成任务，须安全报告并阻止毁坏性清理。
- 重启遗留 running 仍遵守原恢复语义：在幂等清理成功后记真实 interrupted，并在同事务清 marker；不伪称已按用户请求取消。queued / 原 paused 的受理意图可继续取消，不重放模型或外部 MCP 调用。
- 原子保存成功、Task finalize 尚未提交即强杀进程，是现有引擎无法仅靠 Task 状态完全重建的极窄窗口。本任务不承诺“任意崩溃后重现精确赢家”，应遵循原启动 interrupted 语义，不引入伪成功，也不重新调用模型。若要强保证每次原子保存结果与 Task 终态一起提交，属于更大跨服务事务改造，不能口头声称已解决。
- 相同任务的第二个取消请求：pending 时返回明确“已在取消中”（或同一受理状态），结束后返回 409；不再次 cancel、不新增相反成功事实。客户端网络结果不确定时先刷新任务与受理审计，不自动重发。
- 受理审计写失败：整笔意图回滚，不能唤醒取消；业务拒绝按既有失败审计规则记录固定类别，不保存异常原文。
- 当前请求提交后被取消或返回投影失败：已受理事实保留，不补一条“取消受理失败”；marker 驱动 drain / 启动恢复。
- `delete_task` / `clear_finished` / `prune_finished` 不得删 pending 任务；通常该任务仍 OPEN，本身已被排除，但必须显式保护异常终态组合。仅在清理完成后恢复原保留与裁剪规则，不构成历史归档。
- `stop_project_tasks` / purge / expire 必须把 pending marker 与 drain 纳入排空条件；等待前 rollback 原请求会话。drain 的 `cleanup_scope` 贯穿 abort 与最终提交；不能看到 OPEN=0 就删除。
- 不允许删号级联掉尚有取消意图的无作品任务。既有“有作品不能删号”已保护大部分场景；若确有 `project_id=None` pending Task，也必须阻止删号直至 drain 完成。
- 正常关闭应等待已登记取消清理及真实执行链，再关闭数据库；不对执行中的 SQLite INSERT / RETURNING 发新的 cancel。数据库持续不可用时不能保证完成，持久化 marker 和安全失败信息应保留。

### 6. 审计传输与隐私的局部扩展

- 增加 `AuditAction=task.cancel_request`、`AuditTargetType=task`；管理任务自己的作品也必须 `include_own_project=True`，否则 `AuditOperation.__aenter__` 默认会关闭采集。
- `AuditSummary` 新增专门、有界字段，例如 task UUID、task kind、accepted 状态、原状态、`cancel_reason`（trim 后 1–200 字符；拒绝纯空白）。不开放任意 dict / extra 键，不存原始 Task.error / input / output / checkpoint。
- 用户主动填写的取消原因是明确的、局部允许的自由文本，不是任务原始异常；界面提醒勿填密钥或正文。不能宣称任意用户自由文本都能可靠自动脱敏。
- `audit_writer.__aexit__` 当前失败时重置 summary；若产品要求失败取消也保留原因与 UUID，应为该 action 增加精确白名单快照，不能全局保留所有失败 after 值。
- 审计列表标签为“取消请求受理”，result=success 只代表请求受理成功；审计不因 Task 移除而级联删除，仍受已有审计日期清理机制约束。
- 新取消边界的未知异常必须固定安全 500；运维只写 operation_id / UUID / committed，不记录可能包含 Task 输入、SQL 参数或原始上游错误的异常。

### 7. 必须阻断交付的验收证据

以下任一缺失均不能只凭接口能 202 就报告完成：

1. **受理失败全回滚**：注入审计 flush / commit 失败，断言无 Task marker、无终态统计、没有 signal / abort；安全响应和日志无原始错误。
2. **queued 领取屏障**：受理事务与真实 `_claim_and_run` 用事件屏障竞争；只允许领取先赢后走 running，或受理先赢后 queued 不运行，不能新增模型调用。
3. **queued abort 失败与重启**：abort 阻塞 / 失败时原 queued、marker、resource_key 保留；新同资源任务无法生成；重启恢复后只一次真实终态和统计，不误伤后来任务。
4. **paused 继续竞争**：pending 时 owner resume / finish 拒绝覆盖；已有暂停清理不回滚其他新任务。
5. **running 原子保存**：复用 `test_running_cancel_drains_atomic_save_before_cleanup` 的真实 RETURNING / commit 屏障；不能仅 sleep 或纯 mock 掩盖锁。
6. **late done / paused**：分别屏障于结果落库与 finalize 提交，断言真实赢家与 marker 原子清除，取消审计仍只是 accepted，之后不再执行旧 abort。
7. **running abort / 状态保存失败**：不写释放资源屏障的假 cancelled，marker 持久；恢复不重放 LLM、终态统计不重复。
8. **10 秒未完成**：返回仍 pending，不二次 cancel；HTTP 断开后 cleanup 仍在 `project_work`，后台可以继续收尾。
9. **Task ID 复用**：双 session 复用 SQLite ID，旧 request / drain / marker 清空不能影响新 UUID；现有 `test_task_statistics_identity.py` 是必须回归基线。
10. **项目删除 / 裁剪**：排空等待真实 drain；失败/超时作品不删；pending 不能被单删/清理/裁剪/级联抹掉；完成后原保留机制不改变。
11. **真实 actor 与边界**：两个管理员、本人作品、任务所有者各有明确身份；普通 owner 接口仍 404 隔离、管理员不获得 retry / resume / finish / output / stream 权限。
12. **重启窗口**：持久化 marker 后、signal 前模拟新 engine；queued 不执行、running 按原 interrupted 语义恢复；未知终态组合不得无屏障盲 retry abort。

基线回归：`test_tasks.py`、`test_recycle_bin_tasks.py`、`test_task_statistics_integration.py`、`test_task_statistics_identity.py`、`test_admin_audit_logs.py`。并发测试用 asyncio.Event 或受控数据库屏障，不用碰运气的 sleep。

## Related Specs

- `.trellis/spec/backend/admin-audit.md`：扩展受理动作及取消原因的唯一局部自由文本例外；不得把异步取消写成普通同步管理变更的假同事务。
- `.trellis/spec/backend/admin-dashboard.md`：UUID、原子保存、统计与状态同事务、删除不减累计。
- `.trellis/spec/backend/recycle-bin.md`：真实执行链排空、父行屏障、超时及 shutdown 顺序。
- `.trellis/spec/backend/auth-admin.md`：当前启用管理员、本人/最后管理员护栏、普通用户归属规则。
- `.trellis/spec/backend/quality-guidelines.md`：FakeLLM、隔离、事件屏障及精确回归。

## External References

无。问题是仓库既有事务和执行体边界，不需要引入新的第三方服务或调度库。

## Caveats / Not Found

- 本研究未运行测试、未验证 PostgreSQL 行锁；SQLite 结果不能替代真实 PG 并发验收。
- 现有 handlers 的 abort 不能被泛化为“新任务已抢资源后仍安全”；任何复用原 helper 的实现必须逐分支核对真实资源屏障。
- 本任务只保存现存 Task 的操作意图与既有审计，不是独立任务历史、通用 outbox、多实例调度或跨进程续跑。
- 如果实现选择保留“先终态后 abort”的管理员路径，则必须另做资源持有/清理标记并同步资源索引、批量范围检查与所有清理入口；其改动更大，不是上述三字段方案可以顺带保证的事情。
- marker 清空与真实状态提交必须在原 finalize / recover 边界落地；若实现者无法证明这一点，应阻断实现验收并回到设计，而不是在外层补日志或补清理来掩盖。

## 规划复核（2026-10-31）

复核对象：本任务 `design.md` 与 `implement.md` 的待审阅版本；只读复核，未激活任务，未运行产品测试。

### 结论

- 选择仅持久化 `admin_cancel_operation_id`、`admin_cancel_requested_at` **可以自洽**，不需要再增加 requested_status 或 cleanup_done。其充分条件是：queued / 原 paused 在必要清理成功前保留原状态；running 的 finalize / recover 在真实状态与统计的同一个条件提交内清空 marker；失败不越过资源屏障。
- 在此条件下，“paused + pending”只能是尚未结束的原 paused 受理；运行执行体赢得 late paused 时，paused 与清 marker 同时提交，不会留下恢复歧义。succeeded 同理。不能通过外层 drain 事后清 marker 达成该保证。
- 文档第 8 节已明确“正文保存已提交而 Task finalize 未提交”的任意强杀边界，未把既有未知窗口误写为精确赢家保证；没有必要扩大为跨服务原子保存改造。
- 受理提交后 HTTP 取消、10 秒等待未完成、项目排空与单删/裁剪/无作品删号的必要衔接均已被计划覆盖；实现时必须依照门禁测试，不能将文字当作完成证据。

### 必须明确补入实施门禁：检查必须进入条件写，不能只在读取后判断

现有 `task_service.py` 的普通状态 UPDATE 主要只检查 ID / UUID / 原状态（例如 resume 的 205–212 行、finish 的 238–245 行、普通 queued / paused 取消的 341–369 行）。若只在函数开头添加“发现 marker 则委托或拒绝”，仍存在可执行的穿透时序：

```text
owner: 读取 queued，无 marker
admin: 提交 marker + 受理审计
owner: 按旧状态 UPDATE queued→cancelled 并提交（没有 marker 条件）
owner: 在终态释放资源后才开始旧 on_abort
```

这会制造被方案禁止的“终态 + pending”，并让后来的同资源任务暴露于旧 abort。因此必须将以下内容作为阻断性验收，而非可选防御：

1. **普通 queued / paused / 孤立 running 的终态写，和 resume / finish 写**：正常路径条件 UPDATE 增加 `admin_cancel_operation_id IS NULL`；命中 0 时重读原 UUID，已 pending 则委托同一 drain 或拒绝恢复/结束，不能继续旧副作用。
2. **finalize / recover**：若先读取 marker / 清理结果再决定是否终态，真正 UPDATE 必须绑定观察到的 marker（null 则 `IS NULL`，非 null 则原 operation_id），或先取得跨 PostgreSQL / SQLite 都实际生效的行写锁再重读并决策。不能凭一次普通 SELECT 的旧 marker 判定“未涉及管理员意图”，然后把刚受理的标记忽略或覆盖。
3. **管理员 drain 的最终写**：必须匹配原 `id + statistics_key + operation_id + 预期状态`；只有成功命中才更新统计/清标记。条件失败重读事实，不再盲 abort 已完成或身份已更换的行。
4. **事件屏障测试**：分别暂停 owner resume/finish/cancel 于“最后一次检查之后、真实 UPDATE 之前”，让管理员受理先提交；断言旧 owner 写不能穿透。再暂停 finalize 于“读 marker 后、状态 UPDATE 前”，注入新受理及 abort 失败，证明不会写出释放资源屏障的假终态。

这项补充只增强已有协议的条件写证明，不需要新字段、额外历史表或新的产品范围。补齐后，本次只读复核未发现另一个必须增加持久化状态才能表达的竞态；最终仍须以真实双会话与屏障测试证明。
