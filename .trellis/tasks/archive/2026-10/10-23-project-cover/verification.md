# 项目封面验收记录

日期：2026-10-29（Asia/Shanghai）。分支：`codex/project-cover`。

## 自动化验证

- 后端最终全套：`LC_ALL=en_US.UTF-8 uv run pytest`，975 passed、退出码0，耗时410.36秒。
  唯一warning是已明确处理的Imagen SDK弃用提示，未抑制；日志 `/tmp/ainovel-project-cover-backend-final-pytest.log`。
  `uv run ruff check .` 最终通过。
- 独立复核：`ruff check .` 通过；封面/图片 Provider/备份/EPUB/迁移/归属权限定向测试 82 项通过。
- 前端：`npm run lint`、`npm run build`、`npm test` 通过，54 个测试文件、414 项测试。
  最大 chunk 1155.09 kB，低于 1500 kB；生图弹窗按需加载约 3.88 kB。
- 新增真实超时边界测试，验证协程取消、中文400和原封面字节/版本保持；连续三次隔离运行通过，并已纳入975项全套。
- `git diff --check` 通过。

## 浏览器与实际 HTTP

使用独立临时 SQLite、测试账号和模拟图片 Provider，不使用已有账号或真实生图额度。

- 上传 PNG 后概览显示，刷新后仍在；未保存的书名编辑不会因更换封面丢失。
- AI 弹窗正确预填书名、题材、标签、设定与风格，提示词可修改；默认选择图片模型。
- 生成后只显示预览；取消后从 HTTP 读取的持久化封面字节与原始上传文件完全一致。
- 点击“设为封面”后版本 URL 更新，书架图片加载成功。
- 删除封面后恢复文字占位，接口版本字段为 null；确认框的取消按钮已修正为中文。
- 无图片配置时显示“模型配置”链接，禁用生成；伪装 PNG 的文本显示中文错误，原封面 URL 不变。
- 暗色主题375px宽度没有页面级横向溢出；桌面1280px下封面与基本信息表单并列。已恢复浏览器原视口。
- 实际 HTTP：导出备份并导入新项目后封面原字节一致；无 cover 字段旧备份导入成功；损坏 cover 被400拒绝。
- 实际 HTTP：EPUB 包内封面字节一致，manifest含 `cover-image`，spine首项为 `cover.xhtml`。

验收临时服务与页面均已关闭，截图保存于本次会话的 visualization 目录。

## 补充：提示词计数布局回归

用户反馈计数覆盖“设为封面”按钮。`CoverGenerateModal.tsx` 已移除悬浮 `showCount`，
改为正常文档流中的独立右对齐计数，保留2000长度上限，并补齐标签及描述关联。

- `npm run lint`、`npm run build` 通过；独立复核的54文件414项前端测试全部通过，`git diff --check` 通过。
- 浏览器浅色主题935×564及375×812下，计数底部与按钮顶部间距均为20.25px，未重叠，375px下无水平溢出。
- 本次仅修改前端布局；后端代码未变，沿用此前975项通过的验证结果。
- 修复截图：会话 visualization 目录内 `project-cover-count-fixed.jpg`。

## 已知限制

- OpenAI / Gemini 使用真实 SDK + 模拟 HTTP 验证协议；未调用付费的真实图片服务。
- `google-genai 2.32.0` 的 Imagen `generate_images` 当前要求 Vertex/Enterprise；API-key Developer API 返回明确中文提示。
  本次不扩展 Vertex 鉴权，Gemini 原生 IMAGE 路径已验证。官方说明：
  https://ai.google.dev/gemini-api/docs/deprecations#imagen-models
- EPUB保留 JPG/PNG/WebP 原格式，WebP 的实际阅读器兼容性取决于阅读器。
