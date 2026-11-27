# 实施与验证

- [x] 在 test_agent_conversation_final_snapshot 中控制两次读取之间的写入。分别只改 progress、更新 checkpoint 或 step_cursor、完成轮次；记录各自的状态码和响应内容，再覆盖编辑者自己的会话。
- [x] 在详情读取路径区分已验证身份的执行更新与身份失效，落实一次一致性重读。覆盖连续更新和终态提交涉及的会话、消息、提案变化，检查完整详情与私人字段过滤。
- [x] 回归真实撤权、令牌失效、会话或任务替换，以及既有消息替换测试；其他使用共享投影核验的接口保持现有契约。
- [x] 从 backend 运行 .venv/bin/python -m pytest tests/test_agent_conversation_final_snapshot.py tests/test_agent_action_identity.py tests/test_c5_agent_sessions.py，再按涉及的读取快照和权限模块扩测。
- [x] 对照 E4i-h 归档与当前前端调用链记录同源调查结论。将测试结果及限制回填任务。此改动仅涉及读取路径，回滚可还原对应代码及测试，无需数据迁移。
