# 同步改写的 SSE 输出屏障复核

## 发现与处理

- 实际 TCP 慢客户端红灯：原协作改写在业务异步生成器 `yield` 时跨出 `fail_after` 和数据库事务；取消发送后生成器可能由不同 task 关闭，导致 cancel scope 错误及事务未释放，撤权被阻塞。
- 章节、场景和区块的协作改写现由 ASGI 发送协程对每个 `delta` / `done` 独立取得原用户、项目和配置屏障，锁后重验原令牌、授权、模型指纹和内容/可见性代次，在同一协程完成物理 send 和释放。取锁、校验及 send 一起计入 5 秒；超时不重复发送该帧。业务生成器本身不再跨 `yield` 持事务或 cancel scope。
- 超时、断连和取消在同一协程关闭业务生成器；授权失败仅发送固定错误并先完成失败日志。会话关闭失败时尝试使连接失效；如果数据库连接本身连失效也失败，不能声称底层锁一定释放。已发送的字节无法撤回。
- 另一个真实 HTTP 红灯证明成功日志写入的 DBAPI 异常堆栈会泄漏 SQL 参数/正文；协作路径记录固定错误，不记录原堆栈。作者回退模型服务失败时对协作者使用固定错误，不回显提供商返回的模型或密钥。原作者改写错误契约维持不变。
- 区块一次性发送确认、已确认内容不自动重试、普通非协作 SSE 的旧分支保持原有行为。协作结果若新增其他消费者，必须使用相应的输出帧授权，不得只直接消费业务生成器。

## 验证

在 `backend/` 下：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_rewrite_identity.py \
  tests/test_rewrite.py::test_rewrite_preview_then_apply \
  tests/test_rewrite.py::test_apply_ignores_foreign_log_and_writes_baseline \
  tests/test_rewrite.py::test_continue_at_cursor tests/test_rewrite.py::test_rewrite_error_logged \
  tests/test_rich_document_section_ai.py::test_closed_section_only_confirmed_selection_leaves_for_one_operation \
  tests/test_rich_document_section_ai.py::test_confirmation_uuid_concurrent_replay_cannot_send_twice \
  tests/test_rich_document_section_ai.py::test_real_redirect_does_not_send_confirmed_section_selection_twice --tb=short
# 28 passed；运行时独立新增的场景入口测试另跑。

uv run ruff check app/api/sse.py app/api/v1/chapter_versions.py app/api/v1/scene_ai.py \
  app/services/rewrite_service.py tests/test_collaboration_rewrite_identity.py \
  tests/test_collaboration_rewrite_identity_postgresql.py
# All checks passed!
```

项目未配置独立 Python 类型检查器，已通过受影响文件的 `compileall`。SQLite 的真实 TCP 超时/取消和独立撤权检查均通过，不替代 PostgreSQL 锁竞争验证。

## PostgreSQL 阶段验证

2026-11-10 主会话在隔离 PostgreSQL 16.15 错峰执行以下命令：**6 passed in 39.03s**。覆盖真实发送与撤权先后、重邀旧预览、逐帧发送，以及独立连接检查慢客户端超时和断连后的会话非 idle-in-transaction。

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_rewrite_identity_postgresql.py --tb=short
```

此结果对应执行时的代码快照；未部署，也未验证线上模型服务。统一后台身份与其他 AI 入口仍需最终集成验证。
