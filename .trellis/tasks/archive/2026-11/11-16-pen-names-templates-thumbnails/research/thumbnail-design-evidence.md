# Research: 四类设定缩略图的存储、系列归属与兼容边界

- Query: 调查本地设定图片上传的复用点、真实缩略图处理、四类设定及系列投影归属、删除与 Agent 撤销、备份/卡片/模板兼容、前端权限隔离和并发验证。
- Scope: mixed；当前源码只读调查及 Pillow 官方文档核对。本文包含**建议契约**，不代表已实现。
- Date: 2026-11-16
- Active task: `.trellis/tasks/11-16-pen-names-templates-thumbnails`，当前 planning。
- 已确认产品范围：首版本地上传，覆盖角色、世界观（含地点）、组织、职业，支持上传/替换/删除及列表/详情展示；无 AI 生图。个人模板配置＋可选设定/大纲，新书正文为空。
- 规划协调结论：系列共享角色/世界观以共享条目为图片权威；采用作品只显示权威图。主会话已采纳本文的“不可变图片资源＋主体绑定”两表方向，历史资源保留到所属项目/系列永久删除，最终产品说明须交代空间代价。
- 最终规划版本安排：笔名阶段先升单书备份 v10，缩略图阶段再升 v11；设定卡 v7、Agent effects v7、卡片紧凑 JSON 总上限 16 MiB 均已由父任务确定，不再作为待选建议。

## Findings

### 1. 结论与边界

推荐新增一个统一的不可变二进制资源表、一个具有真实主体 FK 的当前绑定表。资源归属单个项目或系列；绑定归属四类书内实体之一或一个系列条目。列表只取图片元数据，不读二进制；图片 GET 通过已鉴权的书内主体或系列条目路径解析当前资源。替换创建新资源并更新绑定，删除只删绑定，设定级联删除也只删绑定。资源不参与 Agent 快照，绑定小行参与快照与撤销。

此设计比在每次 Agent 操作前后复制全部图片更可控，也不需要给所有级联删除入口加图片字节捕获钩子。代价是旧图占用数据库空间直至项目/系列硬删除；单书备份、设定卡和模板仅复制当前有效图，不复制这段历史。首版不增设图库、资源浏览 API、全局去重、后台垃圾回收或对象存储。

图片仅用于人工展示，不进入 AI 提示词、工具模型输入、检索文本/向量、设定时间线基础值、规划正文或章节内容。首版输出真实有界缩略图，不以 CSS 缩小原图冒充缩略图。

### 2. 当前图片能力及可复用位置

| 当前源码证据 | 已有行为 | 对新能力的含义 |
| --- | --- | --- |
| `backend/app/ai/images.py:6`, `:12`, `:23` | 10 MiB 上限，PNG/JPEG/WebP 魔数、可选 MIME 一致性；没有真实解码、像素/边长/帧数检查 | 复用格式与错误语义；魔数检查不能单独作为缩略图安全校验 |
| `backend/app/ai/images.py:34` | base64 长度预检与严格解码 | 供图片导入共同复用，先限制编码长度再分配解码结果 |
| `backend/app/core/upload_limit.py`；`backend/app/main.py:184` | ASGI 中间件在 multipart 解析前控制 Content-Length 和实际流量；封面规则含 1 MiB multipart 余量 | 给本地及系列图片 PUT 路径都加规则；仅 `UploadFile.read(limit+1)` 不够 |
| `backend/app/models/entities.py:292` | `ProjectCover` 将图片 bytes 放数据库，项目 FK CASCADE | 无需新增媒体目录/部署数据卷，整库备份可自然覆盖 |
| `backend/app/services/cover_service.py:46`, `:54`, `:58` | `content.read` 读图、`content.edit` 锁内写图；更新时间至少比前值多一微秒 | 复用鉴权和事务模式，图片版本建议改用不可变资源 UUID |
| `backend/app/api/v1/project_cover.py` | 鉴权图片响应，带版本查询使用 private immutable，无版本 private no-cache | 可复用响应构造；缩略图建议更保守的 private/no-cache，见下文 |
| `frontend/src/components/project/CoverPanel.tsx:20` | 上传异步前后检查项目/用户身份，只合并封面元数据，不重置脏表单 | 四类设定图片采用同样的局部合并和作用域保护 |
| `backend/pyproject.toml:5` | Python `>=3.12,<3.13`，当前依赖无 Pillow/PIL/opencv/imageio/imagesize/pypng | 需要新增解码依赖；本次未安装依赖或改 lockfile |

本次检索 `backend/pyproject.toml`、`backend/uv.lock`、`backend/app` 未发现可复用的真实图片解码库。已有封面测试包含轻量魔数夹具，新缩略图解码测试必须使用真实可解码图片，不能顺带把封面夹具当成有效解码样本。

### 3. 建议的图片处理契约及官方依据

建议值均为本任务的工程约束，不是 Pillow 默认值：

| 项目 | 首版建议 |
| --- | --- |
| 上传格式 | JPEG、PNG、WebP，实际内容与声明 MIME 一致；拒绝 SVG/GIF/HEIC 等其他格式 |
| 文件字节 | 输入不超过 10 MiB；路由请求上限加既有 1 MiB multipart 余量 |
| 解码边界 | 宽、高分别不超过 8192，宽×高不超过 16,000,000；正尺寸；仅单帧，拒绝动画 PNG/WebP |
| 输出 | 最长边 512 px、保持比例、不放大小图；统一 WebP，RGB/RGBA，保留透明背景；建议 quality=85、method=4 |
| 输出大小 | 不超过 512 KiB；异常大结果明确拒绝，避免无上限循环重编码 |
| 元数据 | 应用 EXIF 方向后清除 EXIF/XMP/ICC 等上传元数据；不保存原图或原始文件名 |
| 执行 | 图片处理放有界工作线程，例如并发 2；昂贵解码前轻量鉴权、解码完成后锁内重验权限与主体状态 |
| 导入 | 对已经标准化的合法图执行同样的格式、完整解码、帧数及尺寸检查；保留验证后的原标准图 bytes，避免每次导入重复有损压缩 |

处理流程建议：先魔数/长度 → `Image.open(..., formats=[...])` 识别 → 在 `load` 之前检查尺寸/帧数 → `verify()` 后重新打开并完整 `load()` → EXIF 方向变换 → RGB/RGBA → 等比 `thumbnail((512,512), ...)` → 用新建输出图编码且不转抄 metadata → 校验最终大小。Pillow 解码异常转换成明确 400，超大请求/文件沿用既有大小错误体系。保持 Pillow 的炸弹防护，不全局关闭 `MAX_IMAGE_PIXELS`；把 `DecompressionBombWarning` 当错误，另加更低的显式 16 MP 业务边界。

官方依据：Pillow 的 `Image.open` 是延迟解码，可限定识别格式；完整载入另由 `load` 执行。`verify` 是结构校验且之后需要重新打开；`thumbnail` 在目标矩形内保留比例。官方也明确区分炸弹警告和更高阈值错误。[Image 官方 API](https://pillow.readthedocs.io/en/stable/reference/Image.html)

`ImageOps.exif_transpose` 会应用方向并去除方向标签；它不等于移除全部其他图片元数据，因此输出仍需显式不携带原 metadata。[ImageOps 官方 API](https://pillow.readthedocs.io/en/stable/reference/ImageOps.html#PIL.ImageOps.exif_transpose)

WebP 保存支持 quality、alpha_quality、method；EXIF/ICC/XMP 仅在传入时写入；动画默认只存第一帧不应成为产品的隐式行为，建议明确拒绝多帧输入。[WebP 官方格式说明](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#webp)

访问时 stable 文档为 Pillow 13.0.0，Python 支持矩阵包含 Python 3.12。可新增 `Pillow>=13,<14` 并由实施步骤生成精确 uv 锁版本；打包环境还需确认 WebP 编解码能力，不能只验证 import 成功。[Python 支持矩阵](https://pillow.readthedocs.io/en/stable/installation/python-support.html)

### 4. 四类实体、系列投影及授权的实际结构

- `backend/app/schemas/setting_field_value.py:8` 将主体限定为 character/world/organization/career；地点已经是世界观 `place` 分类，不建第五类图片主体。
- `backend/app/models/entities.py:315`、`:334`：世界观和角色已有 `(project_id,id)` 唯一键；`:365`、`:487` 的组织和职业只有 `(project_id,name)` 唯一约束，需要补 `(project_id,id)` 唯一键以支持数据库级同作品 FK。
- `backend/app/models/series.py:42`：`SeriesEntry` 仅共享 character/world，现有唯一键是 `(series_id,id,kind)`。图片绑定可补 `(series_id,id)` 唯一键，不必仅为 FK 新增冗余 kind 字段。
- `backend/app/models/series.py:59`：`SeriesEntryUse` 用分支 CHECK、单主体唯一键、项目＋实体复合 FK 保证投影身份，并用延迟 NO ACTION FK 管理投影删除。采用后的本书实体 ID 与 canonical ID 不同。
- `backend/app/services/series_service.py:107` 的 `_readable` 支持系列作者或已接受的系列协作者，并非只有作者。书内协作者不因此获得系列访问权。规范的历史描述不能代替这段当前实现。
- `backend/app/services/series_policy.py:54` 对没有系列权限的书内协作者返回不暴露系列 ID 的 `setting_edit_restricted`；有系列能力者返回 `series_setting_managed`。图片本书写入口应沿用这个区分。
- `backend/app/services/series_read.py:38` 最后重新核对书籍授权/令牌/生命周期；`:87` 起将系列绑定投影到本书，`:124` 起隐藏非系列成员可见的系列标识。图片 metadata 与 GET 必须保留此边界。

建议权限：本地图片 GET 使用本书 `content.read`；本地 PUT/DELETE 使用 `content.edit`，锁内确认主体同项目且未绑定共享库。绑定共享库的角色/世界观从书内只读权威图，包括有书内编辑权的协作者；系列图片编辑通过系列路由和现有系列编辑能力执行，不能经本书路由隐式修改其他作品。没有系列权限的合法书内读者仍能读取**本书已采用**条目的当前图片，但不能凭图片路径枚举其他共享条目。

### 5. 存储方案比较与推荐两表约束

| 方案 | 优点 | 主要代价 | 结论 |
| --- | --- | --- | --- |
| 单个图片表，bytes＋四类实体 FK＋series_entry FK，恰好一个主体 | 单一 codec/查询模型，真实 FK 能保护主体 | 主体级联删除会丢 bytes；全量 Agent 前后快照读 bytes/base64 很重；按需捕获需覆盖全部删除入口 | 单纯 CRUD 可行；不适合当前完整撤销要求 |
| 本地图/系列图两张 bytes 表 | 所属分支更直观、DDL CHECK 少 | 二进制处理重复，仍存在 Agent 删除恢复问题，不能靠双表本身解决 | 不推荐作为最终模型 |
| **不可变资源＋当前主体绑定** | Agent 只记录小行；删除/撤销自然；复用 codec，跨作品归属可由 FK 强制 | 历史资源保留占空间，多一层资源关联 | **推荐并已获主会话采纳** |
| 无 FK 的 `(kind,subject_id)` 泛型资产表 | 字段少 | 数据库不能防跨作品绑定/悬空引用，删除与恢复更依赖业务代码 | 不采用 |

建议表一 `setting_image_resources`：

- `id` 为不可变 UUID；`project_id`/`series_id` 恰好一个非空，各自 FK `ON DELETE CASCADE`；bytes、mime_type、width、height、byte_size、sha256、created_at。
- 内容和生命周期归属在创建后不更新。新上传即新资源，不提供资源级公开读/改/删 API；不使用整数 ID 复用作为图片版本。
- 建 `(project_id,id)`、`(series_id,id)` 唯一约束，供绑定表的复合 FK 验证资源同作用域。内容相同的当前图可做 no-op，但不需要全局或跨作用域去重。
- 普通图片替换、删除、设定删除、取消采用均不清理历史资源。只有所属项目/系列永久删除清理资源；软回收保留。项目硬删不应删除仍属系列的权威图，系列硬删不应删除已复制回本书的图。

建议表二 `setting_thumbnail_bindings`：

- 独立 id，SQLite 使用 AUTOINCREMENT；`image_resource_id`、`project_id`、`series_id`，以及可空的 character_id/world_setting_id/organization_id/career_id/series_entry_id。
- CHECK：本地分支 `project_id!=NULL, series_id=NULL, series_entry_id=NULL` 且四类主体恰好一个；系列分支 `project_id=NULL, series_id!=NULL, series_entry_id!=NULL` 且四个本地主体全空。不要仅验证“总共一个主体”而遗漏作用域一致性。
- 每个主体列独立 UNIQUE，保证每个实体/共享条目只有一张当前图。四类 `(project_id,subject_id)` 复合 FK 到各实体 `(project_id,id)`，`ON DELETE CASCADE`。
- 系列 `(series_id,series_entry_id)` FK 到 `SeriesEntry(series_id,id)`，`ON DELETE CASCADE`。
- 两条资源复合 FK：`(project_id,image_resource_id)` 与 `(series_id,image_resource_id)`。只有当前分支的完整非空复合键生效；必须有上述 CHECK，防止利用 NULL 绕过归属约束。
- 资源 FK 可采用现有投影相同的 `NO ACTION DEFERRABLE INITIALLY DEFERRED`，避免项目/系列整体级联时的删除顺序问题，同时不允许留下绑定引用丢失资源；需在 SQLite 和 PostgreSQL 验证硬删。绑定自身加对应项目/系列 CASCADE FK 使生命周期显式可见。

这些约束保证即使绕过服务层写入，也不能把甲作品主体挂到乙作品资源；没有系列成员关系的普通项目仍可拥有自己的四类图片。不要增加“全局公共资源”第三分支。

### 6. 系列转换、锁序与图片版本

现有写入口 `backend/app/services/series_service.py:147` `_edit` 先取得系列锁并校验 expected_revision；`:427` `_lock_projects` 按排序 ID 取得可见性门禁与项目生命周期锁。共享基线更新在 `:731` 的同一事务处理所有投影。保持 **系列 → 排序后的项目门禁/项目锁 → 图片绑定**，不能新增相反的“项目锁后等待系列锁”路径。

| 现有操作与源码 | 图片应执行的原子变化 |
| --- | --- |
| 本地上传/替换/删除，复用 cover 的项目写锁模式 | 解码在长事务外；锁内重新鉴权、查实体和共享绑定，再更新本地图绑定。不能把绑定检查放到项目锁之前作为最终判断 |
| `promote`，`series_service.py:816` | 当前本地图复制成系列所属资源，创建 canonical 绑定，删除本地图绑定，保留原本地资源供既有 Agent 历史；书内原实体 ID 不变，通过采用关系显示 canonical |
| `_source`，`:783` | source_stamp 当前没有图片；必须纳入当前图片资源 UUID/hash，防止提升预览后图片被换却继续提升旧方案 |
| `use_entry`，`:857` | 新建书内投影和采用关系，不复制 canonical 图片、不建本地图绑定；当前图片通过共享关系解析 |
| `unuse`，`:936`；`leave`，`:926` | 当前语义是删除投影，不是留下独立本地副本；只删除采用关系/投影，canonical 绑定与系列资源保持。不得借缩略图更改原有退出语义 |
| `_retire_entry(...move_to=...)`，`:951` | 删除共享条目前将当前图复制到目标作品新资源并创建本地图绑定；已采用目标保留本地实体 ID，未采用目标新建实体。资源不可直接从系列改归项目，防止历史引用跟着移动 |
| 删除共享条目，`:985` | 删除 canonical 当前绑定；资源保留在系列历史。已采用投影的阻塞/作者限制仍沿用现有逻辑，不能绕过 |
| 删除独立设定 | 数据库级联删当前绑定，图片资源保留；Agent 可按原绑定恢复 |

共享图片更新虽不影响 AI 文本，也应推进 `series.revision`，让库编辑 CAS/转换预览发现变化。所有使用该条目的本书列表/详情通过新的资源版本 URL 更新。不要为了图片无差别推进 AI 可见性、重建检索或创建设定演变事件。

建议读 API 为 `/projects/{pid}/settings/{kind}/{id}/thumbnail` 与 `/series/{sid}/entries/{eid}/thumbnail`，操作使用 GET/PUT/DELETE；最终命名可按现有路由风格调整。书内 URL 永远只含本书 kind/id，不含 sid/eid。metadata 只含 `version`、mime、width/height、可编辑能力/已有的来源标记；不返回原文件名、资源所属系列、其他采用者或资源裸读取 URL。

列表批量查 binding/resource **元数据列**，禁止 `select(resource_model)` 把 bytes 读进列表。GET 每次确认当前主体授权和当前生效绑定，不允许用户通过旧版本号访问保留的历史资源。建议 `Cache-Control: private, no-cache`、`ETag` 基于不可变资源版本，先鉴权再判断 304；`v` 用于前端更新而非授权。当前封面的一年 immutable 策略可复用但会延长撤权后客户端本地缓存读取窗口，规划应明确选定；任何服务器方案都不能撤回已经交付的字节。

### 7. Agent effects：只记录绑定、保持删除/撤销完整

当前事实：

- `backend/app/services/agent/effects.py:63` 的 Snapshot 已有私有证据侧字段；`:85` 为明确 EFFECT_TABLES。
- `:125` `_RULES` 从备份 RULES 选取，再**单独**追加 tasks 的 TableRule。这是“备份排除、effects 仍可管理”的直接复用模式。
- `:208` `_columns` 实际遍历模型所有列，`:220` 调用 `jsonable_encoder`；直接把 bytes 表放入 effects 会读取全量二进制并引入序列化问题。
- `:347` snapshot 在操作前后选整书相关行；`:382` diff 保存 before/after。`backend/app/services/agent/service.py:923`、`:976` 的普通与任务效果捕获均依赖此流程。
- `effects.py:849` `_check_dependents` 按 Base.metadata FK 查后添关联，防止撤销旧“新建实体”时静默级联删后来数据。
- `:951` `_preflight_series_effects` 当前只检查角色/世界观及时间线，不能自动保护新增图片绑定。
- `:1151` `_restore_rows` 使用规则拓扑顺序恢复；`:1293` 检查恢复作用域；`:1442` 附近 undo 的整个恢复在保存点内，并在外层项目门禁下完成。

建议接法：

1. 资源表、绑定表均加入 `project_transfer.registry.NOT_EXPORTED`，注明“当前图片经顶层 thumbnails 导出导入”；**不加入通用导入 RULES**。
2. 在 effects.py 的 EFFECT_TABLES 纳入 binding 表，并在 `_RULES` 中显式追加本地 `TableRule(binding_model, scope=project_id==pid AND series_id IS NULL)`，排在四类父实体之后。资源表不进入 EFFECT_TABLES、Snapshot 或 diff。绑定行只含 FK/UUID 等小字段，项目有大量历史图片时 snapshot 仍不读 bytes。
3. `_validate`/恢复前专门校验 binding 的 before/after：只有本地分支；project_id 必须等于当前项目；恰好一个主体；引用资源存在且 `resource.project_id==pid, resource.series_id IS NULL`；父实体属于本项目，允许同次 effects 将其恢复。数据库复合 FK 是第二道保证，不以“存在 resource_id”代替归属验证。
4. 恢复父实体后再恢复绑定；恢复前验证当前 after 状态，保存点内验证最终图归属。资源 UUID 永不复用，不能让历史整数 ID 指向后来新上传的另一张图。
5. `_preflight_series_effects` 的候选集纳入 binding 中的角色/世界观 ID；当前已采用共享条目的任何本地图片绑定插入/改写/删除均报 `undo_conflict`。尤其防止旧 Agent 图片历史在提升后恢复一个本地图覆盖权威图。这里需要保护恢复原语，不能仅保护普通服务入口。
6. 保留 `_check_dependents`：Agent 新建角色后用户上传了图，再撤销该新建操作必须冲突，不能级联吞掉用户的新图；Agent 删除带图角色后撤销应恢复原实体与原资源引用。取消采用/退出仍使用系列操作，不增加 Agent 系列操作能力。
7. 最终规划已确定将 EFFECT_VERSION 从 6 升至 7；旧效果没有 binding 行时，旧实体文本恢复不能清空现有图。旧新建实体撤销有后来图片时仍由 FK dependent 检查阻止。允许正常无图旧历史撤销，不做全局“带图实体不可删除”的能力降级。
8. `tools/base.py:406` 的 `preview_diff` 会泛化展示全部普通列；`schemas/agent.py:100` ChangeOut 实际包含 effects；`services/personal_projection.py:55` 只按已知私有键过滤，不能假设 effects 天然不对外。新增绑定表需显式公共投影：内部资源 UUID/归属字段不送到模型预览或面板，面板如需只展示“缩略图随条目删除/恢复”的摘要。持久 effects 保持完整供撤销，不能就地清理历史。

禁止使用“把 binding 的 image_resource_id 声明为通用 registry fk”的捷径：`project_transfer/registry.py:8` 的契约明确规定目标表没导出时 FK 会变 null，导入就会丢图。effects-only TableRule 不经过这套 remap，图片导入必须走独立逻辑。

### 8. 备份、卡片、模板、整库备份与 API 的反射清单

| 消费方 | 当前实现证据 | 必需变化及范围 |
| --- | --- | --- |
| 单书备份注册表 | `project_transfer/registry.py:90`；`tests/test_project_transfer.py:62` | 两表都登记 NOT_EXPORTED；反射覆盖保持零遗漏；不会向 tables 放 raw bytes 或内部资源引用 |
| 单书导出 | `project_transfer/exporter.py:33` v9、`:35` app_schema0052；`:270` stream，`:283` cover，`:326` 末尾版本复验 | 新顶层 `thumbnails` 数组，以逻辑 `{ref:{kind,id}, mime_type, data:base64}` 导出本书当前有效图，包括通过本书已采用投影解析的权威图；不带未采用共享图、series_id、资源历史或授权 |
| 单书导入 | `project_transfer/importer.py:77` 100 MiB；`:1022` cover 验证；`:1063` 版本判断；`:1565` cover落库 | 最终规划为笔名先升 v10，缩略图再升 v11，兼容导入 v1–v10；旧版本缺失 thumbnails 视为空。先验证图片/唯一主体/总大小，再用实体 ID 映射在新项目创建本地资源和绑定，同一事务提交；拒绝悬空/重复/跨作品逻辑 ref |
| 导出一致性 | exporter `:278` 起快照 AI/规划 revision，`:326` 复验 | 纯图片更新不会自然推进上述版本；导出开始取得当前有效 `ref→resource UUID` 清单，结束重新解析并比较，或明确增加专门图片快照版本。不能用逐张最新图拼成混合时间点。共享图替换、采用关系变化同样检测 |
| 大小估算/导入上限 | exporter `:352`，`:364` cover base64估算 | 核心组增加当前图片的 base64 大小；100 MiB 单书导入上限需包含图片；超限预先报错，不能悄悄丢图。标准图≤512 KiB不意味着整书无界 |
| 设定卡 | `schemas/card.py:21` v6、`:25` 紧凑 JSON 2 MiB、`:214` CardFile；`card_service.py:197`,`:353`,`:780` | 在现有角色/组织/职业卡范围内加入可选当前图片，最终规划已定 v7，旧 v1–v6照常。当前卡没有世界观 payload，不能因图片额外承诺世界观卡导出；世界观图片通过备份/模板覆盖 |
| 卡片体积 | `frontend/src/api/cards.ts:7` 2 MiB；`:9` 文件文本4倍预检；服务端 JSON body | 最终规划已定卡紧凑 JSON 总上限16 MiB，同步前后端和请求体预拦截；仍限制每图与总量，拒绝超限而不静默剥离。不能直接承诺1000张角色图都能装进一份卡 |
| 卡片冲突导入 | card_service 当前 preview/import与解决选项 | 替换已有实体时，缺失图片字段不应清空目标图；选择使用卡中图才替换。新建实体复制图；共享投影冲突沿用 managed-setting保护，不能通过卡片覆盖系列图 |
| 个人模板 | 本任务新增能力，当前无既成实现可证明 | 勾选设定时冻结当前有效图的标准 bytes；不勾选设定则不带图。模板不能引用源项目/系列资源 UUID，否则源删除或换图破坏冻结。应用模板给新项目创建独立资源/绑定，正文仍为空 |
| 整库备份 | `services/backups/database.py:43` SQLite备份、`:72` snapshot、`:108` pg_dump custom | 数据库表 bytes 自动被整库快照覆盖；保留历史图会进入整库备份，这与单书备份仅当前图不同。无需外部媒体包，但需恢复验证和体积评估 |
| Open API | `services/open_api/entities.py:17`、`:43` 显式 schema 投影 | 不自动扩大 API Key 的系列访问，不增加资源级开放下载/上传能力；允许首版公开设定 schema 不暴露图片。内部 schema 加 metadata 时验证不会意外泄露 sid/eid或资源路径 |
| EPUB/文档导出 | 已有封面导出独立能力 | 不额外插入设定图、不将图加入小说正文；无须扩展文档媒体能力 |

个人模板如采用 JSON 冻结快照，可只给当前图增加有界 base64 部分；不必把模板引入项目/系列资源表的第三归属分支。必须验证模板总大小、从协作源读取权限、保存快照和源变更之间的一致性，并只在用户选了设定时复制图。

### 9. 前端落点与异步身份隔离

| 位置 | 调整 |
| --- | --- |
| `frontend/src/components/entities/CharacterCard.tsx`、`pages/project/CharactersPanel.tsx` | 角色列表缩略图，编辑抽屉图片区域；不因图片请求刷新覆盖未保存人物文本 |
| `frontend/src/pages/project/WorldPanel.tsx` | 六类世界观卡片/列表、编辑详情；place自然覆盖地点 |
| `frontend/src/pages/project/OrganizationsPanel.tsx:201`, `:234`, `:311` | 树列表节点小图、OrgDetail及编辑modal；组织成员不是图片主体 |
| `frontend/src/pages/project/CareersPanel.tsx:67`, `:162`、`components/careers/CareerEditDrawer.tsx` | 职业卡片和抽屉；阶段/角色职业关联不是单独图片主体 |
| `frontend/src/components/series/SeriesLibrary.tsx:43`, `:92`，`SeriesEntryEditor.tsx:52` | 共享库列表和编辑详情；标识全系列共享图片。新条目先有持久实体ID再开放图片上传，避免临时孤儿资源 |
| `frontend/src/stores/project.ts:183`, `:192` | 沿用 captureProjectScope/isProjectScopeCurrent；提交前后校验同一用户、作品、邀请/授权版本、当前写权限 |
| `components/series/useSeriesAction.ts:23`、`SeriesAccessBoundary.tsx:8` | 系列操作必须继续使用 series identity/授权版本；切书、登出、撤权后的旧请求不得回写新作用域 |

建议抽出通用展示与编辑组件，共享前端文件初检、object URL清理、上传进度/失败重试、替换/删除。列表图有固定尺寸和懒加载，提供名称 alt、无图/加载失败占位，不需要新增裁剪器、原图查看器或图库。只读用户保留图片展示并不显示操作；共享投影使用现有 managed-setting 文案，不给无系列访问权者显示管理共享库的链接。

图片成功后只合并该实体的 thumbnail metadata；不整页 reload 导致丢失文本草稿。将“保存文字”和“保存图片”分开明确呈现，图失败不伪装成整体成功，避免把文字编辑草稿偷渡进图片请求。图片 GET 401/403/404根据当前作用域处理，异步解码/上传结束后再次检查有效身份；AbortSignal不能代替服务端权限复验。

### 10. 迁移和验证清单（本次未执行）

迁移应新增两表与组织/职业 `(project_id,id)`、系列条目 `(series_id,id)` 唯一键；注册模型到统一 metadata；为历史实体保持无图；固定迁移结构、使用 SQLite/PG都支持的 CHECK和FK；新增迁移号需由父任务统一分配并同步 exporter.APP_SCHEMA。降级不能静默丢弃已有图片：按仓库既有模式拒绝有数据的有损降级，或先明确不可逆策略。迁移测试不能导入当前 ORM动态生成旧 schema 代替真实版本升级。

建议测试职责：

1. 新 `test_setting_thumbnails.py`：四主体 CRUD、越权/跨作品 FK、读者与编辑者、回收/硬删、缺图、替换版本、旧版本不可读；列表查询不带 bytes；请求体无 Content-Length、虚假长度、multipart超限。
2. 解码：真实 JPEG/PNG/WebP、伪魔数/截断/错 MIME、超宽/超像素、Pillow炸弹、动画、EXIF旋转、透明图、≤512px、输出上限和 metadata 清理；导入 bytes稳定。不以仅魔数假图证明解码成功。
3. `test_series.py`、`test_series_projection_guards.py`、`test_collaboration_series.py`：提升后同图且原本地binding移除、提升source_stamp受图变化影响、采用仅解析canonical、取消采用不删系列资源、退出遵守原删除投影语义、移回复制到新作用域、删除共享条目/项目/系列的资源边界、单书协作者不能编辑/枚举系列图。
4. `test_agent_effects.py` 新专题：带图实体删除→撤销完整恢复；替换/删除后资源保留；新增实体后手工加图→旧撤销冲突；提升后旧撤销不能插本地图；伪造跨项目resource UUID/主键复用拒绝；旧effects兼容；整轮撤销原子回滚。用查询监听证明snapshot不取资源bytes，公共预览及模型输入没有图片数据/资源归属。
5. `test_project_transfer.py`、`test_series_transfer.py`、`test_cards.py`、模板测试：当前图roundtrip、新ID映射、共享图落为独立本地图、历史图不导出、旧格式兼容、错ref/重复ref/非法图/超限原子失败、导出过程中换图或撤权拒绝混合快照、模板冻结不随源图变化。
6. `test_backup_contracts.py`、`test_backup_adapters.py`、`test_migrations.py`、新缩略图迁移测试：SQLite/PG表约束、完整库备份恢复当前与保留历史资源、生命周期硬删不遗留外键、反射清单齐全。
7. 前端 API/状态单测＋真实浏览器：作者/书内编辑者/只读者/系列成员，桌面及375px宽屏，上传/替换/删除、列表详情一致，文字脏稿保留；切用户/切书/撤权时旧请求和图片响应不污染下一作用域；共享图片操作入口正确。

**PostgreSQL必须实际并发验证**，SQLite通过不能替代：

- 图片PUT vs 提升、移回、取消采用；图替换 vs Agent删除/撤销；共享图替换 vs book/series撤权；项目/系列硬删 vs 上传；备份/模板复制 vs 换图。用两个独立事务及明确 barrier验证锁等待与最终状态，不靠 sleep猜竞态。
- `backend/tests/test_series_postgresql.py:24` 读取 `AINOVEL_SERIES_POSTGRES_TEST_URL`，严格限定 `postgresql+asyncpg://series_test@127.0.0.1/series_verify`，每次独立schema并使用 `tests.schema_helpers.create_isolated_schema`。
- 协作场景可沿 `backend/tests/pg_support.py:38`, `:80` 的 `AINOVEL_COLLAB_POSTGRES_TEST_URL`/模块专用变量，要求本机 `collab_test`账号/专用库。`pg_support.py:3` 明确提醒：同一database不可并行跑多个pytest进程，advisory lock不按schema隔离；需要并行则分数据库，不能改产品锁来掩盖测试串扰。
- 当前仅调查，没有运行上述产品测试、创建数据库、安装Pillow或进行迁移。

### 11. Files found

- `backend/app/ai/images.py`、`core/upload_limit.py`、`api/v1/project_cover.py`、`services/cover_service.py`：图片输入、请求上限、鉴权读写与版本复用。
- `backend/app/models/entities.py`、`models/series.py`：四类实体、系列归属和当前可用FK结构。
- `backend/app/services/series_service.py`、`series_policy.py`、`series_read.py`：转换、锁序、协作隔离及共享只读投影。
- `backend/app/services/agent/effects.py`、`agent/tools/base.py`、`agent/service.py`、`personal_projection.py`：快照、恢复与公共投影。
- `backend/app/services/project_transfer/{registry,exporter,importer}.py`、`schemas/card.py`、`services/card_service.py`：专用图片格式、反射和引用重映射。
- `backend/app/services/backups/database.py`、`services/open_api/entities.py`：整库范围与开放接口显式schema边界。
- `frontend/src/components/project/CoverPanel.tsx`、四类Panel、series组件、`stores/project.ts`、`api/cards.ts`：列表详情、上传状态与身份隔离复用点。
- `backend/tests/test_project_transfer.py`、`test_agent_effects.py`、系列/协作测试、`tests/pg_support.py`：回归入口和真实并发隔离约束。

### 12. Related specs

已参考 `.trellis/workflow.md`、`.trellis/spec/backend/project-cover.md`、`series-settings.md`、`project-transfer.md`、`quality-guidelines.md` 与 `.trellis/spec/frontend/state-management.md`。这些文档作为待遵守约定；上面的现状结论均以列出的当前源码为证据。实施后需要同步新增缩略图契约、备份/卡片版本、Agent图片binding恢复、系列转换及前端权限状态规范，本次没有修改spec。

## Caveats / Not Found

- 本文是规划调查，不是实现或测试通过证明；所有新表、API、大小阈值、版本号和测试模块均需实施。
- 不可变资源保留到项目/系列硬删除会增加数据库与整库备份体积。主会话已采纳此取舍；最终产品说明应明确。首版不另加垃圾回收/全局去重，也不承诺历史图片可浏览。
- 父任务已统一格式为笔名阶段 project v10、图片阶段 project v11/cards v7/effects v7，卡片紧凑 JSON 为16 MiB。单书总100 MiB既有上限需在有图时继续可见，不能以自动忽略图片规避；实施仍须按真实迁移head顺序分配迁移号。
- 外部Pillow文档已经官方站点核对；Context7库识别成功，但文档查询返回fetch failed，未将其视为内容证据。访问时版本13.0.0支持项目Python3.12；未在本机安装或验证其轮子/WebP编解码。
- 缩略图归属无需新增用户决策：已按确认的范围和主会话采纳方案收敛。512px/有界输入/仅标准化图不保留原图是工程建议，最终方案应明示，若用户后续要求原图/图库则属于新范围。

## 规划对照检查

- 检查日期：2026-11-16；只读父任务与 `../11-16-setting-thumbnails/` 的 `prd.md`、`design.md`、`implement.md`，对照本文已核对的当前源码；未改其他文档、未实施、未运行测试。
- **阻止实施的矛盾／遗漏：无。** 方案已明确四类主体与资源的复合FK、恰好一个作用域CHECK和必要父表唯一索引；系列保持 Series→排序项目锁；effects-only 本地binding规则与通用备份RULES分开，包含同项目资源验证、共享归属预检及公共投影；历史bytes保留、父实体先恢复再恢复binding、跨作用域复制和硬删清理均有契约与验证步骤。
- 该结论仅表示本次有界设计对照未发现阻断，不代表复合FK级联、真实并发、Pillow打包或删除／撤销已经验证；这些证据仍按子任务实施计划执行。
