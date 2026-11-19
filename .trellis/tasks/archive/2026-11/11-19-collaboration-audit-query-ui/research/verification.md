# 验收记录：协作审计查询界面（TODOS 第 21 项）

## 1. 隔离真实浏览器验收（桌面 1280×900 + 移动 375×812）

- 环境：系统临时目录独占 SQLite（从空库 `alembic upgrade head` 升至迁移头 `0064`）、现场随机密钥与回环随机端口、`FRONTEND_DIST_DIR` 同源托管最新生产构建包；本机 Google Chrome（headless）+ Playwright，按身份隔离 5 个浏览器上下文（`e2e_admin`、`e2e_owner`、`e2e_guest`、`e2e_stranger`、`e2e_noaudit`），未触碰产品 `.env` 或用户运行实例。
- 结论：**8 / 8 场景全部通过，0 失败**。

| 编号 | 覆盖场景 | 实测结果 |
| --- | --- | --- |
| S1 | 作品作者在「作品协作者」抽屉邀请 → 协作者书架接受 → 作者改权（查看者→编辑者 + 开启作者模型授权）→ 切到「协作审计」标签页 | 通过：展示 5 条倒序记录（含 2 条脱敏失败邀请），变更摘要准确显示 `查看者 → 编辑者` 与 `不允许使用作者模型 → 允许使用作者模型` |
| S2 | 作者侧防枚举与越权隔离（邀请已禁用账号 `e2e_disabled`、不存在账号 `e2e_nobody`；陌生人 `e2e_stranger` 越权邀请 `e2e_bystander`）；本地当日筛选与只读详情抽屉 | 通过：2 条 `invitee_unavailable` 均展示中性 `未公开 / 不可用`；列表与搜索 `e2e_disabled` / `e2e_stranger` 均为 0 条；按 `e2e_guest` + 当日命中 3 条；只读详情抽屉显示 `operation_id` 与隐私说明 |
| S3 | 协作者工作区与直调接口隔离 | 通过：编辑者打开共享作品无「协作者」按钮；直调 `/api/v1/projects/{pid}/collaborators/audit-logs` 返回 `404` |
| S4 | 系列作者在 `/series/{sid}` 卡片邀请/撤销后切到「协作审计」标签页 | 通过：展示 2 条系列记录（`设定协作者`），不显示作品模型授权字段 |
| S5 | 管理员在 `/admin/audit-logs` 切到「协作审计」标签页查询全站快照（含已彻底删除作品、非作者越权尝试、已禁用目标快照、`resource_id = 0` 无效邀请）与只读详情 | 通过：全量 10 条可见且无「按日期清理」按钮；`resource_id = 0` 精确命中 1 条；详情抽屉明确提示「不受管理操作日志按日期清理影响」 |
| S6 | 管理员在「管理操作日志」执行按日期清理，验证协作审计零损失 | 通过：清理弹窗与预览均明确提示「仅清理管理操作日志，不影响协作审计」；实际清理完成后切回「协作审计」仍为完整 10 条 |
| S7 | 仅有 `projects.read`、无 `audit.read` 的自定义管理员访问 `/admin/audit-logs` 与 `/api/v1/admin/audit-logs/collaboration` | 通过：页面被权限守卫拦截，API 返回 `403` |
| S8 | 375×812 移动端布局（作品抽屉、系列卡片、后台协作审计） | 通过：三处页面横向溢出均为 `0px`；后台协作审计移动端渲染卡片列表（0 个 `<table>`） |

## 2. 自动化门禁与双库验证

- **前端全量门禁**：`cd frontend && npm run lint && npm run build && npm test`
  - `eslint .`：0 错误、0 警告。
  - `tsc -b && vite build`：构建通过，无超限 chunk 告警；`CollaborationAuditPanel-*.js`（5.8 kB）、`CollaborationAuditDetailDrawer-*.js`（5.4 kB）、`AdminAuditLogsPage-*.js`（26 kB）均按需拆分，`RelationGraph` 隔离保持不变。
  - `vitest run`：**262 文件 / 1821 用例全部通过**（含新增 `api/collaborationAudit.test.ts`、`utils/collaborationAudit.test.ts`、`components/collaboration/collaborationAuditUI.test.ts`、扩展 `auditLogsUI.test.ts` 及 `noHardcodedColors.test.ts`）。
- **PostgreSQL 16 隔离专项**：在临时 `postgres:16-alpine` 容器的独立 `ainovel_test` 库（随机 schema，用后自动销毁）运行 `tests/test_collaboration_audit_query_postgresql.py -n 0 -v`：**1/1 通过**（覆盖 PG 原生 `TIMESTAMPTZ` 半开区间、ILIKE `%`/`_`/`\` 字面转义、作者防枚举/越权排除及 `audit.cleanup` 隔离）。
- **后端 SQLite 全量门禁**：`cd backend && env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run ruff check . && env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -n 4 --dist loadfile --durations=20 -q`
  - `ruff check .`：`All checks passed!`
  - `pytest`：**6955 passed, 10 skipped, 19 xfailed**（0 failed，耗时 38m21s；其中 1 项 skip 为 `test_collaboration_audit_query_postgresql.py`，已在上文隔离 PostgreSQL 16 专项通过）。
