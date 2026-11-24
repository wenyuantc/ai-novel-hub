# 多进程任务调度与部署演练：执行计划

> 父任务不直接实现。本文件规定子任务的顺序、每个子任务开工与收口的门禁、跨子任务的验证命令和回退点。各子任务的逐步清单在其自己的 `implement.md`。

## 子任务顺序

```
C8 协作迁移演练 ────────────────────────────────┐（独立，可随时进行）
C1 执行者租约 → C2 调度约束与设置 → C3 跨进程取消 → C4 内部通道与流转发 → C5 引擎外状态 → C6 排空开关 → C7 部署与多进程验收
                      └────────────────────────────────→ C6 只依赖 C2，可提前
```

- [x] **C1** `11-20-mp-worker-lease`：执行者登记、租约、接管协议、SQLite 争用基线。其余多进程子任务都依赖它提供的「孤立任务」判定。
- [x] **C2** `11-20-mp-scheduling-constraints`：串行键与并发上限读库、系统设置跨进程生效。
- [x] **C3** `11-20-mp-cross-process-cancel`：取消请求、管理员 drain、作品删除排空、删除意图。
- [x] **C4** `11-20-mp-peer-channel-stream`：内部通道、流转发、降级路径、活动任务读路径。
- [ ] **C5** `11-20-mp-outside-engine-state`：备份、灵感、项目导入、智能体会话锁。
- [ ] **C6** `11-20-mp-drain-switch`：排空开关与管理端展示。
- [ ] **C7** `11-20-mp-deploy-acceptance`：迁移并发保护、启动方式、真实多进程验收、README 与规范收口。
- [ ] **C8** `11-20-collab-migration-deploy-drill`：协作迁移演练与 README 操作步骤。C7 收口时把排空开关与多进程运维内容并入这份操作步骤。
- [ ] **父任务集成评审**：按 `prd.md` 的 AC1–AC13 逐条核对证据，更新 `TODOS.md` 第 11、25 项，归档。

C1 已归档。C2 已完成 SQLite 全量、PostgreSQL 16 专项和双进程 HTTP 演练，实现在本地功能分支。C3 已在本地功能分支完成跨进程取消与作品删除排空，SQLite 全量、PostgreSQL 16 串行专项和双进程 HTTP 演练均通过。[C3 验收记录](../archive/2026-11/11-20-mp-cross-process-cancel/research/verification.md)

C4 已在本地功能分支完成实现与验收，生产继续使用单个 Uvicorn worker。C5–C8 开工前各自核对前序实现和规划资料。[C4 验收记录](../11-20-mp-peer-channel-stream/research/verification.md)

## 每个子任务的开工门禁

- [ ] 前序依赖已收口并合入。
- [ ] 本子任务的 `prd.md`、`design.md`、`implement.md` 齐备并经用户评审。
- [ ] `implement.jsonl` 与 `check.jsonl` 含真实的规范与研究条目。
- [ ] 设计与父任务 `design.md` 冲突之处已回写父任务。

## 每个子任务的收口门禁

- [ ] 受影响测试文件与底层哨兵通过（任务引擎、智能体、回收站、协作撤权相关文件）；新增的双引擎用例通过。
- [ ] 涉及行锁语义的协议，对应 PostgreSQL 用例在隔离的 PostgreSQL 16 上串行通过。
- [ ] 单进程全量回归通过后才合入（子任务收口属于阶段收口，跑全量）。
- [ ] 触及的 `.trellis/spec/backend/` 规范已更新；尚未兑现的多进程能力不提前写成已支持。
- [ ] README 的「单进程」限制在 C7 之前保持不变。

## 验证命令

在 `backend/` 目录：

```bash
# 代码检查
uv run ruff check .

# 中途切片：只跑受影响文件与哨兵，文件名按实际改动替换
uv run pytest tests/test_tasks.py tests/test_task_statistics.py tests/test_admin_task_cancellation.py -q

# 子任务收口与最终交付：SQLite 全量
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 uv run pytest -n 4 --dist loadfile --durations=20

# PostgreSQL 用例：需先准备隔离的 PostgreSQL 16 与 ainovel_test 库，串行执行
AINOVEL_POSTGRES_TEST_URL=postgresql+asyncpg://<user>:<password>@127.0.0.1:<port>/ainovel_test \
  uv run pytest -n 0 -q tests/<本子任务的 PostgreSQL 用例文件>
```

在 `frontend/` 目录（C4、C6 触及前端时）：

```bash
npm run lint
npm run build
npm test
```

真实多进程验收脚本与命令由 C7 提供并记录在其 `research/verification.md`。

## 风险文件

改动时需要额外复核、并在对应子任务的检查清单中点名：

- `backend/app/services/task_engine/engine.py`：`recover`、`_dispatch`、`_claim_and_run`、`complete_state`、`subscribe`、`stop`。
- `backend/app/services/task_engine/cancellation.py`：管理员 drain 的孤立任务分支。
- `backend/app/services/task_engine/stream_lease.py`、`stream_evidence.py`、`backend/app/api/sse.py`：受控流的逐帧核验（C4）。
- `backend/app/services/task_service.py`：取消、作品排空、活动任务读路径。
- `backend/app/services/recycle_bin_service.py`、`backend/app/services/agent/service.py`、`backend/app/services/backups/runtime.py`。
- `backend/alembic/env.py` 与新增迁移；`Dockerfile`、`docker-compose.yml`。

## 回退点

- 每个子任务独立提交、独立可回退；C1–C6 合入后单进程行为不变，任何一个都可以单独 revert 而不影响已对外宣称的能力。
- 新增迁移的降级遵循现有惯例：存在新语义数据（存活的执行者登记、带 `worker_key` 的 running 任务、未处理的取消请求等）时拒绝，否则删列删表。需要回退已运行过的新版本时，按 C8 的操作步骤恢复升级前快照并启动配套旧版本。
- 多进程支持只在 C7 验收通过后才写入 README。C7 未通过时，前面的子任务保持「已合入、未宣称」的状态，部署仍按单进程。

## 开工前检查

- [ ] 用户已评审并明确批准父任务的 `prd.md`、`design.md`、本文件，以及 C1 的三份文档。
- [ ] 以 C1 为第一个开工的子任务：`python3 ./.trellis/scripts/task.py start .trellis/tasks/11-20-mp-worker-lease`。父任务不执行 `start`。
- [ ] C8 是否与 C1 并行由用户决定；并行时由另一个会话承担，避免同一工作区内两处同时改 README。
