# Research: 自定义设定字段、条目标签与自动分类的前端/API入口

- Query: 调查四类设定面板、字段与剧情变化编辑、作品布局/路由、实体深链、权限、CAS及草稿模式；提出作品级字段配置、四类控件、统一条目标签、非嵌套ANY/ALL分类规则及按时点跨类型结果的最小可用交互。
- Scope: mixed（以仓库内部源码/规范为主，补充 Ant Design 5 官方文档）
- Date: 2026-11-05
- Active task: .trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories
- 研究范围：只写本文件；代码、spec、子任务、备份及核心resolver设计均不修改。以下“建议”是待主会话最终方案审核的设计，不能当作已实现能力。

## Findings

### 1. 已确认范围与本研究的判断边界

- PRD R1–R5已确认：多行文本、单行文本、单选下拉、设定条目引用；四类字段都支持按章回溯；作品内按设定类型复用配置；覆盖组织和职业；按标签/下拉字段规则自动归类。PRD仍处于规划期。
- 主会话进一步明确：两个顺序子任务为 11-05-typed-setting-fields、11-05-setting-auto-categories；父任务统一保存规划与research。本研究的模块边界按“字段先交付，分类复用字段契约”组织。
- 主会话明确标签只作当前整理元数据，不扩展成时间线字段。历史分类因此是“当前标签 + 所选章节的下拉有效值”；界面必须明确口径，不能声称全部条件都恢复到历史时点。
- 非嵌套、多条件ANY/ALL是建议在最终方案中明示审批的能力，尚非已批准实现。本文不扩展自定义数字、公式、嵌套逻辑、跨作品引用、独立场景、富文本、共享模板或AI可见策略。

### 2. 找到的文件

| 文件 | 作用 |
| --- | --- |
| frontend/src/pages/project/CharactersPanel.tsx | 角色列表、基础表单、剧情变化Tab、请求代次与角色草稿缓存。 |
| frontend/src/pages/project/WorldPanel.tsx | 世界观固定分类、基础内容弹窗、剧情变化/提及Tab。 |
| frontend/src/pages/project/OrganizationsPanel.tsx | 组织树、组织资料编辑、成员与历史；没有组织自身字段时间线UI。 |
| frontend/src/pages/project/CareersPanel.tsx | 主修/副修职业定义卡片及详情深链。 |
| frontend/src/components/careers/CareerEditDrawer.tsx | 职业定义、阶段阶梯、持有人；没有职业定义自身字段时间线UI。 |
| frontend/src/components/progressions/SettingProgressionsPanel.tsx | 自定义文本基础值、事件列表、按章有效值的现有组合入口。 |
| frontend/src/components/progressions/TextFieldsPanel.tsx | 条目级文本字段列表/基础值及新增、编辑、删除入口。 |
| frontend/src/components/progressions/TextFieldModal.tsx | 文本字段草稿、409后刷新对照与覆盖确认。 |
| frontend/src/components/progressions/ProgressionEditModal.tsx | 变化编辑、冲突后原草稿及最新事件对照。 |
| frontend/src/components/progressions/ProgressionEditorFields.tsx | 字段、位置、追加/替换、当前三种输入控件。 |
| frontend/src/components/progressions/fields.ts | 字段选择、事件草稿/校验、事件与字段冲突比较纯逻辑。 |
| frontend/src/components/progressions/useProgressionPanelData.ts | 同版本读取、身份/作品/主体/页码隔离及abort。 |
| frontend/src/components/progressions/EffectiveSettingPreview.tsx | 按章节章初/章末只读求值及revision检查。 |
| frontend/src/components/progressions/ChapterPositionInput.tsx | 已有章节ID、计划章节号、章初/章末控件。 |
| frontend/src/components/progressions/useWorldBaselineEditor.ts | 世界观基础编辑的CAS、元数据对照、关闭和切换失效。 |
| frontend/src/components/progressions/worldSnapshot.ts | 基础值/元数据一致快照读取。 |
| frontend/src/components/entities/characterForm.ts | 基础快照转换、只提交差异、会话内草稿与request gate。 |
| frontend/src/components/planning/PlanningFields.tsx | 分页设定选择器和规划标签选择器，可复用交互但不能混用业务身份。 |
| frontend/src/components/planning/PlanningCellDrawer.tsx | 已有四种实体与关系的详情URL映射。 |
| frontend/src/hooks/useEntityDeepLink.ts | 按?id复用页面原打开流程；目前不携带章节时点。 |
| frontend/src/utils/mentions.ts | 严格正ID、深链加载状态及当前角色/世界观URL生成。 |
| frontend/src/router.tsx、components/StudioNavigation.tsx | 作品内路由及桌面/移动共用导航定义。 |
| frontend/src/pages/project/ProjectLayout.tsx | 作品加载、人工管理/只读提示及按作品重建Outlet。 |
| frontend/src/hooks/useProjectAccess.ts、utils/projectAccess.ts | 实时read/edit校验、真实actor与owner边界、AI能力。 |
| frontend/src/hooks/useLoader.ts、useMentionQuery.ts | 页面加载与按身份/查询隔离的可中止查询。 |
| frontend/src/utils/projectRefresh.ts | 资源失效广播，不持有业务缓存。 |
| frontend/src/api/progressions.ts、story.ts、organizations.ts、careers.ts | 现有领域API客户端入口。 |
| frontend/src/api/types.ts、client.ts | 传输类型和Cookie/错误/abort统一边界。 |
| backend/app/api/v1/progressions.py | 现有基础值、事件、有效值和text-fields路由。 |
| backend/app/services/project_service.py、setting_progression_service.py | 人工读写权限及现有stale_revision错误来源。 |
| backend/app/services/planning_queries.py、planning_service.py | 跨类型身份目录查询与分页实现的复用候选。 |
| backend/app/schemas/story.py、organization.py、career.py | 固定设定类型及现有主体输入；未有统一条目标签。 |

### 3. 现有面板与入口事实

| 设定 | 当前入口与证据 | 本任务接入点 |
| --- | --- | --- |
| 角色 | CharactersPanel.tsx:464抽屉；:549基础/描写/状态共用Form；:587按需挂载SettingProgressionsPanel，target为character。 | 保留现有基础资料和其他关系Tab；把新动态字段值接入独立字段区，事件/按章值继续走相同主体。 |
| 世界观 | WorldPanel.tsx:179为基础设定/剧情变化Tab；:242固定分类；:264挂载world主体变化面板。 | 为适用的世界观类型显示共享字段，保持基础内容表单草稿与字段/事件各自保存；固定分类继续保留。 |
| 组织 | OrganizationsPanel.tsx:179组织树与详情分栏；:210编辑Modal；:285组织详情；:328的“成员与历史”是角色成员记录。 | 在组织详情补“组织自身字段/剧情变化”区，不能把成员历史当作组织属性历史。树选中后仍复用同一OrgDetail。 |
| 职业 | CareersPanel.tsx:148打开CareerEditDrawer；CareerEditDrawer.tsx:118编辑职业定义，:173阶段阶梯，:180持有人。 | 在职业定义抽屉补“职业自身字段/剧情变化”区；保留阶段及持有人，不把自定义字段写到某个角色的职业挂接上。 |

重要的同名差异：frontend/src/api/types.ts:3381的ProgressionSubject中，career需要character_id + career_id，membership需要character_id + organization_id；没有organization主体，也没有单独career definition主体。useProgressionPanelData.ts:19进一步只接受character/world。新增组织/职业定义字段必须先由字段子任务提供明确主体契约，前端不能仅把target字符串替换成career而继续发送错误身份。

世界观的现有固定类型为place/faction/item/rule/history/other，标签分别为地点/势力/物品/规则/历史/其他（frontend/src/utils/world.ts:3；backend/app/schemas/story.py:7）。建议配置选择器明确列出角色、世界观各固定子类、组织、职业，不把组织org_type自由文本、角色role_type或职业主修/副修隐式变成新的配置命名空间；主方案如采用世界观整体共享，应在最终范围中明确该粒度。

### 4. 当前字段/时间线模式及必须保留的交互

- 当前字段是条目私有：SettingTextField只有id/timeline_id/label/base_value/sort，base_value为string（frontend/src/api/types.ts:3461）。现有/text-fields路径也是timeline下资源（frontend/src/api/progressions.ts:97；backend/app/api/v1/progressions.py:145）。它不是作品级字段定义API。
- 当前基础值与事件已独立于外层角色/世界观表单：SettingProgressionsPanel.tsx:37说明刷新不回填外层草稿，:172渲染文本字段，:183事件列表，:230有效值预览。最小侵入路线可沿用“基础字段值 → 剧情变化 → 按章预览”的顺序，新增作品配置入口与类型控件，不重写四个完整详情。
- 当前fieldChoices把全部custom字段作为multiline且允许append（fields.ts:54）；ProgressionDraft.value限定string（:25）。ProgressionEditorFields.tsx:64只区分life_status、short_text和TextArea。事件回填也只保留string值（ProgressionEditModal.tsx:21）。因此四类支持不能只新增一个Select外观，必须同步draft、序列化、校验、回填和展示。
- 现有有效值custom_fields是Record<string,string>（api/types.ts:3480）；preview.ts:19直接取字符串。事件列表SettingProgressionsPanel.tsx:32对非字符串JSON.stringify。下拉和引用需要共享“按定义渲染值”的组件，把选项/条目显示为名称与状态，不能让产品界面显示原始JSON、option_id或对象字符串。
- ChapterPositionInput.tsx:13区分已有章节ID和计划章号，:63展示章初/章末；EffectiveSettingPreview.tsx:39的requestKey含身份、作品、timeline、revision、位置，:52拒绝非当前revision结果。四类控件与分类时点必须复用这套概念。
- “当前档案”按规范是应用全部有效变化后的结果；它不等于当前正在写的章节或最后一个已写章节。基础值编辑必须显式标“故事开始时的基础值”，按章预览必须显示位置；不能从列表当前投影反填基础值（characterForm.ts:100；CharactersPanel.tsx:266；.trellis/spec/backend/setting-progressions.md:29）。

### 5. 推荐最小可用作品配置与条目编辑UI

#### 5.1 作品字段配置

建议新增作品内懒加载页，候选URL为/projects/:pid/setting-fields；名称为“字段配置”，放在故事档案导航附近。四类面板工具栏及条目字段空态均提供“配置此类型字段”链接，并预选类型。配置属于作品，不能放到用户级/settings/ai，也不能只放在某条目的新增字段按钮里。

页面只需：类型选择 → 字段清单 → 新增/编辑对话框。字段行显示名称、类型、排序、下拉选项/引用范围摘要、启用状态；可用上下移动排序，不增加拖拽依赖。对话框按类型补下拉选项或允许引用的设定类型。此页编辑“哪些字段可用”；条目详情只编辑“这个条目的值”，两者使用不同保存按钮和草稿。

新增共享字段后，同类型现有/未来条目立即看到空字段，作者逐条设置基础值；不能把某条目的值当成共享默认值，也不能因为新增定义就为所有早期章节自动写入非空内容。MVP不加入默认值、必填开关或批量历史回填。

旧文本字段保留稳定身份和原变化关联，可在详情标“本条目原有字段”；不要按同名自动合并为共享字段。是否提供显式提升为共享字段的迁移UI由核心方案决定，不能为接入新配置而先要求作者重建旧字段。

#### 5.2 四类值控件

| 字段类型 | 基础值/变化控件 | 变化方式 | 展示与清空 |
| --- | --- | --- | --- |
| 多行文本 | Input.TextArea，按内容自适应高度。 | 保留追加/替换。 | 纯文本保留换行；替换为空可清空，追加必须有内容。 |
| 单行文本 | Input，使用定义约束与码点长度校验。 | 仅替换。 | 显示文本；空值明确为“未设置”，不能把上一字段的内容带入。 |
| 单选下拉 | Select单选，候选来自字段定义，value为稳定选项身份。 | 仅替换/清空。 | 显示选项名称；改名不改身份，停用有文字状态。 |
| 设定条目引用 | 允许目标类型选择 + 搜索/分页的单条目选择器。 | 仅替换/清空。 | “类型 · 名称”及可用详情链接；保留同名条目的类型区分。 |

基础值、事件详情、有效值和分类匹配原因共用同一值格式化规则。引用MVP面向本作品内角色、世界观、组织、职业条目；不要为了复用规划选择器顺带把人物关系、知识库、章节或外部URL扩成引用对象。

字段值区独立持有baseline快照、定义快照、dirty和保存状态；现有外层Form保存不能顺手提交它，也不能被它的reload覆盖。独立组件若嵌入外层角色Form，应采用不生成form节点的字段布局或放在Form外，避免嵌套HTML form；现有ProgressionEditorFields.tsx:16已有此模式。

组织与职业在首次保存真实条目后才展示字段时间线操作；新增未保存条目显示“保存后可维护字段与剧情变化”。组织的新区必须和“成员与历史”明确分开，职业的新区必须和“角色挂接/阶段历史”分开。

#### 5.3 统一条目标签

四类详情都提供“整理标签”，列表/分类结果以Tag展示并可按标签筛选。标签来自作品内一个设定条目标签目录，规则引用稳定标签身份；已有别名Select mode=tags不能当成条目标签，别名仍用于提及匹配。规划标签和作品题材标签也不能无提示复用。

标签编辑明确“用于当前整理，不按章节回溯”；可复用既有多选搜索和创建交互，但不产生剧情变化事件。标签改名保留规则引用；删除仍被分类规则引用的标签应提示具体分类并先解除依赖或停用，不能静默把条件删掉。标签保存成功后刷新相应条目与分类结果，不能只改页面Tag。

### 6. 推荐自动分类编辑及结果页

建议新增作品内懒加载页，候选URL为/projects/:pid/setting-categories；导航名“设定分类”。同页显示分类清单、匹配结果和“新增/编辑规则”，不增加目录拖拽或手工拖入分类。字段配置页与分类页彼此有明确链接；标签目录管理可用此页的轻量对话框，不必再建一个路由。

#### 6.1 单层规则表单

- 分类名称；适用设定类型（可选多个）；“满足全部条件（ALL）/满足任一条件（ANY）”；可增删的条件行。
- 条件只提供“包含某标签”或“某类型的某下拉字段 等于 某选项”。一行选择一个标签/选项，多项用多行表达；MVP不提供NOT、文本包含、单行文本判定、引用遍历、嵌套组或表达式输入框。
- 下拉字段选择显示“设定类型 / 字段名”，用稳定字段与选项身份保存。不同类型同名字段不能被当成一个字段；其他类型没有该字段时该行条件为false，不是空值匹配。
- 至少一行有效条件；ANY/ALL都不能用零行变成全匹配。类型范围与某条件不兼容时，表单提示具体行；无效条件不能被悄悄忽略后继续匹配。
- 规则表单可只读预览匹配数量和条目样本，注明“未保存规则预览”；预览不得写入规则、标签或条目。保存后再读取服务端结果。
- 一个条目可同时出现在多个分类，分类并非互斥归属；规则编辑/分类删除只改变组织视图，不修改现有world.category、角色定位、职业主副修、组织树、条目内容及历史。

#### 6.2 跨类型结果与历史口径

默认“当前值”，旁边可切“按章节查看”，复用已有/计划章节 + 章初/章末选择。当前值的说明要与最终resolver一致，不能暗示“最后写作章”；若按现有投影语义包含计划章节的变化，应明确说明这一点。

结果行只需：类型、名称、标签、匹配原因（例如“阵营 = 青云宗”），以及打开详情；桌面可用Table，窄容器用List卡片，避免展示每个类型全部内置字段。服务端负责跨类型筛选、去重、计数和分页；前端不能先取每类第一页再合并，也不能按每行调用effective组成N+1。筛选/时点改变回到第1页，空结果说明“该条件/时点下暂无匹配条目”，与加载失败区分。

历史模式固定提示：“下拉字段按所选章节计算，标签按当前整理信息。”名称、字段标签、选项名称为当前命名元数据，历史回溯的是值身份及生效位置，不承诺恢复过去的名称。标签修改后早期章节的标签条件结果也会改变；这是已明确的范围，不应通过偷偷增加标签时间线去修补。

第20章章末阵营由A改成B：当前分类B可命中；第5章、第20章章初分类A命中；第20章章末、第21章分类B命中。分类的下拉条件必须使用与有效值预览相同的resolver，不能从当前列表对象比较。

#### 6.3 详情跳转与深链

- 四个面板已支持?id入口：CharactersPanel.tsx:219、WorldPanel.tsx:64、OrganizationsPanel.tsx:112、CareersPanel.tsx:38。组织打开的是树选中详情，其他类型打开已有Modal/Drawer，继续复用各自的原打开流程。
- 当前entityHref只将character映射characters，其余映射world（frontend/src/utils/mentions.ts:32）；不能直接喂organization/career。PlanningCellDrawer.tsx:18已有四类映射，应抽为独立设定条目URL帮助函数，供分类/引用/规划使用，别扩大提及系统的MentionEntity范围。
- 历史分类结果跳转要携带经过校验的时点和“查看有效值”的意图，例如?id=42&view=effective&chapter_id=5&boundary=start；计划章节使用chapter_no，二者互斥。该参数形状是建议，最终与统一契约对齐。
- useEntityDeepLink.ts:7目前只消费id、调用onOpen(item)，打开后默认角色basic/世界观base。需扩展独立payload/初始预览参数，使历史跳转进入已有剧情变化/有效值区域并预选相同位置；不能在列表显示第5章，打开详情却直接显示最新下拉值。保持只读查看，不能把历史快照自动复制到基础编辑表单。
- 加载失败保留深链，待重试后消费；只有成功加载并确认缺失才提示“条目不存在或已被删除”。字符ID要经readPositiveId（utils/mentions.ts:3），切换时点/分类/作品不能由旧查询参数反复打开旧目标。缺失章节明确提示重选，不能静默退到当前值。
- 返回分类时保留分类、筛选、时点和页码；普通Link导航仍须经过现有未保存草稿保护。

### 7. 字段变更、失效数据和草稿的UX约束

| 场景 | 必要行为 |
| --- | --- |
| 字段改名、排序 | 稳定字段身份不变，已有值、事件和规则继续关联；重排不重置动态控件草稿。不要用label或数组index作为React key。 |
| 字段类型改变 | MVP建议创建后类型不可变；至少在有任何值/事件/规则后禁止原位改变。若未来支持转换，需显式迁移，不能把引用对象强转文本或删选项后自动丢值。 |
| 字段停用/删除 | 停用与删除分开；停用不抹历史。被历史事件或规则使用时删除应有明确依赖提示，不要求作者先盲删历史。只读历史仍能显示原字段与值。 |
| 下拉选项改名 | 保留选项ID，当前值/历史值和规则按ID继续成立，不以名字重新匹配。 |
| 下拉选项停用 | 值编辑不允许新选停用项；已有选中值和历史仍显示“名称（已停用）”，不自动清空。规则查询仍可保留/选择停用选项以查询历史，文字标明状态。 |
| 定义编辑期间选项变化 | 读取最新定义后保留作者草稿并标出冲突字段；已选项失效要指出该行并阻止新提交，不能偷偷改到第一项。 |
| 引用目标改名 | 通过稳定kind/id显示当前合法名称，源字段事件不改写；同名条目需类型标识。 |
| 引用目标已删除 | 保留原引用身份并显示不可点击的“条目已删除”状态，由作者显式清空/替换；不能把未知引用当null。只有后端安全返回的已存摘要可显示，不跨作品查名称。 |
| 引用未在当前分页中 | 单独解析已选身份/安全摘要；不能把“本页没找到”当删除。PlanningEntrySelect.tsx:19的“已选条目 #ID”兜底可借鉴，但不足以区分加载失败/真实删除。 |
| 引用查询失败 | 保留原值，显示重试；不返回空候选冒充没有条目。搜索换词/换类型时丢弃旧页，已选值不随搜索消失。 |
| 世界观条目改变固定类型 | 若配置按世界观子类区分，应提示适用字段变化；已有字段值/历史不能静默丢失。MVP可在存在受影响值/历史时阻止类型转换，待有明确迁移策略后再开放。 |
| 切换变化字段类型 | 新事件中切换字段时显式清空不兼容值或按字段保留本地草稿；不能将上个引用/下拉值自动带入文本或另一组选项。 |
| 只读及失写权限 | 可看配置、值、历史和分类；所有写按钮、行操作、Form外控件、Popconfirm与提交函数都受canEdit/canEditNow保护。失写保留合法只读内容，失读/换账号立即撤下内容与弹窗。 |
| 409 CAS冲突 | 保留本地草稿与最初快照；读取最新值/定义/规则供比较，只有明确确认后才使用新版本重提。不能只刷新revision后自动覆盖；不能把所有409都当stale_revision。 |
| 网络失败/提交结果未知 | 明确失败或待核对状态，草稿保留；禁止自动重放创建或覆盖操作。先重读服务端确认后再决定提交。 |
| 关闭、切条目/类型、跳配置页 | 脏草稿必须会话内保留，或经明确放弃确认；不能靠destroyOnHidden丢弃。内存草稿按真实actor/作品/主体/定义或规则划分，退出、失读或作用域变化清理，不加localStorage业务草稿持久化。 |
| 快速切换与迟到响应 | query key包含actor/授权快照、pid、主体/分类、筛选、页码、时点、定义/规则版本及请求代次。abort之外仍校验key与canReadNow/canEditNow；旧响应不回填、不toast、不关闭新弹窗。 |

CAS与草稿的现有证据：

- CharactersPanel.tsx:144读取基础快照，:161比较基线变化而保留草稿；:193切角色先rememberDraft；:221提交前后核对ticket及实时权限；:273只对code=stale_revision阻塞重提；:467关闭时保留角色草稿。缓存与gate纯逻辑在characterForm.ts:135、:165。
- 世界观useWorldBaselineEditor.ts:53区分replace/reconcile；:68同时比较基础与元数据；:158收到stale_revision保留窗口草稿。但:89重新openEditor会重新填表、:112关闭仅关会话，并无角色式按条目草稿缓存。不能假设四个面板已有统一关闭/切换保护。
- ProgressionEditModal.tsx:71读取最新事件只更新版本/对照，:111被删除保留草稿，:121明确比较后才能覆盖；TextFieldModal.tsx:51同类模式。两个弹窗的取消当前会卸载实例，因此“冲突时保留”不等于“关闭重开仍保留”；新增配置/规则需自行实现明确的离开保护。
- useProgressionPanelData.ts:41并行读baseline/progressions/fields并在:47检查同revision；:78清理所有pending请求；:115显式刷新不卸载编辑器。新字段定义和条目值也需要同一可验证快照，不能以多次独立响应随便拼接。
- useLoader.ts:19只阻止旧effect交付，失败保留旧data；useMentionQuery.ts:6把身份/查询key纳入可见性。跨时点分类/引用搜索更接近后者，不能让旧数据继续显示在新筛选标题下。

### 8. API入口及前后端契约建议

以下是能力边界，候选路径不是已存在API，也不替代核心研究对持久化、主体kind和ID映射的结论。

| 能力 | 现状 | 最小新增/扩展合同 |
| --- | --- | --- |
| 作品/类型字段定义 | 只有timeline/text-fields。 | 作品下字段定义的读/增/改/停用/安全删除；按稳定设定类型筛选；返回定义、选项、状态和配置revision。候选集合名setting-fields。 |
| 条目基础值 | 内置baseline与私有文本base_value。 | 根据同项目实体解析真实主体，返回类型化值、定义元数据及timeline revision；独立CAS修改，只提交作者改动的值。共享定义ID不能不经适配直接冒充旧custom_field_id。 |
| 类型化剧情变化 | 现有/progressions可承载JSON值，但前端草稿限定string。 | 扩展字段选择元数据/判别联合草稿/输入校验和回填；继续用稳定字段身份、明确位置与expected_revision。 |
| 有效值 | /effective必须有chapter_id或chapter_no（api/progressions.ts:80）。 | 四类字段和新增条目主体都返回同一求值口径；“当前值”需有明确合同，不能给不存在的章节ID或挑最后一章伪装。 |
| 引用目录与已选解析 | planning/entries已有分页目录。 | 只返回允许类型、安全身份摘要及可用状态；支持q/type/page及已选ID解析。可以抽取规划的共同目录查询，避免新字段依赖planning revision、关系能力及规划标签。 |
| 条目标签 | 设定schema没有统一标签。 | 作品级标签目录 + 四类条目当前标签读写；稳定标签身份、独立并发保护和引用校验；不产生时间线事件。 |
| 分类规则 | 没有自动分类API。 | 作品级CRUD/CAS，单层ANY/ALL、类型范围与稳定标签/字段/选项身份；服务端校验整份规则。 |
| 分类预览/结果 | 不能由固定world.category/role_type代替。 | 一个有界结果入口接收category或未保存draft + 当前/章节位置、筛选和分页；返回items/total、实际位置、匹配原因、规则/数据快照标识及安全状态。预览只读。 |

基础API模式：统一走src/api/client.ts:103的request/rawRequest，保留Cookie和AbortSignal；错误从ApiError.body按code收窄（:5、:88），页面只显示errorMessage，401/授权403沿用现有集中处理。新传输类型放api/types.ts，明确null清空/省略不变，不能用JSON.stringify把选项/引用藏入多行文本。

服务器人工管理边界：get_manageable_project默认projects.read，写须显式lock + projects.edit（backend/app/services/project_service.py:39）；setting_progression_service.py:74/:78已有读/写入口。字段配置、标签、分类及引用目录均属于同一作品人工内容管理，不能因为新建路由只校验登录就跳过同作品、生命周期和真实actor权限。

跨类型目录可借鉴：PlanningEntrySelect（PlanningFields.tsx:13）通过server search/pagination加载，保留分页外选中值；planning_queries.py:65只取身份摘要。但planning_service.py:255当前是装载全目录后筛选切片，不是数据库级有界查询，分类按时点不能直接在此基础上为每行另调HTTP effective。具体批量求值/分页方案由核心研究决定，UI合同至少保证total与items同筛选、同时点、同可验证快照。

分类/字段保存后统一走invalidateProject（utils/projectRefresh.ts:1），扩展必要资源键，例如字段配置/设定标签/分类；各面板订阅对应类型和chapters。它只广播失效，不增加全局字段值store；有dirty的字段编辑器收到失效先对照，不直接回填。分类规则与结果版本、字段配置版本、主体timeline revision、authorization_revision分别有不同含义，不能相互代用。

### 9. 推荐拆分模块

名称可在实现设计中调整，职责应保持分离，不把新功能继续堆进600余行CharactersPanel或330余行OrganizationsPanel。

| 模块/目录（建议） | 职责 | 现有复用点 |
| --- | --- | --- |
| api/settingFields.ts + api/types.ts | 字段定义/类型化值合同，镜像后端schema；旧字段适配边界。 | api/progressions.ts的request、CAS。 |
| api/settingEntries.ts | 本作品条目摘要、引用候选与已选解析、统一标签API。 | planning目录查询的安全身份部分。 |
| api/settingCategories.ts | 规则CRUD、只读draft预览、按位置结果。 | api/planning.ts的查询/失效模式。 |
| components/settingFields/FieldValueControl.tsx、FieldValueView.tsx | 四类控件与统一值展示；纯value/onChange，不持有页面导航。 | ProgressionEditorFields、preview.ts。 |
| components/settingFields/SettingEntrySelect.tsx | 类型约束、搜索/分页、已选标签与失效引用。 | PlanningEntrySelect交互抽取，避免复制两套分页组件。 |
| components/settingFields/EntryFieldsPanel.tsx + useEntryFields.ts | 独立基础值草稿、主体/身份key、CAS及刷新对照。 | useProgressionPanelData、characterForm gate。 |
| components/settingFields/FieldDefinitionEditor.tsx + fields.ts | 字段配置/选项编辑；纯校验/草稿转换与界面分离。 | TextFieldModal冲突对照。 |
| pages/project/SettingFieldsPanel.tsx | 作品配置页，按类型路由筛选；懒加载。 | router.tsx:20、:102。 |
| components/settingCategories/CategoryRuleEditor.tsx + rules.ts | 单层规则行、ANY/ALL、验证与请求转换；不在前端另造权威分类求值器。 | Form/受控控件、ProgressionEditModal。 |
| components/settingCategories/CategoryResults.tsx + 查询hook | 类型化结果、匹配原因、分页、当前/章初/章末及过期响应隔离。 | useMentionQuery、ChapterPositionInput。 |
| utils/settingEntries.ts | 四类URL、类型标签、严格ID/位置解析与复合身份key。 | PlanningCellDrawer.entryLink、utils/mentions.readPositiveId。 |
| pages/project/SettingCategoriesPanel.tsx | 分类目录/编辑/结果组合；独立懒加载路由。 | StudioNavigation共用桌面/移动导航。 |
| 四个现有详情适配器 | 传真实entry identity与read/edit能力，放置公共字段/变化区，接已有reload/草稿。 | 各面板已有入口，不复制值逻辑。 |

字段子任务先提供SettingEntryRef、稳定设定类型、字段定义/值、主体求值及通用控件；分类子任务在这些公开合同上补标签/规则/结果，不再独立实现字段求值或另建组织/职业主体。

### 10. 验收场景与验证重点

| 场景 | 可观察验收 |
| --- | --- |
| A1 作品/类型复用 | 在作品A为角色新增四类字段，两角色都显示同一组定义但可保存不同值；作品B不出现；世界观各类型、组织、职业按最终定义粒度覆盖。 |
| A2 独立组织/职业主体 | 为组织自身字段“立场”和职业定义字段“传承来源”记录章节变化，无角色成员/持有人也能保存与回溯；角色加入组织/职业换阶不修改定义字段。 |
| A3 四类完整回路 | 分别保存基础值、创建变化、关闭重开、修改/删除变化、清空，再看按章结果；单行/下拉/引用无追加入口，多行追加保持换行。 |
| A4 时间边界 | 第20章章末把下拉A换B、引用甲换乙；第5章和第20章章初仍显示A/甲，第20章章末和第21章显示B/乙；正文上下文预览与分类取相同有效值。 |
| A5 非嵌套规则 | 验证两条标签/下拉条件的ANY、ALL；无该字段类型为false；空规则不能保存；同条目可命中两分类；分类不改变固定类型与组织树。 |
| A6 标签口径 | 当前标签移除后分类立即移出；选择早期章节仍按当前标签匹配，界面一直显示口径说明；下拉条件随章节变化。 |
| A7 字段/选项维护 | 字段和选项改名后旧事件、历史值、分类仍按原身份工作；停用选项保留旧值/规则可读，值编辑不可新选；有依赖删除明确阻止且列出原因。 |
| A8 引用失效 | 候选分页切换不丢已选值；同名跨类型可辨；目标改名链接正确；删除后显示失效引用且保留草稿；网络失败显示重试，不误判已删。 |
| A9 冲突与未知结果 | 双窗口分别修改基础值、定义/选项或规则；过期保存409保留原草稿，读取最新对照后才能显式重提；网络断开不自动重发创建。 |
| A10 快速切换 | 慢请求期间换作品/类型/条目/分类/页码/章初章末，旧响应不显示在新标题下；旧保存回调不关闭新弹窗、不发成功提示；关闭重开草稿保护可解释。 |
| A11 深链 | 分类结果和引用链接打开对应四类现有详情；带历史位置时预选该位置；刷新仍有效；首读失败保留目标待重试，真实删除才显示缺失；返回分类保留时点/筛选。 |
| A12 只读/撤权 | 人工只读管理可读配置、值、历史和分类；所有外层/行内/弹窗写操作均不可执行；同actor撤写不重发，失读撤下内容；不加载他人AI配置、任务输出或私有会话。 |
| A13 旧文本兼容 | 同名旧条目私有字段不被误合并；旧字段改名后事件仍可读；删除有效引用的字段不静默成功；缺失新结构的旧资料显示可解释空态。 |
| A14 响应式与键盘 | 375px及打开AgentDock后的窄内容容器无页面级横向溢出；长名称/引用/规则可换行，移动列表可读，明暗主题一致；字段label关联、纯图标aria-label、规则行删除后焦点合理。 |

有意义的单测应覆盖类型化draft/清空/序列化、合法选项/引用、规则结构校验、深链/位置解析、并发对照和旧响应丢弃，而非复制组件实现。现有测试参考fields.test.ts、preview.test.ts、worldForm.test.ts、worldSnapshot.test.ts、characterForm.test.ts、api/progressions.test.ts、utils/mentions.test.ts和settingContextPreviewController.test.ts。仓库测试环境为node，DOM交互仍需真实浏览器验证，不引入jsdom或新依赖。

实现阶段前端门禁为frontend目录的npm run lint、npm run build、npm test；再做上述真实保存/冲突/只读/深链与375px明暗主题验收。此研究未修改产品，因此本轮不运行产品测试，也不声称UI已经通过浏览器验收。

### 11. 相关规范

- .trellis/workflow.md：规划期研究写入research；未审核设计不进入实现。
- .trellis/spec/frontend/index.md：前端规范入口；API集中、页面本地状态、跨层合同。
- .trellis/spec/frontend/component-guidelines.md：受控值/onChange、App.useApp、Form外权限、设定剧情变化、主题及内容容器响应式。
- .trellis/spec/frontend/state-management.md：服务端响应为事实源，项目失效广播，不新建全局业务缓存；账号/权限/请求代次隔离。
- .trellis/spec/frontend/hook-guidelines.md：memo loader、统一API客户端、abort与读取模式。
- .trellis/spec/frontend/type-safety.md：传输类型镜像后端、判别联合、PATCH省略/null、码点长度、ApiError.body收窄。
- .trellis/spec/frontend/quality-guidelines.md：lint/build/test、1500kB chunk门禁、懒加载及真实响应式验证。
- .trellis/spec/backend/setting-progressions.md：基础/当前/按章区别、稳定身份、CAS、草稿、孤儿事件及组织成员/职业挂接语义。
- .trellis/spec/backend/story-entities.md：固定世界观类别、组织与职业体系及现有角色挂接边界。
- .trellis/spec/backend/mention-tracking.md：别名与标签不可混用；深链不吞加载失败，原详情与草稿保护。
- .trellis/spec/backend/planning.md：跨类型引用目录、规划标签的独立语义、非自动重放和规则外API复用边界。
- .trellis/spec/backend/rbac-quotas.md、admin-projects.md：真实actor/owner、人工只读范围、逐动作权限与撤权。

### 12. 外部参考与版本

frontend/package.json声明React ^18.3.1、Ant Design ^5.29.3、React Router ^6.30.6、Zustand ^5.0.16、TypeScript ~5.8；这是依赖范围，不声称本轮重新安装或升级后的实际版本。本研究建议全部用现有依赖。

- [Ant Design 5 Input](https://5x.ant.design/components/input-cn/)：核对Input与自适应TextArea；用于单行/多行控件，不引入富文本编辑器。
- [Ant Design 5 Select](https://5x.ant.design/components/select-cn/)：官方5.29.3文档支持options数据、搜索、loading、allowClear、popupRender及选中标签定制；够用来承载选项和分页条目选择。ID是传输值，名称由UI显示，不能把labelInValue中的React节点当领域值持久化。
- [Ant Design 5 Form](https://5x.ant.design/components/form-cn/)：component=false可不生成DOM节点；disabled仅覆盖AntD控件，自定义按钮/提交入口仍需显式保护；自定义受控控件应传value/onChange与id/ref。

## Caveats / Not Found

- 未发现作品级设定字段定义、统一设定条目标签或自动分类API/UI。现有world.category、角色定位、规划标签、别名均各有语义，不能当作已完成的新能力。
- 组织/职业“自身条目”的时间线尚不存在；现有career/membership是角色挂接身份。此项是字段子任务的必要前置，不是只加两个Tab即可交付。
- 四类值的身份/JSON形状、新主体kind、旧custom_field_id映射、定义revision和删除保护由核心研究定案；本文只给前端/API所需能力，不选择数据库结构。
- 字段配置粒度（尤其world整体/六子类）、世界观换类策略、停用字段/选项在规则编辑中的可用范围、当前值是否包含全部计划变化，应在最终方案显式写出。本文给出保留历史且不静默丢值的最小建议。
- 历史分类只回溯下拉值；标签是当前整理元数据，名称也是当前命名。规则本身按当前保存版本应用于历史值，不是规则编辑历史回放。
- 当前角色有会话内草稿缓存，世界观/组织/职业及事件弹窗并非统一有关闭重开草稿保护；不能将已有CAS对照误认为所有离开场景都已覆盖。
- 本轮仅静态源码/规范研究与官方组件文档核对；未启动应用、未建原型、未加依赖、未执行Git操作或产品测试。备份/AI上下文核心兼容由其他研究员覆盖，最终验收仍需跨子任务集成。
