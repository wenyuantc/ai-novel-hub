# 项目封面：上传与 AI 生成

## Goal

为小说项目增加封面图。用户可以自己上传图片，也可以调用已配置的「图片模型」由 AI 生成。封面在概览页和「我的项目」书架卡片上展示，随项目备份保存与恢复，并作为 EPUB 封面导出。

## Background（代码证据，2026-10-29 复核）

- `Project` 模型 `backend/app/models/entities.py:171` 无封面字段；最新迁移为 `0020_prompt_workshop`。
- 模型配置区分 text/image（`resolve_config(..., model_type=...)`，`backend/app/services/ai_config_service.py`）。代码里还没有调用图片生成 API 的实现，`LLMProvider`（`backend/app/ai/providers/base.py`）没有图片方法。Anthropic 本身没有图片生成能力。
- 上传有现成做法：`UploadFile` + `UploadLimitMiddleware`（`backend/app/core/upload_limit.py`），参考 `backend/app/api/v1/book_imports.py:30-40`。
- 项目备份 `backend/app/services/project_transfer/` 要求关联表登记或写进 `NOT_EXPORTED`（`tests/test_project_transfer.py:61`）；顶层 `styles` 字段是非通用表数据的现成先例。
- EPUB 由 `backend/app/services/epub_service.py:343` `build_epub` 生成，调用方 `export_service.py:126`。
- 前端书架占位封面 `frontend/src/components/shelf/BookCover.tsx` 已预留 `coverUrl`；书架卡片 `frontend/src/pages/ProjectListPage.tsx:342`；概览页 `frontend/src/pages/project/OverviewPanel.tsx:210`。
- 鉴权为同源 httpOnly cookie，`<img>` 直接请求接口即可。

## Requirements

- R1 项目可有 0 或 1 张封面。概览页「基本信息」卡片左侧展示（2:3），无封面时显示现有文字占位。
- R2 上传：JPG / PNG / WebP，≤ 10MB，按文件头魔数校验；上传即替换。
- R3 AI 生成：使用图片模型配置（可选择配置，默认用默认图片模型）。弹窗打开时，提示词已按书名 / 题材 / 类型标签 / 一句话设定 / 写作风格拼好，用户可编辑。生成后先预览，可重复生成；「设为封面」才替换，「取消」不改动。无图片模型时引导去「模型配置」；接口不支持生图时给出中文错误。
- R4 可删除封面，回到占位。
- R5 书架卡片显示封面，无封面时保持文字占位。
- R6 只有项目所有者能读写封面，他人或不存在一律 404。
- R7 项目备份导出包含封面，导入后封面恢复；没有封面字段的旧备份照常导入。
- R8 EPUB 导出时有封面则作为 EPUB 封面（阅读器书架可见），无封面时保持现状。

## Acceptance Criteria

- [x] AC1（R1/R2）：上传 PNG / JPG / WebP 后封面立即显示，刷新仍在，书架卡片同步显示。
- [x] AC2（R2）：非图片（改扩展名的 txt 等）或超过 10MB 时给出中文错误，原封面不变。
- [x] AC3（R3）：弹窗提示词已预填；修改后生成能显示预览；取消后封面不变；「设为封面」后替换为预览图。
- [x] AC4（R3）：无图片模型配置时提示并给出去配置的链接；选 Anthropic 等不支持生图的配置时显示明确中文错误。
- [x] AC5（R4）：删除后概览页与书架卡片恢复文字占位。
- [x] AC6（R6）：访问他人项目的封面接口返回 404。
- [x] AC7（R1）：替换封面后不显示旧图（URL 带版本参数）。
- [x] AC8（R7）：带封面的项目备份后导入为新项目，封面字节一致；损坏的封面数据导致导入报错；无 cover 字段的备份正常导入。
- [x] AC9（R8）：带封面导出的 EPUB 含 `cover-image` 资源和封面页；无封面时 EPUB 结构不变。
- [x] AC10：后端 `ruff` + `pytest`、前端 `lint` + `build` + `test` 全部通过。
- [x] AC11（R3）：AI 生成弹窗的字数计数在输入框下方独立显示，935px 与375px宽度下不遮挡底部操作按钮。

## Out of Scope

- 封面裁剪 / 编辑、候选封面历史、在封面上叠加书名文字。
- 生图走异步任务队列（采用同步请求 + 前端加载状态）。
- 把封面提示词纳入提示词模板注册表 / 提示词工坊。
