# Research: 0066 迁移写法与测试夹具影响（implement.md 第 0.3 步）

全量回归补充了一处历史结构对照。`test_kb_index_identity_migration.py` 会把 0045 的完整 `tasks` 表与当前 ORM 建出的表比较。0066 增加的 `worker_key`、`recovered_at` 只从该历史测试的期望列中剔除。实际列、其他约束、降级保护和数据保存仍完整比较，0066 的新增列由本次双数据库迁移测试核对。

- **Query**: 给 `tasks` 加 `worker_key`、`recovered_at` 两列并新增 `task_workers` 表，会触及哪些迁移惯例、结构契约测试与夹具
- **Scope**: internal（只读代码；未运行迁移）
- **Date**: 2026-11-23

## 1. 给 `tasks` 加列的既有写法

`tasks` 被生成日志、智能体等表以外键引用，历次迁移都用原位 `ADD COLUMN`，不用 `batch_alter_table`（批量模式在 SQLite 上会重建表并触发级联副作用）。

| 迁移 | 升级 | 降级 |
|---|---|---|
| `backend/alembic/versions/0026_admin_task_monitoring.py:16-32` | 三次 `op.add_column("tasks", ...)` + `op.create_index`；注释「ADD COLUMN 不重建 tasks 父表」 | 先查拒绝条件，再 `op.drop_index`、`op.drop_column` |
| `backend/alembic/versions/0045_kb_index_identity.py:23-58` | 需要列级 CHECK 时 SQLite 用原生 `ALTER TABLE tasks ADD COLUMN ... CONSTRAINT ... CHECK (...)`，PostgreSQL 用 `add_column` + `create_check_constraint` | 先查拒绝条件，`op.drop_column("tasks", "execution_key")` |

结论：0066 的两列不带 CHECK、不建索引、不建外键，升级直接 `op.add_column`，降级直接 `op.drop_column`，两种方言同一写法。SQLite 的 `DROP COLUMN` 需要 3.35 以上：本机 Python 自带 3.47.1；镜像 `python:3.12-slim-bookworm` 的系统库为 3.40 系列；0026、0045 的降级已依赖同一能力并有测试。

## 2. 降级保护的既有写法

0052 起的迁移在检查前先取锁，避免扫描后有新数据提交（`0052_generation_log_instance_identity.py:105-117`、`0065_scene_moves.py:36-45`）：

```python
db = op.get_bind()
if db.dialect.name == "sqlite":
    db.execute(sa.text("UPDATE alembic_version SET version_num=version_num"))
elif db.dialect.name == "postgresql":
    op.execute("LOCK TABLE <表> IN ACCESS EXCLUSIVE MODE")
else:
    raise RuntimeError("…不支持当前数据库")
if db.execute(sa.text("SELECT 1 FROM … LIMIT 1")).first() is not None:
    raise RuntimeError("…拒绝有损降级…")
```

0066 降级需要同时锁 `task_workers` 与 `tasks`（PostgreSQL 一条 `LOCK TABLE task_workers, tasks IN ACCESS EXCLUSIVE MODE`），再检查两类拒绝条件。

## 3. 新表的既有写法

`0065_scene_moves.py:12-33`：`op.create_table` 显式列出列与约束，索引单独 `op.create_index`。`task_workers` 主键是字符串 `worker_key`，不需要 `sqlite_autoincrement`。

## 4. 结构契约与全链测试

| 测试 | 是否受影响 | 说明 |
|---|---|---|
| `tests/test_migrations.py::test_upgrade_and_downgrade`（`:437-463`） | 需保持通过 | 空库 `upgrade head` → `downgrade base`。空库没有登记行，0066 的降级检查不会拒绝 |
| `tests/test_setting_fields_migration.py::_schema_0037_model_contract`（`:189-212`） | 不受影响 | 冻结结构只比较 `SCHEMA_TABLES`（`:28-31`，七张设定相关表），不含 `tasks` |
| 模型与迁移结果的全表一致性测试 | 不存在 | 仓库没有「head 结构等于 ORM 全部表」的全局断言（只有个别迁移测试比较自己的表，如 `tests/test_scenes_migration.py:125`）。0066 的测试需要自己比较 `tasks` 两列与 `task_workers` 的列集合 |

## 5. 按全部表遍历的代码

以下位置遍历 `Base.metadata.tables` 并沿外键扩散，新增表若带外键会被它们发现并要求登记：

| 位置 | 遍历方式 | `task_workers` 的影响 |
|---|---|---|
| `backend/app/services/wizard_service.py:770-793` | 沿 CASCADE 外键反查向导可替换主体的子表，与冻结图 `_WIZARD_DELETE_FKS` 不一致即阻止向导覆盖 | 无：没有外键 |
| `backend/app/services/chapter_delete_guard.py:88-103` | 沿 CASCADE 外键反查 `chapters` 的子表 | 无 |
| `backend/app/services/agent/effects.py:889-896` | 查找引用被撤销行的外键 | 无 |
| `backend/tests/test_project_transfer.py:52-72` | 直接或间接引用 `projects` 的表必须登记在导出规则或 `NOT_EXPORTED` | 无：不引用任何表 |

这印证了设计中「`tasks.worker_key` 不建外键」的选择：一旦建外键，`tasks`（已属于作品相关表）与 `task_workers` 之间的引用会进入上述图的计算范围，还要重建 `tasks` 表。

## 6. 测试夹具

- SQLite 夹具用 `Base.metadata.create_all` 建表（`tests/conftest.py:101-124`），新表与新列自动出现，不需要调整。
- PostgreSQL 用例用 `tests/schema_helpers.py:10-40` 的 `create_isolated_schema`，它遍历 `Base.metadata.tables` 全部复制，新表自动包含。守卫与随机 schema 的写法见 `tests/test_collaboration_audit_query_postgresql.py:17-47`（要求 `AINOVEL_POSTGRES_TEST_URL` 指向本机 `ainovel_test` 库，驱动 `postgresql+asyncpg`，不带查询参数）。
- 依赖版本（本机 `uv run`）：SQLAlchemy 2.1.6、Alembic 1.20.1、aiosqlite 0.22.1、SQLite 3.47.1。

## 7. 对实现的要求

1. 0066 升级：`op.create_table("task_workers", …)`，`op.add_column("tasks", worker_key)`，`op.add_column("tasks", recovered_at)`。
2. 0066 降级：取锁 → 两类拒绝检查 → `op.drop_column` 两列 → `op.drop_table`。
3. `tests/test_task_worker_migration.py` 自行断言迁移后的列集合与 ORM 模型一致，并覆盖「旧库里已有 `running` 行，升级后 `worker_key` 为空」。
4. 不需要改动 `_schema_0037_model_contract`、向导与章节删除的外键图、项目导出登记表。

## Caveats / Not Found

- 未实际运行迁移；以上来自阅读迁移源码与测试。
- `TaskExecutionIdentity` 的直接构造点（含测试中的位置参数构造）由 `research/takeover-fencing-core.md` 第 8 节给出，本文件未覆盖。
