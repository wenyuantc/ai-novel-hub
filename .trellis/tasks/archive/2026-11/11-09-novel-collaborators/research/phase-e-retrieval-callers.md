# E6 检索维护调用的项目边界

## 范围与反例

本切片只修已有人工记忆、知识库维护、备份恢复和章节号平移，不开放协作者 AI 或向量索引。数据库中 `(source, ref_id)` 是全局唯一但没有来源外键；已有损坏行可以让 A 书的写操作覆盖、改标记或删除 B 书的索引。实际 HTTP/服务反例先失败：文档注入与删除、记忆修改与删除、跨书章节记忆、来源缺失、401 条以上批次，以及建章和同步细纲在失败后被调用方捕获并提交的部分修改。

## 已实现的调用约定

- `validate_project_source_refs` 按最多 400 个 ID 批量验证来源及既有索引归属；记忆若有关联章节，也必须同书。检查 SQL 标量与会话中待修改的身份列，不先自动 flush；缺失、跨书和脏身份均返回匿名 `retrieval_reference_invalid`。调用方必须先持相应项目写屏障。
- `upsert_project_sources` 先验证所有来源再同步检索行，`set_project_flags`、`delete_project_refs` 均在任何写入前整体校验。`require_unindexed_sources` 用于刚创建的来源，禁止认领同书或外书旧孤儿。旧 `upsert_many`、`set_flags`、`delete_refs` 仍为历史原语，新的业务调用不得绕过有界入口。
- 手动记忆、重新分析、导入与 Agent 恢复、章节删除/改号都在来源删除或标记变化前校验。新建记忆和分析同步使用保存点，插入后发现旧孤儿时回滚本操作；备份导入在建立任何分块前验证新记忆 ID，整份导入事务失败并回滚。
- 文档注入、删除和重新索引在改变文档或删除分块之前校验整个分块集合。文档和检索标记不在失败路径中改变。
- 章节整体平移在任何章节号 UPDATE 前验证受影响章节、记忆和有章号的检索来源。同步细纲先计算真实删章和平移区间；保留最后占位的删章没有平移，不检查其范围之外的索引。建章同样只检查实际有位移的区间。两种服务把快照、解绑、绑定、平移和后续投影放在内层保存点；错误即使被外层直接调用者捕获并提交，也不会保存该操作的中间步骤。
- 规划和检索预检均处于 `no_autoflush`。在验证通过之后 SQLAlchemy `begin_nested()` 按其语义可能先 flush 调用方此前已暂存的独立更改；该保存点保护的是此次操作自身的变更。项目行无值更新保证 SQLite 外层物理事务存在，Agent `TransactionSession` 仍只 flush，外层审批保存点继续有效。

## 本地验证

在 `backend` 运行（环境需设置 UTF-8 locale）：

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_outline_pro.py tests/test_collaboration_plan_delete_guard.py \
  tests/test_collaboration_planning_no_autoflush.py --tb=short --disable-warnings
```

35 项通过。检索调用、原检索内核和章号平移此前组合 49 项通过；规划新增专项 12 项覆盖普通及 Agent 会话、提前冲突、后续注入冲突、调用方捕获后提交、无平移和待提交关联不自动 flush。最终改动后，定向重跑原规划、删除守卫和 Agent 细纲工具共 20 项通过；原 Agent 章节恢复另有针对性回归通过。

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_retrieval_callers.py::test_plan_preflight_does_not_flush_staged_binding_changes \
  tests/test_collaboration_retrieval_callers.py::test_plan_shift_conflict_cannot_commit_partial_changes \
  tests/test_collaboration_retrieval_callers.py::test_plan_no_shift_does_not_reject_unrelated_bad_index \
  --tb=short --disable-warnings
```

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest --collect-only -q \
  tests/test_collaboration_retrieval_callers_postgresql.py
```

真实 PostgreSQL 双应用/连接的 6 项测试已在本机 `collab_test` 隔离库执行；与检索发送专项合计 **9 passed in 49.24s**。测试分别覆盖文档开关/删除先后次序、章节平移以及细纲同步/建章等待父行锁后重验。执行须设置 `AINOVEL_COLLAB_RETRIEVAL_POSTGRES_TEST_URL` 指向测试夹具允许的本机 `collab_retrieval` 数据库，或使用 `AINOVEL_COLLAB_POSTGRES_TEST_URL` 指向本机 `collab_test`；不能用生产库。

```sh
uv run ruff check app/services/plan_service.py app/services/chapter_numbering.py \
  app/services/memory_service.py app/services/kb_service.py app/services/retrieval_service.py \
  app/services/project_transfer/importer.py tests/test_collaboration_retrieval_callers.py \
  tests/test_collaboration_retrieval_callers_postgresql.py
```

Ruff 与本次受控文件的 `git diff --check` 通过。

## 尚未完成

旧 `search` 的缓存候选和最终正文读取尚未逐条复验业务来源；现有作者向量执行 `_embed_stale` 仍使用旧 `store_embeddings` 和无快照的孤儿清理。须由后续 E6 集成同时接入原任务身份、出站 guard、已绑定的共享配置、来源快照及批量 CAS，处理撤权和提供方故障语义，并重跑真实 PostgreSQL 竞争测试。本切片不能证明这些路径安全，也不能据此开放协作者索引或宣布 E6 完成。
