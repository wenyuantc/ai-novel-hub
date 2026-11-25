# C6 验收记录

## 真实双进程

`process_c6.py` 启动两个独立 Uvicorn 进程，同用临时 SQLite 主库和租约侧库。外部模型由本地 HTTP 服务模拟，任务受理、调度、授权、流转发与管理状态使用应用真实入口。

1. 实例 A 保存前有 3 个排队和 1 个运行任务。打开排空后，两实例通过 HTTP 提交新任务都得到约定的 503。查询数据库仍只有原 4 条任务，被拒章节保持草稿，模型仅收到已运行任务的调用。
2. 运行任务写入成功后将其回调暂时挂起。实例 B 此时返回 3 个排队、0 个运行、1 个收尾。释放回调后，原订阅收到终态，三个排队任务和派生分析成功完成，计数归零。
3. 实例 B 关闭排空后，A 的下一次受理成功。存活列表合并每个进程的两份登记。停止一个进程后，从剩余进程读取列表确认原进程消失。
4. 排空期间通过有效 API Key 成功读取小说。开放接口专属 OpenAPI 仍只包含读操作。

## 浏览器

Chrome 158 在前端生产构建上通过同一隔离双进程环境验证。管理员保存开关后，另一进程的只读会话展示有效值及存活列表，375px 宽度无横向溢出。随后关闭开关，已打开的只读页自动更新状态和开关显示。

章节编辑器的润色要求、批量生成的附加要求，以及智能体对话输入在真实 503 之后保留。智能体可在关闭排空后直接重试并收到模型替身的应答。公开配置刷新与只读设置的首次加载曾发生竞争，修复后使用独立请求生命周期，追加了真实 Zustand 发布回归并重新通过 Chrome。

浏览器也实际生成了一份待确认的智能体提案。在排空期间拒绝提案后，决定保存且原轮次仍待审核。「继续本轮」返回维护提示，关闭排空后继续原轮次并收到应答。

## PostgreSQL 16

在隔离的 PostgreSQL 16.15 测试库和用例专用 schema 中串行运行，36 项通过。覆盖新建受理及并发先后、模型缺失时的错误顺序、旧轮次拒绝与真实派生、知识库和拆书原子性、智能体提案提交后设置读取故障、手动与计划备份、排空状态投影、设置跨应用发布以及迁移往返和降级保护。备份专项使用 PostgreSQL 16 的原生 `pg_dump`。

```sh
uv run --no-sync pytest -n 0 -q -rs \
  tests/test_task_drain_admission.py tests/test_task_drain_edges.py \
  tests/test_task_drain_status.py tests/test_task_drain_migration.py \
  tests/test_system_settings_cross_process.py tests/test_system_settings_publication_races.py
```

## SQLite 全量与串行复验

最终产品代码的 SQLite 全量收集 7,503 项。八个 pytest worker 按文件分配，首轮 7,417 项通过、54 项按条件跳过、19 项预期失败，13 项需核对测试控制点。

- 12 个向导用例用 `settings_confirmed=False` 暂停领取，被 C6 按持久设置故障拒绝。改用只暂停调度领取的夹具，保留心跳、原任务身份和模型结果断言。向导执行身份、续写身份和向量取用相关整文件串行共 58 项通过。
- 作品配额竞争用例等待各请求进入原 User 写锁。拆书开始先进入和设置保存共用的授权写门禁，SQLite 在该处等待。测试记录真实门禁入口，保留并发请求、单一赢家、配额报错及不消费导入凭据的断言。配额并发与 C6 外围入口串行共 32 项通过。

原 13 个失败用例复验全部通过。两阶段覆盖原 7,503 项，合计 7,430 项通过、54 项条件跳过、19 项预期失败。复验只调整测试的调度和锁观察点。

## 复现

在 `backend/` 执行。将 `C6_SCRIPT` 指向本记录同目录的 `process_c6.py`。

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONPATH=. uv run --no-sync python "$C6_SCRIPT"
```

使用 `--frontend ../frontend/dist` 同时跑 Chrome。先在 `frontend/` 执行 `npm run build`，确保 Chrome 和 Playwright 可用。可通过 `PLAYWRIGHT_PATH` 指向独立 Playwright 安装。截图可用 `C6_BROWSER_OUTPUT` 保存到仓库外目录。

## 已通过的静态检查和前端测试

- 前端 lint、生产构建和 Vitest 全量，264 个文件、1,849 项通过。
- 后端 Ruff、两份前端 TypeScript 配置的类型检查、Compose 配置校验和差异空白检查通过。Alembic 唯一 head 为 `0070`。

## 收尾复验

2026-11-25 核对功能提交 `018faa6`。后端 26 项排空专项通过，1 项 PostgreSQL 迁移用例按环境条件跳过，已在此前的 PostgreSQL 16 专项通过。前端 78 项设置及智能体回归通过。Ruff、迁移头与 Git 差异检查通过。
