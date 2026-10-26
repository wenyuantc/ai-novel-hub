# 执行计划：职业等级体系

## 开工前

- 用 `ls backend/alembic/versions` 确认迁移编号。当前最新是 `0007_story_entities`，本任务计划用 `0008`。
- 阅读 `.trellis/spec/backend/story-entities.md`，重点看 `apply_change` 模式、选角与上下文、卡片 v1。再读 `database-guidelines.md`、`frontend/component-guidelines.md`（实体任务预览和 TaskPanel 注册表）、`frontend/quality-guidelines.md`（包体积）。

## 有序清单

### 后端

1. [ ] 模型和迁移 `0008_careers`：新建 careers、career_stages、character_careers、career_history 四张表。迁移测试要指定版本号。
2. [ ] Schema：职业（含阶段整体提交）、持有人、挂接、历史、批量保存、生成请求。
3. [ ] `career_service`：
   - 职业 CRUD，阶段整体同步：带 id 的更新、新的插入、缺失的删除。被引用的阶段返回 409。同名、空阶梯、阶段重名分别给出对应错误。
   - 持有人查询。
   - `apply_assignment` 和历史，含上限、类别、项目归属的校验。
   - 批量保存。
4. [ ] 路由 `api/v1/careers.py`，再加上角色挂接和历史的端点，然后注册路由。
5. [ ] `cast_service` 和 `context_builder`：职业行、【职业体系】段、裁剪顺序、文件头注释。
6. [ ] 提示词 `ai/prompts/careers.py`，task handler 实现 `career.generate`（同名冲突时标记 conflict）。`character.generate` 草稿新增 `career_suggestions`，`with-relations` 支持 `careers`。
7. [ ] 向导：新增 `wizard.careers`（normalize、整体覆盖、注册 kind 和 resource_key）。角色向导在已有职业时分配职业，结果里返回 warnings。
8. [ ] 卡片 v2：导出、预览（careers 字段）、导入（职业复用或新建、阶段补齐、经 `apply_assignment` 写入），同时兼容 v1。
9. [ ] 测试：新建 `test_careers.py`、`test_career_tasks.py`，并扩展 `test_cards.py`、`test_cast_context.py`、`test_wizard.py`、`test_migrations.py`，覆盖：
   - AC1：阶段被引用时删除返回 409；重排后角色仍在原阶段；职业同名返回 409。
   - AC2：主职业、副职业的上限，重复挂接，类别不符，删除职业后级联清理。
   - AC3：跨级调整和降级的历史、只改备注也写历史、没有变化不写。
   - AC4：生成任务的草稿结构、冲突标记、不落库。
   - AC5：上下文里有职业行和阶梯，不涉及的职业不出现。
   - AC6：向导职业步骤可跳过，角色向导会分配职业，匹配不上时给出 warning。
   - AC7：v2 卡片往返、v1 卡片兼容。
   - AC8：跨用户返回 404、跨项目 id 返回 400。
10. [ ] spec：在 `backend/story-entities.md` 补职业部分（`apply_assignment` 契约、卡片 v2、上下文），在 `database-guidelines.md` 补迁移列表。

### 前端

11. [x] `types.ts`、`api/careers.ts`；把 `TaskKind` 的 `career.generate`、`wizard.careers` 和标签补全。
12. [x] `CareersPanel`（懒加载路由，加 NAV）、职业编辑抽屉（阶段排序、增删、持有人）。
13. [x] AI 生成职业的预览，并在 `taskResultViewers` 中注册。
14. [x] 角色抽屉的「职业」Tab（挂接、换阶、备注、卸下、历史），角色卡片显示主职业和阶段；AI 生成角色时的预览支持 `career_suggestions`。
15. [x] 向导新增职业步骤（可跳过、提示覆盖），角色步骤展示 warnings，`StoryPreview` 支持职业。
16. [x] 卡片导入预览显示职业新建或复用。
17. [x] 测试（纯函数）：阶梯显示格式（如「筑基（2/9）」）、阶段排序和提交数据的生成、挂接上限的前端校验。
18. [x] spec：`frontend/component-guidelines.md`、`type-safety.md`，`quality-guidelines.md` 里的包体积。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收：用假 AI 服务把这几项各走一遍：
- 向导：职业步骤 → 角色带职业。
- 职业页：编辑阶梯和查看持有人。
- 角色换阶和查看历史。
- 章节生成记录的提示词里有职业和阶梯。
- 卡片 v2 跨项目导入。

## 风险与回滚点

- 改造 `context_builder` 和角色向导会影响已有的生成流程，原有测试必须保持通过。
- 卡片 v1 必须仍能导入。
- 回滚：回退代码，再执行 `alembic downgrade -1`。
