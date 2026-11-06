# 整理标签与自动分类：模型、读写及查询合同

## 1. 范围 / 触发条件

维护条目整理标签、分类规则、候选目录或分类时点查询时遵循本契约。字段身份和求值复用[自定义设定字段](./setting-fields.md)，不增加成员快照表，不建立第二套resolver。

规则和标签使用当前整理版本，只有下拉值按所选章初／章末求值；名称同样使用当前显示名。标签不自动进入AI消息，不扩展旧CharacterOut等开放读取白名单。

## 2. 持久身份与函数签名

- `0036_setting_categories`依赖0035，四实体增加非空JSON `setting_tags=[]`，项目增加`categories_revision=0`，不复制作品tags。`SettingCategory`按作品唯一name、适用EntryType、必填all/any及sort；`SettingCategoryRule`保存tag或definition/option真FK。
- Rule CHECK严格互斥：tag有非空字符串且两ID空；dropdown的tag空且两ID非空。category/project级联，rule→definition/option为`NO ACTION DEFERRABLE INITIALLY DEFERRED`，支持PG多分支整作品级联，单删在COMMIT／强制约束检查时仍拒绝。模型与迁移一致，两新表及四实体均使用SQLite AUTOINCREMENT；0035产品迁移不再改写。
- 四实体稳定身份同时防止远端旧标签请求命中新条目。SQLite在0036中一次`batch_alter_table(..., recreate="always", table_kwargs={"sqlite_autoincrement": True})`完成tags列与表重建；仅ADD列或仅传table_kwargs不足以落AUTO。外键开关在迁移autocommit块、任何DROP／rename前处理，记录并finally恢复原状态，前后校验FK。PG继续原ADD列路径。
- 空新态可downgrade；存在标签或类别资料时在任何DDL前拒绝有损降级。空态反向重建四实体恢复0035原结构。升级／降级保留全部旧ID、原文、时间戳、aliases、org.parent、关系／成员／职业、字段／事件／handle／规划／回收站资料和其他表序列高水位，不留下临时表。保证0036后数据库自动分配不复用，不能声称找回升级前已删最高ID或阻止显式手写PK。

```python
# schemas/setting_categories.py
tags = normalize_setting_tags(raw_tags)  # object -> list[str]
# services/setting_category_matcher.py；纯函数
match = match_category(spec, entry_type, tags, dropdown_values)  # MatchResult
# services/setting_category_query_service.py；均不commit
await load_category(db, project_id, category_id)  # CategoryOut
await validate_category_spec(db, project_id, spec, *, retained_rules=())  # CategoryMetadata
async with category_read_snapshot(sessionmaker, authenticated_actor, project_id) as db:
    ...  # 只读一致事务
await query_category_entries(
    sessionmaker, authenticated_actor, project_id,
    category_id=None, draft=None, editing_category_id=None, filters=None,
)  # CategoryEntryPage；category_id与draft恰一
# services/setting_category_service.py；调用方持有授权／项目锁，均不commit
await check_categories_revision(db, project_id, expected)
await claim_categories_revision(db, project_id, expected)  # 一次CAS，返回新版本
await apply_entry_tags(db, project_id, kind, entry_id, tags)  # 只flush，不另领版本
await validate_project_category_graph(db, project_id)  # 只验证，无写入
# services/setting_category_validation.py；纯同步，不改输入
validate_category_graph_snapshot(
    project_id, categories=category_rows, rules=rule_rows,
    definitions=definition_rows, options=option_rows,
)
assert_canonical_setting_tags(tags)
```

`AuthenticatedActor`必须为现有authorization_service的真实身份，不从请求自报权限。retained_rules只来自服务器已保存规则；没有allow_archived全开关。编辑draft仅可用editing_category_id，在同一快照加载旧规则；新建传空retention。

路由前缀`/api/v1/projects/{pid}`：

| 路径 | 操作 |
| --- | --- |
| `/setting-categories[/{cid}]` | GET列表／单项、POST创建、PATCH修改／排序、DELETE |
| `/setting-categories/{cid}/entries` | GET完整匹配后的分页结果 |
| `/setting-categories/preview` | POST只读草稿预览，显式projects.read，不走写审计 |
| `/setting-entries/{kind}/{id}/tags` | GET／PATCH独立整理标签 |
| `/setting-entry-tags` | GET有界q／limit建议，默认30、最大100 |

其他写入显式projects.edit、项目active锁、真实actor的`AuditOperation/project.update`，由外层统一提交。普通配置／标签GET同样复用独立快照，不把认证session的旧Project代次配新内容。

## 3. 规范化 / 查询响应

- tags严格数组／字符串，按Python`str.strip`去首尾空白、去空、保序去重，大小写精确；规范化后最多50项，每项50码点。null、伪字符串与超限拒绝；[]表示空标签。前端标签、tag条件和分类名称共用`stripSettingWhitespace`；JS.trim会误删BOM且漏掉NEL／FS等Python空白，不能直接替换。
- `CategorySpec`为name、非空applicable_types、必填match_mode、sort及1–20条rules。kind判别tag／dropdown，禁止混入另一载荷和伪整数；option须属于同作品的dropdown定义，定义与分类适用类型有交集。
- `match_category`先判断条目类型，再ANY／ALL；下拉只比较option ID，缺字段／null均false。`MatchResult`返回matched和命中rule序号，显示理由由服务端使用当前名称生成。
- `CategoryEntryQuery`含types、q、page（默认1）、page_size（默认50、上限100）、互斥chapter_id/chapter_no及boundary（默认start）。position=None继承字段当前语义，含有效计划变化。
- `CategoryEntryPage`含category、categories_revision、fields_revision、position、items、total、page/page_size。item为SettingEntry＋setting_tags＋reasons；reason有rule_index、可空rule_id、kind、description及相应tag或definition/option身份和显示名。
- 保存分类可保留已有停用身份；新条件不能选择停用配置。保留身份不豁免归属、类型、选项分组或适用交集校验。
- Create／PATCH带expected_categories_revision和expected_fields_revision，Delete只带categories query版本；标签PATCH只带expected_categories_revision。PATCH省略保持，null拒绝，[]仅清标签，不允许空rules。只改名称／顺序且不提供rules时保留规则行；提供rules原子替换，成功逻辑写一次categories递增。
- `CategoryMutation{item,categories_revision,fields_revision}`、`CategoryPage{items,total,page,page_size,...两版本}`与Delete的`CategoryVersions`返回实际代次。`SettingTagsOut{entry,setting_tags,categories_revision}`，输入SettingTagsUpdate用setting_tags和预期categories版本；`SettingTagSuggestions{items,has_more,categories_revision}`。preview用draft、filters、可空editing_category_id。

### 前端状态边界

- `/projects/:pid/setting-categories`为懒加载页；列表、每id／new规则草稿、只读预览／已存结果分别持状态。`settingCategoryToInput`显式剥除行ID且深复制。名称／顺序单改不发rules；1–20条tag／dropdown条件可排序，显示服务端理由及当前整理口径。
- `categoryLocation`与`readCategoryLocation`保留未完成位置草稿：计划空号用`chapter_no=`，已有章空锚用`chapter_id=`，保持模式与start／end；`categoryQueryRequest`仍拒绝缺失章号，不发无效查询。实体深链继续使用原helper，不把分类临时草稿协议扩散到字段页面。
- 分类草稿保留打开时的双revision。409、元数据改变、结果未知均显示原分类／当前草稿／最新分类，明确采用后才更新依据，不自动换版本重提。保存成功仅同步自写广播保留刚保存的同key快照，其他缓存草稿和之后外部广播仍失效。字段目录与分类revision跨HTTP配对失败时重读，不拼成假快照。
- 四详情的`SettingTagsSession`按页面内(kind,id)维护controller，关闭及A→B→A保留本条草稿／搜索／原版本，不建全局store；标签区位于外层表单之外，Enter阻止外层提交，单独PATCH不覆盖姓名、基础字段或职业阶段草稿。
- 失读／actor或项目改变清理会话；失写实时禁用。成功删除条目后`forget(entry)`取消并移除controller；有效refresh／compare 404清掉base、草稿与搜索，不能保留可提交的旧身份。身份／请求代次守卫隔离迟到响应，不能只依赖界面关闭。
- 标签冲突／未知保存结果先读取最新对照，点击“保留草稿，采用对照版本”才继续；若结果已相同不重复提交。列表／结果名称或类型筛选改变回第一页，错误与空结果分开，可重试同一revision；历史结果使用捕获position生成四类深链。

### v5备份、卡片与效果

- project v5／APP_SCHEMA0036保存tags及两张核心表，规则三个真FK按定义→选项→类别→规则拓扑重映射，`deferred`为空；tags／适用类型登记json_plain。v1–v4仅缺失或规范空数组补空，非空未来载荷／null／非法形状拒绝；v5必须含两表，原typed旧版闸门继续保留。preview纯图校验，导入后数据库fieldGraph＋categoryGraph同事务校验。
- cards v5只保存包内角色／组织／职业当前tags，无分类规则、成员快照或world容器。预览带categories_revision，确认必须带expected_categories_revision，并继续字段CAS及v4显式引用映射。只给new／rename写标签，一笔只领一次分类版本；skip／职业reuse保留既有标签，全skip不领分类版本。前端主动更新预览发现任一目录版本变化，也必须展示候选并明确采用，不悄悄替换原预览。
- effects diff／chapter_text格式4，内部完整保留tags与分类图，旧v1–v3继续原安全闸门；整轮首restore前按逆序before构造拟恢复图并纯校验。旧v3遇后来rule依赖阻止恢复，返回legacy人工修正错误；v4损坏图为undo_conflict。旧UPDATE双方无tags时仅在比较副本忽略，恢复仍省略以保留后来tags；旧CREATE遇后来tags拒删，合法旧DELETE才补空。
- fields_revision／categories_revision同时排除备份及effects独立项目列集合。新导入项目从默认起点开始；undo受影响目录从当前高水位递增，失败不增。Core恢复、物化、提交及取消失败都要保持数据库、同session原对象identity和两版一致，局部刷新见[智能体恢复契约](./agent.md)。
- 旧开放读取DTO不增加tags；ORM工具结果序列化单独过滤setting_tags，数据库／effects快照保留完整列，使用真实FakeLLM消息哨兵验证。

## 4. 一致性与有界读取

- `category_read_snapshot`使用Sessionmaker独立session，任何SQL前PG配置REPEATABLE READ＋READ ONLY，SQLite legacy驱动显式真实BEGIN；不改变认证DbDep或全局engine。快照内复查账号启用／token版本、项目人工读取权限与active生命周期，no_autoflush，finally rollback释放。
- 版本、规则、标签、全部候选批次与字段值来自同一快照；不能在逐查询默认快照中拼合旧数据和新代次。
- `catalog.candidate_batch`以character→world→organization→career和(rank,sort,id)游标每批目标200；世界观复用当前固定category细分。公开setting-entries旧排序与page_size<=100保持。
- 先完整匹配再累计total／截取请求页，只保留当前页；不能全载项目后切片、先分页再匹配或前端逐条effective。tag-only完全跳过字段历史。
- `resolve_entry_fields(...,definition_ids=None)`默认完整旧行为。显式范围仅shared、空集合不加载字段；分类只请求规则所需dropdown，不读legacy、无关events或handles。通用reference范围仍正确解码base/current及目标摘要。
- 部分投影走私有field-values map，完整resolve_timelines API不变，不能把部分结果送到AI／物化／事实历史路径。
- 规则依赖同时扩展`assert_definition_dependencies(replacement=...)`的UPDATE／DELETE和`assert_option_unreferenced`。只有规则、零绑定／事件也阻止dropdown改型或缩范围失去交集；改名、排序、停用允许。延迟FK不是服务409的替代。
- `validate_project_category_graph`校验已flush图且无授权／写入／版本／提交：entry tags必须已canonical；四表图委托纯snapshot内核。import／effects另保留fieldGraph。pure入口接收完整或最小标量行，验证父链、同作品、strict唯一ID、规则数量／互斥载荷／canonical tag／字段类型和范围；供effects在首restore前检查拟恢复全图。

## 5. 校验与错误矩阵

| 条件 | 结果 |
| --- | --- |
| 缺分类／定义（含归属不符） | 404 |
| 非dropdown、错组选项或无适用交集 | 400 invalid_category_rule／incompatible_category_rule，附rule_index |
| 新选择停用身份 | 409 category_rule_archived |
| 保存分类形状损坏／非canonical标签 | 409 invalid_category_graph |
| categories／fields写入版本过期 | 409 stale_revision，附对应scope，整笔回滚 |
| 账号禁用／token变化 | 401，独立快照内重查 |
| chapter_id与chapter_no同时存在 | schema拒绝 |
| v5卡片缺expected_categories_revision | 400 categories_revision_required |
| 旧格式夹带非空未来标签／分类资料 | 明确拒绝，不清空后继续 |
| SQLite空新态降级／有新资料降级 | 精确恢复0035结构／任何DDL前拒绝 |

## 6. 实例与必需测试

- Good：阵营A在20章end改B，5／20start按A归类、21按B；当前标签在上述视图相同。字段／选项改显示名不改变规则身份。
- Base：ANY两条只命中一条时入类，ALL时不入；一条目可命中多类。空槽和清空不匹配下拉，不创建绑定。
- Bad：按标签显示名猜option，仅读本页后计算total，以请求自报的retained身份绕过停用检查，修改认证session隔离级别。

`test_setting_categories_migration.py`覆盖新旧SQLite／真实PG结构、旧值保留、强制单删约束、项目级联和降级。`test_setting_category_matcher.py`与`test_setting_category_query.py`覆盖规范化、规则／后筛分页／无N+1／时点／只读，以及SQLite／PG各四个双连接跨批屏障；SQLite WAL仅测试临时库使用。字段默认及真实FakeLLM回归见`test_setting_fields_context.py`等，不以纯matcher通过代替完整字段兼容。

`test_setting_categories.py`覆盖CRUD／tags、两版本CAS、仅rule依赖、只读preview及普通GET三路SQLite／PG双连接快照、CAS后失败全回滚。`test_setting_category_validation.py`覆盖纯拟恢复图且不改输入；原query错误码及服务器retained边界必须保持。新AuditField为setting_categories／setting_tags，前端中文分别为“设定分类”／“整理标签”。

`test_setting_category_entry_identity.py`覆盖create_all／真实Alembic两建库路径×四实体×删最高／删空表共16个真实HTTP场景：新ID严格递增，旧GET／PATCH404且新tags和revision不变。迁移测试另覆盖FK原ON／OFF恢复、全图资料、其他序列高水位、无临时表与精确空态回环。PG测试建库统一`tests.schema_helpers.create_isolated_schema`，防止共享MetaData的PG AddConstraint状态污染后续SQLite FK；三组预置及真实级联必须保留。

父级`test_setting_fields_categories_roundtrip.py`以真实HTTP创建四实体×四类型、16个实际20章end事件、当前tags及ALL／ANY，v5导入新作品后重查5start／20start／20end／21start的固定语义真值表。比较label／名称／类型／tags而不是原ID，逐表验证实体／章节／定义／选项／handle／事件／规则ID隔离，并验证双向修改与跨作品引用404不增版本。永久负控制分别丢事件、把20end改20start，必须在指定时点被同一语义断言捕获，不能只以canonical JSON相等替代时点验收。

前端回归覆盖Python空白、位置模式与边界URL往返、自写／外部广播、双页CAS／显式对照、未知结果、删除／404及迟到响应。真实浏览器检查ANY／ALL、5／20start／20end／21和计划章、跨200候选分页、四标签独立保存／清空、A→B→A草稿、四历史深链、375明暗及v5卡片新建／skip／reuse；检查报告应区分自动化与实际界面证据，未执行项目不能勾选。

## 7. 错误与正确写法

```python
# 错：先目录分页再过滤，total只代表该页，或在每行调用effective。
items = [entry for entry in directory_page if matches(entry)]

# 对：独立一致快照中分批完整匹配，服务返回真实total和所需页。
page = await query_category_entries(
    sessionmaker, actor, project_id, category_id=category_id, filters=filters,
)
```
