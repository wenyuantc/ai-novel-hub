# 阶段 D：AI 可见性配置的人工作品权限

## 2026-11-10 隔离 PostgreSQL 补测

在本机独立 `collab_visibility` 测试库（PostgreSQL 16.15、随机 schema）执行 `AINOVEL_COLLAB_VISIBILITY_POSTGRES_TEST_URL=<专用测试库 URL> uv run pytest -q tests/test_collaboration_visibility_postgresql.py`：**4 passed in 29.39s**。本结果覆盖该测试文件中的权限与发送门禁竞争，不代表阶段 D 或完整协作 AI 发送链已验收。

## 已实现范围

- 设定条目的 AI 与提及配置属于人工项目资料；HTTP 读、写显式传入 `manual=True`，分别使用 `content.read`、`content.edit`。查看者可读不可写，编辑者可修改；兼任拥有 `projects.edit` 的管理账号的查看者仍可按管理员权限写人工内容，但不由此获得 AI 使用能力。服务默认参数保持原作者／管理员边界，未修改严格作者 helper，也未接通模型出站或任务。
- 设置写入、共享字段定义 `ai_hidden`、旧字段隐藏和旧字段绑定使用已有项目访问服务的认证/User → AI 发送写门禁 → 作品父行 → 锁后权限重读协议。原有字段／时间线／AI 可见性版本、跨作品引用限制和事务回滚均保留。
- 单书协作者修改系列绑定条目的共享 AI 属性时，以真实 actor 对冲突脱敏；没有系列访问权的普通协作者不会从错误中获知系列身份。调用已有 `series_policy`，未修改该模块。
- 既有管理员写可见性配置使用的明细 `ai_mode`、`links`、`ai_hidden` 不在审计摘要白名单，会使合法管理员提交返回 500。现使用已有 `setting_fields`、`field_definitions` 安全分类记录，不记录设定值，保留事务内审计。

## 验证

下列命令均在 `backend/` 执行：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_visibility_access.py \
  tests/test_setting_ai_visibility.py tests/test_setting_ai_visibility_formats.py tests/test_setting_fields.py --maxfail=1
# 38 passed；此轮在后续增加的暂存回滚和 SQLite 出站用例之前执行。

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_visibility_access.py::test_denied_visibility_write_rolls_back_pending_changes
# 3 passed：三个入口权限失败后，调用方捕获异常再提交也不落授权前暂存的内容。

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_visibility_access.py::test_editor_visibility_write_respects_sqlite_actual_send
# 2 passed：SQLite 上真实本地 OpenAI 兼容 HTTP 发送期间，策略／字段定义写返回受限冲突；响应头后可保存，旧可见性戳不得再次发送。

uv run ruff check app/services/setting_ai_visibility.py app/services/setting_field_service.py \
  app/api/v1/setting_fields.py tests/test_collaboration_visibility_access.py \
  tests/test_collaboration_visibility_postgresql.py
# All checks passed!

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest --collect-only -q \
  tests/test_collaboration_visibility_postgresql.py
# 4 tests collected；每项内部依次验证 5 类操作，总计 20 个真实数据库竞争情景。
```

PostgreSQL 待在负载恢复后独立执行，**当前未获得完整通过结果**。为避免同数据库不同 schema 的 advisory lock 相互碰撞，优先在隔离专库运行，并确保不与其他使用同库的门禁测试并发：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  AINOVEL_COLLAB_VISIBILITY_POSTGRES_TEST_URL='<本机 collab_visibility 专用测试库 URL>' \
  uv run pytest -q tests/test_collaboration_visibility_postgresql.py --maxfail=1
```

该变量未提供时，测试回退 `AINOVEL_COLLAB_POSTGRES_TEST_URL`，严格限定本机 `collab_test` 测试库；串行运行才可共享该库。各用例仍建立随机 schema。验证包括五类操作分别与撤权、降级、写先提交、真实 provider 发送租约的竞争；响应头后重试必须成功，旧戳不能再次出站。先前为避免数据库锁碰撞及减轻负载而中断的执行不计为通过，也不能用 SQLite 替代。

目前仅证明配置保存与既有发送门禁的互斥；协作者 AI 请求的真实 actor、模型来源和邀请轮次出站重验仍是阶段 E 的工作。本文件和测试不代表完整 Gate D 或 Gate E 已通过。
