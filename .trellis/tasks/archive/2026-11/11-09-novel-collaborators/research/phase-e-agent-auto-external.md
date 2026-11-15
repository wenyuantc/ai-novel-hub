# Agent 自动 MCP 调用：持久占用与未决结果

## 复现与当前开放边界

- 真实本机 HTTP 服务收到请求后阻塞，客户端超时；修改前的 Agent 仍把轮次标为成功并继续下一次模型调用，专项红灯可复现。MCP 连接在该测试中使用测试替身转发到本机 HTTP；它不是 SDK 真实发送门禁的证明。
- 自动 MCP 现暂时 **失败关闭**，`_require_auto_external_transport` 在创建占用或发送之前拒绝请求。MCP SDK 的网络发送发生在独立 transport 协程，普通事务预检无法证明真实 tools/call 发送与认证撤销顺序；不能仅把此函数改为返回成功来开放功能。
- 保留的作者自动调用成功用例在真实发送保护实现前不应视为通过；原手动确认路径不在本切片中改成自动调用。协作者 Agent 入口仍未开放。

## 持久协议（仅在测试替身显式模拟门禁时执行）

- 服务端先冻结原 actor、原认证 token_version、项目、会话、任务 UUID、queued_at、execution UUID；每次调用生成独立 change UUID、claim UUID 和开始时间，构造 `AgentChange` 的 `executing` 记录并提交。
- 内部 `result` 载荷有 `_external_claim_id`、`_external_task_key`、`_external_execution={queued_at, execution_key, actor_token_version}` 和 `_external_snapshot_mac`。MAC 绑定原任务、认证、项目、会话、原完整任务与提案快照及逐次占用，清理必须同锁重算并在 SQL CAS 比较原 MAC；密钥变化、缺失或篡改均保留屏障。私有字段须从公共投影、模型上下文和单书备份中屏蔽。
- 网络期间不持业务写事务；`cleanup_scope` 覆盖占用提交、实际调用、连接关闭和收尾。每次调用使用独立 `McpSendState` 区分发送前与发送后，不能用连接的共享状态判断。
- 正常结果和远端明确 error：原占用删除与工具消息写入同一事务，成功时不改变既有公开提案列表。提交响应丢失时重新核对原执行体、工具消息内容和无残余占用；不能确认时按未知处理。
- 发送后超时、断连、取消、鉴权变化或结果未能应用：原占用转为 `failed` 并保存 `_external_outcome_unknown`、原 claim、原任务与执行快照；会话和原轮次禁止重试、续跑、新派发，包括坏标记和换 call_id。发生在明确未发送前的取消或鉴权失败记录为普通未发送，允许按原任务身份安全重试。
- 自动请求使用 `preserve_outcome=True`，不信任 `read_only_hint`；结果未知不能转为普通工具错误交给模型继续推断或重发。恢复/清理无法证明原身份时保留已有屏障。

## 检查与交接

- SQLite 专项一轮 54 项通过，另补充非只读工具和重试来源模型实例替换等定向回归；覆盖真实 MCP SDK/本机 TCP 的成功、超时、取消、撤权和设置/实例替换，以及提交确认丢失、会话未知屏障、坏标记和旧身份清理。测试局部绕过自动入口硬关闭，但真实 SDK 用正常 request hook 和数据库门禁；另有不替换入口保护的测试验证默认不出站。
- `tests/test_agent_auto_external_fencing_postgresql.py` 已收集 3 项：新 claim 或 execution 先提交、旧清理先提交。使用 `AINOVEL_COLLAB_USER_DELETE_POSTGRES_TEST_URL` 指向本机 `collab_user_delete` 专库，或 `AINOVEL_COLLAB_POSTGRES_TEST_URL` 指向本机 `collab_test` 专库；尚待主代理错峰运行。该测试证明数据库双连接顺序，不证明 MCP HTTP 发送门禁。
- 已把原 `AuthenticatedActor`、项目/会话、原任务和逐次 claim 构造为不可变 `McpSendIdentity` 并交给 tools/call request gate；真实 TCP、授权/撤权提交顺序及 transport/请求错配仍待专项验收。未验收或无法证明时继续失败关闭。
- 未知结果仍需用户核对；目前没有可验证的完成凭据或解除未决证据的流程，不能静默清理或自动恢复。会话删除、作品和用户生命周期必须保留未决证据。
- Agent 受理使用 `_project_model` v2 绑定配置实例 UUID/指纹；HTTP 新消息与 retry 入口已接锁外预检、锁内同绑定复核。执行、事件、日志与终态不得用当前可复用配置 ID 补造原绑定；模型代码及全局迁移仍在独立集成与验证中。
