# E0/E1 场景基础冻结合同

2026-11-07。责任边界：foundation 独占 Scene 模型／schema／service／基础 API、Chapter 两列、0039、版本／建章初始化、备份 v7 核心。planning/progressions 及其 model 由 scene_planning 管理；AI／effects、前端、README／spec 不在本代理写入范围。原行为缺口是现有 Chapter.content 可独立写且没有稳定场景；本轮用显式只 flush 原语替代，不做隐式 ORM／GET 修复。

## 模型与投影

- `app.models.Scene`：`id, chapter_id, title, content, summary, sort, separator_before, active, created_at, updated_at`。id AUTOINCREMENT、不复用；`sort >= 0`，活动行 `(chapter_id, sort)` 部分唯一。title <=200；summary 原样保存；separator_before 是非正文分隔字符串，投影无条件按顺序拼接每个 `separator_before + content`（包括首项，不丢前缀）。
- Chapter.content_revision 从0起；正文／摘要／结构／场景本地规划每次逻辑写推进一次。Chapter.content／word_count 是同事务投影。初始化一个 scene 原样接旧章原文、separator_before=""，不增加 revision。
- ChapterVersion.scene_snapshot null=真实旧纯文本，非空形状 `{schema_version:1, scenes:[{id,title,content,summary,sort,separator_before,planning:...}]}`。结构快照只收活动列表，身份仍存在于软退场 Scene。planning 内容由 planning 原语提供，不保存时间线事件或章默认。

## 对外 JSON

- SceneBrief：`id, chapter_id, chapter_no, chapter_title, title, summary, sort, word_count, content_revision, planning_revision, created_at, updated_at`。无 content。章内页 `ScenePage={items:SceneBrief[], total, offset, limit, content_revision, planning_revision}`。项目目录同形，但 content_revision 为 null（各 item 自带所属章 revision）。
- SceneOut=SceneBrief + `content, separator_before, active, planning`；planning 为统一有效规划视图（由 scene_planning 实现／提供），缺记录不物化。
- 单创建／PATCH 返回 SceneOut；reorder／split／merge／delete 返回 `SceneMutation={items:SceneOut[], content_revision, planning_revision}`，items 是本次相关活动场景，delete 返回空数组。
- GET 章内 `/scenes?offset=0&limit=50`、GET 项目 `/projects/{pid}/scenes`、GET `/scenes/{sid}`。
- POST `/scenes`：`{expected_content_revision,title:"",content:"",summary:"",after_scene_id?:positive-int|null}`；未传 after_scene_id 追加、null 插到首项。默认独立新场景分隔空行；原章空初始 Scene 不自动吞掉。
- PATCH `/scenes/{sid}`：`{expected_content_revision,title?,content?,summary?}` 严格部分更新，不允许 null 值／chapter_id／word_count。
- POST `/scenes/reorder`：`{expected_content_revision, scene_ids:[完整活动ID]}`。
- POST `/scenes/{sid}/split`：`{expected_content_revision,offset:码点,right_title?:string}`。0和len拒绝；右片沿用原本地覆盖意图（不复制有效继承结果），原摘要留左、右摘要为空。保留引用无法等价先拒绝。
- POST `/scenes/merge-preview`：`{expected_content_revision,scene_ids:[left,right]}`。返回 `{scene_ids,content_revision,planning_revision,signature,blockers:[],conflicts:[]}`；签名为完整当前预检摘要哈希。
- POST `/scenes/merge`：上述两项 + `expected_planning_revision,signature,metadata_source:"left"|"right"`。只相邻／顺序正确，右方退场。元数据显式选择，独立节点／交汇／锚点不可被选择吞并。
- DELETE `/scenes/{sid}?expected_content_revision=N`；最后一个场景拒绝。
- GET `/scenes/resolve-position?fingerprint=sha256&start=N&end=N`：`{scene_id:null|int,start:null|int,end:null|int,content_revision,fingerprint,reason:null|"stale"|"cross_scene"|"separator"}`；目标偏移为 Scene 本地码点。光标边界歧义不猜。
- 基础 schema extra forbid、strict int(非 bool)、ID正整数、revision非负。固定路径先注册。

## 只 flush 共用原语（service 不 commit）

以下参数 chapter 均须由调用者先通过项目归属＋项目写屏障取得，不在底层重复猜 actor。

```python
await initialize_chapter(db, chapter) -> Scene # 新章已flush；已有Scene则返回首活动（不修复投影）
await list_active_scenes(db, chapter_id) -> list[Scene] # no_autoflush，有序
await require_scene(db, chapter, scene_id, *, active=True) -> Scene # 同章404
project_scenes(scenes) -> tuple[str,list[SceneRange]] # 纯函数，SceneRange属性 scene_id,start,end,separator_start,separator_end
content_fingerprint(content) -> str # SHA256 UTF8
await check_revision(db, chapter, expected_revision) -> None # 错409，不写
await touch_revision(db, chapter, expected_revision=None) -> int # 原子CAS、只推进一次，可供 planning 本地编辑
await refresh_projection(db, chapter, *, expected_revision=None) -> None # 有序重算＋CAS＋flush
await replace_scene_content(db, chapter, scene, content, *, expected_revision=None) -> None # 更新目标正文＋refresh_projection
await replace_chapter_contents(db, chapter, contents:dict[int,str], *, expected_revision=None) -> None # keys必须完整活动身份，单次CAS
await replace_legacy_content(db, chapter, content, *, expected_revision=None) -> None # 仅单活动Scene，多个409
await resolve_chapter_range(db, chapter, start, end) -> tuple[Scene,int,int] # 跨场景／分隔／歧义409
await capture_snapshot(db, chapter) -> dict # no_autoflush，只读
await restore_snapshot(db, chapter, snapshot, *, expected_revision=None) -> None # 前置引用保护，CAS，一事务
```

version_service 的 `record/ensure_baseline/record_manual` 会自动记录结构快照；结构操作用 `ensure_baseline(..., force=True)` 与 `record(...,"manual", note=...)` 强制边界，不按正文是否相同跳过。旧纯文本多场景恢复409 scene_target_required；新增提取端点由 foundation 提供并交接。

## 错误／锁／跨代理钩子

404 同章校验；422重复ID／schema；409 `stale_scene_revision` + current_revision；`scene_target_required`；`scene_reference_conflict` + blockers；`scene_merge_conflict`；`scene_preview_changed`。基础写前检查 chapter.status 和 batch 范围锁，使用既有共同授权 gate→project→chapter 锁序。

scene_planning 请提供／冻结：0039新增列／约束／表定义；`capture_scene_planning(db,project_id,scene_ids)`、`restore_scene_planning(...)`、`scene_reference_blockers(...)`、`retire_scene_planning(...)` 与 `resolve_scene_planning(...)` 的精确签名。正文结构操作后调用 `on_story_positions_changed`，规划／引用／历史保护失败整体回滚；不删除或迁移独立事实。

## 后续已落实的 wire 增补

- Scene 有内部派生 `word_count`，统一由服务端 count_words 维护；列表 SQL 不选正文。SceneBrief 必含 `planning:ScenePlanningOut`，使用 planning_scene_service.resolve_scene_plannings 对当前页批量解析，无 GET 物化。
- ChapterBrief/Out 必含 content_revision；不加 scene_count。ChapterUpdate 可带 expected_content_revision。
- ChapterVersionBrief 必含 has_scene_snapshot，Out 再含 scene_snapshot。GET `/versions/{vid}/restore-preview?expected_content_revision=N` 返回 `{version_id,has_scene_snapshot,content_revision,scene_ids,retiring_scene_ids,blockers}`。
- POST `/versions/{vid}/restore` 请求 `{expected_content_revision:N}` → ChapterOut；旧纯文本单场景兼容空 body，新结构需要当前版本。
- GET `/versions/{vid}/extract-preview?scene_id=SID&expected_content_revision=N` → `{version_id,scene_id,content_revision,original,replacement,signature}`；POST `/versions/{vid}/extract` 请求 `{scene_id,expected_content_revision,signature}` → SceneOut。仅旧纯文本，其他场景不变，不假称恢复整章。
- merge blocker 元素 `{kind,id,label,scene_id?:int}`，conflict 元素 `{field,left:string|null,right:string|null}`。split 已按最终设计纠正为复制原始本地覆盖（右摘要为空）。
