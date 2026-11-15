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
# 需要区分「没有配置」与「配置了但不可用（端点被拦截、解密失败）」时用它：前者 (None, None)，后者 (None, "「配置名」原因")
async def resolve_embedder(db, user_id, *, cipher, factory, ai_timeout, allow_private) -> tuple[Embedder | None, str | None]
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
  | document | 未分块则分块（写 `kb_chunks` + 检索单元，提交）→ 分批（32 条 / 批，每批提交）为 stale 分块生成向量 → `ready`；没有向量模型或生成失败 → `ready_keyword_only` + warning（任务仍 succeeded）；配置了但不可用时 warning 为 `向量模型不可用：「配置名」原因；仅使用关键词检索`，不得说成「未配置」 |
  | memory | 项目里 stale 的记忆检索单元生成向量；分析成功（`AnalysisHandler.on_success`）、手动记忆增改后由 `kb_index_service.enqueue_memory` 排队（需有向量模型且有 stale 记忆，已有排队中的不重复） |
  | rebuild | 全部 stale 检索单元生成向量，结束后刷新文档状态（分块全有当前向量 → ready）；创建时没有向量模型 → 400 `尚未配置向量模型，请先在「模型配置」中添加向量模型`；有配置但运行时不可用 → 任务失败，error 为 `向量模型不可用：「配置名」原因` |
  **分析派生的记忆向量（E3，仅作者本人且共享向量为 vector 模式）**：`analysis_service.enqueue_memory_for_execution` → `kb_index_service.enqueue_memory_for_analysis`，
  单章 `chapter.analyze`（原 Task `succeeded` 完成收据）与 `chapter.batch` 分析阶段（原 batch `running` + 原分析 reference / `SceneReviewAnalysisReceipt.run_key`）
  都先过 `_analysis_write_barrier`（User → 发送门禁 → 作品 → 原 Task 同 UUID/`queued_at`/`execution_key`），再按 `(source_kind, source_key)` 锁
  `analysis_index_receipts`；已有收据只在 actor/令牌轮次/授权/章节/分析与正文哈希/向量选择全部一致时返回 `existing`（批量不比较 parent 执行轮次——旧执行已被屏障拒绝，
  `existing` 也不再建任务），否则拒绝。新收据、子 `kb.index` Task（`user_id` = 原真实 actor、继承令牌轮次）与统计在一个保存点内同事务写入；提交确认未知时只按本次
  attempt 的 receipt/child UUID 核对，绝不生成第二个任务。`cipher` / `allow_private` 由调用方（含批量）显式传入。E6-c 起协作者执行同样派生：冻结选择为 vector（已授权且作者有向量配置）时，收据记录原邀请轮次（`grant_invitation_id/revision`），子 `kb.index` Task 归真实触发者并经 `bind_task_grant` 携带原轮次，只向量化本次分析写入的记忆，向量为受理时冻结的作者配置（费用由作者承担）；`_verified_analysis_index_source` / `require_bound_index_job` / `store_owner_legacy_embedding_snapshots` 由「仅作者」放宽为一致性检查：作者 ⇔ 无 grant 且 actor 即作者；编辑者 ⇔ 执行、冻结选择与收据三者 grant 逐项相等且配置属作者。`build_index_job` 遇分析绑定键（或无 grant 的 `_shared_embedding`）走 `_build_bound_memory_job`：编辑者按 `require_authenticated(targets=作者)` → `require_write_execution` → `require_project(ai.use, grant_identity)` 重验。无授权编辑者的冻结选择为 keyword，`AnalysisIndexOutcome` 仍为 keyword、不派生、零向量调用。
  **协作者章节任务检索（E4-f）**：`build_chapter_job` 对带 grant 的 generate/revise 只接受原执行身份 + 受理时冻结的 `ResolvedSharedEmbedding`（无授权或作者无向量 → keyword、零发送；
  `allow_owner_ai` 且作者有向量 → 作者向量，查询嵌入经 `for_task` 守卫逐次重验邀请轮次与授权），只读检索不写共享向量；无执行身份的直接调用拒绝，不能落到协作者本人默认向量配置。
  **协作者大纲 / 实体 / 向导检索（E4-g3a/b/c → E6-d 统一，裁决 1/9）**：三类任务与章节入口（E4-f）同口径，统一使用受理时冻结的
  `ResolvedSharedEmbedding`：冻结选择为 vector（accepted editor 已获作者模型授权且作者有默认向量配置；作者本人则为作者默认向量）
  → 查询经 `collaboration_ai_guard.for_task(execution, selection, stamp, extra_bindings=(文本绑定,), …)` → `prepare_provider`
  构造作者向量 embedder，每次真实发送在租约内重验原执行、原邀请轮次、作者授权、两份模型绑定（实体另含已采集来源、向导另含续写来源），
  只读作者旧键 `provider:model:dim`（`shared:v2` 空间键原语 `shared_embedding_model_key` / `store_embedding_snapshot(s)` / `Embedder._space_model_key`
  及其键格式匹配分支已于 E6-f 删除（裁决 14），遗留的 `shared:v2:*` 键行只会被视为「非当前模型」；候选是其他键时按
  `KEYWORD_ONLY_STALE` 退回关键词、不发送查询向量）；冻结选择为 keyword（未授权编辑者 / 作者无向量）→ 检索前以文本 guard
  `require_task_write` 锁内重验后只做关键词检索，任何向量模型零调用。协作者本人的向量配置永不参与。
  - 大纲 `outline_ai_service._retrieval`：去掉旧/新键候选探测，kb 与记忆共用同一 embedder（同查询缓存命中只发一次）。
  - 实体 `entity_ai_service._knowledge_for_execution`：vector 分支对作者与编辑者共用（先 `require_write_execution` + 载入绑定配置，
    `prepare_provider` 结果的 provider/model 须与配置快照一致）；keyword + grant 分支保持原锁内重验。
  - 向导：`WizardHandler._claim` 对作者与协作执行（E6-f 裁决 13 起作者也冻结）都在同一受理事务 `resolve_shared_embedding(for_update=True)`
    并写入 `_shared_embedding`（作者：无 grant、`source=actor`；作者无向量 → keyword）；重试 / 续写按当前授权重新冻结（裁决 15）。
    作者本人默认向量配置本地不可用（`BadRequestError`）时保持原行为：受理成功、不写该键、运行时只用关键词；协作执行仍按原样 400。
    `build_job` 只在输入带该键时反序列化（grant 须与执行一致）；**无该键**的引擎任务（升级前入队的作者 / 协作任务，以及上述作者配置不可用）
    一律只用关键词、零向量发送——不再回落到作者运行时解析的未受控 `knowledge_for_project` 路径。`build_wizard_job` 的引擎执行（`execution`
    非空）作者与协作者共用同一检索分支；`knowledge_for_project` / 续写本人路径只剩无执行身份的直调。
    文本 send guard 与完成后的仅发布 guard 都带 `extra_bindings=(selection,)`（任务输入带 `_shared_embedding` 时缺该额外绑定的 guard 无法构造）；
    `SendGuard._wizard_stream_shape` 允许恰一份共享向量额外绑定（主绑定须为文本模型、额外绑定 grant 须与执行一致——作者为 None），
    `wizard_publication_successor` 另要求前后 guard 的额外绑定相同。
  - **构造普通故障（E6-f 裁决 17）**：大纲 / 实体 / 向导构造作者向量 provider 一律经 `collaboration_ai_guard.prepare_shared_embedding(guard, …)`：
    `prepare_provider` 抛 `CollaborationSendBlocked`（授权变化与 DNS / 端点 / provider 构造故障被统一为阻断）后，新事务内以同一 vector guard
    `require_task_write`（原令牌、原 Task 执行身份、原邀请轮次与 accepted editor、`allow_owner_ai`、冻结选择 `revalidate_shared_embedding`
    含配置指纹 / 默认向量未换 / 本地可用性、可见性戳、实体来源、续写来源）锁内复核：全部成立 → 返回 None，调用方只用关键词继续（零向量发送）；
    任一变化 → 原样失败关闭。续写来源 / 设定来源等策略错误不经降级直接抛出。与同步检索 `kb_service._require_unchanged_shared_selection`
    同口径（HTTP 版按成员轮次复核，任务版按原执行复核，二者都以 `revalidate_shared_embedding` 判定冻结选择）。作者任务同样适用。
  - 代价（与大纲/实体一致，偏严）：冻结的作者向量选择作为额外绑定随文本 guard 逐次重验，作者在协作者任务运行中关闭授权或更换默认向量
    配置时，该任务后续发送 / 帧发布失败关闭。
  - 作者可见行为变化（E6-f 裁决 13 / 17 / 19）：作者本人的向导任务运行中修改或更换默认向量配置（含改密、配置变为本地不可用）→ 任务失败关闭
    （原先按运行时默认静默使用新配置）；构造失败期间冻结配置被改同样失败关闭、不降级（回归 `test_task_shared_embedding_e6f.py::
    test_author_embedding_prepare_failure_with_changed_frozen_config_fails_closed`）。作者名下带记忆条目绑定键的 `kb.index` 任务（分析派生等）
    `retry_blocked_reason=memory_bound`，任务面板不再显示重试（原先点击后 409）。
  - 分析派生记忆召回：`analysis_service` 不做检索（只派生 `kb.index`，见上），无需改动。
  回归：`tests/test_collaboration_{outline_entry_e4g3,entity_entry_e4g3b,wizard_entry_e4g3c}.py`（授权 → 只调作者向量、读作者旧键；
  未授权 → 零向量；`prepare_provider` 后、真实发送前 {直接改库关授权、撤权、降 viewer} → 零向量零文本调用、失败关闭）、
  `tests/test_collaboration_outline_ai.py::test_owner_outline_recalls_only_author_legacy_vectors_with_bound_model`、
  `tests/test_kb_sync_retrieval_e6d_postgresql.py`（实体任务跨实例无通知关授权）。
  **协作者 kb.index 受理与任务身份（E6-a；E6-c 起 HTTP 入口开放）**：`KbIndexHandler.shared_embedding_admission = True` 表示不依赖文本模型的协作受理。
  `validate_index_task(db, *, user_id, params, cipher, allow_private)`：作者沿用原受理（`get_owned_project(lock=True)`，输入不带协作保留键）；
  非作者走 `_validate_collaborator_index_task`：`require_project(ai.use, for_update=True, visibility_write=True)` → `resolve_shared_embedding(require_vector=True, for_update=True)`，
  服务端写入 `_collaboration_grant`（`bind_task_grant`）与 `_shared_embedding`（仅作者默认向量配置；协作者本人向量配置不参与）；未授权 403 `owner_embedding_not_allowed`、
  作者无向量 400 缺失提示。memory 模式（E6-c，裁决 4）只供人工记忆写入后的派生：参数须带写入时原邀请轮次 `expected_grant` 与 `memory_ids`，缺失 → 409 `task_admission_changed`，
  轮次不符 → 403 `permission_denied`；锁内核对条目属本作品且检索单元完整，只把仍缺向量的条目写入服务端保留键 `_manual_memory_ref_ids`（已登记 `_SERVER_TASK_KEYS` 与个人投影内部字段），
  都已有向量 → `MemoryIndexCurrent`（不建任务）。中央 `create_task` 对这类 handler 且无 `accepted_model` 时（非重试，或重试且新旧 grant 相同），
  按 spec 内轮次 `require_project(ai.use, grant_identity=…)` 再锁作品，Task 归真实 actor。
  `build_index_job` 不再以 `project.user_id == task.user_id` 判断：带 grant 的任务走 `_build_collaborator_index_job`——原执行身份/轮次/令牌逐项一致、反序列化冻结选择（vector、配置属作者），
  `require_authenticated` + `require_write_execution` + `revalidate_shared_embedding(for_update=True)`，文档 claim 须属同一 actor；返回 `IndexJob(embedder=None, selection=…, actor_id, grant_identity)`，
  **不调用 `resolve_embedder`**。无 grant 的任务只能是作者本人（执行身份 actor = 作品作者、须带 `_task_actor_token_version`），按作者当前默认向量运行时解析（不写入 Task；`resolve_embedder` 只作可用性探测以保留原降级提示）。
  回归：`tests/test_kb_index_admission_e6a.py`、`tests/test_kb_index_admission_e6a_postgresql.py`。
  **编辑者向量入口（E6-c）**：上传 `upload_document`：编辑者先以 `lock_content_write_actor(visibility_write=True)` 取发送写门禁再锁作品（同事务随后受理协作向量任务，锁序 gate/User → 门禁 → 作品）；
  无作者模型授权 → 同步关键词（`KEYWORD_ONLY_COLLABORATOR`，零任务）；已授权但作者无向量配置 → 同步关键词并提示 `KEYWORD_ONLY_OWNER_UNCONFIGURED`（裁决 2）；已授权且作者有配置但本地校验不可用（密钥无法解密、端点格式/内网拦截、绑定无效）→ 与作者本人上传一致降级为同步关键词并提示 `KEYWORD_ONLY_OWNER_UNAVAILABLE`（零任务零调用，不回退编辑者本人配置）；配置可用 → 建归编辑者的 `kb.index` 文档任务。重新索引 / 重建为显式向量操作，作者配置不可用时仍 400。
  重新索引 / 重建：`_locked_embedding_actor` 对非作者带 `visibility_write`；无授权编辑者 403 `owner_embedding_not_allowed`（含仅关键词重分块，裁决 10），已授权编辑者受理（全书重建允许，费用确认在前端，裁决 3）。
  人工记忆：`memories._enqueue_vectors` 对作者沿用 `enqueue_memory`（全书补空向量）；对 accepted editor 调 `kb_index_service.enqueue_collaborator_memory(engine, authenticated_actor, project_id, grant=写入时轮次, memory_ids=[本次条目])`，
  无授权 / 作者无配置 / 条目已有向量 → 不建任务；任务以 `memory` 模式经 `_build_collaborator_index_job`（`IndexJob.memory_ref_ids`）走 E6-b 受控批次，只补这些条目的空向量（`_require_index_snapshot_identity` 限定快照属绑定条目且须带 grant）。
  viewer 403；待接受 / 非成员 / 已撤权 / 仅管理员 404（路由 `require_project(ai.use)`）；管理员人工保存记忆不派生。协作者 `kb.index` 订阅仍 404（裁决 5）；`index-status` / 检索调试 / `context-preview` 见下「同步检索（E6-d）」。
  协作者重试：`KbIndexHandler.validate_retry` 对文档/重建协作任务重新受理（按当前作者授权与配置重新冻结选择，不沿用旧选择），新轮次须与原任务相同否则 409 `task_round_changed`；`retry_task` 对 `shared_embedding_admission` handler 先取发送写门禁；
  轮次变化（含 HTTP 关作者模型授权——会递增 revision——或撤权重邀）由 `_lock_user_task_project` 返回 404；同轮次但作者模型授权被直接关闭 → 403 `owner_embedding_not_allowed`。分析派生 / 人工记忆条目绑定任务仍不可重试。
  **不可直接重试标识（E6-f 裁决 19）**：`kb_index_service.retry_blocked_reason(task_input)` 是输入层拒绝的唯一判定，`KbIndexHandler.validate_retry`
  与 `TaskOut.retry_blocked_reason`（`task_service.to_out` 经 handler 可选钩子 `retry_blocked_reason(task)` 填写，列表 / 详情 / SSE 快照同源）共用：
  `legacy_index`（0045 前无占用清理记录）→ 409 `kb_legacy_task_not_retryable`；`memory_bound`（分析派生 / 人工记忆条目绑定键，或协作者非文档 / 重建任务）
  与 `binding_invalid`（无 grant 却带选择、grant 不可解析）→ 409「原执行需先核对」；`request_invalid`（request 非对象）→ 409「信息无效」（中央
  `retry_task` 不再对非对象 request 抛 500）。其他任务类型恒为 null；为 null 不代表一定可重试（状态、轮次、授权仍在受理时锁内判定）。
  管理员任务监控使用独立白名单 schema，不含该字段。回归：`tests/test_task_retry_blocked_e6f.py`。
  回归：`tests/test_kb_editor_entries_e6c.py`、`tests/test_kb_editor_entries_e6c_postgresql.py`（撤权先提交 / 上传受理先提交 / 记忆受理先提交）。
  **同步检索与注入预览（E6-d，d1）**：`kb_service.search_access(db, user_id, project_id, env) -> (embedder, grant)` 取代原 `keyword_search_access`，
  供 `POST /kb/search`、`POST /memories/search`、`GET /chapters/{cid}/context-preview` 与 `kb_service.knowledge_for_http` 共用：
  作者 → 本人默认向量（不变，grant 为 None）；accepted editor → 回滚旧事务后锁内（`resolve_access(for_update=True)`）固定原邀请轮次，
  无作者模型授权 / 作者无向量 / 作者配置本地校验不可用（`BadRequestError`）→ 关键词（请求事务保持持锁）；已授权且作者有配置 →
  `resolve_shared_embedding(for_update=True)` 冻结选择 + 读可见性戳 → **回滚**（构造 provider、DNS 与发送租约都在独立会话）→
  `collaboration_ai_guard.for_http(db, actor, selection, stamp)` → `prepare_provider` → `Embedder(bound…)`（作者旧键）→ `guard_embedder`。
  每次查询发送在租约内重验原 HTTP 令牌、原邀请轮次、作者授权与配置；搜索末尾的 `_preflight_guarded_embedder` 再验一次（撤权 → 409
  `permission_denied`），返回前 `_lock_search_result` 按原轮次复核（403/404）。原「授权编辑者检索 409 `collaboration_embedding_unavailable`」删除。
  `prepare_provider` 失败（它把授权变化与 DNS / 构造等普通故障统一为阻断）时，`_require_unchanged_shared_selection` 新事务锁内复核原轮次、
  accepted editor、`allow_owner_ai` 与冻结选择（`revalidate_shared_embedding`）：全部成立 → 视为普通故障退回关键词（裁决 11，持锁返回）；
  任一变化 → 403 `permission_denied`。协作者看到的检索 `warnings` 经 `public_search_warnings` 只保留固定文案（提供方错误原文可能含作者模型名 / 端点）；作者原样。
  `knowledge_for_http(db, *, user_id, project_id, query, env)`：同上选择 → `retrieval.search` → 原轮次复核 → 重新物化命中 → 行格式与
  `knowledge_text` 相同；普通检索故障降级为空串，策略 / 关联类错误原样抛出（转正 `tests/test_shared_http_knowledge_e6.py`，删除其 `shared:v2` 改键步骤）。
  context-preview：章节所属作品的作者 ≠ 请求者时走 `context_builder._collaborator_retrieval_preview`：`require_project(ai.use)`（viewer 403；
  待接受 / 非成员 / 已撤权 / 仅管理员与不存在的章节统一 404「章节不存在」）→ `search_access` → 与作者相同的规划 / 范围 / 前情组装，
  embedder 再以本次 `scope.stamp` `guard_embedder`（与选择戳不同即拒绝发送）→ `scope.verify` → `_lock_search_result`。作者路径不变。
  `GET /kb/index-status`：`require_project(ai.use)`（viewer 403，其余同上 404）；作者完整视图不变；accepted editor 精简视图（裁决 8）`embedding_model`
  恒为 null，不返回配置名 / 模型名 / ID。新增字段（作者与编辑者都返回）：`owner_embedding_configured`、`retrieval_mode: vector|keyword`
  （请求者的同步检索 / AI 上下文会不会用作者共享向量）、`keyword_reason: owner_embedding_not_allowed|embedding_config_missing|owner_embedding_unavailable|null`。
  判定顺序：编辑者未授权 → `owner_embedding_not_allowed`（不再探测作者配置）；作者无向量配置 → `embedding_config_missing`；作者配置存在但本地不可用
  （解密失败、端点格式无效 / 被内网拦截、绑定无效，裁决 16）→ `owner_embedding_unavailable` + `retrieval_mode=keyword`，与上传 `_owner_embedding_usable`
  同口径（`kb_service._shared_embedding_usable`：只读 `resolve_shared_embedding(require_vector=True)`，不加锁、不联网，仅 400 类视为不可用；授权类错误原样抛出），
  作者与编辑者都适用，响应不含故障细节；路由经 `EmbedEnvDep` 取 cipher / allow_private。回归：`test_index_status_owner_embedding_unavailable`、
  `test_index_status_unauthorized_reason_wins_over_unavailable`、`test_index_status_unauthorized_editor_cannot_probe_owner_config_health`（未授权编辑者在作者配置可用 / 不可用时响应逐字一致）。
  `stale_count`（按作者当前默认向量计算，作者与编辑者同口径）即「N 条资料待建向量」（裁决 7）：编辑者上传的关键词资料与授权编辑者人工记忆
  派生失败 / 未派生（E6-c 复核 5b）都计入，作者据此补建。`memory_service.may_enqueue_vectors` 无调用方，已删除。
  回归：`tests/test_kb_sync_retrieval_e6d.py`（授权 / 未授权 / 作者无配置矩阵、{viewer 403、待接受 / 非成员 / 已撤权 / 仅管理员 404}×四入口零调用、
  `prepare_provider` 后发送前 {撤权、HTTP 关授权、直接改库关授权、降 viewer}×{kb、记忆、预览} 零请求、index-status 不泄露配置与待建计数）、
  `tests/test_kb_sync_retrieval_e6d_postgresql.py`（撤权先提交阻塞检索、发送租约先持锁阻塞撤权后返回前复核、跨实例无通知关授权）。
  **受控发送与每批重验（E6-b = E4-h，作者与协作者共用，裁决 6）**：文档/重建/作者旧记忆模式的 `IndexJob` 一律 `embedder=None`，带 `selection`（协作者 = 受理冻结的作者选择；作者 = 构建期作者默认向量的
  `source="actor"` 绑定，形状由 `retrieval_service.legacy_owner_index_selection` 约束）与 `project_owner_id`；分析派生绑定记忆以 `bound_memory=True` 区分，仍走原收据路径。`_embed_stale` 每批：
  ① 短事务读 stale 行 → `snapshot_embedding_inputs(allow_orphans=True)` + 当批 `read_visibility_stamp`，立即回滚；孤儿行在 `_require_index_write` 下 `delete_orphan_snapshot`；
  ② 按截断文本去重后 `collaboration_ai_guard.for_task(execution, selection, stamp, document_claim=…, embedding_inputs=…)` → `prepare_provider` → 真实发送租约内重验原认证/执行/文档占用/本批快照/配置；
  ③ 新写事务 `guard.require_task_write` + `retrieval_service.store_index_legacy_embedding_snapshots`（作者与授权编辑者写作者旧键 `provider:model:dim`，裁决 1；记忆模式只补空向量，重建 CAS 替换全书其他空间，文档模式 CAS 替换**本文档**分块的其他空间——裁决 12，快照身份已限定 `source_document_id`）。
  写入未成功（快照 CAS 未命中或屏障拒绝）时不写任何向量，再以 `_require_index_write` 判定：授权/执行/占用/配置失效 → `EmbeddingPolicyError` 失败关闭（`建索引已停止：授权、模型配置或索引状态已变化，请重新索引`，
  文档由 `on_abort` 释放为 failed，已提交批次保留）；仍有效 → 视为本批来源或可见性代次变化，重新快照（每作业最多 3 次，超出则文档 `ready_keyword_only` + 提示）。快照读取本身的 `retrieval_reference_invalid`（PG 读已提交下两次读取看到不同提交）与写门禁繁忙 `ai_visibility_busy` 同样按「未发送未写入 → 重验 → 重新快照」处理，不得直接记为授权失效。文档模式（裁决 12，作者与授权编辑者）按 `_stale_condition` 选取本文档分块（含旧模型向量）并替换为作者当前模型向量，其他文档与记忆不动；收尾时仍复核本文档分块，真正缺当前作者旧键向量（如并发清空）→ `ready_keyword_only` + `_STILL_STALE` 提示，不得误标 ready。
  向量服务普通故障（`_policy_cause` 为空的 `AIProviderError`）不重发，作者与协作者都降级为关键词（裁决 11）。文档首尾状态写、`_refresh_doc_statuses` 都先 `_require_index_write`
  （原 token + 作者 User 屏障 → `require_write_execution` → 文档占用 → `require_project(ai.use, grant)` → `revalidate_shared_embedding(for_update=True)`）。
  `KbIndexHandler.verify_event` 对非绑定模式在引擎 `_event_task` 重验后补文档占用与配置重验（`done` 帧只核原执行），`verify_finalization` 走 `require_write_execution`。
  守卫侧 `_matches_task_binding(..., legacy_owner_index=kind=="kb.index")` 与 `_require_index_snapshot_identity` 只对 kb.index 接受「Task 不含选择/grant、actor 即作者、作者本人默认配置」的旧作者形状；
  作者旧记忆（无 `_shared_embedding`）不得带任何分析绑定键，不走收据核验。`KbIndexHandler.collaboration_execution_ready = True`：引擎领取并执行有效的协作 kb.index；订阅仍 404（无 `stream_guard`，裁决 5）。
  作者行为变化：运行中作者换默认向量/改密（令牌变化）现在失败关闭，不再按旧 embedder 静默续写；同批文本去重后对重复分块复用同一向量。
  回归：`tests/test_kb_index_guarded_e6b.py`（{撤权、降 viewer、关作者模型授权（HTTP/直接改库）、令牌、作者换默认向量}×{每批之间、发送后写入前}×{通知、无通知}、并发修改资料、普通故障降级、文档模式只替换本文档旧模型向量（作者/编辑者）与收尾缺失保护、协作者重建）、
  `tests/test_kb_index_guarded_e6b_postgresql.py`（写批先提交/撤权先提交两种顺序，`pg_blocking_pids` 证明阻塞，跨实例无通知）。
  富文档提案采纳的自动分析：作者采纳本人任务时须 `_trusted_owner_review_origin`（原 Task 有规范 UUID、`queued_at`、`_task_actor_token_version` 且等于当前认证轮次），
  否则跳过派生、返回 `REVIEW_AUTO_ORIGIN_UNTRUSTED` 警告，不占用资源也不用当前会话补签。
  中断 / 重试：已分块的不再分块，已有当前 key 的块跳过。文档任务失败 / 取消 / 中断 → `on_abort` 把仍在 pending / indexing 的文档置 `failed`；
  可重试任务或 `POST /documents/{id}/reindex`（删分块与检索单元后重新排队；有活动任务 → 409）。进度 = 输出最后一行 `生成向量 x/y`。
  **旧无占用文档任务（E4-e）**：0045 前入队、文档侧 `index_task_key/queued_at/claim_key` 全空的排队文档任务不补造身份、不执行。
  引擎领取、用户取消、作品删除排空共用 `kb_index_service.cleanup_legacy_unclaimed_document_task(db, legacy, status=failed|cancelled)`：
  调用方先 `capture_legacy_document_task(task, task_key=, queued_at=)`（取读取时捕获值）、按原锁序持作品父行锁（领取 gate→User→作品；
  取消 resolve_access；删除排空 `lock_record`）并负责提交/回滚。锁内同一事务全部成立才写：Task 仍 queued、`execution_key`/管理员取消意图为空、
  UUID/queued_at/actor 一致、本作品 document 模式且 `resource_key == kbdoc:{id}`；文档属本作品、pending、三列全空；无该文档其他未结束
  （OPEN 或执行键/管理员意图未清）`kb.index` 任务。随后 Task CAS（failed「旧版知识库索引任务缺少占用记录，未执行；请在知识库中重新索引该文档」/
  cancelled「任务已取消；旧版知识库索引任务已清理…」，input 记 `_kb_legacy_unclaimed_cleanup`）+ 文档 CAS（failed「旧版建索引任务已清理，请重新索引」），
  任一不成立整体回滚并保持原失败关闭（取消 409 `task_cleanup_unproven`，删除 409 `project_tasks_not_stopped`，引擎保持排队并只记一次日志，且按 `(task_id, UUID, queued_at)` 记入本进程退避 `_stalled`：自身执行体结束不立即重领（防 `_on_done → notify` 热循环反复取写锁），外部 `notify()` 或兜底轮询超时清空后重试；「handler 未就绪」、原认证失效、旧章节无 claim、协作失效不可收尾同用此退避）。
  不写正文/分块/向量、不调模型、不派生任务；统计 `record_state_change` 与普通失败/取消一致；提交确认丢失只按原 UUID/轮次持久事实确认。
  引擎在 `_stopping` 或已登记取消意图时不动手。被清理的旧任务重试 → 409 `kb_legacy_task_not_retryable`，作者经 `reindex` 新受理建立带占用的新任务。
  回归：`tests/test_kb_legacy_document_cleanup.py`、`tests/test_kb_legacy_document_cleanup_postgresql.py`。
- **注入开关** `PATCH /documents/{id} {inject}` → 同步 `retrieval_chunks.enabled`；**删除**先删检索单元再删文档（分块级联；有活动任务 → 409）。
- **设置** `kb_settings`（首次 PATCH 时创建，读取时没有行用默认值）：`auto_inject` / `top_k 1–20 (6)` / `min_score 0–1 (0.35)` /
  `max_chars 500–8000 (3000)` / `memory_top_k (8)` / `memory_max_chars 300–6000 (1500)` / `memory_min_score (0.35)`。
- **其他路由**：`GET /documents`、`GET /documents/{id}/chunks?page&page_size`、`POST /search {query, top_k?, min_score?}`
  （命中带 filename / heading_path / 三项分数，`mode` 与 `warnings`）、`GET /index-status`
  `{embedding_model, needs_rebuild, stale_count, stale: {kb, memory}, indexing, owner_embedding_configured, retrieval_mode, keyword_reason}`（协作者精简视图见上「同步检索（E6-d）」）、`POST /rebuild`（202 TaskOut）。
- **注入**：`kb_service.knowledge_text(db, project, query, *, embedder, purpose)` → 参考资料正文（不含标题行）：
  总开关关闭 / 没有命中 / 出错（只记 warning 日志）→ 空串；行格式 `- 《文件名》标题路径：内容`，总长 ≤ `max_chars`（放不下的一条在剩余 ≥ 50 字时截断）。
  向导（作者任务）/ 实体生成（无执行身份直调）/ 灵感模式（作者本人）用 `knowledge_for_project`（按项目所有者构建向量模型；项目没有文档时直接返回空串，不触发任何调用）；协作执行与同步 HTTP 入口见「E6-d」两段。

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
- **注入预览** `GET /chapters/{cid}/context-preview` → `{knowledge, memories}`（带标题行、裁剪前；不注入为空串）；accepted editor 按共享向量矩阵返回，见「同步检索（E6-d）」。

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
