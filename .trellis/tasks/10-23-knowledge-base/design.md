# 技术设计：知识库与向量剧情记忆

## 1. 依赖

新增 `numpy`（向量计算）、`pypdf`（PDF 文字层）和 `python-multipart`（FastAPI 上传）。docx 和 epub 用标准库的 `zipfile` 加 `xml.etree` / `html.parser` 解析，不引入 python-docx 和 ebooklib。

## 2. 检索基建（`app/services/retrieval/`）

### 2.1 向量模型配置

- `ModelType` 增加 `embedding`。AI 配置页可以新建向量模型配置，每个用户可以设一个默认的向量模型（沿用现有"按类型设默认"的逻辑）。
- `LLMProvider` 新增 `async embed(texts: list[str], model: str) -> list[list[float]]`：
  - `openai_compat`：调用 `POST {base}/embeddings`，参数 `{model, input: [...]}`；
  - `gemini`：调用 `batchEmbedContents`；
  - `anthropic`：抛出 `AIProviderError("该提供方不支持向量模型")`，前端在新建向量模型时不提供这个选项。
- 每批最多 32 条，单条截断到 2000 字。沿用现有的超时设置、SSRF 防护和私网开关。
- 模型标识：`embedding_key = f"{provider}:{model}:{dim}"`。维度取第一次返回的向量长度。

### 2.2 存储

- 统一的检索单元表 `retrieval_chunks`：
  - 字段：`id`、`project_id`（CASCADE）、`source`（`kb` 或 `memory`）、`ref_id`（对应 `kb_chunks.id` 或 `story_memories.id`）、`text`（参与检索的文本）、`tokens`（JSON，二字组词频，供 BM25 使用）、`length`、`embedding`（LargeBinary，可为空，归一化后的 float32）、`embedding_key`（String 200，可为空）、`chapter_no`（可为空，仅记忆有）、`enabled`（bool，屏蔽或文档关闭注入时为 false）、`pinned`（bool）、时间戳。
  - 唯一约束：`(source, ref_id)`。
  - 索引：`(project_id, source, enabled)`。
- 知识库和记忆各自保存业务数据（第 3、4 节），`retrieval_chunks` 只保存检索所需的冗余字段。这些冗余字段由业务写入点统一调用 `retrieval_service.upsert/delete` 维护。

### 2.3 打分

- **关键词**：分词方式是中文按字符二元组；连续的英文和数字作为一个词；去掉标点；中文单字不算词。查询时计算 BM25（k1=1.2，b=0.75），文档频率按"项目 + source"的候选集合即时统计。原始分数除以该查询的最高分，归一化到 0 到 1。
- **向量**：查询向量和文档向量都已归一化，点积就是余弦值，范围 -1 到 1，映射成 `(cos+1)/2`，落到 0 到 1。只有当候选块的 `embedding_key` 和当前向量模型一致时才参与计算。
- **混合**：有向量时，最终分数 = 0.7 × 向量分 + 0.3 × 关键词分；没有向量时只用关键词分。`min_score` 对最终分数生效。
- **性能**：一次查询读出该项目、该 source 的全部候选（只取 id、tokens 和 embedding），用 numpy 矩阵乘一次算完。单项目 5 万块以内都在百毫秒级。进程内按 `(project_id, source)` 缓存矩阵，以 `max(updated_at)` 和数量作为失效标记。
- `search(db, project_id, source, query, *, top_k, min_score, filters) -> list[Hit]`，Hit 包含 `{ref_id, text, score, vector_score, keyword_score, chapter_no, pinned}`。

### 2.4 建索引任务

- 新的任务类型 `kb.index`，`serial_key = index:{project_id}`，同一项目串行执行。它有 3 种模式：
  1. **文档模式**（`document_id`）：解析 → 分块 → 写入 `kb_chunks` 和 `retrieval_chunks`（同时算好关键词）→ 有向量模型时分批生成向量。
  2. **记忆模式**（`chapter_id`）：为该章已经写入的记忆补算向量。
  3. **重建模式**（`rebuild`）：为项目中所有 `embedding_key` 与当前向量模型不一致的检索单元重新生成向量。
- 进度按"已处理块数 / 总块数"计算；每完成一批就提交一次。中断后重试时，跳过已有当前 `embedding_key` 的块。
- 生成向量失败（没有配置、提供方报错）时，任务状态为成功并附带 warning，这些块只参与关键词检索。文档状态记为 `ready_keyword_only`。
- **需要重建的判断**：项目中存在 `embedding_key` 不等于当前默认向量模型的检索单元，或者存在 `embedding` 为空的块，而当前已经配置了向量模型。知识库页和记忆页据此显示"需要重建索引"和一键重建按钮。

## 3. 知识库

### 3.1 数据（迁移 `0015_knowledge_base`）

- `kb_settings`：每个项目一行，在第一次访问时创建。
  - 字段：`project_id`（PK，CASCADE）、`auto_inject`（bool，默认 true）、`top_k`（1–20，默认 6）、`min_score`（0–1，默认 0.35）、`max_chars`（500–8000，默认 3000）、`memory_top_k`（默认 8）、`memory_max_chars`（默认 1500）、`memory_min_score`（默认 0.35）。
  - 记忆的参数也放在这里，在记忆页调整。
- `kb_documents`：
  - 字段：`id`、`project_id`、`filename`（≤ 255）、`mime`、`size`、`sha256`、`status`（`pending` / `parsing` / `indexing` / `ready` / `ready_keyword_only` / `failed`）、`error`、`text`（Text，解析后的全文，上限 300 万字）、`char_count`、`chunk_count`、`inject`（bool，默认 true）、时间戳。
  - 唯一约束：`(project_id, sha256)`。重复上传时返回 409 并告知已存在的文档。
- `kb_chunks`：
  - 字段：`id`、`document_id`（CASCADE）、`seq`、`heading_path`（String 500）、`content`、`start_offset`、`end_offset`。

### 3.2 解析与分块

- **解析器**：`kb_parser.parse(filename, data) -> ParsedDoc{text, headings}`。
  - txt：依次尝试 utf-8-sig、utf-8、gb18030 解码。
  - md：同上，`#` 开头的行识别为标题。
  - json：格式化输出为缩进文本，键路径作为标题。
  - docx：解析 `word/document.xml`，`w:pStyle` 为 Heading n 的段落识别为标题。
  - epub：按 OPF spine 顺序读取 xhtml，提取 h1–h3 作为标题、p 作为正文。
  - pdf：用 `pypdf` 逐页提取文字。整篇提取出的文字少于 50 字时，判定为扫描件，返回 400"PDF 没有文字层（扫描件不支持）"。
- **限制**：类型按扩展名和文件头魔数双重校验；单文件不超过 20MB；每个项目最多 200 个文档；解析出的文本超过 300 万字时截断，并在文档上记一条 warning。
- **分块**：
  1. 先按标题切段，每段带上 `heading_path`（例如"第一卷 > 门派 > 天剑宗"）。
  2. 段内按空行分段，累积到约 500 字为一块；超长的段落按句末标点再切。
  3. 相邻块重叠 80 字，重叠部分从上一块末尾按句子边界截取。
- **检索文本**：`retrieval_chunks.text = heading_path + "\n" + content`，标题参与检索。

### 3.3 API（`/api/v1/projects/{pid}/kb`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/settings` | 读取检索设置 |
| PATCH | `/settings` | 修改检索设置 |
| GET | `/documents` | 文档列表，含状态、块数、是否注入 |
| POST | `/documents` | multipart 上传，返回文档和 `task_id`（建索引任务），状态码 202 |
| PATCH | `/documents/{id}` | 修改 `inject`，同步更新 `retrieval_chunks.enabled` |
| DELETE | `/documents/{id}` | 删除文档，级联删除分块和检索单元 |
| POST | `/documents/{id}/reindex` | 重新分块并重建该文档的索引 |
| GET | `/documents/{id}/chunks` | 分页查看分块 |
| POST | `/search` | 检索调试，请求体 `{query, top_k?, min_score?}`，返回命中块（文件名、标题路径、各项分数） |
| GET | `/index-status` | `{embedding_model, needs_rebuild, stale_count}` |
| POST | `/rebuild` | 启动重建模式的建索引任务 |

### 3.4 注入

- `kb_service.context_block(db, project, query, *, purpose) -> str`：
  - 总开关关闭、没有命中或检索出错时返回空串；出错时记 warning 日志。
  - 有命中时，格式为：

    ```
    【参考资料（来自知识库，仅供参考；与项目设定冲突时以设定为准）】
    - 《文件名》标题路径：内容
    ```

    总长度受 `max_chars` 限制。
- 各处的查询组成：
  - 章节生成、按建议重写、批量：用 `chapter_task` 的标题、梗概和关键事件，加上出场角色名；
  - 大纲续写：作者给的方向，加上最近的章纲标题；
  - 大纲展开：要展开的章纲；
  - 向导：作品信息加上本步骤的额外要求；
  - 实体生成：生成请求中的描述，加上作品信息；
  - 灵感模式：想法、补充想法和已定内容。
- **接入方式**：在相关模板中新增一个可选变量 `knowledge`。默认模板在变量非空时，在参考资料之后输出这一块；为空时不输出，所以逐字一致测试仍然通过。章节类提示词里，这一块由 `context_builder` 拼进 `context`，见第 5 节。

## 4. 剧情记忆

### 4.1 数据（同在迁移 `0015`）

`story_memories` 表：

- 字段：`id`、`project_id`、`chapter_id`（可为空，SET NULL；章节删除时会先删除它的自动记忆）、`chapter_no`、`kind`（`summary` / `plot_point` / `hook` / `character` / `foreshadow` / `manual`）、`content`（≤ 1000）、`characters`（JSON 角色名列表）、`importance`（1–5）、`origin`（`auto` / `manual`）、`analysis_id`（可为空）、`pinned`、`muted`、`content_key`（`sha1(kind + 规范化内容)` 的前 16 位，用于在重新分析时匹配标记）、时间戳。
- 规范化内容的方法：去掉空白和标点，再转成小写。

### 4.2 从分析生成

`memory_service.sync_from_analysis(db, chapter, analysis)`。它在分析结果落库的同一个事务里调用，即 `run_analysis_job` 成功写入之后，所以只要分析成功就一定会写记忆。

1. 抽取：
   - `summary`：1 条，重要度 3；
   - `plot_point`：情节点中 `importance ≥ 3` 的；
   - `hook`：钩子中 `strength ≥ 6` 的，重要度取 `ceil(strength / 2)`；
   - `character`：每条角色变化记为"名字：current_state"，生死变化重要度为 5，其余为 3；
   - `foreshadow`：埋设记为"埋下：…"、回收记为"回收：…"，重要度 4。
2. 取出该章原有的自动记忆，按 `content_key` 建立映射。新记忆中能匹配上的，继承原来的 `pinned` / `muted`；没有匹配上的旧记忆删除。手动记忆不受影响。
3. 在 `retrieval_chunks` 中写入对应的检索单元（同时算好关键词）；`enabled = not muted`。
4. 当前配置了向量模型时，排队一个记忆模式的 `kb.index` 任务来生成向量。生成向量放在后台任务里，不拖慢分析本身。

另外：

- **删除章节**：删除该章的自动记忆和对应的检索单元。手动记忆保留，它们的 `chapter_id` 置空，章号保持不变。
- **章节号平移**：`story_memories.chapter_no` 和 `retrieval_chunks.chapter_no` 都登记到 `CHAPTER_NO_REFS`，由 outline-pro 的反射测试兜底。

### 4.3 召回与注入

`memory_service.context_block(db, project, chapter, task, *, excluded_chapter_nos, max_chars)`：

- 召回范围：`chapter_no < 本章`，并且不在 `excluded_chapter_nos` 中（即前情里已经给了全文的章节）。`summary` 类记忆还要排除前情里已经给出摘要的章节。
- 查询：本章标题、梗概、关键事件、叙事目标，加上出场角色名。
- 选取顺序：
  1. 先放入范围内所有钉住且未屏蔽的记忆，按章号排序，直到占满字数上限；
  2. 再从检索结果中补足到 `memory_top_k` 条，只取最终分数 ≥ `memory_min_score` 的；
  3. 最后整体按章号排序。
- 输出格式：

  ```
  【相关旧情节】
  - 第12章（情节）：……
  - 第30章（钩子）：……
  ```

### 4.4 API（`/api/v1/projects/{pid}/memories`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `?kind=&chapter_from=&chapter_to=&pinned=&muted=&q=` | 分页列出记忆 |
| POST | `` | 新建手动记忆，请求体 `{chapter_no, content, characters?, importance?}` |
| PATCH | `/{id}` | 修改 `pinned`、`muted`；手动记忆还可以改内容 |
| DELETE | `/{id}` | 只能删除手动记忆，删除自动记忆返回 400 |
| POST | `/search` | 检索调试，请求体 `{query, before_chapter_no?}` |

## 5. 接入 `context_builder`

`build_chapter_context` 增加两个块：

- 【相关旧情节】放在前情提要之后、最近章节全文之前；
- 【参考资料】放在世界观之后。

计算方式：

- 先算好前情部分，得到"给了全文的章节号"和"给了摘要的章节号"两个集合，再调用 `memory_service.context_block`；
- 知识库块调用 `kb_service.context_block`，查询同第 3.4 节。

两块各自有字数上限。超出总预算时，裁剪顺序在原有第 1 步之前新增两步：

1. 先从尾部截断【参考资料】；
2. 再从最不相关的一条开始，丢弃【相关旧情节】中未钉住的条目；
3. 之后按原来的顺序继续裁剪。

两块都为空时，上下文与现在逐字一致，parity 测试照常通过。文件头的注释同步更新。

大纲续写、展开、向导、实体生成和灵感模式只注入【参考资料】，大纲续写和展开另外注入【相关旧情节】（查询见第 3.4 节）。它们通过模板变量 `knowledge` 和 `memories` 传入；默认模板在变量为空时不输出这两块。

## 6. 前端

- **AI 配置页**：模型类型增加"向量模型"。选择 Anthropic 时不显示这个类型。说明文字是"用于知识库与剧情记忆的语义检索，不配置时使用关键词检索"。
- **项目侧边栏**新增两个页面：
  - **知识库**：上传区（拖拽，前端预先检查文件类型和大小）、文档表格（状态、块数、注入开关、重建索引、查看分块、删除）、检索设置表单、检索调试框（显示命中的文件、标题路径、总分以及向量分和关键词分），以及"需要重建索引"横幅。
  - **剧情记忆**：按章号范围、类型、钉住或屏蔽筛选；记忆列表上可以直接钉住或屏蔽，手动记忆可以编辑和删除；新增手动记忆；记忆召回参数；检索调试（可以指定"按第 N 章召回"）；重建横幅。
- **章节编辑器**：生成面板提供"本次注入的旧情节和参考资料"的预览，调用 `GET /chapters/{id}/context-preview`，返回两块文本。这一项可选，如果实现成本高就放到之后。
- `types.ts`、`api/kb.ts`、`api/memories.ts`；纯函数放在 `utils/kb.ts`，包括状态文案、分数展示、上传前的检查。

## 7. 取舍

- **应用内暴力计算余弦，而不是 pgvector**：单个项目的数据量小，SQLite 和 PostgreSQL 行为一致，不需要换镜像。代价是数据量非常大时性能不如专门的索引，但对小说项目够用。
- **只保存解析出的文本，不保存原文件**：不需要文件存储设施，也避开了封面任务里还在设计的上传方案。代价是不能下载原文件。
- **统一的 `retrieval_chunks` 表**：打分、缓存和重建的逻辑只写一份。业务表与检索单元表靠写入点同步维护，并用测试保证一致。
- **生成记忆向量放在后台任务**：分析和生成不依赖向量 API 的延迟和可用性。还没生成向量时，这些记忆仍然能通过关键词检索召回。
- **记忆从分析结果规则抽取，不再额外调用 AI**：零额外成本，结果确定。

## 8. 兼容与回滚

- 迁移只新增表和一个枚举值（`model_type` 本来就是字符串），downgrade 时删除这些表。
- 没有向量模型、知识库为空、项目还没有分析结果时，所有提示词与现在逐字一致。
