# 前端实施边界

2026-10-31。仅拥有 frontend/** 与本报告；后端由独立 implement_backend 拥有，文档/spec 由主会话维护。

- 当前差距：只有个人任务面板和管理员累计统计，缺少全站安全明细、可靠页面请求所有权及管理取消受理交互。
- 行为所有者：独立 adminTasks API/类型镜像；页面专用 controller 管理列表、目标详情、用户选项和取消请求；组件只展示安全 DTO。
- 预计改动：新增 API、工具函数、controller/hook、列表/详情/确认组件与页面及测试；补管理员路由/AppLayout/首页入口；局部扩展 audit 类型、动作标签与受理摘要。
- 不做：普通任务授权/输出/SSE、个人 store、后端引擎、图表依赖、阈值告警、任务重试/继续/接管；不修改用户既有改动、不提交。
- 复用：request 的 Cookie/数组 query/AbortSignal，TASK_STATUS/TASK_KIND_LABELS，StudioPage，auditDateRange/auditValidPage，startDashboardPolling(interval=5000)，不改首页60秒默认。
- 安全目标：所有详情和取消只按 task_key UUID；不显示原始标题、错误、正文、插件名；401/403/换账号/卸载清空并失效所有请求；临时读取失败保留旧数据并标过期。
- 已与后端冻结 backend/app/schemas/admin_task.py：counts 七状态+unknown、详情平铺+as_of、取消202仅 operation_id/audit_log_id，task_key可空，有限错误类别，未提供队列位置。

## 实施完成补充

实现文件：`src/api/{adminTasks.ts,types.ts,admin.ts,auditLogs.ts}`；`src/utils/{adminTasks.ts,auditLogs.ts}`；`src/components/adminTasks/` 的页级controller/hook、安全列表/详情、两阶段取消弹窗；`src/pages/AdminTasksPage.{tsx,css}`；路由/AppLayout/首页现存入口；审计列表和详情的局部受理文案。对应API/工具/controller/安全结构测试及首页/审计回归同步补充。

主要实现取舍：将创建/分页变化视为新的列表代次（清空不匹配的旧数据）；仅同筛选临时失败保留旧值。取消预检使用当前安全详情，POST不乐观写Task终态，实际结果GET持有独立任务UUID；同UUID详情已打开时复用详情GET，避免重复轮询。所属用户选项扩展既有adminApi.listUsers的可选AbortSignal，不改变旧调用行为。未扩大产品范围。

只读checker发现合法大写UUID深链的逐字比较问题，已在openDetail入口规范小写并补回归；不同UUID仍严格拒绝。主会话浏览器反馈已落实固定操作列、移动页头纵向与移动排序独立行。

最终自动化、运行时证据边界与环境限制见 `frontend-checks.md`。
