# 执行计划：章节版本与局部改写

## 开工前

- 先确认 `10-23-project-cover` 是否已合入迁移 `0005`（`ls backend/alembic/versions`），据此决定本任务的迁移编号（见 design §2）。
- 阅读 `.trellis/spec/backend/database-guidelines.md`、`error-handling.md`（SSE 契约）、`quality-guidelines.md`，以及 `.trellis/spec/frontend/component-guidelines.md`（章节编辑器部分）、`hook-guidelines.md`、`type-safety.md`。

## 有序清单

### 后端

1. [ ] 模型：在 `app/models/entities.py` 新增 `ChapterVersion`、`GenerationLog`，并从 `app/models/__init__.py` 导出。
2. [ ] 迁移 `0005_chapter_versions`（或 `0006`）：新建两张表、索引和唯一约束；在 `tests/test_migrations.py` 补充升级、降级测试。
3. [ ] Schema：在 `app/schemas/chapter.py`（或新文件 `chapter_version.py`）定义 `ChapterVersionBrief/Out/Update`、`GenerationLogBrief/Out`、`RewriteRequest`、`RewriteApply`；给 `GenerateChapterRequest` 加 `auto_continue`。
4. [ ] `app/services/version_service.py`：`record_manual`、`ensure_baseline`、`record`、`prune`、列表、详情、修改、删除、回滚；常量 `MANUAL_MERGE_WINDOW=10min`、`MAX_VERSIONS=50`。
5. [ ] `chapter_service.create_chapter` 和 `update_chapter` 接入 `record_manual`（只在 content 实际变化时触发）。
6. [ ] `app/services/generation_log_service.py`：写入（含每个项目保留 200 条的裁剪）、列表、详情。
7. [ ] 提示词：在 `ai/prompts/chapter.py` 加 `build_continuation_messages`；新建 `ai/prompts/rewrite.py` 实现 `build_rewrite_messages(op, …)`；在 `ai/prompts/__init__.py` 导出。
8. [ ] `generation_service`：成功落库时写 baseline、版本和日志；失败、取消时写日志（status=error/cancelled，用独立 session，放在 shield 里）；实现自动续写循环和 `continue` 事件，续写失败时在 done 里带 `warning`。
9. [ ] `app/services/rewrite_service.py`：`prepare_rewrite_job` / `run_rewrite_job`（只流式输出，不改正文）、`apply_rewrite`（乐观校验）。
10. [ ] 路由：版本 CRUD、回滚、日志、rewrite、rewrite/apply（见 design §6）。
11. [ ] 测试 `tests/test_chapter_versions.py`、`tests/test_rewrite.py`，至少覆盖：
    - 手动保存的合并：10 分钟内更新同一版本，超过窗口新建；用 monkeypatch 模拟时间；内容相同跳过；正文为空不写。
    - 上限：第 51 个版本删掉最旧的未标星版本，标星的保留，最新版本永不删除。
    - AI 生成：覆盖前补 baseline，生成后有 generate 版本；失败或断开时正文和版本都不变，日志记为 error。
    - 自动续写：FakeLLM 依次返回短文本和续写文本，结果出现 `continue` 事件、正文拼接正确、轮次不超过 2；`auto_continue=false` 时不续写；续写报错时保留已生成正文，done 带 warning。
    - 回滚：回滚前补 baseline，回滚后生成 restore 版本；生成中返回 409。
    - 局部改写：预览不改正文；采纳后选区被替换并生成 rewrite 版本；`original` 不匹配返回 409；偏移越界、选区为空返回 400；含 emoji 的正文码点偏移正确。
    - 越权：其他用户访问版本、日志、rewrite 接口一律 404。
12. [ ] 更新 spec：`backend/error-handling.md` 的 SSE 契约表（加 `continue` 事件和 done.warning，以及 rewrite 端点）；`backend/database-guidelines.md` 的迁移列表和版本写入规则。

### 前端

13. [ ] 安装 `diff` 和 `@types/diff`。
14. [ ] `api/types.ts` 镜像新 schema；新建 `api/chapterVersions.ts`；`sse.ts` 和 `runGeneration` 支持 `onEvent`，并补单测。
15. [ ] `utils/textOffset.ts`：UTF-16 与码点偏移互转，配单测。
16. [ ] `components/TextDiff.tsx`。
17. [ ] `ChapterEditorPage`：
    - 加版本抽屉（含版本列表和生成记录两个 Tab），实现对比和回滚。
    - 加局部改写 Tab，实现预览、对比和采纳；预览期间正文只读。
    - 加自动续写开关和续写进度提示。
    - 修改重新生成的确认文案。
    - 保留 spec 里编辑器第 1–8 条既有行为。
18. [ ] 更新 spec：`frontend/component-guidelines.md` 的章节编辑器部分、`hook-guidelines.md`（onEvent）、`type-safety.md`（新类型）。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm test && npm run build   # build 输出检查 chunk 是否 < 1500 kB
```

手动验收：本地起服务后，在一章里依次操作：手动保存两次（应合并）→ AI 生成（应出现 baseline 和 generate 版本）→ 局部改写一段并采纳 → 对比 v1 和当前 → 回滚到 v1 → 再撤销回滚。

## 风险与回滚点

- `generation_service.run_chapter_job` 是现有核心路径，改造后必须保证原有 9 个章节测试全部通过。
- 迁移编号与封面任务冲突：合并前再检查一次 `down_revision`。
- 回滚方式：本任务只新增表和接口，回退代码并 `alembic downgrade -1` 即可，不影响存量数据。
