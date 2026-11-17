# 提交计划

2026-11-17 按最终工作区重写，取代此前只覆盖前三项的清单。

在分支 `codex/pen-names-templates-thumbnails` 一次提交全部功能改动，保持四项功能及其共用的迁移（0055–0057）、备份、权限、规范和验证记录处于同一可运行版本：

`feat: 支持笔名分组、小说模板与设定缩略图`

范围为提交时 `git status` 列出的全部路径：`README.md`、`TODOS.md`、`.trellis/spec/`、`.trellis/tasks/` 下本任务树五个目录、`backend/`（迁移、应用、测试、`pyproject.toml`、`uv.lock`）和 `frontend/src/`。任务开始时工作区已核对为干净，未发现无法归属的改动；测试数据库、图片和临时日志不在仓库内。

随后按 Trellis 流程分别提交任务归档和会话日志，再合并到 `main` 并推送。
