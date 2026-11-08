# 阅读器（阅读进度 / 目录 / 书架字段）

> 来源任务：`10-27-reader-ui`（迁移 `0016`）。备份登记见 [project-transfer.md](./project-transfer.md)，表结构见 [database-guidelines.md](./database-guidelines.md)。

## 场景：阅读进度与书架字段

### 1. 范围 / 触发条件

`projects.reading_progress`、`app/services/reader_service.py`、`project_service._last_edited` / `_reading` / `parse_progress`，
以及任何改动 `ProjectOut` 的 `last_edited_chapter_id` / `reading` 的代码。

### 2. 签名

```python
# projects.reading_progress: JSON，默认 {}；结构 {chapter_id, chapter_no, ratio: 0–1, updated_at: "…Z"}
PUT  /api/v1/projects/{pid}/reading-progress   body ReadingProgressIn {chapter_id: int, ratio: float ∈ [0, 1]} → 204
GET  /api/v1/projects/{pid}/reader/toc         → list[TocItem {id, chapter_no, title, word_count, volume_no: int|null, volume_title: str|null}]
GET  /api/v1/projects/{pid}/reader/chapters/{cid} → ReaderChapter {chapter: ChapterOut, blocks: list[ReaderBlock] | null}
ProjectOut.last_edited_chapter_id: int | None
ProjectOut.reading: ReadingOut {chapter_id, chapter_no, ratio} | None
project_service.WRITTEN = Chapter.word_count > 0   # 「已写章节」
```

### 3. 契约

- **已写章节** = `word_count > 0`（正文有非空白字符）。目录、最近编辑、进度兜底都用同一个条件。
- PUT：项目归属不符 → 404；章节不存在或不属于该项目 → 404 `章节不存在`；`ratio` 越界 / 非数字 / NaN → 422。
  整体替换进度，同时记下章节当前 `chapter_no` 与 UTC `updated_at`。用 Core `UPDATE … SET updated_at = updated_at`
  写入，**不改项目的 `updated_at`**（书架「最近更新」排序不因阅读变化）。可以指向空章节（读取时兜底）。
- `reading`（读时兜底，删章节时不联动）：进度章节存在、属于本项目且已写 → 原样返回；否则以（章号, id）为界，
  取排在它前面的最近一个已写章节，`ratio = 0`。章节已删除时用进度里记录的 `chapter_no` 与旧 id 作界；没有记录 → null。
  前面没有已写章节 → null。结构不对的旧数据按无进度处理，不报错。
- `last_edited_chapter_id`：已写章节中 `updated_at` 最大（同时间取 id 大）的一个（`row_number() over (partition by project_id)`）。
- 列表与详情都返回这两个字段；`list_projects` 查询次数固定（项目 + 统计 + 风格 1 次、最近编辑 1 次、进度至多 2 次），不随项目数增长。
- 目录只含已写章节，按 `(chapter_no, id)` 排序；卷信息经 `outline_id` 取大纲 `volume_no` / `volume_title`，没有关联大纲时都为 null。侧边栏复用分析接口。
- 阅读章接口以一致快照返回 `Chapter.content` 和仅面向读者的块／行内格式；无格式时 `blocks=null`，避免长纯文本膨胀。作者区块、区块标题和标记不出现在阅读载荷中。正文由 Scene 有序投影维护；排序、拆并、恢复和 AI 保存必须同步投影与字数。Reader 不在客户端重新拼场景、不跳过零字／空白景，不把摘要插入正文。TXT／Markdown／EPUB 和开放章正文沿用同一兼容投影；Markdown／EPUB 可包含读者格式，进度仍为章 ID＋ratio。见[场景合同](./scenes.md)、[富文档合同](./rich-documents.md)。
- 备份：`reading_progress` 登记在 `PROJECT_RULE.json_refs`（`remap.reading_progress`），见 project-transfer.md。

### 4. 必需的测试

`tests/test_reader.py`：写入 / 读取与校验（边界 0 / 1、越界 422、他项目章节 404、不改 `updated_at`）、删章 / 空章兜底与 null、
`last_edited_chapter_id` 只看已写章节、目录只含已写章节并带卷信息、跨用户 404、项目列表 SQL 计数恒定、备份往返映射与对应不上时清空并警告；
`tests/test_migrations.py::test_0016_reading_progress`；`tests/test_rich_document_render.py` 覆盖一致快照、公共格式和私有作者资料隔离。

### 5. 错误写法

```python
project.reading_progress = {...}; await db.commit()   # ORM 赋值会触发 onupdate，项目「最近更新」被阅读刷新
for p in projects: await _reading(db, [p])            # N+1：必须批量传入
```
