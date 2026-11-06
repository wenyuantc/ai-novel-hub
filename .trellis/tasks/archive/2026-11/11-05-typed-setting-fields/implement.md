# 类型化设定字段：实施计划

状态：2026-11-05 用户在最终摘要后回复“开始”，本计划已获批准，进入顺序实施。

## 前置及调度

- [x] 读取本 PRD／design、父任务研究与上下文清单；再次核对当前 Git 状态、迁移 head0034和测试命令。
- [x] 保留无关文件和其他会话修改；不操作原有 pelican-cycling.html，不重置或撤销他人修改。
- [x] 用户批准最新父任务完整摘要后，仅 start 本子任务；父任务保持协调用途。
- [x] 每轮 implement/check 第一行 `Active task: .trellis/tasks/11-05-typed-setting-fields`。优先完整原生上下文注入；若缺失，子代理按该路径自行读取 artifacts 和 manifests，不运行 task.py current 猜其他会话状态。
- [x] 模型、迁移、schemas、types.ts、共享service、transfer和effects逐片单写者；只有接口冻结且文件不重叠时才并行前端组件。

以下是顺序小切片。每片先用有意义的失败用例锁定行为，再实现和定向验证；不是一次改完所有文件后才测试。较大片按列出的责任包继续分派，不让一个 worker 无边界接管全仓。S10.D中不依赖新API的既有选择器／URL复用抽取可提前独立进行，保持现有规划行为且文件所有权与后端分开；其余UI仍等待S4合同。

## S1：持久身份与安全迁移

依赖：前置批准。

- [x] 增加定义、选项、引用句柄模型；原字段表 definition_id、label形状和JSON基础值；新organization/career_entry主体与fields_revision。注册模型但保持现有功能行为。
- [x] 新0035迁移保留旧ID／时间戳／事件FK，初始化旧组织和职业的空时间线；不存在默认非空值或按同名合并。
- [x] 迁移回归用含legacy字段、active/inactive/orphan事件、关系／挂接、回收站作品的0034库，验证SQLite父表重建没有误级联；运行foreign_key_check。真实PG验证JSONnull、NO ACTION和整作品CASCADE。

责任包：`models/progressions.py`＋新模型模块与注册；`alembic/versions/0035_*.py`；`tests/test_setting_fields_migration.py`。按模型／迁移及验证顺序派发，不并行写迁移。

验收：旧表行身份和值不变，新约束拒绝错主体；新空态可降级、有新语义资料明确拒绝有损降级。

## S2：类型化codec与纯求值

依赖：S1模型合同。

- [x] 共享值codec固定text/line/dropdown/reference、空值、严格ID及EntityRef↔内部句柄边界；不使用字符串化对象。
- [x] resolver接FieldSpec并深复制对象，四类遵循原位置、排序、孤儿与人工优先；新主体只管理自定义字段，不调用授职物化。
- [x] schemas新增类型化结果，保留旧custom_fields字符串投影。PATCH省略与null区分，旧字符串事件不重写。

责任包：新codec模块＋`setting_progression_resolver.py`；`schemas/progression.py`＋新增字段schema；对应纯测试。每包≤一个共享领域改动，避免同时重构无关规则。

验证：扩展 `tests/test_setting_progression_resolver.py`，覆盖四类、第5/20/21章、章初末、清空、单行换行、跨组选项形状、对象不可变及旧原始字符串。

## S3：共享配置与目录只读API

依赖：S1–S2。

- [x] 字段定义／选项CRUD、适用范围、停用和fields_revision CAS；定义类型一旦被用禁止原地转换。
- [x] 领域中立跨类型目录与条目字段只读投影；空槽LEFT JOIN，不在GET创建绑定或句柄；服务端筛选分页。
- [x] 路由／能力／管理员人工审计登记沿现有规范，所有ORM查询在service。

责任包：`schemas/setting_fields.py`、新配置/目录service、新`api/v1/setting_fields.py`及路由／授权登记；`tests/test_setting_fields.py`。

验收：角色、六世界观子类、组织、职业目录正确且只读无副作用，跨作品不可见、只读可浏览不能写；两目录版本竞争整笔冲突。

## S4：基础值、首事件与旧接口适配

依赖：S3。

- [x] 在一个项目事务内ensure-binding＋写起始值／事件；首事件不能要求不存在的field_id，也不能两次提交留下半份。
- [x] 新写校验目录版本及timeline版本；旧文本端点保留legacy语义；显式关联定义保留字段身份与事件，不合并有资料的绑定。
- [x] 旧事件路由保留expected_revision，对共享事件强制expected_fields_revision；PATCH在内部canonical命令合并、公共value只编码一次，所有事件返回统一解码。回归旧文本请求、reference仅改note／章锚／显式null、缺失／过期目录版本均正确。
- [x] 目录和字段读做前后fields_revision／timeline revision核对；用屏障在多查询中改定义／选项，旧数据不能配新版本；上下文预取同样处理。
- [x] active／inactive／orphan事件读取与类型化回填，停用旧值和“新选择”校验分离；定义及目标删除保护helper可复用。

责任包：字段值service、`setting_progression_service.py`、对应路由／schema及 `tests/test_setting_fields.py`；legacy适配另一个顺序包，避免同文件多人写。

验收：首基础值与首章变化都原子，失败无空绑定残留；清空和省略差异可观测，字段改名不改历史。

## 检查点A：字段基础冻结

- [x] 模型／codec／API／版本合同定向测试通过，`uv run ruff check .`通过。
- [x] 检查事务无内部commit、GET无autoflush写、旧API字符串返回不变、错误码可区分。
- [x] API合同稳定后才进入写入口和UI，不以临时类型断言遮盖未定schema。

## S5：原有创建、换类与删除入口

依赖：S4。

- [x] 包A：角色／世界观原语、category PATCH、普通删除、同名legacy保护；检查旧入口触发相同适用性校验。
- [x] 包B：`new_org/new_career`及组织／职业删除、with-members／with-relations／batch；独立条目主体和人物挂接不混用。
- [x] 包C：向导准入与落库两次检查incoming历史引用、拆书直接构造路径、卡片直接组织构造；保持空字段不阻止无资料覆盖。
- [x] 目标删除有来源明确的409，整部作品清理不被NO ACTION错误阻塞；handle JSON引用包括所有保留历史。

责任文件：`story_service.py`、`organization_service.py`、`career_service.py`、`wizard_service.py`、`book_import/reverse.py`、`card_service.py`；按A/B/C分包。测试扩展各自已有套件并加 `test_setting_field_lifecycle.py`。

验收：每一旧创建入口新条目都可使用适用字段；普通删、向导覆盖及软删／恢复／purge均保持资料图。

## S6：批量投影与实际AI消息

依赖：S4–S5。

- [x] 批量resolver／格式器统一定义、选项、引用名，输出引用身份而不递归正文；继承原预算和裁剪。
- [x] 包A接`context_builder.py`／`planning_context_service.py`；包B接`analysis_service.py`及必要调用参数。只改真实消费边界，不扩大选角或档案选择范围。
- [x] 使用FakeLLM实际messages覆盖生成、整章建议改写、五种局部操作、自动续写、批量及分析；旧模板override和锁定上下文仍工作。

验证：新 `test_setting_fields_context.py`，并运行context/planning/rewrite/batch/analysis相关既有用例。必须断言20章后字段不出现在5章，ref目标正文不递归出现，独立组织／职业字段只在选中范围出现。

## S7：作品备份v4与导入图

依赖：S5；与S6不重叠文件可在单独ownership下执行。

- [x] 按拓扑注册定义／选项／句柄／绑定／事件，APP_SCHEMA0035、projectv4、旧1→2→3→4转换；全历史闭包和字段JSONrefs语义映射。
- [x] 预览、缺引用校验、ENUM/LABELS、反射及单事务导入一起覆盖；旧文件夹带未来字段不能绕过版本校验。
- [x] 导入非空实例后比较新旧ID、有效值及孤立事件；缺核心引用整笔失败不静默null。

责任包：`project_transfer/{registry,remap}.py`→`{exporter,importer}.py`→`tests/test_project_transfer.py`顺序；不与effects同时改registry。

## S8：卡片v4与显式映射

依赖：S7数据合同。

- [x] `schemas/card.py`／`card_service.py`固定包内refs、类型定义／选项快照、内部循环和外部依赖；旧v1–v3继续兼容。
- [x] 卡片预览列出定义冲突和未解析引用；确认请求携带显式映射，未解决／越权／不兼容时整笔拒绝；不增加世界观容器的隐含复制行为。
- [x] v4包内引用遇同名默认skip保持未解析，不能借旧按名char_map绑定；明确复用具体目标或重命名新建后才解析，回归已有目标值／历史不被覆盖。
- [x] 前端`api/cards.ts`及CardImportModal提供映射控件，CardExportModal明确当前快照用途；既有跳过不覆写字段／历史。

责任按后端契约→客户端→模态框顺序分包，API类型由唯一worker修改。运行 `tests/test_cards.py`、前端请求／映射纯逻辑测试和真实导入流程。

## S9：智能体工具与效果安全

依赖：S7注册拓扑。

- [x] 包A扩read/write/references：目录、有效值、显式类型化值与合法清空、同项目校验、provider工具声明；复用正式写服务。
- [x] 包B扩effects v3：新表快照、JSON依赖、预览ID归一、全部旧效果整轮预检、恢复后重验资料图及物化；fields_revision不回退。
- [x] fields_revision同时从PROJECT_RULE导出和effects独立项目快照／恢复中排除；undo从当前高水位递增而非before+1。测试手动→智能体→合法后续修改→undo的单调性及失败不递增，新导入不继承源代次。
- [x] 只证明纯正文等安全旧效果继续工作；不把所有旧效果一律拒绝，也不无条件以默认值恢复新字段。

验证：`test_agent_tools.py`、`test_agent_effects.py`、`test_progression_agent_effects.py`；新增后续引用阻止undo、重复预览一致、混合旧新效果不部分恢复用例。

## S10：作品字段配置与四类编辑

依赖：S4合同冻结，合并S6–S9前最终集成。

- [x] 包A：`api/types.ts`／新`api/settingFields.ts`及客户端契约测试；四类控件、typed draft及值展示纯逻辑。
- [x] 包B：新懒加载字段配置页、router／共享导航与类型筛选；配置和值独立保存，停用／适用性冲突不丢草稿。
- [x] 包C：字段区与变化面板复用，接角色／世界观，再接组织／职业定义，分开外层表单、成员和阶段；首写空槽原子调用。
- [x] 包D：领域中立条目选择器与四类型深链helper，按章只读预览，搜索失败保留所选；连接卡片映射UI完成真实操作。
- [x] canEditNow／身份代次／projectRefresh失效覆盖全部弹窗和行操作；适配375px、主题、键盘，不加依赖。

每个包运行相应API／draft／format／URL测试，lint和build；UI不能用原始JSON/ID代替选项和名称，旧字段来源可见且可显式关联。

## 最终字段门禁与交接

```bash
# backend/
uv run ruff check .
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest
# frontend/
npm run lint
npm run build
npm test
```

- [x] 真实PG迁移／CAS／引用FK／项目级联证据齐全；测试都用隔离库和FakeLLM，不碰用户作品。
- [x] 真实浏览器四类字段、四实体、配置、早晚章节、停用、旧字段关联、两页冲突、草稿、只读、卡片映射及窄屏通过。
- [x] `trellis-check`检查本子任务全部差异并修复，不只最后一片。
- [x] 更新字段spec和前端索引，记录API／版本／批量projection交接；满足F-AC1–11才允许分类开始。
- [x] 父待办此时保持未勾选；提交／归档按父任务统一流程和用户批准处理。

## 回滚点

S1迁移数据不可逆损失则阻止执行并先修复；S4或S6语义失败则后续交付暂停，不能用静态新字段替代。回滚前保存完整v4备份；有新语义数据不执行有损downgrade。只回退本任务差异，不重置他人修改。
