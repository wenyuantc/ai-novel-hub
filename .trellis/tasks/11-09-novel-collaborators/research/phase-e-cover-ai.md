# 封面 AI 协作与图片出站

## 当前实现和开放状态

封面本地提示词使用真实 actor 的 `ai.use` 并在认证/User → 项目锁内完成投影。编辑者可访问，查看者 403，未授权用户和仅有管理员人工权限的账号 404。`visible_default_style` 仅将真实 actor 自有或全局预设风格拼入提示词；不可见的项目私人风格不返回名称、描述、基调或 ID，返回固定警告。提示词及图片预览均使用有界 ASGI 响应，防止慢客户端无限持有授权锁。手工上传、删除和原管理员权限路径保持原有行为。

图片生成的编辑者路径已在 SQLite 与 PostgreSQL 专项验证后开启。实现先在认证/User → 项目屏障内冻结原登录 token、邀请轮次和图片模型绑定，再结束请求事务，通过可信 `create_provider` 与 `SendGuard.for_http/prepare_provider` 逐次发送。本人图片配置优先；没有本人配置时，只有作者授权才选择作者图片配置，指定作者配置 ID 或本人配置无效都不能静默回退。外部模型错误向协作者返回固定安全提示，不回显作者配置、密钥、端点或模型名称；初始与最终数据库异常转换为固定 500，不将 SQL、参数或驱动异常链写入通用异常日志。生成只返回预览，不写封面表，也不新增无法取得用量的生成日志。

返回预览之前，服务重新取得原 actor、原 grant、项目与配置锁，校验原模型和可见性戳；原请求 session 的事务持续到 FastAPI 发送响应。协作者输出使用 5 秒有界发送窗口，慢客户端不能无限期持锁阻塞撤权；已发送的字节无法撤回。响应新增可选 `model_source: actor|owner`，只对协作者展示实际配置来源；作者原来的两个字段响应不变。真实 PostgreSQL 双连接验证了请求 session 在最终 ASGI 发送期间持锁，以及超时后释放锁。

## 图片 URL 下载和真实 SDK

原 OpenAI SDK 的图片生成请求使用受控 hooks，但 URL 下载另起不带 hooks 的 HTTPX 客户端。真实 SDK + 两个本机 HTTP 服务反例表明：生成响应头后撤销编辑授权，再放行含 URL 的响应体，旧代码仍向下载地址发送 GET 并返回图片。修复后下载客户端复用 `current_dispatch_hooks().event_hooks`，每个 GET 和 HTTPX 重定向都重新验证原 token、grant、模型绑定与可见性；同一封面请求不使用 one-shot 确认，因此合法尺寸重试与 URL 下载各自重新经过门禁。

原 URL 安全策略保持：私网关闭时验证下载端点且不自动跟随重定向；返回流逐块限制 10 MiB，下载客户端不携带模型 API 密钥。原 `endpoint_guard.py` 已明确的 DNS 校验与真实连接之间重绑定窗口仍存在，需网络层出站隔离；本改动不声称消除该风险。允许私网的本机 HTTP 测试仅用于观察真实 SDK 的发送次序，不构成 SSRF 验证。原 `tests/test_image_providers.py` 的私网、重定向、大小与凭据专项同时回归。

## 验证与边界

- SQLite 默认开放状态的封面专项、原作者封面、管理员人工封面、管理员不得代用 AI、原协作人工入口、风格与图片 provider/SSRF 回归共 **66 passed in 45.82s**（含 Gemini SDK 的一条废弃警告）。随后补充“作者未授权但编辑者可使用本人图片模型”的真实 HTTP 用例 **1 passed**。测试使用实际 OpenAI/Gemini SDK 和本机 HTTP，覆盖 URL 下载、下载重定向、尺寸错误重试、本人优先、授权回退、无授权不发送、不可信 factory 不执行、作者模型与数据库错误脱敏。响应头后撤权、重邀、作品删除、降级、关闭作者模型授权、原 token 失效和原配置变更均不返回图片；还覆盖冻结绑定后撤权零发送、生成/下载取消释放发送锁、最终 ASGI 发送期间撤权等待和慢客户端超时释放。普通查看者与管理员兼查看者均不能使用提示词或图片模型。
- 隔离 PostgreSQL 16.15 专项 `tests/test_collaboration_cover_ai_postgresql.py` 在本机 `collab_test`、随机 schema 下运行：临时启用时 **7 passed in 27.39s**；默认开启且测试移除开关 monkeypatch 后再次实跑 **7 passed in 29.22s**。覆盖绑定后撤权阻止首次出站、提示词持锁时撤权、发送持锁到响应头、撤权后禁止 URL GET、最终响应到 ASGI send 期间撤权等待，以及图片和提示词慢客户端超时释放锁：

  ```sh
  cd backend
  env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
    AINOVEL_COLLAB_POSTGRES_TEST_URL='<本机 collab_test 专用测试库 URL>' \
    uv run pytest -q tests/test_collaboration_cover_ai_postgresql.py
  ```

- PostgreSQL 用例通过后打开编辑者图片入口；不修改开关的真实 HTTP 作者／编辑者／查看者、原授权和下载竞争回归 **11 passed in 21.39s**。所有封面 PostgreSQL 用例也已移除开关 monkeypatch 并完成默认开放状态复跑。
- 上述验证只覆盖本地可控 HTTP 服务和实际 SDK，不证明外部供应商账单或已发请求可撤回。原作者生成行为、生成预览不落库以及人工上传/删除均继续保留；不把 fake provider 调用当作真实网络证据。
