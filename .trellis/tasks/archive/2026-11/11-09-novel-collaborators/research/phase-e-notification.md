# 阶段 E：协作变更后的任务流唤醒

## 已实现

- 作品邀请、重新邀请、接受／拒绝、角色或作者模型授权修改、撤销，均在业务与成功协作审计的同一事务确认提交后调用同步 `notify(project_id, target_user_id)`；系列变更不唤醒作品订阅。作品写服务要求调用方显式提供通知回调，现有 HTTP 入口注入 `TaskEngine.notify_collaboration_change`，新增内部调用方也必须接入。
- 通知只向当前进程对应订阅队列投递一个不含业务数据的复查信号。订阅按原任务 UUID、邀请轮次和认证身份重新查数据库；通知不是授权依据，也不会直接发私人内容。对外 HTTP 事件与完成的写入不因为通知回调异常被改记为失败；日志只含异常类型。
- 提交确认丢失或请求取消时，既有独立审计协调器先尝试结束／失效原连接并查 operation UUID。已确认成功则唤醒；提交结果未知但已确认连接释放时也允许保守唤醒，因为后续仍只以数据库事实为准。若不能确认连接释放且审计查询也无法确认成功，则不在可能仍持锁的事务内发通知，依靠流的心跳／出队复查；不补造相反审计或重放协作操作。多次取消不打断审计核验与最终唤醒。
- `ProjectChangeNotifier` 是未来扩展本进程执行体唤醒的显式接口；当前只接了现有订阅通知，**没有**取消正在执行的任务，也没有接入协作者任务创建、模型出站、任务状态 CAS 或跨进程主动通知。

## 验证

以下命令在 `backend/` 执行：

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q \
  tests/test_collaboration_notifications.py tests/test_collaboration_cancel.py \
  tests/test_collaboration_audit_edgecases.py --tb=short
# 26 passed in 21.00s

uv run ruff check app/services/collaboration_service.py app/api/v1/collaboration.py \
  tests/test_collaboration_notifications.py tests/test_collaboration_notifications_postgresql.py
# All checks passed!

LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest --collect-only -q \
  tests/test_collaboration_task_postgresql.py tests/test_collaboration_notifications_postgresql.py
# 9 tests collected；其中本切片新增 PostgreSQL 2 项
```

专项包含真实本地 TCP/HTTP SSE，心跳设为 120 秒，验证撤销、降级及作者模型授权变更后不等心跳即收到安全权限失效事件；还覆盖其他用户错误接受、系列变更、过期版本、审计失败、回调异常与取消、成功提交确认丢失、多次取消、连接是否确认释放及直接服务调用漏通知。故障注入中的确认丢失是在 SQLite 真实提交后模拟异常，不能当作真实网络分区证据。

已编写但按主任务错峰要求**尚未执行**的 PostgreSQL 真实 HTTP／事务屏障用例：

```sh
LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 \
  AINOVEL_COLLAB_TASKS_POSTGRES_TEST_URL='<本机 collab_tasks 专用测试库 URL>' \
  uv run pytest -q tests/test_collaboration_notifications_postgresql.py --tb=short
```

该文件复用协作任务的安全测试 fixture：优先读取专用 URL，未设置时回退 `AINOVEL_COLLAB_POSTGRES_TEST_URL`；仅允许本机 `collab_tasks` 或 `collab_test` 专用库并创建随机 schema。测试在提交前屏障验证没有提前唤醒，再提交并验证流关闭；其收集成功不等于数据库竞争已通过。

## 仍需集成

- 协作者执行体的停止、模型／向量每次发送、派生任务和受限清理仍待 E3/E4/E6；单进程通知不能替代重启／其他会话的数据库复查，也不能撤回已发送的数据。
- 任务订阅、项目协作写入、迁移、AI 和前端仍须以最终代码一起做完整回归；本记录不代表 Gate E 已完成或可部署。
