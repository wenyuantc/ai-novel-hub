# 一致性检查、问题清单与修改影响追踪

来源任务：`11-17-consistency-check-issues`（迁移 `0058`，备份 v12）与 `11-17-edit-impact-tracking`（迁移 `0059`，备份 v13，见第 8 节）。跨层合同，适用于后端 `services/consistency/`、任务处理器 `handlers/consistency.py`、接口 `api/v1/consistency.py` 与前端一致性检查页。任务与订阅契约见 [error-handling.md](./error-handling.md)，多步任务模式见 [batch-generate.md](./batch-generate.md)，证据锚点沿用 [mention-tracking.md](./mention-tracking.md)。

## 1. 范围与触发

- 作者手动发起的后台任务 `consistency.check`：先逐章抽取**事实台账**，再跨章比对，产出「潜在冲突」问题清单。检查只读正文、伏笔记录与本域三张表；不写正文、设定、伏笔、剧情记忆、`chapter_analyses`，不写生成记录、不记 token。
- 事实台账是与「修改影响追踪」（第 8 节）共享的契约：`consistency.impact` 任务复用 `ledger.load_source` / `build_extract_messages` / `parse_extraction` / `save_ledger`，不改既有列的含义，只增加「上一版」三列（`prev_*`）。
- 修改抽取或比对的输入、合并规则、三张表、任务事件、备份登记、证据跳转时，对照本文件。

## 2. 签名与存储

本域各表（含第 8 节的 `consistency_impacts`）均 `project_id` 级联删除，**不存章节号**（章号一律读取时按 `chapter_id` 联表，因此不登记 `CHAPTER_NO_REFS`；将来确需存章号必须登记）。

| 表 | 要点 |
|---|---|
| `consistency_ledgers` | 每章至多一行（`chapter_id` 唯一、级联）。`source_hash` = 抽取时的 `analysis_fingerprints`（含开启 AI 可见的作者区块）；`text_hash` = `Chapter.content` 的 `matcher.text_hash`；`facts` JSON；`schema_version`；`prev_facts` / `prev_source_hash` / `prev_text_hash`（0059，上一版，见第 8 节） |
| `consistency_runs` | `task_id`、`actor_id`、`scope {mode: all\|range\|issue, start_chapter_id, end_chapter_id, issue_id?, chapter_ids}`、`status`、`stats`（计数、`skipped_chapters`、`warnings`、`phase`、`cursor`）。`sqlite_autoincrement`：问题按运行 id 先后判断「未复现」，id 不得复用 |
| `consistency_issues` | `category`、`title`、`description`、`suggestion`、`severity`、`status(open\|resolved\|ignored)`、`note`、`revision`（状态与备注的 CAS）、`identity_key`（`(project_id, identity_key)` 唯一）、`evidence` JSON、三个 run 引用、`reappeared` |

- 事实：`{id, category, subject, subject_key, statement, time_hint, quote, start, end}`。`id` 由服务端按保留顺序分配；`subject_key = facts.subject_key`（NFKC、去空白、casefold）；`start/end` 是 `Chapter.content` 的码点半开区间，由 `facts.locate_quote` 逐字查找首个匹配，找不到为 null。**不采用模型给出的任何 id 或偏移。**
- 证据：`{chapter_id, fact_id, quote, start, end, text_hash}`，引句与偏移取自台账。
- 派生字段不落库：`needs_recheck`（任一证据章节当前 `text_hash` 与存值不同、章节不存在或 `chapter_id` 为 null）；`not_reproduced`（`status=open` 且 `last_checked_run_id > last_seen_run_id`）。

接口（前缀 `/api/v1/projects/{pid}/consistency`，权限统一走 `project_access.require_project`）：

| 接口 | 能力 | 合同 |
|---|---|---|
| `POST /checks/preview` | `ai.use` | `{mode, start_chapter_id?, end_chapter_id?}` → `{total, to_extract, reusable, empty, warnings}`；不建任务、不校验模型 |
| `POST /checks` | `ai.use` | 同上 + `ai_config_id?` → 202 `TaskOut` |
| `GET /summary` | `content.read` | `{last_run, counts_by_status, ledger_coverage{fresh,stale,missing}, running_task_id, running_task_key}`；`running_task_key` 只给任务发起者本人 |
| `GET /issues` | `content.read` | `status?,category?,chapter_id?,needs_recheck?,page,page_size(1–100)` → 分页 |
| `GET /issues/{id}` | `content.read` | 证据含当前章号、标题、`stale` |
| `PATCH /issues/{id}` | `content.edit` | `{revision, status?, note?}`；至少一项、拒绝 null、未知字段 422 |
| `POST /issues/{id}/recheck` | `ai.use` | 202 `TaskOut`，范围 = 该问题现存证据章节（`mode: issue`） |

## 3. 关键合同

### 任务

- `resource_key = consistency:{project_id}`（同作品同时只允许一个进行中的检查；修改影响评估共用同一个键，两者互斥），无 `serial_key`，不与章节分析、批量生成互锁。`input = {request, chapter_ids, scope, ai_config_id, _project_model}`；`chapter_ids` 是受理时范围内的非空章，按 `(chapter_no, id)` 定稿。需重新抽取的章数上限 `MAX_EXTRACT = 300`（可复用的不计入）。
- 受理走锁外 `ai.use` 短读 + 模型预检，再进中央受控受理。处理器声明 `execution_identity_aware`、`admission_preflight_aware`、`collaboration_execution_ready`；构建前、每次出站（`for_task` 发送 lease）、**每次落库事务**（`guard.require_task_write`）、每个事件与终态写入前按原 grant 与模型绑定重验。撤权／降级／关作者模型授权后停止且不再落库，已提交的台账、问题与运行记录保留。
- **断点不写 Task**：通用发送守卫要求运行期间 `step_cursor`／`checkpoint` 与领取时逐字一致。进度写在 `consistency_runs.stats`，与每章台账同事务提交；续跑依靠台账新鲜度，重试（`validate_retry`）沿用原 `chapter_ids` 与模型绑定，已抽取且仍新鲜的章不再调用模型，比对阶段整体重做。`TaskOut.checkpoint` 恒为 null。
- 引擎登记：`_COLLABORATION_QUEUED_CLEANUP_KINDS` 与 `_DRAFT_ONLY_KINDS` 含本种类（后者为借用：终态核验失败时任务改记无结果 failed 并释放互斥，已保存数据保留）。智能体 `start_task` 白名单不含本种类。
- 抽取（逐章）：章节已删 → `skipped_missing`；台账新鲜 → `reused`；否则调用模型 → 规范化、定位引句 → `save_ledger` 在落库事务内锁定重读章节，分析指纹或正文指纹与抽取时不同即 `skipped_changed`、不保存。单章模型／解析失败记 `failed` 并继续；需要抽取的章节全部失败时任务 failed。
- 抽取完成后、比对开始前又被修改（或删除）的范围内章节：台账已过期、不参与比对，改记 `skipped_changed` 并进入概要。
- 比对：使用范围内章节加范围外已有的**新鲜**台账（范围外过期的不使用，计入 `stale_outside`）。同一主体的人物经历、状态、物品事实合为一组，时间线、伏笔各自成组；只保留跨两个不同章节且至少一条事实在范围内的组（伏笔组在有伏笔记录时一章即可）。预算内分批，超过预算的大组按章节顺序拆成连续几批。比对批次全部失败时任务 failed。
- **协作者订阅与出站 lease（SQLite）**：原由 `SQLITE_SEND_GAP`（`check._before_send`）人工休眠缓解；2026-11-20（TODOS 第 22 项）在 `stream_lease._task_frame_lease` 改为默认回滚日志模式下的 SQLite 真实 `SHARED` 读事务（与 `_send_lease` 的 `RESERVED` 写锁兼容、仍阻塞撤权等写方 `COMMIT`）后已根治并移除该休眠，详见 [error-handling.md](./error-handling.md)「SQLite 共享读帧租约」。慢模型单次响应头超过帧期限（5 秒）时进度流仍稳定不断线；仅外部手工启用 SQLite WAL 时保守回退写锁路径。
- 事件：`stage`（`第 3/40 章 · 抽取事实`、`比对 2/9 批`、`比对准备中`、`合并问题`，已登记管理员任务监控的安全阶段）、`delta`（进度日志）、`done`（概要）、`error`。**模型原文不进入任务输出。** 进度抽取 70%、比对 25%，单调且 ≤95。

### 出站边界

- 抽取只发送章节正文与开启 AI 可见的作者区块（与章节分析同口径，`ledger.visible_text`）；关闭的区块根本不读取。**不发送任何结构化设定**，与设定的 AI 可见档位无关。
- 比对发送事实陈述、时间线索、原文引句及可定位的场景日期依据。上下文还包括 `include_in_context` 为真的伏笔记录及其章号，以及同类别且证据章节都在本批内的已有问题标题和说明。
- 提示词覆盖按真实操作者读取，协作者不使用作者的私人模板。

### 不静默截断

以下情况必须进入运行记录 `stats.warnings`（前端在「本次检查的提示」展示），由 `issues.batch_notices` 统一生成：

- 大组被拆成多批（跨批的冲突无法检出）；
- 伏笔记录超过 `MAX_FORESHADOWS = 200` 或受预算裁剪而未随比对发送；
- 已有问题摘要超过每批 `MAX_EXISTING_PER_BATCH = 30` 或受预算裁剪而未发送。

### 输出校验与合并（C8）

- 证据必须引用本批输入中存在的 `chapter_id + fact_id`，否则丢弃并记 warning；只保留证据跨两个不同章节的问题，或只涉及一章但指认了本批伏笔记录的伏笔类问题；没有任何证据在范围内的不报告。
- `identity_key = sha256(category | 排序后的 subject_key | 排序后的证据 chapter_id 集合)`（伏笔记录矛盾另加 `#foreshadow:{id}`）。
- `existing_issue_id` 只接受**本批输入给出的**已有问题编号；其余（编造、别的作品、同作品但未给出）在输出校验阶段拿掉并记 warning，改按身份键处理。合并层仍按作品再判一次。按 id 指认且证据章节集合变化时，`identity_key` 跟随新证据。
- 已存在且本次检出：更新说明、建议、证据与 `last_seen_run_id`；`ignored` 保持，`resolved` → `open` 且 `reappeared=true`，`open` 保持。不存在：新建 `open`。
- 未检出的 `open` 问题，证据章节全在本次覆盖内且至少一个在范围内：只更新 `last_checked_run_id`（读取端显示「本次未复现」），状态不变。**有比对批次失败或大组被拆批时不做此标记**（覆盖不完整）。
- 合并在比对末尾单事务完成，`require_task_write` 持作品行锁直到提交；作者的状态 CAS（`require_project(..., for_update=True)`）与之串行。`revision` 只在作者改状态或备注时递增，检查更新不递增。作者改状态时清除 `reappeared`。

### 读取

- `needs_recheck` 筛选与 `chapter_id` 筛选先取全部候选、批量取所涉章节指纹后在内存过滤，再分页；不逐条查库、不静默截断。
- 运行记录终态由任务收尾尽力写入；读取端以任务真实终态校正，任务已不存在而记录仍为 running 时按中断显示。

### 章节生命周期

删除章节：台账级联删除（已登记 `chapter_delete_guard._REVIEWED_FKS`），问题证据保留 `chapter_id` 与引句，读取即 `needs_recheck`。改章号、改标题：读取即当前值，不影响指纹。版本恢复、AI 保存、导入等任何正文写入都按指纹判定，无事件总线；写回原文后证据重新有效。

### 备份（v12）

`consistency_ledgers`、`consistency_issues` 在 `RULES`，`consistency_runs` 在 `NOT_EXPORTED`。不导出 `task_id`、`status_changed_by` 与三个 run 引用（导入后为空，`not_reproduced` 为假；备份中伪造的这些引用被忽略）。`evidence[].chapter_id` 经 `remap.consistency_evidence` 重映射，对应不上置 null、保留引句。`parse_backup` 用 `LedgerFact`／`IssueEvidence` 校验 JSON 结构、数量上限与重复键。v1–v11 补空表导入；旧版本文件夹带这两张表的数据时拒绝。

### 前端

- 路由 `projects/:pid/consistency`（懒加载），导航在「写作与校验」分组；`api/consistency.ts` 镜像 schema，纯逻辑在 `utils/consistency.ts`。
- 页面按 `项目:身份` 整体重建；查询用 `useConsistencyQuery`（作用域比对 + 中止，迟到响应不回填）。显隐经 `consistencyPermissions`：改状态需 `canEdit`，发起／重新核查需 `canUseAI`，取消只对持有 `task_key` 的发起者。
- 证据跳转 `evidenceLink` 复用 `mentionHref` 的 `mention_start/end/hash`：有锚点 → 定位；无偏移 → 该章开头并说明「未能逐字定位」；章节已删 → 只显示引句、不提供跳转；正文已变 → 仍带原锚点，由目标页按指纹提示失效。
- 状态冲突（409 `stale_revision`）保留本地草稿，刷新后 `rebaseDraft` 只让未改过的字段跟随服务端。
- 任务结束（订阅或轮询发现）后刷新概要、列表与打开中的详情。文案一律表述为「潜在冲突」。
- 进行中任务的接上、轮询、终态核对与取消统一在 `components/consistency/useRunningTaskFollow.ts`（检查页与「修改影响」页签共用，不各写一份）。有检查在进行时始终轮询概要（订阅中也保留）。订阅仍显示进行中而概要已不列该任务时（`streamWatchAction` → `verify`），读取任务真实状态，确认结束（`checkEndNotice`）后断开订阅并刷新；任务仍在运行则不动（概要可能是旧数据）。
- 证据链接另带 `mention_from=consistency`：目标页位置失效时用 `staleAnchorMessage` 的中性措辞，不提示「回提及列表」。`mentionSegments` 的焦点高亮严格等于焦点区间，提及在焦点边界切段。
- 详情抽屉：保存后焦点留在抽屉内的状态选项，关闭后焦点回到打开它的列表条目（`data-consistency-issue`）。焦点目标经 `statusFocusTarget`（`components/consistency/statusFocus.ts`）先取当前选中项、没有才退回第一个；不要写成选择器列表 `'input:checked, input'`，`querySelector` 按文档顺序返回第一个命中任一选择器的元素，会落到第一个单选上，键盘用户按空格就把状态改了（2026-11-18 浏览器验收 B-4）。

## 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 非协作者、待接受、已撤权、他人作品、回收站作品 | 404 |
| 查看者发起检查、重新核查、改状态；仅管理员人工权限调用 AI 入口 | 403 `project_readonly` ／ 404，不建任务、不调用模型 |
| 起止章节不属于本作品／起始晚于结束 | 400 |
| 范围内没有非空章节；问题的证据章节均已删除 | 400 |
| 需重新抽取的章数超过 `MAX_EXTRACT` | 400，提示缩小范围（预览给 warning） |
| 没有可用模型、SSRF、解密失败 | 400，不建任务 |
| 同作品已有进行中的检查 | 409 `已有进行中的一致性检查，请等待其完成后再试` |
| 同作品已有进行中的修改影响评估（发起检查、重新核查时） | 409 `已有进行中的修改影响评估，请等待其完成后再试` |
| 问题不存在或不属于本作品 | 404 `问题不存在` |
| `revision` 过期 | 409，`code=stale_revision`，前端保留输入 |
| `status`／`note` 为 null、两者都缺、备注超过 2000 字、未知字段 | 422 |
| 运行中撤权／降级／关模型授权／可见性变化 | 任务 failed `权限或模型授权已变化，已停止发送`，此后不落库 |

## 5. Good / Base / Bad

- Good：第 5 章写明佩剑已毁、第 12 章仍在使用 → 一个「物品去向」问题，两处证据各带引句与码点锚点；再次全书检查不调用抽取模型，问题不重复。
- Base：只检查第 10–15 章 → 不重新抽取范围外章节，仍报告第 5 章与第 12 章之间的问题，不报告完全在范围外的问题。
- Bad：把模型返回的偏移直接存为证据锚点；按模型给出的任意问题编号覆盖已有问题；伏笔记录截到 200 条却不告知作者。

## 6. 必需测试

- `test_consistency_prompts.py`：归一化、码点定位、事实规范化、模板登记与锁定变量、消息只含正文。
- `test_consistency_ledger.py`：服务端偏移、复用不调用模型、调用期间改章／删章不保存、关闭的作者区块不出站、畸形输出。
- `test_consistency_issues.py`：佩剑示例、范围口径、三种状态的重复检查、未复现的三个前提、按 id 指认与未给出编号被拒、伏笔记录边界、派生字段批量计算、分组分批、拆批不标记未复现、截断告警。
- `test_consistency_check.py`：预览、互斥、上限、取消保留台账与重试续跑、单章失败、检查期间及抽取后改章、删章、批次失败、范围、管理员监控阶段、进度流、出站不含结构化设定与隐藏伏笔。
- `test_consistency_api.py`：筛选与过滤后分页、概要与覆盖、CAS、检查不递增修订、删章证据、改章号与正文写回、跨项目与回收站、重新核查范围。
- `test_consistency_collaboration.py`（及 `_postgresql`，需 `AINOVEL_COLLAB_POSTGRES_TEST_URL`）：查看者只读、非成员 404、管理员人工权限不延伸到 AI、编辑者模型与回退、受理后／领取后／发送中／合并前／终态前的权限变化。
- `test_consistency_migration.py`、`test_consistency_transfer.py`：0058 结构与受保护降级、v12 往返与证据重映射、伪造引用被忽略、v11 导入、非法值在预览拒绝。
- `test_consistency_stream_slow_model.py`：模型响应头每次延迟 2 秒、连续四章时，协作者订阅完整收到进度与完成事件；让出窗口只对协作者 + SQLite 生效。
- 前端 `api/consistency.test.ts`、`utils/consistency.test.ts`：接口路径、证据链接四种情况、筛选参数、草稿与冲突、只读显隐、请求代次。

## 7. Wrong vs Correct

```python
# Wrong：信任模型指认的任意编号，覆盖了同作品里一条无关问题的说明与证据
target = by_id.get(model_output["existing_issue_id"])

# Correct：只接受本批输入给出的编号，其余按身份键处理并记 warning（issues.parse_comparison）
if existing_issue_id is not None and existing_issue_id not in offered_ids:
    unknown_refs += 1
    existing_issue_id = None
```

```python
# Wrong：大组拆批后仍把全部章节当作「已覆盖」，旧问题被误标为本次未复现
covered_chapter_ids=data.covered_ids

# Correct：批次失败或拆批时覆盖不完整，不标记（check.run_check_job）
covered_chapter_ids=frozenset() if failed_batches or incomplete else data.covered_ids
```

## 8. 修改影响追踪

来源任务 `11-17-edit-impact-tracking`。实现：`services/consistency/impact.py`（读取、第一级、确认）、`impact_task.py`（评估任务）、`handlers/consistency.py::ImpactHandler`、同一路由文件；前端 `components/consistency/Impact*.tsx`、`utils/consistencyImpact.ts`、`hooks/useChapterImpactHint.ts`。

### 8.1 数据（迁移 0059）

- `consistency_ledgers.prev_facts / prev_source_hash / prev_text_hash`（可空）：`save_ledger` 覆盖已有台账且**来源指纹不同**时把旧的 `facts / source_hash / text_hash` 移入；指纹相同的重复保存（强制重抽、结构版本升级）不移动。只保留一版。无论台账是被评估还是被一次检查刷新，改动前的事实都留有一份。
- `consistency_impacts`：每章至多一行（`chapter_id` 唯一、级联，已登记 `chapter_delete_guard._REVIEWED_FKS` 与归属检查）。`source_hash` / `text_hash`（评估时本章指纹）、`base_source_hash`（改动前台账的指纹）、`changes [{kind: added|removed|changed, category, subject, subject_key, before, after}]`、`items [{chapter_id, subjects[], evidence[{fact_id, quote, start, end, text_hash}], fact_count, status: pending|reviewed|dismissed}]`、`uncovered [chapter_id]`、`acknowledged [setting_progressions.id]`、`revision`（确认状态 CAS，重新评估同样递增）、`actor_id`、`task_id`、`model`、`assessed_at`（空 = 只为保存 `acknowledged` 而建的未评估行）。
- 派生字段不落库：`outdated`（本章当前分析指纹 != `source_hash`）；影响项 `changed`（被列章节已删除，或其当前 `text_hash` 与任一证据存值不同）；`no_change`（已评估且 `changes` 为空）。
- 降级：`consistency_impacts` 有数据或任一台账 `prev_*` 非空时拒绝。`test_consistency_migration.py` 的 0058 冻结结构排除 `prev_*` 三列。

### 8.2 第一级：派生内容过期（零模型调用）

`impact.derived_stale(db, project_id, chapters, fingerprints)` 完全在读取时按 `analysis_fingerprints` 的当前值 `F` 判定，没有「正文已变更」事件：

- 章摘要：该章最新 `ChapterAnalysis.content_hash != F`（与 `document_export/story.py` 同一判定）；无分析不算过期，另给 `analyzed=false`。
- 剧情记忆：`origin=auto`、`chapter_id` 为该章、有 `analysis_id`，由 `memory_service.memory_source_stale` 判定——记忆页的 `source_stale` 与这里是**同一个函数**，不得另写一套。调用方可传已算好的 `chapter_states` 避免重复读章节。
- 设定变化：`SettingProgression.source="analysis"`、`active`、未撤销、`source_change_id → AnalysisChange → ChapterAnalysis.chapter_id` 为该章、`source_content_hash` 非空且 `!= F`。**空哈希的旧记录不判为过期。**
- 作品级汇总按 200 章一批查询，查询次数不随章节数增长（`test_overview_query_count_does_not_grow_with_chapters`）。
- `stale_count` = 摘要 + 记忆 + **未确认**的设定变化。重新分析后摘要与记忆的提示自然消失；旧分析写入的设定变化若仍保留旧哈希则继续提示，直到被替换、撤销或作者确认。

### 8.3 第二级：评估任务 `consistency.impact`

- 受理、模型绑定、发送守卫、落库重验全部复用检查的骨架（`check.admit_task` / `_accepted_binding` / `retry_binding` / `bind_execution` / `_before_send`），处理器 `ImpactHandler` 继承 `ConsistencyHandler` 的事件与终态核验。`resource_key = consistency:{project_id}`，与检查互斥（`check.require_free`）。同样登记 `TASK_KINDS`、`TaskKind`、`_COLLABORATION_QUEUED_CLEANUP_KINDS`、`_DRAFT_ONLY_KINDS`、管理员监控的种类标签与安全阶段（`抽取本章事实`、`判定事实变化`、`查找受影响章节`、`保存评估结果`）。
- **`TaskSpec.chapter_id` 恒为 None**，章节记在 `input.chapter_id` 与 `input.request.chapter_id`。不填 `Task.chapter_id` 的原因：共用骨架与执行身份把「非单章种类的 `chapter_id` 为空」当作不变式（`check.retry_binding` / `bind_execution`），该列外键是 `SET NULL`、又是执行身份的一部分（删章会改写运行中任务的身份），发送守卫与引擎核心不在本域可改范围内。任务面板的跳转改带任务 id，见 8.6。
- 改动前事实的取法（`impact.assess_block` 与任务共用）：台账过期 → 现有 `facts` 即改动前，调用一次 `consistency.extract` 重抽并 `save_ledger`（旧值进 `prev_*`）；台账新鲜且有 `prev_facts` → 不调用抽取，`prev_facts` 即改动前；台账不存在 → `no_baseline`；台账新鲜且无上一版 → `unchanged`；正文为空 → `empty`。
- 差异：先按 `(category, subject_key, NFKC 去空白 casefold 的 statement)` 求集合差（`literal_diff`），两边都为空 → 未发现变化、不再调用模型。否则调用一次 `consistency.impact`，**只发送两边字面不同的事实陈述**（编号 `A*` 改动前、`B*` 改动后；不含引句与正文）。最多两次模型调用。
- 输出校验（`parse_changes`）：只接受本次输入给出的编号，每个编号至多用一次，`kind` 与编号必须匹配（`added` 只引 `B*`、`removed` 只引 `A*`、`changed` 两者都引）；非法条目丢弃并记 warning。不是 JSON、没有 `changes` 列表或空输出时任务 failed——**畸形输出不能当成「没有变化」**。
- 受影响章节（零模型调用，`find_affected`）：章号大于本章的非空章节中，**新鲜**台账里 `subject_key` 命中变化主体的事实按章聚合；证据最多保存 `MAX_IMPACT_EVIDENCE = 5` 条并记 `fact_count`。台账缺失或过期的后续章节进入 `uncovered`，**不静默忽略**。变化的事实都没有主体键时记 warning、不列章节。
- 落库（`_save_impact`）：`require_task_write` 持作品行锁直到提交，与作者的确认 CAS 串行；整体覆盖本章记录并递增 `revision`。同一后续章节仍在清单且其证据的 `text_hash` 集合与上次相同时继承 `reviewed` / `dismissed`（`inherit_statuses`）。
- 评估期间本章被改：抽取阶段由 `save_ledger` 锁内重验，指纹不符即 failed `章节正文在评估期间发生变化，请重新评估`，台账与影响记录都不写。台账已落库之后（判定、查找期间）再改：结果按**评估时**的指纹保存，读取即 `outdated`，不冒充当前正文的结论。
- 失败与重试：判定失败时已重抽的台账保留，重试（`validate_retry` 沿用原章节与模型绑定）直接比较上一版、不再调用抽取。没有运行记录，`on_abort` 无需还原任何数据。
- **评估全程不写 `consistency_issues`**，也不写正文、摘要、记忆、设定、生成记录；模型原文不进入任务输出。`done` 载荷 `{chapter_id, extracted, no_change, changes, items, uncovered, warnings}`。

### 8.4 接口

| 接口 | 能力 | 合同 |
|---|---|---|
| `GET /impacts` | `content.read` | 有过期派生内容或有评估记录的章节：各类计数、`ledger_state`、评估概要（含 `task_id` = 最近一次评估的任务）、进行中的评估。**要读全书正文计算指纹，不用于轮询** |
| `GET /impacts/running` | `content.read` | `{running_task_id, running_task_key, running_chapter_id}`，只查任务表；评估进行中的轮询入口。`running_task_key` 只给发起者本人。路由须注册在 `/impacts/{chapter_id}` 之前 |
| `GET /impacts/{chapter_id}` | `content.read` | 第一级三类明细、`can_assess` / `assess_block`、`assessment`（含 `outdated`、影响项 `changed`、当前章号）、`revision`（0 = 尚无记录） |
| `POST /impacts/{chapter_id}/assess` | `ai.use` | `{ai_config_id?}` → 202 `TaskOut` |
| `PATCH /impacts/{chapter_id}` | `content.edit` | `{revision, items?: [{chapter_id, status}], acknowledged?: [id]}`，至少一项、拒绝 null 与未知字段 |

- `PATCH.items` 只改列出的影响项。`PATCH.acknowledged` 是**合并语义**：请求给出「本章当前仍过期的设定变化」中已确认的 id，服务端只替换这一部分；此前确认过、现已不再过期的 id 原样保留（正文改回再改时不必重新确认），已不存在的 id 顺带清理。请求中的 id 必须是本章分析写入的设定变化。
- 尚无记录时 `revision` 传 0，只能保存 `acknowledged`（创建未评估的行）。

### 8.5 出站边界

抽取与检查相同（只发本章正文与开启 AI 可见的作者区块）。判定模板只收到两组事实陈述（类别、主体、陈述、时间线索），事实本身只来自 AI 可见正文；不含引句、正文、结构化设定与关闭 AI 可见的区块。提示词覆盖按真实操作者读取。

### 8.6 前端

- 「修改影响」页签与所选章节记在地址栏（`?tab=impact&chapter=`）。任务面板的 `taskLink` 对评估任务给 `?tab=impact&task={任务 id}`（任务本身不带章节）；页面用 `impactTaskChapter` 在进行中的评估或概览的 `task_id` 中找到章节后打开，并从地址栏去掉该参数；找不到（未落库即失败、已被新的评估覆盖、任务已清理）就停在列表。
- 评估进行中轮询 `GET /impacts/running`，任务结束后才重新读取概览与打开中的详情。不要改回轮询概览，也不要为概览加会产生陈旧结果的缓存。
- 确认状态的本地勾选（`ImpactEdits`）叠加在服务端详情之上；409 `stale_revision` 保留勾选，刷新后只替换详情。`impactUpdateBody` 只提交改过的影响项与当前仍过期的设定变化中已确认的 id。
- 显隐经 `impactPermissions`：确认需 `canEdit`，评估与发起检查需 `canUseAI`，取消只对发起者。
- 章节编辑页的提示（`useChapterImpactHint`）在进入页面、内容版本变化、最新分析变化时读取单章接口；只读取、失败静默，不参与保存、草稿与离页拦截。
- 提示显示在编辑卡片页头的标题区。页头靠 `ChapterEditorPage.css` 的 `chapter-editor-card` 允许换行并给标题区最小宽度 `min(360px, 100%)`；宽度达到 `TOOLBAR_IN_HEADER_MIN_WIDTH` 时工具栏进页头，按钮一多就会把不换行的标题区挤到 0 宽，提示与章号随之不可见（2026-11-18 浏览器验收 A-2，1280 宽复现）。给工具栏加按钮后在 1280 宽下看一眼标题区。
- `uncovered` 提供「对这些章节运行一致性检查」，预填现存章节的起止范围。

### 8.7 校验与错误矩阵（影响追踪）

| 条件 | 结果 |
|---|---|
| 非协作者、待接受、已撤权、回收站作品；章节不存在或属于别的作品 | 404 |
| 查看者评估或确认 | 403 `project_readonly`，不建任务、不调用模型 |
| 没有台账 / 台账新鲜且无上一版 / 正文为空 | 400 `本章还没有改动前的事实台账，请先运行一次一致性检查` / `本章自上次抽取事实后没有改动，暂无可评估的变化` / `章节正文为空，无法评估影响` |
| 同作品有进行中的检查或评估 | 409 对应提示 |
| `revision` 过期（含评估任务刚覆盖了记录、并发创建） | 409 `code=stale_revision`，前端保留勾选 |
| `items` 中的章节不在本次评估结果里 | 400 `受影响章节不在本次评估结果中，请刷新后重试` |
| `acknowledged` 含不属于本章分析结果的 id | 400 `要确认的设定变化不属于本章的分析结果` |
| `items` / `acknowledged` 为 null、两者都缺、重复章节、未知字段、非法状态 | 422 |
| 运行中撤权／降级／关模型授权／可见性变化 | 任务 failed `权限或模型授权已变化，已停止发送`，此后不落库（已重抽的台账保留） |

### 8.8 章节生命周期与备份

- 删除本章：台账与影响记录级联删除。删除被列章节：影响项保留，`chapter_no` 为 null、`changed=true`；`uncovered` 中同理。改章号、改标题：读取即当前值。正文写回旧内容（含版本恢复）：一律按指纹重新判定。
- 备份 v13：见 [project-transfer.md](./project-transfer.md)。台账的 `prev_*` 随规则导出；`consistency_impacts` 不导出 `actor_id` / `task_id`（文件里伪造的值不采信），`items[].chapter_id`、`uncovered`、`acknowledged` 重映射，对应不上的整项去掉并告警。v1–v12 补空导入；旧版本夹带影响表或非空 `prev_*` 时拒绝。

### 8.9 已知限制

- 受影响章节只依据后续章节已有的新鲜台账；未抽取或已过期的章节只能列为无法判断。
- 只保留一版 `prev_*` 与每章最近一次评估：连续多次改动后，评估比较的是「最近一次台账」与当前正文，更早的差异不再可得。
- 主体靠 `subject_key` 字面相等匹配，同一主体的不同称呼不会互相命中。
- 测试使用 FakeLLM，不代表真实模型的检出率。

### 8.10 必需测试

- `test_consistency_impact_level1.py`：三类过期与跳转标识、其他章不受影响、重新分析后消失、空哈希与非 active 不报、批量查询、确认的合并语义、`save_ledger` 上一版。
- `test_consistency_impact_task.py`：佩剑示例、只改错别字（零调用与模型判定两种）、台账新鲜不重抽、400 前提、`uncovered`、确认继承与 CAS、`changed` / `outdated`、评估不改问题清单、输出校验、畸形输出失败与重试、抽取期间改章不落库、判定期间改章标记 `outdated`、章节生命周期、互斥、轻量轮询接口、管理员监控阶段、进度流。
- `test_consistency_impact_collaboration.py`（及 `_postgresql`）：查看者只读、非成员 404、编辑者评估与确认、发送中与落库前的权限变化。
- `test_consistency_impact_migration.py`（及 `_postgresql`）、`test_consistency_impact_transfer.py`：0059 结构与受保护降级、v13 往返与重映射、伪造引用、v12 导入、旧版本夹带与非法值拒绝。
- 前端 `utils/consistencyImpact.test.ts`、`api/consistency.test.ts`：深链与任务定位、计数文案、跳转、评估门禁、确认请求体与冲突合并、只读显隐、编辑页提示条件、接口路径。

### 故事日期与原文证据

比对阶段把每条引句与当前活动场景的正文投影核对。按 Unicode 码点核对起止位置、原文及唯一匹配，整句落在同一场景才可采用有效日期。日期保存在问题证据的 `story_time` 快照内，包含场景、来源、日期文字及服务端使用的月序。日期相同的事实共享故事日序，回忆与预告仍须根据引句解读。

日期单独修改后可复用事实台账。合并时在作品写锁内重新核对日期和归属，失效的比对也不能把以前的问题标成「未复现」。问题详情保留旧证据，另外标出发生变化的日期或归属；历史备份缺少日期快照时继续支持原文核对。
