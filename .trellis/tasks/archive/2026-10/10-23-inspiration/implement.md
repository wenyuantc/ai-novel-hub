# 执行计划：灵感模式与项目结构化字段

## 开工前

- 用 `ls backend/alembic/versions` 确认迁移编号。目前最新是 `0013_prompt_styles`，本任务计划用 `0014_inspiration`。
- 阅读这些 spec：
  - `backend/prompts-styles.md`：注册模板 key、锁定变量、同步 SSE 预览模式
  - `backend/database-guidelines.md`
  - `backend/error-handling.md`
  - `backend/outline.md`：`wizard_progress`
  - `frontend/component-guidelines.md`
  - `frontend/type-safety.md`

## 有序清单

### 后端

1. [ ] 迁移 `0014_inspiration`：
   - `projects` 新增 6 列；
   - 新建 `inspiration_sessions` 表。

   迁移测试指定版本号，并做往返验证。
2. [ ] 项目字段：
   - 模型与 schema：类型标签规范化，长度、数量和取值范围校验；
   - 把新字段加进 `ProjectInfo`，`format_project_info` 只在有内容时输出对应行；
   - `project_service.project_info` 带上新字段。

   补 `format_project_info` 的逐字段单测，并确认 parity 测试仍然全绿。
3. [ ] `inspiration_service`：
   - 会话的增删改查，每用户最多 50 个，超出时按规则淘汰；
   - 步骤的决定、跳过、保持不变、清除，以及 stale 的传播；
   - 补充想法、`suggest_scale`、转为项目（写入 `wizard_progress.params`）、应用对比与应用、从项目发起；
   - `state` 用 Pydantic 校验。
4. [ ] 提示词：
   - 注册 6 个 `inspiration.*` 模板 key（锁定输出格式与 json_rule），并补上示例变量，供模板页预览；
   - 实现 `normalize_options`。
5. [ ] 生成接口（SSE）：
   - 组装变量、流式输出；
   - 解析失败或选项不足时，在请求内重试，最多 2 次，并发出 `retry` 事件；
   - 成功后追加一轮选项，最多保留 10 轮；
   - 断开时不落库；
   - 同一会话的同一步骤重复请求返回 409。
6. [ ] 路由 `/api/v1/inspiration`，并注册到路由表。
7. [ ] 测试 `test_inspiration.py`，逐条对应 PRD 的验收标准，必须包含：
   - 生成成功后保存这一轮选项；
   - 首次输出非法、第二次合法时，只保存一轮，且出现过 retry 事件；
   - 连续 3 次非法时返回 error，会话不变；
   - 客户端断开时不保存，用 FakeLLM 加 Gate 在流中途断开；
   - 反馈和补充想法出现在 prompt 里；
   - stale 的传播与清除；
   - 规模建议的边界；
   - 转为项目：字段齐全，且 `wizard_progress.params` 已写入；
   - 应用对比：默认勾选只针对空字段，且只写勾选的字段；
   - 从项目发起时预填，同一项目重复发起返回同一个会话；
   - 跨用户访问返回 404；
   - 会话数达到上限时的淘汰与 409。

   同时扩展项目测试，覆盖新字段的校验与输出。
8. [ ] spec：
   - 新建 `.trellis/spec/backend/inspiration.md`，写清会话 state 结构、步骤与 stale 规则、SSE 事件、重试，以及为什么不写生成记录；
   - 更新 `prompts-styles.md` 的模板 key 表、`database-guidelines.md` 的迁移列表，以及【作品信息】字段说明（写在 story-entities 或 prompts-styles 中）。

### 前端

9. [ ] 更新 `types.ts`，新增 `api/inspiration.ts`，在 Project 类型中加入新字段。
10. [ ] 新增灵感列表页和会话页：
    - 步骤条、选项卡片（编辑后选择，类型标签多选）、换一批并附反馈、历史轮次；
    - 自己填或跳过、立项草稿侧栏、补充想法；
    - 规模一步与确认一步：创建项目并跳转到向导，或应用到已有项目（逐字段对比、勾选）。

    流式输出与 `retry` 事件的处理，组件卸载时中止请求。
11. [ ] 项目概览加入新字段和进度条，并加上"用灵感模式补全"的入口；新建项目弹窗加上可折叠的"更多信息"；项目列表页加上"灵感模式"入口；`WizardGenerator` 读取 `wizard_progress.params` 作为初始参数。
12. [ ] 纯函数测试：步骤状态推导、stale 文案、规模建议（与后端用例一致）、应用对比的默认勾选、类型标签规范化。
13. [ ] spec：更新 `frontend/component-guidelines.md` 和 `type-safety.md`。新页面懒加载，包体积不超过上限。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收（用假 AI 服务）：
- 从一个想法开始走完全部步骤，过程中至少做一次"换一批 + 反馈"、一次编辑后选择、一次跳过；回到书名修改后，后面的步骤标为"可能需要更新"。
- 规模一步输入 30 万字，看到建议值，改动其中一个数。
- 创建项目并进入向导：向导参数已预填，概览页能看到新字段和进度。
- 假服务第一次返回非法 JSON：前端显示"正在重试"，最终得到选项。
- 从已有项目发起补全：已有字段已预填；应用时默认只勾选空字段。

## 审查关卡与回滚点

- 第 2 步之后先跑 parity 测试和全部提示词测试，确认字段为空时提示词不变。
- 回滚：执行 0014 的 downgrade。会话功能独立，可以整体移除。
