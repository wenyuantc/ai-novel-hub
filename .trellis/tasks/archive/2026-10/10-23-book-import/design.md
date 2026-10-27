# 技术设计：拆书导入

## 1. 依赖与模块

不引入新依赖。新增和改动的模块如下：

- `app/services/book_import/`
  - `splitter.py`：纯函数，包括清洗后的切章、目录区识别、兜底切块和校正操作，不依赖数据库。
  - `service.py`：解析记录的增删查、校正、预估、开始导入、过期清理。
  - `reverse.py`：`book.import` 任务的执行逻辑，包括装批、通读、合并、汇总、写入、风格提取和分析入队。
  - `merge.py`：纯函数，合并各批反推结果（角色别名合并、各类实体去重）。
- `app/services/task_engine/handlers/book_import.py`：任务处理器。
- `app/api/v1/book_imports.py`、`app/schemas/book_import.py`。
- `kb_parser` 的改动：
  - `parse(..., max_chars=MAX_TEXT_CHARS, truncate=True)` 增加参数。拆书调用时传 `max_chars=5_000_000, truncate=False`，超出直接抛 400。
  - 抽出 `epub_documents(data) -> tuple[str | None, list[str]]`，返回 OPF 里的 `dc:title` 和书脊中每个文档的文字。`_parse_epub` 改为调用它，行为不变。

## 2. 数据（迁移 `0017_book_imports`）

开工时先用 `ls backend/alembic/versions` 确认编号。封面任务可能会抢先占用编号，冲突时按实际顺序递增。

`book_imports` 表：

| 字段 | 说明 |
|---|---|
| `id` | PK |
| `user_id` | FK users，CASCADE，index |
| `filename` | String 255 |
| `kind` | txt / md / docx / epub |
| `size`、`sha256` | 文件信息，只做展示，不去重 |
| `book_title` | 默认书名：epub 元数据，或去掉扩展名的文件名 |
| `text` | Text，清洗后的全文（≤ 500 万字） |
| `split_mode` | `auto` / `regex` |
| `split_pattern` | String 100，可为空 |
| `chapters` | JSON 数组，见下文 |
| `notices` | JSON 数组，切章时产生的提示，例如"已去除目录区 12 行""跳过 3 个空文档" |
| `revision` | int，每次校正加 1 |
| `expires_at` | 创建时间 + 7 天，index |
| 时间戳 | |

章节条目不重复保存正文，只记录指向全文的片段区间，以控制体积：

```json
{"title": "第一章 雨夜", "segments": [[start, end], ...], "volume_no": 1, "volume_title": "第一卷 风起", "source_no": 1}
```

- `segments` 是指向 `text` 的半开区间，按顺序排列。合并两章就是拼接两章的 segments，拼接处在正文里插入 `\n\n`。
- `source_no` 是从标题解析出的原始序号，可为空，只用于"序号断档"提示。
- 字数在读取时计算并缓存在返回对象里，不入库。

项目备份：在 `project_transfer/registry.py` 中把 `book_imports` 登记为与项目无关、不导出的表，让反射测试通过。

## 3. 切章（`splitter.py`）

### 3.1 文本来源

- **txt / md**：使用 `kb_parser.parse` 的 `text`。md 的 `#` 标题行先去掉 `#` 前缀，再参与强规则匹配。
- **docx**：`kb_parser.parse` 已把标题段落转成 `# ...` 行。规则如下：
  1. 有任何标题行匹配强规则时，按强规则切，卷规则同样适用。
  2. 否则统计各标题层级出现的次数，取最常见的层级作为"章"，比它高一级的作为"卷"，再高的层级忽略。
  3. 两种都得不到 ≥ 2 章时，退回纯文本的强规则或兜底切块。
- **epub**：`epub_documents` 返回各文档的文字，用 `\n\n` 拼成全文，同时记录每个文档在全文中的区间。
  - 每个文档默认为一章。
  - 文档内有 ≥ 2 个强规则章标题时，在文档内部再细分。
  - 去掉空白后少于 50 字的文档跳过，并记入 notices。
  - 文档只有一行且匹配卷规则时，视为卷标记。
  - 章标题依次取：文档的第一个 `#` 标题、匹配强规则的第一行、不超过 50 字的第一行。

### 3.2 规则

数字字符集记为 `N = [0-9０-９零〇一二两三四五六七八九十百千万]`，均按行匹配（去掉首尾空白）：

- **卷**：`^第N+[卷部集](?:[\s:：·、.\-—]+(.*))?$`，且行长 ≤ 50。
- **章**：`^第N+[章回节](?:[\s:：·、.\-—]*(.*))?$`，且行长 ≤ 50。
- **英文**：`^(?:chapter|chap\.?)\s*\d+\b.*$`（忽略大小写）。
- **特殊**：`^(?:序章|序言|楔子|引子|前言|尾声|后记|终章|番外)(?:[\s:：·、.\-—].*)?$`，番外后可直接跟内容，如"番外一 xxx"。
- **同一行既有卷又有章**（如"第一卷 第一章 雨夜"）：先开卷，再开章。
- **章节正文**：从标题下一行开始，到下一个章或卷标题为止。
- **序章**：第一个章标题之前的正文去掉空白后 ≥ 200 字时，保留为一章，标题为"序章"；否则丢弃。如果这段内容位于第一个卷标题之前，就不归属任何卷。
- **目录区**：从开头起连续 ≥ 5 个章标题，且每个标题之间去掉空白后的正文 < 50 字，这一段整体丢弃，并记入 notices。
- **兜底切块**：一个章标题都没有时，从当前位置累计到 3000 字后，在 5000 字之内的第一个句末（`。！？!?…` 或换行）断开；找不到句末时在 5000 字处硬切。标题依次为"第 N 段"。
- **自定义正则**：
  - 长度 ≤ 100，必须能 `re.compile`。
  - 拒绝嵌套量词：静态检查形如 `(...[+*]...)[+*{]` 的结构。
  - 只匹配长度 ≤ 60 的行。
  - 在独立子进程中执行（`multiprocessing`，spawn 方式），5 秒超时就终止子进程，并返回 400"正则执行超时"。
  - 匹配到的行作为章标题，不再识别卷，序章和兜底规则保留。
  - 0 个匹配时返回 400"没有匹配到任何标题"。

### 3.3 校正操作

`apply_op(text, chapters, op) -> chapters` 是纯函数，越界或非法时抛 `BadRequestError`：

| 操作 | 参数 | 行为 |
|---|---|---|
| `rename` | `index`、`title`（1–200 字） | 改标题 |
| `merge_next` | `index` | 与下一章合并，保留本章的标题和分卷 |
| `split` | `index`、`line`（章内行号，从 1 开始，不能是第 1 行） | 从该行起拆出新章，新章标题取该行（截断到 200 字），该行本身仍保留在正文里 |
| `delete` | `index` | 删除；只剩 1 章时拒绝 |
| `start_volume` | `index`、`title` | 本章起新开一卷，直到下一个卷起点为止；卷号按出现顺序重排 |
| `rename_volume` | `index`、`title` | 修改本章所在卷的卷名 |
| `clear_volume` | `index` | 本章所在卷并入上一卷；如果是第一卷，就变为"无分卷" |

规则：
- 没有任何分卷时，`volume_no = 1`，`volume_title = ""`。
- 每次操作后重排卷号。

### 3.4 提示（读取时计算）

- `short`：< 300 字。
- `long`：> 20000 字。
- `dup_title`：去掉序号前缀后的标题与其他章完全相同，且非空。
- `gap`：相邻两章 `source_no` 都不为空，且后者不等于前者加 1。
- `too_many`：总章数 > 3000，这是全局提示，对应的导入范围必须缩小。

## 4. API（前缀 `/api/v1/book-imports`，均要求登录，只能访问自己的记录，否则 404）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `` | multipart `file`。先清理过期记录，再检查"未导入 ≤ 5"（否则 409），然后解析并切章，返回 `BookImportOut` |
| GET | `` | 未过期的解析列表 `BookImportBrief[]`（id、filename、book_title、章节数、字数、expires_at、updated_at） |
| GET | `/{id}` | `BookImportOut`：基本信息、revision、notices，以及 `chapters[]`（index、title、volume_no、volume_title、chars、first_line ≤ 60 字、warnings[]）和 `warning_counts` |
| GET | `/{id}/chapters/{index}` | `{title, lines: string[]}`，单章全文按行切分，用于预览和选择拆分行 |
| POST | `/{id}/resplit` | `{mode, pattern?, revision}`，重切，清空校正 |
| POST | `/{id}/ops` | `{op, index, title?, line?, revision}`，revision 不一致时返回 409 |
| POST | `/{id}/estimate` | `{import_range, reverse_range, extract_style, deep_analyze}`，返回 `{import_chapters, import_chars, reverse_chars, batches, digest_input, synthesize_input, style_input, analyze_input, total_input}` |
| POST | `/{id}/start` | `{revision, title, genre, import_range, reverse_range, extract_style, deep_analyze, ai_config_id?}`，返回 `{project_id, task: TaskOut}` |
| DELETE | `/{id}` | 删除解析记录 |
| POST | `/tasks/{task_id}/skip` | 只有暂停原因为 `failed` 的 `book.import` 任务可用：跳过当前步骤，然后续跑，返回 `TaskOut` |

补充约定：
- 范围用 `[start, end]` 表示，均为 1 起的章节 index 且包含两端。`reverse_range` 必须落在 `import_range` 之内；导入章节数必须 ≤ 3000。
- 在 `main.py` 的 `UploadLimitMiddleware` 中新增规则：`POST ^/api/v1/book-imports$` 的上限为 `kb_parser.MAX_FILE_BYTES + MULTIPART_OVERHEAD`。
- 解析和切章是 CPU 密集操作，用 `run_in_threadpool` 执行；自定义正则走子进程。

### 4.1 预估

- `digest_input`：批数 × 模板开销（2000 字）+ 反推范围的字数 + 已知角色名单估算（批数 × 1500 字）。
- `synthesize_input`：40000 字（上限）。
- `style_input`：6000 字。
- `analyze_input`：导入章节字数 + 章节数 × 3000 字（上下文开销）。
- 前端说明这只是"预估输入字数"，不换算成费用。

### 4.2 开始导入（同一事务）

1. 锁定解析记录：`SELECT ... FOR UPDATE`，SQLite 下忽略。校验 revision、各个范围和 AI 配置。
2. 检查该用户没有处于排队、运行中或暂停的 `book.import` 任务，否则返回 409。
3. 新建项目：
   - `title`、`genre` 来自请求，`status = "writing"`，`target_words` 为空。
   - `wizard_progress = {"done": ["outline", "world", "characters", "careers"], "skipped": [], "current": null, "imported": true}`，避免概览一直提示向导未完成。步骤名以 `wizard_service` 中实际使用的为准。
4. 按导入范围逐章重新编号为 1..N：
   - 写入 `outlines`：`volume_no`、`volume_title`、`chapter_no`、`title`，`summary` 为空。
   - 写入 `chapters`：`outline_id` 关联大纲，状态为 `done`，`word_count` 用 `count_words` 计算。
5. 为每章建一个来源为 `import` 的初始版本：`VersionSource` 增加 `"import"`，前端补上对应的标签文案。
6. 调用 `task_service.create_task(db, engine, user_id, "book.import", params)`，由它在同一个会话里提交。`params` 为：

   ```json
   {"project_id": ..., "reverse_chapter_nos": [a, b], "extract_style": bool, "deep_analyze": bool, "ai_config_id": ...}
   ```

7. 提交前删除解析记录。任何一步失败都整体回滚。

## 5. `book.import` 任务

### 5.1 处理器

- `validate_and_claim`：
  - 校验项目归属和 AI 配置；
  - 检查没有其他处于排队、运行中或暂停的 `book.import`；
  - `resource_key = f"book_import:{user_id}"`，`project_id` 为该项目；
  - 标题为"拆书反推：《书名》"。
- `validate_retry` / `revalidate_resume`：重新校验 AI 配置，沿用原有的断点。
- `finish_result`：在当前汇总的基础上加 `ended_early: true`。
- 不需要 `on_abort` 回退：已写入的数据全部保留。
- `progress`：
  - 通读阶段占 0–80，按已完成批数 / 总批数计算；
  - 汇总占 85，写入占 90，风格占 95。

### 5.2 断点结构（`tasks.checkpoint`）

```json
{
  "phase": "digest" | "synthesize" | "apply" | "style" | "analyze" | "done",
  "batches": [{"parts": [[chapter_id, part_idx, part_total]], "state": "pending|done|skipped", "error": ""}],
  "known": {"names": [...], "aliases": {...}},
  "findings": {"characters": [...], "organizations": [...], "world": [...], "careers": [...], "relations": [...]},
  "final": null,
  "stats": {"summaries": 0, "characters": 0, "organizations": 0, "world": 0, "careers": 0, "relations": 0, "style_id": null, "analyses_queued": 0},
  "skipped": [{"stage": "digest", "chapter_nos": [3, 4]}],
  "warnings": [],
  "pause_reason": null
}
```

- `step_cursor` 指向当前批的下标。
- 体积控制：
  - `findings` 中每类最多保留 300 条，超出时按出现次数丢弃最低的；
  - 每条记录的文本字段截断到 300 字；
  - 5 万字以上的书大约 250 批，断点约 1MB，可以接受。
- 装批：
  - 在 `build_job` 首次运行时（`batches` 为空）完成，同时把单章超过 2 万字的章节按句末切成多段（每段 ≤ 2 万字）。
  - 装批只取章节 id 和分段编号，正文在执行时按 id 读取，所以执行期间用户编辑正文以最新内容为准。
  - 读取时章节已被删除，就跳过这段并记一条警告。

### 5.3 通读（`book_import.digest`）

- 变量：
  - `project_info`：书名和主题材；
  - `known_characters`：已知角色名单，格式为"姓名（别名…）"，按出现次数取前 200 个，≤ 3000 字；
  - `chapters_text`：本批每段都以 `【第N章 标题】` 开头，分段的写成 `【第N章 标题（第k/m段）】`；
  - `json_rule`、`output_format`（锁定）。
- 输出：

  ```json
  {"chapters": [{"chapter_no", "summary"}], "characters": [{"name", "aliases": [], "identity", "personality", "appearance", "role"}], "organizations": [{"name", "description", "members": [{"name", "position"}]}], "world": [{"category", "title", "content"}], "careers": [{"name", "type", "description", "stages": [{"name", "description"}]}], "relations": [{"a", "b", "type", "description"}]}
  ```

  `world.category` 复用 `wizard_service.normalize_world_category`。
- 摘要写入：
  - 对应大纲的 `summary` 为空时才写，截断到 150 字；
  - 分段章节的摘要先缓存在断点里，所有段都完成后用"；"拼接再写入，最多 150 字；
  - 摘要和断点在同一事务中提交。
- 每批完成后用 `merge.accumulate` 把本批结果并入 `findings` 和 `known`，然后提交断点并前进 `step_cursor`。

### 5.4 合并（`merge.py`）

- **角色**：
  - 姓名规范化：去空白和全角空格，统一大小写，去掉"·"两侧空格。
  - 用并查集合并：两个条目的姓名或任一别名相同即视为同一角色。
  - 显示名取出现次数最多的姓名；别名取并集，排除显示名。
  - 字段取最长的非空值；`count` 记录出现批数。
- **组织、职业**：按规范化名称去重。职业阶段按阶段名去重，保持首次出现的顺序。
- **世界观**：按 `(category, title)` 去重，内容取最长的一条。
- **关系**：按无序的 `(a, b)` 去重，类型和描述保留首条，双方姓名映射到角色的显示名。

### 5.5 汇总（`book_import.synthesize`）

- 变量：
  - `project_info`；
  - `summaries`：已写入的章节摘要，按章号排列；超过 2 万字时等距抽样，并保留首尾各 10 章；
  - `characters`：前 60 个；
  - `organizations`、`world`、`careers`、`relations`：都以紧凑行文本表示，合计 ≤ 2 万字；
  - `json_rule`、`output_format`。
- 输出：
  - `project`：`{synopsis, theme, tags[], selling_points, audience, genre}`；
  - `characters`、`organizations`、`world`、`careers`、`relations`，结构同通读输出，角色另加 `role_type`（主角、配角、反派等，取值按 `Character` 已有的字段和枚举）。
- 长度限制按项目字段的已有约束截断：简介 2000、主题 200、标签最多 8 个且每个 ≤ 10 字、卖点 500、目标读者 100。
- 汇总被跳过时，`final` 由代码生成：取合并结果，角色按出现次数取前 60 个，项目字段留空。

### 5.6 写入（`apply` 阶段，单个事务）

- **项目字段**：只写空值。项目的 `genre` 为空时才写入 `final.project.genre`。
- **角色**：
  - 项目中已有角色的姓名或别名（如果有别名字段）与条目的姓名或别名相同时跳过；
  - 否则新建，字段映射到 `Character` 已有的列，别名写进介绍或备注字段。实现时先读取模型确认字段。
- **组织**：同名跳过；新建后，成员中能对应到角色的写入 `OrganizationMember`。
- **职业**：同名跳过；新建职业和阶段。
- **世界观**：同标题跳过。
- **关系**：
  - 双方都要能对应到角色，且两人之间还没有关系才写入；
  - 类型通过 `relationship_service` 现有的"按名称查找或创建类型"能力解析；如果没有这个能力，就归入通用类型，并把原类型写进描述。
- 写入后，涉及的实体按各自模块的约定维护检索单元或记忆，与手动新建一致。
- 结果计入 `stats`。

### 5.7 风格（`style` 阶段）

- 从反推范围中抽取样章：第 1、中间、最后各一章，每章取前 2000 字，合计 ≤ 6000 字。
- 复用 `build_style_extract_messages` 和 `normalize_draft`，模板沿用 `style.extract`，同样支持用户覆盖。
- 新建 `WritingStyle`，归属当前用户，名称为"《书名》原文风格"，重名时依次加"（2）""（3）"；名称长度按列限制截断。
- 项目的 `default_style_id` 为空时设为这个风格。
- 失败（重试后仍失败）时只在 `warnings` 中记一条，并继续后续阶段。

### 5.8 深度分析入队（`analyze` 阶段）

- 只在 `deep_analyze` 为真时执行：为项目中正文非空的全部章节排队 `chapter.analyze`。
- 复用 `analysis_service.analyze_pending` 的逻辑，抽出可以在任务上下文中调用的函数；参数取默认的 `AnalyzeRequest`，加上 `ai_config_id`。
- 记录 `analyses_queued`。这一步在 apply 之后执行，所以角色已经存在。

### 5.9 重试、暂停与跳过

- 每次 AI 调用失败（提供方错误或 JSON 解析失败）后，先自动重试 2 次，退避等待 2 秒和 5 秒，沿用 batch 的常量和工具。仍然失败时：
  - 记录 `pause_reason = {"reason": "failed", "phase", "batch_index", "error"}`；
  - 产出 `paused` 事件，载荷为 summarize 的结果。
- **继续**（`/tasks/{id}/resume`）：从同一阶段、同一批开始重做，并清空 `pause_reason`。
- **跳过**（`POST /book-imports/tasks/{id}/skip`）：
  - 要求任务处于 `paused` 状态，且 `pause_reason.reason == "failed"`，否则返回 409；
  - digest 阶段：把当前批标记为 `skipped`，把章号写进 `skipped`，`step_cursor` 加 1；
  - synthesize 阶段：生成代码合并版 `final`，然后 `phase = "apply"`；
  - 保存后调用 `task_service.resume_task`。
- **结束**：沿用 `/tasks/{id}/finish`。
- **中断**（进程重启）：任务状态变为 `interrupted`。重试时 `validate_retry` 沿用原有断点，与 batch 一致，所以已完成的批次不会重复调用。
- 事件：
  - 阶段文案通过 `stage` 事件发出，例如"通读 第 12/80 批（第 45–52 章）""汇总设定""写入设定""提取写作风格""排队深度分析"；
  - 每批完成时发出自定义事件 `batch_end {index, total, state}`，前端用它刷新进度。

### 5.10 生成记录

每次 AI 调用都通过 `generation_log_service.save_log` 记录：`project_id` 填当前项目，`chapter_id` 填本批第一章（汇总阶段为空），`kind` 填 `book_import.digest` 或 `book_import.synthesize`，`params.prompt` 记模板覆盖信息，做法与其他功能一致。如果 `generation_logs.kind` 有枚举或检查约束，需要同步扩展。

## 6. 提示词模板

- `CATEGORIES` 新增 `"book_import": "拆书"`。
- 新增 `book_import.digest` 和 `book_import.synthesize` 的 system 和 user 模板，变量声明见 5.3 和 5.5；`output_format` 锁定。
- 两个模板没有 parity 基线，需要补：默认模板渲染快照测试、锁定变量测试，以及覆盖后生效的测试。
- `formats.py` 新增 `BOOK_DIGEST_FORMAT` 和 `BOOK_SYNTHESIZE_FORMAT`。

## 7. 清理

- `service.purge_expired(db)`：删除 `expires_at < now()` 的记录。
- 触发时机：上传前、列表查询前，以及应用启动时（lifespan 中，与 `sync_presets` 并列）。
- 不另外引入定时器。

## 8. 前端

- 路由 `/book-import`（懒加载 `BookImportPage`），属于 `/` 布局下的子路由。
- 书架"新建"卡片的菜单里加入"拆书导入"。
- `api/bookImport.ts`、`types.ts` 中的镜像类型。

页面结构（AntD Steps）：

1. **上传**：
   - 拖拽上传区，提示支持的格式和上限；
   - 下方是"未完成的解析"列表：继续、删除、剩余天数。
2. **校正**：
   - 顶部显示 notices、提示汇总，以及"自定义正则重切"折叠面板（含示例）；
   - 主体是虚拟化的章节列表（3000 行），使用 antd Table 的 `virtual` 和固定行高，分卷用行内标签显示；
   - 每行显示：序号、分卷、标题（可直接编辑）、字数、开头一行、提示标签，以及操作菜单（合并下一章、拆分、删除、本章起新开一卷、改卷名、取消分卷）；
   - 点击行在右侧打开抽屉，显示带行号的全文；点击某一行可以"从此行拆分"；
   - 每次操作的响应会替换整个章节列表，revision 返回 409 时提示并重新拉取；
   - 支持按提示类型筛选。
3. **选项与预估**：
   - 书名、主题材；
   - 导入范围、反推范围（滑块加输入框）；
   - 提取风格、深度分析的勾选，深度分析的默认值按 ≤ 100 章确定；
   - AI 配置选择；
   - 预估卡片，选项变化后经防抖调用 estimate。
4. **开始**：确认后调用 start，成功后跳转到 `/projects/:pid/overview`。

任务面板（现有组件）：
- `book.import` 的标题和阶段直接显示；
- 暂停原因为 `failed` 时，额外提供"跳过本批"按钮（调用 skip），与现有的"继续"和"结束"并列；
- 完成结果展示写入统计、跳过的章节和警告。

移动端：校正列表改为卡片列表，操作收进"更多"菜单，全文抽屉宽度 100%。不能出现页面级横向溢出，遵守 spec 中禁止在流式宽度里用 `100vw` 的规则。

## 9. 兼容与回滚

- 迁移只新增 `book_imports` 表，`downgrade` 时删除它。`VersionSource` 增加的 `import` 是字符串列取值，不涉及数据库约束；如果存在检查约束，需要在迁移里同步修改。
- 回滚时，已导入的项目都是普通项目，可以继续使用。
