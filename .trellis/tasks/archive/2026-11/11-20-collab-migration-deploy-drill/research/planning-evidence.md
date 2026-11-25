# C8 开工前现状核对

核对日期：2026-11-25。此文件记录规划依据，不是 C8 验收结果。

## 版本与环境

- 旧版 `4501f18da97502eb3e352bf85b1853bb0903d60e` 的最后迁移为 `0043`。
- 当前分支 `codex/mp-peer-channel-stream` 的 `b7b3d248a7462deedd0507275a0e6fd0a6ec8e0e` 包含 C1–C7，迁移 head 为 `0070`；本地 `main` 为 `a16eb2ae1c685d420f7e8850447adc083618622d`，不含当前分支的 C2–C7。演练时以实际包含 C1–C7 的提交为准，不把旧 `main` 当成新版。
- 旧版和当前版的 `backend/uv.lock` SHA-256 不同。规划期空库检查借用了当前虚拟环境，不能证明旧版依赖锁可复现；正式演练须分别使用两个版本的依赖锁，并记录 Python、依赖安装及版本。
- 本机可调用 Docker，服务端版本为 `29.8.2`，本地有 `postgres:16.10-bookworm`；这些只说明启动前提存在，不代表 PostgreSQL 演练通过。执行时记录实际镜像 ID、服务端版本和资源名称。

## 规划期空库烟测

从两个提交分别通过 `git archive` 导出 `backend/` 到自动清理的临时目录。检查副本不含 `.env`，仅传 `PATH`、语言和临时 `DATABASE_URL` 给迁移子进程。先使用旧副本在空 SQLite 上运行 `python -m alembic upgrade head`，再用当前副本对同一个文件执行相同命令；两者均使用现有 `backend/.venv/bin/python`。

| 步骤 | 退出码 | `alembic_version` | `PRAGMA foreign_key_check` |
| --- | --- | --- | --- |
| 旧版空库创建 | 0 | `0043` | 0 行 |
| 当前版迁移 | 0 | `0070` | 0 行 |

临时库与源码副本已由 `TemporaryDirectory` 清理。该检查没有启动应用，没有旧业务数据，没有 PostgreSQL、排空、降级或恢复，也没有使用旧依赖锁，不能替代任何 C8 验收项。正式演练的可复跑脚本和记录需另行保存。

## C7 与 C8 的边界

- `.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/verification.md` 的 C7 总验收覆盖当前版的迁移并发保护及 C6→C7 停服流程；两端 schema 都是 `0070`，不能证明 `0043` 的升级或回滚。
- `.trellis/tasks/archive/2026-11/11-20-mp-deploy-acceptance/research/c7_operation_acceptance/verification.md` 的 C7 操作演练提供隔离进程、本地假模型、PG 容器和恢复工具的可借鉴做法，但未覆盖 `0043`，也未验证生产代理维护模式、GPG 和跨主机。
- `4501f18:backend/app/core/config.py` 没有 `TASK_ADMISSION_CLOSED`；旧版不能照搬 C6 的管理端排空开关。外部限制新业务请求时要保留必要的维护通道，并独立核对同步流、后台任务及备份。
- 停旧服务前的备份验证不能排除备份完成后旧服务再次写入。正式流程还需停掉全部旧写进程后生成并验证最终快照，确定实际回滚点。
- 原背景调研中的旧任务行为、降级与旧版读取新库结论基于当时代码分析。当前版已加入 C1–C7，必须重新观察；不能把旧推断当成通过的实测。
