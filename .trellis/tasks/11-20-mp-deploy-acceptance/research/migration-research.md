# C7 迁移与部署预研

2026-11-25 在 `codex/mp-peer-channel-stream`、`d9d234c` 上核对。以下是规划阶段的隔离实验，**不是产品实现或完整验收**。

## 当前事实

- `backend/alembic/env.py` 每次在线迁移创建单独的 `NullPool` 异步引擎，没有进程间互斥。镜像先运行 `alembic upgrade head`，成功后才运行 Uvicorn；当前 head 是 `0070`。
- SQLite 历史迁移 `0024`、`0035`、`0036`、`0037`、`0039` 在迁移连接开启外键时可能进入 `autocommit_block()`。因此不能只在调用前写一下版本表，就假定锁和全程事务一定持续。迁移 `0066`–`0069` 还有租约侧库与停机保护，不能绕过。
- Python 3.12 的 SQLite 旧事务模式不会因 DDL 自动发出 `BEGIN`；[SQLAlchemy 官方说明](https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#transactions-with-sqlite-and-the-sqlite3-driver)要求显式处理事务。
- PostgreSQL 的会话级 advisory lock 持有到显式释放或连接结束，不因事务回滚释放；[PostgreSQL 16 官方说明](https://www.postgresql.org/docs/16/explicit-locking.html#ADVISORY-LOCKS)。迁移连接必须直连数据库或获得会话级固定连接，不能假定事务池在提交后仍将后续解锁交给同一连接。
- 每个应用进程同时登记请求和调度租约；两个租约共享一个内部监听。Compose 中不同容器的默认 `/tmp` 不共享，因而跨容器需要可互相直达的 TCP 地址，不能把多个副本共用的负载均衡名当作单个进程地址。参见 [C4 监听设计](../../11-20-mp-peer-channel-stream/research/transport-lifecycle.md) 和 [Docker Compose 网络说明](https://docs.docker.com/compose/how-tos/networking/)。
- C8 当前只有需求草稿，尚无已演练的旧版升级或回滚步骤。C7 的当前版本排空演练不能算作父任务 AC13 的旧版本演练。

## 临时实验与结果

将 Alembic 目录复制到系统临时目录，实验只修改该副本。SQLite 用迁移专用连接先关闭 FK、显式 `BEGIN IMMEDIATE`，让 Alembic 在已有真实事务中运行；成功时提交，异常时回滚。PostgreSQL 使用同一连接持有会话级 advisory lock，并在连接上的事务中迁移。两者都启用事务性 DDL。故障通过修改临时副本的 `0037`：在第一次加列之后抛错。

| 隔离实验 | 结果 |
| --- | --- |
| SQLite / Python 3.12.10：空库及 `0034` 库中途报错 | 两次均以非零退出；对比失败前后 schema 和版本，完全一致；外键检查无错误。 |
| SQLite：3 个进程同时迁移空库 | 3 个均成功，日志中仅一个执行 70 次升级；版本为 `0070`。 |
| PostgreSQL 16.15 临时容器：空库及 `0034` 库中途报错 | 两次均以非零退出；对比 `pg_dump --schema-only` 的失败前后结果一致。 |
| PostgreSQL：3 个进程同时迁移空库 | 3 个均成功，日志中仅一个执行 70 次升级；版本为 `0070`。 |

临时文件和 PostgreSQL 容器已清理。上述旧库由空库迁移得到，尚未放入真实业务数据；并发实验也未模拟耗时超过默认忙等待、进程强杀、连接失联、用户请求、历史降级或跨主机共享卷。正式验收必须覆盖这些情形，不能用此结果代替。

## 规划约束

SQLite 方案要在获取写锁之前确认迁移连接的 FK 已关闭，在提交前做外键完整性检查；只允许在受控事务内完成 DDL 和版本变更。未来迁移若引入显式提交或需要 `autocommit_block()`，必须由测试发现并重新设计，不能静默破坏这个保证。锁竞争超过 SQLite 默认单次忙等待时需继续等待或按明确可观察的策略失败，不能误判为迁移成功。

PostgreSQL 方案的锁、迁移和解锁使用同一实际连接；报错时先回滚，解锁或关连接后保持失败。两个数据库都应验证持锁者强杀后的恢复，以及第一次迁移失败、后续进程从完整旧库重试成功的分支。网络分区或提交结果未知时，不凭日志猜测；重新连接并核验版本、结构和一致性。
