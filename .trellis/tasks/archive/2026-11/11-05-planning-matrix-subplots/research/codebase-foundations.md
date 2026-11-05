# Research: 规划矩阵与支线现有基础

- Query: 章节／计划章节、人工关联、提及热力图、伏笔时间线、接入入口及兼容约束能复用什么？
- Scope: internal
- Date: 2026-11-05

## Findings

### 1. 章节与未建章计划不是同一种稳定位置

- `backend/app/models/entities.py:479`：`Chapter` 有稳定 id、`chapter_no`、可空的 `outline_id` 与 `plan_id`、正文与风格覆盖，没有章节 POV、标签字段。
- `backend/app/models/entities.py:504`：`ChapterPlan` 有稳定 id、`outline_id`、卷内细纲序号 `seq`、摘要／事件／角色列表及可空 `chapter_id`；没有独立章节号。只有建章后才附上真实章节号：`backend/app/services/plan_service.py:73`。
- `backend/app/models/entities.py:466`：大纲自身有 `volume_no`、`chapter_no` 和 `sort`，一条大纲可展开多条细纲。不能简单把“一条大纲”当作“一章”。
- `backend/app/services/plan_service.py:236`：批量建章复用已有单章或创建新章，填充双向 plan 链接；插入时平移后续章节号，首次未占用的大纲位置只为额外章节腾位（275 行）。矩阵若涵盖未建章细纲，应按稳定 plan id 锚定，建章时明确合并／转移到 chapter id，不能只认章节号。
- `backend/app/services/outline_service.py:97`：大纲上移／下移只改变 `sort`，不会重排实际章节号；矩阵的故事顺序必须明确使用哪条顺序。
- `backend/app/services/chapter_numbering.py:72`：所有保存章节号的字段登记 `CHAPTER_NO_REFS`；平移统一发生于 `shift_chapter_numbers`。已建章设定变化锚点随 chapter id，只有计划章号参与平移（87 行），可借鉴其稳定身份原则。
- `backend/app/services/chapter_service.py:234`：直接删章删除 Chapter 并更新记忆与设定投影，不自动收紧后续章号。ChapterPlan.chapter_id 外键 SET NULL；其计划仍在。
- `backend/app/models/entities.py:484`、`507`：删除大纲／细纲时正文保留，章上的关联 SET NULL；大纲删除会级联删除细纲。`plan_service.py:163` 重新展开删除细纲时会拒绝删除已有正文对应的细纲，可能删除空章并做编号平移。

### 2. 已有人工角色关联，缺少通用规划关联／支线

- `backend/app/models/entities.py:377`：`ChapterCharacter` 是章节手动出场角色关联表，按 chapter_id + character_id 唯一，双方删除均 CASCADE。
- `backend/app/services/cast_service.py:147`：`set_cast` 整体替换本章人工角色，验证每个角色属于本作品，使用项目写锁／人工编辑权限并记录审计。
- `backend/app/services/cast_service.py:121`：人工角色**已经影响 AI 选角**，顺序是主角 → 手动角色 → 自动匹配；规划矩阵若复用现有出场角色，不能宣称“所有矩阵改动均不影响生成”。
- `backend/app/models/entities.py:519`：细纲已有 `character_ids`；`backend/app/services/plan_service.py:225`、`262`、`295` 在建章时把有效角色追加到 `ChapterCharacter`，不是持续双向同步。
- 全局搜索 `pov` 的领域存储仅发现 `WritingStyle.pov` 文本（`backend/app/models/entities.py:811`），不是绑定视角人物。未发现章节标签、章节手动地点／物品关联、独立 subplot 表或模型；“支线”仅出现在风格文本中。
- 结论：角色人工关联应复用或明确迁移现有模型，避免产生两套出场角色事实；POV、标签、地点等关联、支线与推进节点需要新增契约。

### 3. 自动提及和伏笔可复用展示方式，不能直接充当支线推进

- `backend/app/services/mentions/summary.py:73`：热力图统计只遍历真实 Chapter 原文；按角色／地点／物品字面名称和别名生成确定与歧义次数。不存在的计划细纲不会作为列返回。
- `backend/app/services/mentions/summary.py:91`、`105`：断档计数只累计有正文的章，`gap_chapters` 是最后一次确定提及之后的已写章数，不是支线推进断档。
- `frontend/src/components/mentions/MentionHeatmap.tsx:16`：现有可访问 HTML 表格模式包括粘住条目列、横向滚动、数字／歧义／未写状态并行显示，以及键盘可点击的格子。适合借鉴，数据语义不应混用。
- `frontend/src/pages/project/MentionsPanel.tsx:125`：监听 chapters／characters／world 刷新；提供章窗口、热力图、时间线与逐章表（176–197 行）。矩阵可沿用窗口和渐进加载思路，不必重新扫描正文。
- `backend/app/models/entities.py:711`：Foreshadow 是埋设／回收两阶段模型，含计划／实际四个章号、状态与关联角色，不是任意多个推进节点。
- `frontend/src/components/foreshadows/ForeshadowTimeline.tsx:47`：SVG 已有计划／实际虚实线、当前章、超期、长线箭头及编辑入口。可借鉴轨道布局与色彩规则，不能把“伏笔存在期间”推断成支线持续推进。
- 自动提及属于证据，人工关联属于作者规划，支线节点属于推进记录；合并展示应保持来源可辨认。

### 4. 前端接入点

- `frontend/src/components/StudioNavigation.tsx:41`：创作导航已有大纲、伏笔、提及追踪等入口；独立规划工作台可在此登记。
- `frontend/src/router.tsx:113`、`116`、`118`：伏笔、提及、大纲都是 `/projects/:id/…` 懒加载页面，可沿用独立 `planning` 页面模式；或在大纲页增加视图切换，两者是 UX 选择。
- `frontend/src/pages/project/OutlinesPanel.tsx:61`：已有 `useProjectAccess` 与 `useLoader`，大纲树和角色一次加载，资源变动会刷新；`PlanList` 已展示／编辑细纲。
- `frontend/src/pages/project/ProjectLayout.tsx:39`：管理员人工管理他人作品及只读状态是现有正式能力，新页面必须延续只读查看而非默认所有访问者可写。

### 5. 备份、权限与生命周期必须统一

- `backend/app/services/project_service.py:39`：人工内容使用 `get_manageable_project`，读允许拥有者或 `projects.read` 管理员；写显式 `lock=True, permission="projects.edit"`，再次校验目标与项目 active 生命周期。
- `backend/app/services/cast_service.py:149` 提供人工批量关联写入与审计模板。新增节点／关联引用必须全部属于当前作品，批量更新应在一个事务内完成。
- `backend/app/services/project_transfer/registry.py:216`：Outlines、Chapters、ChapterPlans、ChapterCharacter 已登记；plan 的 JSON character_ids 在 233 行使用 id remap，chapter.plan_id 在 223 行延迟回填以解决环形外键。
- `.trellis/spec/backend/project-transfer.md:9`：所有新作品关联表与新增 FK／JSON 列必须登记 `RULES`，含 id JSON 进入 `json_refs`、无 id JSON 进入 `json_plain`。
- `.trellis/spec/backend/project-transfer.md:69`：已有反射测试检查全项目表覆盖、每个 FK／JSON 列与导入顺序；需要追加新规划数据的导出→导入→再导出往返测试及跨作品引用拒绝。
- `.trellis/spec/backend/project-transfer.md:140`：新增表要评估并推进格式版本与旧版本迁移；当前 `backend/app/services/project_transfer/exporter.py:33` 为 v2，迁移入口 `importer.py:253`，枚举白名单 `importer.py:116`。
- `.trellis/spec/backend/outline.md`、`setting-progressions.md`、`rbac-quotas.md`、`admin-projects.md`、`recycle-bin.md` 是实现阶段必须补充读取的跨层契约。

## 需要用户决定的产品边界

1. 首版章级是否同时包含未建章细纲／大纲占位？这是规划价值的重要边界；只做已有 Chapter 最简单，但无法提前编排尚未建章的故事。
2. 新 POV／支线节点是否进入 AI 上下文？已有手动出场角色本来就影响 AI，因此“仅规划、不接 AI”必须精确限定为新增元数据。
3. 支线交汇是“同章有节点”的视觉提示，还是作者显式声明关联事件？断档是任何节点间空白，还是超过作者设定阈值？两者不能从原有伏笔或提及行为推导。
4. 首版手动设定关联覆盖角色＋地点＋物品，还是连组织／职业等所有设定类型一起覆盖？TODO 只明确矩阵中的角色／地点／支线，但第三项“设定条目”范围更宽。

## Caveats / Not Found

- 只读代码／规范调研；未修改产品代码、未运行测试，也未对外部文档做结论。
- 未发现独立场景模型，因此直接支持“章节／场景”两种粒度会把第 7 项实质引入本任务。
- 计划位置与建章转移、正文自动提及和手动关联、既有角色选角影响是本任务最需要写清楚的兼容边界。
- 本次没有穷尽所有路由权限登记与审计注册文件；实现设计阶段需在新增路由前检索中央路由能力映射并补相应权限测试。
