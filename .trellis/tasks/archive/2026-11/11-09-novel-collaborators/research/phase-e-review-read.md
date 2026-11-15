# 富文档提案读取的身份与撤权复核

2026-11-10 在本机 `collab_test` 测试库（PostgreSQL 16.15、随机 schema）运行 `AINOVEL_COLLAB_POSTGRES_TEST_URL=<专用测试库 URL> uv run pytest -q tests/test_collaboration_review_read_postgresql.py --tb=short`：**2 passed in 20.24s**。测试覆盖 READ COMMITTED 与 REPEATABLE READ 下的授权变化；引擎和提案写入接线后仍须做最终全量回归。

## 已实现范围

- `GET /tasks/{id}/rich-proposals/{token}` 初次读取任务和提案后，以不可变值保存真实任务 UUID、项目、状态、入队时间和原协作 grant，以及提案 ID、章、run UUID、token、状态、内容版本和持久载荷头。任务和提案的初查与 payload 装载使用 `no_autoflush`，不因读取提前保存调用方的其他待写实体。
- 原始候选分块解码、格式映射和响应构建完成后，使用独立数据库会话的单条 SQL 再次验证本人、原始认证令牌版本、账号状态、项目生命周期、任务和提案身份及状态。初查时任务处于活动／待审核状态的，最终仍要求当前 editor 且原邀请 UUID／revision 一致，即使期间任务被置为终态也不会用新授权续签旧响应；普通作者与仍接受的查看者读取本人已结束记录的既有语义保留。
- 最终 SQL 同时验证提案章节属于原作品，且载荷中的全部场景均属于该章；分组条件在一次 SQL 快照内执行。提案状态、任务入队代次或存储头变化时，旧响应返回匿名 404；身份令牌失效或账号停用返回 401。损坏载荷的业务错误也先做同样身份复核，防止撤权后借 409 区分提案状态。
- PostgreSQL `REPEATABLE READ` 请求的原事务不能复查最新授权；最终查询在新会话中复制请求认证时捕获的 `AuthenticatedActor`，不能用数据库当前 token_version 冒充原请求。最终复核后不再 await 业务操作才返回已构建结果；已发送内容不能撤回。

## 验证

- 先用真实 HTTP 构造阻塞：读取分块后在另一客户端提交撤权，原代码返回 200 并泄露候选；现改为 404。单行历史提案伪造外书场景的反例原先同样返回 200，现不返回外书正文。另先复现载荷错误／撤权竞态原先返回 409，修复后返回 404。
- `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_review_read.py --tb=short`：**16 passed in 27.49s**；覆盖撤销、降级、修改授权、重新邀请、令牌撤销／禁用、任务 UUID／入队轮次／状态变化、提案身份／状态／载荷变化、同书章节与跨书场景、管理员重叠、本人终态及未提交修改。
- 旧功能聚焦：`uv run pytest -q tests/test_rich_document_proposals.py::test_proposal_requires_confirmation_then_atomically_applies_once tests/test_rich_document_proposals.py::test_proposal_chunks_round_trip_and_corruption_never_applies tests/test_rich_document_proposals.py::test_missing_proposal_chunk_closes_stream_on_early_rejection tests/test_rich_document_proposals.py::test_edit_during_review_keeps_stale_proposal_readable_but_cannot_apply --tb=short`：**4 passed in 7.10s**。
- `uv run ruff check app/services/rich_document_review.py tests/test_collaboration_review_read.py tests/test_collaboration_review_read_postgresql.py`：通过；`git diff --check -- backend/app/services/rich_document_review.py`：通过。
- PostgreSQL 双应用／双连接 `READ COMMITTED` 与 `REPEATABLE READ` 专项已编写并成功收集 **2 项**，覆盖读取期间撤销、降级、重邀、令牌变更、任务 UUID／入队时间／提案轮次变化和未变化控制；按照主协调要求尚未执行，不能当作通过：

  ```sh
  cd backend
  env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
    AINOVEL_COLLAB_POSTGRES_TEST_URL='<本机 collab_test 专用测试库 URL>' \
    uv run pytest -q tests/test_collaboration_review_read_postgresql.py --tb=short
  ```

  测试允许本机 `collab_review` 专库的 `AINOVEL_COLLAB_REVIEW_POSTGRES_TEST_URL` 或上述标准 `collab_test` 回退，均使用随机 schema 和严格连接校验。

## 未覆盖

本切片只收紧已有提案的读取，不开放协作者 AI 任务创建、提案采纳或执行；采纳的锁后原 grant、任务执行身份和章节 claim 尚属后续 E 阶段工作。最终全后端和实际 PostgreSQL 专项仍需执行。提案内容属于用户原有数据，服务不会推断或清洗自由文本；跨书场景检查只能验证持久场景身份，不能证明任意人工篡改文本的来源。
