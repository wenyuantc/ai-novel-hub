# 知识库索引运行身份：0045 基础切片

## 已实现与范围

- `0045_kb_index_identity` 原位为 `tasks.execution_key` 增加可空运行 UUID，为 `kb_documents` 增加可空 `index_task_key`、`index_task_queued_at` 和 `index_claim_key`。文档三项必须全空或全非空，两个字符串长度均为 36；任务执行键为空或长度 36。服务内部还要求规范 UUID；数据库长度检查本身不是完整 UUID 校验。
- SQLite 采用带约束的 `ADD COLUMN`，不重建有外键引用的任务和文档父表。`downgrade` 在任一文档占用或任务执行键仍存在时拒绝，不能通过清空运行态绕过排空。单书备份排除三个文档占用字段；导入即使收到夹带的旧运行态也不恢复。新备份 `app_schema` 为 `0045`。
- `kb_index_service.DocumentIndexClaim` 是不可变的服务端身份，记录任务 ID、统计 UUID、真实 actor、作品/文档、原入队时间、占用 UUID 和排队/运行阶段；不是权限凭据。
- `bind_queued_document_claim` 只可在服务端鉴权、持作品父行锁且任务已 flush 的受理事务调用，由服务端提供本次随机 queue claim；它对无真实入队时间或无 UUID 的历史任务失败关闭。`activate_document_claim` 在同事务 queued→running CAS 后，以领取者新 execution UUID 轮换文档 claim；绑定失败必须整体回滚领取。调用方在提交确认未知时必须按本次 execution UUID 核实，不能仅凭 running 推断自己成功。
- `document_claim_is_current` 以一次 SQL 核对任务 UUID、actor、作品、文档、入队时间、运行 UUID、请求归属和取消意图；`release_document_claim` 条件清理原 claim，更新固定安全错误，不清新任务占用。调用清理前必须由协调器证明原执行体已停止；不能用本进程状态推断其他进程已退出。
- 同一文档被取消的旧任务与新排队任务之间，旧 claim 即使延迟清理也不能匹配新 claim。无法定位旧任务/确认其终态时不覆盖已有占用。真实生成任务、后台发送/写入及协作成员授权尚未接入这些方法。

## 集成方必须完成

1. `task_service` 在任务统计 UUID 和 queued_at 已实际 flush 后、同一受理事务生成随机 queue claim 并绑定；不得从客户端拿 `_shared_embedding` 或 claim，须以服务端原模型选择覆盖保留键。
2. `TaskEngine` 在认证/User、项目锁之后生成每次领取的 execution UUID，将 Task 条件更新与 `activate_document_claim` 同事务提交；未知提交只按原 UUID 查事实。单进程部署与旧版本执行器排空是现有前提，不承诺多实例崩溃自动恢复。
3. 每批真实发送与写事务仍须分别按项目门禁校验 actor、同一邀请轮次、模型绑定、任务 UUID/执行 UUID、文档 claim，并采用检索层不可变快照与批次 CAS。`document_claim_is_current` 不能替代这些检查，也不能跨事务缓存其结果。
4. 文档终态、失败和中断应由同轮 CAS 完成；Task.execution_key 的清理/续跑需由引擎在事务中协调，避免旧执行体和新任务互相覆盖。现有 `on_abort`、索引出站与正文写入仍是旧实现，**不得据此开放协作者索引或部署**。
5. 旧作者任务可能经统计初始化后补 UUID，无法证明它就是升级前原执行者。`queued_at IS NULL` 的旧任务及缺失身份在此原语中不认领；排空旧进程后应由明确的新受理流程创建新任务，不能把后补 UUID 伪装成原占用。

## 验证

2026-11-10 在本机 `collab_test` 隔离数据库（PostgreSQL 16.15、随机 schema）运行 `AINOVEL_COLLAB_POSTGRES_TEST_URL=<专用测试库 URL> uv run pytest -q tests/test_kb_index_identity_postgresql.py`：**1 passed in 3.47s**。该结果验证 0045 升降级和占用数据保护，不代表索引执行与撤权竞争已通过。

在 `backend/`：

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_kb_index_claims.py tests/test_kb_index_identity_migration.py \
  tests/test_project_transfer.py::test_registry_covers_all_project_tables \
  tests/test_project_transfer.py::test_registry_handles_every_fk_and_json_column \
  tests/test_project_transfer.py::test_registry_import_order --tb=short
# 12 passed in 8.13s

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_kb.py::test_upload_index_keyword_only tests/test_project_transfer.py::test_round_trip_all_entities --tb=short
# 2 passed in 2.04s

uv run ruff check app/models/entities.py app/services/kb_index_service.py \
  app/services/project_transfer/registry.py app/services/project_transfer/exporter.py \
  alembic/versions/0045_kb_index_identity.py tests/test_kb_index_claims.py \
  tests/test_kb_index_identity_migration.py tests/test_kb_index_identity_postgresql.py
# All checks passed!
```

新增 `test_kb_index_identity_postgresql.py` 使用专用 `AINOVEL_COLLAB_INDEX_POSTGRES_TEST_URL`（本机 `collab_index`）或标准 `AINOVEL_COLLAB_POSTGRES_TEST_URL`（本机 `collab_test`）和随机 schema，覆盖升级、父子行保留、完整性约束及两种降级拒绝。按主任务错峰安排，本切片**尚未运行该 PostgreSQL 测试**；尚需写/运行真实双连接任务领取、取消/新受理以及发送/写入竞争测试。SQLite 不证明 PostgreSQL 并发，也不代表 E6 已完成。
