# 研究：聊天批量导出与渲染后权限复核

- 问题：核对 `design.md` 的多会话 proofs、无 TaskEngine 读取及渲染后的最终新快照复核是否兼容当前 `personal_projection`。
- 范围：仓库内部，限定技术可行性复核，不修改主计划或产品代码。
- 日期：2026-11-16。
- 依据：当前 `prd.md`、`design.md:101`–`:121`、`implement.md`，以及前一轮已定位的投影控制流。
- 未运行测试；本文不是验收通过声明。

## 研究结果

### 结论

方案可实施，无须 TaskEngine、TaskOut、AI 调用或数据库迁移。但不能仅将若干单会话 proofs 拼在一起调用现有函数；**批量会话证据归一化、同快照的原协作身份比较、原认证 actor 传递**必须作为新增 helper 的实际契约。现有函数不会自动替导出服务完成这些工作。

### 1. 当前可直接复用的能力

- `project_agent_payload(db, user_id, project_id, payload, proofs)` 不依赖 engine；`_project_payload` 先作私人范围初检、收集并过滤私人引用，再调用 `_final_visibility`，最后对原 payload 投影（`backend/app/services/personal_projection.py:670`、`:684`、`:731`）。
- `messages` 已是按全局 message ID 索引的映射，最终循环对每条消息比较完整 `MessageOut`、所属 conversation、project、actor，并校验发现的 ID 集合与证据集合相等，因此消息证据本身允许多个会话（`backend/app/services/personal_projection.py:602`）。
- 证据需保留完整 `MessageOut.model_dump(mode="json")`；用于渲染的 payload 可以只含标题、角色、时间和正文。完整证据与最小输出是两份独立纯值结构（`backend/app/schemas/agent.py:73`；`backend/app/services/agent/service.py:603`）。
- 无提案时 `changes={}`，无活动轮次时省略 `turn`；保留 `owner_only=False`。当前代码会直接索引 `agent_proofs["changes"]`，不能缺键；默认 owner_only 为 true，漏写会拒绝合法协作者（`backend/app/services/personal_projection.py:495`、`:570`）。

### 2. 必须补足：最终快照中的原协作身份

设计明确要求捕获并复核原邀请／revision（`design.md:115`、`:121`），但现有最终 SQL 的 auth CTE 仅取 member status、role、invitation_id，**没有 member.revision**（`backend/app/services/personal_projection.py:444`）。之后只验证 actor 有效、token_version、作品生命周期、当前 accepted 状态；邀请只被用于重算 `writable`（`:559`、`:628`）。

因此仅靠“最终仍 accepted”及旧会话 writable 比较，会漏过下面的路径：导出者已是 viewer 或正在导出旧邀请会话，期间被撤权后重新邀请并接受，旧会话 writable 前后都为 false，原内容会通过当前投影。角色往返、授权 revision 变化也未必改变 writable。

必需实现约束：

- 为导出专用证据保存原 owner ID、调用者身份、`invitation_id/revision`（作者对应无成员 grant），在**同一最终 auth 快照**加入 member.revision 并与原证据比较；不对既有所有 `project_agent_payload` 调用强制增加新参数。
- 初始只读合法性仍按 owner／accepted member；查看者或旧邀请会话可导出，不应因冻结 grant 而误加 AI 写能力要求。
- 不用两次独立 READ COMMITTED 查询分别比较“export 权限”和聊天证据来声称同快照保证；若 helper 先做一次 `require_project`，其结果不能替代 `_final_visibility` 内的最终 grant 比较。
- 不含聊天的文档也要走等价的最终认证／导出权限／原 grant 核验，否则设计 R4 只对包含聊天的文件生效。

### 3. 必须补足：批量会话证据的独立键和完备性

当前单会话证据为 `proofs["conversation"]`；最终 SQL 只加一个 `_agent_conversation` 行，之后 `next(...)` 只验证该单会话（`backend/app/services/personal_projection.py:517`、`:617`）。仅有 messages 证据并不能验证全部会话标题／创建身份／writable／sealed，更不能覆盖“空会话也被用户选中”的情况。

同时，现有 `proofs["conversations"]` **已经存在**，其含义是提案关联会话 `{id: created_at}`，用于 `_agent_change` 校验，不是完整会话 snapshot（`backend/app/services/agent/service.py:504`；`backend/app/services/personal_projection.py:584`）。新批量快照应使用独立键或有明确版本的结构，不能覆盖该旧含义。

兼容归一化应满足：

1. 把旧 `conversation` 和新批量会话证据内部归一化成统一 ID map，旧路径保持行为；若两种结构同时出现且冲突，拒绝，不允许后一份静默覆盖。
2. 最终查询每个所选会话，逐条验证 project、actor、created_at、原快照和 sealed；发现 ID 集合须与所选证据集合相等，不能只遍历数据库返回行。
3. 所有 message 证据中的 conversation_id 必须属于该次选择的会话 ID 集；使用服务器冻结的数据，客户端只传所选 ID。
4. 只选择 user／assistant 消息作输出，保持其完整证据；空会话、只有 tool 消息的会话也仍需会话级证据。最终是否保留空会话标题依 PRD 的空内容规则，不由核验 helper 决定。

### 4. 原 actor／token 的跨事务传递

`AuthenticatedActor` 只含 `id` 和 `token_version`（`backend/app/services/authorization_service.py:21`）。`visibility_sessionmaker` 只复制引擎及 authorization_runtime，**不会复制 authenticated_actor**（`backend/app/services/ai_dispatch.py:317`）。`_final_visibility` 从传入 session 的 `db.info["authenticated_actor"]` 读取原 actor；不存在时不会执行原 token_version 比较（`backend/app/services/personal_projection.py:436`、`:559`）。

设计已正确要求传递原 `AuthenticatedActor`（`design.md:119`）；实现必须把此项做成导出 HTTP 路径的非可选条件：捕获后关闭业务读事务，最终新 session 显式写入同一个原 actor，并核对 user_id 一致。不能在最终阶段用当前数据库用户的 token_version 重新构造 actor，否则令牌撤销会被“刷新成有效”。

该机制复用的是项目现有 token 版本撤销语义，不应表述为自动重放全部登录 JWT 校验。无需在本次扩张会话模型。

### 5. 渲染后比较必须使用原始冻结 payload

第一次投影后应分别保留：原始最小聊天 payload、完整 proofs、用于渲染的安全聊天纯值。渲染结束后对**原始冻结 payload**重新投影，以新的安全输出与已渲染安全内容作结构化相等比较；不能把已过滤内容当作新的原始材料，不能重查消息后重新冻结证据。

原因是 `_project_payload` 的私人引用收集来自调用时 payload，最终过滤据此决定安全输出（`backend/app/services/personal_projection.py:693`、`:731`）。再次读取新内容或覆盖 proofs，会把原本应拒绝的变化变成新的“合法输入”。证据只在内存核验结构保留，不放入 DOCX／TXT／Markdown 文档模型。

安全输出变化／证据变化应直接丢弃附件；设计中“最后复核后不再等待其他 I/O”与当前方法兼容，但不能宣称文件实际下载完成前仍可撤回。

### 6. 批量规模与数据库快照的实际限制

现有 `_final_visibility` 虽每 400 个 ID 建一个查询片段，但最终把所有片段组合为一个 `UNION ALL` 再执行；**400 分段并未减少整条 SQL 的总绑定参数数量**（`backend/app/services/personal_projection.py:458`、`:503`、`:524`、`:557`）。多会话及长聊天不能仅依靠该循环声称满足设计中的数据库参数上限要求。

需要在实现中给最终核验选定有界方案并验证：例如单个结构化参数形成引用集合，或在一个明确一致的最终只读快照内分批核验。若采用多条查询，须保持该次核验的同一快照和完整 ID 集合，不能在普通 READ COMMITTED 下逐会话查验后就认为全体原子一致。不要为了绕过参数上限退回早期逐会话安全检查。

这是批量实现必须解决的技术点，不要求改变“资料逐项勾选”的产品范围。

### 7. 现有保守行为需要准确写入验收

- 完整 `ConversationOut` 快照含 `updated_at`、`last_message_at`、summary、summarized_until 等字段（`backend/app/schemas/agent.py:50`）。因此捕获后新增消息或会话摘要更新也可能使完整快照不相等，导出报冲突；“新增消息不加入已捕获集合”不能被误读成“新增消息绝不使本次导出失败”。沿用当前保守行为可实施，无须扩大产品范围。
- 目前最终 auth 仅检查当下 deleted_at／deletion_started_at。若 PRD 的“回收后尚未交付结果失效”包括回收后立刻恢复，也需捕获并比较生命周期代次 `Project.last_deleted_at`；既有 `VisibilityStamp` 已使用此值（`backend/app/services/ai_dispatch.py:305`）。单靠最终 deleted_at 为空不能识别该往返。主会话应在最终方案里明确这一点，避免隐含不一致。
- 当前证据变化多返回 404“会话不存在”（`backend/app/services/personal_projection.py:602`、`:617`）；设计表中的“证据变化 409”需要导出专用错误约定或窄范围适配，不应修改旧详情的错误语义。

## 必要测试（方案可实施的验收前提）

1. 至少两个本人会话：其中一个被删除、换标题、消息被改写／删除／挪归属时全部附件拒绝；空会话也核验，未选会话消息不出现。
2. 旧单会话 conversation proofs 与既有 conversations 创建时间映射仍通过现有详情／提案隐私回归；冲突批量证据不能覆盖旧证据。
3. 作者、editor、viewer、旧邀请会话、管理员兼成员及仅管理员；渲染期间撤权→重邀、角色往返、revision 变化都使原请求失效，且普通 viewer 只读不要求 AI 能力。
4. 渲染期间账号停用、token_version 变化、作品回收／删除；最终新 session 传递原 actor，不能凭当前 user_id 通过。
5. 原正文内含私人 JSON／截断内部片段：第一次投影正确隐藏；渲染期间引用可见性变化时重新投影结果不同而拒绝，未使用 TaskEngine／TaskOut。
6. PostgreSQL 在渲染期间由第二连接提交变化，证明最终核验看到新状态；SQLite 与 PostgreSQL 验证跨 400 条消息及接近绑定参数限制的多会话输入，没有静默截断或参数错误。
7. 捕获后新增消息的既有保守冲突行为、无聊天时的最终权限核验、同一原 payload 二次投影无变化时正常下载。

## 相关文件

- `.trellis/tasks/11-16-word-content-export/design.md`：批量证据与渲染后核验方案。
- `backend/app/services/personal_projection.py`：原始 payload 投影、新快照权限与消息／会话证据比较。
- `backend/app/services/agent/service.py`：旧单会话证据装配、提案 conversations 映射。
- `backend/app/services/authorization_service.py`：AuthenticatedActor 与原 token_version 语义。
- `backend/app/services/ai_dispatch.py`：独立核验 session factory 与生命周期代次先例。
- `backend/app/schemas/agent.py`：MessageOut／ConversationOut 的完整证据字段。

## 相关规范与外部参考

- 相关规范：`.trellis/spec/backend/rich-documents.md`、`.trellis/spec/backend/agent.md`、`.trellis/spec/backend/rbac-quotas.md`、`.trellis/spec/backend/quality-guidelines.md`。
- 无外部库研究；本次仅核对已有项目控制流。

## 限制与未发现项

- 批量会话 snapshots helper 尚未存在；本文确认的是可实施路径和必须满足的约束。
- 本轮未启动任务、未实现、未运行测试，也未更改主计划。未发现必须扩展产品范围才能解决的技术障碍。
