# 变更边界

## 现状与目标间最小行为差异

当前以纯文本场景为唯一正文来源，既没有排版／高亮数据，也没有可独立控制 AI 的作者区块。目标是在不破坏该纯文本投影的前提下增加结构化格式、标记与区块，并对阅读／导出、历史／备份和实际 AI 出站做一致投影；不能只替换文本框。

## 预计变更位置

- 后端 `models`、`schemas`、`alembic/versions`、`services/scene_service.py` 和场景 API：数据权威、验证、场景 CAS、拆并和结构保存。
- 后端 `services/version_service.py`、`chapter_version_service.py`、`project_transfer/`：可恢复结构历史、旧格式兼容和完整备份。
- 后端 `generation_service.py`、`rewrite_service.py`、`context_builder.py`、`analysis_service.py`、`agent/`、任务引擎及批量流程：关闭区块的真实出站过滤，结构／格式有风险时的持久确认与正确续跑。
- 后端阅读、导出、提及与必要路由：同一正式正文、结构化排版和旧 Markdown 导出兼容。
- 前端场景草稿控制器、章节写作组件、AI 面板、阅读器和 `api/types.ts`：富文本、区块、全书标记时间线、提案确认及冲突／授权保护。
- 对应后端／前端测试、项目 `README.md` 和 `TODOS.md`，最后按实际行为更新 `.trellis/spec/`。

## 不纳入

不改既有两份未跟踪 source-command skill；不实施完整自动保存、DOCX、跨场景区块拖动、嵌套区块、图片／表格或实时协作。任务尚未完成时不勾选 TODO，不提交或推送。

## 可能的局部重构

若抽取共享文档投影、编辑器坐标映射、AI 可见过滤或结构变更原语，必须用旧正文逐字符、旧场景 API、原 AI 权限与无结构任务对照测试，证明未改变无新增结构时的既有行为；兼容不了时显式冲突，不静默退回纯文本。
