# 阶段 E6：共享检索内部基础

## 已实现的内部接口

- `snapshot_embedding_inputs(db, project_id, ids, *, allow_orphans=False)` 批量读取不可变 SQL 标量快照，按输入顺序返回；来源须是本作品的 `KbChunk → KbDocument` 或 `StoryMemory`，并核对检索冗余正文与业务来源。正文、向量与角色名不进入对象 `repr`；记忆发给向量模型的 `embed_text` 仅含记忆正文，不包含关键词用的角色名。缺失来源默认匿名 409；只允许显式生成孤儿快照供后续受限删除，跨作品来源始终拒绝。ID 使用 PostgreSQL INTEGER 的正数范围，重复及损坏身份不被接受。
- `load_bound_shared_embedding_config(db, selection, *, cipher, allow_private=False, for_update=False)` 在当前事务通过 `revalidate_shared_embedding` 重验原 actor、项目、邀请轮次、作者授权、当前作者向量配置和配置指纹；`for_update=True` 获取现有项目写屏障。返回的 `BoundSharedEmbeddingConfig` 包含内部加密配置字段，`repr` 不展示；不得向 HTTP、SSE、日志或不受控序列化输出。此函数不构造 provider、不发网络，调用方仍须按现有 SSRF/DNS 合同构建 provider。
- `shared_embedding_model_key(config)` 生成 `shared:v1:<SHA-256>`，绑定作者及配置 ID、provider、原始 endpoint、模型和加密密钥版本，不包含可读的 endpoint、模型、密钥或账号。密钥重新加密、endpoint 或配置变化可能保守触发重建；现有 `provider:model:dim` 旧向量原字节保留，但对新共享空间不视作可证明兼容。`_stale_condition` 对共享空间只接受非零、规范十进制维度，损坏 key 仍为 stale。当前旧作者 provider/model key 的既有行为尚未替换。
- `store_embedding_snapshots(db, snapshots, vectors, config)` 在调用方已确认任务 UUID、入队与执行/资源占用代次的**同一短写事务**中，锁内重验原项目和作者配置，再以确定顺序锁住检索行，重读并比对业务来源、版本、正文、旧向量及旧 key；每行更新条件再次包含业务来源与项目归属。整批使用连接级保存点，任何条件变化都会回滚该批并返回 `False`，不提交或自动重新选择模型；调用方须整体回滚自己的任务事务并重新评估原授权，不能将 False 视为任务成功。连接级保存点不触发无关 ORM 草稿自动 flush；上层事务回滚会撤销成功批次。`store_embedding_snapshot` 是单条包装。调用方在下一批/发送和最终状态写入前仍需复验权限与任务 claim。
- `delete_orphan_snapshot(db, snapshot)` 仅在来源当前在**任何作品均不存在**且检索行完整身份、正文、旧向量、key 和版本未变时删除；调用方必须先获取正确的认证及项目写屏障。不能以缺失当前作品来源误删属于他书的行。
- `validate_project_source_refs`、`set_project_flags`、`delete_project_refs` 是新的作品有界操作：先批量验证全部业务来源及现存检索行属于作品，再在 caller 已持有的项目写事务中有界修改，不提交；缺失来源与跨书归属不回显 ID。旧 `upsert_many` 另外增加已有 `(source, ref_id)` 行所属作品不一致时的匿名拒绝，避免直接覆盖他书检索行；授权失败前读取不 autoflush。
- `guard_embedder(..., authorize=..., verify=...)` 会保留已有可见性戳、认证和验证回调，同一真实 HTTP 发送 lease 中依序执行，不为旧负载续签；锁、数据库和本地策略错误统一为 `VisibilityPolicyError` 子类，不得按 provider 故障降为关键词。缓存命中也重验。`embed_query` 区分授权门禁正在等待时的超时和已经通过门禁后的普通 provider 超时：前者失败关闭，后者保留原关键词降级。封装依赖现有 `ai_dispatch._GuardedProvider` 私有实现，后续重构该类时须同步组合与真实传输测试。
- 缓存命中现在通过组合后 provider 的 `preflight()` 取得短事务中的真实 User／发送门禁、原可见性戳及原任务、章节和模型检查，随即释放；该预检不证明之后的网络调用。已注册的章节 wrapper 通过 `_bound_chapter` 验证后保留原 claim，再对新 `_GuardedEmbeddingProvider` 重新登记；显式指定另一 claim、伪造/篡改原包装或以无可信 SDK 的 provider 声称章节资格都失败关闭。可选 `chapter` 与缺失 embedder 不允许退回关键词。
- 受控 embedder 冻结 provider 对象及包装链、认证/验证回调、sessionmaker/engine/runtime、模型、提供方和向量空间；每次 await 前后重验。已生成缓存记录维度和内容摘要，返回向量使用副本，未经本执行体产生或被修改的缓存不返回。`search` 对受控 embedder 要求其可见性戳项目与目标一致，并在读前及返回前重走物理预检；索引 key 不匹配或零结果不再绕过撤权检查。未经包装的原作者检索路径不受这些预检语义影响。

## 边界与后续接入

- 本切片**没有开放协作者 AI、上传或索引任务入口**；不会把 `selection`、作者配置 ID、指纹或空间 key 当成客户端授权凭据。模型创建、队列受理与每次发送 lease、索引每批写、任务 claim 和结果保存必须全部由后续接入方绑定同一服务端授权轮次。
- 现有 `set_flags`、`delete_refs`、`store_embeddings` 和 `search` 的调用者尚未迁移到新作品有界或快照接口；旧 `search` 对损坏来源、业务正文/检索正文不一致仍须补源归属与返回前复核。`upsert_many` 仅防止已有检索行被另一项目覆盖，尚不自行验证新业务来源。不能据此宣称检索、索引或 E6 已完成。
- `delete_orphan_snapshot` 的 caller 同样须验证真实 actor、原 grant 及任务执行身份，且持项目锁；无锁直接调用不满足业务合同。不同来源对象被系统错误绕过项目锁直接篡改不属于同项目有序写入合同，但 SQL 更新仍附带源归属和原始内容条件。
- 通用 provider 真实发送已用本机 OpenAI 兼容 HTTP 验证回调组合和拒绝前零请求。2026-11-10 在本机隔离 PostgreSQL 16.15 的 `collab_test` 随机 schema 中，`test_collaboration_retrieval_postgresql.py` 与调用方 PG 文件合计 **9 passed in 49.24s**，覆盖内容编辑先后次序及真实 HTTP 发送、撤权。初始测试将屏障挂在 User 锁之后，导致循环等待；修正为通过 `pg_blocking_pids` 确认真实阻塞后释放响应头，保留正文未结束断言。后续发送原语改动后须重跑。Gemini、重定向及 SDK 多次尝试的共享授权/模型绑定仍须端到端验证。
- `guard_embedder` 对 `embed_query` 的授权门禁超时做专门识别；其他直接调用 `Embedder.embed` 的外层超时边界仍需在集成处确保不能把授权存储失败当普通网络失败。仅控制结构化向量数据，不能保证对自由文本的敏感信息自动清洗。
- 与发送原语协作后，`SendGuard.verify_without_one_shot` 仅接受同 actor／项目的真实预检 lease，仍重验原 grant、任务、章节、可见性与模型；缓存和关键词预检不消费一次性确认，真实 HTTP 每跳仍执行 `verify` 和确认。组合包装保留并冻结两条回调，附加验证同时覆盖两条路径；同一 bound method 会去重，额外的已知 `SendGuard.verify` 在预检映射为其对应的非消费方法。其他附加验证必须为无副作用的纯检查，不能自行发请求或消费一次性确认。

## 验证

以下命令在 `backend/` 下执行，使用独立临时 SQLite 测试数据库和本机 HTTP 服务：

```sh
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost uv run pytest -q \
  tests/test_collaboration_retrieval_cache_preflight.py tests/test_collaboration_retrieval_core.py \
  tests/test_retrieval.py tests/test_ai_visibility_dispatch.py::test_fake_provider_and_vector_fallback_do_not_accept_old_stamp
# 97 passed in 38.49s；包括真实 OpenAI 兼容 HTTP、共享模型和原章节 claim、缓存授权、取消、
# 绑定/缓存篡改、跨作品、关键词撤权、嵌套 guard 和 one-shot；旧 memory 夹具由测试负责人补齐真实来源。

uv run ruff check app/services/retrieval_service.py tests/test_collaboration_retrieval_core.py \
  tests/test_collaboration_retrieval_cache_preflight.py tests/test_collaboration_retrieval_cache_preflight_postgresql.py
# All checks passed!

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_retrieval_core.py tests/test_retrieval.py \
  tests/test_ai_visibility_dispatch.py::test_fake_provider_and_vector_fallback_do_not_accept_old_stamp
# 41 passed in 8.42s，含撤权及重新邀请不能复用旧共享向量选择

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_retrieval_core.py
# 27 passed in 14.39s；最后收紧内部配置对象类型后重跑

uv run ruff check app/services/retrieval_service.py tests/test_collaboration_retrieval_core.py \
  tests/test_collaboration_retrieval_postgresql.py
# All checks passed!

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest --collect-only -q \
  tests/test_collaboration_retrieval_postgresql.py
# 3 tests collected；仅收集，不是并发通过证据
```

隔离 PostgreSQL 实际验收命令（由主任务错峰运行）：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  AINOVEL_COLLAB_RETRIEVAL_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_retrieval' \
  uv run pytest -q tests/test_collaboration_retrieval_postgresql.py
# 也可仅设置标准 AINOVEL_COLLAB_POSTGRES_TEST_URL，使用 collab_test。
```

fixture 严格限定本机专用库和随机 schema；三项覆盖来源写先、向量写先，以及本机真实 OpenAI 兼容 HTTP 响应头前后撤权。尚不能代替跨实例多个索引任务、网络重试、最终结果写入和全部消费者的 E6 验收。

缓存预检新增两项 PostgreSQL 交错测试已由主会话错峰执行；与 guard PG 文件合计 **6 passed in 17.25s**：

```sh
NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost \
  AINOVEL_COLLAB_POSTGRES_TEST_URL='<本机 collab_test 专用库 URL>' \
  uv run pytest -q tests/test_collaboration_retrieval_cache_preflight_postgresql.py
```

分别验证先撤权时项目行锁后重读拒绝，以及缓存预检先持门禁时撤权等待预检结束；后者以数据库共享 advisory 锁及独占 try 失败证明物理竞争。该结果不能替代最终搜索结果来源一致性、网络重试和任务结果保存的后续联测。
