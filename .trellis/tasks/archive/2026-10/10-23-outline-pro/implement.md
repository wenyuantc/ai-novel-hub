# 执行计划：大纲进阶

## 开工前

- 用 `ls backend/alembic/versions` 确认迁移编号。目前最新是 `0010_foreshadows`，本任务计划用 `0011`。
- 阅读这些 spec：
  - `.trellis/spec/backend/story-entities.md`：上下文组装、实体 AI 任务模式、章节号引用
  - `database-guidelines.md`
  - `error-handling.md`：任务契约
  - `frontend/component-guidelines.md`：向导、TaskPanel 结果注册表、编辑器
  - `frontend/quality-guidelines.md`

## 有序清单

### 后端

1. [ ] 模型与迁移 `0011_outline_pro`：新增 `chapter_plans`、`chapters.plan_id`、`outline_snapshots`、`projects.wizard_progress`。迁移测试指定版本号。
2. [ ] `chapter_numbering.shift_chapter_numbers` 与 `CHAPTER_NO_REFS`。补测试：
   - 每个登记字段都会平移；
   - 用反射扫描所有 `chapter_no` 和 `*_chapter` 列，确认都已登记；
   - 边界值和负数平移。
3. [ ] `plan_service`：
   - 细纲 CRUD；
   - 整体同步（有正文的保留，删除时返回 409；空章随细纲删除并做 -1 平移；最后重排 seq）；
   - 建章（绑定原有的 1 纲 1 章、确定插入点、平移、建章、写出场角色、双向冗余保持一致）。
4. [ ] `outline_service`：追加采纳（章节号顺延）、上移下移；`create_from_outlines` 跳过已展开的大纲。
5. [ ] `outline_snapshot_service`：
   - 保存与裁剪（保留 20 份）；
   - 恢复：先给当前状态存快照，再用原始 id 重新插入，PostgreSQL 校正 `setval`，恢复关联，全程一个事务；
   - 列表与手动保存。
6. [ ] `outline_transfer_service`：导出、预览、导入（append 顺延 / replace 先存快照；角色按名字匹配；上限 2MB）。
7. [ ] 提示词 `ai/prompts/outline_pro.py`，task handler `outline.continue`、`outline.expand`（超过 5 章分批、关键事件去重）。两者都只产出草稿。
8. [ ] 向导：`outline_mode` append/replace（replace 前存快照）；`wizard_progress` 的 PATCH 接口，以及在 ProjectOut 中返回。
9. [ ] `context_builder` 的本章任务优先使用细纲，同步更新文件头注释。
10. [ ] 路由与注册。
11. [ ] 测试：新增 `test_outline_pro.py`、`test_outline_snapshots.py`、`test_outline_transfer.py`、`test_chapter_numbering.py`，并扩展 wizard、context_builder、migrations、跨用户测试，对应 AC1–AC8。确认原有大纲和向导测试仍然通过。
12. [ ] spec：
    - `backend/story-entities.md` 或新建 `outline.md`：细纲、建章、快照、导入导出契约，以及章节号平移契约（新增字段必须登记）；
    - `database-guidelines.md`：迁移列表；
    - 上下文组装规则。

### 前端

13. [ ] 更新 `types.ts`、`api/outlines.ts`，补充 TaskKind 和标签。
14. [ ] 重做 `OutlinesPanel`：按卷分组、展开状态、细纲子表行内编辑、上移下移、工具栏。
15. [ ] 续写和展开的预览组件，注册结果查看器；导入弹窗；快照 Drawer。
16. [ ] 向导：大纲的追加/覆盖切换；按 `wizard_progress` 定位当前步骤。
17. [ ] 章节编辑器：展示细纲要点，预填 `est_words`。
18. [ ] 测试（纯函数）：卷分组、展开状态文案、预览编辑后提交数据的构造（保留项不能删）、导入预览展示。
19. [ ] spec：`frontend/component-guidelines.md`、`type-safety.md`、`quality-guidelines.md`。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build
```

手动验收（用假 AI 服务）：
- 续写 5 条，预览中删 1 条后采纳；
- 展开第 3 条并建章，确认后续章节号和伏笔计划章都平移了；
- 写入一章正文后重新展开，确认该章被保留；
- 向导用覆盖模式，再从快照恢复，确认章节重新关联、正文无损；
- 导出后导入到另一个项目；
- 向导中途关闭后能续跑。

## 风险与回滚点

- 章节号平移影响全局，必须保证登记表完整，并有反射测试兜底。
- 快照恢复时插入原始 id，需要处理 PostgreSQL 的序列。
- 回滚方式：回退代码，然后执行 `alembic downgrade -1`。
