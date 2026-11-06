# 设定随剧情演变（章节级时间线）

## 场景：基础值、章节变化及有效设定

### 1. 范围 / 触发条件

新增或修改角色、世界观、关系、职业挂接、组织成员、组织定义、职业定义、章节位置，以及生成、重写、分析写回、备份或智能体撤销时，必须保持同一套故事时间语义。实现位于 `models/progressions.py`、`setting_progression_resolver.py`、`setting_progression_service.py` 与 `api/v1/progressions.py`。当前只支持章节与章初/章末，不提供独立场景。四类自定义字段与0035持久身份见[自定义设定字段](./setting-fields.md)。

### 2. 签名

```python
# setting_progression_resolver.py；纯计算，不更改输入
resolve_progressions(subject_kind, baseline, custom_fields, events, position, *,
                     baseline_status="confirmed", exclude_event_ids=(), subject_identity=None,
                     field_specs=None)
# setting_progression_service.py；调用方负责外层事务
await resolve_timelines(db, project_id, timelines, position=None, *, exclude_event_ids=())
await apply_baseline(db, timeline, data: BaselineUpdate, *, materialize=True, ...)
await apply_progression(db, timeline, data, ...)
await materialize_project(db, project_id)
await on_story_positions_changed(db, project_id)
# context_builder.py
await build_setting_context(db, project, chapter, *, text_hint="", budget=4000)
```

- `GET/POST /api/v1/projects/{pid}/setting-timelines`；`GET/PATCH /{timeline_id}/baseline`；`GET/POST /{timeline_id}/progressions`；`GET/PATCH/DELETE /{timeline_id}/progressions/{event_id}`；`GET /{timeline_id}/effective`；`GET/POST /{timeline_id}/text-fields`、`PATCH/DELETE /{timeline_id}/text-fields/{field_id}`。以上后缀均接在 `setting-timelines` 下。
- `GET /api/v1/projects/{pid}/chapters/{cid}/setting-context` 返回 `{position: {chapter_id, chapter_no, boundary: "start"}, text, legacy_unverified_count, warnings}`，是人工只读预览，不是完整提示词。
- 数据表：`setting_timelines` 保存主体、`baseline`、`baseline_status` 与 `revision`；`setting_text_fields` 保存文本自定义字段；`setting_progressions` 保存变化、位置、来源和保护状态。迁移为 `0031_setting_progressions`。事件 `source_change_id` 外键为 `ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED`：同一事务删章会同时触发章节和分析来源级联，PostgreSQL 必须延迟引用校验才能让两列均置空。

### 3. 契约

- 主体 `subject_kind` 为 `character | world | relationship | career | membership | organization | career_entry`。关系身份是规范化角色对，career是角色 + 职业，membership是角色 + 组织；不依赖可消失的当前挂接行。organization与career_entry仅管理独立条目的自定义字段。角色支持基础资料与状态字段，世界观支持正文；四类条目支持text／line／dropdown／reference。关系、职业挂接和成员的存在性及结构化状态也纳入时间线。
- 基础值是故事起点，当前档案是应用全部有效变化后的投影；读早期章节时绝不能用当前投影替代基础值。已有作品迁移只以现有档案建立 `legacy_unverified` 基础值，旧历史只读，不推断缺失状态。作者显式核对并确认后才标为 `confirmed`；新建主体基线为 `confirmed`。
- 创建变化必须带 `expected_revision`、`operation: append | replace`、字段（内置 `field_key` 或 `custom_field_id` 恰选一个）及位置。`anchor_kind: chapter` 指向同项目 `chapter_id`；`planned` 指定数字 `chapter_no`，即使同号章节存在也不自动绑定。`boundary: start | end` 表示本章开始或结束后生效；旧实体接口未指定锚类型时保留原章号适配行为。
- 求值先复制基线，排除无效及孤儿事件，再按 `(章号, start 在 end 前, automatic 在 confirmed/manual 前, sort_order, id)` 应用。追加只用于非空文本，以换行连接；替换写入目标值。章节 ID 跟随重排，计划章号由 `CHAPTER_NO_REFS` 平移；删除绑定章节后事件保留为孤儿且不生效，不因重建同号章节自动恢复。
- 关系/职业/成员可先不存在、在章节建立、退出、以后再建立；不存在期间的字段写入不合法。改基线或事件时必须验证受影响的全部历史区间，包括主副职业数量上限；若无效应整笔回滚。手动确认/变化不得被自动分析悄悄覆盖；分析按目标章末记录且按故事顺序重算，重复分析、撤销或确认必须保持来源与事务边界。
- 降级0031会移除时间线与自定义字段，先保存当前完整备份；当前投影保留。存在隐藏关系时，旧版本无法表示其不可见状态，降级明确拒绝以保留旧审计。0035有新类型语义资料时同样拒绝有损降级，详见字段契约。
- 新增/修改/删除时间线事实与当前投影在项目写屏障和同一个外层事务中完成；供事务编排的底层 `apply_*` / 物化服务只 `flush` 不 `commit`；API 入口负责提交或回滚。写请求通过原子 revision 比较，`stale_revision` 时保留草稿。查询和上下文组装全程 `db.no_autoflush`，不得触发其他未提交 ORM 对象写库；读取已明确 flush 的事件必须可见。
- 正文生成、整章重写/建议改写、局部改写和章节分析均使用目标章初有效设定；局部改写的 `setting_context` 为锁定模板变量，旧模板缺变量也有兜底。变更分析执行时若正文或章节位置已变，不能把过期结果保存为当前事实。
- 前端分别展示基础、有效值与事件列表；提供校准、章初/章末、已有/计划章及追加/替换。`frontend/src/api/progressions.ts` 镜像后端类型；切换作品/主体/章节、权限或请求代次须废弃旧响应，冲突或关闭编辑时保留未保存草稿。人工只读用户可看设定预览但不能写入。
- 向导覆盖角色、世界观或职业前及落库前重新检查受影响主体的有效变化和自定义字段；有历史时 409 `wizard_history_conflict`，防止级联删除。拆书与向导的新设定、卡片新导入主体均需创建基础时间线。

### 4. 校验与错误矩阵

| 条件 | 行为 |
|---|---|
| 其他作品的主体、章节或不可见资源 | 按资源权限返回 404 或校验错误，绝不跨项目读取或写入 |
| 版本与当前时间线不符 | 409 `stale_revision`，不部分修改 |
| 章锚缺必填项、混用 ID 和数字、数值越界或字段形状错误 | 422 / 业务校验错误，不写库 |
| 主体尚不存在时修改字段，或操作导致历史区间违反职业上限 | 409，事件、版本与投影一起回滚 |
| 删除含有效变化引用的legacy自定义文本字段 | 409；共享定义／选项／引用目标保护全部保留历史，含inactive与orphan |
| 已绑定章节被删除 | 事件保留并标为孤儿，不参加求值；可显式重绑 |
| 旧智能体效果缺少时间线数据但会影响设定/章锚 | 409 `legacy_effect_requires_manual_correction`；整轮恢复前检查，不得部分撤销 |

### 5. Good / Base / Bad

- Good：第 20 章 `end` 将角色境界从「练气」替换成「筑基」；第 5 章和第 20 章章初仍为「练气」，第 21 章为「筑基」。
- Base：新增世界观设定时保存 confirmed 基线，计划第 12 章追加一段文字；第 12 章 `start` 按换行附加，之前章节不含该段。
- Bad：将最新 `Character.current_state` 直接拼入第 5 章提示词，或把旧关系历史逐条当作完整基线自动重放。

### 6. 必需的测试

- 纯求值：`tests/test_setting_progression_resolver.py` 覆盖章初/章末、同章排序、追加/替换、孤儿、未出现主体、不可变输入和整数边界。
- 数据/API：`tests/test_migrations.py`、`test_setting_progressions.py`、`test_relationship_progressions.py`、`test_career_progressions.py`、`test_membership_progressions.py` 覆盖迁移、版本竞争、回滚、项目隔离及可重建主体。
- 集成：`test_context_builder.py`、`test_rewrite.py`、`test_progression_writeback.py`、`test_chapter_numbering.py`、`test_project_transfer.py`、`test_progression_agent_effects.py` 覆盖写作上下文、乱序分析、锚点移动、备份与撤销；向导、卡片与拆书测试覆盖新建及覆盖入口。
- 前端：`frontend/src/components/progressions/*.test.ts` 及角色、职业、关系表单和章节预览测试；运行 lint、build、完整测试，并用隔离作品验证实际表单和响应布局。

### 7. 错误与正确写法

```python
# 错：绕过时间线，早期章会看到未来状态。
character.current_state = "筑基"

# 对：在项目写事务中以当前 revision 和明确位置调用时间线服务，
# 再由统一物化更新当前投影；读取章节使用 resolve_timelines 的目标位置。
await apply_progression(db, timeline, progression_data)
```

备份 v4、ID 重映射见 [项目备份](./project-transfer.md)，关系与职业旧审计见 [结构化设定](./story-entities.md)，章节平移见 [大纲](./outline.md)，智能体效果见 [项目智能体](./agent.md)。
