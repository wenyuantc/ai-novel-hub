# E 阶段 AI 发送守卫独立复核

2026-11-10：本次只复核内部发送守卫并与章节 lease 原语对接，不开放协作者 AI 路由、生产任务或章节生成入口。

## 修复与证据

1. 真实本机 HTTP 反例证明：注入的第三方 provider 即使不接 dispatch hooks，或仅自称 `uses_dispatch_hooks=True`，原先仍能发出无物理门禁的请求。守卫现只接受仓库 `create_provider` 工厂，调用工厂前先拒绝其他工厂；生成后还核对模型提供商与 OpenAI、Anthropic、Gemini 对应的精确实现及其 hooks。测试 FakeLLM 经测试 fixture 显式注册并模拟本地响应头，实际不发网络，不能作为真实 HTTP 发送顺序的证明。通用 `ai_dispatch.guard_provider` 的其他历史调用方不因此自动获得此工厂限制，集成时必须另行核对。
2. 真实 OpenAI SDK 中，发送锁抛数据库或意外异常时原先会被转成普通模型错误，回显 SQL 和参数。发送 lease 的取得及清理错误现在转为固定且无原始异常链的策略拒绝；守卫对 text、tool、image、embedding 四类调用还检查异常链。原有正常模型错误及取消不被误当作权限重试。响应未知时 SDK 不自动重发，尺寸兼容重试及 307 重定向的每次实际请求重新获得 lease。若关闭连接和失效连接都失败，不能宣称底层锁必然已经释放。
3. 无 autoflush 的重验过去会覆盖 callback 尚未 flush 的撤权 ORM 改动，随后继续执行 Fake 请求。认证、验证及 callback 后现在拒绝有 pending、dirty 或 deleted ORM 对象的发送事务，避免误把数据库旧值当作本事务的最新身份；专项测试也断言没有触发 flush。
4. 任务 guard 现在拒绝把 Task UUID 复用为 execution UUID。`for_task(..., chapter=...)` 可固定一次服务端 `ChapterClaim`，构造时强制匹配 Task ID/UUID、actor、project、queued_at、execution、原 grant 和可选 scene 所属章；真实发送时先按 User → visibility → Project → Task → Chapter → 配置顺序在同一 lease 检查任务和当前章占用。章节 provider 还须经 `chapter_provider_factory` 登记并交由 `guard_chapter_provider` 校验来源。直接 `verify` 没有发送证明会失败；无 hooks Fake 不能获得章节 provider 资格。单章、批量、后续重试必须由接入方传入实际执行体捕获的原 claim，不能从客户端或数据库当前值重新构造。
5. 重复缓存预检过去会多次运行 `after_verify`，提前消耗一次性凭据。现在同一 guard 同时绑定 `verify` 和 `verify_without_one_shot`：前者要求绑定当前请求 hook 的真实 send lease，后者要求绑定同一 actor/project、连接和事务的 preflight lease，再完成原授权、任务、章节与模型检查。两种证明不能互换，直接调用 send lease、跨 actor/project 或预检结束后复用均失败；只有实际每跳发送才执行一次性 callback。预检返回不保留凭据，也不证明后续请求。

## 边界

- `chapter` 参数只对需要章节占用的任务按调用方显式传入；普通无章节 guard 行为保持兼容。未来章节生成、batch、提案及派生调用必须逐个接入并保证不可遗漏，也须处理写回、人工复位、取消、旧任务和提交确认未知；该内部接口不代表现有章节生成链已安全。
- 真正的响应头之后，发送锁不再持有；流输出、结果保存、个人日志和取消清理仍需各自复核授权/原身份。OpenAI 图片 URL 的独立下载不在本次模型发送 lease 内，封面接入需另行约束。
- 共享 embedding 的缓存命中必须复验身份/配置，必要时使用短暂的 `preflight()`，释放后不能作为下一次真实发送的证明；实际批次各自取得 lease。缓存层还需固定模型、provider、空间标识等，避免缓存向量被错误标记为其他模型空间；此项由 retrieval 负责。
- 先前共享向量 PG HTTP 测试在放行响应头前等待 `lock_visibility_write` 入口，但撤权先在目标 User 写锁上等待已持 User 共享锁的发送事务，会形成测试等待环。应先以发送连接 PID 观察真实 PG blocker，再放行响应头，不能把模型超时当作释放证据。

## 验证

- SQLite 与本机 HTTP/真实 SDK：`LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_ai_guard.py --tb=short`：**81 passed**。包括无 hooks 网络/冒充 hooks 反例、请求身份变化、脏数据不 flush、原 claim 复位、SQL 错误、取消、重定向及两种 lease 模式挪用。另 `tests/test_collaboration_retrieval_cache_preflight.py::test_cache_preflight_does_not_consume_one_shot_but_real_send_does` **1 passed**，实际 embedding 首发消费一次、缓存不消费、第二次发送拒绝。
- `uv run ruff check app/services/collaboration_ai_guard.py tests/test_collaboration_ai_guard.py tests/test_collaboration_ai_guard_postgresql.py` 与相关文件 `compileall` 通过。项目未配置独立 Python 类型检查器。
- 主会话在 PostgreSQL 16.15 的本机 `collab_test` 隔离库错峰执行 `tests/test_collaboration_ai_guard_postgresql.py` 与缓存预检 PG 文件，**合计 6 passed in 17.25s**；包括真 HTTP 下章复位等待响应头、旧 claim 后续不得发送，以及多次预检不消费、真实发送只消费一次。此前一次组合中的章节错误类型与缓存锁观测失败已分别修正，保持固定错误和实际数据库锁断言。
