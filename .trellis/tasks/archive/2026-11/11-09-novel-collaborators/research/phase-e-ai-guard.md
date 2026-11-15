# 阶段 E：内部 AI 发送授权守卫

2026-11-10 在本机 `collab_test` 隔离数据库（PostgreSQL 16.15、随机 schema）运行 `AINOVEL_COLLAB_POSTGRES_TEST_URL=<专用测试库 URL> uv run pytest -q tests/test_collaboration_ai_guard_postgresql.py --tb=short`：**2 passed in 11.29s**。这仅验证内部发送守卫的双连接竞争；真实任务接线后须重新验证全链路。

## 范围与接口

新增 `backend/app/services/collaboration_ai_guard.py`，不接任何生产路由、任务执行器或索引。它只为后续集成提供固定模型和发送检查，不代表 E1/E4/E5 或协作者 AI 已完成。

- `for_http(authenticated_db, actor_id, binding, stamp, *, cipher, allow_private=False, scene=None, after_verify=None)` 仅从认证 session 的 `authenticated_actor` 捕获原始 `AuthenticatedActor(id, token_version)`；缺失、不一致或非法快照失败关闭，不能从当前用户行重新构造 token。`for_task(task, binding, stamp, ...)` 从本执行者成功领取时的可信、已持久化 running Task 固定 `id/statistics_key/actor/project/queued_at/execution_key`，缺失或非法 UUID、原授权、服务端绑定均失败关闭；执行期间必须保留此 guard，不能从重读 Task 行重新获取其他执行轮次的身份；协作者不能以管理员人工能力替代 editor 身份。
- 文本/图片任务的 `_project_model`、共享向量任务的 `_shared_embedding` 和 `_collaboration_grant` 必须由服务端锁内覆盖写入。后台每次发送时对数据库 Task 行的身份、running 状态、取消意图、原服务端 payload 和所选绑定重新匹配；读取 payload 时使用模型原语的严格反序列化。关键词模式不得构造向量 provider，不能因协作者有自己的配置而转换成向量模式。
- `await guard.prepare_provider(sessionmaker, factory, request_timeout=...)` 在独立短事务内按 User → 项目 → 任务 → 模型配置顺序验证并锁住固定配置，复制配置快照后结束事务，才执行既有 DNS/端点检查和 provider 构造。返回的 `BoundProvider` 对象含服务端 provider、模型名、provider 类型及上下文/输出参数，不得将该对象序列化、写应用日志或返回客户端；可信生成记录可按来源合同在内部保存真实调用与用量事实，对外再按 actor 权限脱敏。provider 拒绝不同模型名及不匹配的 text/image/embedding 调用；其调用者不能提供自行选择的 provider 作为绑定证明。准备后配置发生变化时，真实发送仍按原 fingerprint 失败关闭，不能重解析、换配置或密钥。
- 内部 `authorize(db)` 在真实发送 lease 之前持原 User 认证读屏障；`verify(db)` 只应在共享发送门禁内使用，先取得项目共享行锁并复验可见性戳，再按需核验受控 `SceneContextStamp`、原 Task 执行行、当前协作成员、作者授权及原模型配置/指纹。共享向量还复验作者当前模型空间；锁持有至响应头后释放。它们作为回调一次性装入现有 `guard_provider`，不会剥离原本的 HTTPX 重定向/重试钩子。不要仅调用 `verify` 当成完整发送授权。
- 不提供任意接收数据库 session 的扩展 callback，避免反向锁序。可选 `after_verify()` 仅供纯内存一次性标记，不能做网络、数据库或阻塞业务；它不接收 session，完成后检查根事务身份并再次核验原 actor、Task 和模型。已有 `section_confirmation_used` 错误仅保留固定安全文本，其他异常统一为 `CollaborationSendBlocked`（`VisibilityPolicyError` / `RequestPolicyError`），不回显私有配置、SQL、密钥或输入。
- 如果之后接入 `retrieval_service.guard_embedder`，应使用该 `BoundProvider` 构造 embedder，并保留原 provider 的 authorize/verify 回调，不要重复追加同一回调或使用另一模型。缓存结果和写入事务仍需各自完成授权与版本/CAS 检查；不能把本守卫当作索引全链路完成。

## 验证

在 `backend/` 下执行：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_ai_guard.py --tb=short
# 33 passed in 7.36s。

uv run ruff check app/services/collaboration_ai_guard.py tests/test_collaboration_ai_guard.py tests/test_collaboration_ai_guard_postgresql.py
# All checks passed!

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_ai_guard_postgresql.py --collect-only
# 2 tests collected，未执行 PostgreSQL。
```

SQLite/Fake 覆盖撤权、降级、重邀、模型授权开关、账号停用、旧 token、配置更改、Task UUID/执行轮次/状态/绑定变更、取消意图、闭包事务提交/回滚/关闭、同事务伪造凭据、取消释放、配置快照后变化及错误脱敏。OpenAI 和 Gemini 使用真实 SDK 与本机 HTTP：撤权后的首个请求未到达服务器；OpenAI 307 重定向时首跳响应头后撤权，第二跳未到达服务器。Fake 的验证不代表真实数据库锁竞争。

需错峰执行真实 PostgreSQL（分别严格限制本机 `collab_guards` 专用库或标准 `collab_test`，随机 schema）：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  AINOVEL_COLLAB_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_test' \
  uv run pytest -q tests/test_collaboration_ai_guard_postgresql.py --tb=short
# 若已建立 collab_guards 专用测试库，可改用 AINOVEL_COLLAB_GUARD_POSTGRES_TEST_URL。
```

测试设计使用本机 HTTP 等待真实响应头及独立 PostgreSQL 连接，验证撤权/任务状态写等待受控发送和旧绑定禁止再次发送；尚未执行，不能计为通过。

## 仍需集成

- 当前任务引擎尚未完成 `execution_key` 的原子领取、未知提交确认和失败收尾；后台 guard 要求持久化执行 key，不为旧作者任务猜测身份。旧作者任务要在已排空旧执行器后走可证明的兼容/重新受理路径。
- 实际章节、改写、区块、向导、大纲、实体、分析、封面、Agent 与检索入口必须逐一从锁内事实构造绑定并接入此守卫，不能直接放宽旧 `get_owned_*`。选择优先级、自动派生任务及个人日志投影仍须对应实现与独立复核。
- HTTP 响应头之后的流输出、任务订阅、最终写入、撤权通知、章节/文档占用清理均需另外重验与 CAS；此 lease 不保证撤回已发送数据。OpenAI 图片 URL 的后续独立下载不在本 helper 的真实模型发送 lease 范围内，封面接入须单独处理。
- 当前真实模型/配置和 PostgreSQL 并发测试未覆盖旧进程恢复或多进程引擎；仓库任务调度保持既有单进程约束，不能据此声称多实例崩溃恢复安全。
