# 阶段 E：本人任务与订阅隔离基础

## 已实现

- `collaboration_task_access.py` 提供 `task_grant_identity`、`bind_task_grant` 和可组合的 `task_visible_to_actor`。任务绑定使用顶层 `_collaboration_grant: {invitation_id, revision}`，其中 UUID 必须规范、revision 为正整数；SQL 在列表 `LIMIT` 之前过滤未接受、非本人、非当前 editor、缺失/错误邀请轮次及已删除作品。纯管理员不能借人工权限读取私人任务；accepted viewer 可读本人已结束任务。
- HTTP session 有 `authenticated_actor` 时，任务查询使用认证时捕获的 token_version；没有 HTTP 快照的授权查询也检查用户仍启用。任务列表、详情、输出和对外 TaskOut 在投影后按原任务 UUID、项目、账号及原活动邀请轮次复查。控制、继续、重试及删除在项目父行锁内重新检查本人身份；系统项目排空和按本人/终态/UUID 裁剪不依赖撤权后的成员身份。
- SSE HTTP 路由捕获 actor/token/任务 UUID 与是否从活动状态进入流。引擎为初始快照、队列事件、终态回放和每次数据出队、心跳使用独立会话验权，出队内容用 `personal_projection.project_payload` 过滤私人引用，投影后再复查原轮次；不可用或失权发送无私人内容的 error 并关闭。`notify_collaboration_change(project_id, actor_id)` 可唤醒本进程订阅，但不替代数据库复查。作者主动删除作品时，保留其旧有取消终态，移除其中断点及私人引用。
- 对 Agent 的内部事件与外部投影约定：runner 保留内部原始证明，真实 HTTP SSE 在 `checked_frame` 首次投影；详情/撤销由 Agent 的 `public_changes` 投影。普通 TaskOut 按作品批量投影内部模型配置、任务及日志引用，禁止公开 `_project_model` 与 `_collaboration_grant`。

## 验证

- `LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_collaboration_task_privacy.py 'tests/test_tasks.py::test_subscribed_task_deleted_ends_with_error' tests/test_task_statistics_identity.py::test_stale_prune_snapshot_cannot_remove_reused_active_task`：15 passed in 43.70s。包含身份矩阵、分页、旧轮次、管理员重叠、令牌版本竞争、待写不自动 flush、嵌套私人引用、投影后降级及作者删除作品的历史取消行为；此组合在最后加入无 HTTP 快照时账号启用检查之前运行。
- 另外运行 `test_revoke_notification_wakes_stream_and_database_is_authoritative`、`test_stream_storage_error_is_a_safe_failure`、`test_system_can_cancel_revoked_collaborator_task`：3 passed in 2.77s；最后一项使用模拟 abort，仅证明系统收尾不依赖当前协作身份，**不是**章节状态 CAS 验证。
- 最终增加用户启用和 SQL 邀请 UUID 格式验证后，`test_noncanonical_stored_invitation_cannot_authorize_active_task` 与 `test_task_http_filters_owner_membership_and_active_grant_before_limit`：2 passed in 2.54s，无警告。
- `AINOVEL_COLLAB_TASKS_POSTGRES_TEST_URL=<隔离 collab_tasks 库> uv run pytest -q tests/test_collaboration_task_postgresql.py`：6 passed in 62.60s，隔离 PostgreSQL 16、随机 schema；包含真实 TCP/HTTP SSE、跨实例撤权、新邀请不续签旧任务、详情/列表/输出/流在投影后降级的数据库屏障。此结果在最后加入无 HTTP 快照时账号启用及邀请 UUID 格式检查之前运行，最终门禁必须重跑。专用测试变量优先，未设时回退现有 `AINOVEL_COLLAB_POSTGRES_TEST_URL`；两者均只允许本机 collab_tasks/collab_test 安全库。
- 范围内 `uv run ruff check app/services/task_service.py app/services/collaboration_task_access.py app/services/task_engine/engine.py app/api/v1/tasks.py tests/test_collaboration_task_privacy.py tests/test_collaboration_task_postgresql.py`：All checks passed。`git diff --check` 在现有跟踪文件上无空白错误。
- 较早的 `tests/test_tasks.py` 与 `tests/test_task_statistics_identity.py` 全组合出现 3 个既有行为回归，随后已按上述聚焦组合修复并通过；最终仍须重跑该完整组合，不把早期组合标记为通过。

## 尚需集成

- 本阶段未开放协作者创建或运行任务。未来受理/派生方必须从锁后事实调用 `bind_task_grant`，覆盖不可信的请求同名键，并在任务准备、模型发送、重试/续跑、工具、保存和每批索引写入复验；不能仅凭此读取过滤声称 E3/E4 已完成。
- 目前提供 `notify_collaboration_change` 接口，但协作写入提交后尚未接线；本地执行体的撤权取消与跨进程重启恢复仍需后续实现。心跳及每帧数据库复查已覆盖本阶段订阅，不等于执行体已停止。
- 章节生成 finally 与引擎 on_abort 可能重复释放状态；后续集成须按任务不可复用 UUID/轮次 CAS 清理，不能误恢复其他任务抢占的 `generating`。
- 真实 AI/Agent、富文档采纳、后台派生任务、生成日志及向量授权仍在对应分工中；跨端功能和最终权限门禁未完成，禁止部署中间状态。
