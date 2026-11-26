# 数据库验收

验证于 2026-11-26。SQLite 使用每例独立文件，PostgreSQL 使用仅监听本机的独立 PostgreSQL 16.15 容器。并发测试使用独立 schema，原生备份测试使用独立的源库和恢复库。pg_dump 和 pg_restore 均为 16.15。

## 改词与提交同时发生

在第一个事务即将提交时，第二个请求通过独立连接及独立授权运行时向数据库发出写操作。SQLite 和 PostgreSQL 各覆盖 8 种组合，共 16 项通过。

| 词表与动作 | 改词先提交 | 定稿或登记先提交 |
| --- | --- | --- |
| 全站或作者词表，章稿定稿 | 旧确认返回 409，并展示新词命中；无新增定稿 | 产生一份定稿，后续改词成功 |
| 全站或作者词表，登记发布 | 旧确认返回 409；发布记录仍为零 | 产生一条发布记录；改词后相同请求重放返回该记录的原收据 |

覆盖入口为 `backend/tests/test_publication_preflight_concurrency.py`。使用 `AINOVEL_POSTGRES_TEST_URL` 指向本机专用 `ainovel_test`，在 backend 目录执行 `uv run pytest -n 0 tests/test_publication_preflight_concurrency.py`。

## 迁移、备份和恢复

在 SQLite 和 PostgreSQL 上分别验证了以下流程。

- 从 `0075` 升级到 `0076`，在未写词表时降级回 `0075` 并核对权限目录，然后再次升级。Alembic 当前只有一个 head。
- 在同一实例为全站、原作者和接收者分别保存词表。单书文件未携带词表，也未包含只存在于原作者词表中的对照词。另一账号导入该书后，在实际定稿接口仅命中全站词和新作者词。
- SQLite 使用在线 backup，PostgreSQL 使用 pg_dump。备份完成后修改原作者词表，再把快照恢复到空目标。恢复后登录三个账号逐份比对词条与修订号，使用恢复的修订号可继续编辑。
- 已写词表时拒绝有损降级，数据库仍保持 `0076`。即使全站词表权限分配后又撤销，也会保留涉及该权限的历史管理审计，拒绝回退到旧权限目录。

整库回归与词表用例共 7 项通过。入口为 `tests/test_publication_terms.py`、`tests/test_publication_terms_migration.py` 和 `tests/test_annotation_native_backup.py`。除了并发测试的连接变量，还需要 `AINOVEL_POSTGRES_BACKUP_TEST_URL` 指向可创建临时数据库的本机测试库，并在 PATH 提供与服务端同主版本的客户端。测试恢复使用 `pg_restore --single-transaction`，确保整份快照在一个事务中写入目标。

完成匹配优化后，另在两库复验重复并发登记返回同一收据、个人词表与登记发生竞争的两个顺序，共 6 项通过。
