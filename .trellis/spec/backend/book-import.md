# 拆书导入（`book_imports` / `book.import`）

> 来源任务：`10-23-book-import`（迁移 `0017`）。断点 / 暂停 / 续跑沿用 [batch-generate.md](./batch-generate.md) 的模式，
> 任务表与状态集合见 [database-guidelines.md](./database-guidelines.md)「后台任务表」，上传解码与上传限制见 [retrieval.md](./retrieval.md)，
> 模板注册与覆盖见 [prompts-styles.md](./prompts-styles.md)，表规则登记见 [project-transfer.md](./project-transfer.md)。

---

## 场景：上传解析、切章与人工校正

### 1. 范围 / 触发条件

`app/services/book_import/splitter.py`（纯函数，不依赖数据库）、`regex_worker.py`（spawn 子进程入口，只依赖标准库）、
`service.py`（解析记录的增删查、校正、预估、开始导入、过期清理）、`api/v1/book_imports.py`、`schemas/book_import.py`。
修改切章规则、校正操作、提示规则、上传限制或开始导入事务时必读。

### 2. 签名

```python
# splitter.py
MAX_TEXT_CHARS = 5_000_000; MAX_IMPORT_CHAPTERS = 3000; HEADING_MAX = 50; REGEX_LINE_MAX = 60; REGEX_MAX_LEN = 100
REGEX_TIMEOUT_SECONDS = 5.0; REGEX_MAX_WORKERS = 2; PROLOGUE_MIN = 200; TOC_MIN_HEADINGS = 5; TOC_GAP_MAX = 50
CHUNK_MIN = 3000; CHUNK_MAX = 5000; EPUB_DOC_MIN = 50; SHORT_CHARS = 300; LONG_CHARS = 20000
def parse_book(filename, data) -> ParsedBook          # 解码 + 清洗 + 自动切章；> 500 万字 400
def split_auto(kind, text, doc_spans=None) -> (chapters, notices)
def split_regex(kind, text, pattern, *, timeout_seconds=None) -> (chapters, notices)
def apply_op(text, chapters, op, index, *, title=None, line=None) -> chapters   # 越界 / 非法 → BadRequestError
def chapter_views(text, chapters) -> (views, warning_counts)
# service.py
MAX_PENDING = 5; TTL = 7 天; STALE = "解析结果已被修改，请刷新后重试"; STALE_CODE = "stale_revision"
async def upload / list_imports / get_import / chapter_text / resplit / apply_op / estimate / start / delete_import
async def purge_expired(db) / purge_expired_all(sessionmaker)   # 上传前、列表前、应用启动（lifespan）
```

章节条目（`book_imports.chapters` JSON）：`{title, segments: [[start, end], ...], volume_no, volume_title, source_no}`。
`segments` 是指向 `text` 的半开区间，正文不重复存；合并 = 拼接 segments，拼接处在正文里插入 `"\n\n"`（`body_text`）。
字数与提示在读取时计算（`chapter_views`，放到线程里），不入库。

### 3. 契约

- **上传**：`POST /book-imports` multipart `file`；`UploadLimitMiddleware` 规则 `POST ^/api/v1/book-imports$`，上限
  `kb_parser.MAX_FILE_BYTES + MULTIPART_OVERHEAD`，在 multipart 解析前拦截（路由再 `read(MAX + 1)` 兜底）。
  顺序：清理过期 → 未导入 ≥ 5 → 409 → 线程中 `parse_book` → 入库（`revision = 0`、`expires_at = now + 7 天`）→ 201。
  txt / md / docx 走 `kb_parser.parse(max_chars=5_000_000, truncate=False)`；epub 走 `kb_parser.epub_documents`（书名取 `dc:title`，
  每个书脊文档的区间记入 `doc_spans`，自动重切时要用）。编码：UTF-8 / UTF-16 BOM → 直接解；否则 GB18030 与 Big5 都试，
  两者都能解时按常用汉字命中率取高的（`kb_parser._han_score`）。
- **强规则**（标题行去首尾空白后 ≤ 50 字，不用「短行 + 前后空行」弱规则）：
  - 卷 `^第N+[卷部集]`（后面要有分隔符或直接结束）；同一行「第一卷 第一章 雨夜」先开卷再开章。
  - 章 `^第N+[章回节]`、`Chapter N` / `Chap. N`、`序章|序言|楔子|引子|前言|尾声|后记|终章`、`番外…`（不含句末标点）。
  - 以 `。，；,;` 结尾、或序号后**没有分隔符**直接接带标点的句子（「第一节课下课后，……」）不是标题。
- **序章**：第一个标题之前的正文去空白后 **≥ 200 字**保留为「序章」（位于第一个卷之前时 `volume_no = 0`），否则丢弃。
  卷标题与本卷第一章之间的卷首语同样 ≥ 200 字才单独成章。
- **目录区**：从第一个标题起连续、标题之间正文去空白后 **< 50 字**的一段里，章标题 **≥ 5 个**时整段丢弃，notices 记
  `已去除目录区 N 行`；紧挨正文第一章的卷标题属于正文。所有标题都几乎没有正文时不当作目录（保持原样，按「过短」提示）。
  **目录后紧跟引言**：上述一段之后的第一个章标题若在后文再次出现（目录复述正文标题），它也属于目录区，其后到下一个标题之间的文字
  作为序章候选（≥ 200 字保留为「序章」），优先于第一个标题之前的文字。否则引言会成为「目录最后一项」的正文，序章丢失且标题重复。
- **兜底**：没有章标题 → 3000 字后在 5000 字以内的第一个句末（`。！？!?…` 或换行）断开，找不到在 5000 字硬切，标题「第 N 段」。
- **docx / md**：`#` 标题行去掉 `#` 再匹配；`#` 行里没有强规则章标题时取最常见的标题层级为章（次数相同取更深）、高一级为卷，
  不足 2 章退回强规则 / 兜底。
- **epub**：每个书脊文档一章；文档内 ≥ 2 个强规则章标题再细分（全部几乎没正文 → 目录文档跳过）；去空白 < 50 字的文档跳过
  （notices 计数）；只有一行且是卷标题的文档 = 卷标记；章标题依次取第一个 `#` 标题、第一个强规则行、第一个 ≤ 50 字的行。
- **自定义正则**（`resplit {mode: "regex", pattern}`）：≤ 100 字符、`re.compile` 通过、静态拒绝嵌套量词（`(…+…)+` 等），
  只匹配 ≤ 60 字的行；在 **spawn 子进程**中执行（`regex_worker.run`，经管道回传），超时 5 秒 → 400「正则执行超时」；
  不论成功、超时还是出错，`finally` 里 `_reap` 回收：`terminate` → `join(1)` → 仍存活则 `kill` → `join(1)` → `close()`，
  不留僵尸进程；同时最多 `REGEX_MAX_WORKERS` 个子进程（`BoundedSemaphore`）。匹配行作为章标题、不识别卷，序章 / 目录区规则照常；
  0 个匹配 → 400。重切清空全部手工校正（前端须二次确认）。
- **分卷表示**：`volume_no` 连续相同的章节属于同一卷；有卷名的卷从 1 起编号，第一个卷之前无卷名的章节为 0；全书一个卷名都没有时
  全部 `volume_no = 1`、`volume_title = ""`。每次切章 / 校正后经 `normalize_volumes` 重排。
- **校正操作**（`POST /{id}/ops {op, index(1 起), title?, line?, revision}`，响应替换整个章节列表）：

  | op | 行为 / 400 条件 |
  |---|---|
  | `rename` | 改标题（1–200 字），重算 `source_no` |
  | `merge_next` | 拼接下一章的 segments，保留本章标题与分卷；最后一章 400 |
  | `split` | `line` 为 `chapters/{index}.lines` 下标 + 1；第 1 行 / 空行 / 越界 400；该行起成新章，标题取该行（≤200），该行仍留在正文 |
  | `delete` | 只剩 1 章 400 |
  | `start_volume` | 本章到所在卷末尾归入新卷（卷名 1–200） |
  | `rename_volume` / `clear_volume` | 不在任何卷中 400；取消分卷 = 并入上一卷（第一卷 → 无分卷） |

- **提示**（读取时算）：`short` < 300 字、`long` > 20000 字、`dup_title` 去掉序号前缀后与其他章相同且非空、
  `gap` 相邻两章 `source_no` 都有且后者 ≠ 前者 + 1（新卷从 1 重新开始不算）；`too_many` = 总章数 > 3000（全局）。
- **修订号（乐观并发）**：`resplit` / `ops` / `start` 都带 `revision`。先比对一次（快速失败），写入时再用条件
  `UPDATE … WHERE id = ? AND revision = ?`（`revision + 1`），`rowcount != 1` → 409。**修订号过期的 409 一律
  `ConflictError(STALE, extra={"code": "stale_revision"})`**，响应体 `{"detail": "解析结果已被修改，请刷新后重试", "code": "stale_revision"}`；
  其他 409（解析满 5 个、已有拆书任务、跳过条件不满足）**不带** `code`。前端只按 `code` 判断是否重新拉取，不匹配文案。
- **预估**（`POST /{id}/estimate`，不写库）：`digest = 批数 × (2000 + 1500) + 反推字数`、`synthesize = 40000`（有批次时）、
  `style = 6000`（勾选时）、`analyze = 导入字数 + 章数 × 3000`（勾选时）、`total` 为四项和。批数用与任务相同的
  `reverse.split_parts` + `plan_batches` 计算。
- **范围**：`[start, end]` 从 1 起两端包含；`1 ≤ a ≤ b ≤ 总章数`（否则 400 `导入范围不正确`）、`b - a + 1 ≤ 3000`、
  `a ≤ c ≤ d ≤ b`（否则 400 `反推范围必须在导入范围之内`）。
- **开始导入（单事务）**：`get_owned_import(lock=True)`（PG `FOR UPDATE`）→ 校验 revision / 范围 / 书名 → 已有未结束的
  `book.import`（queued / running / paused）→ 409 → 线程中拼正文与字数 → 新建项目（`status = writing`、
  `wizard_progress.done = [world, careers, characters, outline]`）→ 按导入范围**重新编号 1..N** 批量写 `outlines`（`volume_no = max(1, 原值)`、
  摘要为空）与 `chapters`（`status = done`、`word_count` 服务端算）→ `version_service.add_initial(..., "import")` 批量写每章
  `seq = 1` 的导入版本（不逐章查 seq）→ 删除解析记录 → `task_service.create_task("book.import")` 提交。
  任务参数 `reverse_chapter_nos = [c - a + 1, d - a + 1]`（换算为新章号）。任何异常 `rollback` 后上抛，不留残缺项目，解析记录仍在。
- 全部写入都是 `add_all` + 少量 `flush`，**不逐行提交**；3000 章 / 500 万字下 CPU 密集部分（切章、字数、提示、预估、正文拼接）
  都在线程里执行，避免阻塞事件循环。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 他人 / 不存在的解析记录 | 404 `解析记录不存在` |
| 单章全文 index 越界 | 404 `章节不存在` |
| 请求体 > 20MB + 开销（含分块传输） | 400 `kb_parser.TOO_LARGE`（multipart 解析前） |
| 扩展名不支持 / 空文件 / 编码无法识别 / 清洗后 > 500 万字 | 400 |
| 未导入的解析已满 5 个 | 409（无 `code`） |
| 正则为空 / 超长（schema 422）/ 嵌套量词 / 语法错误 / 超时 / 0 个匹配 | 400 |
| 校正参数非法 | 400；`op` 不在枚举 → 422 |
| 修订号过期（resplit / ops / start） | 409 `code = stale_revision` |
| 范围非法 / 超过 3000 章 / 反推不在导入范围内 / 书名为空 | 400 |
| 已有未结束的拆书任务 | 409 `已有进行中或待处理的拆书任务…`（无 `code`） |

### 5. Good / Base / Bad

- Good：两个标签页同时点「合并」，后到的请求 409 + `code`，前端重新拉取并提示，章节没有被合并两次。
- Base：GBK txt 开头有 6 行目录与 250 字前言 → 去掉目录区并提示、前言保留为「序章」。
- Bad：在请求线程里直接 `re.compile(user_pattern).search(...)`——`(a|aa)+$` 一类回溯会卡死工作线程，`asyncio.timeout` 也打断不了 C 层正则。

### 6. 必需的测试

`tests/test_book_import_split.py`：强规则真值表、对话体短行、中文数字、分卷 + 序章 + 同一行卷章、序章阈值边界（198 / 199 丢弃、200 保留）、
目录区（恰好 5 项去除、4 项保留、目录项间 < 50 字的文字仍算目录、正文 ≥ 50 字为正文开始、卷标题保留）、兜底切块、md / docx 层级、epub、
编码与超限、正则校验 / 重切 / 超时（无遗留子进程）/ `_reap` 的 SIGKILL 兜底、7 种校正与边界、4 类提示。
`tests/test_book_import.py`：上传 / 列表 / 单章全文、格式与拒绝、multipart 前拦截、5 个上限与过期清理、校正与正则重切、
修订号过期 409 带 `code`（ops / resplit / start）且其他 409 不带、并发同一修订号由条件 UPDATE 拦下、预估、开始导入的校验与整体回滚、
部分导入范围重新编号与反推范围换算、跨用户 404。

---

## 场景：`book.import` 反推任务（断点、暂停、跳过、只写空字段）

### 1. 范围 / 触发条件

`services/book_import/reverse.py`、`merge.py`、`task_engine/handlers/book_import.py`、`analysis_service.enqueue_pending`、
模板 `book_import.digest` / `book_import.synthesize`（分类「拆书」）。

### 2. 签名

```python
KIND = "book.import"; BATCH_CHARS = 20000; MAX_RETRIES = 2; RETRY_DELAYS = (2.0, 5.0)   # 测试 monkeypatch 为 0
PHASES = ("digest", "synthesize", "apply", "style", "analyze", "done")
def split_parts(content, limit=None) -> list[str]; def plan_batches(items, limit=None) -> batches   # 预估共用
def new_checkpoint(batches) -> dict; def summarize(cp) -> dict; def apply_skip(cp, cursor) -> int
async def validate_start / revalidate / build_import_job / run_import_job / skip_paused
POST /book-imports/tasks/{task_id}/skip -> 202 TaskOut      # 继续 / 结束沿用 /tasks/{id}/resume、/finish
```

### 3. 契约

- **任务**：`resource_key = book_import:{user_id}`、`project_id` 为新项目、标题 `拆书反推：《书名》`。同一用户最多一个未结束
  （queued / running / paused）的拆书任务：paused 不受部分唯一索引约束，由 `open_import` 检查（开始导入、`validate_and_claim`、
  `revalidate` 三处）。
- **断点**（`tasks.checkpoint`）：`{phase, batches: [{parts: [[章节 id, 段下标, 总段数, 章节号]], state: pending|done|skipped, error}],
  part_summaries: {章节 id: {段下标: 摘要}}, findings: {characters, organizations, world, careers, relations}, final, stats, skipped, warnings,
  pause_reason}`；`step_cursor` = 当前批下标。`TaskOut.checkpoint` 经 handler 的 `checkpoint_out` 只给 `summarize` 摘要
  `{phase, batches_total, batches_done, batches_skipped, stats, skipped, warnings, pause_reason}`（不含 findings / final）；
  任务 `result` 与 `paused` 载荷是同一结构，`/finish` 后另加 `ended_early: true`。
- **装批**：首次运行（`checkpoint` 为空）时按反推范围 `chapter_no` 顺序装批，每批约 2 万字，单章超出时按句末切段分到相邻批；只存 id 与段号，
  执行时按 id 读最新正文（章节已删 → 跳过 + 警告；正文变短段不存在 → 记空摘要）。
- **通读**：每批带已知角色名单（按出现次数前 200 个、≤ 3000 字）；**摘要只写空摘要**（≤ 150 字），分段章节各段摘要先存
  `part_summaries`，全部段完成后以「；」拼接再写；本批摘要写入、`merge.accumulate` 合并结果与断点（`step_cursor + 1`）**同一事务提交**，
  所以中断后重试不会重复调用已完成的批次。
- **合并**（纯函数）：角色姓名规范化（去全部空白、小写）后并查集合并——姓名或任一别名相同即同一角色；显示名取出现次数最多的姓名，
  别名取并集（排除显示名），文本字段取最长，`count` 累加；组织 / 职业按名称、世界观按 `(category, 标题)`、关系按无序双方（先映射到显示名）去重；
  每类最多 300 条（丢弃出现次数最少的）、文本截断到 300 字。
- **汇总**：抽样摘要（> 2 万字时保留首尾各 10 章、中间等距）+ 合并结果的紧凑文本（合计 ≤ 2 万字）→ `final`；某类为空时用合并结果补；
  模型常漏别名：`merge.restore_aliases` 按姓名 / 别名找到合并结果中的同一角色并回其姓名与别名；该合并组还含另一个最终角色的姓名时
  （各批误并、汇总分开）视为歧义、不补；
  项目字段截断到 schema 上限（简介 2000、主题 200、标签 ≤ 8 个且每个 ≤ 10 字、卖点 500、读者 100、题材 100）。
- **写入设定（只写空字段，单事务）**：项目字段为空才写（`tags` 为空列表才写，`genre` 为空才写）；角色姓名或别名与已有角色的姓名或别名相交 → 跳过；
  组织 / 职业 / 世界观按规范化名称（标题）已存在 → 跳过；关系双方都能对应到角色、不是同一人、且 `find_pair` 为空才经
  `relationship_service.create_relation(source="import")` 写入——已有关系是预期内的跳过，不记警告；写入失败（AppError）只记警告。
  实体经各模块的 `new_*` 原语写入，检索单元 / 历史与手动新建一致。
- **风格**：反推范围内首 / 中 / 末章各取前 2000 字（< 200 字跳过并警告）→ `style.extract`（可被用户覆盖）→ `WritingStyle`
  「《书名》原文风格」（重名加「（2）」…），项目没有默认风格时设为默认；失败（含重试后）只记警告，不暂停。
- **深度分析**：`deep_analyze` 时在写入设定之后经 `analysis_service.enqueue_pending` 批量插入 queued 的 `chapter.analyze`
  （`serial_key = analysis:{project_id}`，不逐章走 `validate_and_claim`），记录 `analyses_queued`。
- **重试与暂停**：每次 AI 调用失败（提供方错误、空输出、JSON 解析失败）自动重试 2 次（退避 `RETRY_DELAYS`），每次都写生成记录；
  仍失败 → `pause_reason = {reason: "failed", phase: digest|synthesize, batch_index|null, error}` 提交后产出 `paused`。
  - 继续（`/tasks/{id}/resume`）：`revalidate` 后同一阶段同一批重做，`build_import_job` 清空 `pause_reason`。
  - 跳过（`/book-imports/tasks/{id}/skip`）：要求 `status == paused` 且 `pause_reason.reason == failed`，否则 409 `SKIP_CONFLICT`；
    通读：当前批 `skipped`、章号写进 `skipped`、分段章节该段记空摘要、**`step_cursor + 1`**；汇总：`final` 改用代码合并结果
    （角色前 60 个、项目字段留空），`phase = apply`。条件 UPDATE（`status == paused`）保存后 `task_service.resume_task`。
  - 结束：`/tasks/{id}/finish`，已写入的数据保留；取消 / 中断同样保留（`on_abort` 不回退）。
  - 中断（重启 → `interrupted`）：`validate_retry` 沿用原输入，`create_task` 复制断点，从 `step_cursor` 续跑。
- **事件**：`stage`（`通读 第 i/N 批（第 a–b 章）` / `汇总设定` / `写入设定` / `提取写作风格` / `排队深度分析`，重试时带「失败，第 n 次重试」）、
  `reset`（每次调用前）、`delta`、`batch_end {index(0 起), total, state: "done"}`、`paused` / `done`（摘要）。
  进度：通读 0–80（按批）、汇总 85、写入 90、风格 / 分析 95，单调。
- **生成记录**：`kind = book_import.digest | book_import.synthesize | book_import.style`（`generation_logs.kind` 在 0017 放宽到 String(32)），
  `chapter_id` = 本批第一章（汇总 / 风格为空），`params.prompt = prompt_meta(key, override)`、`attempt`、`batch_index`。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 同一用户已有未结束的拆书任务（开始 / 重试 / 继续 / 跳过） | 409 `OPEN_CONFLICT` |
| 跳过一个不是因失败而暂停的任务（或非 book.import） | 409 `SKIP_CONFLICT` |
| 跳过时任务状态已变化（并发） | 409 `任务状态已变化，请刷新后重试` |
| AI 配置无效 / SSRF | 400 / 404（不建任务；开始导入整体回滚） |
| 项目在运行中被删除 | 任务 failed `项目不存在` |

### 5. Good / Base / Bad

- Good：第 2 批三次 `AIProviderError` → paused（`step_cursor = 1`）；跳过 → 第 2 章摘要为空并列入 `skipped`，其余批照常完成。
- Base：用户在通读期间手写了角色「晴儿（别名 苏晴）」与简介 → 反推出的「苏晴」被跳过、简介不被覆盖、关系照常对应到「晴儿」。
- Bad：为每批建一个子任务或每章 `create_task("chapter.analyze")` 逐个提交——断点散落在多个任务里，续跑与跳过语义无法保证，3000 章时逐行提交也很慢。

### 6. 必需的测试

`tests/test_book_import.py`：完整流程与统计 / 生成记录 / 版本、已有数据不被覆盖（摘要、简介、标签、角色别名、组织）、已有关系不再写入且无警告、
失败暂停后继续 / 跳过 / 结束、跳过条件 409、汇总失败跳过用合并结果、中断后重试不重复调用（FakeLLM 计数）、深度分析在角色写入之后入队、
风格保留已有默认与失败只警告、模板覆盖生效、执行时章节被删、第二个拆书任务 409、两个模板渲染与锁定变量。
`tests/test_book_import_split.py`：别名并查集合并、实体去重与体积上限、装批与分段、`apply_skip`（cursor 前进、分段空摘要、汇总兜底）、抽样与风格名。
`tests/test_project_transfer.py::test_registry_*`：`book_imports` 只引用 `users`、与任何项目无关，不在备份注册表范围内（`registry.py` 注释说明；若以后给它加 `project_id` 外键，必须登记到 `NOT_EXPORTED`）。

### 7. 错误写法 vs 正确写法

```python
# 错误：靠 detail 文案区分修订号过期，文案一改前端就不再重新拉取
raise ConflictError("解析结果已被修改，请刷新后重试")
# 正确：带结构化标记，前端判断 err.body.code === "stale_revision"
raise ConflictError(STALE, extra={"code": STALE_CODE})
```
