# 本机插件实施与验证清单

本子任务依赖现有用户、MCP 管理与智能体能力，可独立于登录子任务实施。若两个子任务并行修改迁移链，两者合并时核对迁移依赖及最终 head。[需求](prd.md) · [设计](design.md)

1. **登记文件与数据模型。** 定义只读部署清单和安全的目录投影，新增个人目录引用和唯一约束。用 SQLite、PostgreSQL 验证升级前后远程配置一致、重复启用幂等、下架记录可见但不可执行。
2. **作者入口。** 在现有 MCP 管理页增加可用列表，复用个人启停、工具偏好和删除入口。分别验证表单、创建、更新及混合 JSON 导入，确认命令、启动参数及运行环境均不能由作者提交，查看和启用时进程启动次数为零。
3. **管理页真实连接。** 为 stdio 扩展现有连接器，按部署清单启动进程并注册退出清理。用仓库内的独立示例进程验证刷新、试用、错误提示、初始化超时、取消及进程树回收。检查进程并发上限和部署凭据脱敏。
4. **智能体真实调用。** 接入工具映射、确认、实际写入及外部调用恢复。阻塞在启动前和真正发送前，分别模拟关停、撤权、登记版本变更。用持久化计数的示例工具确认，多 worker 接管或发送后断连时不会再执行一次。
5. **部署与浏览器验收。** 记录部署者预装、登记和移除示例；在作者页面从列表启用并完成一次智能体确认。两名作者隔离验收，协作者按旧权限回归。双实例分别演练目录一致、节点缺插件及版本不一致，停止服务后检查子进程。保留旧 HTTP／SSE 的真实或既有回归证据。

| 目录 | 实施期间执行的基线 |
| --- | --- |
| `backend/` | `uv run pytest -q tests/test_mcp_plugins.py tests/test_mcp_settings_gate.py tests/test_mcp_sse_settings_boundary.py tests/test_mcp_cross_process.py tests/test_agent_mcp_send_lease.py tests/test_agent_mcp_skills.py` |
| `frontend/` | `npm test -- src/utils/mcp.test.ts src/api/mcp.test.ts`、`npm run lint`、`npm run build` |

基线之外补充目录校验、真实 stdio、物理发送和并发回收用例。记录演练使用的节点、插件版本、调用次数及进程状态。
