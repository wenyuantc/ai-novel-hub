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
- `Chapter.content_revision >= 0`：章标题、正文、摘要、结构和本地规划的实际变更推进；任务抢占／释放状态不推进。
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
await ensure_chapter_delete_isolation(db, project_id, chapter_ids)
await validate_history_snapshot(db, project_id, chapter_id, snapshot, for_restore=True)
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
- 历史详情、恢复预览和恢复前必须先调用 `validate_history_snapshot`。读取可接受同书的已验证移动归属，恢复仍要求当前章身份，详见[跨章移动](#8-跨章移动与-ai-拆并建议)。
  历史规划单元、POV、标签、角色、世界、组织、职业和关系时间线属于该作品，关系时间线须为
  `subject_kind=relationship`；富文档区块、退场区块及标记 UUID 必须能在同景持久身份中找到。
  查询按最多400个身份分批且禁止自动 flush。损坏、未知字段、缺失或跨作品身份返回匿名
  409 `scene_reference_invalid`，不回显快照、目标身份或异常。合法同书快照原样返回；
  恢复还须先核对当前单元与 `binding_epoch`，确认后才写基线、改变 Scene 或 flush。
  已删除而无法确认归属的历史身份也失败关闭，不能猜测其曾属于本书。

### 协作章节删除和级联归属

- 删除章节及细纲同步中直接删除已建章节，必须已持作品父锁，在任何 `detach_sources`、自动记忆删除、
  快照创建及 `db.delete` 前调用 `ensure_chapter_delete_isolation`。此函数按最多500章分批，
  反查 `Base.metadata` 中 chapters 开始所有直接及递归 CASCADE 关联，并与已审核的 FK 表对比；
  新增或变更的外键动作须先分析后更新登记。数据库中没有映射到 ORM metadata 的约束不在此图内，
  新迁移须同步模型和回归，不能把此检查当作数据库物理目录的完整扫描。
- 对归属可确认的同书关联保持原删除行为；跨书、缺失或无法确认归属的关联统一返回匿名
  409 `chapter_reference_invalid`。检查包括章／景规划和细纲、分析、日志、任务、角色、演变、
  自动记忆及其无 FK 的检索分块、富文档来源链和审核收据；收据无法通过任务键确认归属也须拒绝。
  调整章序号时自动记忆和检索分块同样需防止联动修改他书。
- 纯协作者删章先取 AI 可见性发送写门禁，再持作品锁并锁后重验成员和管理身份；
  若他人的 `queued/running/paused` 单章任务、待审核或批量任务涉及该章，或范围无法可靠判断，
  返回匿名 409 `chapter_in_use`。任务范围策略对作者及原有 `projects.read` + `projects.edit` 管理路径保持原有行为；
  权限来源在锁前后变化时返回 409 `collaboration_busy` 而非逆序补锁。
  发送门禁只保护真实 HTTP 请求发出至收到响应头的窗口；收到响应头后不锁住正文／SSE 流，
  不能据此声称已阻止删除正在接收响应、且没有任务记录的同步 AI 操作。

### AI 快照与前端草稿

- TaskKind 仍为 chapter.generate/polish/revise，`TaskOut.scene_id` 可空；持久 request.target 是严格
  `{kind:chapter}` 或 `{kind:scene,scene_id}`，旧缺省为 chapter。单景也占章写资源。
  历史任务列表投影先检查 input/request 是否为对象；坏 JSON 只读降级、不修写原载荷，不能把损坏 target 当作合法整章任务重放。
- 单景按有效规划与 scene start 组装；结构化 POV／引用必须先过 AiScope 的 never／隐藏字段／系列采用门禁。
  人工读取的 ScenePlanningOut 按内容读取权限返回，不能直接拼进模型范围块旁路。POV 不自动指定人称。
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

### 章节标题条件保存

- `PATCH /projects/{project_id}/chapters/{chapter_id}/editor-title` 接受 `title`、`expected_title`、`expected_scene_id` 和 `expected_content_revision`，返回 `ChapterOut`。原章节 PATCH 发送非 null 的 `title` 时也必须携带相同条件；状态、风格和其他更新沿用各自条件。
- 在原有人工权限和写锁内确认目标场景仍活动且属于该章，再核对版本和读取时的标题原值。先核对再判断同值，避免旧请求在服务器已是目标值时绕过校验。新标题按 `str.strip()` 保存，原标题原样比较。
- 真正改名在同事务推进章内容版本和规划版本。纯标题变更沿用章更新审计；正文版本历史由正文变更驱动。目标相同且条件匹配时直接返回。标题和正文同一次提交共享一次内容版本推进，智能体改名及纯标题撤销同样消耗版本，避免 A→B→A 后迟到请求重新生效。
- 章标题入口、场景 PATCH 与富文档 PUT 支持编辑器身份条件。当前前端同时发送 `X-Editor-Actor-Id` 和 `X-Editor-Actor-Created-At`，创建时间保留 `UserOut.created_at` 的原始微秒精度。服务器对本次已认证账号核对两项，并在写锁内的授权读取中再次校验创建时间，防止换号或 SQLite 数字 ID 复用。条件只约束草稿原作者，权限仍按原有认证与项目访问判断。

### 章编辑器自动保存和恢复

- `ChapterAutosave` 串行调度同一编辑器的标题和场景写入。已确认基线、冻结的请求快照及随后继续编辑的草稿分别维护。普通保存允许输入，自动范围为章标题和文档，手动模式另含场景标题与摘要。根据已确认写入推进版本，继后的编辑及其富文档证据保留。
- IndexedDB 草稿以账号和作品身份查询，章标题也绑定原活动场景 ID。每个编辑实例分配独立写入者和草稿记录。`edit_seq` 记录有效输入，`record_seq` 另跟踪基线、请求及恢复阶段。只在事务完成后提示本地已保留。清理匹配草稿、写入者和记录版本，未同步手动字段、未知请求或恢复暂停保留记录。
- 结果未知时比较服务器完整文档和预期版本。远端仍为请求基线时，旧请求仍可能提交，可以核对或按原条件显式重试原快照。确认远端已经提交该快照时只推进对应基线，版本有额外变化时等作者选择。不能把一次无变化读取当作旧请求已结束。
- 恢复前核对真实账号、作品权限和原目标。恢复创建独立副本，先保留被替换的当前草稿。恢复到编辑区后暂停同步，作者明确保存或进一步编辑自动保存字段才允许提交。失效目标仅查看和导出，不能换绑到同号或同序目标。
- AI、历史恢复和场景结构操作跨越保存与操作请求共享动作屏障。操作前等待保存并核对版本，所有权或目标变化使旧回执失效。安全比较须覆盖文档结构，现有映射规则无法证明可安全替换时保留导出入口。

## 4. 校验 / 错误矩阵

| 条件 | 结果 |
|---|---|
| 不可见作品、错章／跨作品／失活 Scene | 404（不泄漏另一作品资料） |
| 可见但无人工写权限／回收站 | 沿原403／生命周期屏障；非本人AI由严格owner helper返回404 |
| null PATCH、未知字段、重复 ID、非法拆分码点、非正数或超过DB int32的Scene ID／路径ID | 422；不写库 |
| 排序漏景／跨章、不相邻合并 | 400 |
| 内容版本过期 | 409 `stale_scene_revision`，含 current_revision |
| 章节标题保存条件缺少／类型非法 | 422，不写入 |
| 章标题保存的原场景已失效、原标题不匹配 | 分别返回 409 `editor_target_changed`、`stale_chapter_title` |
| 编辑草稿账号或创建时间与本次请求身份不符 | 409 `editor_actor_changed`，保存操作不执行 |
| 本地存储事务失败 | 明确提示保留失败；保留当前输入，已完成的旧本地记录仍可恢复 |
| 草稿格式未知或原目标失效 | 不安装到其他目标；合法账号和作品访问通过后允许保留下载 |
| 规划版本过期 | 409 `stale_planning_revision` |
| 缺活动景或投影矛盾 | 409 `scene_invariant_error`，不在读取修复 |
| 旧整段多景覆盖／跨景局部范围 | 409 `scene_target_required` |
| 最后一景删除 | 409 `scene_last_required` |
| 保留引用／非法历史恢复 | 409 `scene_reference_conflict`，列阻塞摘要 |
| 历史场景、规划引用或富文档身份跨书、缺失或不可确认 | 匿名 409 `scene_reference_invalid`；详情、预览和恢复均不暴露目标或原始快照 |
| 删章关联跨书、归属不明或 ORM 新增未审核 FK | 匿名 409 `chapter_reference_invalid`；无级联与部分清理 |
| 纯协作者删章时他人任务涉及该章或范围不明 | 匿名 409 `chapter_in_use`；不泄露任务详情 |
| 合并／旧稿提取签名已变 | 409 `scene_preview_changed` |
| 读取或 AI 快照变化 | 409 `scene_context_changed`／`planning_context_changed`；流内为明确失败 |
| 有本地／历史引用的规划标签真删除 | 409 `planning_tag_in_use`，列可见来源 |
| 提及指纹已变／跨景／分隔 | resolve-position 返回 reason `stale/cross_scene/separator`，不猜归属 |

## 5. Good / Base / Bad Cases

- Good：`青禾。🌙\n\n白岚。` 在码点4拆分，精确重组；右片继承原raw覆盖意图但摘要为空。
- Base：旧空章升级成一个空 Scene，旧纯文本版本 snapshot=null；只读浏览不新增规划或 Scene。
- Bad：逐景生成完就 commit、用 Chapter.content 反推边界、复制 effective 值冒充 inherit、
  将本景 end 的晋升带入本景 start，或为通过备份验证删除历史引用。
- Good：同书历史本地角色及富文档身份可原样展示和恢复；待审核批量覆盖某章时阻止协作者删章。
- Bad：先 flush 场景恢复再校验他书规划引用；把无法归属的任务／收据视为无关，或允许未知级联静默执行。
- Good：请求保存正文 S 在途时，作者继续写出 D。S 的回执只确认自己的内容，余下 D 下次用已确认版本另存。
- Bad：通过新读取覆盖草稿的版本条件，或用旧回执删除同章其他标签页的草稿。

## 6. 必需测试

- `test_scenes.py`、`test_scenes_migration.py`、`test_scene_versions.py`：Unicode／空白投影、
  CAS、全部建章入口、结构边界、旧稿提取、实际0038升级／fresh及拒绝有损降级。
  旧单景清空须覆盖空串／纯空白 × 有／无历史，并可从精确结构快照恢复原稿。
- `test_scene_planning.py`、`test_scene_progressions.py`、`test_scene_context.py`：七类继承与覆盖、
  全类型引用、只读不flush、同号章／场景边界、孤儿、非法重排整体回滚、never／字段隐藏。
- `test_scene_transfer.py` 及独立检查回归：v7往返、真实旧格式、错章历史 unit、投影矛盾、
  历史标签保护、页头／行版本屏障、贴合 UI 全字段保存的10分钟合并。
- `test_collaboration_chapter_fk_guard.py` 和 `test_collaboration_content_review*.py`：跨书 FK、
  检索分块与未知 FK、合法同书引用、章重排、他人活动／待审核任务、发送门禁及真实 PG 事务竞争。
- `test_collaboration_version_snapshot_isolation.py` 与对应 PG 用例：历史详情／预览／恢复的各类
  越权引用、富文档身份、缺失与未知字段；异常被内部调用方捕获后提交仍无部分写入；
  401 个合法引用跨分批边界、合法历史恢复和独立 PG schema 验证。
- `test_scene_generation.py`：实际 FakeLLM messages、五局部 emoji、有效 log／版本、
  不混旧前景、整章取消／失败／规划与时间线竞态、只改目标、重试身份及旧入口。
- `test_chapter_title_save.py` 与 `test_agent_tools.py`：标题改回原值后拒绝迟到写入、场景锚点及编号复用、同值无副作用、撤销推进版本，两个合法编辑者更换会话仍拒绝提交旧账号草稿。
- 前端 `chapterAutosave.test.ts`、`sceneRichDraftController.test.ts`、`editorDraftStorage.test.ts` 覆盖定时、一份在途快照、损失回执、富文档尾部事务和本地库事务边界。浏览器复验断网关闭后同一配置重开、双标签页冲突、权限变化及 AI 与恢复竞争。
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

# Wrong：先改 Scene/写版本，再检查 JSON 中的历史身份，调用方捕获错误后可能提交部分修改。
# Correct：外层已持项目与章写屏障；先核验全部历史身份和当前绑定，再创建基线或恢复。
await validate_history_snapshot(db, project_id, chapter.id, snapshot, for_restore=True)
await version_service.ensure_baseline(db, chapter, force=True)
await restore_snapshot(db, chapter, snapshot, expected_revision=revision)
```

## 8. 跨章移动与 AI 拆并建议

### 1. 范围

同一作品的单场景移动与同一章节的单次拆并。场景移动保留身份、正文、富文档及独立规划。AI 建议只处理作者预览过的保存稿。

### 2. 接口与数据

路径前缀为 `/api/v1/projects/{pid}/chapters/{cid}`。

| 方法与后缀 | 请求与响应 |
| --- | --- |
| `POST /scenes/{sid}/move-preview` | `target_chapter_id, insert_before_scene_id`。空插入点表示章末。返回 `source, target, source_position, target_position, planning_revision, signature, source_will_be_empty, planning_changes, timeline_events, source_seam, target_seam, moved_start, moved_end, blockers` 和原场景 ID。 |
| `POST /scenes/{sid}/move` | 相同位置与已核对的 `signature`。返回 `scene_id, source_chapter_id, target_chapter_id, source_content_revision, target_content_revision, planning_revision`。 |
| `POST /scene-restructure/preview` | `operation:split\|merge, scene_ids, expected_content_revision`。返回保存正文、候选编号与 Unicode 码点、两版本、签名、元数据冲突与阻塞项。 |
| `POST /scene-restructure/suggestions` | 预览条件加 `signature` → 202 `TaskOut`，任务类型为 `scene.restructure`。 |
| `GET /scene-restructure/suggestions/{task_key}` | 本人有效的建议，含两版本、签名、冲突和 `proposals:[{candidate_id,reason,offset,before,after}]`。合并的 `offset` 为空。 |
| `POST /scene-restructure/suggestions/{task_key}/apply` | `candidate_id, signature`，拆分可填写 `right_title`，合并明确选择 `metadata_source` → SceneMutation。 |

迁移 `0065` 增加 `scene_moves`。场景 FK 级联删除，原章、目标章和规划单元 FK 删除时置空。记录首条移动标识、原章和目标章的结构版本序号。备份 v16 重映射场景、章和规划单元 ID。

### 3. 写入与读取

- 移动先持作品写屏障，再按章 ID 顺序锁两章。锁内重新解析活动归属、插入点、签名和写入占用。移动原场景的 `separator_before + content`，源章失去唯一活动景时创建空景。提交时两章内容版本推进到同一个大于原两章版本的值，规划版本与 AI 发送校验所用的可见性版本同步推进。显式启用 AI 的作者区块随场景移动时，历史保护代次也推进。
- 场景覆盖模式和原值随场景移动。继承项实时读取目标章默认值，预览按项显示差异。场景演变保留原首尾边界，故事位置重验和设定求值与两章正文、版本及审计同事务。章级锚点、冻结定稿和发布记录按原来的章身份读取。
- 现稿写入需要当前归属。历史读取可以使用同书的移动和规划归属证据。同一章的版本恢复使用实际归属与移入版本边界。旧纯文本通过显式提取入口选定当前目标。删章前核对删除集合外仍存的版本证据。若移出场景已成为某章唯一的原生身份，只在确有必要时创建退场空锚点以保留该章的可识别性。
- 连载和搜索使用最早原生场景识别章节，包括已移出的首次归属。导出时保留旧历史关联，省略历史版本时清除移动的版本序号。批次撤销使用执行时的章、场景和版本。
- AI 输入为选中的已保存正文及最多 32 个安全候选编号，任务复用身份、授权、模型绑定和发送守卫。结果持久化候选编号及短理由。空建议有效，合并至多一条，拆分至多三条。重开时按原签名和当前保存稿重建前后段落。采纳再次核对原任务、稿件版本、归属、引用和富文档条件，再使用同一人工拆并事务。预览或作业结束本身对稿件零写入。
- 编辑器预览与确认各通过已有保存屏障。本地未同步草稿可查看或导出。页面切换或撤权后废弃旧响应。成功移动后的目标定位用新的章 ID 和同一个场景 ID。

### 4. 校验与错误

| 条件 | 行为 |
| --- | --- |
| 目标不在可读作品内，或场景不属于请求的章节 | 404，避免泄露目标归属 |
| 输入不合法或插入点不在目标活动目录 | 422 或结构校验错误 |
| 预览后章、规划或故事位置条件发生变化 | 409 `scene_move_preview_changed`，重新预览 |
| 章节占用或文档无法安全拼接 | 409 `scene_move_blocked`，返回阻塞项 |
| 恢复会跨越场景归属或旧的移入边界 | 409 `scene_move_restore_conflict`，继续支持旧版对比 |
| AI 返回重复／未知候选或非法结构 | 任务失败，保留现稿 |
| 确认建议时权限、版本、引用或富文档预检变化 | 现有权限或冲突响应，保留现稿和草稿 |

### 5. 验收样例

- 把带有章内分隔、作者区块和设定变化的场景移到另一章。新提及定位到目标章，源章历史仍能查看完整场景。只改变目标章默认值时，继承项实时刷新。
- 源章唯一场景移出后仍能编辑一个空景。原定稿仍可查看，并显示现稿已改。
- AI 找到两个段落之间的安全分界。采纳后两个活动场景的完整投影逐字符等于原稿，重复确认提示刷新。

### 6. 必需测试

- `test_scene_move.py` 与 `test_scene_move_roundtrip.py` 覆盖双章、继承差异、原文与引用回滚、双连接竞争、冻结稿、备份导入与再移动。
- `test_scene_restructure.py` 覆盖 FakeLLM 实际输入边界、结果结构、取消、过期、采纳原子性与保留原文。
- 浏览器回归覆盖原场景定位、移动预览、移动后跳转、建议重开、明确采纳及窄屏操作。

### 7. 写入示例

对同一个预览条件的重复请求应从当前归属和当前版本重新校验。直接更新 `Scene.chapter_id` 会让旧历史和两端章投影失配。移动应通过预览和同事务提交入口执行。

```text
POST /projects/9/chapters/12/scenes/34/move-preview
{"target_chapter_id":18,"insert_before_scene_id":null}
POST /projects/9/chapters/12/scenes/34/move
{"target_chapter_id":18,"insert_before_scene_id":null,"signature":"<预览签名>"}
```
