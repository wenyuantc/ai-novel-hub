# 管理员任务监控真实浏览器验收

本次验收跨 2026-10-31 至 2026-11-01（Asia/Shanghai）。只验证本地合成数据，不代表平台真实任务历史；完整自动化门禁另见 backend-checks、frontend-checks 与 full-scope-check。

## 隔离环境与清理

- 服务仅监听127.0.0.1:8767；使用每次新建的临时SQLite、项目FakeLLM/Gate、真实HTTP/API/任务handler，不读写开发者数据库，不连接真实模型或MCP。
- 合成3账号（管理员、Alice、Bob）、正常/回收站作品、2个真实生成任务（一个运行、一个排队）、1个待审核智能体任务、28个结束任务；默认现存总数31。
- 临时服务器脚本 `/private/tmp/admin_task_monitoring_runtime.py`；生产等价前端构建 `/private/tmp/admin-task-monitoring-frontend-dist`。Chrome独立无头会话 `admin-task-monitoring`，daemon路径放临时目录，不使用日常浏览器资料。
- 默认沙箱拒绝本地端口监听和Chrome启动；仅在工具审批后启动上述隔离资源，没有变更仓库/缓存目录权限。
- 验收已关闭该独立Chrome与临时服务，清理该会话的登录资料；没有关闭其他服务/浏览器。最后服务器正常退出，结果另存 `/private/tmp/admin-task-monitoring-browser/runtime-final-result.json`。

## 已实际观察通过

1. 未登录直达/admin/tasks跳/login；合成管理员通过真实登录表单后进入任务页，导航/面包屑正确。
2. 最终版本API与页面对账：现存31，queued/running/paused各1，succeeded/failed/cancelled/interrupted各7；默认分页20、次页入口可见。回收站任务显式标识，两个用户归属可辨。
3. 所有合成结束任务故意残留恶意原始标题、插件阶段、错误及输出标记。最终管理列表返回中4类标记均不存在；成功记录的error_code全部null，不因残留error显示失败。
4. 安全详情实际打开、UUID进入URL，没有原始输出/私有标记。以管理账号请求他人普通Task详情/output/stream均404；把整数ID当管理执行标识返回422。
5. 真实填写“用户名monitor_alice + 作品ID1”并点击查询，页面和API均得到16条、owner仅Alice：成功7、中断7、取消2，其余0。不是客户端只过滤当前页。
6. 空取消原因不能进入第二步，显示中文校验；填写后进入再次确认，提交按钮在未勾选确认时禁用。确认前直接读取任务仍queued/running/paused且pending=false，无处置副作用。
7. **最终冻结版本通过真实UI依次取消排队#2、运行#1、待审核#31**：均先填写原因、再次核对并勾选确认再提交；最终三条均cancelled且两列取消意图清空。三个受理审计的actor均monitor_admin，owner分别Alice/Alice/Bob，原因与目标ID逐条一致。
8. 关闭服务后独立读取隔离库：两个章节均draft、正文长度0；三任务均cancelled、marker=null。FakeLLM真实调用共1次、流中止1次，排队任务没有被取消动作启动，监控/详情/处置没有新增模型任务。
9. 观察更新时间在自动模式持续推进；关闭开关后，23:42:57的最后成功时间直到23:44:00仍保持，超过60秒不继续刷新。重新加载恢复默认开启。
10. 只针对任务列表GET注入受控500：页面显示中文失败、过期说明与上次成功时间，31条旧数据保留；移除路由故障并点击刷新后恢复，没有补零。
11. 将**隔离库**中合成管理员实时降权，下一管理GET真实403；自动轮询发现后页面显示“任务监控访问已停止”，列表行数0、详情0、弹窗0、Alice数据不可见。随后注销并用普通Bob真实登录，直达管理路由被转回/，管理API仍403。
12. 1440×1000桌面与375×812移动、浅/暗主题均截图目视检查；375下scrollWidth=clientWidth=375。取消确认完整可读/可操作，移动筛选折叠、排序、日期说明与任务卡片不撑宽整页。

## 真实浏览器发现、修复并复验

- 初版375页头并排把说明挤成多行窄列；已改标题/说明全宽、操作下排。移动排序从Card标题移入正文，标题完整。
- 桌面右侧操作原先在横向滚动区外；已固定操作列右侧，不需滚到底后再横移才能取消。
- 成功任务若残留旧error原先显示“执行失败”；最终后端不读取原Task.error，成功错误摘要为null，最终API与截图复验。

## 截图（已原样保留）

目录：`/Users/wenyuan/.codex/visualizations/2026/10/31/01a19256-fd4d-7131-ab22-91eb19a8aa6f/`

- `admin-task-monitoring-desktop-dark.png`：最终冻结版桌面、固定操作列与安全元数据。
- `admin-task-monitoring-mobile-light.png`、`admin-task-monitoring-mobile-dark.png`：修复后375布局，合成状态截取时刻不同。
- `admin-task-monitoring-cancel-confirm.png`：原因/目标/保留已提交内容提示与明确确认。
- `admin-task-monitoring-demoted.png`：实时降权后清空与停止提示。

## 验收边界

hidden/visible、乱序响应、目标/账号切换、慢请求复用、HTTP断开、提交确认丢失、数据库竞争与任意强杀边界主要由controller/后端屏障测试验证；未逐项真实浏览器手动操作，不能借本报告称全部手动通过。未实际运行PostgreSQL，Docker socket默认沙箱不可访问，不能把SQLite结果视作PG并发已验证。

截图和本报告不替代最终全量门禁；全部代码测试的最终结果必须以最后一次冻结版本完整运行记录为准。
