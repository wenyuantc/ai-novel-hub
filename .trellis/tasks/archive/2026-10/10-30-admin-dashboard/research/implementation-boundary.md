# 实施边界与分工

用户于最终方案摘要后明确回复“开始”，批准实施；任务已激活到 `in_progress`，开发分支为 `codex/admin-dashboard`。

行为缺口：当前没有管理员总览、每日规模快照或不随任务清理丢失的统计。真实修改边界是业务事务的Task创建/状态/删除入口、实例级统计服务与页面，不通过UI猜测或定时扫描任务表补造累计。

在不改变产品范围的前提下，按互斥文件责任划分3条实施线；存储/生命周期先稳定模型与初始化接口，API可先写schema/聚合和定向测试，前端依据固定API契约开发。依赖模型的运行测试在存储文件到位后再执行。禁止同时改同一文件，最后统一全范围检查。

1. **tracking**：唯一拥有 `backend/app/models/dashboard.py`、模型导出、Task的statistics_key、迁移0025、task_statistics_service、task_service、task_engine/engine、analysis_service、kb_index_service、admin_service与recycle_bin_service的统计接入，以及任务统计/迁移测试。负责同事务、幂等、UUID身份和清理保留；不修改main、APIrouter或前端。
2. **dashboard_api**：拥有后台采样器、admin_dashboard_service、schemas/admin_dashboard、api/v1/admin_dashboard、api_router注册、main启停，以及dashboard API/采样器测试。不修改tracking所拥有的模型、任务服务或迁移。通过下述模型契约及初始化函数接入。
3. **dashboard_frontend**：拥有新增dashboard页面/局部组件/API/类型、router、AppLayout和WorkshopPage的最小tab深链接改动与前端测试。不修改backend、公共client、全局请求框架或其他任务页面。

主会话负责契约协调、全范围检查、真实浏览器验收、实施清单与规范更新和收尾。不覆盖原有 `.codex/config.toml`、`.gitignore`、两个source-command技能目录的改动。每个实施子代理直接实现，不再派生implement/check子代理，不提交、不归档任务。

共享模型契约：`models/dashboard.py` 导出 `DashboardTracking`（id/started_at/timezone）、`DashboardDailySnapshot`（day/captured_at/users/projects/chapters/total_words/pending_submissions）、`TaskStatistic`（key/kind/origin/observed_at/created_event_at/status/finished_event_at/removed_at）。Task增加可空、唯一的statistics_key。初始化函数为 `task_statistics_service.initialize_tracking(sessionmaker)`，必须幂等；引擎start负责在恢复前调用，以覆盖不走lifespan的既有测试，main可再次幂等调用后启动采样。

API字段以 `api-contract.md` 为准。如内部接口必须调整，先通知其他实施线，不擅自更改已冻结的HTTP字段。所有执行者都不是独占工作区，禁止回退他人改动；被共享文件临时缺失阻挡时完成自己不依赖它的工作，等待契约所有者，而不是复制另一个模型实现。
