# 规划矩阵与支线交织视图：实施计划

状态：2026-11-05 用户在最终摘要后回复“开始”，已批准实施。产品选择和交汇事件范围已确认，PRD 已收敛，一致性研究的三项修复已写入 design.md。

## 执行组织

单一任务，按依赖顺序交付三个阶段，阶段内可在接口稳定后拆出明确文件所有权。每个 implementation／check dispatch 以 `Active task: .trellis/tasks/11-05-planning-matrix-subplots` 开头；沿项目 Codex 默认模式使用 `trellis-implement`／`trellis-check`，主会话负责范围、协调和文档。不要让两个 worker 同时编辑 entities.py、types.ts、context_builder.py 或迁移文件。

所有阶段都保留他人的现有修改；实施前重新核对 git status 与迁移 head。任务期间不自动改写他人的提交，不把无关文件纳入提交。

`backend/database-guidelines.md` 超过当前单文件注入上限，因此不放入自动注入清单；阶段 1 子代理须按正文目录直接读取其中与新增模型／迁移／事务／外键相关的章节。AI／前端阶段开始前，主会话按对应规范索引补齐该阶段的 manifest，不能因阶段 1 的上下文已配置而省略。

## 阶段 1：稳定规划模型与 API

- [x] 读取 backend 数据库／错误／质量／outline／setting-progressions／project-transfer／权限与回收站规范，以及锚点研究。
- [x] 固定 schema：PlanningUnit（binding_epoch／retired）、POV、标签、五类设定关联、Subplot、SubplotNode、SubplotCrossing／参与集合、项目 planning_revision；新增 Alembic 迁移和模型注册。
- [x] 先实现并验证统一读模型：真实章＋未建章细纲、无数据虚拟行、章／plan 去重、已建章／计划轴分离、待关联记录、分页及类型化实体选择。
- [x] 统一 live 角色权威和 flush-only 更新 helper，接入章 cast／plan／建章旧入口；首次建章保持角色并集。
- [x] 实现批量 add/remove/replace/clear、POV set/clear，项目原子 revision 校验、全量引用校验及失败整批回滚。
- [x] 生命周期 helper 覆盖创建、改纲、建章、删章、删纲／细纲、重展开、向导／导入覆盖、排序与恢复；每个影响源身份或角色权威的入口递增版本。
- [x] 支持双方有规划时的建章冲突预检、明确合并、待关联重新挂接；不能为解决冲突静默丢失节点。
- [x] 新快照明确保存 unit→plan＋binding_epoch；恢复前核验仍存章锚点，旧快照保持 orphan 并提示；恢复不回滚新的规划文本／存活章 cast，不能因裸章 ID 复用而改挂。
- [x] 实现支线／节点／标签／明确交汇 CRUD 与交织接口；参与支线为 2–20 个同作品对象，支线被交汇引用时不能直接删；无普通节点的事件也计入推进。
- [x] MatrixColumn 明确 setting／subplot 两种列和对应 cell；支线格子包含 node／crossing，不把支线只留在独立交织页。
- [x] 关系引用使用同作品 relationship 时间线而非当前载体 ID；支持隐藏／尚无载体资料，接口明确 timeline_id，真删除时清理并递增版本。
- [x] v3 作品备份注册、v1/v2 升级、全部 ID remap、orphan 及快照恢复；同步 APP_SCHEMA／枚举白名单。
- [x] 审核 agent effects 直接 SQL 路径，增加间接规划影响与后续作者编辑冲突保护；revision 不回退。
- [x] 路由能力映射、跨作品／只读管理、回收站屏障与管理员人工审计按现有规范接入。

关键文件（规划入口，最终清单以实际修改为准）：

- `backend/app/models/entities.py` 或按现有模型拆分的新模块、`backend/alembic/versions/`。
- 新 `backend/app/schemas/planning.py`、`backend/app/services/planning_service.py`、只读规划投影模块、`backend/app/api/v1/planning.py` 与路由注册。
- `chapter_service.py`、`plan_service.py`、`cast_service.py`、`story_service.py`、`outline_service.py`、`outline_transfer_service.py`、`outline_snapshot_service.py`、`wizard_service.py`。
- `project_transfer/{registry,exporter,importer,remap}.py`、`agent/effects.py`，中央权限／审计登记文件。

阶段验证：

- [x] 新 `tests/test_planning.py`：读模型、类型关联、批量语义、版本冲突、404／403／跨作品、只读无副作用。
- [x] 新 `tests/test_planning_lifecycle.py`：正常建章、复用章、双方冲突、删除／重展开、相同整数 ID 被重建、全部来源消失、快照恢复和重新关联；包含改挂后再次孤立、retired 单元、旧章 ID 复用、新旧快照和 binding_epoch 不回退。
- [x] 交汇测试：无普通节点、多个同名不同 ID 事件、参与数／重复／跨作品校验、移动、删除引用保护、整作品级联删除、矩阵筛选不改变完整参与集合、备份及 effects 往返。
- [x] 扩展 `test_migrations.py`、`test_outline_pro.py`、`test_outline_snapshots.py`、`test_chapter_numbering.py`、cast 测试、`test_project_transfer.py`、`test_agent_effects.py` 与相应权限／生命周期测试。
- [x] 用真实 PostgreSQL 验证项目锁／CAS并发、FK／唯一约束、迁移和快照序列不回退；不只凭 SQLite 测试推断生产行为。
- [x] API 契约与类型化输出稳定后才进入后续阶段。失败时回到该阶段修复；不跳过强约束换取绿色检查。

## 阶段 2：AI 规划上下文

- [x] 读取 `research/ai-integration-design.md` 及 prompts-styles／story-entities／setting-progressions 规范。
- [x] 实现同一目标章投影供 generate、revise、rewrite 与规划预览复用；读取不写 ORM，不以计划章显示序号当真实章号。
- [x] 本章节点／交汇目标和 POV 使用独立锁定 planning_context；作者概览、其他章／未建章／orphan／retired 内容排除，交汇按事件 ID 只出现一次且不受 UI 筛选影响。
- [x] 角色 POV 选角去重与章初有效状态；world 全分类、组织、人物关系、职业显式关联合并，独立引用没有角色时仍生效，保证不授职／不新增成员或关系事实。
- [x] 人物关系按目标章初时间线解析；不存在时不泄露未来类型，隐藏关系仍能读早期有效值；两端角色仅为参考、不自动写 cast；新显式资料进入受保护块并与普通设定去重。
- [x] 规划块与关联资料计入既有预算；确定性裁剪、标记截断，过小预算明确失败；无新增规划时旧消息逐字不变。
- [x] 默认模板、registry 锁定变量、样例预览、prompt builder 默认参数、旧覆盖兜底与 prompt_legacy 同步。
- [x] 局部操作保留选区范围，自动续写保留首轮消息，批量逐章读取；生成记录包含实际使用的规划身份／版本／截断信息。
- [x] 真实章节预览与未建章规划预览准确标注差异；正文生成入口仍要求真实 chapter。

关键文件：`context_builder.py`、`cast_service.py`、`generation_service.py`、`rewrite_service.py`、`app/ai/prompts/{chapter,rewrite,registry}.py`、相关 `.j2` 与必要的 projection helper。`batch_service.py` 只在真实调用链需要时修改，优先复用现有单章实现。

阶段验证：

- [x] 新 `tests/test_planning_context.py` 捕获 FakeLLM 实际 messages，覆盖生成、整章重写、五类局部操作、批量、自动续写、旧覆盖和短预算。
- [x] 第 5 章输入含目标 POV／节点／交汇；第 20 章节点及交汇、作者结局备注、未建章计划和孤立内容不被新规划投影带入。
- [x] 关系第 10 章建立、第 20 章结束时，第 5／15／25 章的显式参考准确；只有关系／只有交汇的章节各自进入正确分支；三方事件在矩阵筛掉一方后 AI 仍包含完整三方。
- [x] 当前章人物状态／职业阶段历史正确；静态组织／职业档案边界明确。
- [x] `test_context_builder.py`、`test_cast_context.py`、`test_rewrite.py`、`test_batch_generate.py`、`test_prompt_templates.py`、`test_prompt_parity.py`、`test_templating.py` 通过。

## 阶段 3：规划矩阵与交织工作台

- [x] 读取 frontend 组件／hook／状态／类型／质量规范及 `research/ui-and-api-contract.md`。
- [x] 添加 `src/api/planning.ts` 与 types.ts 镜像；封装版本冲突和数据变更通知，禁止页面直接 fetch。
- [x] 添加懒加载 PlanningPanel 和导航；分卷／source／标签／类型／名称筛选、行分页与列窗口、已有章／未建章分组和明确位置文案。
- [x] 矩阵格子区分手动、POV、正文确定／歧义、不支持自动追踪、未写章；设定／支线列均能显示与编辑，关系按 timeline_id 深链／预览，跳转章节／细纲／各类型设定。
- [x] POV、项目标签、全类型设定的单项与批量编辑；批量摘要、明确追加／移除／替换／清空、加载禁用、错误保留草稿与刷新重试。
- [x] 支线管理、推进节点编辑、明确交汇的创建／编辑／移动／删除、交织轨道与等价表格；间隔在完整轴上计算，阈值提醒与两类顺序轴明确。
- [x] 待关联规划、重连／冲突合并界面；建章旧入口遇到 planning_merge_conflict 时给出解决入口。
- [x] AI 规划预览说明目标章、资料类型、用途及截断；未建章说明正式有效设定会在建章后确定。
- [x] 接入项目刷新、变更事件和授权代次隔离；只读查看可用，写控件受真实权限约束。
- [x] 不引入新图谱依赖；主题 token、375px 窄屏、键盘操作、可见焦点、图表文本替代与错误重试均通过。

验证：新 `src/api/planning.test.ts`、`src/utils/planning.test.ts` 验证行为与请求契约，不写镜像实现的同义反复测试；浏览器验证真实 API 的矩阵→批量→建章→AI 预览，以及错误／只读／窄屏交互。

## 最终门禁与验收

实施阶段在对应目录执行并记录输出：

```bash
# backend/；当前主机 locale 警告时采用规范给定的 UTF-8 locale
uv run ruff check .
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest

# frontend/
npm run lint
npm run build
npm test
```

- [x] `trellis-check` 对最终完整差异检查：数据生命周期、权限／只读、所有旧写入口版本递增、binding_epoch／retired、五类引用、交汇、备份／effects、模板覆盖、实际消息、前端请求与响应契约。
- [x] 浏览器桌面／375px、明暗主题、键盘、两标签页冲突、来源删除后重连已通过；后台批量与旧模板由实际模型消息回归覆盖。详见研究目录验收记录。
- [x] 更新 backend／frontend spec 索引与新增规划契约；更新 TODOS 第 3 项为章节级完成并注明场景仍属第 7 项，不能误勾场景能力。
- [x] 保留检查证据与已知限制；112个批准文件已暂存并通过 `git diff --cached --check`，无关文件未纳入。
- [x] 用户已确认提交清单；功能提交`03ffca8`完成，任务已归档，第36次会话日志已记录。

## 回滚点

1. 模型与生命周期尚未稳定：保持规划状态或仅回退本任务产品 diff，不对用户数据执行破坏性迁移。
2. AI 接入异常：保留规划数据，回退本任务提示词／投影接入；新变量和旧覆盖兼容一起回退，不能只删一处默认模板。
3. 前端异常：回退新增入口及组件，已保存规划仍由后端和备份保留；不把 UI 回滚等同于删表。
