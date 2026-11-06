# 调研：限制保存与实际模型发送的先后关系

## 现有证据

- `backend/app/services/generation_service.py:440–515` 在首轮后重复使用 `job.messages`，不会回到章节上下文构建；`task_engine/handlers/chapter.py:95–120` 则在任务开始阶段能从原请求重新构造 Job。故“已入队”“已组装”和“已发给模型”是不同状态。
- `backend/app/services/agent/context.py:128–147,170–242,274–300` 有普通回放、摘要请求和缺工具行时的结果补齐。会话和工具结果没有可靠的历史字段级来源（`backend/app/models/agent.py:12–67`）；不能用字符串删减证明清理完整，亦不能单独删除回执造成不配对调用。Agent runner 续跑证据见 [AI 出口](ai-paths.md)。
- `backend/app/services/project_lifecycle.py:50–71` 的 no-op UPDATE 是现有跨 SQLite／PG 写屏障模式；长期持业务写事务会阻塞其他操作。AI 派生向量可能在构建 Job 或人工预览阶段调用，并早于生成请求（`context_builder.py:799–850`、`retrieval_service.py:142–146,238–244`）。
- 传输接入点为 `backend/app/ai/providers/openai_compat.py:43–61,138–159,221`、`anthropic.py:79–105,135–159`、`gemini.py:178–184,226–250,276–318,382–385`，现有入口均有客户端工厂；鉴权／内网与重定向配置不能放松。
- 工作区 `backend/uv.lock` 锁定 HTTPX 0.28.1，Gemini 锁定 SDK 有 `HttpOptions.httpx_async_client` 注入入口；本地安装源码 `google/genai/_api_client.py:918–925,1368–1376` 表明提供该客户端会使用 HTTPX 异步路径。OpenAI 兼容、Anthropic 已显式提供异步 HTTPX 客户端。以锁定依赖集做本地真 HTTP 测试，不能只 mock 业务工厂。
- [HTTPX 事件钩子](https://www.python-httpx.org/advanced/event-hooks/) 可在真实请求发出前检查、收到响应但未读取响应体时释放屏障。[HTTPCore trace](https://www.encode.io/httpcore/extensions/#trace) 更细，但事件随版本变化；本地 `httpcore/_async/http_proxy.py:282–289` 的 CONNECT 复用 request extensions，不能把任意 body trace 当作业务载荷已发。本期优先使用请求／响应钩子，不依赖 trace 或私有 SDK API。

## 方案判据

1. **不合格**：只在 UI 保存、建任务或首轮构建时验代次；只在网络前无锁 SELECT 一次。保存可在检查与真实 send 之间成功，旧负载仍会发出。
2. **不合格**：写入锁覆盖整个 SSE 或数据库事务包裹整个模型生成。会让修改策略与业务写入长期阻塞。
3. **采用**：调用携带该负载在构造时读到的作品／策略版本；每次有载荷的出站请求与策略保存使用同一跨进程发送屏障。请求钩子用独立短事务锁定并校验，直到 HTTP 响应头或本次传输失败才释放；不持有到响应体、流结束或任务保存。写入等待有界，超时返回可重试的明确状态，不返回假成功。跨连接有两个确定顺序：
   - 保存提交在先，旧戳的请求不能获得发送资格，网络未接到旧请求。
   - 请求先获得屏障，保存不能在其实际发送窗口中成功；窗口结束后可以保存，不等待已经发送的响应流结束。
4. 每个真正的 SDK HTTP 尝试都过门，不能依靠逻辑调用一次的守卫。被策略拒绝不作为可自动重试网络错误；需核验 SDK 的异常包装和 fallback。传输失败或取消按同一 guard 释放，自动重试／重定向若发生也必须重新验戳；保留原有 URL/SSRF 约束和代理。
5. 收紧导致旧会话和续跑失效，但不改人类存档。只携带原始作者输入、未发送且无受限派生副本的任务可安全从当前数据重建；若任务下一轮沿用上一轮提示词、Agent 历史、摘要或无法拆分的工作中间产物，应停止而非原样重试。已经提交业务结果不重新执行。

## 验证要求

- 受控 HTTP server 在已收到载荷后仅发响应头、阻塞响应体；验证保存能在流完成前成功。分别阻塞在准备完成／发送屏障内／请求失败三个位置，检查旧哨兵无重发、等待超时语义、回滚和任务终态。PG 使用两个连接验证锁顺序和作品互不阻塞；SQLite 验证物理写屏障，不假定 SELECT FOR UPDATE 生效。
- 三种 SDK 文本、Agent 工具聊天及支持的 embed 分别经过实际 HTTP hook，覆盖响应头前失败、5xx／重试、重定向／fallback、取消释放。无须向真实供应商发送测试资料。
- 同步检查封面生成只用作者请求 prompt（`services/cover_service.py:82`）、模型连通性测试、知识库原文索引、拆书原文和写作风格试写等无设定入口。若实现期间发现读取设定，必须纳入守卫再交付，不能因接口不在本表而例外。
