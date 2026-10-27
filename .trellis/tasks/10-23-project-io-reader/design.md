# 技术设计：项目 JSON 备份与 EPUB 导出

## 1. 备份格式 `ainovel.project` v1

```json
{
  "format": "ainovel.project",
  "version": 1,
  "exported_at": "2026-10-27T10:00:00Z",
  "app_schema": "0015",                // 导出时的 alembic 版本，仅供参考
  "options": {"versions": true, "snapshots": false, "generation_logs": false, "inspiration": false},
  "styles": [ {"ref": "s1", "preset_key": "lengjun" } , {"ref": "s2", "name": "…", "pov": "…", …} ],
  "project": { …项目字段…, "default_style_ref": "s2" | null, "wizard_progress": {...} },
  "tables": {
    "world_settings": [ {"id": 3, …} ],
    "characters": [...], "organizations": [...], "organization_members": [...],
    "relationship_types": [ 只含本项目自定义类型 ], "relationships": [...], "relationship_history": [...],
    "chapter_characters": [...],
    "careers": [...], "career_stages": [...], "character_careers": [...], "career_history": [...],
    "outlines": [...], "chapter_plans": [...], "chapters": [ {…, "style_ref": "s1" | null} ],
    "foreshadows": [...], "foreshadow_characters": [...], "foreshadow_history": [...],
    "chapter_analyses": [...], "analysis_changes": [...],
    "story_memories": [...],
    "kb_settings": {...} | null, "kb_documents": [ {…, "text": "…"} ],
    // 可选
    "chapter_versions": [...], "outline_snapshots": [...], "generation_logs": [...], "inspiration_sessions": [...]
  }
}
```

- 每行都保留导出时的原始 `id`，外键也保留原值。导入时，按表建立"旧 id → 新 id"映射，再逐列重新映射。
- 不导出的列：`project_id`、`user_id`（导入时取当前值）、`created_at` / `updated_at`（导入时保留原值，便于在版本历史里看到原来的时间）、`content_hash` 之类的派生列（导入时重新计算或原样保留，按表规则处理）。
- 关系类型：只导出本项目的自定义类型（`project_id` 非空）。全局预置类型按 `(role_a, role_b)` 引用；导入时按这两个值找到全局类型，找不到就新建为项目自定义类型，并给出警告。
- 风格：项目默认风格和章节风格用 `styles[]` 里的 `ref` 引用。预设只记 `preset_key`，自定义风格带上全部字段。导入时：
  - 预设：按 `preset_key` 找。找不到（例如预设已被删除），就复制一份作为导入者的自定义风格，并给出警告。
  - 自定义风格：导入者的库里已有同名、且所有风格字段都相同的风格，就复用；否则新建一份。同名但内容不同时，名称加后缀「（导入）」。

## 2. 表规则注册表（`app/services/project_transfer/registry.py`）

```python
@dataclass(frozen=True)
class TableRule:
    table: str                       # 表名
    model: type[Base]
    option: str | None = None        # None = 核心；否则对应 options 键
    fk: dict[str, str] = {}          # 列 → 被引用表（导入时重映射）
    json_refs: dict[str, Callable] = {}   # JSON 列 → 重映射函数（见下）
    exclude: tuple[str, ...] = ()    # 不导出 / 导入时重算的列
    order: int                       # 导入顺序（被引用表在前）
```

- **反射测试**（与 `CHAPTER_NO_REFS` 的思路一致）：
  1. 遍历所有带 `project_id` 的表，以及经由外键间接属于项目的表，每张表都必须注册为 `TableRule`，或者出现在 `NOT_EXPORTED` 清单里（tasks、kb_chunks、retrieval_chunks），清单中的每一项都要写明理由。
  2. 已注册表的每一个外键列，都必须出现在 `fk` 或 `exclude` 中。
  3. 已注册表的每一个 JSON 列，都必须出现在 `json_refs` 中，或者显式标为"不含 id"。

  以后新增表或列时，忘了登记会直接导致测试失败。
- **JSON 内的 id 引用**，逐一定义重映射函数：
  - `chapter_plans.character_ids`：角色 id 列表。
  - `analysis_changes.target`：其中的 `character_id`、`relationship_id`、`career_id`、`organization_id`、`foreshadow_id`、`a_id`、`b_id`、`stage_id` 等键，按键名对应到各表。`before` 和 `after` 中凡是以 `*_id` 结尾的键，也按同样的映射处理；对应不上的值置为 null，并记一条警告。具体有哪些键，实现时以 `writeback_service` 中实际写入的键为准，并由测试覆盖每一种变更类型。
  - `relationship_history.changes`、`career_history.changes`、`foreshadow_history.changes`：其中的阶段 id、章节 id 等，规则同上。
  - `outline_snapshots.data`：其中 outlines 和 plans 的 `id`、`outline_id`，以及 links 里的 `chapter_id`、`outline_id`、`plan_id`，都要重映射。快照中引用的大纲或细纲在当前数据里已经不存在时，按旧 id 分配新 id，保证快照内部自洽。
  - `inspiration_sessions`：`project_id` 和 `converted_project_id` 都指向新项目；`state` 中不含 id。
  - `generation_logs.params`：原样保留，只作信息记录。
  - `chapters.style_id` 和 `projects.default_style_id`：通过风格的 `ref` 映射，不走普通外键映射。
- **自引用和循环引用**：例如 `chapter_plans.chapter_id` 与 `chapters.plan_id` 互相引用。导入分两步：第一步插入时，这类列先填 null；第二步统一回填。在 `TableRule` 中用 `deferred_fk` 标出这类列。

## 3. 导出（`project_transfer.export_project`）

- `GET /projects/{id}/backup?versions=1&snapshots=0&logs=0&inspiration=0`，返回 `application/json` 附件，文件名为 `书名-备份-YYYYMMDD.ainovel.json`（RFC 5987 编码）。
- **流式写出**：按表逐张查询并编码，用 `StreamingResponse` 分块输出，避免一次性把大项目全部放进内存。章节版本和生成记录按 500 行一批分页查询。
- 导出前先校验项目归属，他人的项目返回 404。
- 估算大小：前端在导出前调用 `GET /projects/{id}/backup/estimate`，返回各项数据的行数和字符数，用于提示"包含生成记录后文件会很大"。

## 4. 导入（`project_transfer`）

- `POST /projects/import/preview`：multipart 上传，上限 100MB，复用知识库的上传限制中间件（扩展为对一组路径生效）。
  - 解析时用 `json.loads`，并限制嵌套深度。
  - 依次校验：`format`、版本（大于当前支持的版本时返回 400"备份来自更新的版本，请升级后再导入"）、各表的必需字段、外键能否在文件内部找到。
  - 不写库，返回：`{title, version, options, counts: {table: n}, warnings: [...], errors: [...], token}`。
  - `token` 是一个临时引用：把解析后的数据放进进程内缓存（保留 10 分钟，按用户隔离），供确认导入时使用，避免重复上传；缓存过期时，提示重新选择文件。
- `POST /projects/import`，请求体为 `{token, title?}`，也可以直接用 multipart 上传文件：
  1. 在单个事务中，按 `order` 依次插入：先建项目（重名时书名加「（导入）」）→ 风格 → 各表；对 `deferred_fk` 中的列做第二步回填。
  2. 剧情记忆保留 `pinned`、`muted` 和 `content_key`；知识库文档保存 `text`，并通过分块函数在同一事务里重新生成 `kb_chunks`。随后为整个项目重建 `retrieval_chunks` 中的关键词部分：知识库沿用 `kb_service` 的写入点，记忆沿用 `memory_service` 的写入点，向量留空。
  3. 提交后：如果用户配置了向量模型，就排队一个重建模式的 `kb.index` 任务。
  4. 返回 ProjectOut 和警告列表。
- **PostgreSQL 序列**：导入时一律由数据库分配新 id，不保留旧 id，因此不需要校正序列。
- **格式升级**：`migrate(data) -> data` 按版本逐级升级。目前只有 v1，留好扩展位置。
- **失败**：整个导入在一个事务里，任何一步出错都整体回滚，返回 400 并带上出错的表名和行序号。

## 5. EPUB（`app/services/epub_service.py`）

- 入口：沿用 `GET /projects/{id}/export`，增加 `format=epub`；额外参数为 `author`（不超过 100 字，默认用户名）和 `include_synopsis`（默认 true），`chapter_ids` 与现有导出相同。
- 用 `zipfile` 写出以下文件：
  1. `mimetype`：第一个条目，`ZIP_STORED`，内容为 `application/epub+zip`。
  2. `META-INF/container.xml`。
  3. `OEBPS/content.opf`：EPUB 3，`dc:identifier` 为 `urn:uuid:` 加上由项目 id 与导出时间生成的 uuid5；包含 `dc:title`、`dc:creator`、`dc:language=zh-CN`、`dcterms:modified`、`dc:subject`（每个类型标签一个）、`dc:description`（简介）。manifest 列出所有 xhtml、`nav.xhtml`（`properties="nav"`）、`toc.ncx` 和 `style.css`；spine 的顺序是扉页 → [简介页] → 每卷依次为卷标题页和该卷各章，`toc="ncx"`。
  4. `OEBPS/nav.xhtml`（EPUB 3 目录）和 `OEBPS/toc.ncx`（EPUB 2 兼容），目录都是"卷 → 章"两级；没有分卷时只有一层。
  5. `OEBPS/title.xhtml`、`synopsis.xhtml`、`vol-N.xhtml`、`ch-N.xhtml`、`style.css`。
- **分卷**：章节通过 `outline_id` 找到所属大纲的 `volume_no` 和 `volume_title`。没有关联大纲的章节，归入它前一个有卷的章节所在的卷；整本书都没有卷信息时，不生成卷标题页。
- **正文转换**：每一段（按空行或换行切分）生成一个 `<p>`；全文做 XML 转义，并去掉 XML 1.0 不允许出现的控制字符。章节标题为 `<h2>`。
- **CSS**：`p { text-indent: 2em; line-height: 1.6; margin: 0 0 .4em; }`；标题居中。字体交给阅读器决定。
- 只导出章节正文，不导出分析、伏笔等附加信息。章节正文为空时，可以选择跳过，默认跳过，并在响应头 `X-Export-Warnings` 中注明。

## 6. 前端

- **项目概览页 / 项目菜单**：
  - "导出"弹窗增加 EPUB 选项，选中后显示作者名和"包含简介页"两项；
  - 新增"备份"弹窗：四个可选项（默认只勾选章节版本），显示估算大小，点击后直接下载。
- **项目列表页**："导入备份"按钮打开弹窗：
  1. 选择文件后调用 preview；
  2. 显示统计表格、版本信息、警告和错误；
  3. 可以修改书名；
  4. 点击"导入"后跳转到新项目。
- `types.ts`；`api/transfer.ts`（下载沿用现有导出的 blob 下载工具）；`utils/transfer.ts`（统计表格的标签、大小格式化）。
- reader-ui 完成后，书架卡片上也会放上导出和备份入口（在那个任务里做）。

## 7. 取舍

- **按 id 映射，而不是按名称**：同名角色、同名章节不会冲突，引用关系完整。代价是需要维护一份表规则清单，由反射测试保证没有遗漏。
- **整份 JSON，不打 zip**：可读、容易排查问题。100MB 的上限对文字数据足够；体积主要来自生成记录，已设为默认不勾选。
- **导入时由数据库分配新 id**：多次导入同一份备份互不干扰，也不需要校正序列。
- **EPUB 不引入第三方库**：格式是固定的几个文件，手写更可控。

## 8. 兼容与回滚

- 不需要迁移，只新增接口和服务。
- 备份格式带版本号；以后新增表时，在注册表里登记、提升 `version`，并在 `migrate` 中补上旧版本的升级步骤。
