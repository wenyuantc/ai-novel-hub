# 实施计划：提示词自定义输入表单

前置：`11-08-prompt-components` 已完成并合入（依赖其异步 `snapshot_of` 与组件展开）。

## 步骤

1. **纯函数** `ai/skill_inputs.py`：`placeholders`、`resolve`（必填、下拉合法、长度、未知标识、默认值）、`fill`（单次替换、值不再解析）；单测。
2. **模型与迁移** `skills.inputs` 列（`0043_skill_inputs.py`）；`SkillInputDef` 与 `SkillCreate/Update/Brief/Out` 字段、`input_warnings`。
3. **快照**：`snapshot(db, user_id, skill_id, inputs)` 先展开再填充；`skill_log` 记 `inputs`；所有请求 schema 增加 `skill_inputs` 并透传（章节 generate/polish/revise、rewrite、batch、chapter_version、scene AI、区块改写、智能体消息）；`skill_inputs` 无 `skill_id` → 422。
4. **预览接口** `POST /skills/{id}/preview`。
5. **智能体**：`load_skill` 返回 `inputs` 与代填说明；触发词强制启用时 `forced_skill_message` 附定义；`start_task` 透传与校验，预览哈希包含 inputs。
6. **导入导出**：SKILL.md frontmatter `inputs` 的严格解析与导出；往返测试、YAML 炸弹／非标量拒绝。
7. **前端**：`SkillInputsForm`、`useSkillChoice`；六个入口接入（`ChapterEditorPage`、`RewritePanel`、`BatchGenerateModal`、`SceneWritingPanel`、`RichSectionRewriteModal`、`AgentConversationView`）；`SkillEditDrawer` 输入项编辑器与占位警告；类型同步。
8. **文档**：README；`.trellis/spec/backend/skills-mcp.md`（快照、导入格式、智能体）与 `frontend/component-guidelines.md`（`SkillSelect` + 表单复用）。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm run build && npx vitest run
```

必需测试：校验矩阵（必填、非法下拉、未知标识、超长、无 skill_id）、默认值补齐、旧正文 `{{x}}` 原样保留、各入口快照含填充正文且重试不变、批量逐章同值、智能体三条路径、导入导出往返与拒绝、前端表单必填阻止提交与切换 Skill 重置。浏览器实测：六个入口至少各走一次（FakeLLM），预览与实际注入一致。

## 回滚点

- 迁移独立可回滚（drop 列）。
- 步骤 3 改动所有入口 schema，完成后先跑全量后端测试再做前端。
