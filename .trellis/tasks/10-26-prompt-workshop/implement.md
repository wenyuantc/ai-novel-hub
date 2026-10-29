# 实施计划：实例内提示词工坊

## 顺序清单

1. [x] **模型与迁移**：`app/models/workshop.py`（四张表、分类常量），在 `app/models/__init__.py` 导出；迁移 `0020_prompt_workshop`（含部分唯一索引，downgrade 删表）。确认 `tests/test_migrations.py` 通过。
2. [x] **辅助函数公开化**：`style_service` 提供风格内容清洗与 `unique_name`；`prompt_service` 提供 `apply_save` 与"补当前版本"的 helper。行为不变，跑现有 styles / prompts 测试作为回归。
3. [x] **schemas + service**：`app/schemas/workshop.py`、`app/services/workshop_service.py`——提交、撤回、审核、直接发布、改元数据、下架 / 上架 / 删除、浏览、详情、点赞、导入、署名输出。
4. [x] **路由**：`app/api/v1/workshop.py` 注册到 v1 router；admin 接口用 `AdminUser`。
5. [x] **后端测试** `tests/test_workshop.py`，覆盖 PRD 每条验收：提交校验（预设、默认相同、校验失败）、审核通过 / 驳回 / 409、管理员自动通过与官方标记、更新期间旧版在架与计数保留、重复 pending 409、下架与恢复权限、匿名与删号署名、待审核提交随删号删除、点赞幂等与计数、导入风格重复只计一次、导入模板覆盖后可从历史恢复、模板升级后导入 400、跨用户 404、非管理员 403。
6. [x] **前端 API 与类型**：`src/api/workshop.ts`、`types.ts`。
7. [x] **前端页面与组件**：`WorkshopPage` + `components/workshop/*`；路由、导航；`StylesPage`、`PromptTemplatesPage` 的分享入口；模板导入复用 `PromptDiffModal`。
8. [x] **规范更新（Phase 3）**：新建 `.trellis/spec/backend/workshop.md`（权限矩阵、计数原子性、署名规则、导入契约），更新 backend / frontend index 与 `component-guidelines.md`；父任务进度与差距清单 C2 标注。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm run build && npm test
```

另在浏览器里走一遍：两个账号提交 → 管理员审核 → 另一账号浏览、点赞、导入风格和模板（看差异、从历史恢复）→ 作者更新并重审 → 下架 / 恢复。

## 风险与回滚点

- 部分唯一索引在 SQLite / PG 的写法差异：参照 `skills_mcp` 的 `postgresql_where` / `sqlite_where`。
- `author_id SET NULL` 与项目惯例不同，测试必须覆盖删号路径。
- 标签筛选用 JSON 文本 LIKE，依赖入库前规范化（去空白、去重、禁止引号）。
- 每一步可独立提交；回滚点是步骤 1（迁移）与步骤 2（公开化辅助函数）。

## start 前检查

- `implement.jsonl` / `check.jsonl` 已填入规范条目。
- 用户已确认最终规划摘要。

## 执行结果（2026-10-29）

实现及独立检查已完成，后端 939 项、前端 407 项测试通过，浏览器验收通过。详细证据见 `verification.md`。用户于 2026-10-29 授权按 `commit-plan.md` 提交，并要求归档、推送与合并到 main。最终提交与归档证据由收尾日志记录；推送需先补充当前未配置的远端 URL。
