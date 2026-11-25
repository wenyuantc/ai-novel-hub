# 整库恢复和 PostgreSQL 并发验收

2026-11-25，在临时目录和独立的 PostgreSQL 17.10 容器执行。源库从空库用 Alembic 升到 0074。PostgreSQL 的源、目标各自使用新建的空数据库。工具版本为 pg_dump、pg_restore 18.6。

| 检查 | 结果 |
| --- | --- |
| SQLite 在线快照，经现有冷恢复函数恢复到新路径 | 通过 |
| PostgreSQL 应用备份适配器生成 custom dump，再用 pg_restore 导入新数据库 | 通过 |
| 两个同时提交的相同回复、并发撤权和新回复 | 通过，回复按操作标识去重，撤权后的请求遭拒 |
| 完整替换场景正文与按旧稿创建批注并发 | 通过，最终有效批注不会指向旧稿；后续旧稿提交返回 409 |

两种数据库使用同一组 HTTP 操作，创建有效未处理、有效已处理、原文失效三条讨论。快照后在源库撤权并追加留言。恢复库重新登录后，逐字段比较讨论和全部留言，包括身份、署名、时间、顺序、摘录、偏移与处理状态。查看者恢复了快照时点的回复权，外部账号无法查看，作者可处理失效讨论。

从 `backend/` 执行的命令如下。恢复验收的测试连接需要 CREATEDB 权限，用于建立和清理两个随机测试库。两个 PostgreSQL 连接串均指向本机专用 `ainovel_test`。

```sh
export LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
export PATH="/opt/homebrew/opt/libpq/bin:$PATH"
export AINOVEL_POSTGRES_BACKUP_TEST_URL="postgresql+asyncpg://postgres@127.0.0.1:32914/ainovel_test"
export AINOVEL_POSTGRES_TEST_URL="$AINOVEL_POSTGRES_BACKUP_TEST_URL"
uv run pytest -q tests/test_annotation_native_backup.py
uv run pytest -n 0 -q tests/test_annotation_postgresql.py
```

恢复测试 2 项通过（21.77 秒），并发测试 2 项通过（8.26 秒）。

追加一轮隔离 PostgreSQL 17.10 专项（本轮临时端口为 32915，命令为上面的 `pytest -n 0 -q tests/test_annotation_postgresql.py`）。全部 13 项通过，用时 96.42 秒。其中单书备份 3 项覆盖跨账号恢复、快照期间回复及处理、旧格式和图引用校验。定位 7 项覆盖同句第二处、作者区块、正文整体覆盖、批量修订及撤销、拆分两种边界、移章、退场、AI 改写、历史恢复和事务回滚。并发另有 2 项。迁移 1 项使用真实 0073 旧库，确认升级 0074 后旧稿正常且批注为空；写入讨论和留言后降级被拒，版本仍为 0074，记录完整；清空测试讨论后成功降回 0073，旧稿仍在。
