# 执行计划：章节分析与回写

## 开工前

- 用 `ls backend/alembic/versions` 确认迁移编号：当前最新是 `0008_careers`，本任务计划用 `0009`。
- 读以下 spec：
  - `.trellis/spec/backend/error-handling.md`：任务契约
  - `.trellis/spec/backend/database-guidelines.md`
  - `.trellis/spec/backend/story-entities.md`：`apply_change`、`apply_assignment`、选角与上下文
  - `.trellis/spec/frontend/component-guidelines.md`：编辑器、TaskPanel
  - `.trellis/spec/frontend/quality-guidelines.md`

## 有序清单

### 后端

1. [ ] 模型和迁移 `0009_chapter_analysis`：`chapter_analyses`、`analysis_changes`、`projects.auto_analyze`（回填 true）、`tasks.serial_key`；`VERSION_SOURCES` 加 `revise`。迁移测试要指定版本号。
2. [ ] 任务引擎：
   - 调度器支持 `serial_key`（同键串行、异键并行，与全局和每用户上限同时生效）。
   - 任务成功后调用 `on_success` 钩子，钩子出错只记日志。
   - 为以上补测试，原有调度测试必须全部通过。
3. [ ] `analysis_service`：
   - 构建提示词：涉及角色的当前状态，加上本章正文。
   - 运行与解析：规范化处理，结果带 warnings。
   - 计算本地指标、content_hash、过期判断、状态查询、历史查询。
   - `enqueue_auto` 和一键分析。
4. [ ] `writeback_service`：
   - 把分析结果转成变更条目：名字匹配、生成 `field_key`、无变化的跳过。
   - 自动应用；遇到更晚章节已改过的跳过；用 SAVEPOINT 包住，失败的条目单独标为 failed。
   - 撤销（冲突时 409）、确认、拒绝。
   - `organization_service.apply_membership`，统一在这里写入角色状态。
5. [ ] task handlers：
   - `chapter.analyze`：serial_key 为 `analysis:{pid}`，不写生成记录。
   - `chapter.revise`：在 generation_service 里加 revise 模式，提示词用 `build_revise_messages`；版本来源和生成记录都记为 revise；禁用自动续写。
   - Chapter 和 Revise 的 handler 在 `on_success` 里调用 `enqueue_auto`。
6. [ ] `context_builder`：前情提要改用最新一次分析的摘要，批量查询；更新文件头注释。
7. [ ] 路由 `api/v1/analysis.py`（见 design §8），项目 schema 加 `auto_analyze`。
8. [ ] 测试：新增 `test_analysis.py`、`test_writeback.py`、`test_revise.py`，并扩展 `test_tasks.py`、`test_context_builder.py`、`test_migrations.py`。
   - AC1：分析结果结构完整，历史保留。
   - AC2：过期判断；自动分析开关的开与关；手动编辑不触发分析。
   - AC3：一键分析按顺序串行；同一章重复触发会去重；分析失败不阻塞生成。
   - AC4：自动回写和待确认两类变更的分流；名字对不上时只提示，不写入。
   - AC5：撤销及冲突返回 409；更晚章节优先，被跳过的条目有记录。
   - AC6：前情提要使用摘要。
   - AC7：重写后的版本与生成记录；随后的自动分析和评分对比。
   - AC8：跨用户访问返回 404。
9. [ ] 更新 spec：
   - `backend/error-handling.md`：新增任务 kind、serial_key 语义。
   - `backend/database-guidelines.md`：迁移列表。
   - `backend/story-entities.md`：分析回写契约、`apply_membership`。
   - 补充上下文摘要的规则。

### 前端

10. [ ] `types.ts` 和 `api/analysis.ts`；`TaskKind` 加上 `chapter.analyze`、`chapter.revise` 的标签和跳转。
11. [ ] 章节编辑器的分析 Drawer：状态、评分与差值、指标、摘要、情节点、钩子、建议加重写表单、回写清单（撤销、确认、拒绝）、历史与评分走势。
12. [ ] 章节列表加"分析"列；项目概览加自动分析开关和一键分析。
13. [ ] 测试（纯函数）：分析状态标签、评分差值、回写条目的"前 → 后"展示格式、重写请求的构造与校验。
14. [ ] 更新 spec：`frontend/component-guidelines.md`、`type-safety.md`，以及 `quality-guidelines.md` 中的包体积。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收：用假 AI 服务依次走一遍：

1. 生成章节，确认自动分析。
2. 在分析面板查看结果，撤销一条自动写入的变更，确认一条待确认的变更。
3. 按建议重写，查看评分对比。
4. 手动编辑正文后，确认显示"已过期"，再一键分析。
5. 生成下一章，确认生成记录里用的是前情摘要。

## 风险与回滚点

- 改调度器（serial_key）和任务结束流程（on_success）会影响所有任务，必须保证原有的任务测试全部通过。
- 回写会动到角色、关系、境界、组织：重点看"更晚章节优先"和撤销冲突检测的测试。
- 回滚：回退代码后执行 `alembic downgrade -1`。
