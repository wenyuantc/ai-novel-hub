# 同步改写与区块协作复核

## 变更与边界

- 实际 TCP HTTP + SQLite 红灯：已接受的 editor 请求章级无预览凭据采纳时，原作者专用入口返回 404；因此现有协作链路尚未开放，不能据此判定旧实现已具备完整编辑者能力。
- 编辑者受理先核验 `ai.use` 并固定原用户令牌、邀请 UUID/revision 和服务端模型绑定；仅可使用本人显式或默认模型，或经作者授权的模型回退。原模型指纹、所属人与授权在真实 SDK 每一跳发送 lease 中重验，不在配置失效后切换其他模型。管理员附带 viewer 身份仍不能发起 AI。
- 改写及区块只使用设定、规划与按 actor 过滤的风格上下文，`build_setting_context` 不调用向量检索。共享向量接口仍由相关负责人完善，未在本切片开放或以测试替代其授权。
- 编辑者每条 delta 和 done 的最终快照核验原用户/令牌、作品生命周期、原邀请轮次、配置指纹与章/可见性版本；原 actor 和项目/配置读屏障跨该帧实际 ASGI send，单帧 5 秒超时，结束或取消后释放。已交付 ASGI 的字节不能撤回。
- 成功记录在原用户和作品锁下再次核验原 grant、模型绑定与场景戳，同事务写真实 actor、配置及生成记录；可信 `model_binding` 交由日志服务持久化来源并安全投影。缺少来源存储时协作者在出站前失败关闭。
- 协作者采纳要求本人成功记录、服务端可信模型来源证明和原邀请轮次；旧缺证记录、撤权/重邀后的旧预览和区块 receipt 不能被采纳。作者保留旧章级无日志替换及区块一次性收据；章级采纳响应走安全章节投影。
- 额外红灯：将本人同章成功日志改成其他 AI 操作、改为与原请求 `op` 不同的改写类型，或移除场景身份，原章级采纳均返回 200 并写入正文。协作者现在要求服务器可信来源、改写 `kind` 与 `params.op` 一致、完整场景与上下文快照及原 grant；缺证失败关闭，作者旧章级兼容分支不受影响。
- 由于 `project_ai_config` 尚无公开的绑定指纹比对 helper，本切片复用其现有 `_fingerprint`，避免复制算法；这是需要与模型负责人协调的内部接口耦合。

## 验证

```sh
cd backend
uv run pytest -q tests/test_collaboration_rewrite_identity.py --tb=short
# 10 passed：本机 TCP API + 真实 SDK 与本机 HTTP 模型服务器；含撤权后流、重邀、闭区块单次确认、
# 307 禁止重发、作者回退日志脱敏、viewer/admin+viewer 和伪造作者配置。

uv run pytest -q tests/test_collaboration_rewrite_identity.py::test_editor_apply_requires_matching_rewrite_log_and_full_snapshot \
  tests/test_collaboration_rewrite_identity.py::test_editor_section_single_confirmation_and_apply \
  tests/test_collaboration_rewrite_identity.py::test_reinvited_editor_cannot_apply_old_section_receipt --tb=short
# 6 passed：最后一次采纳来源与上下文校验后执行；另在最后一次校验前全协作专项及作者兼容共 14 passed。

uv run pytest -q tests/test_rewrite.py::test_rewrite_preview_then_apply \
  tests/test_rewrite.py::test_apply_ignores_foreign_log_and_writes_baseline \
  tests/test_rewrite.py::test_continue_at_cursor tests/test_rewrite.py::test_rewrite_error_logged \
  tests/test_rich_document_section_ai.py::test_closed_section_only_confirmed_selection_leaves_for_one_operation \
  tests/test_rich_document_section_ai.py::test_confirmation_uuid_concurrent_replay_cannot_send_twice \
  tests/test_rich_document_section_ai.py::test_real_redirect_does_not_send_confirmed_section_selection_twice --tb=short
# 7 passed；作者旧行为回归。

uv run ruff check app/services/rewrite_service.py app/services/rich_document_section_ai.py \
  app/api/v1/chapter_versions.py app/api/v1/scene_ai.py tests/test_collaboration_rewrite_identity.py \
  tests/test_collaboration_rewrite_identity_postgresql.py
# All checks passed!
```

项目无独立配置的 Python 类型检查器；编译检查通过。SQLite 通过不能替代跨进程锁竞争证据。

## 待主会话错峰验证

`tests/test_collaboration_rewrite_identity_postgresql.py` 的 **5 项**已收集：撤权在真实模型响应头前等待、撤权先提交零发送、重邀旧预览采纳拒绝，以及通过实际 TCP 客户端暂停 ASGI send 的逐帧等待与慢客户端超时释放。尚未宣称通过，按主会话的专用 PostgreSQL 库错峰执行。完整后端、前端、迁移和阶段门禁均不属于本切片验证。


2026-11-10 后续输出 lease 修复后，主会话已在隔离 PostgreSQL 16.15 实跑该专项 **6 passed in 39.03s**；覆盖范围及最新证据见 `phase-e-rewrite-output-lease-review.md`。上面的 5 项“待执行”记录为历史快照。
