# Research: 可再次查看的密钥、鉴权与文档

- Query：支持密钥创建后再次显示 / 复制，独立只读鉴权和专属 OpenAPI 文档的最小兼容实现。
- Scope：mixed；本主题由主会话研究，与创作设定研究子代理互不重叠。
- Date：2026-10-30。

## Findings

### 用户确认与版本

- 用户明确要求“秘钥创建后也可以点击图片显示和复制”，理解为图标显隐和复制，不能采用不可恢复摘要作为唯一存储。
- 锁文件当前版本：FastAPI 0.159.1（backend/uv.lock:371）、cryptography 50.0.2（:303）、SQLAlchemy 2.1.6（:1055）、Pydantic 2.14.1（:790）。不需要升级或新增依赖。

### 复用现有基础

- `backend/app/core/security.py:47` 的 SecretCipher 已有 Fernet 加密 / 解密，InvalidToken 转为安全中文 ValueError。`backend/app/core/config.py:21` 的配置隐藏验证输入；encryption_key 已启动校验。
- `backend/app/services/ai_config_service.py:38` 的 to_out 可参考，但平台 Key 列表不需像供应商凭据列表一样逐项解密：创建时存掩码，专用 reveal 才解密。
- `backend/app/core/deps.py:60` / `:71` 现有认证 Cookie 优先、然后 Bearer JWT。新开放接口应有独立 APIKeyHeader 依赖；不能改变原依赖使全部内部接口同时接受平台 Key。
- `backend/app/core/errors.py:78` 的 HTTPException 处理器保留异常 headers，可承载 429 / Retry-After 和中文 detail；不必为此改变所有业务异常。
- `backend/app/services/project_transfer/registry.py:62` 的 NOT_EXPORTED 是授权关系表的登记点。`backend/tests/test_project_transfer.py:45` 只向引用 projects 的表扩散，因此 api_key_projects 需排除，api_keys 用户级表不应登记。
- `frontend/src/api/client.ts` 提供现有网页登录请求、错误处理与 401 行为；新管理 API 复用它。密钥明文放局部状态即可，不需新 store。

### 官方工具与调用形态

- [Fernet](https://cryptography.io/en/latest/fernet/)：提供经过认证的加密与解密；现有 SecretCipher 可复用。
- [Python 3.12 secrets](https://docs.python.org/3.12/library/secrets.html)：token_urlsafe(32) 使用显式 32 字节安全随机源，不使用 random；Key 添加 anv_ 前缀后计算摘要和密文。
- [FastAPI APIKeyHeader](https://fastapi.tiangolo.com/reference/security/#fastapi.security.APIKeyHeader)：定义 X-API-Key 并纳入 OpenAPI security；auto_error=False 后由业务统一输出中文 401。依赖负责提取，服务仍需校验摘要、到期、撤销、账号状态和资源授权。
- [FastAPI Extending OpenAPI](https://fastapi.tiangolo.com/how-to/extending-openapi/)：get_openapi 的 routes 参数允许以实际开放路由生成专属描述；在同一个 app 中另外提供 /api/open/v1/openapi.json，不替换现有 app.openapi。
- [FastAPI OpenAPI Docs](https://fastapi.tiangolo.com/reference/openapi/docs/)：get_swagger_ui_html(openapi_url=...) 可指向专属 JSON，swagger_ui_parameters 可关闭授权持久化。Context7 官方 FastAPI 索引已核对上述参数。
- [OWASP REST Security](https://cheatsheetseries.owasp.org/cheatsheets/REST_Security_Cheat_Sheet.html)：每次检查凭据与资源权限，Key 不放 URL，超额返回 429。
- [MDN Clipboard.writeText](https://developer.mozilla.org/en-US/docs/Web/API/Clipboard/writeText)：写剪贴板仅在安全上下文等浏览器允许的条件下可用，需处理 NotAllowedError；受限时保留手动复制路径。

### 方案边界

- 密文负责再次显示，摘要索引负责日常校验；列表 / 编辑响应不含密文、摘要或完整 Key。
- 管理接口走原登录认证，reveal 是所有者主动动作并 no-store；打开 / 复制、隐藏和迟到请求的清理都属于实际需求行为。
- 独立 APIRouter + 同 app 中的独立 docs 避免子应用缺少 app.state / 错误处理 / lifespan 的额外同步。
- 不引入 Redis；按 Key 的窗口 / 计数保存到数据库，由条件 UPDATE 原子执行，以免多 worker 各自持有内存额度。

## Related Specs

- `.trellis/spec/backend/auth-admin.md`
- `.trellis/spec/backend/quality-guidelines.md`
- `.trellis/spec/backend/database-guidelines.md`
- `.trellis/spec/backend/project-transfer.md`
- `.trellis/spec/frontend/type-safety.md`
- `.trellis/spec/frontend/component-guidelines.md`

## Caveats / Not Found

- 加密密钥轮换的自动迁移机制当前不存在，不能承诺更换 ENCRYPTION_KEY 后仍可恢复旧密文；列表掩码和摘要验证不依赖解密，用户仍可撤销 / 重建。
- 现有工程无通用限流；新增额度只约束开放读取，不应顺手改变旧登录 / 业务接口的请求额度。
- Clipboard API 在非安全上下文或异步取 Key 后可能受浏览器用户激活规则影响，需提供手动选择复制回退，不能假定所有环境都能直接复制。
