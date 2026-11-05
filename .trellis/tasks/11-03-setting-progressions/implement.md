# 设定随剧情演变：实施计划

## 状态与执行前提

用户已确认范围与 M1 旧数据策略，并在最终规划摘要后回复“开始”，批准实现。先运行 `task.py start`，再依次执行下列切片。

实现与检查按 Codex Trellis 模式交给 `trellis-implement` / `trellis-check`。每次派发以 `Active task: .trellis/tasks/11-03-setting-progressions` 开头，原生上下文注入优先、子代理按该明确路径加载为后备。主会话协调合同、更新规范、核对证据与收尾。

本任务维持一个交付单元；以下是有序实现切片，不另创建前后端子任务。每个派发限定模块／文件所有权，通常每次 3–5 个产品文件；同一切片过大时分连续小包，不放宽验收。共享模型、迁移、schema/types、注册表与公共写服务只能有一个写入者；其他代理不得回退其改动。

## 实施切片与依赖

### S1：确定性求值内核

- [x] 先写失败用例：章首／章末、乱序记录、同位置人工优先、append→replace→append、撤销中间事件、原数据不被修改。
- [x] 新建类型化位置／字段／快照及纯 reducer。按 design 的白名单处理结构化值、原子生命周期、空文本和稳定顺序。
- [x] 输出包含覆盖状态／事件来源，不把 legacy_unverified 标作真实初始状态。

文件责任：拟 `services/setting_progression_resolver.py`、`schemas/progression.py`、`tests/test_setting_progression_resolver.py`。依赖：最终方案批准。验收：替换掉按章过滤或 boundary 比较，核心用例必须失败；此阶段不接模型调用。

### S2：持久化、基线与 M1 升级

- [x] 新增 timeline、text field、progression 模型与真实 FK/CHECK/唯一约束，加入 metadata。
- [x] 基于实现时真实 Alembic head 新建迁移；现有记录复制为待校准基线，当前值不变，旧日志不重放；关系 present 标记默认保持现有记录可见。
- [x] 覆盖 SQLite 和 PostgreSQL 真实迁移、从0030有数据升级、级联／SET NULL；安全降级保留最新值和审计，隐藏关系时拒绝降级；所有新建路径使用明确初始化原语，GET 不回填。

文件责任：`models/progressions.py`、`models/__init__.py`、必要的 `models/entities.py`、新 migration、`tests/test_migrations.py`；分模型/迁移两个连续包。依赖：S1。验收：升级前后当前档案一致，旧数据有待校准状态，缺章锚点不会自动挂回。

### S3：世界观单字段变化闭环（后端）

- [x] 实现基础修正、事件 CRUD、分页、revision 冲突、按章求值 API，先以 world.content 验证完整 HTTP 流程。
- [x] 接入项目人工 read/edit、父行写锁、同作品引用校验、底层只 flush；事实和 latest 同事务。
- [x] 实现 latest 物化与回溯 DTO 分离，验证 GET 前后 ORM 不脏写。

文件责任：`services/setting_progression_service.py`、`api/v1/progressions.py`、路由注册文件、`tests/test_setting_progressions.py`；schema 沿用 S1。依赖：S2。验收：世界观基础A、5章追加B、20章替换C；查看不同位置与最新API都正确，越权／并发失败不部分落库。

### S4：世界观单字段变化闭环（界面）

- [x] 增加 API/TS 镜像类型，建立可复用位置选择、变化表单／列表、有效设定预览；独立于已有基础 Form。
- [x] 先接 WorldPanel，使地点／物品可新增、编辑、删除变化，查看章首／章末结果及孤儿提示。
- [x] 验证目标／位置切换的请求隔离、只读权限、409草稿保留及基础草稿不会被变化刷新清空。

文件责任：`api/progressions.ts`、`api/types.ts`；新 `components/progressions/`；`pages/project/WorldPanel.tsx`；分适配器、组件、接入三个小包。依赖：S3；纯 UI 可在 S3 契约固定后并行构建。

检查点 A：world.content 从 UI → API → 时间线 → 预览贯通，未接入生成也能独立验收；不得以 mock reducer 的响应替代真实端到端数据验证。

### S5：角色文本与最小自定义字段

- [x] 接入角色白名单字段及状态；角色基础表单只加载 baseline 并提交脏字段。旧 PATCH 相同值不污染基线，有变化字段的歧义更新返回明确409。
- [x] 实现角色／世界观文本字段 CRUD、稳定字段ID、基础值、追加／替换、有效引用删除保护。
- [x] 角色详情复用变化与预览组件；校准旧基础为显式动作，单字段保存不自动确认整份旧资料。

文件责任：`services/story_service.py` 与 progression service；`schemas/story.py`；`CharacterFormFields.tsx`、`characterForm.ts`、`CharactersPanel.tsx`；分后端兼容、字段、角色UI三个包。依赖：S4。验收：只改姓名不改变旧境界；自定义字段改名后事件仍对应同一字段；另一字段不被替换清空。

### S6：人物关系的时间线与隐藏载体

- [x] 关系生命周期、身份原子组、标量／描述接入统一写入口；timeline 按稳定人物对，投影不删除旧审计载体。
- [x] 统一当前 list/graph/relations_among、开放API列表／详情／总数、卡片快照、AI缺失关系生成过滤present；明确current-pair与record-pair查询，内部复建包含隐藏载体并复用，历史按timeline查询。
- [x] 关系表单明确基础修正／剧情变化，旧审计保留只读。

文件责任：`relationship_service.py`、`schemas/relationship.py`、`components/relations/RelationEditModal.tsx`、关系API/types、对应测试；拆后端和UI。依赖：S5。验收：建立前不存在；撤销唯一建立事件后当前无幽灵关系但旧审计在；复建同对无唯一键冲突。

### S7：职业与成员生命周期

- [x] 职业按角色＋职业保留历史，阶段更换、卸下／再挂接为显式事件；检查所有受影响区间的主副数量与阶段归属。
- [x] 成员按角色＋组织记录归属／职位／状态／备注，current行消失后仍能回溯。
- [x] 职业快捷换阶、卸下、角色／组织两侧成员表单同步接入位置与意图，不能只改完整编辑弹窗。
- [x] 类型／阶段等引用保护覆盖历史快照，基线修改、章序改变和职业kind变更也做区间约束检查；已卸下职业仍必须参与kind变更校验。

文件责任：`career_service.py`、`organization_service.py`、对应 schemas/API、`CharacterCareersTab.tsx`、成员组件及测试；职业与成员分包，公共时间线写服务由同一所有者维护。依赖：S6。验收：10章加入、20章卸下、30章重挂，15/25/35章分别有／无／有；角色卡不带后期职位。

### S8：整章生成、分析输入与实际设定预览

- [x] context_builder 用统一 DTO 组装，不在格式化中另查当前关系／职业／成员；旧关系备注不因过滤未来关系而重新启用。
- [x] generate/revise/自动续写/批量逐章按合同使用目标快照；analysis 的输入同源。
- [x] 新增无需AI配置的章节设定预览与编辑器入口；角色／世界观有效值和实际模型消息一致。

文件责任：`context_builder.py`、`analysis_service.py`、必要的 `generation_service.py`／`batch_service.py`、新只读API、`ContextPreviewPanel.tsx`；分上下文、分析输入、预览小包。依赖：S7。验收：实际 FakeLLM 消息中的5章角色仍为旧境界，20章分析输入使用章初，N+1能看到N章末。

### S9：局部改写与提示词兼容

- [x] rewrite/expand/condense/deslop/光标continue 注入有预算的 setting_context；复用选角，补充选区／邻近正文中出现的人物。
- [x] 同步 rewrite builder、registry、模板与预览示例；采用 locked 兼容既有保存覆盖，空块维持原 parity。
- [x] 整章 polish 不引入新设定读取；原 Unicode 偏移、选区上下文、风格／Skill行为保持。

文件责任：`services/rewrite_service.py`、`api/v1/chapter_versions.py`、`ai/prompts/rewrite.py`、`ai/prompts/registry.py`、rewrite模板；分调用层与模板层包。依赖：S8。验收：五种op的真实消息不提前带入未来设定；新块一次、旧覆盖兜底、preview锁定、空块逐字回归通过。

### S10：分析写回、确认、幂等与撤销

- [x] 新分析在目标章末登记，比较目标位置而非全局当前值；旧“跳过早章”改为“登记早章但保留较晚投影”。
- [x] 重大变化保留待确认；提交复验正文hash／章节身份和位置／revision，过期结果不激活写回。
- [x] 建立 source_change 关联、独立protection、同字段重跑替代与原事件revision撤销；替代不能被值相同分支跳过，原子失败保留旧组；未再报告的旧事件只提示复核。
- [x] 旧无事件关联的 AnalysisChange 按设计执行受限兼容，人工校准A→C→A后仍不得恢复旧撤销资格；旧创建撤销检查整个主体。

文件责任：`writeback_service.py`、`analysis_service.py`、必要的 analysis schema、`tests/test_writeback.py`、`tests/test_analysis.py`。依赖：S8；可在 S9 之后串行，避免共享提示词/上下文改动重叠。验收：先20再5的分析正确；撤5不覆盖20；原事件被手改后撤销409；重复任务不增副本；SAVEPOINT保留部分成功隔离。

检查点 B：生成／局部改写／分析完整使用统一时间线；角色、关系、职业、成员的存在性和时点都通过实际消息断言。

### S11：章节位置及所有创建／删除路径

- [x] planned章号登记 CHAPTER_NO_REFS，已绑章读稳定ID；插章、直接改号、普通删章、细纲同步删章同事务重算与区间校验。
- [x] 覆盖通用创建、组合创建、向导替换、拆书反推、删除实体／项目、职业定义变更等入口，防止双写或缺基线。
- [x] 删除章后孤儿变化可见但不生效，同号重建不复活；批量插删只在操作末尾统一重算。

文件责任：`chapter_numbering.py`、`chapter_service.py`、`plan_service.py`；另包 `wizard_service.py`、`book_import/reverse.py` 及初始化helper。依赖：S10。验收：所有编号反射测试、新增直接改号／细纲删章／孤儿恢复用例通过，其他作品不受影响。

### S12：项目备份 v2 与角色卡 v3

- [x] 注册新表／枚举／中文标签、JSON ID remap、导入顺序和 APP_SCHEMA；补齐仅由历史快照／inactive／孤儿事件引用的全局关系类型导出闭包。项目格式v1→v2按M1初始化，v2核心引用缺失拒绝导入并完整往返。
- [x] 分析／回写条目继续核心导出，source FK 删除SET NULL、不级联删除事实；来源丢失和手工重绑均测试。导入最后统一重算最新值并保留规定时间戳。
- [x] 角色卡v3包含最新快照和自定义文本，兼容v1/v2导入；不搬运源作品章节位置。

文件责任：`project_transfer/registry.py`、`remap.py`、`exporter.py`、`importer.py` 与测试为两个包；`schemas/card.py`、`services/card_service.py` 与卡片前端类型为独立包。依赖：S11。验收：跨作品ID不相同仍保持所有时点结果；v1导入待校准，v2原有校准状态不丢；中途失败整体回滚。

### S13：智能体效果与撤销

- [x] 设定工具显式表达编辑意图；新表注册 EFFECT_TABLES、标签及依赖检查。
- [x] 新effects带格式版本；Core恢复覆盖基线／事件／字段和隐藏关系标记，恢复后重算或验证当前投影。旧effects涉及时间线主体／章锚变化时在恢复前明确409，纯正文等无关撤销继续正常。
- [x] 保留人工管理员内容权限与owner-only AI界限，回收站拒写。

文件责任：`agent/effects.py`、`agent/tools/write.py`、必要的 `agent/tools/references.py`、`tests/test_agent_effects.py`／`test_agent_tools.py`。依赖：S12。验收：新格式删角色后撤销能恢复历史；预置0030旧effects的角色创建／修改／删除撤销均安全409，纯正文撤销仍正常；期间出现新引用／新变化则冲突且整批回滚，其他作品变更不误触冲突。

### S14：完整质量门禁、规范与最终验收

- [x] 使用 trellis-check 对整个任务检查，按真实证据修复；不删除旧断言以掩盖回归，只对已明确改变的业务契约调整断言并补替代保障。
- [x] 跑前后端完整门禁及迁移／备份／权限回归，完成真实浏览器的完整主流程和目标切换检查。
- [x] 更新实际契约规范与索引、TODOS第1项状态；只勾已验收项，旧数据限制及第4/7项边界保留。
- [ ] 主会话按 Trellis Phase3 处理规范更新、提交及收尾，记录真实验证结果，不把本规划检查冒充产品测试。

依赖：S13。此时才可宣称第1项首版交付；浏览器验证不可用时明确记录未验证项，不能声称全部验收完成。

## 验证命令与敏感用例

在相应目录运行；先跑切片涉及的用例，阶段结尾跑相关集成组。未发生代码修改／新失败时不反复重跑已通过的同一组。

```bash
# backend/：内核与新API
uv run pytest tests/test_setting_progression_resolver.py tests/test_setting_progressions.py

# backend/：历史、真实消息和模板
uv run pytest tests/test_context_builder.py tests/test_cast_context.py tests/test_rewrite.py tests/test_revise.py tests/test_analysis.py tests/test_writeback.py tests/test_batch_generate.py tests/test_prompt_templates.py tests/test_prompt_parity.py tests/test_careers.py

# backend/：迁移、编号、传输与智能体
uv run pytest tests/test_migrations.py tests/test_chapter_numbering.py tests/test_outline_pro.py tests/test_project_transfer.py tests/test_cards.py tests/test_agent_effects.py tests/test_agent_tools.py

# backend/：最终全量
uv run ruff check .
uv run pytest

# frontend/：每个UI阶段按需运行，最终全量一次
npm run lint
npm run build
npm test
```

测试文件名是计划中的新增文件或研究已确认的现有文件；实现分包可细化，不得少测对应能力。macOS迁移测试如遇ASCII环境，按项目规范为单次命令设置 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`，不跳过测试。

浏览器验收：基础值→追加→替换→按章预览→生成/重写；第5/20/21章边界；关系建立前／职业卸下后；旧基线校准；无章节和孤儿空态；375px／暗色；只读管理者；快速切对象／章节；未保存基础草稿；409保留输入。先使用可用浏览器测试工具；若环境不能接后端，先完成可运行自动验证并明确剩余证据。

## 风险文件、回退点和检查证据

- 高风险公共入口：`story_service.py`、`relationship_service.py`、`career_service.py`、`organization_service.py`、`writeback_service.py`。它们同时被HTTP、分析、卡片和智能体调用，每个相关切片需交叉核对调用者。
- 注册表：`chapter_numbering.py`、项目RULES/remap/ENUM、EFFECT_TABLES、提示词registry必须同步；使用现有反射测试锁住覆盖，不维护平行手写名单代替测试。
- 检查点A、B及最终门禁均保存命令、退出码与失败修复说明到任务检查记录；未运行的测试明确标未运行。
- S2之后数据库若已升级，应用回退必须与迁移策略配套；历史与自定义字段只能由新格式完整备份保全。规划阶段不执行用户数据库迁移。
- 父子任务重新拆分只有在出现可独立发布的产品交付时再考虑；任何改动影响已审核范围／数据策略需先更新本任务规划。

## 规划就绪清单

- [x] PRD通过一次收敛整理，覆盖R1–R5及M1限制，无未决范围问题。
- [x] design 与本文件经过有界一致性核对，9项问题已纳入合同及实施验证。
- [x] implement.jsonl/check.jsonl 仅有真实spec/research条目，路径存在且验证通过；较长原规范按连续原文摘录避免单文件注入截断，保留原路径、行号及SHA256。
- [x] 向用户呈现最终目标、范围、排除项、验收、关键规则与限制。
- [x] 用户在该最终摘要之后明确批准实现（2026-11-03：“开始”）。
- [x] 随后运行 `task.py start`，再派发实现。
