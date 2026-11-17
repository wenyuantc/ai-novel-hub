# 验证记录（2026-11-17）

## 自动化
| 项 | 结果 |
|---|---|
| 后端 `uv run ruff check .` | 通过 |
| 后端完整回归（SQLite，`uv run pytest`，启动于缺陷修复之前） | 6725 通过、845 跳过、19 xfail、1 失败 |
| 上述失败用例 `test_collaboration_wizard_entry_e4g3c.py::test_revocation_notification_after_commit_still_finalizes_from_receipt` | 单独重跑 3 次均通过，所在文件 95 通过；向导代码本次未改动。判定为高负载下的偶发（回归期间同机运行浏览器验收与其他测试），未做进一步根因分析 |
| 缺陷修复后重跑：全部 `test_consistency_*.py`（SQLite）、`test_task_sse_scope.py`、`test_collaboration_stream_early_subscribe_e5e.py` | 194 通过 |
| PostgreSQL 16.15（本地测试容器，库 `collab_test`）：`test_consistency_collaboration_postgresql.py` 等一致性用例 | 61 通过（缺陷修复之前执行，之后未重跑 PG） |
| PostgreSQL：`test_consistency_migration_postgresql.py`（0058 建表、拒绝有损降级、清空后往返） | 1 通过 |
| 前端 `npm run lint`、`npm run build`、`npm test` | 通过；240 文件、1694 用例 |

未执行：缺陷修复之后的后端完整回归（只重跑了受影响文件）；完整回归的 PostgreSQL 全量。

## 浏览器
见 browser-verification.md：13 项均有实际观察，佩剑示例全流程通过。发现 3 个缺陷，均已修改：
1. 证据高亮超出引句范围（`mentionSegments` 在焦点边界切段）——有单元测试，修复后未在浏览器复验。
2. 协作者进度流在慢模型下被中断——见下「已知限制」；前端轮询兜底有纯函数测试，修复后未在浏览器复验。
3. 键盘保存后焦点丢失——只过 lint／构建，无组件测试，未在浏览器复验。

浏览器未观察、仅由自动化覆盖的场景见报告「未测项」。

## 已知限制
- SQLite 上，模型请求在响应头到达前占用库级写锁，协作者任务流每帧也需该锁且 5 秒超时。一致性检查连续多次出站会放大该问题（既有的 `outline.expand` 同样受影响）。现缓解：协作者任务在 SQLite 上每次出站前让出 0.25 秒；前端始终轮询概要兜底。单次响应头超过 5 秒时协作者进度流仍会中断，页面靠轮询恢复。根治需改 `stream_lease.py`，不在本任务范围。PostgreSQL 按代码推断不受影响，未实测。
- 协作者在任务被领取瞬间订阅可能得到 `permission_denied`（既有行为），前端回退轮询并可重新连接。
- 超预算的大组拆批后跨批矛盾可能漏检，此时不标「本次未复现」并在概要提示。
- 测试使用隔离数据与 FakeLLM／本地假模型，不代表真实模型的检出率。
