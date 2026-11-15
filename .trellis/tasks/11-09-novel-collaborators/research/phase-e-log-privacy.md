# 阶段 E：生成日志与智能体隐私投影

## 已实现

- `generation_log_service` 的章节列表和详情先检查当前账号是作品作者或 accepted 协作者，再以真实 `GenerationLog.actor_user_id` 过滤。viewer 可读自己历史日志；pending、非成员及只有管理员人工作品权限的账号均不能访问他人的日志。授权前查询使用 `no_autoflush`，章节还要属于请求的作品。
- `generation_log_service.public_logs`、`personal_projection.project_payload` 生成独立响应，不修改持久 ORM。模型配置仅可显示本人配置 ID；任务必须属于本人、同一作品且满足当前任务访问条件；日志必须属于本人和同一作品；全局或本人风格 / Skill 可以显示，私人风格、Skill 和 MCP 插件按归属过滤。嵌套列表、预览 diff、字符串内 JSON 和作者模型绑定、邀请轮次等内部保留键均经过投影。token 用量保持原值和 null；既有 Agent 写日志从 provider `usage` 保存可得用量。
- Agent **本人历史日志**单独校验项目身份和本人会话，允许当前 accepted viewer 读取，未放宽 `owned_conversation`，因此没有开放协作者会话写、AI、任务、提案或发送。
- Agent 详情、单项/整轮确认与撤销、预览变化错误、公开任务 SSE 的 `proposal` / `executed` 等输出使用统一投影。任务 SSE 只在 actor 与登录版本复查通过后出队投影；runner → task hub 是内部事件，保留必要证明供公开流投影。当前唯一公开订阅路由 `/tasks/{id}/stream` 传入认证 actor 和 token 版本；无 actor 的 `TaskEngine.subscribe` 仅供内部调用，不可作为公开入口。
- 新 effects 在已有快照批量查询中记录任务真实 actor/不可复用 `statistics_key`，以及日志真实 actor/创建时间；章节版本引用日志也记录原始证明。对外投影必须同时核对当前项目中的真实归属及证明，避免 SQLite 删除后复用整数 ID 将他人的旧链接当成本人记录；没有证明的旧 effects 私人链接/版本日志引用保守隐藏。预览 diff 将表名和整数 ID 嵌入 `field`，对缺证明的 tasks/generation_logs 行直接过滤，章节版本日志 ID 的 `before/after` 隐藏。证明字段不会送给客户端，完整内部 effects 保留给原有撤销逻辑。
- 项目任务投影的 `task_visible_to_actor` 复用阶段 E 的任务访问谓词；viewer 或授权轮次失效后，其他响应中的活动任务引用也被隐藏。`project_payload(db, user_id, project_id, payload)` 对整批 payload 查询个人身份并返回独立结构，顶层不可见 effects 链接返回 `None`，嵌套列表删除该链接。不要把已经投影的 Agent effects 再投影，否则丢失证明会再次隐藏本人链接；runner 原始事件只允许在内部流转，公开边界投影一次。

## 验证

- 开发前的两个真实 HTTP 红灯分别为：accepted viewer 的本人章节日志返回 404；项目作者的 Agent 变更直接泄露其他 actor 的 Task ID。
- `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_log_private_views.py tests/test_collaboration_log_privacy.py`：当时 6 passed（后续又新增实时 SSE、私人资源和 ID 复用用例，故这不是最终全部新用例的结果）。
- 新增历史 Agent 日志和已有 Agent 日志元数据组合及当时的新隐私用例：5 passed；本人/他人 Skill、风格、插件与嵌套 JSON 的专用用例：1 passed。
- 最后一次代码变更后的章节删除实际撤销、实时 SSE 同时显示本人/隐藏他人链接、删除重建导致整数 ID 复用的聚焦组合：3 passed。原始 effects 保持不变，实际撤销恢复了被删除章的他人任务、日志与版本日志引用。SSE 用例模拟 effects 内真实同形链接验证公开流投影。
- 最新投影代码的章节日志角色矩阵和本人历史 Agent 日志复核：2 passed，含认证前无自动 flush、管理员撤权后仅保留人工权限、作者模型 ID 与工具参数隐藏。
- `tests/test_chapter_versions.py`：16 passed；范围内 Ruff 检查通过。最终 effects 查询优化后，既有空章恢复和旧 identity map 隔离两项通过。
- 隔离 PostgreSQL 16.15：`AINOVEL_COLLAB_LOG_POSTGRES_TEST_URL=<本机 collab_logs 专用库> uv run pytest -q tests/test_collaboration_log_private_postgresql.py` 1 passed，随机 schema、双应用连接，验证本人日志、作者模型 ID 隐藏、同一活动任务在降为 viewer 后的 ID 隐藏与撤权后 404。该次执行早于最后的 Agent 效果证明字段和投影查询优化；**最终代码仍需在隔离 PostgreSQL 重跑**。新测试优先使用该专用环境变量，未设时回退项目标准 `AINOVEL_COLLAB_POSTGRES_TEST_URL`；只允许本机 collab_logs/collab_test 和随机 schema。
- 并行组合 `tests/test_agent_api.py tests/test_agent_runtime.py tests/test_agent_effects.py ...` 在大量其他 pytest 同时运行及任务服务仍被并行修改时出现两个失败，主动中断，**不计通过**。失败的 `test_serialization_failure_rolls_back_write_and_keeps_proposal` 与 `test_new_turn_after_cancel_completes_abandoned_tool_protocol` 以当时最新代码分别单独重跑通过；Agent 全组合仍需在修改稳定后重新执行。

## 剩余集成

- 任务、模型解析与外部发送尚未接通协作者；本切片不代表阶段 E 完成。新受理任务必须写入可信的 `_collaboration_grant` 和 `_project_model`，后续发送与每次保存仍需重验权限、授权轮次及模型来源。不得让客户端控制这些保留字段。
- Agent 的人工会话详情、发送、工具和 SAVEPOINT / 失败审计目前仍严格作者，未来开放时需逐动作接入真实 actor、当前协作能力与原邀请轮次；只有投影不构成执行授权。API 层 `chapter_versions.py` 中日志方法注解仍写 ORM `GenerationLog`，实际返回 Pydantic 投影；响应模型已正确，后续可同步注解。
- `personal_projection` 根据真实行和明确字段名识别结构化引用，不能修复其他路径把私人数据直接拼进任意纯文本的业务错误；模型上下文、任务输出、富文档提案和其他 AI 流须分别在来源处校验并执行同一公开边界。
- 最新版本的全部 Agent 测试、任务并发 / 回收竞态及 PostgreSQL 测试仍需错峰完成，不能以 SQLite 测试或本次较早的 PG 结果冒充最终门禁。
