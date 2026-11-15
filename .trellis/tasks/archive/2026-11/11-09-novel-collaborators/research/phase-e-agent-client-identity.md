# 阶段 E：Agent 客户端持有的对象身份

## 可复现的问题

- `backend/tests/test_agent_stale_change_identity.py` 保留了三项真实 HTTP 红灯。同一作者、同一作品、同一会话，删除旧 proposal 后同秒重建相同整数 ID、`created_at`、`tool_call_id`、任务 ID，旧客户端的 `POST /changes/{id}/reject` 仍以 200 拒绝了新提案。其他单项动作也只接收整数 ID；实际外部确认若指向替代提案可能调用错误的远端工具。
- 用相同 task ID 重建本人同作品的另一轮，旧 `POST /turns/{id}/reject-all` 以 200 拒绝新轮提案。`confirm-all` / `undo` 使用相同的整数轮次选择方式；整轮还可能包含客户端未展示的新提案。
- 同一重建场景中旧 `GET /tasks/{id}/stream?task_key=<旧任务 UUID>` 以 200 回放了新任务正文。现有 stream 入口只从数据库读当前 UUID 并传给 engine；它能拦截**建立连接之后**的替换，却无法识别客户端此前看到的旧轮次。普通 Task 流入口由所有功能共用，不仅是 Agent。
- 上述复现为同 actor、同作品的错误目标和状态；当前本人/作品鉴权仍限制跨 actor 数据。三项红灯已保留并在原身份协议接入后转绿，后续仍需最终 PostgreSQL 与前后端组合验证。

## 现有标识与边界

- `Task.statistics_key` 是既有的不可复用 UUID，允许为历史记录 null；缺失时不能把当前数据库值冒充客户端的旧凭据。`queued_at` 是同任务的新入队轮次，继续/重试时应按具体接口语义额外绑定。富文档已有对本人公开 task_key 的先例。
- `AgentChange` 目前只有可复用整数主键和 `(conversation_id, turn_task_id, tool_call_id)` 唯一约束；后者会在 retry 时重绑任务，删除后可重用。SQLite `created_at` 默认秒精度，不是身份。`updated_at` 亦不是可靠版本。
- 单书备份的 `project_transfer/registry.py` 明确排除 `agent_changes`、`agent_conversations` 和 `tasks`；单书导入不迁移这些运行态。整库备份保留数据库表和迁移版本，恢复旧库后必须先运行新迁移。

## 最小接口建议

1. `agent_changes.change_key` 使用服务器生成、规范小写、非空唯一 UUID；迁移对每条历史记录独立回填，不能基于整数 ID/时间确定。会话/任务关系改变时保持不变；删除后重建必须得到新 key。旧整库备份恢复必须先迁移，旧客户端打开的提案需重新读取。
2. `ChangeOut` 和已经过本人会话、任务授权的 proposal/executed SSE 投影提供可供客户端持有的身份；不能把任意嵌套对象或其他 actor 的 key 泄露给客户端、模型回执或日志。单项 `confirm/reject/undo` 必须携带原 key，持锁后和每次执行 CAS 比较 ID + key + 本人作品/会话，缺失和不匹配失败关闭；无数字 ID 回退。
3. 单项确认还应绑定客户端展示过的内容版本。对**按真实 actor 投影**的 `tool_name/kind/arguments/preview/turn_task_id/turn_task_key` 和完整内部 guards 用域分离的服务端 HMAC 绑定原 actor、作品及 change UUID；不会公开无密钥 guards hash 供枚举。状态不参与版本，使同一身份和预览的重复确认/撤销只返回现状；状态机仍禁止重复执行或反转结果。409 或密钥轮换后重新获取预览，由用户再次确认，不自动重放旧操作。
4. `TaskOut` 对有权读取本人的任务提供 `task_key`，由真实 `statistics_key` 产生；null 历史任务无法凭此操作。整轮 `confirm-all/reject-all/undo` 必须携带原 task_key 以及已展示的完整目标集合（每项 ID + change_key + 内容版本）。操作前在锁内核对集合，逐项执行前继续核对对应身份/版本；整轮撤销的集合检查和撤销保留在同一事务。不能扩大集合到新出现但用户未见的提案。
5. `/tasks/{id}/stream` 初次连接与每次重连均须通过 `?task_key=...` 提交客户端持有的原 task_key，入口比较后传原 key 到 `engine.subscribe`。新格式覆盖所有 Task 流，避免旧 Agent ID 被另一种任务复用；缺值失败关闭。前端 `TaskOut`、`useTaskStream`、`subscribeTask`、Agent timeline/API/类型与事件解析同步使用该参数。
6. `GET`、输出、cancel/retry/resume/finish/delete 等其它以 task ID 选择对象的操作也须独立审计客户端身份绑定；该接口变化不能由单项 Agent UUID 补齐。签名、前端缓存、错误码和旧客户端升级顺序由主线统一决策。

上述 UUID 仅是对象身份，不替代当前 actor 的身份、项目成员/邀请轮次和权限检查。不可把内部 `_external_task_key`、`_external_claim_id`、章节或共享向量占用字段当客户端凭据。
