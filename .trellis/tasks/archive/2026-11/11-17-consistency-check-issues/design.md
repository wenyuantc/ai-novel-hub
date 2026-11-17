# 技术设计：一致性检查与问题清单

需求见 prd.md（C1–C13）；现有代码证据见父任务 research/code-inventory.md。行号以实施时代码为准。

## 1. 边界
- 新增领域 `consistency`，与章节分析并列，互不调用对方的写路径。检查只读正文、伏笔记录和自己的表；不写正文、设定、伏笔、剧情记忆、`chapter_analyses`。
- 分层沿用后端规范：`api/v1/consistency.py` → `services/consistency/` → `models/consistency.py`；提示词在 `ai/prompts/consistency.py` 与模板目录；任务处理器 `task_engine/handlers/consistency.py`。
- 事实台账是本任务与子任务 2 的共享契约（第 3 节）。子任务 2 只读台账并调用单章抽取函数，不改表结构的既有列。

## 2. 数据模型（迁移 0058）
三张表，均 `project_id` 外键级联删除。**不存章节号**，章号一律在读取时按 `chapter_id` 联表得到，因此无需登记 `CHAPTER_NO_REFS`（backend/app/services/chapter_numbering.py:73）；若实施中确需存章号，必须登记。

`consistency_ledgers`（每章至多一行，`chapter_id` 唯一、外键级联）
- `source_hash`：抽取时的 `analysis_fingerprints` 值（backend/app/services/rich_document_ai.py:61），用于判断台账是否过期，口径与章节分析一致（含开启 AI 可见的作者区块）。
- `text_hash`：抽取时 `Chapter.content` 的 `matcher.text_hash`（backend/app/services/mentions/matcher.py:69），证据锚点的指纹。
- `facts`：JSON 数组，见第 3 节。`model`、`task_id`（置空删除）、`schema_version`、时间戳。

`consistency_runs`
- `task_id`（置空删除）、`actor_id`、`scope`（`{mode: all|range, start_chapter_id, end_chapter_id, chapter_ids}`）、`status`、`stats`（抽取／复用／跳过／新增／再现／未复现计数与跳过章节列表）、`started_at`、`finished_at`。用于「最近一次检查」概要与问题的首次／最近检出引用。

`consistency_issues`
- `category`（character_history | character_state | item | timeline | foreshadow）、`title`、`description`、`suggestion`、`severity`（high | medium | low）。
- `status`（open | resolved | ignored）、`note`、`status_changed_by`、`status_changed_at`、`revision`（状态与备注的 CAS）。
- `identity_key`：去重身份，见第 5 节；`(project_id, identity_key)` 唯一。
- `evidence`：JSON 数组 `[{chapter_id, fact_id, quote, start, end, text_hash}]`；`start/end` 为 `Chapter.content` 的码点偏移，无法逐字定位时为 null。
- `first_run_id`、`last_seen_run_id`、`last_checked_run_id`（均置空删除）、`reappeared`（布尔）。
- 派生字段不落库：`needs_recheck`（任一证据章节当前 `text_hash` 与存值不同或章节不存在）与 `not_reproduced`（`status=open` 且 `last_checked_run_id` 晚于 `last_seen_run_id`）在读取时计算。

## 3. 事实台账契约
每条事实：`{id, category, subject, subject_key, statement, time_hint, quote, start, end}`。
- `id` 为章内稳定序号字符串；`category` 取 character_history | character_state | item | timeline | foreshadow。
- `subject` 为模型给出的主体原名；`subject_key` 为本地归一化键（去空白、全半角与大小写归一），用于分组，不发往模型以外的用途。归一化是纯函数，独立单测。
- `quote` 为模型返回的原文引句；`start/end` 由服务端在 `Chapter.content` 中逐字查找首个匹配得到，找不到（改写、引自作者区块）则为 null。**不信任模型给出的任何偏移。**
- 台账是否可复用：`source_hash == 当前 analysis_fingerprints`。`schema_version` 不符视为过期。

## 4. 检查任务
任务种类 `consistency.check`，按批量生成的多步模式实现（.trellis/spec/backend/batch-generate.md；handlers/batch.py），不拆成多个串联任务。
- 受理：HTTP 入口锁外做 `ai.use` 短读与模型预检，再进入受控受理；处理器声明 `execution_identity_aware`、`admission_preflight_aware`、`collaboration_execution_ready`，每个出站与落库检查点按原 grant 与模型绑定重验（参照 handlers/analysis.py、collaboration_ai_guard）。不新造授权路径。
- 互斥：`resource_key = consistency:{project_id}`；不设 `serial_key`，不与章节分析、批量生成互锁（只读正文）。
- 输入：`{request, chapter_ids（范围内非空章，按 (chapter_no, id) 排序定稿）, ai_config_id, _project_model}`。需重新抽取章数上限 `MAX_EXTRACT = 300`，超出返回 400 并提示缩小范围；复用的章不计入。
- 断点：`checkpoint = {phase: extract|compare, items: [{chapter_id, state: pending|extracted|reused|skipped_changed|skipped_missing|failed, error}], groups_done, run_id}`，`step_cursor` 为抽取阶段下一章下标。每章台账单独事务提交；中断后最多重做当前章。重试经 `validate_retry` 沿用原 `chapter_ids` 与断点。无审核暂停点。
- 抽取步骤（逐章）：读取章节与当前指纹 → 台账新鲜则 `reused` → 否则调用模型（模板 `consistency.extract`，输入为与章节分析相同的 AI 可见正文渲染，含开启的作者区块，不含结构化设定）→ 规范化、定位引句 → 落库事务内重验章节仍存在且指纹未变，变了则 `skipped_changed` 不保存（C13）。单章失败记 `failed` 并继续，不中断整次检查；失败章不参与本次比对的新鲜性保证，在概要中列出。
- 比对步骤：取参与比对的台账（范围内章节加范围外已有的新鲜台账；范围外过期台账不使用并计入概要），按 `subject_key` 与类别分组，时间线类另成一组；每组按当前章号排序，在上下文预算内分批调用模型（模板 `consistency.compare`）。伏笔组附带 `include_in_context` 为真的伏笔记录及其埋设／回收章号。比对输入同时给出该组相关的既有问题摘要与 id，允许模型以 `existing_issue_id` 指认同一矛盾（做法参照 `ANALYSIS_SCHEMA_WITH_FORESHADOW_REFS`）。
- 输出校验：模型输出经 `extract_json` 与本域规范化；证据必须引用输入中存在的 `chapter_id + fact_id`，否则丢弃该条并记 warning；只保留至少一条证据在范围内且证据跨两个不同章节（或章内与伏笔记录矛盾）的问题。
- 事件与进度：`stage`（`第 3/40 章 · 抽取事实`、`比对 2/9 组`）、`done`（概要）、`error`；抽取占进度 70%，比对 25%，单调且 ≤95。模型原文不进入任务输出。
- 取消／失败：`on_abort` 无需还原业务状态（不占用章节状态）；把 `consistency_runs.status` 置为对应终态。已保存台账保留。
- 不写生成记录、不记 token（与章节分析一致；用量统计属第 17 项）。

## 5. 问题合并规则（C8）
- `identity_key = sha256(category | subject_key | 排序后的证据 chapter_id 集合)`；模型给出有效 `existing_issue_id` 且该问题属于本作品时优先按 id 合并。
- 本次检出且已存在：更新说明、建议、证据与 `last_seen_run_id`；`ignored` 保持；`resolved` → `open` 且 `reappeared=true`；`open` 保持。
- 本次检出且不存在：新建 `open`。
- 已存在的 `open` 问题，其全部证据章节都在本次比对覆盖内但未检出：仅更新 `last_checked_run_id`，由读取端显示「本次未复现」；状态不变。
- 合并在比对阶段末尾单事务完成，持作品行锁；状态 CAS 与之串行，`revision` 只在作者改状态或备注时递增，检查更新证据不递增（避免作者正在编辑的备注被检查顶掉冲突）。

## 6. 接口
```
POST  /projects/{pid}/consistency/checks/preview   {mode, start_chapter_id?, end_chapter_id?} -> {total, to_extract, reusable, empty, warnings}   ai.use
POST  /projects/{pid}/consistency/checks           同上 + ai_config_id?  -> 202 TaskOut                                                ai.use
GET   /projects/{pid}/consistency/summary          -> {last_run, counts_by_status, ledger_coverage{fresh, stale, missing}, running_task_id}  content.read
GET   /projects/{pid}/consistency/issues           ?status&category&chapter_id&needs_recheck&page&page_size -> 分页                     content.read
GET   /projects/{pid}/consistency/issues/{id}      -> 详情（证据含当前章号、标题、stale 标记）                                          content.read
PATCH /projects/{pid}/consistency/issues/{id}      {status?, note?, revision} -> 详情；修订不符 409 code=stale_revision               content.edit
POST  /projects/{pid}/consistency/issues/{id}/recheck -> 202 TaskOut（范围 = 该问题现存证据章节）                                      ai.use
```
- 权限统一走 `project_access.require_project`（backend/app/services/project_access.py:122）：非协作者 404，查看者写操作 403 `project_readonly`。
- `needs_recheck` 筛选需按当前正文指纹计算：列表查询批量取所涉章节的 `text_hash`（可用 `MentionCache` 同款缓存或按 `content_revision` 缓存），不得逐条查库；分页在过滤后进行，不静默截断。
- 错误沿用 `AppError` 层级与 `{"detail": ...}` 响应包。

## 7. 证据跳转
- 复用提及锚点契约：前端用 `mentionHref`（frontend/src/utils/mentions.ts:26）同一组查询参数 `mention_start / mention_end / mention_hash` 跳到阅读器或编辑器，由既有 `resolve-position` 与失效提示处理；不新增第二套锚点参数。
- 无偏移的证据跳到该章开头，并在证据卡片上说明「未能逐字定位」。
- 章节已删除的证据只显示引句与「章节已删除」，不提供跳转。

## 8. 章节生命周期
- 删除章节：台账随外键级联删除；问题证据保留 `chapter_id` 与引句，读取时章节不存在即 `needs_recheck`。不新增删除钩子。
- 改章号、调序：无存储章号，读取即为当前值；比对排序用运行时章号。
- 版本恢复、AI 保存、导入等任何正文写入都会改变指纹，自然使台账过期、问题需重新核查，无需事件总线。

## 9. 备份（v12）
- backend/app/services/project_transfer/registry.py：`consistency_ledgers`、`consistency_issues` 加入 `RULES`；`consistency_runs` 登记到 `NOT_EXPORTED`（任务运行态）。问题的三个 run 引用导出为 null，导入后 `not_reproduced` 自然为假。
- `evidence[].chapter_id` 属 JSON 内 id，在 remap.py 重映射；映射不到的证据保留引句、`chapter_id` 置 null。
- `exporter.VERSION` 升 12、`APP_SCHEMA` 升 `0058`；importer 增加 `_migrate_v11_to_v12`（旧备份补空表）。`test_registry_covers_all_project_tables` 须通过。
- 智能体效果快照 `EFFECT_TABLES`（backend/app/services/agent/effects.py:86）不纳入：智能体不写这三张表。

## 10. 提示词
- 新增模板键 `consistency.extract`、`consistency.compare`，登记到 backend/app/ai/prompts/registry.py，输出 schema 放 formats.py；遵守沙箱模板、锁定变量与 parity 对照测试（.trellis/spec/backend/prompts-styles.md），并更新规范中的模板键数量。是否开放用户覆盖沿用注册表对分析类模板的现有做法。
- 抽取提示要求：只记录正文明确写出的事实，逐条给出原文引句，不推测。比对提示要求：只报告两处事实不能同时成立的情况，每个问题引用具体事实 id，给出修改建议，表述为潜在冲突。

## 11. 前端
- 路由 `projects/:pid/consistency`（frontend/src/router.tsx:106–127 懒加载），页面 `pages/project/ConsistencyPage.tsx`；导航加入 `PROJECT_GROUPS` 的「写作与校验」分组（frontend/src/components/StudioNavigation.tsx:31–54）。
- `src/api/consistency.ts` 与 `types.ts` 镜像后端 schema；组件放 `components/consistency/`：检查概要与台账覆盖、发起检查弹窗（范围选择 + 预览 + 确认）、问题列表与筛选、问题详情抽屉（证据卡片、状态与备注表单）。
- 任务进度复用 `stores/tasks.ts`、`useTaskStream` 与 `TaskPanel`；任务结束后刷新概要与列表。
- 约定：按 `useProjectAccess` 做只读与按钮显隐；切换作品、身份或筛选时丢弃迟到响应（请求代次）；状态冲突保留本地备注草稿并提示刷新；窄屏用卡片布局；深色主题与键盘可达。

## 12. 取舍
- 每章一份当前台账而非保留历史：存储与复用判断简单；子任务 2 需要的「旧台账」由其在重抽前自行留存差异，不在本任务预留历史表。
- 引句由服务端定位而非采用模型偏移：模型偏移不可靠，定位失败可降级；代价是改写式引句无法精确跳转。
- 问题身份用确定性键加模型指认双轨：单靠模型文本去重不稳定，单靠键在证据章节集合变化时会新建问题；双轨后仍可能偶发重复，属可接受范围并在规范中写明。
- 不做自动标记已解决：模型输出不稳定，自动改状态会让作者失去对清单的信任。

## 14. 实施偏差记录（2026-11-17）
- **断点不写 Task**（第 4 节原定 `step_cursor` + `checkpoint`）：通用发送守卫要求运行期间 Task 的断点与领取时逐字一致，只有批量与向导有专门的推进协议；照原设计须改 `collaboration_ai_guard` 的既有分支，超出本任务允许的改动范围。现做法：进度写在 `consistency_runs.stats`（`phase`、`cursor`、跳过／失败章节），与每章台账同事务提交；续跑靠台账新鲜度，重试沿用原 `chapter_ids`，已抽取的章不再调用模型。代价：`TaskOut.checkpoint` 恒为 null；没有 `groups_done`，重试时比对阶段整体重做；逐章状态只保留计数与跳过列表。
- **引擎白名单登记**：`_COLLABORATION_QUEUED_CLEANUP_KINDS` 与 `_DRAFT_ONLY_KINDS` 追加 `consistency.check`。后者语义上是借用（本任务会落库）：终态核验失败时任务改记无结果 failed 并释放互斥，已保存的台账、问题与运行记录保留。
- **新增 `mode: "issue"`**：单问题重新核查的范围写入运行记录的 `scope`。
- **整体失败口径**：需要抽取的章节全部失败，或比对批次全部失败时任务记 failed；其余单章／单批失败只记入概要。
- **分组**：同一主体的人物经历、状态、物品事实合并为一组比对；「本次未复现」另要求至少一个证据章节在本次范围内；事实 id 由服务端分配；按 id 指认后 `identity_key` 跟随新证据。
- **降级**：三张表有数据时拒绝降级（沿用 0056／0057 惯例），空表才删。
- **删章守卫**：台账外键登记到 `chapter_delete_guard._REVIEWED_FKS`。
- **智能体**：`start_task` 白名单未加入本种类，智能体不能发起检查。

## 13. 兼容与回滚
- 迁移 0058 只新增表，降级即删表；不改既有表。旧作品无数据，页面显示「尚未检查」。
- 功能不依赖部署开关；回滚代码后新表数据闲置，备份 v12 文件需要新版本才能导入（与历次升版一致）。
