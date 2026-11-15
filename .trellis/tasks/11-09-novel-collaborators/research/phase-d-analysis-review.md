# 阶段 D 分析状态与历史复核

## 范围和修复

人工分析状态、历史与详情先要求作品内容读取与作者或已接受成员身份；纯管理员不能借作品人工权限读取分析或私人任务。管理员兼已接受成员、查看者和编辑者按其真实成员身份处理：查看者仍可读取本人已结束任务，活动协作任务只属于创建时的 editor 授权轮次，不能观察其他人的任务。作者原有权限保留。

- 在全部后续异步读取完成后，使用独立事务的一条 SELECT 同快照核对真实账号启用和原认证令牌版本、作品生命周期、当前作者或已接受成员身份，以及需要显示的任务。没有可见任务引用时仍执行此授权检查；拒绝认证或成员身份分别返回 401 或 404。
- 活动及历史私人任务引用保留首次读取到的 `statistics_key`、`queued_at`、项目和章节身份，以及活动任务原始协作 `invitation_id/revision`；最终快照核对当前任务、当前成员与原快照，避免任务 ID 复用、重新入队、旧授权换签、降级或撤销时错误暴露。状态接口只显示最终仍为活动状态的原任务；原先活动的任务即使中途结束也不能借 viewer 资格续签其私人引用。
- 历史任务引用还检查所属项目及章节。存量空任务 UUID 不能证明原引用身份，仅在响应中隐藏任务引用/活动状态，不改分析正文或数据库记录。最终验证使用独立事务且不会自动 flush 调用方会话。
- 最终 SQL 先按作品和 actor 过滤当前任务，再与本次引用快照比对，避免按大量任务 ID 构造一个超出数据库参数上限的最终查询。

## 验证

新增 `backend/tests/test_collaboration_analysis_task_visibility_postgresql.py`：隔离 PostgreSQL schema、真实 HTTP 请求及另一事务的不同 backend PID，使用 `asyncio.Event` 在首次投影后、最终验证前暂停。覆盖授权修订、降级、重邀后成员与任务授权事实变化、任务终态与 ID 复用、重新入队、撤权、无引用撤权和令牌失效；同时显式设置并检查请求事务为 READ COMMITTED 或 REPEATABLE READ。另有作者空 UUID 兼容用例，以及新增的真实 HTTP 撤销→重邀→接受用例；前者通过数据库直接提交授权事实变化，后者通过 HTTP 和审计写入分别确认三个操作均使用另一条连接。

在 `backend/` 下使用（测试库须为独立本机 `collab_test`）：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest --collect-only -q tests/test_collaboration_analysis_task_visibility_postgresql.py
# 原先收集 15 项，另增真实 HTTP 重邀用例 2 项。

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 AINOVEL_COLLAB_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_test' uv run pytest -q tests/test_collaboration_analysis_task_visibility_postgresql.py
# 主代理实际运行原有 15 项：15 passed in 61.92s。

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 AINOVEL_COLLAB_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_test' uv run pytest -q tests/test_collaboration_analysis_task_visibility_postgresql.py::test_analysis_rechecks_after_postgres_http_reinvite
# 新增真实 HTTP 重邀用例：2 passed in 17.69s。

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_analysis_task_visibility.py tests/test_collaboration_planning_no_autoflush.py tests/test_collaboration_planning_access.py::test_analysis_reads_hide_other_actors_tasks_and_manual_writeback tests/test_collaboration_planning_access.py::test_analysis_rejects_inconsistent_project_children tests/test_collaboration_planning_access.py::test_admin_and_collaborator_use_combined_manual_capabilities tests/test_collaboration_planning_access.py::test_shared_agent_services_remain_author_only
# 26 passed in 57.23s。

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check app/services/analysis_service.py tests/test_collaboration_analysis_task_visibility.py tests/test_collaboration_analysis_task_visibility_postgresql.py
# All checks passed!
```

后端未配置独立 Python 类型检查器；SQLite、静态检查和 PostgreSQL 的结果必须分别报告。本记录只覆盖分析读取切片，不代表阶段 D/E 的集成验收。上述私人任务和最终读取快照约束应由主代理在功能整体完成时同步到对应 `.trellis/spec/`。
