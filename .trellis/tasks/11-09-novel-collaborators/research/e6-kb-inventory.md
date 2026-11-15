# Research: E6 知识库共享向量 + E4-h（KB 重鉴权）现状盘点与切片

- **Query**: 逐条核对 E6（及绑定的 E4-h「KB」重鉴权、E5 遗留中与 KB 相关部分）的现状，并拆成可独立实现、可验证的切片
- **Scope**: internal（静态读码 + 任务文档；未跑测试，未改代码）
- **Date**: 2026-11-13
- **说明**: 当前分支 KB 相关文件全部为未提交改动（`git status`：`kb.py`、`memories.py`、`kb_service.py`、`kb_index_service.py`、`memory_service.py`、`retrieval_service.py`、`handlers/kb.py` 为 M；`0045`、`project_ai_config.py`、`stream_evidence.py`、`knowledgeUpload.ts` 及多数 `*_e6*.py` 测试为 ??）。下文行号以当前工作树为准。早期研究文档（phase-e-index-core / retrieval-core 等，2026-11-09~10）中的部分描述已过时，以本文为准（如共享空间键已是 `shared:v2:`，不是 `v1`）。

---

## 0. 结论摘要

1. **基础原语大多已就绪，生产接线基本没做。** 0045 文档占用、`DocumentIndexClaim`、`require_sending/writing_document_claim`、`snapshot_embedding_inputs`、`store_*_embedding_snapshots`、`ResolvedSharedEmbedding`、`SendGuard.for_task(document_claim=…, embedding_inputs=…)`、引擎领取期协作复核（含 `kb.index` 文档占用释放）、E4-e 旧任务清理都已存在并有测试；但 `build_index_job` 的文档/重建/旧记忆分支仍只允许作者（`project.user_id != task.user_id → 404`），`_embed_stale` 仍用未受控的旧 embedder 和旧 `store_embeddings`。
2. **无授权关键词路径已经完成**（上传同步分块、`ready_keyword_only`、零任务；关键词检索锁内重验；记忆人工编辑只清本条向量），并有 SQLite 与 PG 测试。
3. **授权开启路径是关闭的**：编辑者上传、重新索引、重建、KB/记忆检索在 `allow_owner_ai=true` 时一律 `409 collaboration_embedding_unavailable`；记忆派生和分析派生不排队；`index-status` 和 `context-preview` 对编辑者返回 404。
4. **AI 上下文检索口径不一致**：协作者章节/批量（E4-f/g2）在授权时已经用作者向量做查询嵌入（旧键 `provider:model`）；大纲、实体、向导、分析派生记忆（E4-g1/g3）仍只用关键词。按规范，这些要在 E6 统一切换，并翻转对应的零向量断言。
5. **有一处需要裁决的结构性冲突：向量空间键。** 作者写入的是旧键 `provider:model:dim`；`shared:v2:<sha>`（`store_embedding_snapshots`）没有任何生产写入方。实体用的是 `shared:v2`，大纲两种都试，章节用旧键；预写的 `test_shared_http_knowledge_e6.py` 期望 `shared:v2`。检索要求候选全部同键（`retrieval_service.py:1818-1834`），所以混用会让整书退化为关键词检索。

---

## 1. E6 要求逐条核对

### 1.1 无授权：协作者只维护关键词、检索只用关键词、不调用作者向量、不覆盖已有不同向量

| 子项 | 状态 | 位置 / 说明 |
|---|---|---|
| 无授权编辑者上传 → 同步分块 + 关键词、零任务、`ready_keyword_only` | **已做** | `kb_service.upload_document` `kb_service.py:300-363`（`grant is not None` 分支 343-358：`chunk_document` → `status="ready_keyword_only"` + `KEYWORD_ONLY_COLLABORATOR` 警告 → commit → 换新事务 `lock_content_write_actor` 重验后返回 `task=None`）。路由 `kb.py:43-61` 先 `capture_content_write_actor` 再 `require_project(ai.use)` |
| 无授权编辑者重新索引 / 重建 → 403 `owner_embedding_not_allowed` | **已做** | `_require_vector_task_owner` `kb_service.py:132-139`；`reindex_document` 429、`start_rebuild` 660 |
| 无授权 KB / 记忆检索只用关键词 | **已做** | `keyword_search_access` `kb_service.py:142-163`（编辑者返回 `(None, identity)`，新事务锁内重验原邀请且仍 `allow_owner_ai=false`）；`search_debug` 526-561、`memory_service.search_debug` `memory_service.py:650-702`；返回前 `_lock_search_result` + `verify_retrieval_hits` |
| 不使用编辑者个人向量模型 | **已做** | 测试 `test_collaboration_shared_index_e6.py:206`（个人模型键匹配也走关键词，零请求） |
| 人工记忆修改只清本条向量，其他向量不变 | **已做** | `upsert_many` 正文变化时只置空本行 `embedding/embedding_key`（`retrieval_service.py:1495-1499`）；测试 `test_collaboration_shared_index_e6.py:167` |
| 编辑者人工记忆不派生作者名下 `kb.index` | **已做** | `memories.py:_enqueue_vectors` 60-74 只在 `write_actor.source == "owner"` 时排队；`memory_service.may_enqueue_vectors` 301-304 |
| AI 上下文（任务）在无授权时用关键词 | **已做**（各入口） | 章节/批量：受理时冻结 `ResolvedSharedEmbedding`，keyword 时 `embedder=None`（`generation_service.py:902-918, 1131-1153`）；大纲/实体/向导：协作执行一律关键词（规范 `retrieval.md:124-128`；向导 `wizard_service.py:1052-1058`） |
| 分析派生记忆在协作执行下不排向量任务 | **已做**（保守） | `analysis_service.py:1882-1915`：`execution.grant_identity is None and mode=="vector"` 才排队，否则返回 `keyword`/`unavailable` |
| 不覆盖已有不同向量 | **已做（写入侧）** | 编辑者没有任何向量写入路径；文档模式 `_embed_stale` 只选 `embedding IS NULL AND embedding_key IS NULL`（`kb_index_service.py:1064-1075`）；作者 `rebuild` 按 `_stale_condition` 替换旧模型向量（作者本人的语义） |
| **副作用**：关键词行会让整书混合检索退化 | **现状如此（需裁决 Q7）** | `search` 要求全部候选 `key_matches_model`（`retrieval_service.py:1818-1834`），编辑者新增的空向量行会让作者检索也退回 `KEYWORD_ONLY_STALE`，直到作者重新索引或重建；`index_status` 会对作者显示 `needs_rebuild` |

### 1.2 授权开启：协作者触发共享索引 / 检索可用作者项目向量模型，界面提示费用来源

| 子项 | 状态 | 位置 / 说明 |
|---|---|---|
| 共享向量选择（只用作者默认向量，需要授权） | **原语已做** | `project_ai_config.resolve_shared_embedding` `project_ai_config.py:928-956`、`revalidate_shared_embedding` 959-983（保留原 vector/keyword 决策，不自动转换） |
| 授权编辑者上传 | **关闭** | `upload_document` 310-311 → 409 `collaboration_embedding_unavailable`；测试 `test_collaboration_shared_index_e6.py:147` 断言零任务、零文档 |
| 授权编辑者重新索引 / 重建 | **关闭** | `_require_vector_task_owner` 137-138 → 409 |
| 授权编辑者 KB / 记忆检索调试 | **关闭** | `keyword_search_access` 152-153 → 409 |
| 授权编辑者人工记忆向量 | **未做** | `_enqueue_vectors` 仅作者 |
| 授权编辑者的分析派生记忆向量 | **未做** | `_verified_analysis_index_source` `kb_index_service.py:675-682` 要求 `grant_identity is None` 且 actor 为作者；`analysis_service.py:1882` 同样只放行作者 |
| 同步 HTTP 上下文入口 `knowledge_for_http` | **未做** | 预写的 strict xfail：`tests/test_shared_http_knowledge_e6.py`（整文件，2 个用例 / 3 个 case） |
| AI 任务上下文使用作者向量 | **部分** | 章节/批量**已用**（`generation_service.py:1131-1153`，`for_task` 守卫）；大纲/实体/向导**仍只用关键词**（规范 `retrieval.md:126-128` 明确列出 E6 需要翻转的断言：`test_editor_outline_uses_own_model_and_keyword_retrieval`、`test_collaboration_entity_entry_e4g3b.py::test_editor_entity_ai_uses_own_model_and_keyword_retrieval`、`test_collaboration_wizard_entry_e4g3c.py::test_editor_wizard_uses_own_model_writes_content_and_keyword_retrieval[*-shared|legacy]`） |
| `context-preview`（章节 AI 页签） | **对编辑者 404** | `context_builder.retrieval_preview` `context_builder.py:1026-1046` 用 `get_owned_chapter/get_owned_project` 和 `env.embedder(db, user_id)`（本人默认向量）；前端 `ChapterEditorPage.tsx:731` 在 AI 页签对 canUseAI（含编辑者）挂载 `ContextPreviewPanel` → 编辑者打开会看到错误 |
| `index-status` | **对编辑者 404** | `kb_service.index_status` 639-656 用 `get_owned_project`；返回作者配置名 `cfg.name · cfg.model` |
| 费用提示 UI | **部分** | `KnowledgePanel.tsx:260-265` 已有「仅关键词/不产生作者费用」和「作者已授权…可能产生作者费用」两条 Alert；`knowledgeUpload.ts:39` 上传成功文案。授权模式实际不可用（`unavailable`） |

### 1.3 IndexJob / kb.index 记录真实 actor，修正 `project.user_id == task.user_id` 假设

`IndexJob` 定义：`kb_index_service.py:562-583`。已有 `task_execution_identity`、`selection`、`cipher/factory/visibility_stamp`、`document_claim` 字段，**没有**显式的 actor / grant 字段（可从 `task_execution_identity.actor_id/grant_identity` 推导）。

KB 链路中所有「作者 = 执行者」的假设（grep `project.user_id ==/!= …`、`get_owned_project`、`resolve_embedder(task.user_id)`）：

| 位置 | 假设 | E6 处理 |
|---|---|---|
| `kb_index_service.py:596` `validate_index_task` → `get_owned_project(lock=True)` | 受理只允许作者 | 编辑者走 `require_project(ai.use, grant)` |
| `kb_index_service.py:609` rebuild 检查 `default_embedding_config(project.user_id)` | 正确（作者配置） | 保留，改为由 `resolve_shared_embedding(require_vector=True)` 统一 |
| `kb_index_service.py:924` `enqueue_memory` → `project.user_id != actor_id → 404` | 仅作者 | 编辑者走新的受控入队（绑定 refs + `_shared_embedding` + grant） |
| `kb_index_service.py:953-959` bound memory 分支：`execution.grant_identity is not None → 拒绝`、`get_owned_project` | 仅作者 | 放开 grant，改用 `require_project(ai.use, grant)` |
| `kb_index_service.py:976` **`project.user_id != task.user_id → 404`**（文档 / 重建 / 旧记忆） | implement.md E6 点名的假设 | 改为按 execution 判定：作者 ⇔ grant 为空；编辑者 ⇔ grant + 冻结选择 |
| `kb_index_service.py:989` `resolve_embedder(task.user_id)` | 用任务主人的默认向量（编辑者会变成**编辑者本人**的模型，违反设计） | 改为冻结 `ResolvedSharedEmbedding` → 作者配置 |
| `kb_index_service.py:1124-1155` `require_bound_index_job`：`selection.project_owner_id != execution.actor_id`、`grant_identity is not None` → 拒绝 | 仅作者 | 放开为「作者无 grant / 编辑者有 grant」的一致性检查 |
| `kb_index_service.py:675-682` `_verified_analysis_index_source`：`grant is not None` 或 actor≠owner → 拒绝 | 仅作者 | 放开（0051 收据表已有 `grant_invitation_id/revision` 列，`kb_index_service.py:832-857` 已写入） |
| `retrieval_service.py:1194-1216` `store_owner_legacy_embedding_snapshots`：actor≠owner 或有 grant → `EmbeddingPolicyError` | 仅作者写旧键 | 编辑者写入需要新变体，或放宽为「受控执行者写作者空间」（取决于 Q1） |
| `memory_service.py:301-304` `may_enqueue_vectors` | 仅作者 | 同 `enqueue_memory` |
| `kb_service.py:122,145,174` 搜索 / 索引时的作者分支 | 正确（区分作者与编辑者的入口） | 保留，并增加授权分支 |
| `kb_service.py:639-641` `index_status` `get_owned_project` | 仅作者 | 见 Q8 |
| `context_builder.py:1031-1034` `retrieval_preview` `get_owned_chapter` + 本人 embedder | 仅作者 | 编辑者走共享选择 |
| `kb_service.py:628` `knowledge_for_project` 用 `project.user_id` 的 embedder | 只能给严格作者调用方 | 调用方：`wizard_service.py:1061`（作者分支）、`entity_ai_service.py:1184`（`execution is None`）、`inspiration_service.py:528`（525 行作者判定）——目前都只在作者路径上 |
| `agent/service.py:1375` `enqueue_memory`、`agent/tools/read.py:330` `resolve_embedder` | Agent 仅作者（E5a） | E6 不动 |

已经是真实 actor 的部分：`task_service.create_task` 写入 `Task.user_id=user_id`；分析派生子任务 `user_id=execution.actor_id`（`kb_index_service.py:852`）；`_enqueue_memory_locked` 的 `user_id=actor_id`。

### 1.4 0045 迁移（文档绑定任务 UUID 与入队代次）

| 子项 | 状态 | 位置 |
|---|---|---|
| 迁移文件存在 | **已做**（未提交） | `backend/alembic/versions/0045_kb_index_identity.py`：`tasks.execution_key`（≤36，CHECK）、`kb_documents.index_task_key/index_task_queued_at/index_claim_key`（三列全空或全非空，CHECK） |
| 有损降级保护 | **已做** | `downgrade` 45-60：存在任何文档占用或 `execution_key` → `RuntimeError("仍有索引占用或执行轮次，拒绝有损降级…")` |
| 备份排除 | **已做** | `project_transfer/registry.py:503` `exclude=("status","chunk_count","index_task_key","index_task_queued_at","index_claim_key")`；`APP_SCHEMA="0052"`（`exporter.py:35`） |
| 受理绑定 / 领取轮换 / 释放 | **已做** | `task_service.py:605-609` → `bind_queued_document_claim`（`kb_index_service.py:196`）；引擎 `activate_document_claim`（302）；`release_document_claim`（426）；`on_abort`（1363-1380） |
| 旧作者任务兼容 | **已做（E4-e）** | `capture_legacy_document_task` / `cleanup_legacy_unclaimed_document_task`（`kb_index_service.py:451-556`）；引擎 `_fail_legacy_document_queued`；用户取消 `_cancel_legacy_document_task`；重试 409 `kb_legacy_task_not_retryable`（`handlers/kb.py:38-41`）；`queued_at IS NULL` 的更早任务仍保持排队（已退避，不热循环） |
| 测试 | **已有** | `test_kb_index_identity_migration.py`、`test_kb_index_identity_postgresql.py`（PG 1 passed）、`test_kb_index_claims.py`、`test_kb_legacy_document_cleanup{,_postgresql}.py`、`test_project_transfer.py` 注册表用例 |
| 未覆盖 | — | 0045 升降级在 PG 上**只有**专用用例；「清理提交确认丢失」的故障注入没有单独用例（implement.md E4-e 遗留） |

结论：0045 本身不是 E6 的阻塞项；E6 只需复用，不需要新迁移（除非 Q1 选择新的共享空间迁移/回填）。

### 1.5 编辑者维护知识库 / 记忆：路由开放与权限检查

| 路由 | 服务端检查 | 编辑者现状 |
|---|---|---|
| `GET /kb/settings` | `content.read` | 200 |
| `PATCH /kb/settings` | `content.edit` + 父行锁 | 200 |
| `GET /kb/documents`、`GET …/{id}/chunks` | `content.read` | 200 |
| `POST /kb/documents`（上传） | 路由：`capture_content_write_actor` + `require_project(ai.use)`；服务：`lock_content_write_actor` + `_locked_embedding_actor` | 无授权 → 202 + 关键词 / `task=null`；授权 → **409**；viewer → 403 |
| `PATCH /kb/documents/{id}`（注入开关） | `content.edit` + 父行锁 + `set_project_flags` | 200 |
| `DELETE /kb/documents/{id}` | `content.edit` + 父行锁；有活动任务时 409 | 200（可以删除作者建立的向量，属于人工维护） |
| `POST …/{id}/reindex` | `ai.use` + `_require_vector_task_owner` | 无授权 403 / 授权 409 |
| `POST /kb/rebuild` | 同上 | 无授权 403 / 授权 409 |
| `POST /kb/search` | `ai.use` + `keyword_search_access` | 无授权 → 关键词；授权 → 409 |
| `GET /kb/index-status` | `get_owned_project` | **404** |
| `GET/POST/PATCH/DELETE /memories…` | `content.read/edit`；写路径 `capture/lock_content_write_actor` | 200；不派生向量 |
| `POST /memories/search` | `ai.use` + `keyword_search_access` | 同 KB 检索 |
| `GET /chapters/{id}/context-preview` | `get_owned_chapter` | **404** |

---

## 2. E4-h（KB 每批写 / 向量发送前的重鉴权，撤权失败关闭）

| 路径 | 发送前 | 每批写 | 撤权失败关闭 | 可达性 |
|---|---|---|---|---|
| 分析派生的 bound memory（`_run_bound_memory` `kb_index_service.py:1202-1240`） | **受控**：`for_task(execution, selection, stamp, embedding_inputs=snapshots)` + `prepare_provider`（发送 lease 内 `_verify_locked` → `require_sending_embedding_snapshots`） | **受控**：`guard.require_task_write` + `store_owner_legacy_embedding_snapshots`（批量 CAS） | 是 | 仅作者 |
| 文档模式（`_run_document` 1261-1310 → `_embed_stale` 1045-1100） | **不受控**：`embedder.embed(...)`（1088 附近），embedder 来自 `resolve_embedder(task.user_id)`，无 SendGuard / lease | 只有 `_lock_running_document`（287-300：项目 active 锁 + Task CAS + 文档 claim），**不重验认证、令牌、成员、授权**；写入用旧 `store_embeddings`（1094），无来源快照 CAS | 部分（只靠 claim 和 Task 条件） | 仅作者（976 守卫） |
| 重建 / 旧 memory 模式（`_run_vectors` 1313-1328） | 不受控 | **完全不重验**（`document_claim is None` 时 `_embed_stale` 不调用 `_lock_running_document`） | 否 | 仅作者 |
| `_refresh_doc_statuses`（1331-1349） | — | 无锁、无重验，直接改所有 ready 文档状态 | 否 | 仅作者 |
| `KbIndexHandler.verify_event / verify_finalization`（`handlers/kb.py:71-87`） | — | 只检查 `same_execution`；文档 / 重建模式不调用任何鉴权 | — | — |
| 引擎领取期 | `_collaboration_claim_failure`（`engine.py:803-830`）对带 grant 的任务核对账号、令牌、accepted editor、原邀请轮次和 `task_requires_owner_ai`（`collaboration_task_access.py:38-50` 已识别 `_shared_embedding` 非 keyword）；`kb.index` 在 `_COLLABORATION_QUEUED_CLEANUP_KINDS`（`engine.py:141-144`）中，失效时释放文档 claim | — | 是 | 引擎侧对 `kb.index` 协作任务已就绪 |
| 运行中撤权 | E4-d `notify_collaboration_change` → 数据库复查 → `cancel_running`（通用，适用于 kb.index） | — | 是 | 通用 |
| 发送 / 写原语 | `require_sending_document_claim`（386-423）、`require_writing_document_claim`（360-383）、`require_sending/writing_embedding_snapshots`（`retrieval_service.py:1034, 1148`）已接入 `SendGuard._verify_locked`（`collaboration_ai_guard.py:477-500`）和 `require_task_write`（775-800） | — | — | 测试：`test_kb_document_claim_send_e6.py`、`test_kb_document_guard_integration_e6.py`、`test_kb_document_guard_e6_postgresql.py`、`test_collaboration_embedding_snapshot_send_e6.py`；**没有生产调用方**（文档模式） |

结论：E4-h 的唯一实际缺口是**文档 / 重建 / 旧记忆模式的 `_embed_stale` 及其状态写入**。原语已有测试，需要把 `_embed_stale` 改为「锁内快照 → 事务外 guard 发送 → `require_task_write` + 批量 CAS 写」，并让 `verify_event / verify_finalization` 对文档模式重验 claim 和 grant。e4-reauth-inventory §E4-h 已建议与 E6 合并。

---

## 3. E5 遗留中与 KB 相关的项

| 遗留 | 现状 | E6 后建议 |
|---|---|---|
| 协作者订阅 `kb.index` 任务 → 入口 404 | `stream_evidence.stream_kind_open`（`stream_evidence.py:457-473`）只放行实现了 `stream_guard` 的 handler；`KbIndexHandler` 没有该方法；`SendGuard.require_task_read` 显式拒绝 `_document_claim` / `_embedding_inputs`（e5 inventory：`collaboration_ai_guard.py:541-544`）。测试 `test_task_sse_scope.py::test_collaborator_stream_uses_guarded_pipeline_and_closed_kinds_stay_404` 断言协作者 `kb.index` 订阅 404、详情 200 | **建议保持 404**（Q5）。kb.index 帧只有进度文本，不含正文；文档列表（`content.read`）和任务详情 / 输出已可轮询。开放需要新增 `stream_guard` 并扩展 `require_task_read` 的读证明协议，成本高、收益低。前端已对非白名单类型只显示状态（E5-g，`COLLABORATOR_STREAM_KINDS`） |
| `book.import` 协作者订阅 404 | 拆书是作品外的个人任务，build_job 拒绝 grant | **保持关闭**，不属于 E6 |
| 协作者检索只用关键词 | 见 §1.2 | **授权开启时开放**：KB / 记忆检索调试、`context-preview`、`knowledge_for_http`、大纲 / 实体 / 向导 / 分析派生上下文；**无授权时保持关键词** |
| 全局任务面板中其他共享作品的 KB 任务订阅 404 提示 | 已补说明文案（implement.md E5-g 复核） | 保持 |

---

## 4. 前端现状与 E6 调整

### 现状（E5-g）

- `utils/projectAccess.ts`：`canUseAI`（作者或 accepted 编辑者）与 `canUseOwnerAI`（仅作者）两档。
- `pages/project/knowledgeUpload.ts:9-17` `knowledgeUploadMode`：作者 `owner`；编辑者无授权 `keyword`；编辑者已授权 `unavailable`；其余 `none`。
- `KnowledgePanel.tsx`：
  - `useIndexStatus(..., enabled: canUseOwnerAI)`（90）、`IndexStatusBanner`（259）、单文档「重新索引」Popconfirm（223）、`KbSearchDebug`（321）都只对作者显示。
  - 上传按钮：`canUpload = canEdit && (owner|keyword)`（62）。
  - Alert：keyword 模式提示「不会调用作者的向量模型，也不会产生作者向量费用」（260-262）；`unavailable` 模式提示「作者已授权…可能产生作者费用」（263-265，但实际不能上传）。
  - 编辑者看到 `ready_keyword_only` 文档时的提示：「如需向量索引，请联系作者」（193-194）。
- `MemoriesPanel.tsx`：`IndexStatusBanner`（266）和 `MemorySearchDebug`（391）只对作者显示；人工编辑按 `canEdit`。
- `ChapterEditorPage.tsx:731`：AI 页签对 canUseAI 挂载 `ContextPreviewPanel` → 编辑者调用 `/chapters/{id}/context-preview` 得到 404（现存问题）。

### E6 后建议

1. `knowledgeUploadMode` 增加 `shared`（编辑者 + `allowOwnerAI`）：允许上传，返回 `task` 时跟踪。Alert 改为确认式费用提示：「将使用作者的向量模型建立索引，外部费用由作者的 API 账户承担；任务计入你的并发额度」。
2. 重新索引 / 重建按钮：作者照旧；授权编辑者显示，并用 Popconfirm 写明费用来源；无授权编辑者隐藏或禁用，说明需要作者授权（与服务端 403 `owner_embedding_not_allowed` 对应）。
3. `IndexStatusBanner` 对编辑者开放精简版（见 Q8：不显示作者配置名，只显示「作者向量模型：已配置 / 未配置」和待建数量）。
4. 检索调试（`KbSearchDebug` / `MemorySearchDebug`）对 canUseAI 开放；结果 `mode` 为 keyword 时显示降级原因（未授权 / 作者未配置 / 存在未建向量的资料）。
5. 费用提示统一放在三处：上传区 Alert、重建 / 重新索引确认框、检索调试面板顶部一行小字（授权模式：「查询会调用作者向量模型」）。授权关闭时，前台 15 秒轮询（`startCollaboratorAccessPolling`）会刷新快照，UI 回到 keyword 模式。
6. `ContextPreviewPanel`：后端开放前先对非作者隐藏；开放后按 canUseAI 显示。
7. 编辑者文档列表的状态轮询：不能依赖作者专用的 `useIndexStatus`。建议在有本人 pending/indexing 文档时轮询 `GET /kb/documents`，或复用任务面板的详情轮询（kb.index 订阅 404）。

---

## 5. 切片建议（按依赖排序）

> 通用门禁：每片完成后 `ruff`、受影响文件 + 哨兵测试（按「精简回归策略」），PG 用例单文件顺序运行；负对照通过备份→改动→`shasum -c` 恢复的方式完成。

### E6-a 受理与 IndexJob 身份（后端，不开放入口）

- **改动面**
  - `task_service.create_task`：为 `kb.index` 增加不依赖文本模型的协作受理分支。当前带 grant 的受理以 `accepted_model is not None and collaboration_execution_ready` 为前提（`task_service.py:550-556`），否则走 `get_owned_project`。服务端在锁内调用 `resolve_shared_embedding(for_update=True)`，覆盖保留键 `_collaboration_grant`（`bind_task_grant`）和 `_shared_embedding`；客户端传入的保留键已由 `_contains_reserved_task_key` 拒绝。
  - `kb_index_service.validate_index_task`：作者沿用原逻辑；编辑者改为 `require_project(ai.use, for_update=True, grant)`。
  - `build_index_job`：删除 976 行的 `project.user_id != task.user_id`；以 `execution` 为准（作者 ⇔ grant 为空，编辑者 ⇔ grant + 冻结的 vector 选择）；编辑者不调用 `resolve_embedder(task.user_id)`。`IndexJob` 显式携带 `actor`（`AuthenticatedActor`）、`grant_identity`、`selection`。
  - `KbIndexHandler.validate_retry`：带 grant 的任务拒绝重试，或按原 grant 重新受理（不能续签旧轮次）。
  - 仍**不**设置 `collaboration_execution_ready`。
- **关键测试**：编辑者任务的 `Task.user_id`、`_collaboration_grant`、`_shared_embedding` 均由服务端写入；伪造保留键被拒；作者受理行为逐字不变；引擎领取期对协作 `kb.index` 的撤权、降级、关闭模型授权和重邀会失败并释放文档 claim（复用 `test_collaboration_task_revocation_e4.py` 的参数化）。
- **PG**：受理与撤权两种提交顺序（撤权先提交 → 受理 403/404 且无 Task、无占用；受理先提交 → 撤权后领取失败关闭，占用释放）。
- **负对照**：恢复 976 行的作者判断 → 编辑者 build 失败；去掉服务端覆盖 `_shared_embedding` → 伪造用例失败。
- **风险**：`task_service.py` / `engine.py` 是中央文件，需要独占。

### E6-b 文档 / 重建 / 记忆索引的受控发送与每批写（= E4-h）

- **改动面**
  - `_embed_stale`：每批改为「短事务锁内 `snapshot_embedding_inputs` → 释放 → `for_task(execution, selection, stamp, document_claim=…, embedding_inputs=snapshots)` 发送 → 新事务 `guard.require_task_write` + 批量 CAS 存储 → commit」。存储函数取决于 Q1，参考 `_run_bound_memory`（`kb_index_service.py:1202-1240`）。
  - 作者路径同样迁移（Q6），作者的 selection 可用 `_author_embedding_selection` 类似构造（`generation_service.py:326`）。
  - `_run_document` 的首尾状态写和 `_refresh_doc_statuses` 在 `require_task_write`（或 `require_writing_document_claim`）下进行。
  - `verify_event / verify_finalization` 对文档 / 重建模式也重验 claim、grant 和绑定。
  - 语义：provider 普通故障 → `ready_keyword_only` + 警告（作者原行为）；`VisibilityPolicyError / EmbeddingPolicyError` → 失败关闭，不降级（规范 `retrieval.md` 已规定安全拒绝优先）。
- **关键测试**：多批发送在第 k 批前撤权、降级、关闭模型授权、作者换向量配置（A→B→A）、来源正文被改、文档被删除或重新索引（claim 换代）时，后续批次零请求、零写入，已提交批次保留，任务失败，文档 claim 释放；作者路径回归（`test_kb.py`、`test_retrieval.py`、`test_kb_index_claims.py`、`test_kb_legacy_document_cleanup.py`）。
- **PG**：写批次与撤权的两种顺序（`pg_blocking_pids` 证明阻塞）；HTTP 响应头前后撤权（参照 `test_kb_document_guard_e6_postgresql.py`）；重建与新上传 / 重新索引并发。
- **负对照**：去掉每批 `require_task_write` → 撤权后仍写入的用例失败；去掉快照 CAS → 来源改动用例失败。
- **风险**：作者行为可能变化（受控后作者遇到可见性代次变化会失败，而不再静默继续）；SQLite 下每批多一次写锁，吞吐下降。

### E6-c 开放编辑者向量索引入口（依赖 a、b）

- **改动面**
  - `upload_document`：授权且作者有向量配置 → 创建编辑者名下的 `kb.index` 任务（`resolve_shared_embedding` 结果冻结进任务）；授权但作者无配置 → 同步关键词（Q2）；无授权保持现状。
  - `reindex_document` / `start_rebuild`：授权编辑者可受理（rebuild 见 Q3）；无授权 403 不变。
  - `memories.py:_enqueue_vectors`：授权编辑者 → 新的受控入队，绑定本次 memory ref（Q4）。
  - 分析派生：放宽 `_verified_analysis_index_source`、`require_bound_index_job`、`build_index_job` bound 分支和 `analysis_service.py:1882` 对 grant 的限制；新增编辑者存储变体。
  - `KbIndexHandler.collaboration_execution_ready = True`。
- **关键测试**：翻转 `test_collaboration_shared_index_e6.py::test_authorized_editor_does_not_silently_start_unfenced_vector_task`（改为创建编辑者任务，向量请求使用作者配置，发送次数正确）；编辑者有个人向量配置时仍使用作者配置；关闭授权后排队 / 运行中任务失败关闭；任务计入编辑者并发额度（`rbac-quotas`）；分析派生的子任务 `user_id` 为编辑者，收据包含 grant。
- **PG**：上传受理与撤权竞争；分析完成收据与撤权竞争（参照 `test_analysis_memory_enqueue_postgresql.py`）。
- **负对照**：编辑者任务使用 `resolve_embedder(task.user_id)` → 「个人模型不被使用」用例失败。

### E6-d 同步检索与上下文（可与 c 并行，依赖 Q1）

- **d1 HTTP 检索**：`keyword_search_access` 增加授权分支 → `for_http` guard + 冻结选择；实现 `kb_service.knowledge_for_http`（转正 `test_shared_http_knowledge_e6.py`，并按 Q1 调整其 fixture 中的键）；`context_builder.retrieval_preview` 支持编辑者（`require_project(ai.use)` + 共享选择）；`index_status` 对编辑者开放精简投影（Q8）。
- **d2 任务上下文统一**：大纲、实体、向导切换为冻结的共享向量选择（向导需要在受理时新增 `_shared_embedding` 冻结；规范注明「向导未冻结共享向量选择」）；翻转规范 `retrieval.md:126-128` 列出的三组零向量断言。
- **关键测试**：授权时一次查询嵌入使用作者配置；关闭授权后零发送且返回 403/409；撤权发生在 `prepare_provider` 之后、发送之前时零请求（xfail 第二个用例）；无授权时仍零请求；作者路径不变。
- **PG**：检索嵌入发送 lease 与撤权（参照 `test_collaboration_retrieval_postgresql.py`、`test_collaboration_retrieval_cache_preflight_postgresql.py`）。
- **负对照**：去掉 guard 的 `_verify_bindings` → 撤权用例出现外发请求。

### E6-e 任务订阅（可选）

- 推荐保持 `kb.index` 订阅 404（Q5），只需在规范和 implement.md 中记录。若裁决开放：为 `KbIndexHandler` 增加 `stream_guard`，扩展 `require_task_read` 接受文档 claim 和向量输入的帧证明，并修改 `test_task_sse_scope.py` 的断言。

### E6-f 前端（依赖 c、d1）

- 按 §4 调整 `knowledgeUpload.ts`（新增 `shared` 模式）、`KnowledgePanel`、`MemoriesPanel`、`ContextPreviewPanel` 和 `api/types.ts`（`IndexStatus` 精简投影）。
- **测试**：`knowledgeUpload.test.ts`、`KnowledgePanel.upload.test.ts`、`api/kb.test.ts` 的模式矩阵（作者、无授权编辑者、授权编辑者、作者无配置、viewer、管理员、管理员兼编辑者）；授权中途关闭后刷新快照、UI 回到关键词模式；真实浏览器分别以作者、授权编辑者、无授权编辑者走一遍。

---

## 6. 需用户裁决的问题（附推荐答案）

| # | 问题 | 选项 | 推荐 |
|---|---|---|---|
| Q1 | 编辑者写入作者空间时用哪种向量键？现状：作者写 `provider:model:dim`；`shared:v2:<sha>` 没有生产写入方；章节用旧键，实体用 `shared:v2`，大纲两者都试；xfail 测试期望 `shared:v2` | A 统一写作者旧键（与作者现有索引同空间，配置一致性由 guard 在发送和写入时重验）；B 全量迁移到 `shared:v2`（作者也写新键，需要一次全书重建，旧索引在迁移前退化为关键词） | **A**：V1 只有一个作品空间，B 会让所有作者现有索引立即失配。同步把实体 / 大纲的检索键统一为旧键，并修改 xfail fixture |
| Q2 | 授权编辑者上传，但作者没有向量配置 | 同步关键词（与无授权相同）/ 409 | **同步关键词**，提示「作者未配置向量模型」 |
| Q3 | 授权编辑者能否触发全书重建（会替换所有旧模型向量，费用较高） | 按设计允许 / 仅作者 | **按设计允许**，并加费用确认框；可再加「同项目同时只有一个 rebuild」（已有 `kbrebuild:{pid}` 资源键） |
| Q4 | 授权编辑者人工记忆的向量化范围 | 只针对本次写入的 memory ref / 补齐全书所有空向量 | **只针对本次 ref**，费用归属清楚，不替他人的关键词行花作者的钱 |
| Q5 | 协作者订阅 `kb.index` 是否开放 | 保持 404 / 开放 | **保持 404**，使用详情和文档列表轮询 |
| Q6 | 作者自己的文档 / 重建 `_embed_stale` 是否也迁移到受控快照路径 | 迁移 / 保留旧路径，仅编辑者走新路径 | **迁移**（满足 E4-h「每批」要求，避免两套写入逻辑）；作者 provider 故障仍降级为关键词 |
| Q7 | 编辑者新增的关键词行会让整书混合检索退化为关键词，直到补齐向量 | 接受，并向作者提示「有 N 条资料待建向量」/ 修改检索为部分混合 | **接受 + 提示**（修改检索算法超出 V1 范围） |
| Q8 | `index-status` 是否对编辑者开放 | 开放精简投影（不含作者配置名）/ 保持作者专属 | **开放精简投影**，`embedding_model` 对非作者返回 null，另加 `owner_embedding_configured: bool` |
| Q9 | 大纲 / 实体 / 向导 / 分析派生的上下文切换到作者向量，是否放进 E6 | 放进 E6（d2）/ 推迟 | **放进 E6**（设计 §6.2「所有检索入口都遵守」，规范已列出需要翻转的断言） |
| Q10 | 无授权编辑者能否对失败文档做「仅关键词重新分块」 | 不能（403，按设计）/ 允许同步关键词重新分块 | **按设计 403**；失败文档可以删除后重新上传 |
| Q11 | 编辑者文档任务遇到 provider 普通故障 | 降级为 `ready_keyword_only` / 失败 | **降级**（与作者一致），策略类错误失败关闭 |

---

## 7. 相关文件

| 路径 | 用途 |
|---|---|
| `backend/app/api/v1/kb.py`、`memories.py` | 路由与前置检查 |
| `backend/app/services/kb_service.py` | 上传、注入、删除、重新索引、检索、索引状态、`knowledge_for_project` |
| `backend/app/services/kb_index_service.py` | 0045 占用、`IndexJob`、受理、`build_index_job`、`_embed_stale`、bound memory、旧任务清理 |
| `backend/app/services/memory_service.py` | 记忆人工维护与检索 |
| `backend/app/services/retrieval_service.py` | embedder、空间键、快照、受控存储、`search` |
| `backend/app/services/project_ai_config.py` | `resolve/revalidate_shared_embedding` |
| `backend/app/services/collaboration_ai_guard.py` | `for_task/for_http`，文档 claim 与向量输入校验 |
| `backend/app/services/task_engine/handlers/kb.py`、`engine.py`、`task_service.py`、`stream_evidence.py` | handler、领取复核、受理、订阅白名单 |
| `backend/app/services/{generation,outline_ai,entity_ai,wizard,analysis,inspiration}_service.py`、`context_builder.py` | 检索消费者 |
| `backend/alembic/versions/0045_kb_index_identity.py`、`project_transfer/registry.py:503` | 迁移与备份排除 |
| `frontend/src/pages/project/{KnowledgePanel.tsx,MemoriesPanel.tsx,knowledgeUpload.ts}`、`components/chapter/ContextPreviewPanel.tsx`、`hooks/useIndexStatus.ts` | 前端入口 |
| `.trellis/spec/backend/retrieval.md:120-140` | 协作者检索现行口径与 E6 翻转清单 |

现有 E6 / KB 测试：`test_collaboration_shared_index_e6{,_postgresql}.py`、`test_shared_http_knowledge_e6.py`（strict xfail）、`test_kb_document_claim_send_e6.py`、`test_kb_document_guard_integration_e6.py`、`test_kb_document_guard_e6_postgresql.py`、`test_collaboration_embedding_snapshot_send_e6.py`、`test_bound_memory_index_e6{,_postgresql}.py`、`test_analysis_index_receipt_enqueue_e6.py`、`test_collaboration_memory_write_e6{,_postgresql}.py`、`test_collaboration_shared_context_e6.py`、`test_memory_index_actor_e6.py`、`test_collaboration_retrieval_{core,callers,cache_preflight}{,_postgresql}.py`、`test_kb*.py`、`test_memories.py`、`test_retrieval.py`。

## 8. Caveats / Not Found

- 未运行任何测试；文中「已做」以代码和已有测试文件为依据，不代表当前工作树测试通过。
- 大纲 / 实体协作执行「只用关键词」以规范 `retrieval.md:126-127` 为准；代码中 `entity_ai_service.py:1037-1055` 和 `outline_ai_service.py:556-581` 的 `shared:v2` 分支看起来属于作者执行路径，没有逐行追到分支条件，实施 d2 前需要再确认。
- 上传在持有作品写锁（SQLite 下为全库写锁）期间解析文件（`kb_service.py:308-324`，最大 20MB），作者和编辑者都是如此；这是现状，没有评估其耗时。
- 没有核实 `IndexStatus` 前端类型和 `useIndexStatus` 的全部调用点。
