# 检索基建：知识库与剧情记忆

> 来源任务：`10-23-knowledge-base`（迁移 `0015`）。表结构与外键见 [database-guidelines.md](./database-guidelines.md)，
> 任务契约见 [error-handling.md](./error-handling.md)「后台任务与 SSE 契约」，章节上下文见 [story-entities.md](./story-entities.md)，
> 模板变量见 [prompts-styles.md](./prompts-styles.md)，章节号平移见 [outline.md](./outline.md)。

---

## 场景：统一检索单元与打分（`retrieval_service`）

### 1. 范围 / 触发条件

任何读写 `retrieval_chunks` 的代码：知识库分块、剧情记忆、建索引任务、检索调试、上下文注入；以后新增的检索来源（如拆书）也必须复用。
**业务写入点只经 `upsert_many` / `set_flags` / `delete_refs` / `store_embeddings` 维护检索单元，不得直接改表。**

### 2. 签名

```python
# backend/app/services/retrieval_service.py
BM25_K1 = 1.2; BM25_B = 0.75; VECTOR_WEIGHT = 0.7; KEYWORD_WEIGHT = 0.3
EMBED_BATCH = 32; EMBED_TEXT_CHARS = 2000; QUERY_EMBED_TIMEOUT_SECONDS = 20
def tokenize(text) -> list[str]; def term_freq(text) -> dict[str, int]            # 纯函数
def normalize_vector(values) -> np.ndarray | None; encode_vector / decode_vector  # float32 小端
def make_embedding_key(model_key, dim) -> "provider:model:dim"; key_matches_model(embedding_key, model_key)
@dataclass class Embedder: provider, provider_name, model; model_key; key; async embed(texts) -> list[np.ndarray]
@dataclass(frozen=True) class EmbedEnv: cipher, factory, ai_timeout, allow_private; async embedder(db, user_id)
async def get_embedder(db, user_id, *, cipher, factory, ai_timeout, allow_private) -> Embedder | None
async def default_embedding_config(db, user_id) -> AIConfig | None
async def upsert_many(db, project_id, source, items: list[ChunkInput]) -> None      # 不 commit
async def set_flags(db, source, ref_ids, *, enabled=None, pinned=None, chapter_no=None)
async def delete_refs(db, source, ref_ids); async def store_embeddings(db, rows, vectors, key)
async def stale_chunks(db, project_id, model_key, *, source=None, ref_ids=None, limit=32) -> list[RetrievalChunk]
async def stale_counts(db, project_id, model_key | None) -> {"kb": n, "memory": n}
async def search(db, project_id, source, query, *, top_k, min_score, embedder=None, filters: SearchFilter | None)
    -> SearchResult(hits: list[Hit], mode: "hybrid" | "keyword", warnings, candidates)
# Hit: ref_id, chunk_id, text, score, vector_score | None, keyword_score, chapter_no, pinned
# SearchFilter: enabled_only=True, chapter_lt, exclude_chapter_nos, exclude_ref_ids, ref_ids
```

### 3. 契约

- **分词**：NFKC + 小写；中文（含假名）按字符二元组、连续英文数字为一个词、标点空白切分、**中文单字不算词**
  （`tokenize("林远山，在GPT-4的天剑宗！ 林") == ["林远","远山","gpt","4","的天","天剑","剑宗"]`）。
- **关键词分**：BM25（k1=1.2、b=0.75），df / avgdl 按过滤后的候选集合即时统计；除以本次最高分 → [0, 1]（最高分恒为 1）。
  只用关键词时，关键词分为 0 的候选**不算命中**。
- **向量分**：入库前归一化为 float32；`(cos + 1) / 2` → [0, 1]。只有当**全部**候选的 `embedding_key` 都等于当前查询向量的 key 时才混合；
  否则整次查询只用关键词并给 warning `KEYWORD_ONLY_STALE`（换了模型 / 维度、重建或建索引尚未完成、某些块向量失败）——
  **旧向量一律不用**，候选里有不属于当前模型的向量时连查询向量都不求。
- **混合**：`0.7 × 向量分 + 0.3 × 关键词分`；`min_score` 作用于最终分（阈值以下不返回，测试覆盖）。
- **降级**：查询向量失败（未配置 / 超时 / 提供方报错）→ 关键词 + warning，**不抛错**。
- `embedding_key = f"{provider}:{model}:{dim}"`（维度取首次返回的长度）；「属于当前模型」= 以 `"{provider}:{model}:"` 开头
  （模型名可含冒号）。`stale` = 没有向量或 key 不属于当前默认向量模型。
- **缓存**：进程内按 `(project_id, source)` 缓存候选矩阵与倒排表（不含正文，命中后按 id 取正文）；签名
  `(count, max(id), sum(version), sum(coalesce(chapter_no, 0)))`。`retrieval_chunks` 开启 `sqlite_autoincrement`（id 不复用），
  每次更新 `version + 1`，章节号平移改变 `sum(chapter_no)`，因此任何写入（包括不经本模块的平移）都会让缓存失效。
- **向量模型**：`ModelType` 增加 `embedding`（按类型各自默认）；`openai_compat` → `POST {base}/embeddings {model, input, encoding_format: "float"}`
  （按 `index` 还原顺序），`gemini` → `batchEmbedContents`，`anthropic` → `AIProviderError("该提供方不支持向量模型")`，
  新建 / 修改为 anthropic + embedding → 422 / 400 `该提供方不支持向量模型`。连通性测试对一条短文本求向量并报告维度。
  向量模型只经 `ai_config_service.build_provider`（SSRF / 私网开关 / 超时与文本模型一致）。

### 4. 校验与错误矩阵

| 条件 | 结果 |
|---|---|
| 没有向量模型 | 只用关键词，`mode = keyword`，无 warning |
| 有向量模型，但候选缺少当前模型向量 | 只用关键词 + `KEYWORD_ONLY_STALE` |
| 查询向量失败 / 超时 | 只用关键词 + `查询向量生成失败（…）` / `…超时` |
| 最终分 < `min_score` | 不返回 |

### 5. Good / Base / Bad

- Good：业务写入后 `await retrieval_service.upsert_many(db, pid, "memory", [ChunkInput(...)])`，与业务数据同一事务提交。
- Base：`search(..., embedder=None)` → 纯关键词，结果仍可用。
- Bad：`db.add(RetrievalChunk(...))` 不算 tokens / 不清旧向量；或在 `RetrievalChunk` 上直接 `update(...)` 不加 version（缓存不失效）。

### 6. 必需的测试

`tests/test_retrieval.py`（分词规则、向量往返、关键词分有界与阈值、混合分有界与公式、无向量只用关键词、key 不一致不用旧向量与部分重建、
查询向量失败降级、缓存失效（改正文 / 屏蔽 / 删除最大 id 再插入 / 章节号平移）、过滤器、改正文清空向量、
退化向量（空 / NaN / inf / 非一维）、BM25 边界（无可用词 / 全 0 / 单候选 / top_k=0）、向量字节数与 key 维度不符时整次只用关键词）；
缓存是进程级的，`tests/conftest.py` 的 `app` fixture 前后都 `retrieval_service.invalidate()`（每个测试新库、项目 id 从 1 开始，签名可能相同）；
`tests/test_kb.py::test_openai_compat_embed_request_format` / `test_gemini_embed_request_format`（假服务校验请求体）。

---

## 场景：知识库（`kb_parser` / `kb_service` / `kb.index`）

### 契约

- **解析**（`kb_parser.parse(filename, data) -> ParsedDoc{kind, text, warnings}`，CPU 密集，用 `anyio.to_thread.run_sync`）：
  扩展名 + 魔数双重校验（docx / epub = `PK\x03\x04`，pdf = `%PDF-`，文本类前 8KB 不含 NUL）；≤ 20MB；解析结果统一为类 Markdown
  （标题行 `#… 标题`，段落空一行）；> 300 万字截断 + warning。txt / md：utf-8-sig → utf-8 → gb18030（UTF-16 BOM 单独处理）；
  json：键路径为标题、标量 `键：值`、列表 `- 值`（对象项优先用 name / title 作标题）；docx：`word/document.xml`，
  样式名 / id 为 `Heading n` / `标题 n` / `Title` 或带 `outlineLvl` 的是标题（读 `styles.xml`）；epub：OPF spine 顺序，h1–h3 标题，
  其余块级元素为段落，忽略 head / script / style；pdf：pypdf 文字层，有效字数 < 50 → 400 `PDF 没有文字层（扫描件不支持）`；
  docx / epub 防压缩炸弹：条目数 > 10000、声明解压总量 > 200MB、被读取的单个条目 > 64MB → 400 `文件解压后过大，无法处理`
  （zipfile 按声明大小读取，实际更大时 CRC 失败 → 400）；XML（document.xml / styles.xml / container.xml / OPF）经 `_xml`
  预检，**带 DTD 一律 400**（防实体膨胀，UTF-16 编码同样识别）；pdf 累计文字超过 300 万字即停止提取剩余页。
  **只保存解析出的文本**（`kb_documents.text`），不保存原文件。
- **分块**（`chunk_text(text) -> list[Chunk]`）：按标题切段（`heading_path` 如 `第一卷 > 门派 > 天剑宗`，≤500 字，代码块内的 `#` 不算标题）
  → 段内按空行累积到 ≈ 500 字（`CHUNK_CHARS`）→ 超长段落按句末标点切，单句超长硬切 → **同一段内**相邻块重叠 ≤ 80 字
  （从上一块末尾按句子边界取最早的边界；末句 > 80 字时硬取最后 80 字）。每块是原文连续子串：`text[start:end] == content`。
  检索文本 = `heading_path + "\n" + content`。
- **上传体积**：Starlette 解析 multipart 时会先把整个请求体写进临时文件，路由里的 `file.read(limit + 1)` 拦不住超大请求；
  `app/core/upload_limit.UploadLimitMiddleware`（`main.create_app` 注册，规则 = POST `/api/v1/projects/{pid}/kb/documents`，
  上限 20MB + 1MB multipart 开销）在解析前拦截：`Content-Length` 超限直接 400，分块传输边收边计数、超限抛 `HTTPException(400)`，
  detail 均为 `文件过大，单个文件上限为 20MB`。新增上传端点时在同一处加规则。
- **上传** `POST /projects/{pid}/kb/documents`（multipart `file`）→ 202 `{document, task}`：请求内校验并解析（400），文档数 ≥ 200 → 400，
  同项目 sha256 重复 → 409 `该文件已上传过：《原文件名》`（响应体另带 `document_id`）；文档以 `pending` 落库，同一事务创建 `kb.index` 文档任务。
- **任务** `kb.index`（`serial_key = index:{pid}`；文档 `resource_key = kbdoc:{id}`，重建 `kbrebuild:{pid}`，记忆为空）：
  | 模式 | 内容 |
  |---|---|
  | document | 未分块则分块（写 `kb_chunks` + 检索单元，提交）→ 分批（32 条 / 批，每批提交）为 stale 分块生成向量 → `ready`；没有向量模型或生成失败 → `ready_keyword_only` + warning（任务仍 succeeded） |
  | memory | 项目里 stale 的记忆检索单元生成向量；分析成功（`AnalysisHandler.on_success`）、手动记忆增改后由 `kb_index_service.enqueue_memory` 排队（需有向量模型且有 stale 记忆，已有排队中的不重复） |
  | rebuild | 全部 stale 检索单元生成向量，结束后刷新文档状态（分块全有当前向量 → ready）；创建时没有向量模型 → 400 `尚未配置向量模型，请先在「模型配置」中添加向量模型` |
  中断 / 重试：已分块的不再分块，已有当前 key 的块跳过。文档任务失败 / 取消 / 中断 → `on_abort` 把仍在 pending / indexing 的文档置 `failed`；
  可重试任务或 `POST /documents/{id}/reindex`（删分块与检索单元后重新排队；有活动任务 → 409）。进度 = 输出最后一行 `生成向量 x/y`。
- **注入开关** `PATCH /documents/{id} {inject}` → 同步 `retrieval_chunks.enabled`；**删除**先删检索单元再删文档（分块级联；有活动任务 → 409）。
- **设置** `kb_settings`（首次 PATCH 时创建，读取时没有行用默认值）：`auto_inject` / `top_k 1–20 (6)` / `min_score 0–1 (0.35)` /
  `max_chars 500–8000 (3000)` / `memory_top_k (8)` / `memory_max_chars 300–6000 (1500)` / `memory_min_score (0.35)`。
- **其他路由**：`GET /documents`、`GET /documents/{id}/chunks?page&page_size`、`POST /search {query, top_k?, min_score?}`
  （命中带 filename / heading_path / 三项分数，`mode` 与 `warnings`）、`GET /index-status`
  `{embedding_model, needs_rebuild, stale_count, stale: {kb, memory}, indexing}`、`POST /rebuild`（202 TaskOut）。
- **注入**：`kb_service.knowledge_text(db, project, query, *, embedder, purpose)` → 参考资料正文（不含标题行）：
  总开关关闭 / 没有命中 / 出错（只记 warning 日志）→ 空串；行格式 `- 《文件名》标题路径：内容`，总长 ≤ `max_chars`（放不下的一条在剩余 ≥ 50 字时截断）。
  向导 / 实体生成 / 灵感模式用 `knowledge_for_project`（按项目所有者构建向量模型；项目没有文档时直接返回空串，不触发任何调用）。

| 调用点 | 查询 | 注入方式 |
|---|---|---|
| 章节生成 / 按建议重写 / 批量 | 章节标题（或任务标题）+ 梗概 + 关键事件 + 涉及角色名 | `context_builder`【参考资料】（世界观之后） |
| 大纲续写 | 方向 + 最近 10 条章纲标题 | 模板变量 `knowledge`（+ `memories`） |
| 大纲展开 | 章纲标题 + 梗概 + 额外要求 | 模板变量 `knowledge`（+ `memories`） |
| 向导（四步） | 作品信息 + 额外要求 | `knowledge` |
| 实体生成（角色 / 组织 / 职业） | 描述 + 定位 + 作品信息 | `knowledge`（`relations.parse` 不注入） |
| 灵感模式（从已有项目发起的会话） | 想法 + 补充想法 + 已定内容 | `knowledge` |

### 必需的测试

`tests/test_kb.py`：6 种格式解析与拒绝矩阵、大小 / 截断、分块大小 / 重叠 / 标题路径 / 子串、上传建索引（keyword only）、
上传校验（类型 / 20MB / 扫描件 / 重复 409 / 200 上限）、`test_upload_body_limit_before_parsing`（Content-Length 与分块传输在解析前拦截）、
`test_parse_rejects_dtd_and_zip_bombs`（DTD / UTF-16 DTD / 单条目上限 / 条目数上限）、6 种格式上传、任务失败 → 文档 failed → 重试成功 → reindex → 删除清理、
阈值与 top_k、注入开关、设置校验、配置向量模型后 needs_rebuild → 重建 → hybrid → 换模型 → 重建前不用旧向量、向量失败 warning、
anthropic 不支持、连通性、向导 / 实体 / 章节注入与总开关、`max_chars`、跨用户 404。

---

## 场景：剧情记忆（`memory_service`）

### 契约

- **抽取**（`extract_items(analysis)`，纯函数，不调 AI）：summary（重要度 3）、plot_point（importance ≥ 3）、hook（strength ≥ 6，
  重要度 `ceil(strength / 2)`）、character（`名字：当前状态`，只有存活变化时 `名字：已死亡`，两者都有 `名字：状态（已死亡）`；有存活变化 5，否则 3）、
  foreshadow（`埋下：…` / `回收：…`，4）。相关角色：人物类为该名字，其余按 `cast_service.match_characters` 匹配项目角色。
- **同步**（`sync_from_analysis(db, chapter, analysis)`）：在 `run_analysis_job` 写入分析与回写之后、**同一事务** commit 前调用。
  `content_key = sha1(kind + "\0" + 规范化内容)[:16]`（规范化 = NFKC、去空白与标点 / 符号、小写）；同 key 的旧行复用（保留 pinned / muted，
  正文相同时向量也保留），匹配不上的旧自动记忆连同检索单元删除；手动记忆不受影响。检索单元 `text = 内容 + "\n" + 相关角色名`、
  `enabled = not muted`、`pinned` 同步、`chapter_no` 冗余。分析失败不写任何记忆。
- **删章**：`chapter_service.delete_chapter` 与 `plan_service`（删空章节）在删除前调用 `on_chapters_deleted`：删自动记忆与检索单元；
  手动记忆保留，`chapter_id` 由外键置空、章号不变。章节 PATCH 改 `chapter_no` → `on_chapter_renumbered` 同步该章记忆与检索单元。
  平移（插入 / 删除章节）由 `CHAPTER_NO_REFS` 兜底（`story_memories.chapter_no`、`retrieval_chunks.chapter_no` 已登记）。
- **召回**（`recall(db, project, chapter_no, query, *, embedder, excluded_chapter_nos, summary_chapter_nos, settings=None)`）：
  范围 `chapter_no < N`、不在 `excluded_chapter_nos`（前情给了全文）；summary 类再排除 `summary_chapter_nos`（前情给了摘要）。
  先放钉住且未屏蔽的（按章号，直到 `memory_max_chars`），再从检索结果（最终分 ≥ `memory_min_score`）补足到 `memory_top_k` 条，
  最后按章号排序。行格式 `- 第12章（情节）：……`（标签：摘要 / 情节 / 钩子 / 人物 / 伏笔 / 备忘）。**任何异常 → 空列表**（只记日志，含钉住的）。
- **手动管理**（`/projects/{pid}/memories`）：`GET ?kind&origin&chapter_from&chapter_to&pinned&muted&q&page&page_size`、
  `POST {chapter_no, content ≤1000, characters?, importance?, pinned?}`（201，按章号关联已有章节）、
  `PATCH /{id} {pinned?, muted?, chapter_no?, content?, characters?, importance?}`（自动记忆改内容 → 400 `自动记忆只能钉住或屏蔽，不能修改内容`）、
  `DELETE /{id}`（自动记忆 → 400 `自动记忆不能删除，可以屏蔽它`）、`POST /search {query, before_chapter_no?, top_k?, min_score?, include_muted?}`。
  增改后路由调用 `enqueue_memory` 并 `engine.notify()`。
- **注入预览** `GET /chapters/{cid}/context-preview` → `{knowledge, memories}`（带标题行、裁剪前；不注入为空串）。

### 必需的测试

`tests/test_memories.py`：抽取规则、content_key 规范化、重新分析替换 + 标记继承 + 手动不变 + 检索单元同步、删章清理、改章号、
手动记忆 API 与筛选 / 检索调试、召回范围（只取第 N 章之前、全文章节不召回、前情摘要不重复）+ 屏蔽 + 钉住优先 + 位置、预算与按章号排列、
记忆与知识库分块注入 + 预览一致、检索出错照常生成且无空块、记忆向量任务、大纲续写注入、分析失败不写记忆、跨用户 404。

---

## 场景：接入新的注入点

1. 组好查询（只放与本次生成相关的短文本：标题、梗概、关键事件、角色名；**不要**把整段角色卡拼进去——MuMu 的反例）。
2. 有 `cipher / factory` 的服务用 `retrieval_service.get_embedder(...)`（或路由注入 `EmbedEnvDep`），没有向量模型时传 `None`。
3. 参考资料调用 `kb_service.knowledge_text` / `knowledge_for_project`；旧情节调用 `memory_service.recall` + `format_lines`。
4. 模板里新增可选变量 `knowledge` / `memories`（`registry` 声明 `KNOWLEDGE_VAR` / `MEMORIES_VAR`），默认模板写
   `{% if knowledge %}\n\n【参考资料（来自知识库，仅供参考；与项目设定冲突时以设定为准）】\n{{ knowledge }}\n{% endif %}`；
   变量为空时提示词必须与改造前逐字一致（`tests/test_prompt_parity.py`）。
5. 检索失败只能让块为空，不能让生成失败。
