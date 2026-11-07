# 独立质量审查

2026-11-07。角色：独立 `trellis-check`。先审查冻结的 E1/E2/前端，再于 E3 冻结后完成跨层只读审查；不能把本轮局部门禁写成全任务通过。已读取原生 hook 完整落盘输出、任务三产物、check.jsonl 的规范／研究合同和两层 index；没有递归派发代理。

## 最终结论

**全范围独立审查通过，C1–C12均已闭合，无未修复的实质性发现或待验审查项。** 保留以下真实失败与修复过程，不把首轮写成全绿。

- 最终后端完整门禁由主会话执行：2669 passed、1条既有SDK warning、无skip；独立核实原始终态、exit0及当前604文件逐个SHA与最终cut一致。
- 最终前端完整门禁由主会话独立重跑：lint／含`tsc -b`的build／147文件1054 tests均退出0；独立核实日志和当前644文件SHA。本代理另独立32条最终C11 focused通过。
- 主会话真实浏览器证据29项全部passed、无pending，包括C11B双tab未订阅异景任务完成后分析stale、未保存emoji草稿保留及无伪分析。本代理直接读取`evidence.json`核实29/29及该项结构化证据，不冒充亲自操作浏览器。
- README／spec／31条manifest／9份规范摘录终审通过。完成状态、TODO、服务清理、提交及归档仍由主会话按工作流处理；本代理没有越权改状态或提交。

## 已修复发现

### C1：场景页的父章版本未纳入一致快照核验

- 位置：`backend/app/services/scene_service.py::list_scenes`。
- 证据：确定性地在父章读取后用另一 HTTP 请求提交正文，原返回200且页顶 `content_revision=0`、行内 `content_revision=1`。
- 修复：父章读取时的版本参加末尾SQL标量比较，包括 offset 超过末页、没有行内版本的情况；混合读取返回409 `scene_context_changed`。
- 回归：`test_scene_review_regressions.py::test_scene_page_rejects_chapter_changed_between_parent_and_rows` 两个offset。

### C2：结构历史可以夹带另一章的真实规划身份

- 位置：`backend/app/services/project_transfer/scenes.py::validate_scene_tables`。
- 证据：两个章节各有Scene本地规划，把第一章结构历史 `unit_id` 换为第二章真实unit后，旧预览200接受。
- 修复：历史unit当前来源Scene须可证明仍属于该历史章。仅同章显式改挂不拒绝，不要求当前 `scene_id` 或 `binding_epoch` 等于旧快照。
- 回归：跨章unit400；经真实 binding API 同章后续改挂的完整备份仍200。

### C3：删除Scene标签损坏可恢复结构和完整备份

- 位置：`planning_service.delete_tag`、`planning_scene_service.assert_tag_unreferenced`。
- 证据：Scene本地关联标签，或后来恢复继承但历史仍引用该标签时，DELETE原返回200，紧接完整备份预览400「场景历史标签引用缺失」。
- 经主会话确认：增加最小引用保护，返回409 `planning_tag_in_use` 及本项目 `sources`，不删除旧快照、不吞本地意图。没有Scene本地／历史依赖的旧章标签仍可删除。
- 回归：本地依赖、仅历史依赖、旧章-only允许删除；失败不推进规划版本，之后完整备份仍可预览。

### C4：UI全字段草稿误触发每次正文保存的结构历史边界

- 位置：`scene_service.update_scene`。
- 证据：与 `SceneDraftController.save` 一致发送相同title/summary及两次不同content，原产生3个版本，绕过固定十分钟合并窗口。
- 修复：按规范化后的title及原样summary的实际差异判定metadata；正文原样保存。真实metadata修改或正文由有字变为空白仍独立留档。
- 回归：两次普通全字段正文保存合并；真实标题、摘要修改各自留边界。

### C5：删章把Scene本地角色误复制为旧章孤立cast

- 位置：`planning_lifecycle.detach_sources`。
- 证据：Scene本地关联角色后，DELETE章节204、完整备份导出200，但预览400「场景规划单元不能携带旧章孤立角色副本」。
- 修复：仅章层保存 `detached_character_ids`；场景保留真实本地角色join与覆盖模式，来源失联后不凭同号对象自动认领。
- 回归：实际删章后完整备份200，孤立Scene单元保留join而没有旧章cast副本；原 `planning_lifecycle` 的章层脱离／重挂测试保持通过。

### C6：新基础Scene路径／body身份未在绑定数据库前约束范围

- 位置：`api/v1/scenes.py`、`schemas/scene.py`。
- 证据：新路径的0/负值／超int32／极大整数及after_scene_id、reorder IDs的16个HTTP负例全部RED；极大值可进入数据库绑定，出现内部500，其余也未按请求422拒绝。
- 经主会话确认：仅新scenes路由的pid/cid/sid使用统一 `PathId=1..2^31-1`，`SceneId` body同范围且保持strict非bool；不扩大到旧章／无关API。
- scoped AI 同类缺口由E3 owner修复；保留内部target严格解码，不把损坏目标悄悄变成整章。

### C7：显式Scene错章校验被旧版本检查抢先

- 位置：E3 `generation_service.validate_and_claim`、`rewrite_service.prepare_rewrite_job/_apply`，由原owner修复。
- 独立focused曾复现：同项目错章sid本应404，返回409 `stale_planning_revision`。不刷新测试body掩盖，先验证真实Scene归属，再验内容／规划版本。
- 新进程错章单例、原提交前／后取消边界与当时审查12例合计17 passed，确认不是旧进程导入早期源码后的误报。

### C8：Agent Core撤销遗漏仅保留在Scene历史的实体依赖

- 位置：`agent/effects.py::_check_dependents/_setting_references`，由E3 owner提出并修复，独立审查确认。
- 证据：Agent新建实体后，作者在Scene引用又恢复继承，当前raw没有typed FK，只有 `ChapterVersion.scene_snapshot` JSON保留引用；旧依赖扫描可能删除目标并留下不可导入历史。
- RED：owner仅在测试进程内让 `chapter_versions` collector返回空集，真实角色用例即报 `DID NOT RAISE ConflictError`，未改产品源码或放宽测试。
- 修复：在原精确semantic扫描中加入非空结构历史，复用备份 `scene_snapshot` remapper收集真实引用，只保留本次拟删除目标；继续按同批effect的before／一起删除判断合法赢家，不粗暴阻止所有撤销。
- 独立复核：角色、世界、组织、职业、关系五类历史唯一依赖拒绝撤销；历史行与角色同批撤销允许。六例均实际导出完整备份再导入成功，6 passed（6.34s）。

### C9：超合同的跨章正文依赖破坏原合法任务并行

- 位置：`scene_ai_context.read_stamp`，由E3 owner发现并经主会话确认边界后修复。
- 证据：同项目两个不同章节可合法并行；原 `test_per_user_limit_and_queue_position` 中前章先提交使后章因额外前章content_revision依赖失败，不能改队列测试掩盖。
- 决定与修复：前章普通正文是构建时背景，不升级为目标章写冲突；仅移除跨章普通正文revision列表。目标章内容／位置、项目planning、全部潜在时间线、字段／分类及visibility快照仍严格核验，同章暂存提案仍不可混稿。
- 独立新进程复核：原并行任务例、整章Gate四参数及审查28例合计33 passed（28.43s）。真实三SDK同章旧正文stamp仍在visibility不变时拒绝，服务器零请求；最终采纳竞态仍不半写。

### C10：真实Scene演变被错误标为章边界／章已删除

- 位置：前端 `position.ts` 及设定变化、变化编辑、关系、成员、职业面板，由主会话真实浏览器发现、前端owner修复。
- 证据：实际Scene2 end变化的后端锚点正确，列表却显示「本章结束后」。独立扩查发现职业第六处仍用章初／章末，附加orphan标识也误称章节已删除。
- 修复：六处统一 `describeBoundary`，真实Scene显示「从本场景起／本场景结束后」；旧章／计划章的长、短标签保持。Scene失活文案与真实身份一致，无法区分类型的汇总使用「关联位置已失效」。不改时点求值或写入。
- 独立复核：六处调用及orphan文案只读检查通过；position／职业／关系／StoryPosition四文件33 tests通过（295ms），`npm run lint`及含`tsc -b`的`npm run build`均退出0。前端owner完整145文件1042 tests通过；主会话负责刷新浏览器复验。

## 文档与上下文终审

- README、新 `backend/scenes.md`、两层index及planning／progressions／transfer／series／mention／reader／agent／prompts／visibility／batch／frontend component规范已只读终审。
- 两处frontend旧合同遗漏已由主会话修正：第14条只限整章成功自动分析，单景仅过期；第15条建议重写按scope与两版本分派。C8保留历史引用与同批赢家、C9前章普通背景不绑定版本、scoped错误语义均与最终源码一致。
- 独立程序逐段验证9份spec-excerpts的来源SHA、真实行号及逐字符正文一致，均小于32768字节；最后一次在frontend第14／15修正后执行。
- `task.py validate 11-07-independent-scenes`：implement/check各31条，零警告。未提前勾选任务完成或第7项TODO。

## 完整门禁收口（保留失败历史）

- 主会话首轮后端完整测试：18 failed／2640 passed，691.83s；604个源码文件hash全程不变。该结果由主会话执行，不是本代理独立全量。18项已分组修复，原owner分别报告迁移34、人工入口79、AI194及PG5通过；第二轮随后完整2665 passed，见下节。C12尚未纳入该次cut，不能以这次通过覆盖新发现。
- C12已由独立精确GREEN及第三轮2669 passed完整闭合；C11所有入口已修复并独立32 focused／最终147文件1054 tests门禁核实。最后双tab真实浏览器同样通过，29项证据全部passed、无pending；没有以自动化替代浏览器。

## 已执行门禁

全部pytest使用本机有效 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`。测试使用独立tmp_path／FakeLLM；真实SDK仅访问隔离本地HTTP服务器，PG仅用隔离schema，不访问真实模型或用户数据库。

- 首批两项RED：2 failed / 1 passed；修复后新增回归4 passed。
- 新回归＋scenes＋scene_transfer：24 passed（24.49s，后续用例有增加）。
- scene_planning / scene_progressions / scene_context / scene_versions / scenes_migration：21 passed（29.68s）。
- 标签修复相关回归＋scene_planning＋planning：30 passed（30.55s）。
- metadata修复后新回归＋scene_versions＋scenes：22 passed（17.62s，C5加入前）。
- C5修复后新增9例＋planning_lifecycle＋scene_planning：46 passed（30.78s）。
- C6修复后审查28例（含真实SDK3例／非法ID16例）＋scenes11例：39 passed（27.29s）。
- 独立真实PG：`test_scenes_postgresql.py` 2 passed（3.24s）；私有scenes_test/scenes_verify的新UUID schema已finally DROP。覆盖真实fresh/0038→0039、子行、CAS、rank交换、级联；不是create_all冒充迁移。
- 独立真实PG AI：`test_scene_ai_postgresql.py` 5 passed（9.43s），同一确定性Gate跑成功一次提交及主失败／取消／规划变化／时间线变化，schema已finally DROP。
- 实际三SDK出站：`test_actual_sdk_send_rejects_old_scene_stamp_without_visibility_change` 3 passed（2.35s）。OpenAI／Anthropic／Gemini真实SDK访问隔离本地HTTP服务器；构建后仅改场景正文，确认visibility stamp完全不变，旧载荷仍以 `scene_context_changed` 拒绝且服务器请求列表为0。不是只验证FakeLLM fallback。
- 独立E3 generation/Agent首轮49 passed / 1 failed（C7），其修复后新进程相关单例＋原取消边界＋审查用例17 passed（15.34s）。
- C8独立复核：`test_scene_agent.py -k entity_undo_respects_retained_scene_history` 6 passed / 8 deselected（6.34s），含合法同批撤回及完整备份再导入。
- C9独立复核：原任务并行例＋整章Gate四参数＋审查回归28例，33 passed（28.43s）。
- 前端独立 `npm run lint`：退出0。`npm run build`：退出0；编辑器95.41kB，最大vendor-antd1158.46kB，无1500kB超限。
- 前端独立focused：scenes API、SceneDraftController、场景展示、StoryPosition、tasks、planning 六文件56 passed（329ms）。
- C10后前端独立focused四文件33 passed（295ms），独立lint/build再过，最大vendor-antd1158.46kB、editor95.41kB。
- 本审查拥有文件ruff与tracked/new no-index空白检查通过；C8后独立完整backend `uv run --no-sync ruff check .` 通过。后端没有配置独立静态类型检查器；前端类型门禁由`npm run build`中的`tsc -b`通过。

## 第二轮最终全范围只读审查

- 先执行 `get_context.py --mode packages`：单仓库、backend/frontend两层；重读两层index及完整Quality Check，复核当前PRD／design／implement与31条manifest。
- 本轮只读复核三份 `full-suite-*-fixes.md` 及实际产品／测试差异；没有改源码、测试或spec，没有重复大组pytest。后端独立 `uv run --no-sync ruff check --no-cache .` 再次通过。
- AI七项修复逐项核验：范围块和规划从同一配置预算扣除；Agent fixture经权威Scene写原语而非篡改投影；坏历史Task JSON只读形状守卫不修写、不放松严格新target；章共同梗概恢复自动匹配但手动cast仍接受场景覆盖；旧章级单景revise保留300下限且实际日志一致；旧风格parity只扣除必要新增范围块；quota仅提前固定故事结构，保留原并发／额度／成功断言及5秒屏障。
- 迁移修复保留真实0034/0035/0036目标，读写当时反射表；冻结旧CHECK逐字匹配0031源码，在独立同方言期望库生成归一化文本，不复制实际历史库约束、不删CHECK比较。其余列／FK／唯一／序列／JSON null／真正PG提交及拒绝有损降级断言保留。
- 人工入口适配仅给新结构恢复提供真实CAS、给实际生成章种子显式初始化Scene、补齐章级位置的两个null字段；审计清空新增空正文结构版本的身份／序号／哈希／actor／隐私断言，原changed/missing及删后快照保护未放宽。
- 重新检索旧正文写入：章PATCH、版本、生成／重写、Agent正文与effects仍委托Scene权威原语；其余Chapter Core UPDATE为状态或单一CAS。新增路由不直接查ORM，Task读投影不外露私有载荷。
- 主会话第二轮完整终态：**2665 passed／1条既有Gemini SDK warning，635.15s（runner640.13s），exit0**；604文件SHA全程稳定、四项PG URL全启用、无skip，runner已reap。原始日志为主会话私有QA的`verification/backend-full-2.log`。当时C12尚未纳入，该轮未作为最终结果，后由第三轮闭合。
- C12纳入后的主会话第三轮完整终态：**2669 passed／1条既有Gemini SDK warning，693.90s（runner698.74s），exit0**，四项PG全启用，无skip。独立读取原始`backend-full-3.log`尾部及`backend-run-3.json`确认退出／时间／`running=false`／`source_unchanged=true`／`changed_files=[]`；再逐个重算当前604文件SHA，全部与`backend-source-cut-3.json`匹配。没有把主会话执行的完整测试冒充本代理重跑。

### C11（代码／独立focused／完整门禁／真实双tab UI闭合）：场景建议重写仍承诺全章重分析与评分对比

- 独立审查从 `AnalysisDrawer`「留空=原文字数」与已批准旧章单景300下限的边角不一致报给主会话；主会话继续追读并确认实际跨层缺口：抽屉不展示目标scope、无条件承诺自动分析、`reviseCompareFor`未按Task.scene_id排除单景，导致成功单景任务显示伪全章评分比较／等待重分析。
- 主会话已交前端owner窄修，真实SSR／派生状态7条RED先复现；新增范围提示区分显式Scene、多景章及旧章单景300默认下限，运行提示取真实Task目标，不借当前表单扩大范围。单景revise返回null compare并清旧全章比较；仅真实分析任务运行才称「正在重新分析」，保留手动重跑。
- 独立追读再发现两个异景完成入口：已订阅任务在`followTask`中提前return，以及从未attach另一Scene任务的`wasOtherSceneWriting` active-store退出watcher。两处均已明确保稿并刷新章分析／作品状态，清旧compare；未订阅入口只在GET章成功、仍有读权限后刷新，不猜终态、不自动分析。owner实际effect RED为refreshAnalysis未调用，修复后覆盖未attach、退出仅执行一次、首次无任务不伪造、默认preserveDraft和无analyze。
- 最终冻结后独立执行 `npx vitest run src/api/analysis.test.ts src/components/chapter/AnalysisDrawer.test.ts src/pages/project/ChapterEditorPage.taskScope.test.ts` → **3文件32 tests passed，2.04s**。这些是真实SSR／实际handler与effect回归，不宣称浏览器交互。
- 前端owner最终147文件1054 tests、lint/build通过；主会话另独立完整重跑，三命令exit0（lint20.94s、build10.81s、test15.8s，合计47.6s）。本代理直接读取`frontend-run-final-2.json`、相应原始日志及测试147／1054终态，逐个重算当前644文件SHA，全部匹配`frontend-source-cut-final-2.json`，`source_unchanged=true`、`changed_files=[]`。
- 主会话同景真实Gate浏览器已确认默认21字而非300、Task#7绑定Scene#1、章分析stale且手动分析仍仅1条、无伪比较。
- 最后双tab真实浏览器通过：tab2预先打开Scene3已分析状态并输入未保存emoji稿；tab1的Scene1 polish Task9主Gate期间，tab2明确显示另一景后台写作、未接流、草稿保留且只读。release后tab2自动变「已过期」，保留原稿并显示明确最新版本核对提示；HTTP确认Task9成功、仅Scene1改正文／一次revision、Scene3服务端未写、分析任务仍2条无自动分析。
- 主会话证据case为 `browser_c11b_unsubscribed_other_scene_finish`，包含running／finished UI JSON、DOM与实际messages。独立直接读取私有QA `evidence.json`：29条全部`passed=true`、无未通过项，C11B含原草稿保留／未接流／一次revision／服务端未写等结构化证据；不把主会话浏览器操作写成本代理执行。
- 最新README／spec与design旧章单景例外、C12判空、C11真实scope／评分／异景刷新合同一致。最终9份摘录来源SHA、实际行号正文逐段一致、每份小于32768字节（排除明确非原文的EOF sentinel再比较）；两manifest各31条再次validate零警告。本轮没有改产品、测试、spec、任务状态或提交。

### C12（已修复，独立GREEN及第三轮完整闭合）：旧单景PATCH纯空白清空漏存结构历史

- 位置：`chapter_service.py`旧PATCH只有 `content_changed and not data.content` 才force基线，纯空白字符串绕过；随后 `version_service.record_manual`在空白且无latest时返回None。显式Scene更新已有正确空白清空保护。
- 经主会话明确批准，仅在 `/private/tmp/ainovel-c12-readonly-coqeifxs/test_legacy_whitespace_history.py` 写一个隔离回归，加载既有 `tests.conftest`；未改backend目录、框架或当前full source cut。
- 从backend执行（有效UTF-8 locale及 `PYTHONDONTWRITEBYTECODE=1`）：`uv run --no-sync python -m pytest -p tests.conftest -p no:cacheprovider -c pyproject.toml /private/tmp/ainovel-c12-readonly-coqeifxs/test_legacy_whitespace_history.py -q -s --tb=short`。
- 真实HTTP新章原稿为`完整原稿😀\n必须可恢复`，隔离fixture准备合法无历史状态；before正文完整、revision=0、versions=0。同一旧单景PATCH写入` \n\t`返回200，Chapter／Scene逐字符投影均为空白、word_count=0、revision=1，但after versions仍0、retained_contents=[]。
- 正确保留原稿与清空结果结构边界的断言实际 **1 failed in 2.14s**。这不是环境错误或人为放松断言；证明旧稿可直接丢失。只运行此1例，没有重复已有测试组或触及用户数据库。
- 主会话严格等待第二轮完整终态才交foundation窄修；产品仅把旧PATCH判定改为 `not data.content.strip()`，传给权威写原语的正文仍原样，版本原语／10分钟规则不改。永久回归原代码RED为1 failed／3 passed，修复后owner四例及关联44例通过，证据见 `research/c12-whitespace-history.md`。
- 冻结后独立原`/tmp`测试不改任何内容精确重跑：**1 passed in 1.35s**。before仍revision=0／versions=0；after revision=1／versions=2，完整保留`[" \n\t", "完整原稿😀\n必须可恢复"]`，Chapter／Scene逐字符一致。
- 冻结后独立永久四参数：`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -p no:cacheprovider tests/test_scene_versions.py::test_legacy_single_scene_clear_preserves_exact_structural_history -q --tb=short` → **4 passed in 6.77s**。逐项核验同一Scene两结构快照、原version ID不合并、Unicode／CRLF／制表符／尾空格、一次revision及真实恢复仍保留清空版本。
- 主会话另用真实API「不含versions备份→导入新独立书」构造合法无历史章，不经人工SQL种子；纯空白旧PATCH后两份精确结构历史并可恢复原稿通过。该实际HTTP/API证据来自主会话，不冒充本代理或浏览器操作。
- 主会话第三轮完整后端2669 passed，原始终态及当前604文件SHA已独立核实，C12后端闭合。本代理只运行上述1＋4精确例，没有重复完整后端。
