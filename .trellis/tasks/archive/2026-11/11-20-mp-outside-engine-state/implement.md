# C5 实施与验收计划

按照 [PRD](prd.md) 中的 R1–R9 和 AC1–AC9 验收。用 [设计](design.md) 固定状态、事务及失败语义。开工分支从已验收的 C4 基线出发，先核对其他工作树和迁移头，保留父任务与其他子任务的现有工作。

## 实施顺序

1. [x] **建立基线与迁移**。用已存在的双应用夹具及同一文件 SQLite，复现至少一条备份争用、跨进程导入过期、同一步骤重复生成和提案操作争用。记录公开 OpenAPI 与当前迁移头。补充迁移、模型、冷恢复与 worker 登记裁剪条件。验证 0068 旧活动备份、旧灵感会话、现有导入文件及历史外部提案保留原兼容行为。
2. [x] **备份和周期维护**。改 `backups/runtime.py`、`backups/service.py` 及应用启停接线。先实现领取与交接，再处理恢复、可核对的失效提交、写入主机、保留及计划并发。覆盖两方争领取、B 启动时 A 仍在生成、租约失效后接管、恢复者中途退出、失败通知唯一及异主机下载与保留。顺带用两方周期测试核对回收站、仪表盘和用量收尾。对应 AC1–AC4、AC8。
3. [x] **灵感生成**。补步骤占用和会话修订，串起 `inspiration_service.py` 与 SSE 生命周期。先验证同一步骤只有一个模型发送者，再验证不同步骤同时完成、编辑笔记和生成交错、断连及原 worker 失联。对应 AC5。
4. [x] **项目导入**。串起 `api/v1/transfer.py`、原解析器及令牌暂存。核对 100 MiB 上限、到期、用户隔离、淘汰顺序、跨进程确认、同令牌并发、失败重试、直接上传及旧版本备份。另测中途取消和迁移回退时的暂存状态。对应 AC6。
5. [x] **智能体会话**。沿 `agent/service.py` 的实际入口替换占用逻辑并核对任务受理、外部调用与恢复。对照 `agent/access.py`、任务执行入口及审计协调器验证锁序和授权。测试暂停轮次、发送与撤销、确认与拒绝、删除与进行中的外部调用、两实例启动恢复以及失联后结果未知。SQLite 与 PostgreSQL 16 都要通过，使用外部工具替身计数。对应 AC7。
6. [x] **执行收口**。运行各模块原测试和 SQLite 全量测试，完成 PostgreSQL 16 竞争及迁移专项。用两个独立应用进程、同一隔离库、可控模型和备份适配器补测中止和恢复。记录实际命令、结果、数据库版本与跳过项。完成质量复核后更新受影响的规范、AC 和父任务进度。多主机目录要求交给 C7。[验收记录](research/verification.md)

## 验证命令

在 `backend/` 运行，双应用新增测试以 `test_c5_` 开头。设置 PostgreSQL 环境变量前使用测试夹具校验过的本机专用 `ainovel_test`，每例单独 schema。PostgreSQL 门禁须验证目标节点实际执行。

```sh
uv run alembic heads
uv run ruff check .
uv run pytest -n 0 -q -rs tests/test_c5_*.py
uv run pytest -n 0 -q tests/test_backup*.py tests/test_inspiration*.py tests/test_collaboration_inspiration_*.py tests/test_project_transfer*.py
uv run pytest -n 0 -q tests/test_agent*.py
uv run pytest -n 4 --dist loadfile --durations=20
git diff --check
```

从 0068 在 SQLite 与 PostgreSQL 分别升级至新 head，尝试空库降级、带活动操作降级，以及生产形态的备份和冷恢复演练。比较前后公开 OpenAPI 和项目备份格式。SQLite 如遇已有时间敏感测试，在保留原断言和超时的前提下串行复跑并如实记入验收。

## 回滚与评审

- 重点复核备份在快照、发布、保留清理、SMTP 和恢复中的归属检查，以及租约失效和本地文件操作交接。
- 检查导入空间上限、用户隔离、令牌消费的提交边界，以及灵感 JSON 和智能体跨提交窗口有无遗失更新或副作用重复。
- 回滚前停止受理并排空，确认占用、外部执行和备份收尾后再决定是否降级数据库。禁止新旧运行器同时调度。
- 规划资料经评审后才能激活 C5。生产多进程放量仍由后续任务验收。
