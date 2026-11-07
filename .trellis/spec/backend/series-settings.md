# 系列作品与跨书共享设定

## 1. 范围

适用于角色、六类世界观的系列归属，以及任何读取或写入其本书投影的入口。系列唯一可写基础不等于多书共用一条实体行：同一共享条目在不同书有不同的本书 ID，各书拥有独立的时间线、角色状态、字段和剧情引用。

## 2. 签名

- `0038` 新增 `series`、`series_books`、`series_entries`、`series_entry_uses`，以及原父表复合键索引，不重建父表。一书一系，复合 FK 确保同作者、同系列、同书及同类型的绑定。成员随整书彻删，软删除不移除；延迟 NO ACTION 阻止单删仍被绑定的实体。
- 内部 `/api/v1/series`：系列 CRUD、`/{sid}/books` 加入、`/books/{pid}` 退出；`/{sid}/entries` 及 `/entries/{eid}` 读写共享库；`/entries/promote`、`/entries/{eid}/uses`、`/entries/{eid}/uses/{pid}`、`/entries/{eid}/move-to-book` 管理归属。
- `GET /series/{sid}/impact?action=promote|leave|unuse|move|delete`，按操作提供 `project_id`、`kind`、`entry_id`；返回 `revision, affected_books, blockers, source_stamp, requires_ai_settings`。条目写返回 `{revision, item, ref}`，后两项随动作可空。除创建系列外，修改提交 `expected_revision`（DELETE 用 query）。

## 3. 契约

- `SeriesEntryOut.id` 是系列 ID；`uses[].ref` 才是本书 `{kind,id}`。作品读接口附加 `series: {id,name,revision}|null`、角色和世界观的 `origin`。管理员可见所授权书已采用投影，但无系列 ID 或系列库权限；开放 API 只返回本书资料，不增加系列元数据。浏览未采用条目不会将其纳入本书目录、提及、AI 或备份。
- 规划分页 `/projects/{pid}/planning/entries` 的 `PlanningPickerEntry.series_shared` 在同一规划版本下对当前页批量投影，用于区分同名选项；规划提交仍只接受本书 ID，不返回或解析系列 ID。
- `series_policy.py` 集中划分共享静态列、共享基础键和条目 AI 策略。投影的实体正文、简介等当前值由**该书**事件物化，系列编辑通过 `_apply_series_baseline` 合并共享键并重新物化，不覆盖本书事件。提升读取已确认 `baseline`，来源戳复核且沿用原本书 ID；新采用新建本书 ID，不靠同名复用。
- 系列写序：校验 owner → `series_service._edit` 持系列写屏障 → 按书 ID 排序取 `lock_visibility_write` → 同序锁父项目 → 重验依赖后写入。同事务更新档案、投影和受影响作品可见版本；匹配竞争或披露收紧递增历史代次。单书引用写与恢复须持父作品锁，不反向取系列锁。回收站投影只由已授权的系列内部写路径刷新，正常 AI 出站仍要求作品活跃。
- 旧入口按绑定保护共享列/基线；向导受理及最终覆盖均检查绑定；Agent Core 效果恢复必须按当前归属预检，允许合法本书事件撤销并重算。卡片、拆书和大纲导入不能按同名自动拿共享投影挂边。
- 单书 v6 备份保留已采用投影和本书事件，排除成员和用途绑定、重映射本书引用；`app_schema` 为 `0038`。整库快照保留系列。系列数据已存在时不允许降级删除元数据。

## 4. 校验与错误

| 情况 | 响应 |
| --- | --- |
| 他人系列、跨书/跨系列伪造实体身份 | 404，不披露系列内容 |
| 系列版本旧／提升来源变动 | 409 `stale_revision`（`scope=series`、`current_revision`）／`series_source_changed` |
| 提升的基础未确认／旧 AI 档位未显式选择 | 409 `series_baseline_unverified`／`series_ai_confirmation_required` |
| 取消采用或归属变更会破坏书内资料／结构化引用 | 409 `series_setting_in_use`，包含 `blockers[]`；不部分写入 |
| 用本书 CRUD、策略或向导修改由系列管理的基础 | 409 `series_setting_managed` |

预检不能授权最终写入；实际提交必须重新检查活跃与可恢复资料（快照和 JSON ID 也算）。正文字面提及不是硬引用。无法在当前范围重验的旧 AI 载荷和撤销拒绝执行。

## 5. 正常／基线／反例

- 正常：A 提升角色、B 采用；A 的章末受伤只在 A 生效。修改共享简介后两书各自按新基础重算，B 的选角 ID 不变。
- 基线：独立作品不出现系列入口数据；恢复单书备份得到独立角色和可打开的章节引用。
- 反例：B 仍在可恢复快照中引用角色时把角色移回 A；必须整笔失败。不能只统计活跃书或 FK。

## 6. 必需测试

- `tests/test_series.py`、`test_series_migration.py`：提升/独立演变、归属引用/回收、跨作者与 CAS、升级和拒绝有损降级。
- `tests/test_series_projection_guards.py`：锁内共享字段保护、合法书内事件、向导、撤销、同名导入及快照。
- `tests/test_series_transfer.py`：已采用和未采用条目在 API Key 与 v6 快照的边界；真实 HTTP 发送拦截所有受影响作品的旧戳。
- `tests/test_series_postgresql.py`：显式本机专用库 `AINOVEL_SERIES_POSTGRES_TEST_URL`，验证父锁竞争及跨书级联，不用 SQLite 结果代替。
- 前端：系列请求类型、归属预检冲突和草稿保留；真实浏览器覆盖建系列、采用、跨书刷新、非成员与依赖阻断。

## 7. 错误与正确写法

错误：把本书物化后的 `world.content` 写到系列，再复制给其他书；或将 `entry.id` 直接填进 `character_ids`。

正确：提升从 `SettingTimeline.baseline` 读取基础；系列修改只同步共享基础键后按各书事件重算。通过 `POST /series/{sid}/entries/{eid}/uses` 取得本书 `ref.id` 才可加入出场。
