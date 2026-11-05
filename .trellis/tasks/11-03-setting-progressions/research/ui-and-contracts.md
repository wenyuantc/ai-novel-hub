# Research: 设定演变的界面与跨层契约

- Query: 核实角色／世界观详情、关系与职业维护、章节上下文预览的现状，提出最小可复用的演变维护界面和 API 契约。
- Scope: internal；仅研究，不修改产品代码，不决定后端时间线算法。
- Date: 2026-11-03
- 已确认范围：章节级、追加／替换、最小文本自定义字段、角色状态／人物关系／职业阶段回溯；独立场景和完整字段配置系统不在本期。

## Findings

### 1. 现有界面与数据契约

| 文件与锚点 | 核实结果及对本任务的影响 |
| --- | --- |
| `frontend/src/pages/project/CharactersPanel.tsx:49`、`:81`、`:254` | 角色是卡片列表 + 宽 760 的 Drawer，基础／描写／状态共用一个 Form，已有角色才出现关系／组织／职业／伏笔标签；保存后留在抽屉中。适合在已保存角色上新增「剧情变化」标签。 |
| `frontend/src/pages/project/CharactersPanel.tsx:88`、`:104`、`:108` | 保存调用 `formToCharacterInput`，响应会整体 `setEditing(saved)` 和 `setFieldsValue`。独立变化保存不能直接复用这段回填逻辑，否则可能覆盖未保存的基础设定草稿。 |
| `frontend/src/components/entities/characterForm.ts:37`、`:55` | 转换函数整组读写 `current_state`、`life_status`、外貌／性格／背景／简介等。它不区分基础值与时间线求值结果；仅改姓名也会带上其他旧字段。新增演变后不能将有效值不加区分地回填到基础表单后整体提交。 |
| `frontend/src/components/entities/CharacterFormFields.tsx:101`、`:121` | 描写字段为多行文本；状态区为 `current_state` 文本；`life_status` 是基础区的结构化下拉（`:73`）。文本追加／替换与存活状态替换必须使用不同控件，不能把枚举当自由文本。 |
| `frontend/src/pages/project/WorldPanel.tsx:49`、`:59`、`:181` | 世界观只有编辑 Modal；内容、标题、分类、排序整体提交；保存即关闭。地点／物品复用同一 `WorldSetting` 界面，没有独立详情路由。可在原 Modal 内增加「基础设定／剧情变化／按章查看」标签或拆出共用详情组件，不必新建地点／物品页面。 |
| `frontend/src/api/story.ts:12`、`:22` | 角色、世界观使用通用 list/create/update/remove；update 是 PATCH，但前端实体表单实际上发送全部字段。新接口需明确哪个返回值是基础值、哪个是派生有效值。 |
| `frontend/src/api/types.ts:551`、`:570` | `WorldSetting` 与 `Character` 当前没有自定义字段、基线、版本或变化列表字段；不能声称已有文本字段框架可以直接挂接。 |
| `frontend/src/components/relations/RelationEditModal.tsx:76`、`:114`、`:253` | 编辑弹窗每次将章节回填为 null；`chapter_no` 文案是「发生章节（可选，记入历史）」。类型、方向、亲密度、状态、描述一起提交。新增时间语义后应替换这段文案并明确操作种类，不能只改变后端解释。 |
| `frontend/src/api/types.ts:1297`、`:1309`、`:1344` | 关系创建／更新接受可选章节号，历史 `changes` 为 `Record<string, unknown>`，类型和方向存名称。历史目前是展示日志，不是前端可回放的状态契约。 |
| `frontend/src/components/relations/RelationHistoryModal.tsx:14` | 只读倒序展示时间、来源、章节、备注和旧→新，没有历史编辑／删除能力；新增可维护剧情变化不要直接给旧审计日志加编辑按钮。 |
| `frontend/src/components/careers/CharacterCareersTab.tsx:93`、`:103`、`:130`、`:144` | 职业完整编辑表单可填章节，但列表快捷换阶只发 `{stage_id}`，卸下调用无请求体 DELETE。若只升级编辑 Modal，换阶／卸下仍会绕过章节定位。必须覆盖这些快捷入口。 |
| `frontend/src/components/careers/CharacterCareersTab.tsx:36`、`:276` | 职业历史展示来源、章节、字段变化；章节是可选 InputNumber，不要求已经建章。新的选择器是否仍允许未来章节应与时间线存储约定一致。 |
| `frontend/src/api/types.ts:1668`、`:1683`、`:1695` | 挂接 API 的 `Assignment` 只表示当前职业阶段；`AssignmentUpdate` 明确注释章节号仅记入历史。不可在原类型中默默将当前字段改成任意章节快照。 |
| `frontend/src/api/chapters.ts:16`、`frontend/src/api/types.ts:632` | 现成的章节摘要列表提供 `id/chapter_no/title`，可构造可搜索的“第 N 章 · 标题”选项，不需要读正文。 |
| `frontend/src/components/chapter/ContextPreviewPanel.tsx:23`、`:30`、`:51` | 现有折叠预览仅显示剧情记忆与知识库，展开时加载，可手工刷新；它不是完整设定或最终提示词预览。 |
| `frontend/src/api/types.ts:2755`、`backend/app/schemas/memory.py:116`、`backend/app/api/v1/memories.py:96` | `/chapters/{id}/context-preview` 仅返回 `{knowledge, memories}`，路由依赖 `EmbedEnvDep`，委托 `retrieval_preview`。建议新增独立的设定预览读取，避免“查看按章设定”依赖向量配置／检索调用。 |
| `frontend/src/pages/project/ChapterEditorPage.tsx:94`、`:738` | 编辑器用 `key={chapterId}` 重建章级组件，现有 ContextPreviewPanel 因此在切章时卸载。若新预览放在角色／世界观抽屉内，它不会天然获得该保护。 |

### 2. 最小可复用界面建议

以下是供主设计采用的建议，未替代用户对同章生效等产品语义的确认。

1. **基础设定、剧情变化、按章查看各有明确入口。** 角色保留现有基础／描写／状态标签，新增「剧情变化」标签；世界观在现有详情中加入同一变化面板。新建对象先保存，得到稳定 ID 后才维护变化和自定义字段。
2. **`SettingProgressionsPanel`（新组件）**：接收 `projectId`、类型明确的目标引用、`onChanged`；承担列表加载、字段筛选、新增／编辑／删除变化及预览选择。独立保存，不嵌入角色已有 Form 中形成嵌套 HTML form。组件按 `projectId + target.kind + target.id` 重建或清空局部请求与草稿。
3. **`ProgressionEditModal`（新组件）**：一次只改一个字段；顺序是目标字段 → 生效章节／边界 → 追加或替换 → 值 → 备注。追加仅适用于文本；存活状态、关系状态／亲密度／类型、职业阶段等使用对应类型控件且只提供合法操作。字段清单由统一契约给出，不在各面板重复手写允许字段。
4. **`ChapterPositionInput`（可复用）**：显示“第 N 章 · 标题”，可搜索；章节存在时提供跳转。若主设计保留尚未建章的数字锚点，应允许输入并标“第 N 章（尚未创建）”，不能伪造可跳转的 chapter ID。若改为仅关联已有章节，旧的未来章节历史需明确显示处理方式。
5. **变化列表**：故事顺序和同章顺序由服务端返回；每行显示字段名、追加／替换、生效位置、来源、内容摘要及操作。用“记录于”标注墙钟时间，避免把创建时间误当剧情顺序；旧关系／职业审计历史继续只读。
6. **`EffectiveSettingPreview`（新组件）**：按选定章节请求服务端求值，展示基础值、截至位置的有效值、涉及的变化及必要的旧数据提示。前端不自行重放历史，避免与生成算法出现两套口径。可以复用 `TextDiff` 查看某一文本字段的基础→有效差异，但默认直接读文本已经足够，不必额外引入图表库。
7. **最小文本自定义字段**：条目内增加“字段名 + 基础文本”，使用稳定字段 ID 关联变化；改名不改 ID。首版不提供字段类型选择、全作品类型模板、引用字段、下拉、自定义分类或可见性策略。
8. **关系／职业表单**：复用现有视角称呼、阶段选择、状态标签；新增明确的“修订基础设定／记录剧情变化”意图或提供独立变化入口。快捷换阶应打开带章节信息的小表单；卸下也应区分剧情上的卸下与删除误建数据，不能让两个动作共用含糊的 DELETE。
9. **章节编辑器**：可在现有 AI 区放置“本章有效设定”折叠面板，接同一只读设定求值接口；正文生成仍由后端决定实际选角与裁剪，界面须说明预览范围。若仅展示某个条目的有效值，不将其称作“最终完整提示词”。

### 3. API 形状建议与边界

最终路径和字段名由后端设计统一确定。建议遵守以下最小契约，而非把旧历史展示 JSON 直接暴露成可写输入。

- `GET /projects/{pid}/.../{target_id}/progressions`：目标的可维护剧情变化；可带字段筛选。输出中基础值、变化列表和按章求值应分开命名。
- `POST/PATCH/DELETE` 演变资源：明确目标引用、字段引用、生效位置、文本模式／结构化操作、值、备注；更新使用服务端版本或等价的冲突检测，防止两个编辑窗口静默覆盖。
- `GET /projects/{pid}/.../{target_id}/effective?chapter_id=...` 或统一 equivalent：返回确认过的故事位置、有效字段、关联变化 ID、数据完整性提示；服务端统一执行权限和目标归属校验。读取不触发 AI、不修改设定。
- 自定义字段 CRUD 独立于演变 CRUD：稳定 `id`、`label`、`base_value`；改名时保留引用。删除一个有变化的字段必须有明确冲突／级联契约和 UI 说明，不能留下悬空变化。
- 建议的字段引用是判别联合，例如 `{kind: 'builtin', key: 'appearance'}` 或 `{kind: 'custom', id: 42}`；结构化关系／职业目标使用它们自己的明确类型，避免“任何字符串 key + unknown value”允许任意 ORM 列写入。
- 追加／替换值也需有清晰空值语义：替换为空可表示清空文本；追加全空白可以拒绝；PATCH 的省略、null 与空串应在 Pydantic 和 TS 类型中一致，不能由各表单自行猜测。
- 创建／修改结果应包含服务端归一化后的记录与版本，客户端随后重新请求求值结果；不能将 `after` 字段当作全实体最新值。
- 章节列表只用于展示与定位；API 的正式引用究竟是 `chapter_id`、`chapter_no` 或二者之一，必须由后端设计统一。不要双向由 UI 推导转换而隐藏插章／删章规则。

现有约定可复用：

- 传输类型集中在 `frontend/src/api/types.ts`，snake_case 镜像后端 schema；本地表单类型留组件或专门表单模块（`.trellis/spec/frontend/type-safety.md:19`）。
- 请求集中 `src/api/`，现有 `request` 支持 `AbortSignal`，`ApiError` 保留结构化响应体；需要可取消 GET 时用 `request(path, {query, signal})`，不要绕开 fetch 包装器（`frontend/src/api/client.ts:96`、`:126`）。
- 冲突标记按 `ApiError.body.code` 分支，不根据中文 detail 字符串猜测；409 保留用户草稿并提供重新读取／比较，不自动重发业务写入（现有规范 `.trellis/spec/frontend/component-guidelines.md:418`）。
- 关系视角复用 `typeOptions/parseTypeOptionValue/perspective`（`frontend/src/api/relationships.ts:96`、`:119`、`:128`），职业展示复用 `stageLabel/formatAssignment/formatCareerChange`（`frontend/src/api/careers.ts:82`、`:89`、`:94`）。

### 4. 只读权限、异步响应与刷新

**已有权限事实**：

- `projectAccess` 区分项目拥有者与管理员查看他人作品；他人作品的人工读取／编辑由 `projects.read/projects.edit` 控制，`canUseAI` 始终为 false（`frontend/src/utils/projectAccess.ts:5`）。演变维护属于人工设定操作，使用 `canEdit`，不能误放在 `canUseAI` 下。
- `useProjectAccess.canEditNow/canReadNow` 在调用时检查卸载、当前项目、登录用户、管理权限修订（`frontend/src/hooks/useProjectAccess.ts:14`）。因此输入禁用之外，提交前、验证 await 后、请求前和响应后仍需复核。新增的模式切换、删除／排序／快捷按钮都受同一约束。
- `useLoader` 用 effect 的 `alive` 避免旧 fn 响应覆盖新 fn，但切换时保留旧 data，且不自动清空实体编辑草稿（`frontend/src/hooks/useLoader.ts:19`）。不能把它当完整的目标切换隔离。
- `useAuthorizationScope` 有请求代次及身份校验，可参考其 `begin/current` 模式；它本身没有项目／实体／章节键，不能直接代替这些维度的检查（`frontend/src/hooks/useAuthorizationScope.ts:8`）。

**本任务必需的处理**：

1. 用目标实体 + 章节 + 当前请求代次区分求值预览；快速切换第 5／20 章时，较慢的第 5 章响应不得显示在第 20 章标题下。切换时清空旧结果或明确标为旧结果。
2. 同一 Modal 从角色 A 切到 B、关闭后重开、项目／身份变化时，旧读写响应不更新当前表单、不关闭新弹窗、不覆盖错误／loading 状态。
3. 变化保存只刷新相关列表、徽标和求值结果，不整体 `setFieldsValue` 未保存的基础表单。基础修改采用明确基础快照及脏字段 PATCH／版本检查；不能将已经过时间线合成的展示值重新保存成基线。
4. 复用 `invalidateProject` 广播：角色变化至少失效 characters，关系失效 relationships，职业失效 careers/characters，世界观失效 world；求值面板同时订阅与其结果相关的资源和 chapters。现有资源联合与广播位于 `frontend/src/utils/projectRefresh.ts:1`；按需扩充一个 progression 资源也可以，但不能只发新资源而忘记原角色卡／职业徽标。
5. 仅查看的作者／管理员仍可选择章节和看变化；所有写按钮和字段编辑禁用。独立设定预览不挂载模型配置、私人日志或任务接续。

### 5. 需要主规划明示的产品语义

这些问题不能仅由现有界面推断；可在最终方案中统一展示，避免分别询问纯技术细节。

- **同章变化**：第 20 章发生的晋升，在重写第 20 章时应表现为章初旧境界、章末新境界，还是整章使用新值；分析回写与手工“从本章起”是否区分。字段文案和预览位置必须与生成规则完全一致。
- **基础编辑与剧情变化**：修改“当前状态”或直接换阶时，是修正整本书的基础事实，还是记一次某章变化；不能继续沿用模糊的“发生章节（可选，仅记历史）”。
- **未来章和删章**：新演变能否绑定尚未创建的章节；删除目标章节后如何显示关联。旧界面允许输入任意正章节号，不等于用户已确认新演变也必须如此。
- **旧历史可维护性**：目前历史日志没有编辑能力；建议保留为只读，另维护规范化演变。若旧数据无法还原，应显示局部提示，而不是把未知的早期状态展示成可信事实。
- **自定义字段删除**：有演变引用时拒绝删除，或明确确认后连同演变删除；两者都会影响实际用户数据，应在最终契约中写明。

### 6. 测试与验收建议

前端现有命令：在 `frontend/` 运行 `npm run lint && npm run build && npm test`。脚本见 `frontend/package.json`；Vitest 使用 node 环境及 `src/**/*.test.ts`（`.trellis/spec/frontend/quality-guidelines.md:9`）。本研究未运行测试，没有产品改动。

| 验证层 | 有意义的用例 |
| --- | --- |
| API 与类型 | 新增接口的路径、方法、故事位置参数、字段引用、追加／替换载荷；更新时省略/null/空串；冲突 code 保留；读取不触发 AI。现有模式：`frontend/src/api/relationships.test.ts:29`、`frontend/src/api/careers.test.ts:170`。 |
| 表单纯逻辑 | 文本字段允许追加／替换，结构化字段仅合法替换；基础修订不带时间线派生值；仅修改名称不会发旧 current_state；自定义字段改名保留 ID；空替换和空追加按统一规则处理。 |
| 请求失效 | 延迟第 5 章响应晚于第 20 章；切换对象／项目／关闭弹窗／同 actor 撤权后旧成功或错误响应无效；409 不丢草稿也不自动重试。可按现有独立 controller／request-gate 模式测试，不要求引入新的组件测试框架。 |
| 真实浏览器 | 角色／地点／物品基础设定、追加、替换、编辑、删除及按章预览；关系／职业快捷入口；没有章节／没有变化／字段被删除的空态；只读人工查看；375px 与暗色主题；切换对象不串数据。 |
| 跨层验收 | 第 20 章晋升后第 5 章的 UI 有效设定与服务端生成上下文一致；基础草稿未保存时在另一个标签修改变化，不清空草稿且不会在随后保存时污染时间线。 |

## Related Specs

- `.trellis/spec/frontend/component-guidelines.md`：实体组件复用、只读权限、响应式、主题、冲突处理。
- `.trellis/spec/frontend/type-safety.md`：TS 镜像 schema、PATCH 空值、unknown 收窄。
- `.trellis/spec/frontend/hook-guidelines.md`、`.trellis/spec/frontend/state-management.md`：局部状态与 loader／任务边界。
- `.trellis/spec/frontend/quality-guidelines.md`：lint/build/test、node 测试、包体积、禁止模式。
- `.trellis/spec/backend/story-entities.md`、`.trellis/spec/backend/rbac-quotas.md`、`.trellis/spec/backend/admin-projects.md`：关系／职业和人工项目权限的跨层约束。
- `.trellis/spec/guides/cross-layer-thinking-guide.md`：同一 payload 与求值逻辑的唯一所有者。

## External References

本研究仅依赖当前源码和项目规范，无新增外部库或第三方 API 调研。Novelcrafter 的产品语义参考已由主会话记录在 PRD；本文件不将其当作本项目已存在的行为。

## Caveats / Not Found

- 没有发现可直接复用的设定时间线、文本自定义字段框架或完整设定上下文预览。
- 没有修改产品代码、测试、规范或 Git 状态；唯一产物是本研究文件。
- 本文件的接口／组件名称为设计建议，不是实现承诺；后端回放、迁移、删除和备份语义由另两份研究及主设计收敛。
