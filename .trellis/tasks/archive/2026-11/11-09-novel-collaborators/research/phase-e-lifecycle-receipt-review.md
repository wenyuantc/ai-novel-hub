# 用户和作品删除：未知提交收据复核

2026-11-10。真实 HTTP 红灯：管理员删除无自有作品的用户时，其终态任务含 `_analysis_pending: null`，旧实现返回 204 并级联删除唯一任务收据。另用 SQLAlchemy `before_flush` 复现删除入口在安全检查前自动刷新调用方暂存的任务；这些失败均不得仅靠任务的终态字符串推断已安全收尾。

## 实现边界

- 发现待写对象时，删号、软删及 purge 先以无自动 flush 的只读查询复验身份和权限，在开始协调事务、写屏障与异常回滚前拒绝，并保留调用者的暂存；无实质修改的 ORM 对象不误阻，无权限仍按原 403/404 处理。对于数据库已提交任务，删号持目标 User 锁后，先收集 Task.project_id 和 Chapter.project_id，按作品 ID 排序取得锁，再锁定原用户的 Task、重新读取关联并调用 `task_service.has_unresolved_receipt`。等待期间若关联到未持锁的作品则失败关闭；无项目任务同样检查。其后才执行原统计标记和用户级联删除。
- 作品软删和回收站物理删除的 `_ensure_idle` 由任务引擎负责人维护，持原项目锁，在任务、原用户和原资源证明不匹配或收据损坏时停止；有效完成收据需验证原身份、版本及结果，不能仅凭 `succeeded` 放行。合法无收据的旧历史与不相关作品/用户保留原删除行为。
- `AgentChange.result._external_execution` 不管单独残留、JSON null、坏类型或字段缺损，均与外部执行中的其他标记一样阻止级联。作品范围还检查会话及只由原任务 UUID 关联的跨作品提案。Task.input/checkpoint 中同名内部标记，以及批量未知结果 `_batch_outcome_unknown` 的 null、损坏和跨类型残留，均由共享任务谓词失败关闭；不向错误响应暴露作品、章节、任务或收据内容。
- 完成收据当前只能证明作者自己的执行；`analysis_service` 与 `generation_service` 的终态验证要求任务用户为作品作者。将来开放协作者实际执行时，需增加原 actor 与授权轮次的完成证明，不能以作者正向用例代替协作者验证。

## 验证与尚待复验

- SQLite：单章 v2 收据主体接线后，生命周期完整测试一次 **49 passed**；后续新增的待写事务保护、批量未知结果及篡改事实分别作定向复验。畸形 input/checkpoint、真实有效分析软删和 purge 4 项通过；篡改真实分析结果后的软删和 purge 2 项通过。删号的待写对象、权限优先和无实质修改 10 项通过；不相关用户与作品不被 pending 收据阻挡。
- 真实单章生成成功后，保留原完成收据、execution_key 已清空时，作品软删和管理员 purge 正向通过；随后篡改原 Task.input 或 ChapterVersion 正文，软删和 purge 均拒绝，2 项通过。单章 v2 完成证明由其他负责人进一步完善，集成最终代码后需重跑。
- 自动 MCP `_external_execution` 的独立 null、损坏及不完整对象针对删号、软删、purge 的 HTTP 测试 3 项通过；只以原 task_key 关联另一作品和目标用户的 3 项通过。共享 Task.input/checkpoint 的 6 项加无关范围对照 7 项通过。
- 批量 `_batch_outcome_unknown` 的 input/checkpoint、null、损坏及跨类型针对删号、软删、purge 6 项通过；对应跨连接场景已加入 PostgreSQL 待运行清单。
- 旧删号与外部操作范围的聚焦回归 9 项通过，管理员既有级联与已结束协作任务回归 2 项通过。Ruff、Python `compileall` 与 `git diff --check` 通过；项目未配置独立 Python 类型检查器。
- 主会话在本机隔离 PostgreSQL 16.15 顺序执行 `tests/test_task_receipt_deletion_guard_postgresql.py`：**11 passed in 96.53s**。覆盖分析、外部执行或批量未知收据先提交，删号、软删、purge 等待后保留证据；删号或软删先提交时，等待中的旧写入不能成功。该结果对应当时的代码快照，后台统一身份和协作者接线完成后须复验。

单章 v2 终态证明及自动外部调用仍由其他负责人修改；上述分段结果并非最终集成全套的通过证明。
