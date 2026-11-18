# 自动保存与异常草稿恢复：实施计划

方案 A 已完成实施与本地验收。复选框记录执行状态，验收编号见 `prd.md`，结果见 [验收记录](research/verification.md)。

## 1. 进入实施的条件

- [x] 用户评审并批准本轮最终规划摘要，已运行 `task.py start`。
- [x] 已确认工作区及任务归属，已创建 `codex/autosave-draft-recovery`。
- [x] 规划文件及 JSONL 已通过 `task.py validate`，实现代理按当前任务注入上下文并读取适用层规范。

单一任务按下列依赖顺序交付。后端标题契约与前端纯状态可以在契约固定后分别开展，但前端共享控制器／编辑器文件由同一责任代理顺序修改，避免并行互改。存储仓库可在接口冻结后独立实施。主会话负责整合、文档和最终检查。

## 2. 阶段 S1：受保护的标题保存

责任范围：`backend/app/schemas/chapter.py`、`backend/app/api/v1/chapters.py`、`backend/app/services/chapter_service.py`、`backend/app/services/agent/effects.py`，以及相关后端测试。前端接口类型在 S4 接入，先固定四字段及错误码。

- [x] 新建 `editor-title` 严格输入模型及路由，四字段为 `title / expected_title / expected_scene_id / expected_content_revision`，响应沿用 ChapterOut。
- [x] 原章 PATCH 的 HTTP 标题写入要求同样条件；title=null、状态／风格等非标题更新保留原语义。HTTP schema 与内部 Agent 参数模型分离。
- [x] 在原权限与写屏障内核验场景归属、活动状态、内容版本和原标题；排除审计字段中的 guard，保留实际标题变更的规划版本及审计。
- [x] 标题、场景文档与场景元数据保存核验 `X-Editor-Actor-Id` 和 `X-Editor-Actor-Created-At`。两个账号都有权限或数字 ID 被复用时，仍不能以新账号提交原作者的草稿。账号创建时间按服务端微秒精度核对。
- [x] 前置条件先于同值 no-op；不新增数据库列／迁移／备份格式。实际标题变更推进现有 content_revision，覆盖共享 Agent 执行及纯标题 effects 撤销；混合正文提交时去重，不回灌历史修订号。
- [x] 补充 `test_chapter_title_save.py`：标题-only 冲突、相同目标值但旧条件、空原标题、缺条件、类型非法、正文变化、锚点跨章／退场／章 ID 复用、权限、锁内竞争和无副作用拒绝。
- [x] 检索现有全部测试中的 HTTP 标题 PATCH；按测试真实意图补齐读取时的条件或改用受保护入口，缺条件用例保留 422 断言。不得在测试环境关闭 guard，也不得给冲突用例偷偷读取最新版本使其失去意义。
- [x] 回归 Agent 预览／确认／撤销：内部调用仍生效，旧预览遇人工变更仍拒绝，不能只用 manual 参数识别 HTTP。
- [x] 用受控延迟复现标题 A→B→A 与旧 S 迟到，证明单调版本拒绝旧写；覆盖纯标题撤销、混合 title+content、no-op 不消耗条件，以及未知且远端仍 B 时不能提前完成采用／舍弃。

本阶段门禁：新增测试及现有受影响章节／权限／Agent 测试通过；审阅 API 与 service 未出现新的裸写路径。失败则保留草稿能力尚未接入的状态，不先开启自动保存。

## 3. 阶段 S2：在途输入与富文档证据

依赖：S1 契约已固定。责任范围：`frontend/src/components/scenes/sceneDraftController.ts`、`RichSceneEditor.tsx`、`richSectionHistory.ts`、必要时 `richSectionGuard.ts`，及新纯状态／证据模块和对应测试。

- [x] 引入 B/S/D、字段级 editSeq／确认边界与不可变 attempt；一次一个服务器写入链，保留请求期间的新编辑。
- [x] 分开输入 editSeq 与记录 record_seq；ACK 必须匹配当前 attempt ID／阶段，只通过控制器发布完整新快照，迟到旧回执不能回退新 attempt。
- [x] 将自动正文保存与手动全部保存分成明确字段集合；独立场景标题／摘要不被自动正文保存顺带提交或清掉 dirty。
- [x] 将编辑证据分为 B→S、S→D；实现已确认、明确未提交和未知结果的基线推进／合并；遗留授权错误也保留未确认 S。
- [x] 先用真实 PM 事务验证：在途新增后删除、删除后撤销、移动区块、标记重定位／退场、文档已存后后续失败、中文组合输入遇迟到 ACK。
- [x] 墓碑同步使用受校验且 `addToHistory=false` 的事务，基线变化不重建整份 EditorState；跨保存撤销／重做仍有效。
- [x] 验证成功响应只确认 S，不替换 D、不清除尾部证据；两个阶段部分成功不恢复旧基线。

本阶段门禁：现有控制器、富文档与历史测试加新增竞态通过，证明普通保存可以接受在途输入后再接通定时保存。不得用恢复全局输入锁代替该门禁。

## 4. 阶段 S3：本地仓库、快照与恢复分类

依赖：S2 的可序列化快照接口已固定。责任范围建议为 `frontend/src/components/editorDrafts/` 中的纯类型／解码／恢复判定模块，`frontend/src/utils/editorDraftStorage.ts`，及对应测试；最终命名可按现有目录规范调整。

- [x] 实现 schema 1 的标题／场景判别记录、独立 writer/draft ID、稳定场景锚点及 B/S/D／证据完整载荷。
- [x] 封装原生 IndexedDB，注入 IDBFactory；持续写入最新有效快照，最多一个执行中事务和一个待写最新快照，完成事件才推进本地状态。
- [x] 原子条件更新与清理、恢复副本建立、旧序号保护、按账号和目标查询；不按章节整批删稿、不淘汰未同步数据。
- [x] record_seq 覆盖无新输入的阶段／基线／暂停变化；删除前所有手动字段也须 clean，恢复默认保留来源；关闭分支后旧写入不能复活记录。
- [x] 处理 abort、quota、blocked、versionchange、close、损坏／未来 schema；禁止清库兜底，权限清空不能落成空稿。
- [x] 还原遗留 sending 为 unknown；根据授权后读取的 B／S／远端完整值分类，服务器有额外版本变化时保持暂停。
- [x] 添加 `fake-indexeddb` 开发依赖并锁定版本；不增加生产依赖。逐例独立 IDBFactory 验证事务行为，另用故障注入覆盖事务 request success 后 abort。
- [x] 覆盖两标签页分支隔离、复制标签页、恢复过程中来源变动、已保存旧快照不能删除新输入，以及登出／换账号期间迟到本地事务。

本阶段门禁：序列化往返保持完整富文档与证据；损坏记录不会自动提交或消失；mock 测试不得声称浏览器磁盘持久化已验。

## 5. 阶段 S4：调度、标题与动作屏障

依赖：S1–S3。责任范围：`frontend/src/api/chapters.ts`、`frontend/src/api/types.ts`、`ChapterEditorPage.tsx`、`useSceneEditor.ts`、新章标题／保存调度控制器与 hook。

- [x] 标题只走 `/editor-title`，失败不 fallback 裸 PATCH；前端条件、响应与后端同步，标题 trim 后确认不覆盖后来输入。
- [x] 1500 ms 防抖／10 s 最长等待／组合输入暂停；手动保存并入同一队列；已知离线保留本地、重连先核对、未知结果须显式重试。
- [x] 提取章标题基线和草稿状态，删除自动保存路径中无条件 `setTitle(response.title)` 的覆盖行为。
- [x] 自有标题成功后验证版本推进并更新场景基线，保留尾部文档／证据；刷新规划版本后才放行 AI，额外非自有版本推进仍冲突。
- [x] 分开普通保存忙与动作锁；正文输入在请求期间有效，AI／恢复／场景结构／转换继续受控。
- [x] `flushThroughCurrentEdit` 读取当前状态，配合短暂动作屏障消除“保存完成到 AI 请求之间又有输入”的窗口；等待旧写入排空，避免取消后假定未提交。
- [x] 外部任务完成、版本恢复、目标变化、卸载和权限变动使旧定时器及确认失效；尊重原后台任务 detach 语义，不在卸载时取消 AI。
- [x] 保存携带草稿作者 ID 和原始创建时间。恢复与导出重新核对登录身份和作品访问，同源跨页换号不能以新会话读取或提交旧账号的本地草稿。
- [x] 持久化订阅只接有效编辑与确认事件，处理 visibilitychange/pagehide 催促和 beforeunload 提示；广播只传元信息且不是互斥依据。

本阶段门禁：fake timers + deferred promises 验证实际请求体、版本、输入和记录，不只断言 timer 次数；覆盖服务端已提交但响应丢失／响应阶段撤权的路径。

## 6. 阶段 S5：恢复面板、冲突比较与状态

依赖：S4。责任范围：`SceneConflictReview.tsx`、`SceneWritingPanel.tsx`、`ChaptersPanel.tsx`、`ChapterEditorPage.tsx`，复用 `TextDiff.tsx`／下载工具，新恢复与保存状态组件。

- [x] 编辑页显示本地／同步状态，提供失败重试、核对和草稿入口；自动保存成功不反复 toast。
- [x] 作品章节列表提供该账号该作品的“本地草稿”，使退场场景／已删除章的记录仍可达；鉴权完成前不加载／渲染草稿内容。
- [x] 多分支列出时间／目标／字数，由作者选择；恢复到编辑区本身不发写请求。无冲突时可“保存恢复稿”或在继续编辑后进入自动保存。
- [x] 恢复替换当前 D 前须持久化当前副本并处理在途请求；存储失败不能直接替换。持久 recovery_hold 只由明确保存或对应自动字段的新编辑解除，刷新／重连／独立摘要编辑不解除。
- [x] 扩充完整文档差异，确认绑定本地 editSeq 和远端版本；继续输入或版本变化就重置确认。
- [x] 安全可同步时作者选择本地版本仍带条件；采用服务器时先保留原稿副本；结构不能复用时完整比较／导出，禁止退化成纯文本强覆盖。
- [x] 离开提示区分已保留草稿、存储失败，以及未纳入持久化的笔记／排序／AI 预览；不误报所有修改均可恢复。
- [x] 明暗主题、375px 布局、键盘、焦点与简体中文状态可用。

## 7. 验收矩阵

| PRD | 自动化验证 | 真实浏览器验证 |
| --- | --- | --- |
| A1 | 标题条件写、调度时间、字段确认、no-op | 标题＋正文停顿保存、手动快捷键 |
| A2 | B/S/D 与真实 PM tail／墓碑／历史测试 | 延迟网络中连续输入，撤销／重做，组合输入 |
| A3 | 快照往返及恢复分类，事务完成边界 | 断网输入至“本地已保留”，关闭页，联网同 profile 重开恢复全文／格式／区块／标记 |
| A4 | S/B/远端三种分类，ACK 丢失与旧序号清理，未知 B 不提前采用／舍弃 | 请求已提交但响应丢失，重连核对并同步新输入 |
| A5 | 两请求竞争、标题 ABA／Agent undo 单调版本、两分支 IDB、陈旧确认失效 | 同浏览器双标签页及独立客户端的标题-only／正文冲突 |
| A6 | AI/恢复动作屏障、任务完成使旧 attempt 失效 | 另一端完成 FakeLLM 任务／版本恢复，旧稿不自动覆盖 |
| A7 | 账号／权限／会话代次、迟到网络及本地事务 | 撤权、只读、换账号后不显示或串写旧稿 |
| A8 | IDB quota/abort/blocked 故障注入 | 受控存储失败时状态、手动保存和离开提示 |
| A9 | 版本／清空／结构历史现有回归 | 清空后恢复、场景退场草稿仍可查看／导出 |
| A10 | 自动正文保存不提交独立摘要；富文档全差异 | 格式／区块／标记冲突的比较与安全保留 |
| A11 | 稳定锚点、防 ID 复用、复制页分支、恢复来源 CAS | 多份草稿逐项选择及目标失效入口 |
| A12 | 分阶段成功后失败、403/404 后未知结果、旧接口缺 guard | 混合版本失败可见，无裸写回退 |

浏览器使用隔离数据、测试账号和 FakeLLM；不得调用真实模型或更改用户正式作品。完整浏览器步骤和结果落盘到 `research/verification.md`，逐项区分已通过、未验和限制。浏览器模拟 IME 不等同所有 OS 的原生输入法验证。

## 8. 验证命令

以下在相应目录分别运行；新增文件名仅在其创建后有效。开发期间按本阶段受影响文件执行，不重复无变化的全量套件。

后端定向（`backend/`）：

```bash
uv run pytest -n 0 tests/test_chapter_title_save.py tests/test_chapters.py tests/test_scenes.py tests/test_scene_versions.py tests/test_scene_review_regressions.py tests/test_rich_document_api.py tests/test_rich_document_history.py tests/test_chapter_versions.py tests/test_collaboration_content_access.py tests/test_agent_api.py tests/test_agent_tools.py tests/test_agent_tools_e4ic.py tests/test_agent_effects.py tests/test_scene_agent.py -q
```

前端既有关键定向（`frontend/`）：

```bash
npm test -- src/components/scenes/sceneDraftController.test.ts src/components/scenes/sceneRichDraftController.test.ts src/components/scenes/richCompositionHistory.test.ts src/components/scenes/richSectionTransaction.test.ts src/components/scenes/richSectionOperations.test.ts src/components/scenes/richSectionMarkerMapping.test.ts
```

新模块定向（按 S3/S4 最终落点维护参数）：

```bash
npm test -- src/components/editorDrafts src/utils/editorDraftStorage.test.ts
```

定稿后的完整门禁（前后端命令可在各自目录并行，但记录全部结果）：

```bash
# backend/
uv run ruff check .
uv run pytest -n 4 --dist loadfile --durations=20

# frontend/
npm run lint
npm run build
npm test
```

只采用当前 README／CI 的 SQLite 测试资产；不恢复已移除的 PostgreSQL 套件，不宣称 PG 锁行为已实测。遇当前 locale 导致 Alembic 中文读取错误时按规范使用 UTF-8 locale，不改业务迁移或删断言。

## 9. 最终检查、文档及回退点

- [x] `trellis-check` 已复查 A1–A12、写入权限和版本条件、完整文档证据及旧响应，问题已修复。前端、后端和浏览器结果见 [验收记录](research/verification.md)。
- [x] README 和 TODOS 已同步自动保存、本地草稿、联网恢复、结构冲突及验收结果。
- [x] frontend 状态／组件规范及 scenes／rich-documents 契约已同步，包括自动保存与手动字段、证据基线和标题写入条件。
- [x] 已检查 Git 差异和新增文件的空白格式，两个 Trellis 上下文文件均校验通过。
2026-11-18 用户批准提交、归档、合并到 `main` 并推送。提交和归档按 Trellis 收尾流程处理。

回退点：S1 未接 UI 时可单独回退代码；S2–S3 未接调度时保留旧手动路径供回归；S4 后回退前端应保留后端标题 guard 和所有未同步 IndexedDB 记录。不得为回退清库、删除草稿或放松服务器前置条件。后端完全回滚时新标题入口失败并保留草稿，其边界见 `design.md`。
