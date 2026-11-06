# 实施记录

## 2026-11-05 启动

- 用户在最终完整摘要后回复“开始”，批准两个顺序子任务。
- 功能分支：`codex/custom-setting-fields-categories`；字段子任务已 `task.py start`，父任务协调，分类保持等待。
- 工作区原有 `pelican-cycling.html` 不属于任务，后续不纳入提交。
- 前端未改动时的基线：`npm run lint`、`npm run build` 均通过。
- 本机8000端口已有服务，不触碰其数据或进程。后续UI验收另起隔离SQLite/FakeLLM和端口。
- 专用临时PostgreSQL16容器：`ainovel-fields-verify-20261105`，仅本机32814，库`fields_verify`、用户`field_test`，仅测试资料；不连接用户数据库。
- 当前无Chrome DevTools MCP；真实界面验收使用可用的内置浏览器能力，不能把源码或mock检查写成浏览器已验收。

## 首轮文件所有权

- `field_core_implement`：S1–S2，模型／0035迁移／字段codec与resolver／schema及相应测试；不碰transfer、agent、前端或新API。
- `neutral_entry_ui`：提前执行S10.D中与新API无依赖的复用抽取；只改通用EntrySelect、entityLinks及规划中的两个复用点和相关测试，不碰types.ts或路由。
- `field_api_implement`：模型合同明确后承接S3–S4，负责新配置／目录／字段值service与API、旧事件service适配及路由／授权；models、codec、resolver、迁移只读，schemas/progression.py待core明确释放后再改。
- core已释放schemas/progression.py给API单写者，自己继续0035、codec、resolver及定向测试。
- `field_frontend_implement`：S10.A-B，types.ts唯一写者、新settingFields API／配置页／导航及必要projectRefresh、审计中文映射；暂不改四实体或progressions/cards。
- `field_lifecycle_implement`：S5，story/org/career/wizard/book_import入口及测试；只调用API worker发布的初始化／依赖保护helpers。
- `field_transfer_implement`：S7/S8后端，project_transfer全目录与card schema/service/routes及测试；不碰agent/effects或前端，卡片映射UI后续交接。
- `field_context_implement`：S6，context_builder/planning_context/analysis、新纯setting_field_formatting及实际消息测试；不写API service/schema。
- `field_agent_implement`：S9，agent tools/effects/service及测试；registry/remap只读并与transfer协调。
- 主会话负责任务状态、规划同步、环境与集成；待基础合同稳定后分派S3–S9和其余UI。

### 核心接口交接（实现中）

- FieldSpec及validate/encode/decode由`setting_field_values.py`唯一持有；字段API不重复校验器。
- resolver新增field_specs关键字，field_values是canonical类型化映射，custom_fields保留text字符串投影。
- uv缓存若被沙箱阻止，使用本任务`UV_CACHE_DIR=/tmp/ainovel-fields-uv-cache`和`uv run --no-sync`；不降低测试门禁。

## 检查约束

每个切片先定向验证；最终字段完整门禁通过后才启动分类。主共享文件同一时刻单写者。全部完成之前不勾选TODOS第4项，不自动提交。

## 已到达的验证点

- S1–S2 首轮107个核心定向用例通过，随后加真实PG用例共108通过。PG随机schema验证0034→0035→0034→0035、原文／时间戳／事件保留、JSONnull、四目标NO ACTION单删与整作品级联；无需额外deferred。
- 核心物化兼容曾发现EffectiveSnapshot新增field_values没有默认值，已用default_factory=dict保留旧构造方式，未删除旧测试。
- 旧0031迁移测试若直接同当前模型对比需按版本合同固定旧列／索引；0035测试保持完整当前模型比较，不能以删断言放宽。
- S10.D独立抽取8文件完成：针对26例、完整前端123文件876例、lint及空白通过。隔离假数据浏览器验收覆盖分页外选择／失败重试／中止／迟到响应／键盘／375px。该证据只代表通用组件，不冒称完整字段产品UI已验收。
- 并行新增AuditField导致最终一次build暂缺两项中文标签，已交types/audit唯一owner补；先前构建通过，最终完整构建仍待全部改动收敛后运行。
- neutral_entry_ui已进入S10.C四实体字段／typed时间线接入新一轮，持对应Panels/progressions/api-progression客户端；types.ts、settingFields.ts、配置页/router仍field_frontend_implement持有。

## 第二轮集成与证据

- S1–S2局部独立check完成，仅补充测试；最终117 passed，含真实PG16、完整ORM／迁移结构、停用／孤儿历史降级保护和JSONnull检查。报告见research/core-check.md。本报告不代替字段整体check。
- S5原入口及生命周期完成，定向及既有回归197 passed；覆盖四实体引用、向导覆盖、软删除／恢复／purge。
- S6实际模型消息与既有上下文回归158 passed，其中43项新测试验证四类值、第5／20／21章、引用不递归及跨批目录一致性。六项读屏障经历RED→GREEN。
- S10.A-B配置页与API完成；该轮完整前端125文件890例、lint/build通过。卡片v4映射前端已另行分派给同一worker；四实体值面板仍由neutral_entry_ui持有。
- 作品备份旧版测试必须使用真实旧版结构；仅将v4的version改为2不再视为合法v2。保留产品严格闸门，最小修正test_planning_lifecycle对应fixture，不删除其原规划清理断言。
- 隔离真实后端8014与前端5179已启动；fixture作品1具备四实体、四类共享字段及第20章章末事件。真实用户服务8000未改动。环境详见research/runtime-environment.md。
- 内置浏览器已登录隔离账户，字段配置四类型及适用范围正确显示；已实测键盘选择范围、新建字段、收起／重开保留草稿及保存成功。值面板、按章与卡片映射仍待本轮实现收敛后验证。

## 字段整体检查与真实界面验收

- 前端完整独立check已修复预览同版本失败重试、跨HTTP双版本核对、可键盘清空和行确认框身份失效；最终lint／build通过、129文件914例通过。卡片映射标签追加修复后scope lint／build通过，无DTO变化。详见research/field-frontend-check.md。
- 用户截图要求“显示顺序：0”和编辑字段保留间距；卡片操作行改为独立一行及16px间距，真实375px宽度（scrollWidth=375）复验并展示截图。
- 浏览器实测：四实体四类字段名称和引用摘要；角色第5／20章start旧值、20end与21新值；地点／组织／职业历史深链与5／21章；组织成员／职业持有人均未因独立字段新增。
- 职业单行起始值独立保存，外层职业名称未保存草稿保持；角色空多行字段关闭重开保留两行原文并首次保存；空下拉字段不先建基础值，直接原子保存计划第12章首事件成功。
- 真实引用搜索明确选择青云山，显示类型与名称；“清空驻地”按Enter后经最新版本对照保存null；旧本地字段经显式目标及对照勾选关联，原文保留并从legacy区转共享区。
- 只读账户创建／授予projects.read初次被自动审批拒绝；用户后续明确批准仅8014隔离环境的fields_reader，已执行成功。实际登录后可浏览历史／字段配置，记录、保存、清空均禁用。该授权不涉及用户8000实例。
- 卡片由真实8014/cards/export产生v4文件。先保持同名跳过，再将林岚重命名新建；四定义显式复用、两个下拉选项逐项映射、包外幽冥城明确选target后才可确认。真实导入成功：新角色1、跳过组织1；新角色四字段为最新快照，事件列表空，旧组织保留。
- 浏览器证据临时文件：/tmp/ainovel-field-spacing-375.jpg、ainovel-field-readonly.jpg、ainovel-card-explicit-mapping.jpg、ainovel-card-import-complete.jpg、ainovel-field-values-375.jpg。375px字段抽屉实际bounds为0–375，无横向溢出；明暗外观均已观察，临时viewport已reset。
- 后端独立check补充真实PG服务／HTTP CAS、导入整图与失败回滚／卡片导出竞争3例无skip；修复transfer旧版仅配置夹带、空绑定往返、卡片sort严格int32和effects事件↔绑定归属等边界。
- ID复用追加正式RED：同timeline旧空绑定删除后新定义可能复用field ID，旧effect错误恢复。已批准在未交付0035及模型同步SQLite AUTOINCREMENT，不抢占分类0036；正在受影响门禁及最后完整重跑。修复前完整2143通过只作中途证据，不冒称最终通过。
- 身份修复后受影响core／迁移／effects／transfer／cards与字段PG套件298 passed、旧0031可选PG一项skip；最终全量仍由checker运行。旧0031要求独立ainovel_test库，本任务仅获授权fields_verify，不能修改该旧环境防护骗过门禁；本任务0035和三项服务PG均实跑无skip。
- 最终0035新库真实页面再次确认5章旧值与21章新值、控制台error为空。真实停用青云宗选项后，5章仍显示“青云宗（已停用）”；新事件键盘选择跳过该停用项，已将选项恢复启用，不留下测试配置偏移。
- 身份修复后首轮完整后端2144通过、1失败、1可选PG跳过；失败在关系生成的SSE测试等待10秒超时，尚未进入业务断言。原样独立复验两次均通过，只证明独立未复现，不据此归因为环境。
- 已关闭本任务IAB标签并停止8014／5179，仅保留本任务PG容器及不触碰的用户服务。安静环境第二轮完整测试989.71s，2144通过／1失败／1可选PG跳过；失败为 `test_progression_writeback.py::test_early_analysis_writes_end_event_without_overwriting_later_state`，同为首次任务流10秒超时，未到业务断言。checker继续原样复现与任务／订阅／事件循环时序诊断；不增加超时、不删断言、不将重跑当作根因修复。分类继续等待字段最终门禁。

## 2026-11-06 最终字段门禁诊断

- 当前前端全部改动（含最后的卡片映射标签与分页焦点修复）由独立checker顺序复验：lint／tsc＋Vite build／前端空白检查均退出0，129文件914用例于00:00:29全过（6.98s）。Node localStorage实验性警告原样保留，未过滤或改门槛。
- 第二个后端失败节点原样独立重跑两次均0.71s通过，全部早晚章回写和revert断言执行；两个失败节点同进程临时日志小例2通过（3.50s），任务终态后done正常出队、流结束，尚未复现根因。仅/tmp诊断插件记录终态／队列／loop／GC，产品与原10秒超时未改。后续先相关模块同进程定位，不盲目重复全量。
- 两相关模块带日志同进程50通过（32.19s）。随后一次原顺序1246节点受控前缀退出0：1245通过／1旧可选PG跳过（572.71s），未复现目标超时。日志捕捉到stream中gen2 GC 4.054s与loop lag4.030s重叠；全前缀最大gen2 9.907s发生于目标用例前23.61s，目标两流均<0.08s。目标前当前RSS约4.53GiB、GC对象约2932万，仍不能将其直接认定为两次10秒失败根因。checker继续卸载流包装后的少量fixture弱引用／referrers诊断，定位保留来源，不改超时／断言／门槛。证据前缀`/tmp/ainovel-fields-backend-01a1ac16-prefix-diagnostics*`。
- 独立8例结束后funcargs已清空，手动GC后8/8 app、TaskEngine、DB engine仍活；无stream插件。另2例反向图确认全局根链：`fastapi.dependencies.models._is_async_gen_callable_cached` → `_CallIdentity.call` → `install_docs.<locals>.openapi_schema` → closure cell → 已结束app（`retention-root.jsonl`）。这是实例保留根的直接证据，尚不冒称原两次超时已通过修复验证。
- 主会话授权checker对`api/open/docs.py`及正式回归做最小必要修复：模块级handler通过Request.app使用实例缓存，安装函数仅登记路由；必须保留路径／匿名文档／实际API Key检查／root_path／缓存行为。先永久回收测试RED→GREEN，不清框架LRU、不改原10秒；既有open_api和相关模块过后再跑最终完整门禁。
- 正式弱引用回归旧实现1失败（1.67s）→修复1通过（0.53s）；独立8例修后5.81s全过，窗口外GC后app／TaskEngine／DB engine／动态schema弱引用全部为空，ModelField与Dependant恢复原基线。只读独立审阅通过；原样无诊断插件的open_api22项＋相关任务流50项共72通过（50.26s），全后端Ruff／compileall／diff检查通过。已授权修复后最后完整门禁，产品冻结。
- 防复发分析见`research/docs-retention-retrospective.md`；后端质量规范与跨层指南已同步实际寿命契约。本应用无Trellis模板目录，不创建无关副本；提交按父任务统一最终计划处理。
- 修复后最终完整后端真实退出0：2146 passed／1旧可选PG skipped／1既有SDK warning（1006.15s），日志`/tmp/ainovel-fields-backend-01a1ac16-final-docs-fix.log`。checker释放全部产品／测试ownership，仅继续报告。字段F-AC1–11、完整check、PG／实际AI消息／前端／浏览器均已闭环；字段保持未归档、未提交，按父任务最终统一处理。
- 再次核对head0035、分支`codex/custom-setting-fields-categories`、全仓已跟踪空白检查及分类10／11条上下文验证通过；用户既有批准覆盖两个顺序子任务，无需再次征求开始许可。分类现在进入C1，TODOS第4仍未勾选。

## 分类C1所有权

- `field_core_implement`承接C1，唯一持有models/entities.py及注册、新categories模型／0036迁移／迁移回归；旧0035产品迁移冻结，仅可将旧版本测试结构预期固定到对应版本，保留约束／数据断言。
- C2及后续尚未开始，field/progression services、API、transfer/cards/effects和前端暂无新writer。用户8000和真实库不操作，临时UI仍停止；PG仅fields_verify随机schema验证。
- `field_api_implement`仅只读准备C2的DTO／批量projection可选范围／一致事务接口方案，不编辑、不测试；待C1通过后再明确授予实施权，不提前启动C2产品改动。
- C1最终136 passed（33.82s），含真实PG／SQLite新旧迁移、完整0036 ORM对照、旧四字段早晚章投影、FK与降级／稳定ID保护，Ruff／编译／空白通过。PG实际发现规则NO ACTION检查顺序阻止整项目级联，已仅两条rule→definition/option FK使用DEFERRABLE INITIALLY DEFERRED；真实COMMIT／SET CONSTRAINTS单删和缺引用仍拒绝，项目级联无孤儿。RED日志`/tmp/ainovel-c1-pg-cascade-red.log`。0035产品迁移未改，全部ownership已释放。

## 分类C2所有权

- `field_api_implement`正式接C2：新schema／normalizer／纯matcher／只读一致query service与测试，catalog候选游标批原语、field/progression必要可选scope。模型／迁移冻结；本轮无API CRUD／权限／删除依赖保护、transfer/cards/effects或前端writer。
- 内部kind顺序character→world→organization→career；候选200／批，公开目录100上限不变。definition_ids=None保留默认全量，显式范围不读legacy，分类dropdown不读handles；通用reference范围仍应正确解码。仅字段投影可用部分scope，不送到完整facts／物化／AI消费路径。
- 引用校验冻结为`validate_category_spec(...,retained_rules=())`，无allow_archived全局开关；旧停用身份由服务器规则识别。编辑草稿预览在同一快照根据editing_category_id载入原规则，新增条件仍要求active。
- `field_transfer_implement`仅只读准备C4版本升级／旧版夹带边界方案，不编辑／测试；registry／transfer／cards尚无writer，仍须C3完成后才实施。
- C4只读方案完成，root将design“按版本清理”精确收敛为：合法旧包缺失／规范空[]补空；非空未来分类语义或非法形状明确拒绝，不静默丢数据，字段旧typed闸门保留；v5要求核心两表。cards同策略、skip/职业复用不覆tags，无world新容器；两revision排除不继承。此为既有兼容与资料保护合同的具体化，无新用户许可问题，C4仍未开始。
- `field_agent_implement`仅只读准备effects v4／旧v3缺标签列的整轮安全预检，不编辑或测试；agent/effects、registry仍无writer，待C3／拓扑接口稳定后另行授权。
- effects只读方案已接受：当前旧v3完整行比较会因缺tag列冲突，并非已在静默清标签；兼容只忽略合法旧UPDATE两侧都缺的tag比较列，恢复payload继续省略以保留当前tags。旧新建实体后来有tags必须整轮拒绝删除；旧删除恢复仅原PK确空且旧默认可证安全才空tags。两revision四处排除／高水位、rule-only范围保护及显式整图校验均待C4实现，不泛化忽略其它列。
- C2中途checkpoint：默认字段／progression／实际FakeLLM context88通过，pure30通过；新query SQLite11（含4屏障）、真实PG4跨批屏障通过。catalog／field／progression共享文件已冻结释放，schema/query仍C2持有并补no-N+1／位置／只读回归；不将中途结果当C2最终门禁。
- `field_lifecycle_implement`仅只读准备C3 API／权限审计／依赖保护与图校验文件边界，不编辑或测试。C3实际writer尚未启动，等待C2正式释放。
- C2正式收口并释放全部产品／测试：分类53 passed（30pure＋23集成，20.29s），含SQLite／真实PG各4个双连接跨批屏障；默认字段／时间线／实际FakeLLM context88 passed（45.66s），此后共享产品未改。Ruff／compile／受管和7个新文件空白检查通过。部分投影是私有field-values map，不改变完整resolve_timelines API；空scope和dropdown无legacy／handles，无N+1、位置与只读细节均已覆盖。

## 分类C3所有权

- `field_lifecycle_implement`正式写C3 service／routes／tests、C3写DTO与版本响应、field_service的rule-only依赖保护、router注册和AuditField。C2query/matcher/catalog/progression、模型／0036冻结；transfer/cards/effects/frontend仍无writer。
- Create/PATCH带fields与categories双CAS；Delete仅categories；Tags仅categories，null拒绝、[]清空。配置响应两版本与内容同一快照，preview明确projects.read，其它写projects.edit＋项目锁＋真实actor审计。graph无写无commit且tags须canonical，C4导入／effects显式复用；tags赋值原语将给C4用。
- C3核心HTTP／graph首轮21通过，DTO/API合同已发布冻结供客户端镜像；C3仍持后端writer补普通GET双连接快照／PG／失败回滚／canonical图，不将中途结果当C3整体完成。AuditField新增setting_categories、setting_tags；apply_entry_tags(db,pid,kind,entry_id,tags)调用者授权持锁＋一次CAS、只flush；validate_project_category_graph(db,pid)无写无commit。
- C4 agent只读追加核对发现实际AI输出风险：旧read工具均显式DTO无tags；角色／world写工具返回ORM后tools/base.serialized全列会自动把新tags送下轮模型。后续C4仅ORM输出排setting_tags，保留DB/effects内部全列，并用execute＋实际下一轮FakeLLM消息验证；当前尚未修复，不扩大read/references。

## 分类C5.A客户端所有权

- C3接口已冻结且核心测试通过，`field_frontend_implement`先独立写types.ts（唯一owner）、settingCategories API／请求测试、AuditField中文标签、projectRefresh新scope；不碰页面／四详情或cards v5（待C4后端契约）。C3继续其后端回归，前后端无重叠writer。
- C5.A完成并释放：新分类16＋旧字段请求3共19例通过，scope ESLint、build与新文件／受管空白检查通过；客户端`settingCategoriesApi`、`settingCategoryToInput`剥ID深复制、冲突scope／ruleIndex解析、`setting-categories`资源及中文Audit标签冻结。cards和旧四实体DTO未改。
- `neutral_entry_ui`正式接四详情独立TagSection（components/settingTags及四Panels／CareerEditDrawer），只读API/types；保留外层草稿、actor/版本/请求失效、显式冲突对照与375px键盘。分类页面不归它，后端只读。
- C3最终41例（37默认＋PG4）通过；合并C3／C2／字段API／生命周期／实际context／progression共220 passed（117.32s），Ruff／编译／空白通过，全部ownership释放。普通GET各SQLite／PG快照、CAS失败全回滚及canonical图验证均闭环，DTO无改动。

## 分类C4与C5.B／C6所有权

- `field_transfer_implement`正式持project_transfer与cards后端／相应测试，先registry反射及拓扑／双revision排除冻结，再projectv5／cardsv5；0600 PG env复用同库随机schema，不另造库。C3helpers及Graph只读复用，agent/effects暂未开始，等待registry发布。
- Cardsv5 preview返回categories_revision，confirm强制expected_categories_revision并一次CAS／锁；旧v1–v4无新tags写意图保留旧请求兼容，v4字段／引用显式映射不退回。精确card DTO由backend发布后再由唯一frontend owner镜像。
- `field_frontend_implement`接分类页面／components/settingCategories、router/nav、规则草稿及结果／历史深链；不改TagSection或四Panels。types仍它唯一writer，cards暂不改。两UI owner完成后统一协调global build与全量测试，避免修别人施工中的文件。

## 分类C4–C6完成与最终检查

- transfer/cards最终108通过（非PG）及真实PG4通过；project／cards v5、APP_SCHEMA0036、严格旧版补空／未来载荷拒绝、规则三FK拓扑、双revision排除、软删恢复／整库快照闭环。cards只new／rename写tags，skip／职业reuse保留，v5确认带categories CAS，原v4显式字段／引用映射继续执行。
- C3纯拟恢复图校验追加35例，合并query／API129通过含PG；C4不复制规则语义，effects在首restore前调用共同纯图内核。新effects格式4、旧版整轮闸门及真实AI工具ORM输出tags泄漏修复通过216项集成，数据库／effects完整tags保留，FakeLLM消息不自动携带。
- effects真实恢复后失败会残留ORM缓存，已用fieldGraph、career materialize、preview、整轮前项成功后后项失败和commit失败证明并修复。整轮commit成功前不expunge新建identity；失败局部刷新本轮实例＋Project，不改变全局session，不用前置tag校验掩盖通用错误。独立check又复现Task.cancel漏过Exception捕获，补BaseException同路径刷新及原异常重抛。
- SQLite四实体ID复用被真实HTTP RED发现：旧标签请求可命中新行。core独占四文件，在未交付0036和ORM补AUTO；最终154通过含PG。16场景覆盖create_all／实际Alembic×四实体×删最高／删空，旧GET／PATCH404且新tags与revision不变；保留全图旧行、时间戳、FK原开关、其他表sequence高水位、无临时表与0035精确空态回环。此后才创建分类UI新库。
- frontend独立check补Python Unicode strip共用（tags／规则／分类名）及成功删除forget、有效404清缓存，防止旧草稿复用；四详情外层姓名／基线／职业阶段与标签独立。首轮136文件980测试绿。
- 真实CUA发现计划空锚URL丢模式／边界、自写保存广播误stale，两项分别4＋2例RED→GREEN。实际计划5／25和end、刚存刚开复验通过；最终frontend lint／tsc＋Vite／diff为0，136文件986例于02:38:04开始通过，既有5条Node警告原样保留。

## 分类后端完整门禁与测试建模隔离

- 首轮完整1210.18s：2405通过／2失败／1旧0031可选PG跳过，exit1。一个旧规划断言仍要求角色ID复用，按0036合同精确改成严格递增，保留并加强JSON／POV／旧行删除／revision断言。
- 另一项大纲替换仅完整序失败：真实PG卡片→原样SQLite大纲的最小顺序1通过／1失败，失败库缺plan_id外键。SQLAlchemy PG AddConstraint改变共享MetaData约束创建状态，后续SQLite根本未建FK，foreign_key_check也无法识别缺失约束。
- 仅测试增加create_isolated_schema，以to_metadata副本建PG并复用关系／风格／技能三组原预置事件，统一4处调用；产品模型／迁移／全局engine/session不动。永久回归RED→GREEN、原样真实顺序2通过、受影响9模块123通过后才开必要完整复验，不降低超时／删断言／清私有状态。
- 最终完整后端1174.18s：2408通过／1跳过／1既有SDK警告，真实exit0；全Ruff／compile／diff为0。唯一skip是旧0031专用ainovel_test未配置，本任务fields_verify的PG全部实跑。报告research/category-backend-check.md及/tmp/ainovel-categories-backend-01a1ac16-final-isolated-schema.log保留准确证据。

## 分类真实界面与外部权限阻挡

- 新ROOT ainovel-categories-browser-1onr9vwt，8014／5179／FakeLLM。真实ALL／ANY、5／20start／20end／21、计划25、221候选总数与第12页、名称后筛回第一页、两页规则及标签CAS、四独立标签、[]退出／恢复入类、A→B→A草稿、四历史深链、375明暗及键盘通过。
- 真实导出v5角色／组织／职业标签；同名skip后旧标签保留，rename新建后角色／组织标签带入，职业reuse保留目标标签。标签卡片用无字段值合成条目；原字段阶段实际v4引用映射另有报告，v5字段兼容由完整自动化证明，不混称一次验完所有路径。
- 读取失败重试和分类只读页面两项准备验收时，浏览器返回用户拒绝5179；用户重新授权并确认已解除后仍返回已保存权限／管理策略拒绝。每次CUA立即停止并记录completion，没有换端口／浏览器／工具绕过。8014曾为故障准备短暂停止，随后按最终代码恢复；失败UI动作未发出，不能记作通过。
- 已写research/category-browser-verification.md并保留截图。两项界面验收未勾；TODOS第4、C-AC8、父AC10与归档／提交仍待用户明确决定。父集成另补v5导入后源／目标四时点字段＋分类语义直接断言，仅新测试／定向验证，不重复无产品更改的全量。
- 新分类spec、前后端索引及transfer／story-entities／agent／quality规范已同步最终0036、v5、effects4、稳定身份、双CAS、独立草稿和跨方言测试隔离合同。

## 父级v5相同时点集成收口

- 新增唯一文件test_setting_fields_categories_roundtrip.py，真实HTTP四实体×四类型、16个20章end事件、当前标签及ALL／ANY；v5导入新project后对5start／20start／20end／21start逐点按label／名称／类型／tags比较固定真值表，并逐表验证新ID、双向修改隔离、跨项目引用404且revision不变。
- 合法包故意移除16事件的正式RED精确失败于(20,end)，1 failed in3.45s；健康导入、永久丢事件／end移start负控制及隔离全部1 passed in6.06s。新增＋直接相关备份／query模块及原typed往返共40 passed in48.96s，真实PG包含且无skip／warning；Ruff和新增空白检查通过。产品／原测试／fixtures未改，不重复完整2408轮。日志/tmp/ainovel-categories-backend-01a1ac16-parent-roundtrip-related.log。
- 父AC7／同位置备份验收已勾。外部阻挡的两项UI、C-AC8／父AC10、TODOS第4及任务归档仍保留未完成。已准备research/commit-plan.md中4个本地工作提交，222个本任务文件；pelican-cycling.html是原无关未跟踪文件，排除。未执行git add／commit。

## 2026-11-06 用户要求完整收口

2026-11-06：在已说明两项UI受站点策略阻挡的前提下，用户明确要求“提交 收尾 合并到main并推送”。按现有代码与验证记录交付收尾；未执行的只读分类页／失败重试仍保留人工未验，不记为通过。

- 原4个工作提交已执行，本地main已先同步远端3个文档／规范提交，再以5f84fcce5d04a911c5bae28159b44b330275d0a8合并功能分支。唯一backend/index冲突保留双方条目，1164个产品文件与已验证快照完全一致。
- 已按远端新规范同步README的功能、迁移及v5数据说明，并更新TODOS第4项；两项人工未验仍原样保留，未改写C-AC8／父AC10全浏览器条件。
- 已停止8014／5179与本任务PG容器，保留库／截图；无关pelican-cycling.html未纳入提交。接续归档父子三任务、记录journal与推送main。
