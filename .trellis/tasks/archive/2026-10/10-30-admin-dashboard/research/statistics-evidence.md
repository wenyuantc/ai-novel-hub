# 管理员首页统计证据与实现约束

调研日期：2026-10-31。只记录当前代码证据和由此得到的技术约束；产品决定以 `../prd.md` 为准。

## 1. 规模统计与现有权限

- `backend/app/models/entities.py:140`：`User` 包含 `is_admin`、`is_active`，禁用不是删除；账号总数不能加启用过滤。
- `backend/app/models/entities.py:172`：作品存在回收、删除中状态；`Project.user_id` 已是 `RESTRICT`，不能假定删用户会级联作品。
- `backend/app/models/entities.py:473`：当前章节保存 `word_count`；历史版本是另一张表，不能一起求和。
- `backend/app/services/admin_project_service.py`：正常作品管理使用 `project_lifecycle.active_conditions()`，不符合本任务“总量包含回收站”的口径，不能直接复用该过滤。
- `backend/app/services/admin_service.py:131`：删号必须保留原有审计与无作品护栏；用户级无作品任务仍可能经级联删除。
- `backend/app/services/recycle_bin_service.py:203`：作品彻删由审计事务里的 `db.delete(project)` 触发子表级联；到期自动清理也复用该入口。
- `backend/app/models/base.py:19`：通用时间戳只有创建/最近更新时间，不构成逐日历史。不能依据现存数据重建已删除内容或过去字数。
- 统计与采样应在 SQL 中聚合；账号、作品、章节和字数分别用标量子查询，避免用户/作品/章节 join 后重复计数。规模总量不使用生命周期过滤，字数只求和当前章节的 `word_count`。

## 2. 任务写入并非只有一个入口

| 路径 | 当前行为 | 统计接入约束 |
| --- | --- | --- |
| `task_service.py:133` `create_task` | 校验/抢占、Task 插入、提交后通知引擎 | 统计写入必须在原提交之前；失败一并回滚 |
| `analysis_service.py:553` `enqueue_pending` | 直接批量插入，只 flush，不提交 | 统计 helper 不得擅自 commit |
| `analysis_service.py:585` `enqueue_auto` | 独立会话直接插入自动分析任务 | 必须覆盖此入口，不能只改通用任务 API |
| `kb_index_service.py:98` `enqueue_memory` | 独立会话直接插入记忆索引任务 | 同上 |
| `task_service.py:187` `resume_task` | 同一 ID 从 paused 回 queued | 不得增加新建数 |
| `task_service.py:216` `finish_paused_task` | paused 直接 succeeded | 记录一次真实结束，不等待引擎 finalize |
| `task_service.py` `cancel_task` | queued/paused 原子更新，running 交给引擎或兜底更新 | 所有终态分支均要接入 |
| `task_engine/engine.py:114` `recover` | 遗留 running 条件更新成 interrupted | 原子更新成功后同事务记录，重启不能重复累计 |
| `task_engine/engine.py` `_execute` | queued 条件领取为 running | 保留原资源键/并发语义 |
| `task_engine/engine.py:362` `_finalize` | 写入结果、提交后裁剪 | 真实终态和统计同事务；统计不得在 prune 后补写 |

`Task` 只有整数主键，SQLite 下没有 AUTOINCREMENT 约束。保留统计如果仅按 `task_id` 关联，删除后新任务可能复用整数 ID；必须引入独立、不可复用的统计身份。

## 3. 删除与裁剪

- `task_service.py:359/383/400` 分别提供删除、清理和裁剪；`MAX_FINISHED_PER_USER = 200`，普通目录任务的结束记录会被裁掉。
- 必须在这些入口，以及作品 purge/expire 和用户删除前，用原业务事务标记统计记录的移除；统计本身不设置指向业务表的级联外键。
- 所有删除路径都不得减少已记录的新建量和结束结果。未结束任务被移除不意味着任务成功或失败，不凭空补写结果；当前在途数仍按实际 Task 表计算。
- 当前终态没有原 ID 重开路径，失败重试会创建新 Task；因此“每任务一条持久化统计记录 + 一次真实终态时间”足以提供所需的每日新建/结果和滚动 24 小时数量，无须引入通用事件总线。

## 4. 启停与测试

- `backend/app/main.py` 的 lifespan 负责引擎与回收站清理器启停；统计初始化应早于任务恢复，采样器应早于数据库 dispose 停止。
- `backend/tests/conftest.py:51` 使用 `Base.metadata.create_all`，不走 lifespan，只显式启停任务引擎。新增模型必须被 `app.models` 导入；测试需要覆盖初始化与采样器本身，不能仅依赖迁移创建表或启动钩子。
- 当前迁移 head 为 `0024`。新增迁移不应为增加统计身份列而重建 SQLite 的 `tasks` 父表，否则可能触发生成日志、智能体消息等外键副作用。
- `test_project_transfer.py` 只要求与作品存在外键关联的表登记；实例级统计表不持有作品/账号外键，也不进入作品备份。

## 5. 前端与入口

- 现有管理员路由见 `frontend/src/router.tsx`；`RequireAdmin` 已处理登录/普通用户访问。新页面应通过 `lazyPage` 加载。
- `frontend/src/components/AppLayout.tsx:39` 及账户菜单、全局侧栏和面包屑分别维护管理员文案/入口，接入时必须同步，不能只加路由。
- `frontend/src/pages/WorkshopPage.tsx:22` 目前使用本地 tab，忽略查询串。待审核入口如使用 `?tab=review`，必须增加受管理员权限约束的 tab 解析，并测试普通用户与非法 tab 兜底。
- `frontend/src/hooks/useLoader.ts` 已保留旧数据并丢弃过期 effect 结果；页面可复用该模式，复杂轮询如需额外 controller 则限于 dashboard，不改造全站请求机制。
- `frontend/src/api/client.ts` 的 `request` 已支持 `AbortSignal`，GET 快捷方法不支持；需要可取消 GET 时直接使用现有 `request`，不必修改公共客户端。
- `frontend/src/utils/time.ts` 的 `formatTime` 按浏览器本地时区展示 UTC 时间戳。统计日必须直接使用后端的北京时间日期，不能再次按浏览器本地时区分日。
- 当前没有统计图表依赖。90 个日点的折线/柱状图可用 SVG 与现有 `usePalette`、`AutoGrid` 实现，并提供 HTML 数据表，不额外引入大型图表库。

## 6. 时区运行依赖核对

2026-10-31核对官方Python镜像源码：项目使用的Python3.12 slim系列，其[bookworm版本Dockerfile](https://raw.githubusercontent.com/docker-library/python/master/3.12/slim-bookworm/Dockerfile)和[trixie版本Dockerfile](https://raw.githubusercontent.com/docker-library/python/master/3.12/slim-trixie/Dockerfile)均安装系统tzdata，因此本次不额外增加Python时区包或改Dockerfile。此项只是官方源码核对，不代表本机实际构建或运行过容器；若换为裁剪掉时区数据的自定义基础镜像，需要另行保证Asia/Shanghai可加载。
