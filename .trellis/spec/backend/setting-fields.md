# 自定义设定字段（四类型、章节值与持久身份）

## 1. 范围 / 触发条件

新增字段类型、编辑共享定义／选项、条目起始值／剧情变化、引用目标删除、上下文组装、卡片／备份导入或智能体撤销时遵循本契约。字段类型为 `text | line | dropdown | reference`，四类值均按章回溯；名称、适用范围、选项名称及启用状态使用当前配置。

AI 消费不能使用人工字段 DTO 直接渲染。`ai_hidden` 在共享定义或未绑定旧字段上配置，按当前策略隐藏名称、原值、选项与历史；改绑不得静默放宽。见 [设定 AI 可见性](./setting-ai-visibility.md)。

原五类时间线事实见[设定随剧情演变](./setting-progressions.md)。公开条目身份仅 `EntityRef {kind: character | world | organization | career, id: 严格正整数}`；世界观类型另用 `world.place/faction/item/rule/history/other`。职业定义的时间线主体为 `career_entry`，组织定义为 `organization`，不能混用人物职业挂接 `career` 或成员 `membership`。

## 2. 签名（DB / API / 服务）

- 迁移 `0035_typed_setting_fields`：新增 `setting_field_definitions`、`setting_field_options`、`setting_reference_handles`，保留 `setting_text_fields` 与事件原 ID；`base_value` 从文本转为 JSON 字符串，原文不 trim。`projects.fields_revision` 独立于时间线 `revision` 和 `planning_revision`。
- legacy 绑定 `definition_id=NULL, label非空`；共享绑定 `definition_id非空, label=NULL`，按 `(timeline_id, definition_id)` 唯一，名称和类型 JOIN 定义。SQLite绑定表同样使用AUTOINCREMENT，删除最大空绑定后不复用其身份，避免旧effect把事件恢复到同timeline的新定义。空槽只读，不因 GET 建绑定或句柄。
- handle 有真实四选一目标 FK（NO ACTION）和项目 CASCADE；JSON→handle 的历史依赖由服务检查，不能只依赖 FK。整作品删除同时删除目标／句柄；单删目标受全部保留值与事件保护。

路由前缀 `/api/v1/projects/{pid}`：

| 路径 | 操作 |
|---|---|
| `/setting-field-definitions[/{did}]` | GET／POST 列表与创建，GET／PATCH／DELETE 单项 |
| `/setting-field-definitions/{did}/options[/{oid}]` | POST 创建，PATCH／DELETE 选项 |
| `/setting-entries` | GET，重复`types`参数、`q`、`page`、`page_size<=100` |
| `/setting-entries/{kind}/{id}/fields` | GET，当前或 `chapter_id/chapter_no + boundary` |
| `/setting-entries/{kind}/{id}/fields/{did}/baseline` | PATCH 起始值，首次原子建绑定 |
| `/setting-entries/{kind}/{id}/fields/{did}/progressions` | POST 首次或后续章节变化 |
| `/setting-entries/{kind}/{id}/legacy-fields/{fid}/binding` | POST 显式关联共享文本定义 |

```python
# setting_field_service.py；批量读不 commit、不 autoflush
await resolve_entry_fields(db, project_id, entries, position=None, definition_ids=None)
# -> dict[(kind, id), EntryFieldsOut]
await load_field_specs(db, project_id, fields)  # -> dict[field_id, FieldSpec]
await assert_targets_unreferenced(db, project_id, targets)
await assert_definition_dependencies(db, definition, replacement=None)
await assert_option_unreferenced(db, project_id, definition_id, option_id)
await validate_project_field_graph(db, project_id)
# 唯一codec：setting_field_values.py
validate_field_value(spec, value, *, operation="replace")
encode_public_value(spec, value, *, reference_handles=...)
decode_public_value(spec, value, *, reference_targets=...)
# 唯一纯格式器：setting_field_formatting.py
format_entry_fields(fields)  # EntryFieldsOut -> str
```

## 3. 请求／响应及值契约

| 类型 | 公共值 | 持久／纯求值 canonical 值 | 操作 |
|---|---|---|---|
| text | string | string | append／replace |
| line | string，<=500码点，无换行分隔符 | string | replace；空串清空 |
| dropdown | `{option_id}` 或 null | 同公共值 | replace；null清空 |
| reference | `{kind,id}` 或 null | `{reference_handle_id}` 或 null | replace；null清空 |

- 文本不是 nullable；PATCH 省略值保留原值，null 只清下拉／引用，不能把对象 stringify 成文本。内部保留 JSON null，不能误写 SQL NULL。
- 定义／选项写带 `expected_fields_revision`；值写带 `expected_fields_revision, expected_timeline_revision, field_type, value`。首事件另带明确 `anchor_kind/chapter_id或chapter_no/boundary/operation`。旧事件接口保留 `expected_revision`，共享字段事件额外强制目录版本。
- `EntryFieldsOut` 含 `entry, timeline_id可空, timeline_revision, fields_revision, position, fields, legacy_fields`。空时间线 revision=0；共享槽 `field_id` 可空，不能拿 definition_id 冒充。槽含 base_value／value、当前 options／reference_types、source、archived、reference／base_reference 目标摘要；摘要仅身份、名称、类型，不递归正文。
- `EffectiveSnapshot.field_values` 保存全部 canonical 字段；`custom_fields` 保持旧 text string map。新增参数 `field_specs` 后仍兼容旧 resolver 调用及旧 snapshot 构造。
- 普通批量读对字段目录和时间线做前后版本核对；标量 DTO 快照避免 ORM identity map 旧定义与新 revision 混搭；全部 `no_autoflush`。跨批查询必须由调用方维持完整一致性边界，上下文也校验整体字段目录代次。
- 可选definition_ids=None保留完整旧行为；显式范围只读shared，空集合不加载字段。分类仅请求所需dropdown，不加载legacy／无关events／handles；显式reference范围仍解码所需引用。部分结果走私有field-values map，不改变完整resolve_timelines／AI／物化，见[分类查询合同](./setting-categories.md)。
- `position=None` 求所有有效事件之后，包括计划章；指定位置继承章初末排序、人工优先与孤儿处理。引用目标／选项停用后保留历史回放，新的选择必须 active。
- 上下文仅扩展原有选角／设定范围；生成、局部与整章改写、续写、批量、分析、规划共享字段投影和格式器，不自动带入引用目标档案。
- 前端字段配置页 `/projects/:pid/setting-fields`；独立字段面板与外层条目表单分别保存。409保留原草稿与保存版本，查看最新后显式重新编辑；刷新不能自动替换版本再提交。actor／项目／权限／请求代次变更使旧响应失效。
- 前端跨HTTP组合effective／entry-fields或事件对照时，同时匹配timeline与fields_revision；失败后的显式重试使用独立请求代次，同revision也必须重发。下拉／引用提供具名且可键盘触发的清空按钮，映射控件有可访问名称与关联label。
- EntrySelect的注入式loader接收`{q,page,page_size:30}`；翻页保持展开，Go to可聚焦输入，分页Enter不能冒泡误选条目，Escape正常关闭。跨页保留已选名称与身份，分页焦点守卫不改变搜索／项目／actor失效合同。

## 4. 校验与错误矩阵

| 条件 | 行为 |
|---|---|
| 跨作品定义、字段、选项、目标或章节 | 404／业务校验失败，不能写入 |
| 缺共享目录版本（旧事件接口） | 400 `fields_revision_required` |
| fields／timeline版本变化 | 409 `stale_revision`，带 scope、current_revision，整笔回滚 |
| 字段类型、ID形状、单行换行、非法append、引用范围不符 | 422／业务400，绑定与事件均不残留 |
| 已用定义换类型／缩范围、有保留历史的定义／选项／目标删除 | 409；保护 inactive 与 orphan，不只当前有效值 |
| 字段同名、唯一绑定冲突 | 409 `field_conflict`；legacy同名不自动合并 |
| 首值或首事件失败 | ensure-binding与写入在同一外层事务，一起回滚 |
| 0035有类型化资料时downgrade | 明确拒绝有损降级；空新结构可退回0034 |

`assert_definition_dependencies(..., replacement=...)` 同时服务 UPDATE／DELETE；未来新增依赖必须同时保护类型／范围修改与删除，不能只拦 DELETE。目标覆盖／向导／导入／撤销统一复用完整图校验。

## 5. Good / Base / Bad

- Good：第20章end把阵营A改为B、驻地青云山改为青云宗；第5章与20章start仍为A／青云山，第21章为B／青云宗。改显示名后身份不变。
- Base：定义适用于角色／组织，GET新条目显示空槽；第一次直接保存计划章事件，同事务建立绑定，不要求先保存虚假基础值。
- Bad：把最后已写章当“当前”；GET批量创建空绑定；将definition_id当field_id；用新的active校验拒绝历史停用值；仅检查有效事件后删除仍被孤儿引用的目标。

## 6. 必需测试与恢复合同

- `test_setting_field_values.py`、`test_setting_progression_resolver.py`：四类、JSONnull、不可变、单行全部换行符、5／20／21章与planned、append／clear。
- `test_setting_fields_migration.py`：SQLite子行完整保留、ORM结构／FK／CHECK与0035一致；真实PG16的0034→0035→0034→0035、四目标NO ACTION、整项目CASCADE。专用环境变量 `AINOVEL_FIELDS_POSTGRES_TEST_URL`，仅隔离随机schema，finally清理。
- `test_setting_fields.py`／`test_setting_field_lifecycle.py`：真实HTTP首写、全部旧入口、归属／只读／CAS／no_autoflush、旧字段身份及时间戳、inactive／orphan保护、向导／回收站。
- `test_setting_fields_context.py`：断言实际FakeLLM messages、早晚章节、旧模板及预算、引用不递归、目录跨批竞争。
- 备份 project v4／APP_SCHEMA0035，完整保存三新表及全部历史，JSON option／handle按语义重映射，缺核心依赖整笔失败；`fields_revision`从PROJECT_RULE与effects各自项目列集合排除，新导入不继承源代次。
- 卡片 v4保留最新快照与局部ref，包外目标（包括world）显式映射；同名default skip不解析包内typed引用，需具体target或rename，未解决can_import=false。旧v1–v3继续导入。见`test_project_transfer.py`／`test_cards.py`与前端映射测试。
- 空绑定只有空串／null且无任何保留事件时才可随定义缩范围；完整备份保留它，卡片省略已不适用的合法空快照及未使用定义。两路径复用has_field_content判据，不能把inactive／orphan事件当空。导入后的统一图校验额外要求event.timeline_id等于绑定timeline_id。
- effects v3涵盖三新表、JSON全历史依赖和稳定预览ID；恢复后重验全图及每行项目scope，再物化；fields_revision从当前高水位递增，失败不增，不恢复before旧代次。旧v2纯正文／章节标题／legacy文本可安全撤销，触及typed图或typed历史的旧章锚结构恢复整轮预检拒绝，不能恢复一半后才失败。见`test_setting_field_agent.py`及[项目智能体](./agent.md)。
- 前端lint／build／完整tests与真实四实体、四控件、按章／只读、卡片映射、375px／主题／键盘／两页冲突。全部字段门禁通过后才交接分类。

## 7. 错误与正确写法

```python
# 错：对象转文本、读接口建空绑定或逐条查effective。
field.base_value = str({"kind": "world", "id": target_id})

# 对：公开EntityRef交codec编码，值读统一批量投影；数据库事务由调用方持有。
projections = await resolve_entry_fields(db, project_id, refs, position)
text = format_entry_fields(projections[("character", character_id)])
```
