# 实施计划：提示词可复用组件

先于 `11-08-prompt-inputs` 实施。

## 步骤

1. **纯函数** `ai/components.py`：`find_refs`、`expand`（返回文本 + 区间表）、组件内禁止引用；`tests/test_prompt_components_expand.py`。
2. **模型与迁移** `models/prompt_components.py`、`alembic/versions/0042_prompt_components.py`；`tests/test_migrations.py` 补用例。
3. **服务与接口** `prompt_component_service`（CRUD、CAS、上限、使用关系扫描、删除／改标识 409、内容影响校验、导入导出）+ `api/v1/prompt_components.py` 注册路由（固定路径先于 `/{id}`）。
4. **模板覆盖接入** `prompt_service`：保存／导入／预览／回滚先展开再校验，行号映射；`get_override` 返回展开内容；导出展开；详情响应 `components`。
5. **Skill 接入** `skill_service`：保存／导入校验引用、全局禁止、`snapshot_of` 异步展开（更新全部调用点）、导出展开；`load_skill` 展开。
6. **工坊**：分享 `prompt` 时展开。
7. **前端**：类型与 `api/promptComponents.ts`；组件库页（列表、搜索、编辑抽屉、使用关系、导入导出、409 冲突提示）；路由与导航入口；`PromptTemplatesPage`、`SkillEditDrawer` 显示引用组件与插入组件的快捷选择；保存错误展示组件来源。
8. **文档**：README 功能说明；`.trellis/spec/backend/prompts-styles.md`、`skills-mcp.md`、`workshop.md` 更新契约（Phase 3.3）。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm run build && npx vitest run
```

必需测试：展开与行号映射、沙箱校验对展开结果生效（组件内含禁止语法被拒）、parity 不变、引用完整性（删除／改标识／改坏内容）、跨用户 404、快照不受后续修改影响（含重试）、导出／工坊分享无 `{{>`、全局 Skill 拒绝、组件库导入导出、CAS 409。浏览器实测：组件库页、覆盖与 Skill 编辑页引用显示、整章生成实际使用组件（FakeLLM）。

## 回滚点

- 步骤 2 后：迁移可独立回滚。
- 步骤 4、5 各自独立提交前跑全量后端测试；`snapshot_of` 异步化影响面最大（生成、批量、改写、场景、区块、智能体），单独核对调用点。
