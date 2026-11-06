# 分类子任务完整后端独立检查

日期：2026-11-06。Active task：`.trellis/tasks/11-05-setting-auto-categories`。

范围为分类新增及其字段／备份／卡片／effects集成。字段阶段2146通过的完整独立检查作为已交付基线，未重复旧字段全文。前端与真实浏览器由主会话及前端checker负责，本报告不代表整个父目标完成。

**最终后端结论：四项发现均已修复。全backend Ruff／compile／diff检查通过；含真实PG的原样完整pytest为2408 passed, 1 skipped, 1 warning in1174.18s（19:34），真实退出0。已向主会话明确释放全部后端产品／测试ownership，随后仅写本报告。C-AC1–8的后端部分可交接；前端／浏览器及父目标由主会话最终收敛。**

## 所有权与依据

- 已按本任务check／implement manifests读取PRD、design、implement、setting-categories／fields／progressions规范与父category-handoff、冻结implementation-contracts、一致快照和规划门禁。
- 四实体ID不复用修复最初由core owner独占；收到最终154项通过及正式释放后，已独立复查`models/entities.py`、0036迁移、分类迁移测试及新entry_identity测试。core释放前没有运行完整门禁或修改保留文件。
- 0035产品迁移及全局engine/session策略保持冻结；其余分类后端已获独立审查和局部修复授权。不提交、不改任务状态，不触8000或真实数据库。

## Findings (fixed)

### 1. 智能体备份往返仍断言旧APP_SCHEMA

- File：`backend/tests/test_agent_api.py::test_agent_versions_and_success_error_logs_survive_backup`。
- Issue：实际project v5已经按0036导出／往返，旧断言仍要求0035，阻断真实成功／失败日志兼容验收。
- Fix：精确更新为0036，保留全部版本、成功／失败日志及agent.turn归属断言，不改产品版本护栏。
- RED：**1 failed in1.21s**，实际0036与旧0035断言不等，日志 `/tmp/ainovel-categories-backend-01a1ac16-agent-schema-red.log`。GREEN：**1 passed in1.05s**，真实exit0，日志同前缀`agent-schema-green.log`。

### 2. 单次撤销的任务取消漏过回滚后ORM刷新

- File：`backend/app/services/agent/effects.py::undo`；回归`backend/tests/test_setting_category_agent.py::test_cancelled_single_undo_refreshes_restored_objects_in_same_session`。
- Issue：Core恢复并刷新同会话对象后，真实`Task.cancel()`产生`CancelledError`（BaseException），原`except Exception`不执行已有`refresh_after_rollback`；保存点已正确回滚数据库，原持久对象却仍显示临时恢复值。
- Fix：仅扩展现有异常边界为`BaseException`，继续复用仅本轮identity+Project的no_autoflush刷新及原异常重抛。不改超时、任务引擎或公共DTO。
- RED：**1 failed in0.96s**，数据库新分类名与原对象identity均正确，精确失败于对象仍显示旧分类名；`/tmp/ainovel-categories-backend-01a1ac16-cancel-undo-red.log`。GREEN：**1 passed in0.70s**，真实exit0；同前缀`cancel-undo-green.log`。受控屏障在Core恢复、ORM刷新之后，只取消一次，未通过睡眠猜时序。
- 补齐整轮commit失败证据：`test_turn_commit_failure_restores_identity_tags_revisions_and_change_status`，所有restore／状态／消息均flush后受控拒绝commit；原分类、已有条目及新建条目对象仍为同一持久identity，回滚恢复标签、双revision、executed状态且没有撤销消息残留。**1 passed in1.01s**，真实exit0；同前缀`commit-failure.log`。

### 3. 旧规划回归仍要求角色ID复用

- File：`backend/tests/test_planning_lifecycle.py`。
- Issue：旧wizard角色替换回归要求删除后自动复用ID，与0036已批准稳定身份合同矛盾。
- Fix：精确改为新ID严格大于原两角色最大ID，追加旧两行确实删除的断言，保留全部JSON／POV引用清理与revision递增检查；用例改名为`test_wizard_character_replacement_cleans_json_without_reusing_identity`。
- RED：原样定向仍精确`3!=1`；修后大纲／规划两模块**35 passed in17.71s，exit0**，同前缀`failures-modules.log`。

### 4. PG测试建模污染共享MetaData，后续SQLite漏建细纲外键

- File：新增`backend/tests/schema_helpers.py`与`test_schema_isolation.py`；四处PG调用点为`test_setting_fields_postgresql.py`、`test_setting_category_query.py`、`test_setting_fields_migration.py`、`test_setting_categories_migration.py`。
- Issue：PG `create_all`经AddConstraint修改共享Constraint创建规则，后续SQLite的`chapters.plan_id`外键被省略，导致大纲替换后细纲关联残留。根因及真实最小RED见下节。
- Fix：仅PG测试建库改用`Table.to_metadata`独立副本，显式复用已有关系类型／风格／技能三组after_create函数，原ORM全局metadata、产品模型／迁移／engine／session不改；不重置私有创建规则或清缓存。
- 永久回归同时检查PG DDL前后全部原metadata SQLite DDL不变、三组预置完全相同、真实SQLite的plan FK与删除级联／正文保留。**1 failed in0.30s→1 passed in0.17s，exit0**，同前缀`schema-isolation-{red,green}.log`。
- 原样真实PG卡片→原样SQLite大纲顺序：**1 passed/1 failed in6.46s→2 passed in3.85s，exit0**，同前缀`pg-sqlite-fk-{red,green}.log`；未改原大纲测试或产品。

## 独立静态审阅覆盖

- 数据与迁移：四实体`setting_tags`默认独立空数组，四实体及类别／规则SQLite自增身份；0036重建前检查旧FK、暂关并恢复原连接FK开关、保留既有ID／子FK／旧列值和其他序列；空态降级精确返回0035结构，有新资料先拒绝；PG原序列、规则NO ACTION+deferred与模型同步。
- 规则与写服务：严格互斥tag/dropdown载荷、稳定ID和项目父链、旧归档身份仅由服务器已有规则保留；统一纯校验供API／transfer／effects使用；规则专属依赖保护定义类型／适用范围及选项删除，即使无绑定／事件也拒绝破坏。
- 查询与上下文：独立SQLite真实BEGIN／PG RR READ ONLY快照中重新验证真实actor、token、归属及生命周期；有界200候选游标，完整求值后累计total，仅留请求页；metadata、实例和事件只投影规则所需定义。默认完整字段／AI上下文调用保持原语义，不把内部partial投影暴露为第二套求值器。
- 备份／卡片：project v5／0036、两张核心表、规则真FK拓扑映射；v1–v4仅缺失或规范空数组补空，非空未来标签／分类／规则及非法形状拒绝；原typed未来包闸门不放宽。卡片v5仅带包内条目标签、跳过／复用不覆盖现有标签，导入双CAS，失败整笔回滚，无新增world容器。
- Effects／授权：v4快照覆盖标签／类别／规则，整轮restore前纯图预检与legacy safe闸门；项目两revision在registry／独立快照／比较／恢复四处排除，受影响目录仅按当前高水位递增。单次、整轮、真实图失败、预览及commit失败保留同会话身份；成功commit后才移除撤销的新对象。实际ORM工具序列化过滤tags，effect快照保留；普通与开放DTO未追加标签；preview显式只读路由、审计只记安全字段。

## 失败轮与根因定位

首轮完整门禁真实退出1：**2 failed, 2405 passed, 1 skipped, 1 warning in1210.18s**。该轮不标绿或用分片结果替代；修复后的最终完整结果独立记录于Verification。准确失败及后续诊断如下。

1. `test_planning_lifecycle.py::test_wizard_character_replacement_cleans_json_before_id_reuse`依赖角色删除后ID复用，与0036已批准不复用合同矛盾；已将该唯一旧断言改为新ID严格大于旧两角色最大ID，并确认旧两行确实删除，原JSON／POV清理和revision递增断言全部保留。新名字`test_wizard_character_replacement_cleans_json_without_reusing_identity`。原样定向RED仍`3!=1`；修后两模块**35 passed in17.71s，exit0**，`/tmp/ainovel-categories-backend-01a1ac16-failures-modules.log`，最终完整门禁亦已复验通过。
2. `test_outline_snapshots.py::test_restore_after_wizard_replace`line60未全解除细纲关联：只读完整轮临时库，outline_id均为NULL，plan_id残留1／2，`PRAGMA foreign_key_list(chapters)`缺少plan_id FK；独立绿色库拥有该FK。原样两节点复现为**1 passed, 1 failed in1.52s**（仅旧角色断言失败）；最小真实PG卡片→原样SQLite大纲顺序重现为**1 passed, 1 failed in6.46s**（精确大纲line60失败），日志同前缀`pg-sqlite-fk-red.log`。SQLAlchemy本地源码`sql/ddl.py:1539,1159`证明PG `create_all`以`AddConstraint(isolate_from_table=True)`修改共享Constraint的`_create_rule`，后续SQLite建模省略use_alter FK。独立mock PG／SQLite实验证实相同因果；对克隆metadata做PG DDL则原metadata仍保留FK。随后按既有局部自修授权完成第4项，仅更改测试隔离入口；先取得永久回归、原样顺序及123项定向通过后，才执行修复后的最终完整验收。

该结构差异的最小RED数据库已复制至`/tmp/ainovel-categories-backend-01a1ac16-fk-failure-580.sqlite3`，与独立绿色库对照在同前缀`fk-database-evidence.log`；全部均为本次pytest独立库，不涉及用户库。仓库外具体候选方案`metadata-candidate.py`已exit0：每次`Table.to_metadata`创建独立元数据，显式复用现有三个after_create函数（关系类型／风格／技能）；PG DDL前后原元数据SQLite DDL完全不变，后续真实SQLite保留plan_id SET NULL，预置20／6／4项，删除大纲后细纲级联、章节两关联NULL、正文保留且FKcheck为空。日志同前缀`isolated-metadata-candidate.log`。未通过重置SQLAlchemy私有状态或清缓存让检查变绿。

## Findings (not fixed)

**无未修复的后端产品／测试问题，未擅自更改公共DTO或产品设计。**受影响九模块123项及最终完整2408项均已通过；首轮失败证据保留。规范同步建议交主会话：跨PG／SQLite测试建模用独立MetaData及原预置回调；Core恢复后的取消异常也必须执行仅受影响identity的回滚刷新。前端／浏览器与任务状态、提交、归档均不属于本代理交付范围。

## 验收合同与证据对应

| 合同 | 后端实际路径及回归 |
| --- | --- |
| C-AC1／2 | 严格标签规范化、纯ANY／ALL匹配、API标签变更与分类删除不删条目；`test_setting_category_matcher.py`、`test_setting_categories.py`。 |
| C-AC3／4 | 复用字段时点projection；5／20章start/end／21章、当前tags、显式清空、同名不同ID与停用身份；`test_setting_category_query.py`及既有默认字段／真实AI上下文回归。 |
| C-AC5 | 200行游标批次、跨类型完整匹配后分页、literal筛选、只读SQL、pending caller不flush、规则所需字段投影；真实SQLite／PG并发屏障覆盖跨批字段／标签／规则快照。前端迟到响应与深链由frontend checker另验。 |
| C-AC6 | 真actor及token/lifecycle重验、跨作品不可见、只读POST preview、管理员安全审计、categories／fields双CAS、仅规则引用的更新／删除保护；`test_setting_categories.py`／query／validation。四实体迁移与metadata两条HTTP建库路径共16场景证明旧GET／PATCH404且新条目ID严格递增。 |
| C-AC7 | 带真实0035资料的0036 SQLite FK ON/OFF精确回环、PG DDL／NO ACTION事务末约束与整项目级联；`test_setting_categories_migration.py`。`test_category_transfer.py`／cards覆盖v5往返、真实旧包升级、非法未来载荷、双代次及提交失败整笔回滚；PG参数实跑。 |
| C-AC8（后端部分） | 新旧effects／agent／open API／普通DTO／备份／字段默认上下文完整门禁；本报告不替代前端独立报告、浏览器实机及父目标最终一致性验收。 |

## Verification

- 两处完整轮失败修复后，九个受影响模块：schema隔离、fields PG、0035／0036迁移、分类查询、原大纲快照／规划、分类卡片／备份，**123 passed in110.25s，无skip，真实exit0**；包含四处PG建模调用点，日志同前缀`schema-affected.log`。
- 定向effects新旧集成：`pytest tests/test_setting_category_agent.py tests/test_agent_effects.py -q --tb=short -ra`，**98 passed in42.67s，exit0**，包含真实PG参数，无skip；`/tmp/ainovel-categories-backend-01a1ac16-effects-focused.log`。
- 全backend `ruff check .`：**All checks passed，exit0**；`python -m compileall -q app alembic tests`：**exit0**；`git diff --check -- backend`：**exit0**。最终日志为`/tmp/ainovel-categories-backend-01a1ac16-final-isolated-schema-{ruff,compile,diff}.log`。项目未配置独立静态类型检查工具，不把编译检查冒称类型证明。
- 完整门禁首轮：shell实际`.start`记录2026-11-06 **02:31:01 CST**（18:31:01 UTC），session **84247**，命令`uv run --no-sync pytest -q --tb=short -ra`；**2 failed, 2405 passed, 1 skipped, 1 warning in1210.18s（20:10），真实exit1**。独立日志`/tmp/ainovel-categories-backend-01a1ac16-final-pytest.log`与`.start`／`.exit`文件；本轮期间未改产品／测试、未插桩、未降低超时或断言。完成后才按准确节点定向诊断，不自动重复全量。
- **最终完整门禁通过**：**03:06:45 CST**（19:06:45 UTC）开始，session **95491**；**2408 passed, 1 skipped, 1 warning in1174.18s（19:34），真实exit0**，独立`.exit`文件为0；03:26:34 CST观察并确认进程已退出。日志`/tmp/ainovel-categories-backend-01a1ac16-final-isolated-schema.log`及`.start`／`.exit`。基于永久回归RED→GREEN、原样真实PG→SQLite顺序GREEN及123项定向GREEN后，执行必要完整验收，期间后端产品／测试冻结；没有临时插件、插桩、超时调整、过滤、删断言或新增skip。Ruff／compile／diff／环境前置检查重新执行且全部exit0，日志同`final-isolated-schema-*`前缀。真实0已先发主会话并明确释放全部产品／测试ownership，随后仅写research。
- 唯一skip：`tests/test_migrations.py::test_0031_postgresql_cascades_sources_and_subjects`要求另一个`AINOVEL_POSTGRES_TEST_URL`且只允许`ainovel_test`；本任务授权仅fields_verify，旧变量未冒充。本任务全部PG参数在完整轮实跑。唯一warning：既有`test_gemini_current_sdk_imagen_unavailable_is_chinese`产生SDK generate_images ExperimentalWarning／deprecation，未过滤或改依赖。
- 最终环境前置检查exit0：未设置`PYTEST_PLUGINS`、`PYTEST_ADDOPTS`、`PYTEST_DISABLE_PLUGIN_AUTOLOAD`、`PYTHONPATH`或字段诊断环境；命令没有`-p`包装，不加载/tmp插件；旧`AINOVEL_POSTGRES_TEST_URL`未设。最终日志`/tmp/ainovel-categories-backend-01a1ac16-final-isolated-schema-environment.log`。
- 使用UTF-8、`UV_CACHE_DIR=/tmp/ainovel-fields-uv-cache`、`uv run --no-sync`。真实PG仅从专用环境文件读取fields_verify URL，随机schema finally清理，不打印凭据或冒充旧0031的ainovel_test环境。

最终完整命令（`backend/`工作目录，已自动审批本机专用测试网络）：

```bash
source /tmp/ainovel-fields-01a1ab26-postgres.env
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  UV_CACHE_DIR=/tmp/ainovel-fields-uv-cache \
  uv run --no-sync pytest -q --tb=short -ra
```

## 父集成补充：导入新作品后的同组时点验收

日期：2026-11-06。补齐父`implement.md`的直接验收要求，不以备份canonical／ID映射一致代替导入后的实际查询。唯一新增落点为`backend/tests/test_setting_fields_categories_roundtrip.py`，其余产品、测试和fixtures保持冻结；未改任务状态、未提交或归档。

- 一个完整HTTP集成用例复用既有`_entry`／`_definition`／`_create`／`_tags`／`_backup`／`_import` helper：创建角色、world.place、组织、职业及四种共享字段，共16份基线和16个实际第20章`end`事件。reference由本作品world改为organization；事件使用实际chapter_id，覆盖新作品章锚映射。
- 源作品先按固定真值表验`5/start、20/start、20/end、21/start`，再经真实v5预览／导入路径创建新作品，对新作品重做同一组HTTP字段／类别查询。比较按字段label、选项label、引用type/name、条目type/name/current tags及匹配原因归一化；不以跨项目raw ID比较代替语义。字段同时验证base_value与当前value，reference摘要与本项目目录EntityRef必须一致。
- 当前tags仅角色／world含“同盟”，两分类使用同一组tag＋新阵营dropdown条件：早期ALL=0／ANY=2，20章end及21章start为ALL=2／ANY=4。四种类型的值都随20章end改变，当前tags在所有历史位置不变。
- 源／目标project_id不同；四实体、章节、时间线、定义、选项、handles、字段绑定、事件、类别和规则共13张有资料的核心表，行数相同且新旧ID集合逐表不相交。跨作品reference PATCH返回404、timeline revision不增、全部时点语义不变。
- 新作品修改角色起始口令及当前tags，旧作品四时点完整矩阵保持不变；再修改源world起始多行文本，新作品四时点完整矩阵保持其编辑后状态。起始值修改只影响事件前有效值，事件后仍继承既有剧情变化，证明两个作品的引用和修改隔离。
- **有意义RED**：在该新文件暂时将健康v5包的16个事件全部移除；文件仍合法且真实导入成功，原样语义比较精确在`(20, end)`同时发现字段值及ALL／ANY成员不等，**1 failed in3.45s，真实exit1**。日志`/tmp/ainovel-categories-backend-01a1ac16-parent-roundtrip-red.log`。随后仅移除临时故障注入，没有修改产品或放宽断言。
- **GREEN及永久负控制**：健康往返独立**1 passed in6.06s，真实exit0**，日志同前缀`parent-roundtrip-green.log`。用例保留两个合法损坏副本的负控制，并用相同语义断言证明必须报错：丢事件仅在`20/end、21/start`不同；把20章`end`改`start`仅在`20/start`不同。没有复制resolver、mock求值或扩展AI场景。
- **直接相关集成回归**：新文件＋`test_category_transfer.py`全模块＋`test_setting_category_query.py`全模块＋既有`test_typed_v4_roundtrip_remaps_all_history_and_resets_revision`，**40 passed in48.96s，无skip／warning，真实exit0**，含本任务fields_verify实际PG参数。日志`/tmp/ainovel-categories-backend-01a1ac16-parent-roundtrip-related.log`。原产品未变，按主会话要求没有重跑此前2408项完整套件；本补充结果不冒称新的完整套件计数。
- 新文件Ruff通过（exit0）；untracked文件以`git diff --no-index --check /dev/null <新文件>`检查，原始status1仅表示新增差异，stdout／stderr均为空，无空白错误，严格检查wrapper退出0。日志同前缀`parent-roundtrip-ruff.log`／`parent-roundtrip-diff.log`。

主会话已完整审读新文件并确认符合限定范围；相关模块绿色后已释放该新测试文件ownership，随后仅追加本节。未发现产品缺口。父任务／C-AC8／TODOS及两项尚未完成的UI验收仍由主会话单独记录，本补充不替代浏览器证据。
