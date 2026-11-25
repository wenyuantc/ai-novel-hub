# C6 实施与验收

以 [PRD](prd.md) 与[设计](design.md)为准。在当前工作分支核对 C1 至 C5 提交、工作目录状态及唯一迁移头，再启动已经存在的 C6 子任务。

## 实施顺序

1. [x] 确认最新方案后启动 C6。沿用当前包含 C1 至 C5 的工作基线。使用 C6 的 implement 与 check 上下文交接给相应 Trellis 子代理。
2. [x] 接入第七项系统设置。先验证默认值、严格 PATCH、恢复部署值、版本和权限、审计字段与迁移往返。覆盖只有旧设置的数据库，以及覆盖和历史审计分别阻止降级的情况。
3. [x] 加入维护错误与事务内判断。依次覆盖新建、重试、继续、直接改为排队的富文档批量采纳、拆书、知识库和智能体。追踪每处任务创建和恢复写入，核对草稿、提案、断点及资源没有部分提交。验证设置保存与受理两种顺序。
4. [x] 核对原运行凭据的派生。控制单章成功回调及分析索引的执行点，验证排空时能完整结束。补充人工操作可选派生、计划备份和手动备份的分支。
5. [x] 接入管理员只读状态和前端。有权限时显示开关、活动和收尾计数、备份与存活进程。覆盖跨实例读、权限变化、过期状态与刷新不覆盖草稿。沿用受理 API 错误处理和请求成功后清输入的逻辑，验证章节、批量、智能体三个入口。
6. [x] 运行完整验收，记录 SQLite、PostgreSQL 16、双 Uvicorn 进程和真实浏览器证据。更新系统设置、监控、审计规范及 README。
7. [x] 按确认的分组提交，更新父任务 C6 条目。归档与日志由 Trellis 记录。

实施和评审按需读取 `.trellis/spec/backend/error-handling.md` 的错误类型和 API 响应、`.trellis/spec/backend/database-guidelines.md` 的事务和原子状态迁移、`.trellis/spec/backend/quality-guidelines.md` 的隔离数据库和迁移夹具，以及 `.trellis/spec/frontend/type-safety.md` 的系统设置与 API 契约。

## 验收步骤

| 验收项 | 应观察到的结果 |
| --- | --- |
| 受理与并发（AC1、AC2、AC4） | 专项测试使用独立应用、独立连接池和同一数据库。阻断重载通知，验证 A 保存后 B 的下一次请求、预检后翻转、先受理后开启、关闭后恢复。保存前后对比任务、资源、断点、提案、知识库与假模型的调用数。 |
| 已受理工作（AC3、AC5） | 两个真实 Uvicorn 进程共用隔离 SQLite，假模型可控阻塞。预置 3 个排队和 1 个运行任务，打开开关后确认处理完成。故意暂停成功回调，在另一进程读到收尾占用，恢复后见派生任务入队、结束和最终归零。已打开的 SSE 跟随到终态。核对两份租约归并成真实的进程列表，停止一个进程后消失。PostgreSQL 16 用同一业务断言覆盖写屏障和真实数据库读值。 |
| 浏览器（AC6） | 从管理端保存开关并查看状态自动刷新。章节编辑、批量弹窗和智能体输入显示维护提示，保留已录入的内容及选项。关闭功能后无需刷新页面即可手动重试。检查提案决定保存后的轮次继续入口、窄屏以及只读管理员账号的控件状态。 |
| 开放接口与备份（AC7、AC8） | 断言 API Key 只读路由和成功响应不变。执行前后对比备份行和到期时间，验证暂停计划、已受理继续、手动完成和恢复计划后的去重。 |
| 兼容（AC9） | 两库执行当前 head 到新 head 往返，读回旧业务记录和既有设置。分别构造排空覆盖、仅残留排空审计的库验证拒绝降级。默认关闭时完成 SQLite 全量和相关专项。 |

## 验证命令

在 `backend/` 执行。专项文件名在落地后随验收记录核对。

```sh
uv run ruff check .
uv run alembic heads
uv run pytest -n 0 -q tests/test_task_drain*.py tests/test_system_settings*.py
uv run pytest -n 0 -q tests/test_tasks.py tests/test_task_admission.py tests/test_task_retry_admission_api.py tests/test_admin_task_cancellation.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -n 4 --dist loadfile --durations=20
```

将 `AINOVEL_POSTGRES_TEST_URL` 指向隔离 PostgreSQL 16 测试库，从 `backend/` 串行运行本任务的数据库专项和设置跨进程用例。按实际运行情况另查知识库、智能体、富文档、拆书和备份的关联用例。双进程演练沿用 C5 的隔离启动方式，C6 的专用命令和结果见[验收记录](research/verification.md)。

在 `frontend/` 执行。

```sh
npm run lint
npm run build
npm test
```

收尾检查 `git diff --check`、任务验收勾选与规范。任务开关关闭时按现有流程继续受理。需要撤回已经使用排空设置的版本时按设计使用配套快照，生产启动与部署验证遵循父任务 C7、C8。
