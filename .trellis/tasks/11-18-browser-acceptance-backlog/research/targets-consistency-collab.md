# Research: 浏览器验收对象调研（一致性／修改影响、协作两条路径、智能体前端）

- **Query**: TODOS 第 19 项前四条验收对象的入口、接口、可见状态、现有自动化覆盖与建议验收场景
- **Scope**: internal
- **Date**: 2026-11-18

来源条目：`TODOS.md:249-259`（第 19 项）、`TODOS.md:171-184`（第 14 项）、`TODOS.md:132-142`（第 11 项）。
本文只描述现状，未改任何产品代码，未读取 `.env`／凭据类文件。路径均相对仓库根。

---

## A. 修改影响追踪（第 14 项）

### A1. 入口与文件

| 位置 | 文件:行 | 说明 |
|---|---|---|
| 路由 | `frontend/src/router.tsx:131` | `/projects/:pid/consistency` → `ConsistencyPage` |
| 侧栏 | `frontend/src/components/StudioNavigation.tsx:55` | 「一致性检查」（验收记录中位于「写作与校验」分组） |
| 页签 | `frontend/src/pages/project/ConsistencyPage.tsx:233-236` | 「潜在冲突」／「修改影响」，页签记在地址栏 `?tab=impact` |
| 深链 | `frontend/src/utils/consistencyImpact.ts:17-67` | `?tab=impact&chapter=<id>` 打开单章抽屉；`&task=<id>` 由任务面板带入，定位后从地址栏去掉 |
| 页签主体 | `frontend/src/components/consistency/ImpactPanel.tsx:215-268` | 说明、进度、章节列表、抽屉 |
| 单章抽屉 | `frontend/src/components/consistency/ImpactDrawer.tsx:174-266` | 三段：可能过期的派生内容／评估影响／评估结果 |
| 编辑页提示 | `frontend/src/pages/project/ChapterEditorPage.tsx:245`、`:1120-1121`；`frontend/src/hooks/useChapterImpactHint.ts:9-21` | 页头金色 Tag，点击跳 `impactHref(pid, chapter.id)` |
| 任务面板 | `frontend/src/api/tasks.ts:166`、`:218-220`；`frontend/src/components/TaskPanel.tsx:319` | 种类标签「修改影响评估」，跳转到影响页签 |
| 任务跟随 | `frontend/src/components/consistency/useRunningTaskFollow.ts:34-156` | 订阅进度流＋3 秒轮询兜底（`CHECK_POLL_MS`，`frontend/src/utils/consistency.ts:13`） |

后端接口（`backend/app/api/v1/consistency.py`）：

| 接口 | 行 | 权限（文件头注释 `:4`） |
|---|---|---|
| `GET /projects/{pid}/consistency/impacts` | 93-95 | content.read |
| `GET .../impacts/running`（评估中轮询的轻量接口） | 98-101 | content.read |
| `GET .../impacts/{chapter_id}` | 104-106 | content.read |
| `POST .../impacts/{chapter_id}/assess` → 202 任务 | 109-117 | ai.use |
| `PATCH .../impacts/{chapter_id}`（`revision` CAS） | 120-124 | content.edit |

实现：`backend/app/services/consistency/impact.py`（第一级判定、`assess_block` 在 `:140-155`）、`impact_task.py`（评估任务）。设计文档：`.trellis/tasks/archive/2026-11/11-17-edit-impact-tracking/design.md:9-49`。规范：`.trellis/spec/backend/consistency-check.md`。

### A2. 用户可见的状态与文案

章节列表行（`ImpactPanel.tsx:231-257`，文案来自 `consistencyImpact.ts`）：

- 过期标签（`:99-105`）：「章摘要已过期」「剧情记忆 N 条已过期」「设定变化 N 条待确认」；都没有时显示「派生内容无过期」（`ImpactPanel.tsx:243`）。
- 台账状态（`:71-75`）：「事实台账最新」「事实台账已过期」「尚未抽取事实」。
- 最近评估结论（`:92-96`）：「未评估」「未发现剧情事实变化」「N 处事实变化」。
- 附注（`:108-116`）：「评估后正文又有修改」「可能受影响 N 章，待复核 M 项」／「…均已处理」「N 个受影响章节在评估后已修改」「N 章无法判断」。
- 空列表：「目前没有派生内容过期的章节，也没有评估记录」（`ImpactPanel.tsx:228-229`）。
- 评估中：行上「评估中」Tag（`:245`），列表上方「查看正在评估的章节（第 N 章…）」（`:221-223`）。

单章抽屉（`ImpactDrawer.tsx`）：

- 标题「修改影响 · 第 N 章 · 标题」（`:174`），顶部台账 Tag、「尚未分析」Tag、「打开本章」（`:185-187`）。
- 第一级「可能过期的派生内容（N 项）」（`:193-237`）：
  - 章摘要卡：「摘要来自 <时间> 的分析，此后正文已修改。」＋「打开本章分析」（仅可编辑者，链接 `chapters/{id}?panel=analysis`，`consistencyImpact.ts:141-143`）。
  - 剧情记忆卡：逐条列出类型 Tag 与内容，「到剧情记忆页查看」（只到记忆页，无逐条定位，`consistencyImpact.ts:145-148`）。
  - 设定变化卡：类型 Tag、主体名、「查看设定变化」（跳转规则 `consistencyImpact.ts:161-181`），可编辑者有复选框「已确认无需更新」，只读者只在已确认时显示同名 Tag（`:228-230`）。
  - 无过期：「没有过期的派生内容。」／未分析：「本章尚未分析，没有由分析得到的摘要、记忆或设定变化。」（`:200`）。
- 第二级「评估对后续章节的影响」（`:239-255`）：按钮「评估影响」／已评估后为「重新评估」，Popconfirm「评估本章的修改影响？最多调用两次模型（使用默认文本模型）。」；无 AI 能力显示「当前权限不能发起评估。」。
- 不能评估的原因（`consistencyImpact.ts:185-215`）：
  - `no_baseline`：「本章还没有改动前的事实台账，请先运行一次一致性检查；之后再修改本章才能评估影响。」并带「运行一致性检查」按钮（预填本章范围，`ImpactDrawer.tsx:246-247`）。
  - `unchanged`：「本章自上次抽取事实后没有改动，暂无可评估的变化。」
  - `empty`：「章节正文为空，无法评估影响。」
  - 互斥：「已有进行中的一致性检查，结束后才能评估影响。」／「已有进行中的修改影响评估，请等待其完成。」；无模型：「尚未配置可用的文本模型。」
- 评估结果（`:57-116`）：「评估于 <时间> · 模型 <名>」；`outdated` →「评估后正文又有修改」警告；`no_change` →「未发现剧情事实变化」成功提示；否则「事实变化（N 处）」卡片（新增／消失／改变 Tag、类别、主体、改动前／改动后）与「可能受影响的后续章节（N 章，待复核 M 项）」。
- 受影响章节卡（`:17-55`）：状态 Tag「待复核／已复核／无需处理」（`consistencyImpact.ts:77-81`），引句、`在阅读器中定位`／`在编辑器中定位`，单选组逐项确认；被列章节之后改过 →「该章在评估后已修改，下面的原文与位置可能已失效」，被删 →「该章已被删除」。
- 「无法判断」：`有 N 章尚未抽取或已过期，无法判断`，列出章节，说明「这些章节在评估时没有可用的事实台账，是否受影响未知…」，按钮「对这些章节运行一致性检查」（`:104-114`）。
- 底部「保存确认」按钮与「有尚未保存的确认／确认状态已保存」（`:177-180`）；冲突时「确认状态已被其他人或一次新的评估更新…你的勾选仍保留在页面上」（`:189-191`）；有未保存勾选时关闭／切章弹「放弃未保存的确认？」（`ImpactPanel.tsx:106-108`）。
- 只读：「当前为只读权限，不能修改确认状态。」（`:264`）。
- 任务结束提示（`consistencyImpact.ts:318-323`）：「修改影响评估已完成」「评估已取消，正文与设定未受影响」「修改影响评估未完成」。发起成功「已开始评估」（`ImpactPanel.tsx:191`）。
- 编辑页提示：`本章改动后有 N 项派生内容可能过期`（`consistencyImpact.ts:310-313`），title「在一致性检查的「修改影响」页签查看并处理」；在进入页面、`content_revision` 变化、最新分析 id 变化时重读（`ChapterEditorPage.tsx:245`），读取失败静默。

### A3. 现有自动化覆盖

前端（纯函数与 API 合同，没有组件挂载测试）：

- `frontend/src/utils/consistencyImpact.test.ts:30-231`：页签与深链、任务 id→章节、列表行结论与计数、派生内容跳转、评估按钮禁用原因、确认请求体与冲突合并、只读显隐、快捷检查范围、编辑页提示显示条件、任务结束提示。
- `frontend/src/api/consistency.test.ts:54-82`：概览、单章、评估、确认写入的路径与请求体；评估中轮询轻量接口；任务面板标签与跳转。

后端：

- `backend/tests/test_consistency_impact_level1.py:36-202`：改章后三类过期直到重新分析、`acknowledged` 合并、空哈希旧记录不报、概览查询数不随章数增长、上一版台账保留规则。
- `backend/tests/test_consistency_impact_task.py:112-444`：佩剑示例第 12 章进清单带证据、改错别字无变化、非法判定引用丢弃、畸形输出失败与重试、抽取期间改章不落库、判定期间改章存为 outdated、章节改号／删除／恢复、评估与检查互斥、进度流事件。
- `backend/tests/test_consistency_impact_collaboration.py:75-167`：查看者只读、非成员 404、编辑者评估并确认、发送中／落库前权限变化不保存。
- `backend/tests/test_consistency_impact_transfer.py:56-189`、`test_consistency_impact_migration.py:15-35`：备份 v13 往返与迁移。

归档任务自述的未验收范围：`.trellis/tasks/archive/2026-11/11-17-edit-impact-tracking/check-report.md:10`、`:13`（影响页签、评估流程、确认操作、编辑页过期提示、任务面板跳转；检查页改用共用任务跟随 Hook 后未在浏览器验证）。

### A4. 建议的浏览器验收场景（12 个）

前置：隔离库＋本地假模型；作品 15 章左右，第 5 章「佩剑被毁」相关、第 12 章「拔出佩剑」；第 5 章已做章节分析并产生剧情记忆与设定变化；已跑过一次全书一致性检查。

| # | 步骤 | 预期可见结果 |
|---|---|---|
| A-1 | 未改任何章节时进入「修改影响」页签 | 空状态文案「目前没有派生内容过期的章节，也没有评估记录」 |
| A-2 | 在编辑器修改第 5 章并保存 | 页头出现「本章改动后有 N 项派生内容可能过期」Tag；点击进入影响页签并自动打开第 5 章抽屉（地址栏含 `tab=impact&chapter=`） |
| A-3 | 查看列表与抽屉第一级 | 行上三类过期标签与「事实台账已过期」；抽屉列出章摘要、剧情记忆、设定变化三张卡；其他章节不出现在列表 |
| A-4 | 依次点「打开本章分析」「到剧情记忆页查看」「查看设定变化」 | 分别到达章节分析抽屉、记忆页、对应设定详情 |
| A-5 | 勾选一条「已确认无需更新」→「保存确认」→ 刷新 | 提示「已保存」，刷新后勾选保持，列表「设定变化 N 条待确认」计数减少 |
| A-6 | 点「评估影响」并确认 | 「已开始评估」，出现进度与「评估中」Tag；完成后「修改影响评估已完成」，结果显示「事实变化（N 处）」，第 12 章出现在受影响清单并带引句 |
| A-7 | 受影响卡片点「在阅读器中定位」「在编辑器中定位」 | 到达第 12 章并高亮引句 |
| A-8 | 将第 12 章设为「已复核」保存并刷新；再到「潜在冲突」页签 | 状态保持，标题「待复核 0 项」；潜在冲突清单的问题状态未被改变 |
| A-9 | 修改第 12 章后回到抽屉；再修改第 5 章 | 第 12 章卡片出现「该章在评估后已修改…」；第 5 章出现「评估后正文又有修改」，按钮为「重新评估」 |
| A-10 | 只改第 N 章错别字后评估 | 「未发现剧情事实变化」，不列后续章节 |
| A-11 | 让某后续章节台账过期或未抽取后评估 | 「有 N 章尚未抽取或已过期，无法判断」及章节名；点「对这些章节运行一致性检查」打开预填范围的发起弹窗 |
| A-12 | 对从未检查过的章节改稿后打开抽屉；另以查看者、编辑者打开 | 该章只显示第一级与 `no_baseline` 文案＋「运行一致性检查」；查看者无复选框／单选／评估按钮并显示只读文案；编辑者可评估与确认 |

可并入的附加观察：重新分析第 5 章后摘要与记忆提示消失（AC1）；两个标签页先后保存触发冲突提示且勾选保留；检查进行中评估按钮禁用文案；任务面板「修改影响评估」跳转定位章节；评估中取消。

---

## B. 一致性检查「浏览器验收后修改的 3 个缺陷」

三个缺陷均已查实，来源是 `.trellis/tasks/archive/2026-11/11-17-consistency-check-issues/browser-verification.md:34-54`，修改情况汇总于同目录 `check-report.md:16-20`。

修复没有独立提交：全部包含在 `7899de3`（2026-11-17 17:57，`feat(consistency): 全书一致性检查与修改影响追踪`）中，提交说明里只点名了第 1 个（「修复提及高亮在焦点与设定名重叠时超出范围」）。工作日志 `.trellis/workspace/aiNovel/journal-1.md:1581-1608`（Session 48）只有任务级摘要，没有逐个缺陷的记录。

| # | 缺陷 | 原始复现（`browser-verification.md`） | 修改位置（现代码） | 现有测试 |
|---|---|---|---|---|
| 1（中） | 引句内含设定名称时，证据高亮超出引句范围，一直标到段末 | `:38-40`：作品里有角色「林远」；问题证据引句「林远的佩剑在山谷中被巨石砸断，彻底毁了。」；从问题详情点「在阅读器中定位」或「在编辑器中定位」；实际把引句外的「他只得赤手下山。」也标亮，第 12 章多标「守军士气大振。」 | `frontend/src/utils/mentions.ts` 的 `mentionSegments`（提交 diff 中 `:40-60` 一带）：片段在焦点边界处切开，高亮严格等于焦点与窗口的交集 | `frontend/src/utils/mentions.test.ts:26-65`（5 例）。修复后未在浏览器复验 |
| 2（高） | 模型响应较慢时，编辑者（协作者）的检查进度流被服务端中断，页面一直停在「进行中」 | `:44-48`：编辑者（无本人模型、用作者授权模型）发起全书检查，需重抽 5 章，假模型每次抽取延迟 2–3 秒；页面停在「一致性检查进行中 · 生成中／第 1/14 章 · 0%」，无完成提示，概要与清单不刷新，「发起检查」保持禁用，手动刷新才恢复；只在 SQLite 上观察到 | 后端：`backend/app/services/consistency/check.py:75`（`SQLITE_SEND_GAP = 0.25`）、`:673-687`（协作者任务在 SQLite 上每次出站前让出库锁）。前端：`frontend/src/components/consistency/useRunningTaskFollow.ts:80-115`（订阅期间保留轮询、核对真实终态、轮询发现结束），`frontend/src/utils/consistency.ts:13`、`:236`（`CHECK_POLL_MS`、`streamWatchAction`） | `backend/tests/test_consistency_stream_slow_model.py:53`、`:88`；`frontend/src/utils/consistency.test.ts:209` 起（`streamWatchAction` 纯函数）。属缓解而非根治：`check-report.md:25` 写明单次响应头超过 5 秒时进度流仍会中断，页面靠轮询恢复 |
| 3（低） | 键盘保存后焦点丢失到 `BODY`，Esc 关不掉抽屉；抽屉关闭后焦点不回到问题条目 | `:52-54`：仅用键盘打开问题详情，改状态，Tab 到「保存」回车 | `frontend/src/components/consistency/IssueDrawer.tsx:56-68`（保存结束后把焦点放回状态单选）、`:73`（`afterOpenChange` → `onClosed`）；`frontend/src/pages/project/ConsistencyPage.tsx:99-104`、`:261`（关闭后焦点还给 `data-consistency-issue` 条目）；`frontend/src/components/consistency/IssueList.tsx:79` | 无组件测试（`check-report.md:20`：只过 lint／构建） |

同一提交里还改了「其他观察」中的一条（非三缺陷之一）：位置失效提示在一致性来源下改为「原文已变化，旧位置已失效，请回到来源页面重新定位。」（`frontend/src/utils/mentions.ts` 的 `staleAnchorMessage`、`MENTION_FROM_PARAM`；`frontend/src/utils/consistency.ts:68` 追加 `mention_from=consistency`；测试 `mentions.test.ts:73`）。

`7899de3` 之后触及相关文件的提交只有 `708aa7f`（全书搜索工作台，范围含 `mentions.ts` 等）；它对上述三处修复逻辑的具体影响 **未查实**（未逐行比对该提交 diff）。

### 建议的复验场景（4 个）

| # | 步骤 | 预期可见结果 |
|---|---|---|
| B-1 | 建角色「林远」；跑检查得到佩剑问题；从详情分别点两处证据的「在阅读器中定位」「在编辑器中定位」 | 高亮恰好覆盖引句，「他只得赤手下山。」「守军士气大振。」不被标亮；引句内的「林远」仍可点击 |
| B-2 | 构造引句不以设定名开头的证据（原报告 `:40` 注明此情形未单独构造） | 高亮不向前溢出 |
| B-3 | SQLite；编辑者（无本人模型＋作者授权）发起全书检查，假模型每次抽取延迟 2–3 秒；再试一次延迟超过 5 秒 | 2–3 秒时进度正常推进并出现完成提示；超过 5 秒时进度流可能中断，但页面在轮询周期内自行恢复：概要与清单刷新、「发起检查」恢复可用，无需手动刷新 |
| B-4 | 仅用键盘：Tab 到问题→回车→方向键改状态→Tab 到「保存」回车→Esc | 保存后焦点落在抽屉内的状态单选上；Esc 直接关闭抽屉；关闭后焦点回到该问题条目；控制台无 `Blocked aria-hidden` 警告 |

---

## C. 作品协作的两条路径（第 11 项）

未验收的原始记录：`.trellis/tasks/archive/2026-11/11-09-novel-collaborators/implement.md:87`（G5 未勾）、`:1113`、`:2314`、`:2375`。

### C1. 系列设定协作者

入口：

| 位置 | 文件:行 | 说明 |
|---|---|---|
| 路由 | `frontend/src/router.tsx:47-48` | `/series`、`/series/:sid` |
| 系列列表 | `frontend/src/pages/SeriesPage.tsx:42-48` | 非作者的系列带「设定协作者」Tag |
| 系列详情提示 | `SeriesPage.tsx:77` | 非作者：「设定协作者可维护共享基础；作品访问权限由作者单独授予。」 |
| 成员作品卡 | `SeriesPage.tsx:85-103` | 重命名／加入作品／删除空系列／退出系列只对作者渲染；非作者且有不可见成员书时空状态为「没有可查看的成员作品」 |
| 邀请面板 | `SeriesPage.tsx:107-109`；`frontend/src/components/collaboration/MemberPanel.tsx:104-131` | 标题「系列设定协作者」，只有用户名输入（无角色选择、无模型授权复选框），提示「系列协作者只能编辑共享设定，不因此获得系列内作品的访问权。」 |
| 受邀处理 | `frontend/src/pages/ProjectListPage.tsx:72`；`frontend/src/components/collaboration/Invitations.tsx:19` | 书架上的待处理邀请，系列邀请显示「设定协作者」 |
| 共享库 | `frontend/src/components/series/SeriesLibrary.tsx:79-80`、`:104`、`:115-119` | 新建共享角色／世界观、编辑共享基础、收回指定作品、删除；非作者对已采用条目删除按钮禁用并显示「已被作品采用，请系列作者处理删除。」 |

权限规则：

- 需求：`.trellis/tasks/archive/2026-11/11-09-novel-collaborators/prd.md:51-56`；用户文档：`README.md:501-507`。
- 设定协作者可读取、新建、编辑共享条目；只能删除未被任何成员书采用的条目（前端判定 `frontend/src/components/series/seriesState.ts:32-34`，按服务端 `has_uses`；采用情况标签 `:36-41` 对非作者只显示「已有作品采用／尚未被作品采用／采用情况未确认」，不显示本数）。
- 改名、删除、成员书加入／退出、协作者管理仅系列作者（`backend/app/services/series_service.py:194-199`：`can_manage_collaborators=is_owner`）。
- 提升、采用、取消采用、移回及影响预览需要系列权限＋所有受影响作品的作者或编辑者身份；管理员权限不能替代（`README.md:506`；前端可选目标 `seriesState.ts:51-59`）。
- 系列权限与作品权限互不替代：只有系列权限者看不到成员书正文，系列页只列其可读作品；只有单书权限者看不到该书所属系列信息（`README.md:507`）。
- 系列授权版本用于淘汰旧请求：`frontend/src/components/series/seriesAuthorization.ts:2-28`。
- 后端「删除已采用条目返回冲突」的具体代码行 **未查实**（只确认了 README 与前端判定）。

后端测试：`backend/tests/test_collaboration_series.py:28-378`（只读取授权作品并可编辑共享基础、归属变更需书籍编辑权、回收站采用与管理员不能绕过、隐藏书冲突不泄露依赖等）、`test_collaboration_series_read_race.py:17-308`、`test_series_authorization_version.py:24-160`。前端：`frontend/src/pages/SeriesPage.test.ts`、`frontend/src/components/series/*.test.ts`、`frontend/src/components/collaboration/collaborationUI.test.ts`。

### C2. 管理员兼协作者

入口与界面分支：

- 顶层导航返回目标：`frontend/src/components/AppLayout.tsx:43-45`（`administeringOther = managingOther && !isCollaborator`；协作者含管理员兼协作者回个人书架，纯管理员回作品管理）。
- 作品内：`frontend/src/pages/project/ProjectLayout.tsx:55`（代管提示 `ProjectManagementNotice` 只对纯管理员显示）、`:56`（不可编辑时协作者显示「当前为查看者，不能修改此作品」，纯管理员显示「当前为只读内容管理，不能修改此作品」）、`:51`（「协作者」按钮仅作者）、`:52` 与 `:62`（「项目智能体」按 `canViewAgent`）。
- 前端能力解析：`frontend/src/utils/projectAccess.ts:37-57`（`canUseAgent` 要求 `can_use_ai` 且来源为作者或协作者；`canViewAgent` 含查看者与管理员兼查看者，纯管理员不显示）。

权限规则（哪种身份生效）：

- 后端单点：`backend/app/services/project_access.py:42-67`。
  - `role` 取值优先级：作者 > 已接受的协作角色 > admin（`:54`），所以管理员兼协作者的 `role` 是 `viewer`／`editor`，`is_collaborator=true`。
  - 人工读写取并集：`can_read = owner or accepted or admin_read`，`can_edit_content = owner or editor or admin_edit`（`:63-64`），`can_recycle = owner or admin_recycle`（`:65`）。
  - 不因管理员扩大：`can_export = owner or accepted`、`can_use_ai = owner or editor`、`can_manage_collaborators／can_backup／can_write_reading_progress = owner`（`:64-66`）。
  - 管理员能力来自 RBAC 权限 `projects.read`／`projects.edit`／`projects.recycle`（`:50-52`）。
- 文档：`prd.md:31`、`:37`、`:49`；`README.md:497`（管理员兼编辑者凭编辑者身份使用 AI；协作撤销后只保留管理员原本的人工操作）。
- 智能体：管理员人工来源不放行智能体写入，见 `implement.md:1509`、`:1779`（9 身份矩阵含管理员兼 viewer／编辑者）。

### C3. 2026-11-10 早期快照上抽查过什么

`.trellis/tasks/archive/2026-11/11-09-novel-collaborators/research/phase-g-integration.md:56-60`：本机临时 SQLite、单独测试账号、当次构建的前端，真实浏览器抽查到：

- 作者能看到成员面板和三名成员；
- 查看者书架标明角色、作品表单只读且无 AI／备份入口，能阅读已完成章节，作者阅读进度保持 `{}`；
- 编辑者可保存作品信息；
- **管理员兼查看者**可按管理员人工权限编辑，但页面没有 AI 入口；
- **系列编辑者**可见系列和已采用条目，已绑定条目的删除按钮禁用；仅有作品查看权限的成员书架没有系列身份。

同处注明未检查：邀请界面完整交互、撤权在途提示、移动布局、最终 AI／索引链；「不替代代码最终稳定后的浏览器角色矩阵和跨层联调」。

### C4. 之后哪些代码变了

协作功能整体在一个提交 `4ecf119`（2026-11-15 19:44）落库，git 历史无法还原 2026-11-10 快照与最终代码的逐文件差异（**未查实**）。能从实施记录确认的后续变化：

- 2026-11-10 快照时 `canUseAI` 是「仅作者」的临时门禁（`research/phase-g-access.md:8`、`phase-g-integration.md:19`）；最终代码按服务端 `can_use_ai` 对编辑者开放（`frontend/src/utils/projectAccess.ts:45-46`），对应 E4-f～E4-g3c、E5-g、E6-e（`implement.md:196-521`、`:1051`、`:1337`）。
- 智能体对协作者开放并新增 `canViewAgent`：管理员兼查看者现在能看到「项目智能体」（只读本人会话），与 11-10 抽查时「页面没有 AI 入口」的观察不同（`implement.md:2024-2026`、`:2049`、`:2076`）。
- `MemberPanel` 修改／撤销弹窗增加「会立即中止其进行中的 AI 任务（包括智能体对话）」提示（`implement.md:2039`）。
- 协作者前台权限快照轮询：`ProjectLayout.tsx:37-38`（来源 `implement.md:1149` 一带）。
- README 把面板名从「系列成员」改为界面实际标题「系列设定协作者」（`implement.md:2376`）。
- `4ecf119` 之后：`dfd50bc`（2026-11-17，设定缩略图）改了 `backend/app/services/series_service.py`、`frontend/src/components/series/SeriesEntryEditor.tsx`、`SeriesLibrary.tsx`；`6b14689`（2026-11-18，AI 用量）改了 `frontend/src/components/AppLayout.tsx`。这两次改动对系列协作者／管理员分支的具体影响 **未查实**（只看了文件级统计）。

### C5. 建议的浏览器验收场景（13 个）

系列设定协作者（7 个）：

| # | 步骤 | 预期可见结果 |
|---|---|---|
| C-1 | 系列作者在系列详情底部面板输入准确用户名邀请 | 面板标题「系列设定协作者」，无角色下拉与模型授权复选框；确认弹窗「邀请「X」成为系列设定协作者？」；成员行显示「设定协作者 · 待处理」 |
| C-2 | 受邀人在书架接受邀请后进入「作品系列」 | 邀请卡显示「设定协作者」；系列列表该项带「设定协作者」Tag；详情提示「设定协作者可维护共享基础；作品访问权限由作者单独授予。」 |
| C-3 | 设定协作者查看成员作品卡与页面动作 | 无重命名／加入作品／退出系列／删除空系列，无协作者面板；无书籍权限时显示「没有可查看的成员作品」，不出现无权作品的标题；直接访问成员书地址被拒 |
| C-4 | 设定协作者新建共享角色、编辑共享基础 | 保存成功；作者端与已采用该条目的作品中共享基础随之更新，各书本书记录的剧情变化不变 |
| C-5 | 设定协作者对已采用条目与未采用条目分别尝试删除 | 已采用：删除按钮禁用并显示「已被作品采用，请系列作者处理删除。」，标签为「已有作品采用」而非本数；未采用：可删除 |
| C-6 | 同时是某成员书编辑者的设定协作者执行「收回指定作品」；仅有系列权限者执行同一操作 | 前者只能选到自己有编辑权的书并可完成；后者没有可选作品 |
| C-7 | 只有单书查看权限的用户打开该书与书架；作者撤销系列协作者后对方刷新 | 单书成员看不到系列身份与系列入口内容；被撤销者系列列表不再出现该系列，旧页面操作被拒并提示重新读取 |

管理员兼协作者（6 个）：

| # | 步骤 | 预期可见结果 |
|---|---|---|
| C-8 | 有 `projects.read`＋`projects.edit` 的管理员被邀请为查看者，接受后从书架打开作品 | 书架显示共享作品与角色；作品内不出现代管提示条；能人工编辑并保存作品资料与正文；返回入口回到个人书架而非作品管理 |
| C-9 | 同一账号检查 AI 与作者专属入口 | 无生成／润色／分析等 AI 入口，无备份、无「协作者」按钮；「项目智能体」按钮可见但面板只读（只能查看本人已有会话） |
| C-10 | 只有 `projects.read` 的管理员兼查看者打开作品 | 显示「当前为查看者，不能修改此作品」，表单只读 |
| C-11 | 管理员被邀请为编辑者（分别测有／无作者模型授权） | 可发起 AI 与智能体；无本人模型且有授权时显示作者模型费用提示；任务与会话归本人 |
| C-12 | 作者撤销该管理员的协作身份（对方页面不刷新） | 作品从其共享书架消失；从管理员作品管理进入时出现代管提示条，仍可按管理员权限人工编辑；AI、智能体、导出入口消失，此前协作下的任务输出与私人记录不可见 |
| C-13 | 管理员兼系列设定协作者对成员书执行提升／采用类操作（无该书编辑者身份） | 操作被拒，管理员权限不能替代书籍编辑权 |

---

## D. 智能体前端 E4i-h 之后的改动（第 11 项）

### D1. E4i-h 是什么

- 切片记录：`.trellis/tasks/archive/2026-11/11-09-novel-collaborators/implement.md:2316-2335`（进度）、`:2337-2346`（复核）。日期 2026-11-15，仅前端，起因是当天浏览器验证的发现 1、2（`implement.md:2310-2313`）。
- 没有独立提交：随 `4ecf119`（2026-11-15 19:44，`feat(collab): 作品级协作者邀请`）一并入库。此后 `frontend/src/components/agent/` 与 `frontend/src/utils/agent.ts` 没有新提交（`git log 4ecf119..HEAD` 为空）。
- 改动文件：`frontend/src/components/agent/useAgentConversation.ts`、`frontend/src/components/agent/AgentTimeline.tsx`、`frontend/src/utils/agent.ts`，新增测试 `frontend/src/components/agent/useAgentAction.test.ts`，补 `frontend/src/utils/agent.test.ts`、`frontend/src/components/agent/agentUI.test.ts`；规范 `.trellis/spec/frontend/component-guidelines.md:515`。
- 入口：作品内右上「项目智能体」按钮（`frontend/src/pages/project/ProjectLayout.tsx:52`），或地址栏 `?agent=open`（`:31`）；面板 `AgentDock`（`:62`）。

### D2. 行为 1：发送后清空输入

- 预期：服务端一旦受理发送请求就清空输入框（并清除所选 Skill）；被拒绝或请求失败时保留输入。受理后的会话重读被并发读取取代时不再误提示「作品权限已变化，请重新读取对话。」。
- 代码：`useAgentConversation.ts:134-155`（`runAgentAction` 返回 `{accepted, ok}`；被取代的重读最多再读两次）、`:296-341`（`act` 使用它，`send` 返回 `.accepted`）；`frontend/src/components/agent/AgentConversationView.tsx:58-61`（`agent.send(...)` 为真时 `setInput('')`）。
- 单元测试：`useAgentAction.test.ts:22-89`（9 例：正常；受理后重读被取代；受理后重读失败；受理后作用域失效；卸载致取代；服务端拒绝不重放；撤权拒绝后重读被取代仍交失败处理；受理后重读 404；连续取代放弃）。
- 遗留：原偶发现象的唯一根因未在浏览器确认（`implement.md:2334`、`:2381`），记录建议留意发送后是否出现「作品权限已变化」或网络错误提示。

### D3. 行为 2：已停止的工具步骤显示

- 预期：没有返回结果的工具步骤，只有所属轮次仍未结束（排队／运行／待确认）或正在实时订阅该步骤时显示「处理中」；轮次已结束（授权失效作废、失败、取消、中断）时显示「已停止」，展开说明为「本轮已结束，此步骤没有返回结果。」。有结果的步骤仍为「完成」／「未完成」。作者与协作者路径相同。
- 代码：`frontend/src/utils/agent.ts:123-137`（`isAgentTurnOpen`、`AGENT_TOOL_STOPPED`、`agentToolStepState`）；`frontend/src/components/agent/AgentTimeline.tsx:67`（计算状态）、`:83-89`（`ToolStep` 渲染「标签 · 状态」与说明）。
- 单元测试：`frontend/src/utils/agent.test.ts:98-111`（状态判定、历史步骤带轮次身份）；`frontend/src/components/agent/agentUI.test.ts:173-190`（SSR 时间线：failed／cancelled／无活动轮次 →「修改章节标题 · 已停止」，running／paused／实时步骤 →「处理中」）；`frontend/src/utils/agentCollaborator.test.ts:18-23`（`isAgentTurnOpen`）。
- 未处理的相邻发现：恢复同一邀请轮次的编辑者后，已作废轮次上「重试本轮」重新出现，按后端裁决维持现状（`implement.md:2313`、`:2335`、`:2380`）。

### D4. 建议的浏览器验收场景（5 个）

前置沿用 2026-11-15 的做法（`implement.md:2298`）：SQLite 隔离库、假模型带流式工具调用脚本（首轮发 `search_knowledge`＋`update_chapter_meta`，有工具结果后出正文）、作者／编辑者账号。

| # | 步骤 | 预期可见结果 |
|---|---|---|
| D-1 | 编辑者在新会话发送首条消息，再连续发送 3–5 条（含切到后台再切回、发送后立即切页） | 每次受理后输入框立即清空；不出现「作品权限已变化，请重新读取对话。」或网络错误提示 |
| D-2 | 作者在同样条件下发送 | 输入清空，行为与编辑者一致 |
| D-3 | 构造被拒绝的发送（例如编辑者发送前被作者降为查看者，页面未刷新） | 输入保留不清空，出现权限变化提示并进入只读恢复 |
| D-4 | 编辑者发起带改名提案的轮次停在「待确认」，作者关闭其模型授权（页面不刷新） | 提案显示「已作废」；该工具步骤显示「修改章节标题 · 已停止」，展开为「本轮已结束，此步骤没有返回结果。」，不再残留「处理中」 |
| D-5 | 作者发起轮次：运行中观察步骤，再取消一次、让假模型失败一次 | 运行中／待确认时为「处理中」；取消或失败后无结果步骤变为「已停止」；有结果的步骤为「完成」／「未完成」 |

---

## Related Specs

- `.trellis/spec/backend/consistency-check.md` — 一致性检查与修改影响追踪契约
- `.trellis/spec/frontend/component-guidelines.md:515` — 智能体协作者模式：发送受理语义、工具步骤「已停止」
- `.trellis/spec/frontend/index.md` — 前端规范索引（`7899de3` 中有更新）

## Caveats / Not Found

- B：三个缺陷本身、复现步骤、修改位置均已查实；但没有独立修复提交（并入 `7899de3`），journal 中无逐缺陷记录。`708aa7f` 对这三处修复的影响 **未查实**。
- B-2 对应的「引句不以设定名开头时向前溢出」在原验收中未构造，是否曾真实出现 **未查实**。
- C：2026-11-10 快照对应的确切代码状态无法从 git 还原，「之后变了什么」只能依据实施记录列出，逐文件差异 **未查实**；`dfd50bc`、`6b14689` 对两条路径的具体影响 **未查实**。
- C：后端删除已采用共享条目返回冲突的代码行 **未查实**；纯管理员（非协作者）访问系列页的行为未调研。
- A：纯管理员（非协作者）在「修改影响」页签的显隐未调研；`progressionHref` 各类跳转目标页是否按预期打开「字段与剧情变化」只看了链接生成，未核对目标页。
- D：E4i-h 问题 1 的唯一根因在原记录中即为未确认。
- 建议场景中的预期结果依据代码文案与归档文档推导，未在浏览器执行。
