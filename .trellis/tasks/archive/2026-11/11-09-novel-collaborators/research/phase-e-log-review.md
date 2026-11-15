# 阶段 E：日志、Agent 效果与个人投影复核

## 范围与已修复问题

- 个人日志和 Agent 效果的最终投影改为独立会话同一 SQL 快照复核认证时 token 版本、账号、作品/成员状态及先前可见的私人引用。活动任务仍需原授权轮次、UUID 和入队时间，已结束本人日志可由 accepted viewer 阅读；无管理员人工权限旁路。投影后不修改 ORM 或持久 effects。真实 HTTP 竞态覆盖撤权 404、降级后历史日志仍可读但活动 Task 引用隐藏。
- 对 `_project_model`、`_shared_embedding`、`_chapter_claim`、批量及分析收据、任务原令牌代次、`_external_claim_id`、`_external_task_key`、`_external_execution`、`_external_snapshot_mac`、`_external_outcome_unknown` 和模型来源绑定等内部键，在结构化数据、diff、大小写与分隔符别名和嵌套 JSON 中递归过滤；截断的结构化内容按有限深度失败关闭。任意文本和跨 SSE delta 拼接不能依靠逐帧投影防泄露；模型上下文、日志及事件源头必须不生成其他人的私人值。
- 迁移 0013 保留的 `projects.style` 历史原文会随 Agent projects 效果的完整行出现。现在依据最终查询的真实作者身份，只对本书作者保留，协作者和兼任管理员的 viewer 不可见，非本项目或缺证效果也隐藏。其他对象同名普通字段、空值及数据库中的完整撤销快照不变。
- 章节生成 claim 三列不进入 Agent 快照、比较、对外效果或 `_values` 恢复。伪造旧效果不能覆写运行态，正常历史撤销仍可用。generation_logs/tasks 仅用作章节关联效果，恢复只允许 `id/project_id/chapter_id`，旧效果不能将模型来源、私人数据或任务执行身份写回。
- 撤销新增记忆时，在来源删除前用 `delete_project_refs` 按项目校验并删除索引；撤销已删除记忆时，新增来源前用 `require_unindexed_sources` 防止继承同书或他书孤儿索引。两者均在同一保存点内，损坏归属匿名失败关闭。
- `GenerationLog.model_source` 与 `model_config_owner_id` 为服务器专用列，来源只能由 `write_log(model_binding=ResolvedProjectModel)` 验证 actor/project/owner/config 后写入，客户端 params 同名键先清除，grant 由绑定重建。个人输出对作者回退仅显示 `model_source=owner` 并隐藏 config ID/provider/model；本人有证明的模型保留信息。旧无证日志仅当最终快照确认本人为作品作者时保持旧行为，协作者、管理员兼协作者均隐藏；配置删除后原来源与 token 用量仍保留。旧日志 params 的伪造键不作为证明。
- 采纳延迟提案时，若原模型配置被删除后同整数 ID 重新分配，`write_log` 以服务端绑定的 `config_instance_key` 和原配置所有者匹配并持共享行锁至提交；不匹配时仅将外键置空，保留真实来源、模型和用量。无可信绑定的历史提案由采纳路径避免补认配置 ID。
- Agent 单项操作使用 `change_key + view_version`，整轮使用原 `task_key` 加所展示提案的精确集合；缺/错身份、内容变化、增加/删除提案均失败关闭。工具预览旧行 `guards` 的无密钥 hash 可枚举私人风格 ID，公共/送模预览不展示其原值；确认版本以服务器派生密钥的域分离 HMAC 绑定完整 guard、已投影内容、真实 actor/project 和 change UUID。guard 变化后的旧确认失效；密钥轮换后用户须刷新。确认含其他不可见关键参数的提案拒绝；状态不纳入版本，保留同一提案重复确认/撤销幂等。内部 guard 和撤销效果仍在数据库完整保存。
- Agent 历史消息和提案仅当持久 `turn_task_key` 与当前本人可见任务 UUID 在最终快照相同，才显示原 `turn_task_id/turn_task_key`；旧 NULL 不能凭当前整数 ID 猜补。proposal/executed SSE 的实际行与 change_key 校验由引擎公开边界负责。
- 会话详情在生成活动 TaskOut 后，以最后一次 SQL 快照统一核验原令牌、完整会话/消息/提案和原活动任务事实，之后不再 await；同 ID 会话或消息替换、活动任务 UUID 复用与令牌失效均拒绝旧响应。活动任务 checkpoint 也通过个人投影过滤，原库值不改变。
- 外部 MCP 调用在 `executing` 时固定提案 UUID、调用 claim UUID、原任务 UUID、开始时间和服务端域分离 `_external_snapshot_mac`，MAC 绑定原 actor/令牌版本、Task 与提案快照及完整执行身份。恢复时核验 MAC、锁原任务并逐条 CAS 原始身份/快照；旧记录缺证、密钥轮换或证据损坏时保留执行屏障，不伪造失败。通常恢复需要可信本进程 engine 且该作品没有其它进行中的工作或任何 cleanup_scope（包括当前协程）；`stale_only=False` 仅依项目既有单进程升级前已排空旧执行器的启动约定。会话级联删除同样拒绝未决证据。
- 手动外部请求返回和异常清理时除 UUID、提案、MAC 与任务快照，还核验原 `queued_at` 和当时为空的 `execution_key`；同一 Task UUID 换轮次后，旧结果不能覆盖原提案或消除未决证据。数据库与模型结果保留完整内部值。
- 进入外部调用边界后出现超时、连接丢失、取消、崩溃或状态失效且结果无法证实时，持久 failed + `_external_outcome_unknown` + 原 claim/task UUID 与 MAC，并提示“外部调用结果未知，请核对”；不自动重发。`McpSendState` 每次调用独立记录是否通过实际 HTTP 请求钩子的发送门禁：本地 resolve/参数校验或连接前拒绝与远端明确 error 按普通失败；发送后异常/取消才是未知。连接复用时不得沿用上一调用状态。远端实际是否完成并不因本机超时而可知，删除保护须保留未知结果直至有可靠证据或后续产品定义人工处理。已收到正常远端响应、仅本地授权或删除意图使结果不能应用的情况，可按已有 drain 合同清理已完成本地执行。
- MCP 真实 Streamable HTTP / SSE transport 的每次 `tools/call` POST 在 HTTPX 请求钩子内取得独立发送事务：原认证版本、作品、任务完整快照、执行体、提案、占用 MAC、插件和工具映射均在锁内核验，一次性资格禁止重复 POST。响应头到达或传输终止后释放；响应正文/SSE 继续等待时不持门禁。PostgreSQL 用共享行锁与可见性共享门禁，SQLite 按现有发送规范以无值变化 UPDATE 持物理写锁到响应头；不持有提案占用事务跨网络。未经登记的自定义 connector 失败关闭。自动路径要求原领取执行身份与当前合法 checkpoint 一致，集成验证由 runner 负责人完成。
- 真实发送事务还在作品锁前取得全局 MCP 设置共享门禁，并读持久开关与原认证；管理员设置写通过同一门禁排他提交，初始无行由固定命名空间覆盖。Worker 与门禁内工具解析不以进程中可能陈旧的内存开关替代持久校验。工具发现和确认前解析仍使用本进程快照，跨实例恢复后可能暂时少列工具或误拒绝，须另行统一恢复语义；实际发送不得绕过持久门禁。

## 验证与待整合

- 修改前的多项真实 HTTP 红灯包括日志投影中撤权与降级、Agent 续跑后账号/会话/提案替换、提交后同秒 ID 复用、陈旧客户端提案/轮次/SSE ID、MCP 超时后远端仍继续。相应小范围测试已写入 `test_collaboration_log_private_views.py`、`test_agent_action_identity.py`、`test_agent_stale_change_identity.py`、`test_agent_unknown_outcome.py`、`test_collaboration_model_log_privacy.py` 和 `test_agent_recovery_claim_identity.py`。
- `tests/test_agent_unknown_outcome.py tests/test_collaboration_model_log_privacy.py tests/test_agent_recovery_claim_identity.py`：17 passed in 23.79s，含本机真实 TCP 握手前断开、HTTP 发送后超时/取消而远端继续、远端明确 error、配置删除后的来源、旧作者兼容、旧协作者伪造和无凭据恢复；同一连接多次调用的独立发送状态测试另有 1 passed。此前身份/日志/效果组合为 33 passed in 32.56s，身份/陈旧客户端/效果专项另有 20 passed in 19.08s。并行代码仍在变化，整合后的最后一次运行结果应另记。
- 项目未配置独立 Python 类型检查器；Ruff 与 `compileall` 可提供静态风格及语法验证，不能冒称类型检查通过。
- 新增隔离 PostgreSQL 的日志/Agent REPEATABLE READ、owner 模型配置删除及 recovery 双连接用例已写入 `test_collaboration_log_private_postgresql.py`，由主代理错峰执行；不以 SQLite 或旧 PG 结果代替最终门禁。
- 最新 SQLite 定向验证：来源日志 5 项、Agent 预检锁外与原身份失效 3 项、嵌套内部凭据投影 1 项通过；实际 SDK/TCP 覆盖配置同 ID 换实例、手动任务换 queued_at/执行 UUID、全局设置关闭前后与正文等待的三种竞争共 6 项通过。陈旧内存 false 而持久开关已恢复、持久关闭返回 `mcp_disabled` 另 2 项通过；真实 proposal 采纳的重用配置 ID、原配置删除和旧提案缺绑定共 3 项由提案负责人验证通过。
- MCP 真实 SDK/未知结果组合首次 33 passed、1 failed；失败是旧连接复用单测仍把过期内存开关作为发送门禁，现改成第二次真实门禁拒绝且断言只外呼一次，单项复跑通过。其他 33 项在该次运行通过；整合后的组合尚待主线复跑。
- 追加真实 TCP 取消交错：请求已发且响应头已到后原 Task 同 UUID 重新入队，再取消 HTTP 请求；旧清理保留原 executing/claim，不覆盖新轮次，1 passed。旧自动外呼成功类回归目前由明确的生产硬关闭阻断，不能通过放松门禁来消除红灯。
- Agent 客户端身份及会话最终快照两个专项整组复跑 16 passed；本范围 Ruff 和 `compileall` 通过。独立 Python 类型检查工具未配置，不将语法检查当成类型检查。
- 章节失败收尾使用真实 HTTP 建立协作关系、再以服务端原语固定 task/claim；撤 token、降级、撤 grant、移除成员、同 ID Task 换 UUID 和身份缺失共 6 项由章节负责人验证不新增 GenerationLog。此结果不代表协作者真实生成端到端已开放，PG 仍由主线错峰验证。
- PostgreSQL 设置共享/排他门禁已收集 8 项双连接竞争，主线首跑存在失败并正在定位，尚不能声称通过；最终整体组合仍须主线错峰验证。编辑者 Agent 与生产自动 MCP 入口保持关闭，未完成前不得据 SQLite 结果宣称可部署。作者手动确认兼容保留。
- 远程调用正在进行时取消 paused Task 再删除的真实 HTTP 红灯在 `test_agent_authenticated_actor.py::test_task_deletion_waits_for_inflight_external_claim`；持久删除/裁剪保护由 task_service 的对应负责人接线与回归。
- Agent 子任务 `result.task_id` 目前没有创建时捕获的原 UUID 证明，前端已禁用相关读取/控制；须由创建方持久绑定真实 actor/project/task UUID，最终验证后才可恢复。历史无证不得从当前整数 ID 补码。
- 真实协作者 Agent 的写授权、原 grant 和管理员审计交集尚需主线完成；目前 author-only 写入口不能当作 E5a 已开放。生成日志来源、富文档延迟采纳、单书备份和外部未知结果的跨模块全链仍需在并行修改稳定后统一回归，不代表整个任务已完成。
