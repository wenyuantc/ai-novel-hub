# Research: 章节生成占用、任务执行与失败清理

- Query: 为单章、场景和批量生成设计可证明的章节占用及受限清理；覆盖旧任务、恢复、备份、历史效果和 PostgreSQL 竞争。
- Scope: internal，基于当前工作树和项目规范的静态分析，不修改产品代码。
- Date: 2026-11-10

## Findings

### 文件和现有约束

| 文件 | 与本问题的关系 |
| --- | --- |
| `backend/app/services/generation_service.py:138-231,271-294,434-469,516-541,644-670` | 单章受理时抢占、组装任务、正常结果／富文档提案提交和 finally 清理。 |
| `backend/app/services/task_engine/handlers/chapter.py:47-115,117-170` | 单章 TaskSpec、运行 job、重试及引擎 on_abort；当前 Task 创建在抢占之后。 |
| `backend/app/services/batch_service.py:273-292,323-405,432-516,636-718` | 批量的 Task checkpoint、逐章和逐 attempt 抢占、失败清理及断点推进。 |
| `backend/app/services/task_service.py:220-274,277-330,553-653,779-849` | 任务插入、retry/resume、取消后清理、项目排空；部分实现正由另一代理修改。 |
| `backend/app/services/task_engine/engine.py:1-14,95-109,159-210,265-304,344-404,507-520,524-671` | 单进程调度、启动恢复、领取及确认未知、输出落库、终态和 on_abort。 |
| `backend/app/services/task_engine/cancellation.py:25-80,95-149`、`admin_task_service.py:185-233` | 管理员取消标记和独立 drain；必须纳入同一个运行身份与资源清理协议。 |
| `backend/app/services/rich_document_review.py:125-170,175-215,218-238,281-325,370-411` | 持久提案、释放章节、三重提案身份、采纳及批量继续。 |
| `backend/app/services/chapter_service.py:345-365,407-424`、`app/schemas/chapter.py:19-28` | 作者可以显式将生成中的章改回 draft/done；普通章编辑和原管理员保护。 |
| `backend/app/models/entities.py:79-84,520-544,645-703`、`task_statistics_service.py:24-68` | status 集合、章节与任务列、统计 UUID 基线和现有部分唯一索引。 |
| `backend/app/services/project_transfer/registry.py:88-105,285-295`、`importer.py:1462-1467` | 单书备份排除任务、章节运行列排除，导入 generating 改 draft。 |
| `backend/app/services/agent/effects.py:199-208,338-363,998-1038,1074-1101` | Agent 快照按模型列枚举，检查和撤销恢复也会读取列；新增运行列不能只在备份注册表中排除。 |
| `backend/app/services/admin_service.py:205-235` | 用户删除与任务级联；这部分当前也由其他代理修改，完成后仍需与新的 claim 状态交叉验证。 |
| `backend/alembic/versions/0025*`、`0026*`、`0044_novel_collaborators.py`、进行中的 `0045_kb_index_identity.py` | 旧 `statistics_key` 和 `queued_at` 可空；旧表原位扩列及 KB/Task execution 新迁移。 |

当前 `README.md:89,874`、`.trellis/spec/backend/database-guidelines.md:544-546` 和 `task_engine/engine.py:1-14` 明确要求任务引擎单进程。以下建议以**旧执行进程已停止并排空、同一数据库只有一个调度器**为恢复前提；数据库双连接用于验证锁和持久事实，并不等于新增多实例恢复支持。若将来允许多实例，须另有可证明持有者存活的租约／心跳及 fencing，不能只查本进程 `_running`。

### 现状：状态与既有 UUID 不是章节占用凭据

1. 单章 `validate_and_claim` 在任务记录建立前执行 `claim_chapter`；后者按 `id + status != generating` 更新章状态，不记真实任务、执行体、抢占序号或抢占前状态。Task 插入与该 UPDATE 在同一事务，但只在 `input.prev_status` 冗余旧状态（`generation_service.py:154-231`、`handlers/chapter.py:83-115`、`task_service.py:227-260`）。现有 `resource_key=chapter:{id}` 的唯一索引只排斥相同资源键的 queued/running Task（`entities.py:79-84,648-658`），批量任务使用 `batch:{project_id}`，不能证明当前 `generating` 仍属于某个单章任务。
2. 批量每次 attempt 都重新调用 `claim_chapter`，并与 `checkpoint.current={chapter_id,prev_status}` 同事务提交（`batch_service.py:432-467`）。同一 Task UUID 和同一 `queued_at` 下最多有多次自动重试（`batch_service.py:445-454,506-516`），因此把 `(task_key, queued_at)` 或新的 `Task.execution_key` 单独放在章上仍有 **ABA**：旧 attempt 清理延迟后，新 attempt 已重抢同章，两个 attempt 的任务和执行身份相同。每次章抢占必须使用新的不可复用 `chapter_claim_key`。
3. `ChapterJob.run_key` 目前在 job 构造时才生成（`generation_service.py:77-98`），晚于单章入队和批量章节抢占；它主要用于待审核提案。如果复用它作为 claim，必须把随机生成移到抢占前、与章占用和 Task 断点／输入一起提交，且证明所有现有提案调用仍使用同一身份。更容易审计的选择是将内部 claim UUID 与对外提案 run UUID 分开，严禁接收客户端提供的 claim。
4. `_restore_status`、`ChapterHandler.on_abort` 和 `batch_service.restore_current` 只比较整数章 ID 与 `status=generating`（`generation_service.py:434-440`、`handlers/chapter.py:154-166`、`batch_service.py:391-405`）。前者在 runner finally 执行，随后 `_finalize` 可能再次调用 on_abort（`engine.py:524-550`）。旧 finally 或重复 cleanup 可复位一个新 claim；`restore_current` 读取的是当前 Task checkpoint，也不能代替执行体捕获的旧 attempt 身份。
5. 作者可以单独或连同其他允许字段 PATCH `status=draft|done`，使旧任务状态复位而不推进 `content_revision`（`chapter_service.py:359-365,407-424`；`scenes.md:20`）。之后另一个批量或单章任务可抢同章；旧 `_save_chapter_result` 只检查当前状态和内容／规划 stamp（`generation_service.py:458-469`），可能把新占用当成自己的占用，提交旧输出或写提案。实际模型调用已有 `SceneContextStamp`，但 `scene_ai_context.py:62-80` 不含 claim 身份，因此不能替代 fencing。
6. 批量组装前先 `db.get(Chapter)` 再使用读到的 `chapter.status` 当 `prev_status`，UPDATE 条件没有旧状态比对（`batch_service.py:458-467`、`generation_service.py:219-231`）。并发人工 draft→done 且未推进内容版本时，返回的 `prev_status` 可能来自旧读值。应先按固定顺序持项目／任务／章节锁并重读行，或使用等价原子条件确保捕获的状态与成功抢占的是同一个事实；不能只给旧 UPDATE 增一个 UUID。
7. `_claim_and_run` 在提交确认未知时只查同一 Task UUID、queued_at 是否是 running（`engine.py:384-402`）；无法区分其他竞争者提交的 running。索引代理的 `Task.execution_key` 设计为每次实际领取前生成 UUID，与 queued→running 和 KB claim 变化同事务提交；确认未知只认本次生成的执行 UUID。该身份应由执行体捕获并贯穿 ChapterJob、BatchJob、hub、checkpoint 与最终状态更新，不能在旧执行体运行时重新读数据库当前值替换。
8. `TaskEngine.recover` 按本进程运行表判断遗留，先调用 handler.on_abort 再单独提交 interrupted（`engine.py:159-210`）；用户 queued 取消先提交 cancelled，再独立 on_abort（`task_service.py:573-586`）。崩溃可能发生在这两笔之间。若任务已终态但 claim 仍在，用户删除仅检查非终态任务并不足以防止级联删除清理依据。应优先把未运行任务的终态和匹配 claim 释放放进**同一事务**；运行任务协调取消／完成后也尽可能在同一受控事务写终态和释放，若必须拆分，须持久记录可恢复的 cleanup 意图，删号、裁剪和恢复都识别它。
9. 旧 `tasks.statistics_key` 和 `queued_at` 分别由 0025／0026 新增且迁移时未回填。更重要的是 `TaskEngine.start()` 在 recover **之前**调用 `initialize_tracking()`，它会给无 UUID 的旧任务补一个新的统计 UUID（`engine.py:101-108`、`task_statistics_service.py:24-46`）。恢复时 UUID 非空不是其曾与旧章状态绑定的证据，统计基线补码也不能冒充原 claim。新任务可在受理时显式生成自己的 UUID；旧任务需专门迁移／恢复策略。
10. Agent 撤销的 `_columns` 对章节枚举几乎全部 ORM 列，只排除 `content_revision`；`_values` 从持久 effect 读取当前存在的列并回写（`agent/effects.py:199-208,1086-1101`）。新 claim 列若只加入单书备份 `exclude`，仍可能被 Agent effects 捕获、对外暴露或被旧／伪造快照恢复。运行态列必须在生成快照、对旧 `before/after` 的比较归一、公开投影和 `_values` 恢复时统一排除；旧 effects 不能写入声称的 claim。不能为让旧记录过比较而忽略真实业务列差异。

### 建议的最小可证身份与数据

下面是实现方案，而不是现有代码已具备的保证。建议按功能切片实现并独立审查：

1. **任务身份**：在 `create_task` 调 handler 前就由服务端生成 `task_key = UUID4` 和入队代次；把该身份显式传入 `validate_and_claim`、Task 构造和统计登记，全部同事务。不得接受 request 中的保留字段，不以 SQLite 可复用整数 task ID 作为充分依据。retry 是新的 Task UUID；暂停后的 resume 可复用 Task UUID，但必须有新的 `queued_at` 和之后新的 execution UUID。
2. **实际执行身份**：采用 0045 进行中的 `Task.execution_key` 协议：每次 queued→running 前生成新 UUID，CAS 时同事务写入；未知提交只核对原 Task UUID、原 queued_at、原 execution_key 与状态。它区分不同领取执行体，**不能**代替批量同一执行中的章节 attempt UUID。运行过程中仅使用领取时捕获的不可变 primitive；不把新读出的执行 UUID当作旧执行体的凭据。
3. **每次章节占用**：建议在 Chapter 保存服务端内部的 `generation_claim_key`、`generation_claim_task_key`、`generation_claim_prev_status`（命名可调整）。三者完整成组或全空；完整组仅允许 status 为 generating、prev 为 draft/done，规范 UUID 严格验证。每次新抢占生成全新 claim_key，并且与章 status、所属真实 project、Task 的服务端 `input._chapter_claim`（单章）或 `checkpoint.current`（批量）在同一事务提交；对外 TaskOut、SSE、日志和提示词均不返回内部 claim。Chapter 上的随机 claim 加明确 task_key 是推荐的最小可查组合：Task 行本身提供 queued_at、execution_key、actor、grant，避免把多份可变字段当作互相独立的授权来源。也可冗余更多列，但所有副本必须在同一事务更新并交叉验证，不能只信某一个 JSON。
4. **queued 与 running 规则**：单章在受理时已持章，因此 queued 时 claim 绑定 Task UUID 和 claim UUID；引擎领取时须在同一事务检查原 queued 章 claim 未被作者复位并绑定／确认领取的 execution UUID。批量在运行时逐 attempt 新建 claim，事务同时写 `checkpoint.current`，且 Task 必须仍是原 UUID、queued_at、execution_key 和 running。批量下一次 attempt 必须生成新 claim，即使 chapter、Task、queued_at、execution_key 全相同；旧 attempt 的 finally／abort 只拿自己捕获的旧 claim。
5. **不可变接口示意**：

   ```python
   @dataclass(frozen=True)
   class ChapterClaimIdentity:
       project_id: int
       chapter_id: int
       task_key: str
       queued_at: datetime | None      # 仅明确验证的旧作者兼容分支可为空
       execution_key: str | None       # queued 清理时为空；运行发送/保存不得为空
       claim_key: str                   # 每次章抢占新 UUID
       prev_status: Literal["draft", "done"]

   async def claim_chapter(db, identity, *, task_status) -> ChapterClaimIdentity: ...
   async def require_chapter_claim(db, identity, *, purpose) -> Chapter: ...
   async def release_chapter_claim(db, identity, *, restore_status) -> bool: ...
   ```

   `claim_chapter` 在锁后从数据库读合法 prev_status，不信任调用者猜测；`require` 同时校验 project、章节、Task UUID/queued_at/运行 execution_key、Task 状态／取消标记、章 claim 和原协作 grant；`release` 在系统清理路径只可按已证实的该章 claim 恢复原 status 并清空 claim，不要求已撤销用户重新有权，也不得写正文。接口不能让 HTTP 或模型参数直接指定身份。若锁内结果与已捕获身份不同，应安全失败而不是重签。

### 事务、锁序、提案与执行体收尾

- **写路径锁序**：与任务设计保持认证 gate／相关 User（按 ID）→ 如影响真实发送先持 visibility 写门禁 → 作品父行 → Task／章节及其子资源，取得屏障后重新读取有效账号、项目、协作 role、invitation UUID/revision、模型来源和 claim。单章入口不要在 task row 之后倒取项目锁；批量目前 `db.get` 即抢占，应调整到相同协议。哪些写操作确实需要 visibility 写门禁以阻止后续出站，应由调用点按设计显式声明；SQLite 使用已有限时物理写锁及恢复 busy_timeout。进入保存点前也不得先 flush 未授权业务实体。
- **发送**：每次真实模型及向量 HTTP 请求／重试取得发送 lease 后，复核原 Task execution、当前章节 claim、原 grant、模型绑定及 SceneContextStamp；持锁至响应头，不把项目事务持到长响应流。作者手工复位要在项目锁内同时撤销旧 claim；如承诺复位已提交后没有后续发送，应与相同发送门禁串行。正在飞行或已返回的字节及费用无法撤回。后续输出／事件也需当前权限复查。
- **正常落库**：在项目、Task、章节锁内比较不可变身份与 stamp；正文、场景、版本、真实 actor 日志和清除 claim 应同事务提交。身份不匹配、cancel marker、授权收紧、章所属项目异常或章记录已删除，一律不写旧正文。数据库提交确认未知时只查询原操作／claim 的持久事实，不能重新提交模型结果或将其他运行者提交视为自己的成功。建议在同事务保存最小可信完成收据（任务 UUID、execution、claim、章节、结果状态和版本），使恢复能区分真正提交与未提交；收据不得外发内部模型绑定。若尚不能证明，则保留未知状态供人工核对，不重复发送。
- **批量断点**：`_write_checkpoint` 和所有后续状态变更须按 Task UUID + queued_at + execution_key + 状态 CAS；`checkpoint.current` 与同事务章 claim UUID 一致。正文成功和将 checkpoint 从 generate 推进 analyze 当前是两笔（`batch_service.py:681-698`）：崩溃窗口内 `include_written=true` 的显式 retry 可能再次生成已提交正文。应将最小章节完成收据与正文同事务提交，重启按事实推进／提示核对，不能自动重发未知外部模型请求；已完成前章保持不变。
- **提案**：`persist_pending` 在保存时校验 Task UUID、execution UUID、原 grant、章 claim、stamp 与 proposal token，并在同一事务写持久提案和 checkpoint、恢复章节 prev_status、清除该 claim（`rich_document_review.py:175-215`）。提案 `run_key/token` 是确认身份，不等于当前章仍被任务占用。日后 `accept_review` 必须在锁内校验本人、**原**协作 grant、提案 task/run/token、源内容／格式快照与无其他占用；撤权重邀不能让旧提案自动续签。批量采纳后 queued_at 更新、新领取 execution UUID；已确认的提交不因后续撤权撤销。
- **人工复位**：作者原本可以复位生成状态，保留该可见行为；在同一持锁事务清空精确当前 claim 并使旧执行失效，不把新状态回写到旧 task input。非作者的原管理员／协作限制保持当前项目规则。状态改变后旧 cleanup 对旧 claim 的 CAS 只能影响 0 行，新执行不被复位；不能只推进 `content_revision` 伪装资源身份。
- **取消与恢复**：排队取消在同一事务把 Task 置 terminal、匹配 claim 回退、统计变更和必要审计一起提交；运行中取消等待本进程已捕获身份的执行体或在已排空条件下由系统按精确身份清理。多次取消、晚 finally、on_abort、管理员 drain 的重复调用都幂等：claim 不匹配即不修改；若实际 DB 异常则保留可恢复事实并报告收尾失败。`_flush`、`complete_state`、`recover`、`admin_cancellations.winners/completed`、`_jobs`、hub 等不能继续只按 task ID/Task UUID识别旧执行，均需检查 execution/queued 代次。项目删除／账号删除／裁剪要等待未清理占用，而非仅判 Task 的 terminal 状态。
- **项目锁与对抗未 flush 值**：`no_autoflush` 保护授权前的待写对象；锁后使用标量或安全 refresh 重读身份字段，危险的待修改 identity 拒绝，不能通过 populate_existing 静默覆盖调用方合法普通字段。若 `claim` 在活动 Task 提交前失败或冲突，章节和 Task 统计均必须原子回滚。

### 迁移、备份、Agent 历史和统计兼容

- 建议在当前 index_identity 代理负责的 `0045` 之后使用独立的章节迁移，或先与其协调统一迁移；不要两个代理并行修改同一迁移。Chapter 是 Scene、ChapterVersion、规划等多张表的外键父表，SQLite 增加多列 CHECK 时不要盲目通过 batch 重建父表；可研究与 `0045_kb_index_identity.py:23-35` 同类的原位列／检查约束，并在打开 `PRAGMA foreign_keys=ON` 的旧库真实验证既有章、景、版本、规划和任务不丢失，执行 `foreign_key_check`。PostgreSQL 与 SQLite 的 nullable / CHECK、UUID 长度与类型检查必须一致。旧 `generating` 且 claim 全空必须能表示为**历史未认证占用**，不能在迁移中臆测作者、默默改成 draft 或写假 claim；新写路径不允许制造该状态。
- **旧任务恢复前提**：按 `design.md:84-89` 停止／排空旧任务和旧服务，禁止新旧进程混写；新版引擎调度前处理。旧 queued/running 即便已有 Task UUID，也没有章 claim 的历史对应证据；`initialize_tracking` 可能刚补了 UUID。若无法唯一证明同项目、同章、真实作者、任务范围和无人存活，应失败关闭，保留原正文与状态并提供明确人工核对／复位路径。对于已确认全部旧执行体停止且**唯一**旧作者任务／章关联满足严格校验的记录，可在项目、Task、Chapter 锁内原子新建服务端 claim 后再恢复，不从公开请求或缺损 checkpoint 推测；已结束／待审核有持久 proposal 的原结果不能重复发送。对 null queued_at 的旧作者需要明确独立编码和比较，不把新任务的合法时间误判为旧 sentinel。
- **单书备份**：Task、提案和执行身份本属 `NOT_EXPORTED`；章 claim 列加入 Chapter 的 `TableRule.exclude`，不可随 JSON 泄露或导入。现有导入器将 generating 改为 draft（`importer.py:1462-1467`），保留该兼容，导入的新章 claim 一律空，且不能从客户端文件填充。反射注册、旧版本、活跃章与空章往返都要测；单书导出运行中数据时原有内容版本和事务一致性约束保持不变。系统级备份仍保留真实数据库运行列；隔离恢复时旧进程必须先排空并遵守同一恢复协议，不能直接运行旧服务读取新多用户日志。
- **Agent effects**：将全部章 claim 列从 `_columns` 查询快照、`_validate` 的历史 `before/after` 比较、所有公开 effects、`_values`／`_restore_rows` 中排除或明确拒绝；历史无新列的效果不应因此全部冲突，新格式即使意外含 claim 也不能回写它。无效生成中章继续按原要求拒绝撤销；测试删除空章后的旧效果恢复、旧无字段效果、试图注入运行字段的坏效果、新 claim 在撤销中途被替换、正文与时间线保持原有边界。
- **统计和管理取消**：`task_statistics_service.record_created` 使用 Task.statistics_key，且 `record_state_change` 只在真实 CAS 命中后登记（`task_statistics_service.py:44-82`）；不得在本次生成 claim 或 retry 中重新赋值已有 key，也不能把 execution/claim 误认为新 Task 而额外计数。`admin_task_service` 的 operation ID、drain 的 winners/completed 和 TaskOut/SSE 都需绑定实际任务及运行代次；移除 Task 前 `mark_removed` 与 claim 检查顺序须维持 Task→统计的现有约束，并兼顾项目锁顺序。
- **降级**：除了 0044 的协作／日志保护，新增占用迁移有任何非空 claim 或执行／清理事实时拒绝降级；不能靠删记录通过。对于旧 `generating` 无 claim 且无法确认处理，降级若会让旧服务接触仍运行的结果同样不能声称安全。先在隔离库验证空库、旧库、活动占用、异常部分值、有无已保存内容和待审提案，确认各类失败均不清理数据。

### 验证清单（均为建议，尚未执行）

在独立 PostgreSQL 16 库的随机 schema 中，用两个真实连接和单进程内受控的两个执行体／阻塞点，分别测试锁的两种获得顺序；不能用 FakeLLM 的无物理 lease 路径替代真实本机 HTTP 请求：

1. 单章任务 A 正在流式发送，作者只改 status、随后 batch B 抢同章；A 的后续每次出站、保存、待审提案和 finally/on_abort 均不得通过；B 最终正文／章状态和 claim 不受 A 清理影响。原内容未改时 stamp 一样，专门证明 claim 而非内容版本阻止 ABA。
2. 同 batch 同一 Task UUID、queued_at、execution_key 内，首 attempt A 失败后被延迟清理，attempt B 获新的 claim；A 不得清 B，B 可成功。额外测试 retry 创建新 Task、paused/resume 改 queued_at 和 execution、用户取消、管理员取消、进程关停、重复 finally 和双重 abort。
3. 旧抢占事务提交确认丢失：分别实际提交／实际回滚后让其他尝试取相同章节，确认只认原 claim 与原 execution 的持久收据，不把竞争者的 running 或 generating 认作己方；结果提交先于 crash／cancel、未提交先于 cancel 两种顺序，保持已提交正文但不重发未知外部调用。
4. 取消 queued 与新 claim、状态终态与资源释放、删用户／裁剪／删除项目并发：旧 cleanup 先提交和新工作先提交；在任务已 terminal 但清理失败时拒绝删掉唯一证据，恢复只按旧 claim 回退且不改新用户任务。账号禁用、协作撤销、降级、修改授权及重新邀请均阻止下一次发送／保存，cleanup 仍能受限完成。
5. 初始合法 prev_status 与人工 draft→done 的并发：成功抢占必须保存锁内真实前状态；不覆盖他人的合法变更。跨项目伪造 chapter ID、任务整数 ID 复用、null/畸形/非 canonical UUID、损坏 checkpoint、旧作者 null `statistics_key/queued_at`、统计 baseline 补发 UUID 都失败关闭或明确进入兼容修复路径。
6. 富文档需要审核与不需要审核两条；验证提案与 claim 原子转移、旧邀请不能采纳、重复相同采纳幂等、不同采纳冲突、batch 恢复及收据不重发分析；旧 effects 与单书／整库备份不携带或恢复 claim，旧正文／场景／版本完整。用真实 PG 和 SQLite 外键开启测试迁移、降级及 `foreign_key_check`。
7. 运行既有 `test_tasks.py`、`test_batch_generate.py`、`test_rich_document_proposals.py` / `test_rich_document_postgresql.py`、`test_scene_ai_postgresql.py`、`test_admin_task_monitoring*`、`test_recycle_bin_tasks.py`、`test_task_statistics_integration.py`、项目备份和 Agent effects，再跑新增专用协作并发；旧用例中手工插入 generating／running（如 `test_tasks.py:336-357`、`test_batch_generate.py:491-509`）须分别标明模拟新 claim 或明确测试旧兼容，不能只修改断言让测试绿。

## Related Specs

- 本任务 `design.md` §§3.3、5、6，`implement.md` E3–E5：真实 actor、邀请轮次、发送与持锁保存、受限清理及部署边界。
- `.trellis/spec/backend/database-guidelines.md:512-553`：Task 状态、唯一资源索引、统计和单进程恢复前提。
- `.trellis/spec/backend/batch-generate.md:47-83`：逐章断点、自动重试、暂停和旧恢复语义；新 claim 必须同步修订已实现契约。
- `.trellis/spec/backend/scenes.md:143-162`：场景 stamp、原子正文及任务 retry；`.trellis/spec/backend/rich-documents.md:25-38`：提案与运行身份、备份和确认语义。
- `.trellis/spec/backend/admin-task-monitoring.md`、`recycle-bin.md`、`project-transfer.md`、`admin-backups.md`：管理员 drain、项目排空、单书与整库备份现有边界。

## External References

本研究仅核对本仓代码、任务设计和项目规范，没有使用外部文档。数据库驱动实际提交确认丢失、SQLite DDL 和 PostgreSQL 锁顺序均需上述真实隔离测试；静态描述不等于测试结果。

## Caveats / Not Found

- 本文为共享工作区并行修改期间的静态快照，行号和接口可能变化；`0045` 及 `Task.execution_key` 当下仍在其他代理实现中。未修改产品代码，未执行测试或真实模型请求。
- 章 claim 尚未在当前 Chapter 模型及调用链中实现，上述 schema / API 为建议，不能声称 E3/E4 或协作者 AI 已完成。
- 当前产品明确是单进程调度；两个连接／执行体的压力测试用于验证 fencing，不构成多进程调度、租约或跨实例恢复承诺。
- 若希望兼容旧 UUID 缺失的活动任务，需在旧进程全部停止后定义可审计、可证明的修复流程；简单给它补 UUID 不足以证明历史 claim。
