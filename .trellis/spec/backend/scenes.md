# 独立场景：正文权威、规划、历史与写作闭环

## 1. 范围 / 触发条件

适用于所有章正文写入、建章、规划、故事位置、版本、作品备份和场景编辑器。实现入口为
`models/scenes.py`、`schemas/scene*.py`、`services/scene_service.py`、
`planning_scene_service.py`、`scene_ai_context.py`、`api/v1/scene*.py`，前端为
`api/scenes.ts`、`components/scenes/` 与 `ChapterEditorPage`。

**Scene 是正文权威，Chapter.content 是兼容投影，不是另一份可独立修改的原稿。**
阅读、导出、提及、开放读取和既有章级分析继续读取完整章投影；场景改稿不冒充整章分析。

## 2. 签名（数据库 / API / 内部原语）

- `0039_independent_scenes`（parent `0038`）：`scenes`、
  `chapters.content_revision`、`chapter_versions.scene_snapshot`，场景规划来源／本地角色与演变锚点。
- Scene：稳定自增 `id`、`chapter_id CASCADE`、`title`、`content`、`summary`、`sort >= 0`、
  `separator_before`、`active`、内部派生 `word_count`；活动行的 `(chapter_id, sort)` 部分唯一。
  退场不复用身份；SQLite 保留 AUTOINCREMENT 高水位。
- `Chapter.content_revision >= 0`：正文、摘要、结构、本地规划修改推进；任务抢占／释放状态不推进。
- 只 flush 的内部原语（外层先鉴权和取写屏障，并拥有审计／版本／提交）：

```python
await initialize_chapter(db, chapter)
await list_active_scenes(db, chapter_id)
await require_scene(db, chapter, scene_id, active=True)
project_scenes(scenes)  # -> (完整正文, SceneRange 列表)，纯计算
await check_revision(db, chapter, expected_revision)
await replace_scene_content(db, chapter, scene, content, expected_revision=revision)
await replace_chapter_contents(db, chapter, contents, expected_revision=revision)
await replace_legacy_content(db, chapter, content, expected_revision=revision)
await capture_snapshot(db, chapter)
await restore_snapshot(db, chapter, snapshot, expected_revision=revision)
```

以下路径位于 `/api/v1/projects/{pid}/chapters/{cid}`：

| 方法 / 后缀 | 请求与响应 |
|---|---|
| `GET /scenes` | `offset=0, limit=50`（1–200）→ ScenePage；目录不加载正文 |
| `GET /scenes/{sid}` | SceneOut，含正文、分隔与原始／有效规划 |
| `POST /scenes` | `expected_content_revision,title?,content?,summary?,after_scene_id?` →201 SceneOut；省略 after 追加，显式 null 首插 |
| `PATCH /scenes/{sid}` | 版本及非空 PATCH 字段 `title/content/summary` → SceneOut；空字符串可用，null 禁止 |
| `POST /scenes/reorder` | 版本及本章**全部活动** `scene_ids` → SceneMutation |
| `POST /scenes/{sid}/split` | 版本、Unicode 码点 `offset`、`right_title?` → SceneMutation |
| `POST /scenes/merge-preview` | 版本、按原顺序两个相邻 ID → conflicts/blockers/两版本/signature |
| `POST /scenes/merge` | 前述参数及 `expected_planning_revision,signature,metadata_source:left\|right` → SceneMutation |
| `DELETE /scenes/{sid}` | query `expected_content_revision` →200 SceneMutation，不是204 |
| `GET /scenes/resolve-position` | `fingerprint,start,end` → scene 局部码点或不可可靠定位原因 |
| `GET /scenes/{sid}/setting-context` | 只读本场景开始时的有效设定 |
| `POST /scenes/{sid}/generate\|polish\|revise` | 原操作参数＋必填两版本 →202 Task，使用既有章级任务种类 |
| `POST /scenes/{sid}/rewrite` | 原 RewriteRequest＋两版本，坐标为 Scene 局部码点 → SSE 提案 |
| `POST /scenes/{sid}/rewrite/apply` | 原文/before/替换稿/log_id＋两版本 → SceneOut |
| `GET /versions/{vid}/restore-preview` | query 内容版本 →目标结构／退场／阻塞项与版本；正文差异由UI另取版本计算 |
| `POST /versions/{vid}/restore` | `expected_content_revision` → ChapterOut |
| `GET /versions/{vid}/extract-preview` | query `scene_id,expected_content_revision` →旧稿提取差异与签名 |
| `POST /versions/{vid}/extract` | `scene_id,expected_content_revision,signature` → SceneOut |

项目级轻量目录为 `GET /api/v1/projects/{pid}/scenes`，同样使用 offset/limit。

## 3. 契约

### 投影、版本与无损结构操作

- 唯一投影为按真实顺序拼接 **每个活动场景的 `separator_before + content`**，包括第一项。
  不 trim、跳过空景、按字数猜边界或在客户端另造 join 规则。字数由现有 `count_words` 派生。
- 旧章升级完整原文成为一个初始场景，分隔为空，作者摘要为空；不自动拆分、不补造旧结构历史。
  新空章、细纲建章、计划复用、向导、拆书和合法旧备份显式初始化；GET 不补写缺失 Scene。
- 新建场景通常使用空行分隔；手拆右片分隔为空，精确重组原稿。左片保留身份与摘要，
  右片摘要空、复制原始本地覆盖意图，不固化有效继承值，不复制独立时点／支线事实。
- 合并保留左 Scene 身份，右 Scene 退场；正文与中间分隔原样拼接。
  标题／摘要／本地规划差异由作者明确选择；保留事件、节点或交汇阻塞时先处理引用。
  删除最后一景拒绝；含独立本地规划的删除同样受保护。
- 外层使用项目／章屏障和内容 CAS；排序先释放活动唯一 rank 再赋正式 rank。
  改顺序、退场、恢复后调用统一故事位置重验；任何历史区间不合法整笔回滚。
- SceneBrief 无正文，含章身份、标题、摘要、排序、字数、两版本和 planning。
  ScenePage 为 `{items,total,offset,limit,content_revision,planning_revision}`；项目目录顶层内容版本为 null，
  各行仍有本章版本。章目录页头／行／末尾版本共同核验，读取中变化明确409。
  SceneMutation 为 `{items: SceneOut[],content_revision,planning_revision}`。

### 默认继承、按项覆盖与故事位置

- `planning` 同时返回 raw 和 effective：七项 `pov/tags/character/world/organization/career/relationship`
  各自 `inherit|override`；raw 为本地 POV、tag_ids、typed references，effective 另带逐项 `sources:chapter|scene`。
  override 的 null／空集合是明确无；inherit 不夹带本地值。不把场景角色反写 ChapterCharacter。
- 复用规划 batch，Scene targets 必须带项目 `expected_revision` 和唯一按章的
  `expected_content_revisions:[{chapter_id,content_revision}]`；成功返回 `content_revisions`。
  多个同章目标只推进该章版本一次。set/clear/add/remove/replace 切换对应项为 override，
  `inherit` 恢复继承；add/remove 从当前有效集合计算，其他项不变。
- 矩阵 `granularity=scene` 和交织 `axis=scene` 使用完整真实场景轴；与 chapter/plan 轴分开。
  场景节点与章共同背景分开，不把每景都要求完成整章目标。
- 演变写入 `anchor_kind=scene` 时仅携 `scene_id,boundary:start|end`。
  服务端从活动 Scene 解析 `StoryPosition.chapter_id/chapter_no/scene_id/scene_order`。
  旧章初／章末仍包住同号章的场景边界；本景 end 事件不进入本景 start。
  inactive／物理失联景为孤儿，不凭新序号或同号章复活。详见 [演变](./setting-progressions.md)。

### 结构历史、旧写入口与备份

- 新版本有 `scene_snapshot:{schema_version:1,scenes:[…]}`，保存真实 ID、排序、正文、摘要、分隔和
  `{unit_id,binding_epoch,raw}` 本地规划。记录前断言投影逐字符等于 Chapter.content。
- 普通正文手写保持10分钟合并窗口；仅传入相同 title/summary 不算元数据变更。
  真正元数据变更、结构操作、清空及恢复强制留基线／边界，零字与纯空白也保留。
  旧单景章 PATCH 与 Scene PATCH 对清空统一用 `not content.strip()` 判断，而非 `not content`；
  无历史原稿也必须先 force baseline 再保存精确空白结果，不能 trim 入库正文或让 `record_manual` 的无历史空稿分支丢掉原稿。
- 结构恢复验证同章身份／epoch／占用，不回滚章默认、独立节点／交汇或时间线；恢复自身生成新版本。
  历史 null 是真实旧纯文本：单景正常恢复，多景只能查看／对比或明确签名提取到指定景。
- 旧整段章 PATCH 只有单景可安全桥接；Agent replace及旧局部请求在多景章也可处理，但须先证明区间唯一落在一景内。
  不接受跨景整段改写，不通过自动拆并“兼容”。新 effects 使用 v6；旧不安全多景恢复拒绝，
  无关旧操作不全盘封禁。详见 [Agent](./agent.md)。
- 项目备份 v7／APP_SCHEMA 0039 保留退场身份、完整 raw 意图、新 FK 与结构 JSON 引用并显式重映射；
  v1–v6 真正逐级转换为单初始景，非法夹带新结构拒绝。正文／结构矛盾、核心引用缺失、
  历史 Scene 或规划 unit 错所属章在 preview 拒绝。卡片格式仍 v6。
- 本地／历史实体与规划标签引用保护真删除，不能让刚导出的合法完整备份变成不可导入资料；
  无 Scene 依赖的旧章标签删除保持原语义。新资料存在时拒绝有损 downgrade。

### AI 快照与前端草稿

- TaskKind 仍为 chapter.generate/polish/revise，`TaskOut.scene_id` 可空；持久 request.target 是严格
  `{kind:chapter}` 或 `{kind:scene,scene_id}`，旧缺省为 chapter。单景也占章写资源。
  历史任务列表投影先检查 input/request 是否为对象；坏 JSON 只读降级、不修写原载荷，不能把损坏 target 当作合法整章任务重放。
- 单景按有效规划与 scene start 组装；结构化 POV／引用必须先过 AiScope 的 never／隐藏字段／系列采用门禁。
  原始 ScenePlanningOut 只供作者，不能直接拼进模型范围块旁路。POV 不自动指定人称。
  自动检测素材保留章／细纲共同梗概，再补当前 Scene 素材；这不是把被覆盖的章手动角色重新 union 进来。
  普通上下文预算须扣除真实锁定范围块和规划块预留；测试计算实际块长度，不把单个样例的剩余额度写死。
- 整章按当前 Scene 顺序暂存提案，后景衔接使用已生成前景，而非再混入旧 DB 前景；
  总目标字数分配到各景。全部主生成成功且快照一致后一次 CAS 保存，只变正文。
  空景润色／重写保持空景；主失败或提交前取消不半写，已完成前章的批量断点不回退。
  revise 未指定目标字数时，仅旧章级入口且只有一个活动景保持 `max(原字数, 300)`；
  显式 Scene 目标或多景章按各景原字数。明确指定整章总目标则分配，不对每景重复套300字下限；日志必须与实际各景提示词一致。
- 发送前与最终采纳／保存都核验目标章内容、规划、位置及时间线／字段／可见性快照。
  前章普通正文是构建时背景，不额外绑定其他章content_revision；合法不同章并行生成不能因前章先提交而互相误判失败。
  普通额外续写失败保留 warning；快照撤回是安全冲突，不能把旧首轮提案作为成功保存。
- 新局部 apply 需可核验的有效 log_id，绑定目标、局部原文／before 和快照；前端先 UTF-16→码点，
  done 返回 scene_id/chapter_id/两版本。过期选择或跨景旧请求在出站前拒绝。
- 显式任务 retry 重新鉴权并读取同一仍活动 Scene 的新快照；不复用旧提案、不按 sort 猜新身份。
  整章成功按原流程分析一次，单景改稿只使旧分析过期，不生成伪全章事实。
- `SceneDraftController` 以稳定身份隔离正文／摘要草稿；任务流始终是独立只读提案。
  另一景 done、卸载晚响应和规划刷新不能覆盖本地输入；actor／项目／章／授权代次变化废弃旧响应及确认框。
- 409 保留草稿和拟排序，展示最新内容／规划并由作者明确核对后更新版本再重试；不自动重放。
  排序先收齐同一版本的轻量目录，不能只提交当前分页 IDs。375px 提供目录抽屉及上／下移按钮。

## 4. 校验 / 错误矩阵

| 条件 | 结果 |
|---|---|
| 不可见作品、错章／跨作品／失活 Scene | 404（不泄漏另一作品资料） |
| 可见但无人工写权限／回收站 | 沿原403／生命周期屏障；非本人AI由严格owner helper返回404 |
| null PATCH、未知字段、重复 ID、非法拆分码点、非正数或超过DB int32的Scene ID／路径ID | 422；不写库 |
| 排序漏景／跨章、不相邻合并 | 400 |
| 内容版本过期 | 409 `stale_scene_revision`，含 current_revision |
| 规划版本过期 | 409 `stale_planning_revision` |
| 缺活动景或投影矛盾 | 409 `scene_invariant_error`，不在读取修复 |
| 旧整段多景覆盖／跨景局部范围 | 409 `scene_target_required` |
| 最后一景删除 | 409 `scene_last_required` |
| 保留引用／非法历史恢复 | 409 `scene_reference_conflict`，列阻塞摘要 |
| 合并／旧稿提取签名已变 | 409 `scene_preview_changed` |
| 读取或 AI 快照变化 | 409 `scene_context_changed`／`planning_context_changed`；流内为明确失败 |
| 有本地／历史引用的规划标签真删除 | 409 `planning_tag_in_use`，列可见来源 |
| 提及指纹已变／跨景／分隔 | resolve-position 返回 reason `stale/cross_scene/separator`，不猜归属 |

## 5. Good / Base / Bad Cases

- Good：`青禾。🌙\n\n白岚。` 在码点4拆分，精确重组；右片继承原raw覆盖意图但摘要为空。
- Base：旧空章升级成一个空 Scene，旧纯文本版本 snapshot=null；只读浏览不新增规划或 Scene。
- Bad：逐景生成完就 commit、用 Chapter.content 反推边界、复制 effective 值冒充 inherit、
  将本景 end 的晋升带入本景 start，或为通过备份验证删除历史引用。

## 6. 必需测试

- `test_scenes.py`、`test_scenes_migration.py`、`test_scene_versions.py`：Unicode／空白投影、
  CAS、全部建章入口、结构边界、旧稿提取、实际0038升级／fresh及拒绝有损降级。
  旧单景清空须覆盖空串／纯空白 × 有／无历史，并可从精确结构快照恢复原稿。
- `test_scene_planning.py`、`test_scene_progressions.py`、`test_scene_context.py`：七类继承与覆盖、
  全类型引用、只读不flush、同号章／场景边界、孤儿、非法重排整体回滚、never／字段隐藏。
- `test_scene_transfer.py` 及独立检查回归：v7往返、真实旧格式、错章历史 unit、投影矛盾、
  历史标签保护、页头／行版本屏障、贴合 UI 全字段保存的10分钟合并。
- `test_scene_generation.py`：实际 FakeLLM messages、五局部 emoji、有效 log／版本、
  不混旧前景、整章取消／失败／规划与时间线竞态、只改目标、重试身份及旧入口。
- `test_scenes_postgresql.py` 与 `test_scene_ai_postgresql.py`：真实 PG UUID schema，
  迁移、CAS／rank／级联及 AI 最终提交竞争；SQLite 不替代 PG。
- 旧章、备份注册表／拓扑、Agent/effects、batch、reader/export/EPUB、mentions、权限全组必须回归。
  前端 scenes API／controller／offset／planning 与完整 lint/build/test；浏览器独立验证
  规划→编辑→AI→保存／恢复、双页冲突、只读、375px、键盘和明暗主题。

macOS 迁移测试须使用可用的 UTF-8 locale（本机 `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8`）；
不能因 `C.UTF-8` 实际原生 ASCII 就修改中文 Alembic 配置或删失败测试。

## 7. Wrong vs Correct

```python
# Wrong：破坏唯一权威，记录历史时将触发 scene_invariant_error。
chapter.content = model_output
await version_service.record(db, chapter, "generate")

# Correct：外层已持权限／作品／章屏障并核验快照；全部景成功才统一写。
await scene_service.replace_chapter_contents(
    db, chapter, {scene.id: proposals[scene.id] for scene in scenes},
    expected_revision=expected_content_revision,
)
await version_service.record(db, chapter, "generate")
# 外层服务统一 commit；失败 rollback，不逐景提交。
```
