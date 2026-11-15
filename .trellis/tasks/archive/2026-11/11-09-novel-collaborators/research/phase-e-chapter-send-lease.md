# 章节逐跳发送的物理租约

2026-11-10。本记录只覆盖内部原语和专项测试；它不表示协作者章节 AI 已开放，也不表示 E 阶段已完成或具备部署条件。

## 契约

- 章节发送调用方用 `chapter_provider_factory(factory)` 构造实际文本及向量 provider。生产只接受 `create_provider`，并按 provider 名校验精确的 OpenAI 兼容、Anthropic 或 Gemini SDK 类型；登记的实例才能绑定章节。未知 factory 在调用前拒绝，自称支持 hooks 的第三方或直接构造而未经该 factory 登记的实例也不能使用章节发送。没有章节身份的既有通用 guard 保留原接口；接线方不得用它绕开章节专用 guard。
- `guard_chapter_provider(..., chapter=claim, authorize=..., verify=..., verify_preflight=...)` 固定服务器捕获的原 `ChapterClaim`，并固定 provider、stamp、sessionmaker、引擎、认证协调器和两类核验回调。每跳必须由对应 guard 的实际 request hook 建立独立发送事务，响应头后释放；claim 在附加 verify 回调前后各复核一次，防止遗漏回调或回调内改变身份。`require_sending_chapter` 拒绝 queued、伪造或旧 claim，检查原 Task UUID、queued_at、execution、取消标记、grant 和任务/章双向指针；查询不自动 flush，校验失败回滚，且只传播固定策略错误。
- 对精确 `_BoundModelProvider` 固定 model/type 与实际 SDK 的密钥、端点、timeout、follow_redirects；每次操作由原受信 factory 从该不可变快照构造未暴露的私有 SDK，沿用原 request hooks、重定向和超时设置。实际 HTTP ABA 反例让原实例在组装时短暂改成另一密钥/服务器并在 hook 前恢复，修复前请求误发至另一服务器；修复后仅原服务器收到请求，实际 Authorization 头仍为原凭据。快照仅保留内存且其 `repr` 不包含密钥。
- `authorize_send_actor` 在真实 `_send_lease` 内认证并锁住原 actor，物理共享可见性门禁与 stamp 校验之后，私有状态才允许章节检查；绑定同一 AsyncSession、当前 asyncio 任务、根事务、SQLAlchemy 连接及底层驱动连接。结束事务、重开事务、嵌套 SAVEPOINT、连接关闭/替换、身份不同或校验失败均不能复用。非 lease 调用只认证，不发放证明；`db.info` 和普通布尔值不能伪造证明。锁序为 User → visibility → Project → Task → Chapter → 配置；config 必须由调用方在章节校验后固定和锁定。
- `preflight()` 是只用于缓存等无网络路径的短物理 lease，返回时凭据及锁均失效。它和真实 send 使用不同的私有模式，`require_preflight_lease` 不接受真实 send，`require_actual_send_lease` 不接受预检或无 request hook 的直接调用；两者都校验 task/session/根事务/物理连接/actor/project/门禁。无一次性副作用的 `verify_preflight` 与真实发送的 `verify` 分别绑定，旧调用未提供预检回调时沿用原 verify。向量包装、叠加、缓存和每批真实出站均须固定同一 claim，不同 claim 或已注册包装被篡改时失败关闭。信任记录仅使用进程内私有状态，不能跨进程或跨任务转移。
- hook 未进 yield 前出现的普通异常转成无原始异常链的固定策略错误；响应/传输异常维持其原有语义。关闭发送事务遇到重复取消时等待清理；close 抛错时尽力 invalidate 连接，然后报告固定策略错误。若 invalidate 本身也失败，不能证明数据库连接和锁已释放，需依赖连接池/数据库处置，不能声称发送安全停止。

## 测试与边界

- SQLite `tests/test_chapter_send_lease.py`：最终 27 passed，含真实本机 HTTP、不同目标服务器/凭据 ABA、伪造/未登记 factory 和 provider、无 lease/无 request hook 直接调用、claim 被替换、verify 省略或修改 claim、预检与真实 send 模式挪用、不同 actor/任务、事务 commit/rollback/重开、嵌套事务、连接关闭、异常吞掉以及重复取消。布尔值 `True` 冒充用户 1 的真实出站反例先红后修，修复后包含在完整专项内。主动关闭底层连接的反例产生一条 SQLAlchemy 事务已分离警告。
- 所有本切片 Python 文件 Ruff 与 `compileall` 通过。既有 `tests/test_ai_visibility_dispatch.py` 本轮为 18 passed、1 failed、1 skipped：并发负载下 Anthropic tools 的响应头放行后，写端 150 ms SQLite 锁等待超时；该失败项隔离复跑 1 passed。仍需低负载完整复跑，不能将单项通过当作全套通过，也不能确认失败仅因调度。
- `tests/test_chapter_send_lease_postgresql.py` 已收集 8 项，文本和向量均以本地 HTTP 服务器及隔离 PostgreSQL 两种连接顺序覆盖复位后新抢占与撤权。主会话曾在加入私有 SDK 和 send/preflight 区分之前执行 8 passed；最终代码仍需由主会话错峰复跑，旧证据和收集通过不等于最终 PG 并发通过。
- 明确无网络的测试 Fake 可以在测试代码中模拟 request/response hooks，并实际取得短 lease；这个测试能力不证明真实网络行为。生产不接受任意无 hooks 或自称 hooks 的 provider 替身。
- 本原语不能撤回已经发送的内容，也不能证明执行体已停止。章节接线、配置快照及配置锁、任务结果持久化和失败清理仍需各自的事务证据；旧执行体排空、跨进程调度和恢复不由本切片解决。
