# 阶段 C 记录

## 已接入范围

- `project_access` 显式计算作者、已接受协作者与管理员人工权限的并集；`require_project` 写入时执行认证/User、可选发送写门禁、项目父行及锁后重读。旧严格作者和管理员 helper 未扩权。
- 作品/系列邀请支持 pending、accepted、declined、revoked 和每轮新 UUID；版本、用户及生命周期在父行屏障后重验。成功审计和业务同事务；失败回滚后独立审计，提交结果未知先通过 operation UUID 复查。仅成员复合唯一约束被识别为重复邀请，其他业务约束及审计故障安全返回内部错误。
- 未知或非本人邀请失败审计内部约定 `resource_id=0`，成功事件的 writer 明确拒绝 0；所有业务 API 的资源 ID 必须为正且不超过数据库整数上限。
- 书架批量合并本人作品与 accepted 共享作品，按 actor 投影 `access`，过滤作者私人风格、阅读进度及无权限的系列信息。真实 HTTP 的创建、详情、更新、封面、备份导入、灵感转换响应均显式传 actor；内部缺 actor 时不推断作者。

## 验证和边界

- `test_collaboration_invites.py` 覆盖真实 HTTP 邀请轮次、版本、角色、停用/不存在用户、自邀、撤销、恢复、审计失败、提交确认丢失、账号停用、书架配额、私人风格和阅读进度隔离，以及真实加入系列后的系列/单书授权分离。
- `test_collaboration_access_postgresql.py` 使用隔离 schema 与两套应用实例，观察 PostgreSQL 阻塞关系，验证撤权先提交和编辑先提交的结果、并发重复邀请，以及作品/系列复合唯一约束的真实错误识别。SQLite 覆盖同类约束、错误分类和 WAL 旧快照回滚。
- 阶段 A 清单仍是逐路径待复核的候选清单。本阶段仅将项目详情和项目资料更新接入新的能力解析；章节、场景、设定、导出、封面协作者写、AI、后台任务、系列内容和跨书操作尚需 D/E/F 分阶段迁移。布尔能力投影并不代表这些入口现已开放或完成锁后重验；作者模型回退、日志 `ai_config_id` 脱敏、撤权通知与任务轮次重验也尚未完成。禁止单独部署阶段 C。

在 `backend/` 下执行，均使用独立测试数据：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 AINOVEL_COLLAB_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_test' uv run pytest -q tests/test_collaboration_invites.py tests/test_collaboration_audit.py tests/test_collaboration_access_postgresql.py
# 28 passed in 34.61s

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 AINOVEL_COLLAB_POSTGRES_TEST_URL='postgresql+asyncpg://collab_test:collab_local_test@127.0.0.1:32816/collab_test' uv run pytest -q tests/test_collaboration_access_postgresql.py
# 5 passed in 15.28s；在双连接测试增加父行 NOWAIT 锁检查后重跑

env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -q tests/test_admin_projects.py tests/test_projects_and_story.py tests/test_project_cover.py tests/test_inspiration.py tests/test_project_transfer.py::test_round_trip_all_entities tests/test_agent_tools.py tests/test_rbac.py tests/test_collaboration_route_inventory.py
# 120 passed in 89.68s；在最后的协作冲突分类与输入范围收紧之前执行

uv run ruff check .
# All checks passed!
```

后续阶段完成对应入口后，应按阶段 A 清单逐条复核并补齐反例。
