# 实施计划：项目封面

## 后端

1. 模型与迁移：`entities.py` 新增 `ProjectCover`；`alembic/versions/0021_project_covers.py`（含 downgrade）；`app/models/__init__.py` 导出。
2. 图片工具：`app/services/cover_image.py`（或放进 `cover_service.py`）里的魔数识别 `sniff_image(data) -> mime | None`、`MAX_COVER_BYTES = 10MB`。
3. Provider：`base.py` 增加 `GeneratedImage` + 默认 `generate_image`；`openai_compat.py`、`gemini.py` 实现；`FakeLLM` 支持 `generate_image`。
4. 提示词：`app/ai/prompts/cover.py` 的 `build_cover_prompt(project, style)`。
5. 服务与路由：`cover_service.py`（get / put / delete / default_prompt / generate）；`api/v1/project_cover.py` 并注册路由；`main.py` 增加上传限制规则。
6. `ProjectOut.cover_updated_at`：`project_service` 的 `list_projects` / `project_out` 带出。
7. 备份：`registry.NOT_EXPORTED` 登记；exporter 写顶层 `cover`、estimate 计入；importer 校验与写入。
8. EPUB：`epub_service.build_epub` 增加 `cover` 参数；`export_service` 传入。
9. 测试：`tests/test_project_cover.py`
   - 上传 / 读取 / 替换 / 删除
   - 非图片与超限拒绝
   - 他人 404
   - 版本字段随替换变化
   - 生成成功 / 无图片配置 / provider 不支持 / 返回非图片
   - 默认提示词包含书名且要求无文字

   另外两处扩展：
   - `test_project_transfer.py`：备份往返后封面字节一致；损坏 cover 报错；无 cover 字段的旧备份可导入。
   - EPUB 测试：有封面时含 cover-image。

   Provider 单测用 mock client，覆盖 OpenAI 的 b64 / url / size 回退，以及 Gemini 的 imagen / generate_content 两条路径。

## 前端

10. `api/types.ts`、`api/projects.ts`：类型与接口函数。
11. `AIConfigSelect` 增加 `modelType` 参数（默认 text）。
12. `CoverPanel.tsx`、`CoverGenerateModal.tsx`；接入 `OverviewPanel.tsx`。
13. `ProjectListPage.tsx` 给 `BookCover` 传 `coverUrl`。
14. 若有纯函数（如 `coverUrl`、base64→Blob），补 `*.test.ts`。

## 验证命令

```bash
cd backend && uv run ruff check . && uv run pytest
cd frontend && npm run lint && npm run build && npm test
```

另外需要手动验证：启动应用，在概览页上传、AI 生成（预览 / 取消 / 确认）、删除；书架卡片显示；备份导出后再导入，封面仍在；EPUB 在阅读器里显示封面。

## 风险与回滚点

- 各中转对 `images.generate` 参数支持不一 → 已设计 size 回退；真实接口只能手动验证，自动化测试用 mock。
- `parse_backup` 对未知顶层键的行为需要确认（旧版本读新备份）。
- 迁移编号可能与并发会话冲突：开工前再查一次 `alembic/versions`，必要时顺延编号。
- 回滚点：后端步骤 1–6 可独立提交与验证；备份（7）、EPUB（8）、前端（10–14）各自独立。

## 执行结果（2026-10-29）

步骤1–14已完成，AC1–AC11均通过验收。自动化与浏览器证据见 [verification.md](./verification.md)。
用户已确认按 [commit-plan.md](./commit-plan.md) 完成本地提交、归档与记日志，再将 `codex/project-cover` 合并到本地 `main`。

补充界面修复：提示词计数进入正常文档流，935×564与375×812均验证不遮挡按钮；前端lint/build及414项测试再次通过。
