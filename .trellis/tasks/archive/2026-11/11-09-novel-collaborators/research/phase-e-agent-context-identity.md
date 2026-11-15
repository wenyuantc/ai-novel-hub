# Agent 上下文摘要与任务执行身份

## 本切片

- Agent 后台上下文摘要要求从已领取的 `AgentJob` 捕获原任务 UUID、入队时间、执行 UUID、真实账号、作品、会话、输入消息与本人模型配置；仅接受当前作者的 `agent.turn`，不接受协作 grant。无任务证据时可以进行不调用模型的本地上下文构建，不能发送历史摘要。
- 摘要构建快照在进程内绑定会话原摘要/压缩位置、旧消息和输入消息，且在真正发送租约内按账号 → 发送门禁 → 项目 → Task → 会话/消息 → 模型配置顺序锁后核验。不得在模型请求期间持写事务；身份/账号/历史/配置变化时停止旧结果。
- 模型完成后独立短事务取得相同写屏障并重验，摘要和真实 actor 的 GenerationLog 同事务写入；调用者的未提交状态不得在身份验证前自动 flush。日志失败或提交确认异常不自动重发模型请求，事务结果不明确仍需在整体任务恢复设计中处理。
- `guard_agent_provider` 可让 runner 复用相同的 actor、Task、会话、消息和模型配置原快照保护主模型出站；调用方必须在网络前结束自身读事务，并对响应帧与最终业务写回继续单独验证。
- 已完成 external 结果、旧 tool/assistant 消息和工具调用参数均递归删除与个人投影一致的内部保留键；嵌套 JSON 字符串可解析时重建，截断且包含保留键的旧文本整段隐藏。不会修改数据库中的撤销/恢复证据，其他合法工具值和 provider 协议签名保留。

## 测试

- 先以真实任务 ID 删除重用、模型发送后停用账号复现旧代码错误：2 failed，旧摘要路径均未拒绝失效执行体。之后补充队列／执行轮次更换、模型配置更换、历史修改、缺执行证据、协作 grant、待提交修改及主模型发送前身份变化。
- 补充旧 external 结果与历史 tool JSON 的私有字段反例，旧代码先失败；修复后覆盖嵌套字典、JSON 字符串、截断 JSON、工具调用参数及保留 provider 签名。
- `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_agent_context_execution_identity.py tests/test_agent_context.py --tb=short --maxfail=1`：**25 passed in 9.78s**。原上下文测试以真实 running Task 和原身份造数，保留摘要、日志、协议组及安全提示词原断言。
- 本切片 `ruff check`、`compileall` 与已跟踪文件 `git diff --check` 通过。
- `tests/test_agent_context_execution_identity_postgresql.py`：2026-11-10 由主会话使用本机 PostgreSQL 16.15、隔离 `collab_test` 随机 schema 和本机真实 HTTP 错峰运行，**2 passed in 26.74s**；验证任务执行轮次替换先于发送时无请求，以及账号停用等待共享发送事务到响应头、响应体未完成时即可提交并阻止旧摘要与日志。该次早于历史工具值过滤修复，最终代码需复跑。

## 边界

- 协作者 Agent 和共享模型未开放；本切片不声称 Agent 全链授权、费用、任务派生或撤权响应帧已完成。后台任务无 HTTP 原 token，以真实任务 actor 与账号启用状态为准。
- 当前沿用作者既有 `guard_provider` 与生产 SDK 的 hooks 合同；它不能证明任意第三方、伪称自己执行 hooks 的自定义 provider。若要承诺不受信工厂下的逐跳物理门禁，需要在整体实现中验证工厂来源或限定可信 SDK；FakeLLM 不是物理网络证据。
- 本切片不设计跨进程租约；既有任务引擎仍以单进程、更新前旧执行器已排空为部署前提。数据库失败和 commit 确认丢失时不盲目重发，最终恢复策略及批量/主任务持久收据仍需整体闭环。
