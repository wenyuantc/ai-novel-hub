# E2 规划与故事位置冻结合同

2026-11-07。本代理独占 planning/progressions 模型、schema、服务及 API、位置消费者与系列依赖保护。不修改 Scene 存储、章投影、迁移、备份核心、前端、README/TODO/spec；不回退其他代理编辑。

行为缺口在规划来源与时间线求值层：目前只接受章／细纲并从章 cast 取角色。本轮扩展 scene 来源，统一逐项有效规划与真实场景位置，保持章级旧语义；不在 GET 物化，不复制章默认，不做跨章移动或自动迁移事实。

## 0039 与注册表（foundation 单点集成）

- planning_units 新增 scene_id 可空、唯一、FK scenes.id ON DELETE SET NULL；source_level String(8) NOT NULL DEFAULT 'chapter'。
- 七列 String(8) NOT NULL DEFAULT 'inherit'：pov_mode、tags_mode、character_mode、world_mode、organization_mode、career_mode、relationship_mode。每列 CHECK IN ('inherit','override')。
- CHECK source_level IN ('chapter','scene')；chapter 层 scene_id 必须 NULL；scene 层 chapter_id/plan_id 必须 NULL；retired 检查扩展为三来源均 NULL。scene_id 可以 NULL（物理删除后仍保留 scene 层孤立规划）。
- 新 planning_unit_characters：unit_id FK planning_units CASCADE、character_id FK characters CASCADE，复合 PK、character_id 索引、TimestampMixin，与既有关联表同形。加入模型导出、PLANNING_TABLES/备份顺序及真实 FK 映射。
- setting_progressions.scene_id：可空 FK scenes.id ON DELETE SET NULL、索引。anchor_shape 改为 chapter: scene_id/chapter_no NULL；planned: chapter_id/scene_id NULL 且正章号；scene: chapter_id/chapter_no NULL（scene_id 可空表示物理删章后的孤儿）。anchor_kind String(8) 足够。
- v7 原始场景规划与结构快照内的 unit_id/scene_id/角色/tag/全类型引用必须显式 ID 重映射。旧行 source_level='chapter'，模式 inherit，不改变其实际旧规划意义。

## 原始／有效规划 JSON

SceneOut.planning 使用 ScenePlanningOut：

```json
{
  "unit_id": null,
  "binding_epoch": null,
  "raw": {
    "modes": {"pov":"inherit","tags":"inherit","character":"inherit","world":"inherit","organization":"inherit","career":"inherit","relationship":"inherit"},
    "pov_character_id": null,
    "tag_ids": [],
    "references": []
  },
  "effective": {
    "pov_character_id": null,
    "pov_character_name": null,
    "tags": [],
    "references": [],
    "sources": {"pov":"chapter","tags":"chapter","character":"chapter","world":"chapter","organization":"chapter","career":"chapter","relationship":"chapter"}
  },
  "revision": 0
}
```

references 与原 PlanningReference 相同：实体 `{kind,id}`，关系 `{kind:"relationship",timeline_id}`。raw 只保存本地值；override 的 null/空集合是明确清空。inherit 下 raw 不携旧遗留本地值。effective.tags 为现有 PlanningTagOut；sources 精确为 chapter 或 scene。

## 写入／矩阵／交织

- 复用 PATCH `/planning/units/batch`：target.kind 新增 scene；POV op 和各集合 op 新增 inherit，不可带 ID。既有 set/clear/add/remove/replace 在 scene 上将该项切为 override，add/remove 从当前有效集合开始计算。仅修改请求提供的项。
- scene target 写入必须携 `expected_content_revisions:[{chapter_id,content_revision}]`（同章唯一），与 expected_revision 同事务检查；响应新增 `content_revisions` 同形。同章多目标只推进内容版本一次。
- GET `/planning/matrix?granularity=chapter|scene`，默认 chapter 保留旧结果；scene 返回实际活动场景，PlanningRow 新增 scene_id/scene_sort/scene_planning/content_revision，章分组仍有 chapter_id/chapter_no/title。source_refs 支持 scene。
- node/crossing source_kind 支持 scene；GET weave axis 新增 scene，完整实际场景顺序独立算距离，不混章／计划轴。
- GET `/planning/units/context-preview?source_kind=scene&source_id=SID` 使用统一解析器；既有 scope=chapter|selection 保留。

## 只 flush / 只读集成原语

统一放 `services/planning_scene_service.py`：

```python
await resolve_scene_planning(db, project_id: int, scene_id: int) -> ScenePlanningOut
await capture_scene_planning(db, project_id: int, scene_ids: list[int]) -> dict[int, dict | None]
await restore_scene_planning(db, project_id: int, scene_id: int, snapshot: dict | None) -> None
await scene_reference_blockers(db, project_id: int, scene_ids: list[int]) -> list[dict]
await retire_scene_planning(db, project_id: int, scene_ids: list[int]) -> None
```

snapshot 为 `{unit_id,binding_epoch,raw}` 或 null；不能恢复独立节点/交汇/时间线。restore 校验同章真实 Scene、unit 绑定代次与占用、类型引用，仅恢复本地意图；不提交、不推进章内容版本（外层统一负责）。retire 保留规划及 scene_id 身份，inactive 来源从 AI 排除，仍可在 detached 目录展示；不得 retired=True 偷删作者数据。

blockers 检查全部保留事件（含 inactive）、独立 node/crossing；返回 `{kind:"progression"|"node"|"crossing",id,scene_id,label}`。拆并/删除/恢复先调用；不把中间事件移至外边界。本地 metadata 合并由 foundation 显式选择并经 capture/restore，独立事实不随之回滚。

`planning_context_service.load_planning_context(db,project,"scene",scene_id,...)` 支持 scene，实际角色手动集合由 resolve_scene_planning.effective.references 取 character，不能再 union 章 cast。AI 组装由 main 实现，位置与明确 cast 参数必须传给现有 builder/build_scope。

## 场景位置与演变

- ProgressionCreate/Update/Out 新增 scene_id；anchor_kind 新增 scene。写 scene 锚只接 scene_id、boundary，不可混 chapter_id/chapter_no。
- StoryPosition 为解析后位置，追加 scene_id 与 scene_order；scene_order 仅服务端从当前 Scene.sort 取得，不接受客户端自报事实。
- `await setting_progression_service.resolve_scene_position(db,project_id,scene_id,boundary="start") -> StoryPosition`：同项目、活动 Scene，返回真实 chapter_id/chapter_no/scene_id/scene_order/boundary。
- 旧 effective／字段／分类按位置查询增加 scene_id query，与 chapter_id/chapter_no 互斥。GET `/scenes/{sid}/setting-context` 可调用本层只读预览 helper（待接 foundation 路由）。
- 排序键是章号→旧 start→按 chapter_id、scene_order 的 scene start/end→旧 end；旧 chapter/planned 事件相互次序不变。失活/失联 Scene 事件是孤儿，不按 sort/章号复活。
- `on_story_positions_changed` 保留无 commit 完整历史重验/物化；所有结构写的外层事务失败整体回滚。

## 实施交接补充

- SceneBrief 同样携 required planning。列表可批量调用 `resolve_scene_plannings(db,project_id,scene_ids,*,chapter_rows=None)`；读取 Scene 轻量列，不读取其正文。大集合按400分块，GET不创建单元。
- `PlanningRow` 已新增 `source_level` 与 `source_chapter_id`。退场来源在 detached 目录，保留 scene_id；source_chapter_id 从真实 Scene FK 取得，物理失联为null并禁止跨章认领。
- `clone_scene_planning(db,project_id,source_scene_id,target_scene_id)` 已实现：同章复制 raw 意图，可覆盖目标原始本地值，保持目标unit/epoch且保留源unit。不复制独立剧情事实。
- `planning_context_service.scene_setting_context(db,user_id,project_id,chapter_id,scene_id)` 返回 SettingContextOut；基础路由已接入。
- PlanningContext/PlanningContextPreview 另带 `scene_id,content_revision`；内部 PlanningContext 提供 `position,manual_character_ids`。`load_planning_context` 的scene分支先解析唯一有效规划，再进入统一AiScope/字段隐藏门禁；章节点仅作为共同背景，不读其他场景节点。
- `context_builder.prepare_chapter_settings` 的新可选关键字为 `position=None,manual_character_ids=None,source_text=None`。空手动集合明确替代章cast；source_text替代当前自动检测素材。`resolve_effective_cast/effective_world_text` 接 position；两个 build_* 同样可接上述三参数并优先复用 planning.setting_scope。旧调用默认语义不变。
- E3整章暂存前景需要再加 `preceding_text: str|None` 贯穿load/prepare；现extra_text只追加，不替代DB prior_text。主会话已接管context_builder.py，后续AI应只用暂存前景，不临时写DB。
- 演变、字段、分类和旧关系/成员/职业入口已支持显式scene锚；旧未指定类型的章号适配保持。职业区间校验现在逐精确场景边界检查，非法重排整体回滚。
- setting_field_service.assert_targets_unreferenced 前置检查场景本地及结构历史依赖；系列取消采用也保留本地/历史引用。场景原始元数据只供作者界面，不得直接拼入AI范围块绕过never/隐藏字段。
- 场景上下文读取、时间线解析与矩阵/交织检查故事位置/内容版本，拒绝混合快照；真实发送及最终采纳仍由E3校验内容、规划、时间线/字段和可见性快照，不能只依赖构建时校验。

### 本轮实际验证（不是全任务验收）

- `tests/test_setting_progression_resolver.py tests/test_planning.py`：66 passed。
- 新场景规划/演变及旧membership/career/relationship/planning_context组：86 passed。
- 最终三个新增文件 `test_scene_planning.py test_scene_progressions.py test_scene_context.py`：15 passed；随后补充scene交汇目录/返回/独立轴断言，单例重跑1 passed。
- 初次旧progression/context/fields/categories组为135 passed、4 skipped、5 failed；5失败均为test_planning_context直接ORM建章缺少Scene，按正式写入口显式initialize后保留原断言重跑通过。未采用GET补写。
- 本轮拥有文件ruff通过；已跟踪diff和五个新增文件的空白检查通过。未运行本轮完整pytest、真实PG或浏览器；这些仍由主会话/E5集成验证，不能把本轮结果写成AC全部完成。
