# 后端最终契约（供前端，后端已实现）

**以后端代码为准**：`backend/app/schemas/book_import.py`、`backend/app/api/v1/book_imports.py`、`backend/app/services/book_import/reverse.py`。

## API（前缀 `/api/v1/book-imports`）

所有 index 和范围都从 1 起、包含两端，`Range = [int, int]`。访问他人记录或记录不存在时返回 404「解析记录不存在」。

| 方法 | 路径 | 请求 | 响应 |
|---|---|---|---|
| POST | `` | multipart `file` | 201 `BookImportOut` |
| GET | `` | — | `BookImportBrief[]` |
| GET | `/{id}` | — | `BookImportOut` |
| GET | `/{id}/chapters/{index}` | — | `{index, title, lines: str[]}` |
| POST | `/{id}/resplit` | `{mode: "auto"\|"regex", pattern?: str≤100, revision}` | `BookImportOut` |
| POST | `/{id}/ops` | `{op, index≥1, title?≤200, line?≥1, revision}` | `BookImportOut` |
| POST | `/{id}/estimate` | `{import_range, reverse_range, extract_style=true, deep_analyze=false}` | `EstimateOut` |
| POST | `/{id}/start` | `{revision, title: 1–200, genre="" ≤100, import_range, reverse_range, extract_style=true, deep_analyze=false, ai_config_id?: int\|null}` | 202 `{project_id, task: TaskOut}` |
| DELETE | `/{id}` | — | 204 |
| POST | `/tasks/{task_id}/skip` | — | 202 `TaskOut` |

`op` 的取值和必填参数：

| `op` | 额外必填 |
|---|---|
| `rename`、`start_volume`、`rename_volume` | `title` |
| `split` | `line`，取 `chapters/{index}` 返回的 `lines` 中的下标加 1，不能是第 1 行 |
| `merge_next`、`delete`、`clear_volume` | 无 |

## 数据结构

- `BookImportBrief`：`{id, filename, book_title, chapter_count, total_chars, expires_at, updated_at}`
- `BookImportOut`：`{id, filename, kind: "txt"|"md"|"docx"|"epub", size, book_title, split_mode: "auto"|"regex", split_pattern: str|null, revision, notices: str[], chapters: BookChapterView[], warning_counts: {short, long, dup_title, gap}, chapter_count, total_chars, too_many: bool, max_import_chapters: 3000, expires_at, created_at, updated_at}`
- `BookChapterView`：`{index, title, volume_no: int, volume_title: str, source_no: int|null, chars, first_line: str(≤60), warnings: ("short"|"long"|"dup_title"|"gap")[]}`
  - `volume_no = 0` 表示不属于任何卷（位于第一个卷之前）。
  - 完全没有分卷时，所有章节的 `volume_no` 都是 1，卷名为空。
- `EstimateOut`：`{import_chapters, import_chars, reverse_chars, batches, digest_input, synthesize_input, style_input, analyze_input, total_input}`，单位为字符。

## 错误

- **409**
  - 修订号过期：「解析结果已被修改，请刷新后重试」。
  - 未导入的解析超过 5 个。
  - 已有拆书任务处于排队、运行或暂停状态：「已有进行中或待处理的拆书任务…」。
  - 跳过一个并非因失败而暂停的任务。
- **400**
  - 文件：类型不支持、超过 20MB、超过 500 万字。
  - 正则：过长、含嵌套量词、语法不合法、超时、没有匹配到任何标题。
  - 范围：导入范围不合法、超过 3000 章、反推范围不在导入范围内。
  - 书名为空。

## `book.import` 任务

- 标题为「拆书反推：《书名》」。
- `TaskOut.checkpoint`、`result` 和 `paused` 事件的载荷是同一个摘要结构：

  ```
  {phase: "digest"|"synthesize"|"apply"|"style"|"analyze"|"done",
   batches_total, batches_done, batches_skipped,
   stats: {summaries, characters, organizations, world, careers, relations, analyses_queued,
           style_id: int|null, project_fields: str[]},
   skipped: [{stage: "digest", batch_index, chapter_nos: int[]} | {stage: "synthesize", chapter_nos: []}],
   warnings: str[],
   pause_reason: {reason: "failed", phase: "digest"|"synthesize", batch_index: int|null, error: str} | null}
  ```

- `/finish` 之后，result 额外带 `ended_early: true`。
- 「跳过本批」按钮的显示条件：`status == "paused" && checkpoint.pause_reason?.reason == "failed"`。
- 事件：
  - `batch_end`：`{index (从 0 起), total, state: "done"}`。
  - 其余沿用：`reset`、`delta`、`paused`、`done`、`error`。
- 阶段文案由 `stage` 给出。
- 进度：通读阶段 0–80，汇总 85，写入 90，风格和分析 95，完成 100。

## 需要补的前端标签

- 版本来源新增 `import`，显示为「导入」。
- 生成记录新增三种 kind：
  - `book_import.digest`：拆书通读
  - `book_import.synthesize`：拆书汇总
  - `book_import.style`：拆书风格提取
- 任务类型新增 `book.import`，显示为「拆书反推」。
