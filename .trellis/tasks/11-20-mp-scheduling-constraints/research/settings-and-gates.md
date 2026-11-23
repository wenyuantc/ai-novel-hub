# Research: 系统设置的跨进程读取、发送门禁与周期重载

- Query: C2 如何让注册和 MCP 在下一次判定生效，让普通配置在一个调度周期内收敛，并保持保存、恢复和在途调用的现有语义。
- Scope: mixed，当前后端、测试、Trellis 契约，以及数据库与客户端官方文档。
- Date: 2026-11-23

## Findings

### 持久值、保存和恢复

| 源码（相对仓库根） | 当前行为 |
| --- | --- |
| backend/app/core/config.py:30、backend/app/schemas/system_settings.py:13、backend/app/models/system_settings.py:12 | 六项白名单，三项严格布尔、三项正整数，开放接口阈值最高 2,147,483,647。固定第 1 行，无行代表 revision 0。有效 revision 可以对应空覆盖。 |
| backend/app/services/system_settings_service.py:35、60、84 | 构造时保存六项部署基线和原始显式字段来源，先置本进程 settings_confirmed=False。用后台覆盖合并部署基线，同步写回同一个 Settings 的六个字段，再确认和通知调度。 |
| backend/app/api/v1/admin_system_settings.py:15、backend/app/services/system_settings_service.py:93 | 管理 GET 需要 settings.read，调用 load 重读持久数据；验证或读库失败记录固定日志，协调恢复并返回安全 500；GET/PATCH 均 no-store。 |
| backend/app/services/system_settings_service.py:122、148、173、195；backend/app/services/audit_writer.py:256、315 | PATCH 通过 shield 保护独立写任务，manager 锁内先重读，再取 runtime → 授权 gate → 升序 User → 设置独占门。事务内重验原操作者和 settings.update，比较 revision 和覆盖，第一次唯一 INSERT，其后条件 UPDATE，设置和审计同事务。确认 commit 后无 await 发布。异常时保留同一 runtime 立即重读真实持久值，不重放原候选。审计按 operation_id 查已提交结果。 |
| backend/app/services/system_settings_service.py:205、220；backend/app/services/task_engine/engine.py:825、1362 | 重读失败置本进程 uncertain 并关闭准入，每秒重试，最多一个恢复任务。close 拒绝新写，等待已受理的写，取消并回收恢复任务。dispatch 与 claim 两处都查 confirmed，已在运行的执行体可继续。 |
| backend/app/main.py:111、131、161；backend/tests/conftest.py:102 | 授权底座 → 设置 load → bootstrap → 任务恢复，app/engine 共用同一个 Settings。当前退出时先关 manager 后停引擎；启动在 yield 的 finally 之前失败也可能需要回收。 |

保存矩阵沿用 .trellis/spec/backend/system-settings.md 第 4 节。未登录/过期/禁用 401，缺权或等锁后降权 403，非法输入整批 422，旧 revision 或首次写竞争 409。审计/业务提交失败返回原业务错误或安全 500。提交已落盘但确认丢失仍可返回 500，重读并发布真实结果，不能写入相反的失败审计。不能确认时保留最后好值、阻止本进程新准入和继续覆盖；数据库读取失败绝不能解释为没有设置行。B 能自行确认时可继续使用数据库事实。

### 注册、插件与网络发送

1. **自主注册与公开配置。** backend/app/api/v1/auth.py:26、32 均读内存。用户创建、首次登录时间和提交在 backend/app/services/auth_service.py:29、57、79。建议在创建用户的同一事务先取 authz.gate，再确认本进程可判定并读取持久开关；这样 A 的关闭与 B 的注册按同一授权行串行，配置行不存在也受保护。若拆出 bcrypt，需保留关闭优先及重名 409，在真正写入前仍在门内复核。登录、管理员建用户、bootstrap 沿用各自入口。公开 GET 可用独立短只读查询、一次投影两个字段，保留匿名、no-store，无审计、无调度唤醒；无法确认时返回固定错误，既有前端保留最后一次成功的展示。
2. **现有两种物理门。** backend/app/services/mcp/settings_gate.py:44、70 的智能体发送要求干净会话、原 actor/token、冻结部署值、严格验证和本进程 confirmed。PG 管理器用已有双整数事务级 advisory 独占锁，发送用同键共享锁，锁后读最新记录，仅支持 READ COMMITTED，包括无行/首次插入。SQLite 的 _lock 分支本身未取物理锁；智能体发送通过 services/authorization_service.py:260 的 User 空更新取得物理写锁，管理写由同文件 105、237 行的 gate/User 取得写锁。不能直接把 SQLite 的 lock_for_reload 当成“已锁住设置直到实际发送”。backend/app/services/agent/mcp_tools.py:163 的完整发送租约还锁定作品、任务、提案、模型等证据。backend/app/services/mcp/client.py:121、157 只匹配一次 tools/call，收到响应头后释放；流正文可继续。
3. **管理入口。** backend/app/api/v1/mcp_plugins.py:21 的 require_mcp 保护列表、创建、导入、详情、修改、删除、工具偏好、刷新、试用九种操作。建议改为独立短会话中判定持久有效值，保留禁用 403「管理员已关闭 MCP」以及启用后原认证、404、校验顺序。A 已关闭时，B 的新操作应在查目标、解析地址、建立连接或调用前结束。backend/app/services/mcp/service.py:240 的导入按项提交；若逐项复核，全站关闭需向上返回，不能被逐项 AppError 包成 200，也不能将提交过的项描述为回滚。
4. **探测与连接。** mcp/service.py:129、169、199 的创建/改址/导入校验可能触发 DNS（backend/app/ai/endpoint_guard.py:26）。mcp/client.py:327 的 connector 在 yield 前已校验地址、打开传输并发送 initialize；实际管理调用在 mcp/service.py:296、344 又有连接后 list/call 判定。关闭后新请求在 DNS/connector 前短查；要挡住“已进处理、尚未发包”的业务请求，在实际 transport 处也要复核。mcp/client.py:296 的 list_tools 有分页，SSE GET、发往服务端指定地址的 POST、Streamable HTTP 初始化/通知/list/call/重连/清理都可能产生请求。不能仅以 fake.calls 为空作零外连证明。
5. **刷新与试用的物理边界。** 建议复用同一 PG 共享/独占 settings 键和 SQLite 物理事务协议，提取“校验持久行、合并原部署基线”的通用逻辑。路由在 core/deps.py:80 取得原 actor/token；试用已在 mcp/service.py:371 关闭请求会话，因此网络层要显式取得 sessionmaker 和不可变 actor。刷新时读插件、独立网络租约、写结果分开，避免 SQLite 在外层还有写事务时再开内层写会话。管理端 connector 显式接 gate，在两种传输的 HTTP request/response 钩子按请求持锁到响应头，保留逐请求 SSRF 检查；每页 list、试用 POST、重连都重新判定。关闭后允许本地释放连接，跳过会产生新外连的远端会话终止。拒绝结果要穿透 SDK/anyio 包装，不要在 refresh 变成插件 last_error 或普通 400。超时、断开、取消、无响应头也释放租约。已发送工具的正文继续读完，不自动重发。不能跨整个连接/正文持写锁，也不能将管理身份包装成具有作品/提案权限的智能体身份。
6. **智能体在途兼容。** agent/mcp_tools.py:309、400 仍从内存决定暴露的工具集；351 行的 resolve 专门把最终设置判定交给发送租约。新工具映射/展示若按“下一次判定”处理，可当次读取 DB 投影，保留旧 checkpoint 及原发送租约。智能体工具 POST 只取原来那一份完整租约，同一 SQLite 请求不可叠加两个独立写会话。backend/tests/test_agent_mcp_send_lease.py:201、283 刻意令远端先收到 initialize、尚未返头，此时管理员关闭成功，随后工具 POST 必须受阻。管理用的“握手持锁到响应头”不能无差别套在这条既有路径；智能体新连接可先短查，已有握手可结束，下一工具 POST 仍受强门。关闭先于新的刷新或试用则零连接。若管理侧某次请求先持物理发送租约，写方可等待该请求收到响应头，随后的新请求仍须重判。

### 普通配置的传播、读取与停止

- **两类读取。** 周期重载按 manager._lock → 本进程 runtime → DB 持久门读取，再在锁内同步发布同一 Settings。已持 authz.gate 的注册/领取等写事务可以直接读取这一事务中的持久设置；禁止在该位置反向等待 manager._lock 或另外开一个写会话。PG 延用现有事务门；后台只读刷新在 SQLite 回滚日志模式可在新会话显式 BEGIN 并实际读取配置表，使共享读锁覆盖验证与发布，参考 task_engine/stream_lease.py:204 的方式。WAL 的读锁不阻挡提交，若支持此模式应取得真实写互斥或失败关闭，不能把 WAL 读快照当发送门。
- **一个调度周期。** backend/app/services/task_engine/engine.py:255 的 poll_interval 默认 5 秒；791 行的调度循环可能在接管、取消扫描中等待，而且有引擎未运行的应用。可让每个服务进程的 manager 拥有单个独立轮询协程，使用同一 poll_interval 的单调期限；无任务、持续 notify、未确认、引擎停止但 HTTP 仍服务时都能刷新。每轮读取仅有六项的小行及 revision；若先只查 revision，变化或未知时在锁内另行读取完整行，不能把较早的异步 probe 回写。持久行未变化且已确认时只更新最后成功校验时间，不发布、不 notify；变化或从未知恢复才发布和唤醒。未知或检测到损坏必须全量验证，不能靠同 revision 清除故障保护。此安排较父设计第 3 节的“在调度循环内”细化了生命周期，若采用应同步说明。
- **领取按持久硬顶。** engine.py:1362 在既有 runtime、authz.gate 内取最新全局与继承阈值，用作这一笔领取的输入；无需从已持 gate 的事务调用 manager.load。用户配额三态及真实 actor 由 backend/app/services/quota_service.py:28、51 统一解析；limited=0 仍阻止领取，unlimited 仍服从全局硬顶。降限只挡新的不合格领取。quota_service.py:83 的 used 取本进程 admitted_count，应接 C2 的数据库已用并发查询，与执行者归属、恢复和成功回调的待定名额口径一致；展示和准入可按各自契约指定是否计成功回调。
- **实际消费者。** 自动分析在 backend/app/services/analysis_service.py:944 的单章成功、batch_service.py:177、193、1573 的批量预览及逐章阶段、rich_document_review.py:1188 的富文档审核通过派生。每次判断仍与作品 auto_analyze 同时为真，已排队、运行及手动分析照常。开放接口 core/open_api_deps.py:19 按请求传阈值；api_key_service.py:200 中认证与 UTC 分钟计数已由同一条件 UPDATE/RETURNING 原子完成。阈值变更不能重置 rate_count/rate_window，保留 429、Retry-After、失效 Key 401、单调窗口与原已在途认证语义。
- **不可读或卡住。** 读取/校验失败把该进程转为未确认，保留最后好值供诊断；新注册/MCP、领取不得用失效缓存自行放行。设置保存恢复与周期刷新共用等待/重读逻辑，一次只运行一轮，固定脱敏日志、有限频率重试。若数据库或事件循环停滞，一周期的绝对时钟保证物理上无法成立。建议用“上次成功读取的开始时刻 + poll_interval”界定缓存新鲜度，已过期的新消费在锁序允许的现有会话直接取有效值，或在无业务锁时等受控刷新；真相仍未知则固定可重试错误。开放接口不按旧高阈值扣量，自动派生按阶段跳过或给出可见告警；不可因单次读成功随意解除已在协调的本地未知提交。发生故障时不可中断已经授权发出的调用或正在执行的任务。
- **生命周期。** 关闭标记先阻止新轮询和设置写，令定时任务退出并 await，等待已经 shield 的写/必要即时重读，取消并收回恢复任务；晚到的读不再启动后台任务。先阻止引擎新领取，再协调 manager 清理以及在途任务收尾，最后 dispose engine。启动 load 或后续服务初始化失败也应清理；显式 load/close 的 fixture 与不触发 lifespan 的应用要有同等验证。不要让仅停止引擎的测试意外关掉 HTTP 正使用的设置能力。
- **默认值。** 以每进程启动时冻结的六项值与原显式来源为部署基线，只有缺行/缺键才取基线。后台 false 保留，PATCH null 删除覆盖并恢复部署值。同一组在线实例必须来自同份部署配置；未覆盖键若两个副本基线不同，当前数据库不可能给出共同有效值。更改基线应按整体部署完成切换，保留新应用启动时以新基线解释无覆盖键。不要把环境默认自动持久化为后台覆盖。自动比较实例配置若有需要属另外的协议，C2 可以记录前提及验证。
