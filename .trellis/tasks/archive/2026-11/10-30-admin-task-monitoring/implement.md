# 管理员全站任务监控执行计划

日期：2026-10-31。状态：**实现与验证已完成，已获提交及收尾授权，正在执行**。

入口：`prd.md` → `design.md` → 两份 research。实施前必须由用户在看到最终摘要后另行批准；不因此前四次“推荐”自动启动。

## 0. 启动门槛与基线保护

- [x] 用户明确批准最新最终规划摘要；若方案发生实质变化，重新审阅。
- [x] `prd.md/design.md/implement.md` 齐备，implement/check JSONL 经过整理并 validate。
- [x] 保存 `git status --short` 与 HEAD 基线；已知用户已有 `.codex/config.toml`、`.gitignore` 及两个 source-command skill 的未提交改动，**不覆盖、不顺手清理、不纳入本任务提交**。
- [x] 在 `codex/admin-task-monitoring` 分支开始，按允许范围保护现有未提交改动；执行 `task.py start` 后读取 Phase 2.1 细节。
- [x] 默认 Trellis implement/check 子代理；每次提示以 `Active task: .trellis/tasks/10-30-admin-task-monitoring` 开头，原生注入优先、子侧 pull fallback。派发前读取 trellis-channel 的相关协议；明确单一写入所有权，不让两个代理同时改任务引擎或共享传输类型。

## 1. 先固定跨层契约与失败测试

- [x] 读清规范与两份 research；把 design 的安全 DTO、UUID 路由、query、202 受理语义镜像到后端 schemas 与前端 types 草案。
- [x] 定义有限阶段模板、安全错误枚举与 unknown 回退；测试 agent 原始标题/私有插件 stage/含密钥错误均不泄露。
- [x] 为取消受理先写事件屏障测试骨架：审计失败无信号、queued claim 竞争、pending 时普通取消/继续/提前结束边界。
- [x] 任何实现提议若改成“取消后补审计”或“终态释放资源后盲重试 abort”，立即阻断，不进入下一步。

## 2. 后端实现（建议同一 implement 代理顺序拥有）

### 2.1 数据和队列时间

- [x] `backend/app/models/entities.py`：新增可空 queued_at、admin_cancel_operation_id、admin_cancel_requested_at；新 Task ORM 默认可信 UTC queued_at。
- [x] 新迁移 `0026_admin_task_monitoring`：只新增可空列/所需窄索引，不重建 SQLite tasks 父表，不虚构旧入队时刻；pending 时拒绝破坏性降级。
- [x] `task_service.resume_task` 以及其他恢复入队入口：原条件迁移成功才刷新 queued_at；核实通用/分析/KB 新建默认均覆盖。
- [x] 迁移测试覆盖 0025 存量含生成日志/智能体外键、null、foreign_key_check、head 链和安全降级。

### 2.2 安全查询

- [x] 新 `schemas/admin_task.py`、`services/admin_task_service.py`、`api/v1/admin_tasks.py`，在 v1 注册；路由无 ORM。
- [x] 单一过滤 CTE、聚合/分页同 SQL 读视图，组合筛选、日期与边界校验、字面转义、稳定/时长排序；无匹配、空页仍有正确计数。
- [x] 安全列投影、匹配 UUID 的活动进度；不透传原始标题/stage/error 或 TaskOut，不访问模型配置/输出/断点。
- [x] 现存含回收站/删除中标识；UUID 详情，不用整数主键作异步身份；GET 不写库、不创建 Hub 或任务。

### 2.3 受理审计与执行协调

- [x] 扩展 `schemas/audit.py`、`audit_writer.py` 的 task.cancel_request/task 及专用有界原因字段；仅此动作允许该自由文本，不放宽其余摘要。
- [x] 接受事务明确复核当前管理员、捕获真实 actor/owner、安全目标标签；本人作品也采集。
- [x] UUID + OPEN 状态 + marker 空的条件更新与受理审计同事务；202 只表达受理；重复 pending 不新建受理/不重复 signal；提交后投影失败不记相反失败。
- [x] 引擎持有 single-flight drain，立即/启动/周期唤醒且不阻塞调度；HTTP 中断不取消独立清理；只用受理标记作执行依据，不依赖审计尚未被显式清理。
- [x] queued：扫描和 claim 都加 pending 屏障；保留 queued/资源键先收尾，再 cancelled+统计+清 marker 同事务；失败仍保持屏障。
- [x] paused：保留其原不占并发语义，仅做当前暂停 handler 的安全清理；不能回滚其他新任务资源。
- [x] running：保留一次 cancel、真实原子保存和 10 秒等待窗口；finalize 的真实状态/统计提交原子清 marker；abort/保存失败保持 pending running 与资源屏障。
- [x] late succeeded/paused 保留真实赢家，不再执行旧 abort；提交确认不确定时重读 UUID 事实。
- [x] 把 pending 检查提升为真实条件写屏障：普通 queued/paused/孤立 running 终态、resume/finish 的 UPDATE 均 CAS marker IS NULL，0命中后重读委托/拒绝；finalize/recover 绑定已观察 marker 或锁后重读，防止受理夹在读写之间被旧分支忽略。逐个搜索其他 Task.status/断点跳过/自动继续写入口，不仅修改 HTTP cancel 路由。
- [x] 恢复：queued/paused 已受理意图不丢；running 按原 interrupted 语义安全恢复并清 marker，不重放 LLM/MCP。异常“终态+marker”不得盲 abort。
- [x] 普通 cancel 遇 pending 委托同一工作，resume/finish 拒绝覆盖；清理/裁剪/单删/删号/项目 purge/expire 都保护 pending 与真实 cleanup_scope；完成后原保留策略不变。
- [x] shutdown 等待真实工作链/清理再关数据库；未知错误与日志固定安全处理。

### 2.4 后端局部门禁

工作目录为 `backend/`，locale 使用 UTF-8；按实际新增测试文件名同步计划，不能通过漏选测试缩小验收：

```bash
uv run ruff check .
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_admin_tasks.py tests/test_admin_task_cancellation.py tests/test_admin_task_monitoring_migrations.py
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest tests/test_tasks.py tests/test_task_statistics.py tests/test_task_statistics_integration.py tests/test_task_statistics_identity.py tests/test_recycle_bin_tasks.py tests/test_admin_audit_logs.py tests/test_admin.py tests/test_batch_generate.py tests/test_book_import.py tests/test_migrations.py
```

运行前先核实测试文件名，若原文件不同，依据仓库真实对应文件修正命令，不创建空壳测试满足清单。

## 3. 前端实现（后端/TS 契约冻结后）

- [x] `api/types.ts` 镜像所有新类型及审计扩展；新 `api/adminTasks.ts` 用现有 request、Cookie、AbortSignal、数组 query，不直接 fetch。
- [x] 新 AdminTasksPage 与页级 controller/hook、必要详情/确认组件；复用已有标签/StudioPage/AntD，不抽象全局数据框架。
- [x] 筛选、当前筛选计数、20/100 分页、创建/时长排序、未知时长、状态/生命周期标识与安全详情。
- [x] 默认 5 秒可关闭轮询、hidden/visible、慢请求去重、手动刷新与局部重试、过期提示及上次成功时间。
- [x] actor/筛选/页码/目标所有权 gate；401/403/卸载清空并停止；取消确认忙态/关闭/目标切换不能由旧响应复活。
- [x] 原因 1–200 字必填、真实目标确认、说明已提交内容保留；202/取消中/真实终态分别展示，网络不确定先刷新，不自动重发。
- [x] 审计标签/安全摘要展示增加受理原因与身份，明确成功只表示受理，不把全局其他成功记录换标签。
- [x] RequireAdmin 懒路由、AppLayout 账户/导航/面包屑；首页现存任务/失败/中断链接和口径说明；不向个人 useTasksStore 注入他人任务。
- [x] 桌面紧凑表格、375px卡片、浅暗色、键盘/焦点/读屏文本；不新引入 UI/图表依赖。

工作目录为 `frontend/`：

```bash
npm test
npm run lint
npm run build
```

聚焦新增 API/controller/纯函数/结构组件测试，同时回归 tasks、adminDashboard、auditLogs。静态渲染不替代交互验收。

## 4. 必须通过的跨层/竞态门禁

| 验证 | 对应验收 | 必须观察的事实 |
| --- | --- | --- |
| 权限与两个用户 | AC1、AC5 | 每接口401/403，普通 owner 404 不变，无新模型调用 |
| 组合查询/排序/时区/生命周期 | AC2、AC6 | counts/list 同读视图，null 不补零，回收站标识、清理后明细消失 |
| 审计 flush/commit 失败 | AC3 | marker 与受理成功均不提交，signal/abort 为零，日志无秘密 |
| queued 接受与实际 claim 竞争 | AC3、AC4 | 受理先赢不运行；领取先赢走真实 running，不复制任务 |
| queued abort 失败/重启 | AC4 | 活动资源键及 marker 保留，新同资源任务不能抢占；安全恢复后唯一终态 |
| paused 与普通继续/结束竞争 | AC4、AC5 | pending 不被覆盖，暂停不占并发，也不还原后来任务 |
| owner/finalize/recover 读写间受理 | AC3、AC4 | 在原分支检查之后、UPDATE之前插入受理，真实CAS阻断旧状态写，不制造终态+marker |
| 真实 RETURNING/commit 屏障 | AC4 | 提交前取消全回滚；保存已提交则成功获胜；独立SQLite写入不再被活游标锁住 |
| late done / late paused | AC4 | 原 finalize 同事务清 marker，保留真实赢家，没有后续旧 abort |
| running abort/终态保存失败 | AC4 | 不写释放资源屏障的假 cancelled，恢复不重放 LLM |
| 等待未完成/HTTP断开/shutdown | AC3、AC4 | pending 明确、只 cancel 一次、清理仍在 project_work，关库无悬挂会话 |
| SQLite ID 真复用 | AC4 | 旧请求/执行体/清标记/输出不能作用于新 UUID |
| 清理/裁剪/项目删除/无作品删号 | AC4、AC6 | pending 与实际清理不丢失；完成后恢复原策略，统计/审计不级联删 |
| 隐私注入 | AC7 | error/title/stage 中的密钥、指令、插件名、URL不出现在管理响应/UI/自动审计 |
| 轮询与失效 | AC8 | 5秒、开关、隐藏、慢请求、切换筛选/账号/权限、关闭详情旧响应全部可控 |

上述竞态用 asyncio.Event、可控 Future 或真实数据库屏障，不用盲 sleep 或单次偶然通过的测试。

## 5. 全量检查、运行时核验与收尾

- [x] 交给独立 trellis-check 子代理检查完整任务变更，不只看最后一批 diff；重点挑战受理原子性、资源代次、真实赢家与权限清理。
- [x] `backend/` 全量：`uv run ruff check .`；`env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest`。
- [x] `frontend/` 全量：npm test、npm run lint、npm run build；执行 git diff --check。
- [x] 用独立临时数据库/测试账号启动本地预览，真实浏览器检查桌面与375px、浅暗主题、筛选/详情/原因确认/取消/失败恢复/降权；不改开发者真实库、不调用真实 LLM。
- [x] 若有 PostgreSQL 验证环境，核验行锁/受理-claim竞争与迁移；无环境则如实标“未验证”，不能声称 SQLite 结果等价。
- [x] 留存 verification/check 报告及相关测试/截图路径；未通过项明确说明，不能提前报告已实现/已验收。
- [x] 更新 `.trellis/spec/backend/admin-task-monitoring.md` 及相关 indexes、admin-audit/admin-dashboard/recycle-bin/error/database/前端规范的真实变更契约，尤其“原因局部自由文本”和“受理不等于终态”。
- [x] 审阅本任务改动并按项目流程取得提交授权；不提交用户原有无关修改。提交/归档/日志使用 Trellis finish-work 流程，不在规划阶段执行。

## 6. 风险点与回退

- 最高风险文件：task_service.py、task_engine/engine.py/取消协调、audit_writer.py、Task 模型/0026迁移、项目排空/删号保护、api/types.ts 与前端请求 controller。
- 后端先完成迁移/受理/恢复与竞态测试，再挂前端入口；接口不可交付时不先挂死链。
- 若本地阶段失败，只回退本任务拥有的变更，不还原原有工作区修改；已开始运行的测试/清理工作先安全结束。
- 部署回退前停止接入并排空或明确处理 pending，不在有取消意图时降级，不把已提交内容回滚承诺给用户；审计及累计统计保持原保留口径。
- 任何关键不变量无法证明时回 Phase 1 更新 design/PRD 并重新审阅，不以新增隐藏前提、扩散全局改造或事后日志掩盖。

## 当前实施分工（2026-10-31）

- implement_backend：backend产品/迁移/主测试；不改对抗性专属测试文件。
- implement_frontend：frontend全部产品/测试，与后端冻结契约同步。
- implement_adversarial_tests：仅新增 backend/tests/test_admin_task_adversarial.py，公共API与并发屏障补强，不改产品；发现交后端修复。
- 主会话：协调、隔离浏览器运行时验收、任务文档/规范、最终质量门禁与提交授权；保持原有无关工作区改动。

最终门禁：后端1292 passed（exit0）、前端594 passed、lint/类型/等价构建/独立全范围检查与真实浏览器通过；PG未实测、标准前端缓存权限边界见verification.md。用户已授权提交、收尾、合并main及推送；严格保留用户原有无关改动。
