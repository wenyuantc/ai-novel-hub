# 阶段 E：Agent 项目工具默认风格隐私

## 问题与边界

- `get_project_overview` 和 `update_project` 的工具结果原先复用 `project_out`。该人工投影保留管理员代管时查看作品作者私有风格的能力；管理员同时为编辑协作者时，模型工具因此可能收到不属于真实操作者的风格 ID、名称和描述。
- 本切片修改 `backend/app/services/agent/tools/read.py`、`write.py` 和 `context.py`，共享 Agent 专用项目投影并约束历史重放；人工 API 投影和数据库中的作品风格绑定不变，未开放协作者 Agent。
- Agent 投影先复用项目访问重验，再以单条 SQL 在同一读取快照中核对项目的当前默认风格、风格归属和内容，仅允许真实操作者本人或全局预设。它从最终结果重建风格摘要，不使用管理员人工投影或 ORM identity map 中可能过时的风格名称和描述；身份不匹配时将两个字段置空。
- `update_project` 继续通过原服务执行权限与字段校验；非作者显式提交 `default_style_id`（包括 `null`）仍被拒绝。预览和执行不因脱敏覆盖原绑定。

## 测试证据

- 修复前 `uv run pytest -q tests/test_agent_project_style_privacy.py`：4 failed、1 passed；失败分别复现管理员重叠身份的概览、执行回执、模型消息泄漏，以及同 ID 风格归属变化后复用旧名称。
- 修复后最终 `uv run pytest -q tests/test_agent_project_style_privacy.py`：35 passed；覆盖作者、普通编辑者、管理员兼编辑者、全局预设、风格本人、预览与执行、非作者显式 `null`／预设拒绝、作者变更、旧回执、截断数据、摘要、模型消息、同 ID 归属替换和内部 guards 过滤。
- `uv run pytest -q tests/test_agent_context_execution_identity.py tests/test_agent_context.py tests/test_agent_tools.py::test_project_update_allowlist tests/test_agent_api.py::test_generation_logs_are_conversation_scoped_and_hide_native_metadata`：27 passed。
- `uv run ruff check app/services/agent/tools/read.py app/services/agent/tools/write.py tests/test_agent_project_style_privacy.py tests/test_agent_project_style_privacy_postgresql.py`：通过。
- `uv run pytest -q --collect-only tests/test_agent_project_style_privacy_postgresql.py`：4 项已收集，涵盖不同身份的投影竞争、历史回执与风格归属变化竞争、真实 HTTP 出站前风格变化；真实数据库执行由主代理错峰安排，不能把收集当作并发验证。

## 历史送模处理与尚未完成的边界

- 历史结构化项目回执先以单 SQL 同快照核对会话 actor、作品和当前风格，旧 ID、名称、描述与当前本人／全局值同时匹配时才保留；不可信字段、损坏或截断数据安全隐藏，助手工具参数、预览差异和无调用 ID 的工具备注同步过滤。数据库原始历史不变。
- 旧摘要是无结构文本，无法凭当前风格 ID 证明来源。非作者会收到 `agent_history_unverifiable` 并停止送模，作者的摘要行为不变。模型发送守卫也捕获风格证明，并在实际发送前重验。
- 旧助手自由正文或 provider 签名元数据可能转述历史泄漏内容，不能可靠地从任意自然语言反推出来源；此事已通知主代理，协作者 Agent 开放前仍须定义历史信任／封存策略。
- 通用工具预览的 `guards[].hash` 是完整项目旧行的无密钥 SHA256，可能在其他行字段已知时被枚举以推测不可见的风格整数 ID。模型历史已过滤可识别的 Agent 内部预览 guards（包含嵌套或截断 JSON），保留普通外部工具的 guards 与数据库原始冲突证据；对外投影和确认链由 `log_review` 单独处理，不能仅据送模过滤宣称整个侧信道已关闭。
- 本任务整体仍需原始令牌、可信 provider、模型来源证明和协作者 Agent 授权等阶段 E 门禁；本记录不表示这些能力已开放。
