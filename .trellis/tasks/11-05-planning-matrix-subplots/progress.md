# 实施进度

- 用户已于 2026-11-05 在最终摘要之后回复“开始”，实施授权已记录。
- 状态：in_progress；分支 `codex/planning-matrix-subplots`。
- 实施前基线：`4c80eca381225050f288de612449ef69587018b8`。
- 实施前工作区仅本任务规划目录和既有 `pelican-cycling.html` 未跟踪；后者不属于本任务。

## 当前分工

- `planning_backend`：模型／0034迁移、schema、新 planning_service／API、领域测试。
- `planning_lifecycle`：planning_lifecycle.py、原有章／细纲／cast／大纲源生命周期、备份v3、effects及回归。
- `planning_ai`：planning_context_service.py、context_builder、生成／重写及提示词、AI实际消息测试。
- `planning_frontend`：frontend 全部规划 API／类型／组件／页面／导航及测试；schema 已固定后启动。
- 主会话：接口协调、完整验收、PostgreSQL／浏览器验证、spec／提交／收尾。

## 验证资源

- 隔离 PostgreSQL 容器：`ainovel-planning-verify-20261105`（postgres:16-alpine，仅本机随机端口，--rm）。完成后停止；不操作其他容器。
- PostgreSQL 本机端口 `32813`，仅测试用户／数据库 `planning_test`；验收时独占使用，避免并发 DDL。
- 浏览器验收计划使用独立临时数据库和假模型，不连接用户作品数据。
- 临时浏览器 fixture：`/tmp/ainovel-planning-browser.J1ZUSm/server.py`，后端 `8013`、前端 `5178` 已完成浏览器验收，临时前后端进程已关闭；PG容器已停止并自动移除。

## 当前证据

- 规划文档与上下文路径验证通过；14条实施与13条检查上下文均未触发单文件注入截断。
- 所有实现worker及最终check已完成并停止编辑；完整浏览器验收见`research/browser-verification.md`，PG证据见`research/postgres-verification.md`。
- AI worker 已运行旧模板回归的红测试；uv沙箱缓存采用独立 `/tmp` 路径且 `--offline`，无须联网安装。

## 最终检查进度

- 后端首轮1890用例发现7项失败，全部已修复或解除隔离端口沙箱限制后复测通过；新增10项通过，最终check覆盖1900个唯一用例。修复包括版本/审计autoflush边界、只读查询flush、搜索参数遗漏、effects失败回滚的对象过期；旧open-api夹具同步已建章cast权威而保留原断言。
- 后端Ruff通过。主会话最终全量pytest已通过：1900 passed、0 skipped、1条既有Gemini SDK弃用警告，996.82秒；显式启用隔离PG条件用例。
- 前端最终ESLint/生产构建通过，118文件858测试通过；最大vendor-antd块1158.45kB，规划页面约54kB独立懒加载。
- 最终check未留未修问题；所有22规划路由的隔离/只读权限及7只读无flush入口有回归。
- 已新增跨层planning规范并同步backend/frontend索引、备份v3、快照和提示词规范。
- 全量门禁完成，用户已确认提交清单，正在按workflow 3.4执行本地提交。
- 新出现未跟踪`.openidea/`为不识别的无关文件，与原有`pelican-cycling.html`均不纳入本任务。

- 112个文件的提交方案已写入`commit-plan.md`；全部本任务实现/规范/验收已准备可审阅。用户已回复“明白了，按清单提交并收尾。”，提交、归档与会话日志均已获得授权。

- 最终后端pytest进程正常退出，exit code 0；临时PostgreSQL容器`ainovel-planning-verify-20261105`已停止且--rm移除。
