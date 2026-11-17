# 个人小说模板

## 场景：从作品快照创建空正文新书

### 1. 范围 / 触发条件

修改个人小说模板、来源作品快照、依赖预览、模板图片或模板建书时使用本合同。模板是作者私有创作资源，和提示词模板、单书备份是不同功能。协作者及管理员不能浏览或修改作者的模板，也不能从作者作品保存个人模板。

### 2. 签名

```text
POST /api/v1/projects/{project_id}/novel-template-preview
  {include_config, character_ids, world_ids, organization_ids, career_ids, outline_ids}
  → {source_stamp, missing_dependencies, included_definitions, counts,
     included_relations, omitted_relations, json_bytes, image_bytes, estimated_bytes, warnings}
GET/POST /api/v1/novel-templates
GET/PATCH/DELETE /api/v1/novel-templates/{template_id}
PUT /api/v1/novel-templates/{template_id}/snapshot
GET /api/v1/novel-templates/{template_id}/images/{ref}?revision=...
POST /api/v1/novel-templates/{template_id}/projects
  {expected_revision, intent_key, title, pen_name_id?, overrides?} → ProjectOut
```

请求模型位于 `backend/app/schemas/novel_template.py` 和 `novel_template_snapshot.py`，拒绝多余字段；选择列表不得重复。`intent_key` 是规范 UUID。模板名称最多 100 字符、说明最多 2000 字符；列表分页最大 50，仅返回摘要。

### 3. 契约

- `ainovel.novel-template` v1 为服务端构建的严格白名单快照，使用带类型的局部 ref，验证全部引用；不接受客户端提交任意快照。所选角色、世界观（含地点）、组织、职业、章纲和细纲按当前内容保存。用户可只保存配置，或关闭配置并选择内容。当前值包含已体现的 planned 变化；应用时写入新书 confirmed 初始基线，不重放源历史。
- 配置包括立项信息、完整字段／选项和分类规则、默认风格内容；不选配置时仍携带所选数据必需的最小定义和选项。依赖预览列出缺少的实体及原因，必须由作者明确补选后重新预览，不能静默丢引用。只保留两端均已选择的关联，并在预览中显示保留／省略数量。
- 关系类型快照包含类别、双方称呼和排序；全局类型仅在完整内容唯一匹配时复用，否则重建为本书类型。项目类型始终重建，关系保留有序端点。重建类型发生唯一键冲突时整笔拒绝，不凭名称合并或反转方向。
- 不保存源书名作为新书名、源笔名、系列绑定、协作者、模型及密钥、正文、章节、场景、笔记、聊天、任务、知识库和来源事件历史。新书填写自己的书名并可选择本人笔名。所选大纲按卷首次出现顺序重新连续编号，保留卷标题；章纲号、排序及每条章纲的细纲序号连续。图片冻结为模板自己的标准图片，应用时复制为新书资源，来源、模板和新书相互独立。
- 选择总计最多 2000 个设定、2000 条章纲、5000 条细纲，快照规范 JSON 最多 10 MiB，标准图总计最多 50 MiB，单图最多 512 KiB。获取图片时先检查元数据及实际长度，再核验摘要和标准图片；不得先无界读入损坏的大字段。预览不占作品额度。
- 当前字段使用模板专用投影：按既有时间线排序只流式读取最后有效替换及后续追加，受字节上限约束；不把被覆盖的旧值加载到应用内存。数据库仍须扫描并排序所选时间线的历史事件元数据，不把应用侧有界内存误称为查询成本恒定。
- 预览在一致性读快照中构建图，结束后以原身份在新事务复验授权及来源生命周期。`source_stamp` 覆盖快照和图片、来源标题及回收代次；保存／替换在原用户写屏障下重建并比较，改变即 409。锁顺序为来源风格 → 来源作品；修改模板再锁模板行，避免风格删除以外键更新作品时反向死锁。
- `novel_templates` 和 `novel_template_images` 为用户资源，与来源作品无外键；SQLite 显式 `AUTOINCREMENT`，PostgreSQL 使用 sequence，删除后不得复用模板 ID。修改、替换和删除使用 `expected_revision`；图片读取要求本人当前模板和 revision。迁移 `0057` 在模板、图片或成功收据存在时拒绝有损降级。
- `novel_template_creation_receipts` 只记录成功事务，按 `(owner_id, intent_key)` 唯一；结果作品外键为 `SET NULL`，模板删除不删除收据。单书备份不包含模板或收据，整库备份保留它们；作品备份仍为 v11。
- 建书先锁原登录 User，然后普通读取成功收据。相同摘要先锁并复核结果作品后重放，即使模板已删除或配额已满；不同摘要或结果不可用返回冲突，不创建另一部作品。没有收据时才校验模板、图片和配额，创建作品、设定、大纲、图片、笔名与收据并一次提交；任何异常或取消都回滚。不要先锁收据再锁 Project，因为系统 purge 的外键更新顺序是 Project → receipt。
- 前端只给本人展示来源保存入口；模板库、预览和建书响应按账号、作品及请求代次失效。网络结果未知时保留原建书意图和请求参数，同 key 重试；用户明确开始另一意图后旧响应不能导航或覆盖新意图。

### 4. 校验与错误矩阵

| 条件 | 结果 |
| --- | --- |
| 他人或不存在的来源、模板或图片 | 404；不泄漏私人内容 |
| 请求字段、ID 或 UUID 非法 | 422 |
| 实体依赖未补全 | 400 `template_dependencies_missing` |
| 快照或图片无效、引用图不完整 | 400 `invalid_novel_template` |
| 条目、快照或图片超限 | 400 `template_too_large`，不留部分数据 |
| 来源变化或模板版本过期 | 409 `template_source_changed` 或 `stale_revision` |
| 同意图参数不同／已创建作品不可用 | 409 `creation_intent_conflict`／`creation_result_unavailable` |
| 关系类型无法无损重建 | 400 `template_relationship_type_conflict` |

### 5. Good / Base / Bad

- Good：从有正文的作品勾选角色和章纲，补齐所需引用，用不同笔名创建新书；新书有独立设定和大纲，章节数与字数均为零。
- Base：只保存配置且没有设定条目，也保留字段、选项和分类目录；不选配置时只携带所需配套定义。
- Bad：将整书备份删掉正文字段当作模板；或超时后换新 UUID 重建，造成重复作品。

### 6. 必需测试

- 严格 DTO／局部引用：四类主体、重名、循环依赖、缺依赖、typed 字段、隐藏规则、职业阶段、关系方向和类型冲突；当前 planned 值落为 confirmed 基线。
- SQLite 和真实 PostgreSQL：真实迁移及拒绝有损降级、ID 不复用、私库和来源权限、来源／风格／图片变化、revision CAS，以及来源回收／删除后模板独立。
- 同用户相同／不同 key 并发、最后作品额度、提交结果未知、模板删除后重放、结果回收／恢复／彻删、系统 purge 锁序；故障与取消不残留项目、图片或收据。
- 前端和真实浏览器：依赖明确补选、私人入口、模板重存、不同笔名、空正文、身份切换和迟到响应、未知结果同 key 重试、桌面及 375px。

### 7. 错误写法 vs 正确写法

```python
# 错误：先锁收据，再锁结果作品，与 purge 的外键更新形成反向锁序。
receipt = await db.scalar(select(NovelTemplateCreationReceipt).with_for_update())
project = await project_lifecycle.lock_record(db, receipt.result_project_id)

# 正确：见 backend/app/services/novel_template_service.py，先取得用户屏障，普通读取收据。
await authz.require_authenticated(db, actor, write=True)
receipt = await db.scalar(select(NovelTemplateCreationReceipt).where(
    NovelTemplateCreationReceipt.owner_id == actor.id,
    NovelTemplateCreationReceipt.intent_key == body.intent_key,
))
```
