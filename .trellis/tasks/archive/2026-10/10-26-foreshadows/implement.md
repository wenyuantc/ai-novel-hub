# 执行计划：伏笔管理

## 开工前

- `ls backend/alembic/versions` 确认迁移编号。目前最新是 `0009_chapter_analysis`，本任务计划用 `0010`。
- 阅读 `.trellis/spec/backend/story-entities.md`，重点看三处：分析回写契约、写入入口模式、上下文组装。再读 `database-guidelines.md`、`frontend/component-guidelines.md`（分析面板、编辑器、角色抽屉）、`frontend/quality-guidelines.md`。

## 有序清单

### 后端

1. [ ] 模型和迁移 `0010_foreshadows`，包括 foreshadows、foreshadow_characters、foreshadow_history 三张表。迁移测试用指定版本号。
2. [ ] Schema：伏笔的增删改查、快捷操作、统计、提醒，以及确认接口的 overrides。
3. [ ] `foreshadow_service`：
   - CRUD 和 `apply_foreshadow`（加历史记录）
   - 关联角色校验
   - 派生属性 `is_long_term`、`overdue_by`，`current_chapter` 取最大的有正文章节号
   - 统计和筛选排序
   - `reminders`
4. [ ] 路由 `api/v1/foreshadows.py` 和章节的 `foreshadow-reminders`，并注册。
5. [ ] 分析联动：
   - 提示词增加 `foreshadows_text` 参数、`ref_id` 规则和 `partial`。没有伏笔时提示词保持不变。
   - `analysis_service` 选取伏笔清单。
   - `writeback_service` 新增 3 个 kind，覆盖自动写入、跳过、更晚章节优先、撤销、确认（含 overrides 和同名 409），以及撤销已确认的新伏笔（等于删除，若伏笔已被修改则返回 409）。
6. [ ] `context_builder`：新增【伏笔提醒】段（放在前情之前），裁剪时先去掉"长线"，再去掉"即将到期"，都在"截断名单"之前。同步更新文件头注释。
7. [ ] 测试：新增 `test_foreshadows.py`、`test_foreshadow_analysis.py`，并扩展 `test_context_builder.py`、`test_migrations.py`、跨用户隔离测试。覆盖范围：
   - AC1：CRUD、角色解除关联、跨项目返回 400、快捷操作的必填校验。
   - AC2：按 id 回收、部分回收、撤销、无效 id 和无 id 的提示。
   - AC3：计划埋设自动标为已埋；新伏笔进入待确认；带 overrides 确认；同名返回 409。
   - AC4：五档的选取和上限、开关与状态过滤、裁剪顺序。
   - AC6：跨用户访问返回 404。
   - 无伏笔时，提示词和上下文都与原来一致。
8. [ ] 更新 spec：`backend/story-entities.md` 加伏笔一节（`apply_foreshadow` 契约、回写 kind、提醒规则、裁剪），`database-guidelines.md` 补迁移列表。

### 前端

9. [ ] `types.ts`、`api/foreshadows.ts`，以及回写条目的新 kind（`api/analysis.ts` 中的展示格式）。
10. [ ] `ForeshadowsPanel`：懒加载路由并加入 NAV，包含统计、筛选排序、列表加快捷操作、编辑抽屉加历史。
11. [ ] 时间线 Tab（纯 SVG）。
12. [ ] 分析面板回写清单展示 3 个新 kind；`foreshadow_new` 确认时弹可编辑表单。
13. [ ] 章节编辑器加"本章伏笔"Popover 和徽标；角色抽屉加"伏笔"Tab。
14. [ ] 测试（纯函数）：超期和长线的判定、时间线坐标计算、提醒分档的展示、overrides 构造。
15. [ ] 更新 spec：`frontend/component-guidelines.md`、`type-safety.md`、`quality-guidelines.md`。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收（用假 AI 服务）：
- 手动建两条伏笔：计划第 3 章埋、第 5 章回收。
- 第 3 章分析自动标为已埋，新伏笔确认时改标题。
- 生成第 5 章时，提示词里有"必须回收"。
- 第 5 章分析自动回收后撤销。
- 伏笔页的统计和时间线显示正确。

## 风险与回滚点

- 分析提示词和上下文改动会影响现有流程。无伏笔时必须保持原样，要有断言保证。
- 回写清单新增 kind，原有回写测试必须全部通过。
- 回滚：回退代码后执行 `alembic downgrade -1`。
