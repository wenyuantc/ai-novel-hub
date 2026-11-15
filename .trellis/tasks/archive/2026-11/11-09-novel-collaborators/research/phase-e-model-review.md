# 阶段 E 模型来源与配置锁独立复核

2026-11-10 在本机独立 `collab_models` 测试库（PostgreSQL 16.15、随机 schema）运行 `AINOVEL_COLLAB_MODEL_POSTGRES_TEST_URL=<专用测试库 URL> uv run pytest -q tests/test_collaboration_ai_config_postgresql.py --tb=short`：**18 passed in 53.27s**。该结果覆盖模型原语及配置锁竞争，不代表协作者生产发送链已接入。

## 已修复

- 模型配置 create、update、set-default、delete 先在同一事务取得请求原始 actor 的 User 认证共享屏障，再取得该用户配置独占锁，并按原始 token version 再次认证。锁持至提交；撤销或停用先提交时旧请求失败，写入先持锁时认证变更必须等待。两个屏障均处在 `no_autoflush` 内；SQLite 写锁冲突整体回滚并返回 `collaboration_busy`。原直接服务调用以当前启用账号为准，不伪造 HTTP 令牌。
- 作品文本／图片模型解析先按用户 ID 升序取得 actor 与作品作者配置共享门禁，在等待结束后重新读取项目、成员和邀请，再选择本人或作者配置，防止本人缺失与作者新增来自不同时点。真正任务受理仍必须调用 `for_update=True`，先按认证/User → 项目父行 → 配置门禁顺序锁住并核验授权；仅读取解析不构成受理授权。SQLite WAL 旧快照或锁冲突整体回滚，不能沿旧视图继续。
- 显式配置 ID 只接受正 PostgreSQL INTEGER 范围的严格整数；布尔值、字符串、零及溢出统一返回安全的配置不存在，不能把 `True` 转成配置 1。

## 核对范围与未完成

- 现有单元测试覆盖本人配置优先、显式选择的归属与类型、授权作者回退、配置失效不换密钥、向量作者空间、当前默认切换和严格内部序列化。伪造的模型/向量 JSON 错误为固定文本，相关测试断言不回显测试秘密；`repr` 不显示绑定字段。作者历史缺席使用专用 sentinel，显式 null 与缺席不同。
- `collaboration_ai_guard.SendGuard` 当前在认证 User 和发送共享门禁后调用 `revalidate_project_model` / `revalidate_shared_embedding`，默认持配置锁；这仅是尚未接生产入口的内部协议。实际受理、发送、索引写、投影、费用和撤权后的执行体清理仍须阶段 E 集成和真实测试。
- 原 `phase-e-model-core.md` 对配置 CRUD 锁序的“只取配置门禁”描述已过时：当前为 User 认证共享屏障 → 该用户配置独占锁。项目模型选择为认证/User → 项目父行（受理时）→ 按 ID 升序的配置共享锁。所有新配置写路径必须使用相同门禁，且不能在配置门禁后反向取得需要其他事务持有的 User/项目写锁。
- 真实 PostgreSQL 的新增双连接认证／选择测试已编写但未在本切片执行；其结果及实际模型 HTTP lease 不可用 SQLite 或模拟替代。读写中的外部直连 SQL 不参加应用 advisory 门禁，仍需部署约束。

## 验证

在 `backend/` 下实际执行，均为本机隔离测试数据：

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_ai_config.py tests/test_collaboration_ai_config_auth.py \
  tests/test_collaboration_ai_config_selection.py tests/test_ai_configs.py \
  tests/test_ai_config_model_type.py tests/test_ai_config_params.py --tb=short
# 92 passed in 44.83s

uv run ruff check app/services/ai_config_service.py app/services/project_ai_config.py \
  tests/test_collaboration_ai_config.py tests/test_collaboration_ai_config_auth.py \
  tests/test_collaboration_ai_config_selection.py tests/test_collaboration_ai_config_postgresql.py
# All checks passed!

uv run python -m compileall -q app/services/ai_config_service.py app/services/project_ai_config.py \
  tests/test_collaboration_ai_config.py tests/test_collaboration_ai_config_auth.py \
  tests/test_collaboration_ai_config_selection.py tests/test_collaboration_ai_config_postgresql.py
# exit 0
```

新增 HTTP 测试在 SQLite 验证四种操作的令牌撤销和账号停用先提交，均不写配置；选择测试覆盖普通读取、持项目锁、SQLite WAL 旧快照整体回滚。完整组件组合中包括这些测试；数据库锁先后顺序尚需 PostgreSQL 验证。项目未配置独立 mypy／pyright 类型检查，`compileall` 不冒充类型检查。

按主代理安排待错峰执行，使用本机专用库且每次创建随机 schema：

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
AINOVEL_COLLAB_MODEL_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_models' \
uv run pytest -q tests/test_collaboration_ai_config_postgresql.py --tb=short
```

其中 `test_config_http_write_waits_for_auth_change_then_rejects`、`test_config_http_write_holds_actor_lock_while_waiting_for_config`、`test_model_selection_holds_both_users_config_snapshot` 和 `test_model_selection_rechecks_membership_after_waiting_for_config` 是本次新增并发反例；此前已有向量默认变更和作者撤权的竞争测试。专用 URL 缺席时 fixture 可回退标准隔离库 URL。
