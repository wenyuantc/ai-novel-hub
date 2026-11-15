# 阶段 E：个人任务与订阅独立复核

2026-11-10 在独立 `collab_tasks` 测试库（PostgreSQL 16.15、随机 schema）执行 `AINOVEL_COLLAB_TASKS_POSTGRES_TEST_URL=<专用测试库 URL> uv run pytest -q tests/test_collaboration_task_postgresql.py --tb=short`：**7 passed in 22.54s**。此结果是任务引擎 0045 执行身份接线前的基线；引擎修改后必须重跑。

0045 执行身份接线后，与任务领取和协作通知专项合并错峰复跑：**10 passed in 101.95s**。后续章节 claim 会再次修改任务服务和引擎，届时仍需以最终代码复验。

## 已核对与修复

- 列表在 `LIMIT` 前按真实 actor、账号状态、作品生命周期、accepted 成员及活动任务原邀请轮次过滤；详情、输出、列表投影和 SSE 帧在返回前再次验证本人、原 task UUID、作品、活动 grant 与入队代次。管理员人工能力不授予私人任务权，accepted viewer 可读取本人已结束记录。无稳定 UUID 的旧行不向用户投影，系统统计初始化可为真实遗留任务建立 UUID；不能以整数 ID 猜测其身份。
- 中断前已有的 6 项专项修复在本次聚焦组合中重新运行：完整 JSON 输出与首帧私人引用脱敏、个人无作品任务取消前原 token 版本重验、取消响应绑定原 UUID、无 UUID 的记录隐藏，以及 SSE 绑定原作品和入队轮次。结构化投影不能可靠清理任意自由文本或分片数字；生成源仍须避免发出他人的私人标识。
- 本轮构造真实 HTTP 反例并修复：旧取消请求在获取任务后等待时，若另一个请求已通过 HTTP 续跑同一 UUID，旧请求不再取消新的 `queued_at` 轮次，而以 409 `task_round_changed` 失败。任务重试、续跑、结束、取消和删除在取写屏障后检查原 grant 和入队代次；排队与孤立运行取消的状态 CAS 限定原 `queued_at`。`TaskEngine.cancel_running` 新增可选 `expected_queued_at`，用户取消传入原代次，以免仅凭 UUID 取消进程中新轮次。
- 仅含个人无作品任务的批量清理也必须获取 User 认证写屏障，失效原 token 返回 401；预检候选时使用 `no_autoflush`，防止未经授权的 ORM 暂存先行写入。任务详情／输出／列表区分认证失效 401 与协作撤权 404；列表最终查询仍位于身份复验之后，避免在最终任务投影之后额外 await 造成旧授权输出。

## 验证

在 `backend/` 下、SQLite 独立数据上执行：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_task_review.py tests/test_collaboration_task_privacy.py \
  tests/test_tasks.py::test_cancel_running_task tests/test_tasks.py::test_cancel_queued_task \
  tests/test_tasks.py::test_list_filters_delete_and_clear \
  tests/test_task_statistics_identity.py::test_old_cancel_running_cannot_cancel_replacement_execution \
  tests/test_task_statistics_identity.py::test_stale_cancel_cannot_cancel_reused_task_or_invent_old_terminal
# 35 passed in 41.79s；测试启动后才给孤立 running 状态 CAS 增加 queued_at，需另验最终代码。
```

- 随后以最终代码重跑 `tests/test_tasks.py::test_cancel_running_task`、`tests/test_collaboration_task_review.py::test_cancel_does_not_target_resumed_round`（两种身份）及 `tests/test_collaboration_task_review.py::test_bulk_clear_denial_does_not_flush_unrelated_pending_writes`：4 passed in 3.50s。
- 本范围 `uv run ruff check app/services/collaboration_task_access.py app/services/task_service.py app/services/task_engine/engine.py app/services/task_engine/hub.py app/api/v1/tasks.py tests/test_collaboration_task_review.py tests/test_collaboration_task_privacy.py tests/test_collaboration_task_postgresql.py` 通过；所改 Python 文件 `compileall` 和 `git diff --check` 通过。本项目未配置独立 Python type checker，不能以编译冒充类型检查。
- `test_collaboration_task_postgresql.py::test_postgres_task_round_and_personal_clear_barriers` 包含真实 HTTP 取消与续跑、撤销 token 先提交、批量清理先取 User 锁时撤销等待三种顺序；后来与领取和通知组合在 0045 接线后实跑通过，见本文开头。环境使用隔离库与随机 schema：

  ```sh
  env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 AINOVEL_COLLAB_TASKS_POSTGRES_TEST_URL='<本机 collab_tasks 专用测试库 URL>' \
    uv run pytest -q tests/test_collaboration_task_postgresql.py
  ```

  未设置专用变量时 fixture 回退标准 `AINOVEL_COLLAB_POSTGRES_TEST_URL`；只接受本机对应测试数据库。不能把未运行的 PostgreSQL 测试视为通过。

## 未完成与后续接口

以下为当时的复核快照：通知提交后接线、0045 执行身份以及提案读取已有后续实现和专项测试，分别见 `phase-e-engine-fencing.md`、通知记录及 `phase-e-review-claim.md`；章节接线和最终整体验证仍在进行，不能将这些历史缺口或专项通过直接等同于最终状态。

- `TaskEngine.notify_collaboration_change(project_id, actor_id)` 当前仅实现订阅者唤醒，协作变更成功提交后尚未接线，且本进程执行体尚未收到撤权停止信号；跨进程仍靠出队/心跳复查。必须在撤销、降级和授权收紧的真实提交确定后接入通知，并保证数据库持续为权威。
- `TaskEngine._claim_and_run` 提交确认未知后只按原 task UUID、`queued_at` 和 running 状态核验，另一个进程的同轮领取可能被误认。`0045` 正设计 `Task.execution_key` 和资源 claim；后续需将原子领取、结果写入、取消与 cleanup 都绑定新的唯一执行身份。
- 孤立 running 的 `on_abort` 在更新任务终态前运行，虽然最终 UPDATE 已限制原 UUID 与 `queued_at`，没有资源 claim 时尚不能证明回滚属于原执行体。章节和知识库状态清理必须在对应 claim 集成后做 CAS；不得把本轮任务读取隔离报告为 E3/E4/E5 完成。
- `rich_document_review.get_review` 的初查后 payload 读取和映射之间存在可观察的撤权窗口，已交另一代理在该服务中修复并验证；当前不得宣称提案读路径已闭环。
- `personal_projection` 的共享向量内部 `_shared_embedding` 保留键和最终引用复查由独立日志隐私代理处理；本轮没有修改该文件。模型发送、协作者任务受理及派生、数据库失效恢复和完整后端回归仍待集成。

## 客户端持有的任务身份（2026-11-10 补充）

- 实测发现：请求开始前整数任务 ID 已被复用时，原实现会忽略旧客户端传入的 key，以新任务的相同 ID 读取其正文或执行操作。真实 HTTP 红灯中，旧客户端详情请求收到 200 和新任务 UUID。
- `TaskOut.task_key` 从 `Task.statistics_key` 投影，未知身份允许 null；创建、列表和其他合法 TaskOut 是客户端取得凭据的入口。任务详情、输出、SSE、取消、重试、续跑、结束、单项删除，以及拆书 skip 必须携带原响应中的 query `task_key`：缺失 422，错误或无权 404。SSE 初连与重连都使用原 key，不依据当前整数 ID 补取；retry 返回新任务和新 key，resume 保留任务 key。
- 富文档取消/结束 body 的 task_key 与 query 不一致时拒绝；单项删除的 `task_key` 为基础必填字段，额外的 `run_key`、`token` 必须同时提供。本人可见 TaskOut 与 `rich_review` 的 key 在最终资格投影中保留；任意无证实上下文的嵌套 key 由私人投影屏蔽。列表与按条件批量清理不以客户端旧 ID 指定单个目标，沿用其权限和批量筛选机制。
- 历史任务空值期间禁用动作；现有 `task_statistics_service.initialize_tracking` 可为真实存量行补发 UUID，首次向本人输出后可用来识别当前任务行。补发的 UUID 不能证明补发前的章节占用、Agent 消息/提案关联或旧执行体，这些历史关联仍须具有原始 claim 或 `turn_task_key` 等独立凭据；保留现有统计基线机制。
- 在 `backend/` 下 `.venv/bin/pytest -q tests/test_task_client_identity.py` 曾获 **24 passed**；随后加强有效 SSE 帧及富文档嵌套 key 的断言，针对 `test_owner_reads_and_streams_with_original_task_key` 和 `test_delete_basic_key_and_optional_review_identity` 重跑 **2 passed**。并行引擎改动仍在进行，集成最终代码后须再跑全文件。
- `test_book_import.py` 已迁移其任务流、详情、重试、结束及 skip 调用，始终从启动/列表/暂停响应保留身份，原业务断言不变。7 个测试函数、含参数化共 **9 passed**。新 API、schema 与上述测试 Ruff 和 Python `compileall`、`git diff --check` 通过；项目无独立 Python 类型检查器。
- 新增 `tests/test_task_client_identity_postgresql.py`，从首次任务列表持有 key，另一应用提交复用 ID 后检查输出、SSE、删除、skip；2026-11-10 在 PostgreSQL 16.15 本机 `collab_tasks` 隔离库实跑 **4 passed in 28.06s**。任务服务及引擎仍在继续集成，需在最终代码复跑。专用隔离库命令：

  ```sh
  # 先设置 AINOVEL_COLLAB_TASKS_POSTGRES_TEST_URL 为本机 collab_tasks 专用测试库 URL。
  .venv/bin/pytest -q tests/test_task_client_identity_postgresql.py
  ```

- 既有大量测试及 PostgreSQL 协作流仍在迁移必填 key；此前无 key 的旧测试通过记录不能代表新契约的最终回归。前端接线和 Agent 历史关联凭据由其他负责人处理；未取得原任务凭证时禁用相应动作。
