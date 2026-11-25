# C7 后端完整回归记录

## 第一轮：未通过

2026-11-25 在 `backend/` 执行，测试开始前移除 `AINOVEL_POSTGRES_TEST_URL`，使用本地 SQLite 与 UTF-8 locale：

```sh
env -u AINOVEL_POSTGRES_TEST_URL LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  uv run --no-sync pytest -n 4 --dist loadfile --durations=20 \
  --junitxml=/tmp/ainovel-c7-01a2104f-pytest.xml
```

结果：**4 失败、7,468 通过、76 跳过、19 xfailed**，耗时 2,777.94 秒。运行期间同时进行隔离容器和真实多进程验收；相关影响不能在没有证据时归因于负载。PostgreSQL 条件用例按环境跳过，C7 相关 PostgreSQL 16 专项另行执行。

第一轮从原 `backend/` 目录启动；事后确认 `conftest.py` 导入模块级默认应用时，配置会读取该目录现有 `.env`。没有查看或输出该文件；业务 fixture 使用独立临时数据库，代码中默认应用只创建惰性引擎，其启动期数据库操作位于 lifespan。缺少完整运行时审计，因此不能把这轮视为未读取项目配置的隔离验收。后续回归使用不含 `.env` 的独立源码副本，并将默认数据库显式指向临时 SQLite。

| 失败 | 当前处理 |
| --- | --- |
| `test_tasks.py::test_subscribed_task_deleted_ends_with_error[cancel-settings_overrides0]` | 删除期间订阅末帧实际为“任务不存在”，测试期望“任务已取消”。后续受控实验复现了两种合法次序，并为测试加入明确屏障；原失败的内部次序没有记录，不能确定其具体路径。 |
| `test_setting_categories_migration.py::test_0036_preserves_every_0035_value_anchor_and_revision_then_empty_downgrade[True]` | 旧断言期待迁移专用连接退出时仍启用 FK；C7 在迁移前关闭专用连接 FK 并在提交前检查。已调整测试观察点并保留原数据、模式、版本和完整性断言。 |
| `test_setting_categories_migration.py::test_0036_rejects_invalid_legacy_fk_before_rebuilding_any_table[True]` | 同一连接状态预期冲突；保留无效旧外键拒绝、原结构和版本不变的断言。 |
| `test_c3_schema_boundaries.py::test_0068_downgrade_observes_concurrent_committed_work[sqlite]` | 旧测试等待的版本表更新位于 C7 的 `BEGIN IMMEDIATE` 之后，与测试中已持有的写事务相互等待；已将观察点移到实际写锁起点，保留并发写入、拒绝降级及状态不变断言。 |

修正后，0036 全文件 23 项通过；0035/0036/0037 相关文件 38 项通过。修正后的 0068 相关文件在 SQLite 环境 7 项通过、4 项按环境跳过，在独立 PostgreSQL 16.15 库 11 项通过。完整回归在测试修正前已收集，不能将上述后续测试当作本轮通过，仍须重新完成完整回归。

任务流竞态已用受控屏障复现两种原有合法终态，测试修正保留两分支各自的结果与授权断言，详见[调查记录](c7_task_acceptance/race_investigation/verification.md)。在不含 `.env` 的源码副本中重新运行四个失败用例对应的参数，结果为 7 通过、1 项 PostgreSQL 参数跳过。

## 第二轮：主动中止，不作为完整通过

从当时 Git HEAD 导出源码后叠加本任务修改和新增 C7 测试，副本未包含项目 `.env`；复用现有 Python 虚拟环境，实际导入的 `app.main` 路径核对在副本内。对照当时工作区的后端受版本控制文件、C7 新测试及启动配置共 1,143 个文件，SHA-256 均一致。以干净环境、UTF-8 locale、专属临时目录及 `DATABASE_URL` 指向的专属 SQLite，运行 `uv run --no-sync pytest -n 4 --dist loadfile --durations=20`，收集 7,613 项。

第二轮启动后，构建上下文检查发现并修正了 `.dockerignore` 对本地数据库、伴随文件和 `.env` 副本的遗漏；新增手动脚本 `tests/c7_build_context_smoke.py` 使用全是假数据的隔离目录分别验证默认 BuildKit 及旧版构建器。随后 SQLite 恢复 CLI 的误导性成功提示也已修正并增加针对性测试；该产品文件和新测试未包含在第二轮快照中。因此在约 54% 主动中止，结果为 4,177 通过、22 跳过、6 xfailed，无失败标记，**不是完整回归通过**。最终源码需另起完整回归，最终镜像构建与启动也须单独复验。

## 第三轮：完整通过

在新的独立源码副本中纳入最终 `.dockerignore`、SQLite 恢复提示与对应测试后，重新以干净环境、专用临时目录及临时 SQLite 运行同一全量命令。实际导入路径在副本内，没有项目 `.env`。收集 **7,614 项**，结果为 **7,519 通过、76 跳过、19 xfailed，0 失败**，耗时 **2,357.31 秒**，另有 21 条警告。JUnit 核对 0 failures、0 errors；其中的 95 项 skipped 包含上述 19 项 xfailed。条件跳过不算通过，C7 PostgreSQL 专项结果另见下节。

本轮实际命令如下；临时快照路径只记录本次运行，重新执行时应创建新的无 `.env` 源码副本与临时目录：

```sh
cd /tmp/ainovel-c7-final.fqZ34hMhW1HZ/backend
env -i PATH="$PATH" LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  TMPDIR=/tmp/ainovel-c7-final.fqZ34hMhW1HZ/tmp \
  DATABASE_URL=sqlite+aiosqlite:////tmp/ainovel-c7-final.fqZ34hMhW1HZ/tmp/default-import.db \
  uv run --no-sync pytest -n 4 --dist loadfile --durations=20 \
  --junitxml=/tmp/ainovel-c7-final.fqZ34hMhW1HZ/backend-results.xml
```

运行结束后核对后端、C7 脚本及部署配置共 1,148 个文件：唯一差异是手动应用镜像探针 `tests/c7_application_build_smoke.py` 在快照后增加了隔离检查与专属容器卷清理；它不在 pytest 自动收集范围，也未被其他 Python 脚本导入，最终镜像探针另行执行。产品源码、自动收集测试和部署配置与快照一致。

## PostgreSQL 16 迁移专项：完整串行复验

2026-11-25 04:32:46 UTC 开始，使用 `/tmp/ainovel-c7-final.fqZ34hMhW1HZ/backend` 最终源码副本；已检查副本根目录和 `backend/` 均没有 `.env`。本轮创建唯一名称与标签的 `postgres:16-alpine` 专用临时容器及 `ainovel_test` 数据库；实际查询确认服务端为 **PostgreSQL 16.15（160015）**。容器配置 `max_prepared_transactions=10`，使预备事务防护测试不会依赖服务器默认禁用两阶段提交。只连接本轮容器的回环随机端口，不使用既有数据库、容器或卷。

测试进程仅保留必要的系统路径、UTF-8 locale 和本次测试变量；默认 `DATABASE_URL` 指向本轮临时 SQLite，`AINOVEL_POSTGRES_TEST_URL` 指向专属 PostgreSQL 库。实际执行如下，其中临时目录和连接串以占位符脱敏；密码通过本轮 0600 环境文件和测试进程环境传入，没有写入命令或本记录：

```sh
cd /tmp/ainovel-c7-final.fqZ34hMhW1HZ/backend
env -i PATH="$PATH" LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  TMPDIR="<本轮临时目录>" PYTHONPATH="$PWD" PYTHONDONTWRITEBYTECODE=1 \
  DATABASE_URL="sqlite+aiosqlite:///<本轮临时目录>/default.sqlite" \
  AINOVEL_POSTGRES_TEST_URL="<本轮专属连接串>" \
  .venv/bin/python -m pytest -n 0 -q -p no:cacheprovider \
  --basetemp="<本轮临时目录>/pytest" --junitxml="<本轮临时目录>/results.xml" \
  tests/test_c7_migration_postgresql.py --tb=short
```

退出码 **0**；JUnit 统计 **22 passed、0 skipped、0 failed、0 errors**。pytest 报告耗时 **89.40 秒**，外层计时 **94.50 秒**：

```text
......................                                                   [100%]
22 passed in 89.40s (0:01:29)
```

JUnit 每项结果如下，耗时单位为秒：

| 用例 | 结果 | 耗时 |
| --- | --- | ---: |
| `test_pg_three_processes_run_each_migration_once[empty]` | 通过 | 4.086 |
| `test_pg_three_processes_run_each_migration_once[old-with-data]` | 通过 | 4.653 |
| `test_pg_three_failed_migrators_leave_schema_and_data_intact[empty]` | 通过 | 5.397 |
| `test_pg_three_failed_migrators_leave_schema_and_data_intact[old-with-data]` | 通过 | 3.902 |
| `test_pg_rejects_autocommit_block_before_any_changes_commit` | 通过 | 3.417 |
| `test_pg_rejects_abort_before_it_breaks_migration_atomicity` | 通过 | 3.378 |
| `test_pg_rejects_start_or_prepare_transaction[start]` | 通过 | 3.250 |
| `test_pg_rejects_start_or_prepare_transaction[start-with-comment]` | 通过 | 3.387 |
| `test_pg_rejects_start_or_prepare_transaction[prepare]` | 通过 | 3.631 |
| `test_pg_rejects_start_or_prepare_transaction[prepare-with-nested-comment]` | 通过 | 3.524 |
| `test_pg_allows_regular_prepared_statement` | 通过 | 3.366 |
| `test_pg_rejects_transaction_escape_with_sql_prefix[semicolon]` | 通过 | 3.432 |
| `test_pg_rejects_transaction_escape_with_sql_prefix[comment-semicolon]` | 通过 | 3.328 |
| `test_pg_rejects_transaction_escape_with_sql_prefix[bom]` | 通过 | 3.448 |
| `test_pg_rejects_transaction_escape_with_sql_prefix[mixed]` | 通过 | 3.257 |
| `test_pg_rejects_transaction_escape_after_nested_comments[two-levels]` | 通过 | 3.514 |
| `test_pg_rejects_transaction_escape_after_nested_comments[three-levels]` | 通过 | 3.458 |
| `test_pg_rejects_transaction_escape_after_nested_comments[line-and-nested]` | 通过 | 3.654 |
| `test_pg_rejects_transaction_escape_after_nested_comments[carriage-return]` | 通过 | 3.599 |
| `test_pg_allows_ddl_with_nested_comment_prefix` | 通过 | 3.430 |
| `test_pg_killed_migrator_releases_lock_after_waiting_over_five_seconds` | 通过 | 11.927 |
| `test_pg_current_stamp_and_downgrade` | 通过 | 4.239 |

本次测试前后以下文件 SHA-256 一致，测试前也与工作区对应文件一致：

| 文件 | SHA-256 |
| --- | --- |
| `tests/test_c7_migration_postgresql.py` | `48ba4e25d4dd581293f7aa64087e5b2fb12e20ccd9a5d9b5889fbc86a4809178` |
| `tests/test_c7_migration_concurrency.py` | `777379e6509c0007e8fcc3a1fae4328b90d89a4de13590195f551ba397f2518e` |
| `alembic/env.py` | `2f49468408f6c0ef12ba0cf70294845cd3676860ff27f91be278effd984b9eed` |
| `pyproject.toml` | `74e34a542426e4cf81433ae0a6bd11026dafaaef2b7edb2e48cb0c1a6f108c3f` |

运行结束后只删除本轮容器并通过 Docker 查询确认已移除；本轮临时目录、环境文件和原始 JUnit 文件已清理，上表为其中的结果摘录。pytest 主进程禁用缓存提供器和字节码写入；测试创建的 Alembic 子进程未继承后者，可能生成共享快照内的 Python 字节码缓存，本次未清理共享缓存。本次没有更改产品源码或测试代码，也没有输出或留存数据库凭据。
