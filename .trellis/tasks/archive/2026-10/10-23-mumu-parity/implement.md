# 父任务收尾计划

- [x] 确认最后一个未归档任务与 17 个子任务记录；识别并保留既有未跟踪 HTML 文件。
- [x] 补齐最终验收设计及子代理上下文清单，恢复父任务。
- [x] 复跑后端 Ruff / 全量 pytest，前端 lint / build / 全量 Vitest。后端 975 项、前端 417 项通过。
- [x] 在隔离环境复跑新建项目、四步向导、大纲展开、批量生成与分析、按建议重写、版本对比 / 回滚、EPUB / JSON 导出及新项目恢复。57 个 HTTP 请求、123 条断言通过。
- [x] 验证智能体、Skill、MCP 与工坊交叉路径及权限、脱敏、SSRF 边界。相关模块 144 项及临时补充 6 项通过。
- [x] 核对所有功能的最终归属、取消范围、17 个归档子任务与依赖；复查未复制文本，见 `research/final-coverage.md`。
- [x] 汇总新鲜验收证据，更新父任务验收勾选与最终进度；本轮无产品契约变更，无需新增 spec 规则。
- [x] 归档前核对完成：无产品代码待提交，任务资料由 `task.py archive` 提交，会话由 `add_session.py` 记录；归档结果以 `task.json` 的 completed 状态与会话日志为准。

## 验证命令

- `cd backend && LC_ALL=en_US.UTF-8 uv run ruff check .`
- `cd backend && LC_ALL=en_US.UTF-8 uv run pytest`
- `cd frontend && npm run lint`
- `cd frontend && npm run build`
- `cd frontend && npm test`
- `git diff --check`

每项以本轮退出码和输出为准。隔离接口脚本路径、命令及断言数量由对应检查子代理返回，汇总进最终验收记录。
