# Research: 字段与自动分类设计的UI/API闭环审阅

- Query: 只读审阅父任务与两个子任务design.md，核对四类型UI、空槽原子首写、共享选择器、分类时点/ANY/ALL/后筛分页、历史深链、权限/失效/CAS草稿及卡片外部引用预览映射。
- Scope: internal
- Date: 2026-11-05
- Active task: .trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories

## Findings

### 审阅结论

**通过；本轮发现0条需要修改设计才能进入实现的实质阻塞缺口。**

按主会话已选方案审阅：四类实体的setting_tags是字符串数组，分类tag条件保存规范字符串；不建立可重命名中央标签库。这是明确设计选择，取代fields-categories-ui.md中的候选标签目录方案，不列为缺陷。标签、名称和规则使用当前版本；四类字段值支持按章回溯。

### 审阅文件

| 文件 | 作用 |
| --- | --- |
| .trellis/tasks/archive/2026-11/11-05-custom-setting-fields-categories/design.md | 子任务顺序、共享产品合同、权限/事务及备份/卡片总体边界。 |
| .trellis/tasks/archive/2026-11/11-05-typed-setting-fields/design.md | 四类值、空槽/绑定、组织和职业主体、字段API/UI及卡片v4合同。 |
| .trellis/tasks/archive/2026-11/11-05-setting-auto-categories/design.md | 字符串标签、单层规则、批量后筛分页、历史结果及分类UI合同。 |
| frontend/src/hooks/useEntityDeepLink.ts | 现有仅消费id的接收侧，供核对历史深链实施落点。 |
| frontend/src/components/entities/CardImportModal.tsx | 现有卡片预览/冲突选项/提交界面，供核对新增映射能力。 |
| frontend/src/api/cards.ts、frontend/src/api/types.ts | 现有卡片preview/import客户端及解析/决策类型。 |
| frontend/src/components/progressions/EffectiveSettingPreview.tsx | 现有按章值状态初始化与请求失效模式；已在此前研究核对。 |
| frontend/src/components/planning/PlanningFields.tsx | 现有分页条目选择器；已在此前研究核对。 |

### 检查结果与证据

| 检查项 | 设计证据 | 结论 |
| --- | --- | --- |
| 四类UI与主体区分 | typed-setting-fields/design.md:11将organization、career_entry与角色挂接career/membership分开；:94配置页，:95四类详情独立入口，:96统一TextArea/Input/Select/分页单选。 | 覆盖，组织/职业没有误借角色挂接时间线。 |
| 空槽与原子首次保存 | typed-setting-fields/design.md:28规定只读LEFT JOIN空槽且field_id=null；:29首次基础值/变化都在同事务建绑定；:72和:73分别给出复合首写API；:77同时检查目录与时间线版本。 | 覆盖，前端无需先发创建绑定再保存值/事件，不冒充custom_field_id。 |
| 共享选择器与导航 | typed-setting-fields/design.md:70提供领域中立目录；:96明确抽取规划分页选择器及四类型URL helper，且不依赖规划版本/标签。 | 覆盖，避免四种详情重复实现搜索及跨模块版本耦合。 |
| 最小字符串标签 | setting-auto-categories/design.md:11定义独立setting_tags及归一化；:16明确无中央重命名系统；:52独立CAS保存；:64独立标签保存区。 | 覆盖，和别名、作品题材、规划标签及剧情变化分开。 |
| 当前标签/规则与历史下拉 | setting-auto-categories/design.md:31明示position=None、已有/计划章、start/end及固定口径提示；父design.md:42和:43明确当前值含全部有效变化、名称/规则不另建时间线。 | 覆盖，不以最后写作章节代替当前值，也不虚称标签历史回放。 |
| ANY/ALL与失配 | setting-auto-categories/design.md:22限制1–20条单层条件；:24和:25限tag/dropdown；:27先类型筛选，完整条件any/all，缺字段/空值为false，错误条件保存时拒绝。 | 覆盖，没有零条件全匹配、隐式忽略条件或表达式扩张。 |
| 后筛分页与统一求值 | setting-auto-categories/design.md:35禁止前端按行effective；:37候选分批；:38共享resolver；:39完整规则求值后累计total并只留目标页；:40返回匹配理由与版本/位置。 | 覆盖，既无先分页再归类，也未照搬规划全目录装载规模假设。 |
| 历史结果打开详情 | setting-auto-categories/design.md:65提供章初末筛选；:66要求四类URL携带有效位置和只读意图、验证参数、不灌入基础草稿；:82将深链列为验收重点。 | 覆盖端到端行为要求，实施必须落到原详情接收侧。 |
| 权限/失效/CAS草稿 | typed-setting-fields/design.md:77和:79规定双版本CAS及区分错误code；:98分开草稿且不自动刷新版本覆盖；:99只读和真实权限。setting-auto-categories/design.md:51明确POST预览按读取权限；:59、:67、:68、:69覆盖作用域、换身份/失读、失写、对照和广播不覆盖草稿。 | 覆盖，未将POST预览误判成写动作，也未用隐藏按钮替代提交时校验。 |
| 卡片外部引用预览映射 | 父design.md:92要求包外引用显式映射且未解决整笔拒绝；typed-setting-fields/design.md:105明确世界观为包外目标、可映射既有条目；:106要求兼容定义复用/改名、先建实体再映射、缺映射或不兼容整笔回滚。 | 覆盖用户可完成的预览决策合同；不是只要求后端拒绝而无映射产品能力。 |

### 实施清单应落实的现有代码差距

以下属于设计已经要求的修改落点，不新增产品范围，也不计为设计缺口：

1. **历史深链接收侧**：useEntityDeepLink.ts:7的onOpen目前只接item，:14只读取id；角色/世界观打开后默认基础Tab。分类设计:66的时点与只读意图必须实际传到四类详情及有效值预览的初始位置，不能只修改URL生成函数。现有EffectiveSettingPreview.tsx:32默认第一章，实施时要响应经过校验的跳转位置；无效或已删位置不能默默退成当前值。保留既有加载失败不消费深链和草稿守卫。

2. **卡片映射预览/提交**：CardImportModal.tsx:27当前choices只有characters/organizations，:56只把这些choices提交；api/cards.ts:13与:14也只有既有preview/import载荷。需按字段设计:105和:106扩展预览中包外引用及定义冲突的决策区，复用共享条目选择器，保持映射与当前预览文件/作品绑定，把明确决策提交给导入API；未解决项在预览中可见且不能声称可以直接导入。字段设计已经要求显式映射，不应把这部分遗漏为纯服务端工作。

3. **真实浏览器闭环**：字段设计:96至:99及分类设计:63至:69要求的交互，应以真实API验证四类控件、空槽直接新增变化、两窗口冲突草稿、切作品/条目/章节迟到响应、只读与历史深链；卡片需覆盖“包内映射＋包外世界观手动映射＋缺映射阻止导入”，并检查375px、键盘与明暗主题。现有node单测不能替代这些用户操作证据。

### 相关规范与外部参考

- .trellis/spec/backend/setting-progressions.md：基础/当前/按章值、CAS、稳定字段身份、只读与旧响应隔离。
- .trellis/spec/backend/story-entities.md：职业定义与角色挂接、卡片预览/导入边界。
- .trellis/spec/backend/mention-tracking.md：深链接收时保留失败目标、沿用原详情与草稿保护。
- .trellis/spec/backend/planning.md：共享目录与规划标签独立、CAS及有界查询。
- .trellis/spec/frontend/component-guidelines.md、state-management.md、type-safety.md、quality-guidelines.md：统一客户端/传输类型、Form外权限、身份失效、草稿、懒加载与真实响应式验收。
- 无新增外部资料查询；本轮为内部设计覆盖审阅，组件依赖与版本依据沿用fields-categories-ui.md，未建议增加依赖。

## Caveats / Not Found

- 本结论针对本轮读取的三份design.md，属于规划覆盖审阅，不代表代码已实现、API已通过测试或浏览器已验证。
- 没有读取implement.jsonl/check.jsonl；没有修改design、实现清单、代码、spec、子任务或Git状态。主会话并行编写实现清单时，需把上面的既有合同落到对应文件与验证步骤。
- 不将字符串标签、只允许新建规则选择当前可用选项、名称/规则不回放视为缺陷；这些都已被主方案明确选定。
