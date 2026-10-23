# 执行计划：结构化设定体系

## 开工前

- 运行 `ls backend/alembic/versions`，确认迁移编号。当前最新是 `0006_tasks`，本任务计划用 `0007`。
- 阅读这些 spec：
  - `.trellis/spec/backend/database-guidelines.md`
  - `.trellis/spec/backend/directory-structure.md`
  - `.trellis/spec/backend/error-handling.md`（任务订阅契约）
  - `.trellis/spec/frontend/component-guidelines.md`
  - `.trellis/spec/frontend/quality-guidelines.md`（包体积）

## 有序清单

### 后端

1. [ ] 模型与迁移 `0007_story_entities`：
   - 给 characters 和 world_settings 加列；新建 organizations、organization_members、relationship_types、relationships、relationship_history、chapter_characters 六张表。
   - 写入预置关系类型；回填 category 和 always_in_context。
   - 迁移测试按指定版本号执行，断言回填结果和预置类型数量。
2. [ ] Schema：角色和世界观扩展；新增组织、成员、关系类型、关系、历史、图谱、cast、卡片、生成请求的 schema。
3. [ ] `relationship_service`：类型 CRUD（预置类型只读）、关系 CRUD（含 pair 唯一、视角称呼）、`apply_change` 和历史、图谱数据。
4. [ ] `organization_service`：CRUD、循环检测、成员管理。
5. [ ] `cast_service`：`is_protagonist`、`select_cast`（名字和别名匹配，含最短长度和长名优先），以及 cast 的读写。
6. [ ] 重写 `context_builder` 的角色部分（涉及角色卡、关系、组织、名单），世界观按分类输出，裁剪顺序按 D4 落地。更新文件头注释。
7. [ ] `card_service`：导出、导入预览、导入（名字映射、重命名、类型匹配、单事务）。
8. [ ] 提示词 `ai/prompts/entities.py`，以及 task_engine 的 handlers：`character.generate`、`organization.generate`、`relations.parse`。
9. [ ] 向导适配：角色向导输出结构化字段和关系，并写关系表；世界观向导输出 category；保持兼容旧格式。
10. [ ] 路由：organizations、relationships、cards、cast、`characters/with-relations`、三个生成端点，在 `api/v1/__init__.py` 中注册。
11. [ ] 测试（新增 `test_organizations.py`、`test_relationships.py`、`test_cards.py`、`test_cast_context.py`、`test_entity_tasks.py`，并调整原有测试）：
    - AC1：迁移保留并回填数据
    - AC2：组织循环、成员唯一
    - AC3：视角称呼、角色对唯一、历史、级联删除
    - AC5、AC6：生成和解析任务（FakeLLM 输出 JSON），草稿不落库，保存后落库，未匹配的名字被标记
    - AC7：卡片跨项目往返、冲突处理、类型匹配与新建
    - AC8：30 个角色中正确选角，包括别名和手动指定；预算裁剪顺序；"林"与"林远"的误命中用例
    - AC9：向导新旧两种格式都能解析
    - AC10：跨用户访问返回 404，导入超限或格式错误返回 400
12. [ ] 更新 spec：
    - `backend/database-guidelines.md`：新表、关系语义、`apply_change` 契约
    - `backend/directory-structure.md`
    - 上下文组装规则：写进 database 或新建 `context-guidelines`，以现有 spec 的组织方式为准

### 前端

13. [ ] 依赖：`@xyflow/react`、`@dagrejs/dagre`。`types.ts`，以及 `api/organizations.ts`、`relationships.ts`、`cards.ts`、`story.ts` 的扩展。
14. [ ] `CharactersPanel` 重做（抽屉分 Tab、旧备注只读加解析、生成、导入导出、勾选）。
15. [ ] `OrganizationsPanel`（树、成员）、`RelationshipsPanel`（列表 + 懒加载图谱），加入 `ProjectLayout` 的 NAV 和路由。
16. [ ] `WorldPanel` 按分类展示；`StoryPreview` 适配向导的新结果格式。
17. [ ] AI 预览组件（角色、组织、关系建议）。`TaskPanel` 增加"kind → 结果查看"的注册表，已结束的生成任务可以从面板打开预览。
18. [ ] 卡片导入弹窗（读取文件、预览、冲突选择、导入）。
19. [ ] 章节编辑器加"出场角色"多选。
20. [ ] 测试：纯函数部分，包括视角称呼换算、亲密度颜色、卡片冲突重命名预览、组织树构建。
21. [ ] 更新 spec：`frontend/component-guidelines.md`（新页面、编辑器 cast、TaskPanel 扩展点）、`type-safety.md`、`quality-guidelines.md`（包体积，图谱 chunk）。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build   # 主 chunk < 1500 kB，图谱单独 chunk
```

手动验收（用假 AI 服务）：
- 角色的增删改查和关系视角显示
- 图谱的筛选和点击
- AI 生成角色时关闭页面，从任务面板打开预览并保存
- 旧备注解析
- 卡片跨项目导入，含重命名
- 章节出场角色在生成记录的提示词中正确体现

## 风险与回滚点

- 改造 `context_builder` 会影响所有章节生成。原有的 `test_context_builder` 必须继续通过，或者在不减少覆盖的前提下调整。
- 向导输出格式要兼容新旧两种。
- 包体积：如果图谱懒加载后主 chunk 仍然超限，就把其他页面也改成懒加载，不允许调高上限。
- 回滚：回退代码，再执行 `alembic downgrade -1`。
