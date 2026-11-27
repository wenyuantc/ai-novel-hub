# TODO #33 验证记录

## 第 11 项 E4i-h 关联调查

第 19 项的 verification-g7-g4.md 记录了作者 9/45、编辑者 11/55 的接口 404，轮次结束后恢复。该次浏览器操作未观察到页面提示。E4i-h 归档在 11-09-novel-collaborators/implement.md 的「发送后输入框偶发未清空」段记录了多个可能原因，没有与当次异常对应的会话详情响应体。

当前 frontend/src/api/agent.ts 的 isAgentAccessLost 将 404 归入失去访问。useAgentConversation.ts 在读取失败后可进入权限恢复流程；「作品权限已变化，请重新读取对话。」也可由读取被取代或前端作用域变化产生。可确认错误 404 会触发权限处理，无法将 E4i-h 当时的现象唯一归因于此。确认该次历史事件需要当时关联到发送动作的详情 GET 状态码、响应内容及提示时间。

本次执行前端 useAgentAction、useAgentConversation、agentCollaborator 三组现有测试，36 项通过。

## 并发复现与回归

修复前，对作者和编辑者分别在两次读取之间提交 checkpoint 更新、step_cursor 更新、轮次完成并更新消息及提案，六个用例均触发 404。另行复现了刷新同轮次提案预览时的 404。单独更新 progress 时仍返回 200；只把轮次改为终态也返回 200，但原接口可能附带过期的活动轮次。

修复后在 SQLite 和隔离的 PostgreSQL 17 上分别执行 test_agent_conversation_final_snapshot.py，各 25 项通过。覆盖同实例预览换签、旧签名失效、重读期间再次写入、令牌和资源身份替换、私人字段过滤。合并运行 C5 会话回归共 61 项通过。另验 SQLite 帧租约、任务和日志隐私共 40 项通过，PostgreSQL 智能体流与并发撤权专项 1 项通过。

后端实施回归合计 131 项通过（含上述 61 项、40 项及动作身份和角色矩阵 30 项）。独立审查另行复核协作入口、SQLite 帧租约、动作身份和会话详情。Ruff 和 git diff --check 均通过。
