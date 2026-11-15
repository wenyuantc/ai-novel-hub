# Agent 提案与任务身份迁移（0047）

## 实现

- `AgentChange.change_key` 是每次新建由服务端生成的规范小写 UUIDv4，非空且全局唯一。历史提案按主键分页，逐行生成独立 UUID；既有整数 ID、会话、任务、内容、状态、时间戳和外键均不变。整数 ID 即使重用，新建提案也得到新 UUID。创建路径不得采纳客户端给出的新提案身份；唯一索引约束的是当前行，UUIDv4 的跨删除不复用依赖服务端随机生成，不是允许手工复写任意历史 UUID 的永久墓碑表。
- `AgentMessage.turn_task_key` 和 `AgentChange.turn_task_key` 是可空的规范小写 UUID 字符串，用于服务端在创建时保存所绑定 Task 的 `statistics_key`。历史行一律保持 NULL：仅靠当前整数 FK 和任务被补写的 UUID，不能证明它是历史创建时的任务。任务删除时 `turn_task_id` 按原外键规则置空，已保存的身份保留；重建同整数 ID 也不自动绑定历史行。
- SQLite 不重建已有表。新增非空列时临时以空串作为 SQLite 规定的常量默认；原位回填后，唯一索引和 INSERT/UPDATE 触发器阻止空串写入，列级 CHECK 同时检查 UUID 格式；ORM 自建表及 PostgreSQL 使用等价的非空、唯一和 UUID CHECK。两张表的任务键也各有可空格式约束。
- SQLite legacy 模式下迁移先启动真实写事务，DDL、回填、索引及触发器作为整体回滚；回填中和后续建列阶段的故障注入都验证可重试。升级需要停掉仍按旧模式插入 AgentChange 的旧应用实例；旧写入缺少身份会被拒绝，不能通过猜测创建身份兼容。
- 仍存在任意 AgentChange，或存在携带非空 `turn_task_key` 的 AgentMessage 时拒绝降级：删除这两类持久身份属于有损操作。清空相关提案、并确认没有保留任务身份的消息后，才可无损移除新列；其他消息及现有外键保留。
- 项目单书备份仍将 `agent_conversations`、`agent_messages`、`agent_changes` 排除。系统整库备份必须包含这些持久列；SQLite 实际在线 backup/恢复检查已覆盖。PostgreSQL 系统备份沿用原生 pg_dump，尚未在本次执行真实 dump/restore。

## 验证

```sh
cd backend
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_agent_change_identity_migration.py --tb=short --disable-warnings
```

8 项通过：存量 505 行和分页回填、空库直接升级与降级、SQLite 外键开启和扩展子表、重复/缺失/错误值、任务及提案整数 ID 重用、真实整库 SQLite backup、单书备份排除、ORM 约束、迁移故障回滚重试。正常和故障路径均未重建父表。

```sh
uv run ruff check app/models/agent.py tests/test_agent_change_identity_migration.py \
  tests/test_agent_change_identity_postgresql.py
uv run ruff check --isolated --line-length 120 --target-version py312 --select E,F,W,I,B,UP,ASYNC \
  alembic/versions/0047_agent_change_identity.py
```

以上 Ruff 与 `git diff --check` 均通过。2026-11-10 在 PostgreSQL 16.15 的本机 `collab_test` 隔离库错峰执行 `tests/test_agent_change_identity_postgresql.py`：**2 passed in 8.99s**，覆盖空库和存量库。测试自动建立和删除随机隔离 schema。同步 `APP_SCHEMA=0047` 后，在 SQLite 复跑 0047 与 0046 迁移组合：**10 passed in 64.14s**。

## 集成边界

`turn_task_key` 的可靠写入和任务/提案读写时的原身份比较由 Agent 服务及引擎接线；旧记录为 NULL 时须按无法证明身份处理。本人客户端若需以 `change_key` 作操作凭据，应使用受限的显式响应，不将内部任务身份投影到模型回执或其他用户。主会话已将 `project_transfer/exporter.APP_SCHEMA` 和现行备份版本断言同步到 0047，完整备份回归仍需运行。此迁移和测试不代表协作者 Agent 或整个阶段 E 已完成。
