# 字段 → 分类实施交接

本文件记录已经实现的字段落点与后续所有权。字段最终完整后端2146通过／1旧可选PG跳过（真实退出0），前端129文件914例与真实界面验收通过；已于2026-11-06完成前置核对并进入分类实施。已批准产品语义仍以两个子任务design.md为权威。

## 已实现且应直接复用

- `.trellis/spec/backend/setting-fields.md`是实际跨层合同；field-backend-check／field-frontend-check报告保留验证范围与复验记录。
- `setting_catalog_service`的EntityRef和跨类型目录，公开kind为character/world/organization/career；独立职业timeline为career_entry。
- `setting_field_service.resolve_entry_fields(db,pid,entries,position)`返回公开EntryFieldsOut，值与当前名称已解码；`FieldSpec`／codec及纯resolver不能另造。
- `position=None`含所有有效计划章；历史章节ID和计划章号互斥；当前标签／当前规则与时点下拉值混合口径须明示。
- `assert_definition_dependencies(...,replacement=...)`已被定义UPDATE/DELETE共同使用，`assert_option_unreferenced`统一选项删除；分类规则依赖应扩展此处，不只保护有字段绑定的情形。
- `validate_project_field_graph`已在导入后和effects恢复后调用；恢复还按TableRule.scope验证每一行。分类整图验证需要覆盖规则仅引用定义／选项的情况，不能只靠FK。
- SQLite共享绑定已在模型及0035使用AUTOINCREMENT；新类别／规则同样使用不可自动复用身份。不要用值形状或同名推断旧身份。
- 字段完整门禁诊断额外修复`api/open/docs.py`的全局缓存→闭包→app保留根；模块级handler通过Request.app取实例。保留`test_open_docs_do_not_retain_closed_applications`及质量规范的新寿命合同，不通过清框架缓存或增加SSE超时回避回归。完整证据见`docs-retention-retrospective.md`。
- `EntrySelect`注入`{q,page,page_size:30}` loader；已验证真实36条目录的分页焦点、Go to／Enter／Escape及跨页保留。复用四类型entityLinks，历史位置在外层open回调捕获后传只读预览。

## 顺序与文件边界

1. C1模型／0036／迁移回归由单owner持有，基于已有真实0035数据。当前0035回归包含完整ORM结构对照；0036新增结构后须将旧迁移预期精确固定到0035，并让0036承担完整当前ORM对照，保留全部旧约束与数据断言，不能删断言来消除版本差异。分类未开始时不提前写产品文件。
   规则→定义／选项两条NO ACTION FK已由真实PG证明需DEFERRABLE INITIALLY DEFERRED以支持整项目级联；单删仍在COMMIT／SET CONSTRAINTS时拒绝，C3服务主动409不依赖延迟报错。具体证据在C1报告和design.md，不改0035。
2. C2先冻结规则／标签公共schema、纯规范化和匹配，再接批量查询；先完成对字段projection必要的可选范围扩展并释放该共享文件，再让C3修改同文件依赖保护。
3. C3复用C2 DTO和查询接口，负责CRUD／标签写入／权限／审计及规则依赖保护；query service和write service不交叉抢写。
4. C4待持久／图校验接口稳定后处理project v5、cards v5、effects v4；registry、transfer、card和effects依次单写者，保留字段阶段全部恢复合同。
5. C5／C6由types.ts唯一owner先冻结客户端；分类页／规则／结果与四详情独立标签区可按无重叠文件拆分。所有层完成后再做完整分类check与父整体验收。

## 批量查询的实施边界

- 候选目标200条／批，先完整规则匹配再累计total／请求页；tag-only规则不需要加载字段历史。
- 200是内部批量目标，现有公开`/setting-entries`的page_size上限仍为100；复用内部查询原语，不为分类放宽已冻结公开目录合同。标签建议也需有界返回与保留集合，不能先把全作品所有标签装入Python再切片。
- 当前字段projection默认读取完整目录和该批全部字段。分类若原样逐批调用，会多加载无关定义／选项／handles；实现应先由owner给出兼容的可选definition范围参数并冻结，SQL只查规则所需dropdown及事件，默认公开字段读取行为保持不变。
- 同一查询的所有批次使用独立只读一致事务：SessionmakerDep创建session，任何SQL前配置PG REPEATABLE READ或SQLite显式BEGIN；重新校验归属／生命周期。不能把已认证启动的请求DbDep事务改隔离级别。
- 保留no_autoflush和前后代次检查；最终要用实际并发屏障证明整批一致或明确冲突，单元匹配函数绿不能替代此证据。
- C2/C3规则引用共用`validate_category_spec(...,retained_rules=())`，不提供全局allow_archived开关。retained身份只来自服务器已存规则；draft预览可带仅该分支合法的editing_category_id，并在同一快照内加载旧规则。新建／新增条件仍要求可选状态，不能由请求自报历史身份。

## 必须保留的边界

- 独立整理标签不加进旧CharacterOut等开放读取DTO，也不自动进入AI消息；专用tags/category响应与cards v5显式承载。
- POST分类preview是只读权限的精确例外，不能放宽其他POST。配置／标签写入沿项目屏障、真实actor审计和CAS。
- 目录冲突保留原稿和原版本，读取最新后明确核对；前端拼合多HTTP结果同时核对timeline和fields_revision，失败重试需独立请求代次。
- 备份和effects分别排除fields_revision／categories_revision；undo按当前高水位递增，失败不增。旧安全正文效果继续可撤销，不一刀切拒绝旧版本。
- 升effects v4时注意旧v3实体快照没有setting_tags列：不能通过补空数组恢复而清掉后来整理的标签；按已有整轮旧格式预检判断可证安全范围，并保留正文／标题等安全撤销。标签变化与分类规则变化都应触发categories_revision，不能只看新表delta。
- 旧版本升级／未来字段处理严格按分类design.md执行，不能借“兼容”绕过类型、规则归属或全历史保护。
- C4旧版策略已收敛：合法旧版缺失／规范空数组补空；非空未来标签／类别／规则或非法形状明确拒绝，不静默清理。v5两分类核心表必须存在；旧字段typed闸门不退回。cards同策略，skip／职业复用不覆盖tags，无world新容器。两revision只作运行代次排除，不继承源值。
- 真实UI仅使用本任务临时SQLite/FakeLLM；8000用户服务及用户数据不触碰。fields_reader/projects.read的隔离验收已获用户明确批准，授权随该测试环境结束。
