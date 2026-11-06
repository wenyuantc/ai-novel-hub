# 分类前端独立检查

本报告对应 `.trellis/tasks/archive/2026-11/11-05-setting-auto-categories`，由已派出的 `trellis-check` 直接审阅与局部自修；没有再派代理、提交代码或修改任务状态。前置字段阶段的完整门禁与浏览器证据另见 `field-frontend-check.md`，本轮不重复旧字段实现审阅。

## 检查范围与边界

- 按本任务 check／implement manifests、PRD、design、implement、父 `category-handoff.md`、实际 `setting-categories`／`setting-fields` 跨层规范和前端状态／权限／类型／质量规范检查；同时应用 `trellis-check` 与 `frontend-ui-engineering` skill。
- 首片为四详情 `settingTags` 会话及标签整合；Page／cards owner 冻结释放后扩到分类页、规则／结果／草稿编辑器、router／共享导航、分类／卡片 DTO 与 API、审计标签和刷新广播、cards v5 导入预览／确认及相关测试。
- 未编辑后端、`EntrySelect`、旧字段／剧情实现、用户服务和数据、`pelican-cycling.html`。本轮没有打开浏览器；真实 8014／FakeLLM 验收由主会话负责，不能以本报告静态或 controller 测试替代。

## Findings (fixed)

### 分类名称与标签条件的 Unicode 空白合同

- 文件：`frontend/src/components/settingCategories/categoryDraft.ts`、`categoryDraft.test.ts`、`frontend/src/components/settingTags/tagDraft.ts`。
- 问题：草稿使用 JS `trim()`，与服务端 Python `str.strip()` 的空白集合不同。合法 BOM 前后缀会被前端剥离；NEL／FS 未被剥离。只修改分类名称时可能意外重发改变过的 tag 规则，只修改排序时也可能意外改写已有 BOM 名称。
- 修复：规则复用现有严格 `normalizeSettingTags`；分类名称复用同一 Python 空白 helper `stripSettingWhitespace`，不增加另一份空白规则。规则载荷仍只包含业务身份，未改变 DTO／服务端合同。
- 证据：标签条件回归先在旧代码失败，修复后 `categoryDraft`＋`tagDraft` 2 文件 9 例通过（02:12:06）。名称回归随后单独先 RED（02:16:39），修复后覆盖保留 BOM、剥离 NEL／FS、空名称拒绝与仅排序 PATCH 不重写名称。

### 标签缓存的删除生命周期与有效 404

- 文件：`frontend/src/components/settingTags/settingTagsController.ts`、`settingTagsController.test.ts`，四个 `Characters`／`World`／`Organizations`／`CareersPanel.tsx` 成功删除处理。
- 问题：页面本地 Session 永久按 kind:id 缓存 controller，删除成功后仍保留旧标签草稿；原有实体 SQLite ID 如被复用，同 ID 新条目可能继承旧草稿。有效 GET 已确认 404 时也仍保留 base 和可保存草稿。
- 修复：经主会话明确批准，增加局部 `Session.forget(entry)`，仅在四详情删除 API 成功后调用；关闭并取消请求、清空状态、移除该 controller。refresh／compare 收到当前有效 404 时撤下 base／original／latest／草稿／搜索，继续保留错误与显式读取重试入口。503 等暂时失败仍保留已有草稿。未以名称或时间戳自造身份。
- 证据：4 实体 delete→recreate 与 refresh／compare 双 404 共 6 例先 RED（02:15:54），修复后 GREEN；覆盖取消 signal、迟到响应不回填、旧 controller 不再保存、同 ID 新 controller 初始为空、其他条目草稿保留。包含这些修复、Unicode 与编辑器合同的 4 文件 38 例通过（02:16:47）。
- 边界：上述前端卫生处理不能替代服务端对远端删除与 ID 复用的身份保护。主会话已将实际 HTTP／0036 身份问题交给后端 owner；本 reviewer 未改后端，未声称已验证其解决。

## 其余独立审阅结论

- 标签的读／写／建议 ticket 独立；actor／授权／作品失效会取消旧结果，失读撤下内容，提交和回填均复查实时写权限。关闭与 A→B→A 保留各条目原快照、原 CAS、草稿与未提交搜索；广播在响应到达时重新判断 dirty，不覆盖在途输入。
- 标签 409／未知保存结果进入显式对照；读最新不更换原 CAS，明确采用后才可继续保存。建议失败不阻止合法自定义标签，`[]` 明确清空，50×50 以码点和共享 Python 空白规则处理。标签区没有外层 Form 提交，Enter 只处理标签，不混名称、基线或职业阶段保存。
- 分类配置、结果与完整字段元数据的 HTTP 投影核对 `fields_revision`，不拼合异版名称。显式 retry 发新请求代次，同 revision 失败可以恢复；分类／查询／时点／actor 改变会取消旧请求。
- 规则保持字段／选项 ID，换字段清空 option；停用身份仅允许原分类保留的 pair，新条件不可借同名绕过。分类 PATCH 省略未改变的 rules，保留原规则行身份；保存携带原 fields／categories 双 CAS，409／未知结果保留原稿和原版本，最新对照须明确采用。
- 成员结果由服务端完整匹配、计数和分页，不在客户端逐条 effective。筛选、时点或页大小变化回第一页；深链使用本次请求捕获的位置。历史章节无效不静默降为当前，界面可见说明当前标签／规则与按章下拉值的混合口径。
- cards v5 在确认时携带原 preview 两版本；主动更新发现任一版本变化也只产生候选对照，不能自动采用新保存依据。修改映射必须重预览；skip／职业 reuse 摘要保留目标已有 tags；旧 v1–v4 不解释未来标签。服务端 CardTaggedEntry 要求标签已经 canonical，非 canonical 卡片直接拒绝，前端不替它清理后导入。
- 新控件有可访问名称与明确按钮，标签和长文本有换行边界，移动端分类／结果用卡片，独立标签区 16px 间距保留。375px、主题、真实键盘／焦点及实际网络行为仍需主会话浏览器验收。

## Findings (not fixed)

- 远端实体删除后 ID 复用的全写入路径保护：属后端持久身份范围，已向主会话给出可复现路径并交接，不在前端伪造身份。
- 文档索引／规范同步：前端索引目前缺 `setting-categories` 跨层入口；建议在统一文档收口补标签会话删除清理、异版 HTTP 拒绝拼合和显式 compare 合同。已报告主会话，不抢写其他 owner 文档。
- 真实浏览器验收不属于本 reviewer 的执行范围；本报告不将静态检查和 controller 通过描述为真实 UI 通过。

## Verification

- Tag 首片 scope eslint：退出 0，无 warning；4 文件 39 例通过（02:08:04，含原 Tag 17 例、分类 API 与人工只读整合）。
- 完整分类 scope eslint：退出 0，无 warning（02:17 后；含本轮修复涉及的所有文件）。
- 最终统一全量 lint／build／tests／前端 diff check：已按以下顺序完成。

### 最终当前代码门禁（2026-11-06 02:18–02:19，Asia/Shanghai）

本轮覆盖最新分类页／cards v5 owner 冻结的全部前端代码，以及本 reviewer 最后的 Unicode、Session.forget／有效 404 修复；验证后没有再改产品或测试文件。

| 命令（frontend 为工作目录，diff check 在仓库根执行） | 结果 |
| --- | --- |
| `npm run lint` | 退出 0，完整 `eslint .`，无 warning／error。 |
| `npm run build` | 退出 0；实际脚本 `tsc -b && vite build`，类型检查与 Vite 均通过，无 warning。3610 模块；独立 `SettingCategoriesPanel` chunk 40.26 kB，最大 vendor-antd 1158.45 kB，未触发 1500 kB 警告。 |
| `npm test -- --run` | 退出 0；package 的 `test` 为 `vitest run`，实际执行 `vitest run --run`。**136 files／980 tests 全部通过**；开始 02:18:56，耗时 7.42s。 |
| `git diff --check -- frontend` | 退出 0，无输出；仅限定前端的 Git 差异检查。新 untracked 前端模块同时已被上述全量 lint／build／Vitest 扫描。 |

测试原样保留 **5 条** Node 运行时警告：`ExperimentalWarning: localStorage is not available because --localstorage-file was not provided.` 这是测试进程读取 Node 实验性 localStorage 时的环境警告；不是 ESLint／TypeScript／Vite warning，也不是测试失败。没有使用 warning 过滤、禁用检查、修改阈值或改 Vitest isolate 配置来获得通过。Vitest 另外输出了 136 workers 的常规性能建议，未据此修改配置。

前端写 ownership 已释放给主会话，当前代码冻结。本报告表示分类前端独立静态检查／自动门禁完成，**不表示整个分类子任务或父任务完成**；后端身份保护及真实 UI 验收仍以各 owner 的独立证据为准。

## 真实 CUA 反馈后的定向复核（02:31–02:34）

主会话在隔离 5179／8014 真实 CUA 发现以下两项，重新授权 reviewer 仅修改分类位置／编辑器状态和必要回归；未扩改共享字段主流程、API／types 或 tags session。

### 已修：章节模式切换的空输入不能往返 URL

- 主会话复现：`?id=1&view=effective&chapter_id=3&boundary=end` 已有完整结果后切“计划章节”，控件退回已有章节／章初并报位置错误。
- 根因：共享 `ChapterPositionInput` 正确发出 `planned`＋空锚并保留 end，但分类 `categoryLocation` 把 null 锚完全省略；严格深链解析遂失去模式，分类过滤组件用 chapter/start 默认值重建。
- 最小修复：仅 `settingCategories/categoryQuery.ts` 让空的明确锚（`chapter_no=` 或 `chapter_id=`）保留为分类位置输入草稿；分类 parser 仅识别有效 view／boundary 且单一明确空锚。未填章号仍被 `categoryQueryRequest` 拒绝，不发请求、不降为当前；混锚／伪整数／坏 boundary 继续报错。共享 `readEntityFieldPosition` 与字段流程未改。
- 回归：已有章↔计划章 × start／end 共4条，在旧代码全部 RED（02:31:26），修复后验证空输入与时点可往返、补齐后才查询、分页回1和原查询不被改动。相关4文件26例 GREEN（02:31:47），scope eslint 0。

### 已修：成功保存被自身广播误标过期

- 主会话复现：新分类 preview／保存成功后立即打开，没有外部写入仍出现配置过期提示，预览与删除不可用。
- 根因：`useCategoryEditor` 成功回调同步广播 `setting-categories`；同编辑器订阅广播后把刚从成功响应建立的新快照也标成 `reviewRequired`。两 revision 本身正确，并非继承旧 new 草稿。
- 最小修复：`categoryEditor.ts` 在同步自写通知期间，仅保留本次成功保存的 category key；其他缓存旧草稿仍失效，随后外部广播仍使该分类失效，失败／409／身份失效路径不豁免。删除成功仍使其余草稿失效。
- 回归使用实际 `subscribeProjectRefresh`／`invalidateProject` 广播链路，分别覆盖新建、修改；2条先 RED（02:33:14），修复后刚保存重开可预览／取得删除 token，其他草稿保留原 CAS 且需要对照，之后外部广播仍禁用旧确认。
- 最新定向验证：4 文件28例 GREEN（02:33:38）；四个实际改动文件 scope eslint 0，无 warning。

这两项产品修复发生在上文 136／980 门禁之后，该门禁仅为此前快照的证据。按主会话要求先冻结当前修复供真实 CUA 复验，等待其余反馈后再统一运行最终前端全量门禁；本报告不以定向通过替代最终 TypeCheck／Build／全量测试。

### 主会话提供的真实 CUA 复验（本 reviewer 未打开浏览器）

- 计划切换通过：已有章空输入→选择章末→切计划章节，planned radio 保持 checked，end 保留，URL 有 `chapter_no=`；填5得到4条，填尚未创建的25得到0条，均保持 planned/end，没有重置。
- 双页 CAS 通过：另一页改名保存后，本页原 ANY＋2条件＋sort30 保存被拒绝；名称、option、tag、顺序全部保留。三栏对照展示打开时原名和第二页新名，明确采用后 preview1条且有两条理由，再显式保存成功。
- 刚存刚开不误 stale 复验通过：主会话干净 reload，编辑已保存4号分类改名并保存成功，立刻再开同条，`falseStale=false`，preview／delete 正常 enabled。
- 四实体标签均经主会话真实独立保存；角色外层姓名 dirty、职业外层姓名＋stage dirty 均保留，Enter 未误提交外层。组织保存 `[]` 后 ALL 计划5结果从4条降到3条，恢复 tag 后回到4条。

## CUA 修复后的最终完整门禁（2026-11-06 02:37–02:38）

主会话明确授权后，在四个分类修复文件保持冻结的当前代码上重新完成前端完整质量门禁。这是覆盖最后计划位置往返、自身广播修复的最终证据，取代先前 136／980 快照作为当前计数。

| 命令 | 当前结果 |
| --- | --- |
| `npm run lint`（frontend） | 退出 0；完整 `eslint .`，无 warning／error。 |
| `npm run build`（frontend） | 退出 0；`tsc -b && vite build` 均通过，无 warning。为确认顺序，lint 退出后独立复跑 build 并采用该结果。3610 模块；SettingCategoriesPanel chunk 40.79 kB，最大 vendor-antd 1158.45 kB，无超 1500 kB 警告。 |
| `npm test -- --run`（frontend） | 退出 0；实际 `vitest run --run`。**136 files／986 tests 全部通过**，开始 02:38:04，耗时 8.04s。相比前轮增加4条位置状态和2条自身广播回归。 |
| `git diff --check -- frontend`（仓库根） | 退出 0，无输出；新 untracked 模块也已经过上述完整 lint／build／测试扫描。 |

测试仍原样输出5条 Node `ExperimentalWarning: localStorage is not available because --localstorage-file was not provided.`，未过滤、未降低标准。Vitest 136-worker 性能提示为常规建议，没有据此修改测试配置；lint／TypeScript／Vite 没有 warning。

本轮只追加本报告，验证后没有再改产品／测试、任务状态或提交。分类前端写 ownership 再次全部释放，当前代码冻结。主会话继续只读／375px／主题／实际 cards 验收；本 reviewer 不把尚未回报的真实 UI 场景描述成已通过，也不将本轮前端门禁等同整个分类／父任务完成。
