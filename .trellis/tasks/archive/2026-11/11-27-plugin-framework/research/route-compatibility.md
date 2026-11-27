# 空登记表与 MCP 兼容验证

从提交 `87f01a65c52ba0caa61f29727531da2de18fbd6f` 用 `git archive` 提取旧后端，复制当前后端源码到另一临时目录。分别在独立 Python 进程中用相同依赖、临时配置和相同的静态目录构造实际应用，并断言当前登记表为空。通过 `fastapi.routing.iter_route_contexts(app.routes)` 展开实际挂载的路由，按原次序提取路径和 HTTP 方法，静态挂载单计一项。

| 检查 | 结果 |
| --- | --- |
| 基线路由项 | 607 |
| 当前路由项 | 609 |
| 新增 | `GET /api/v1/admin/plugins`、`PATCH /api/v1/admin/plugins/{plugin_id}` |
| 基线项及次序 | 删除数为零，移除两项新增后与基线列表逐项相同 |
| 既有 MCP 路由 | `/api/v1/mcp-plugins` 及其子路径共 11 项，路径、方法和顺序相同 |

## MCP 专项回归

- 前端在 `frontend/` 执行 `npm exec vitest -- run --maxWorkers=2 src/utils/mcp.test.ts src/api/mcp.test.ts src/components/skills/skillsMcpUI.test.ts`，3 个文件共 26 项通过。
- 后端在 `backend/` 执行 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_mcp*.py tests/test_agent_stdio_mcp.py tests/test_managed_stdio_workflow.py`，61 项通过（98.75 秒）。
