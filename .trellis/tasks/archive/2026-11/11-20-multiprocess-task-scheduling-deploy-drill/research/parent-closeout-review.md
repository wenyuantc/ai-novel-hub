# 多进程任务父计划收口核对

核对日期：2026-11-25。当前分支 `codex/mp-peer-channel-stream`，核对时 HEAD 为 `83300117be5d4fa1c6d1a66975ba525db4a44a77`。父计划只列 C1–C8，没有独立的 C9；本记录核对剩余的父任务收口。

## 任务与基线

- 父任务的八个子任务均已在 `.trellis/tasks/archive/2026-11/` 下找到各自的 `task.json`，状态均为 `completed`。父任务自身尚在 `planning`，且计划规定父任务不直接执行实现。
- C8 的父任务集成评审逐项列出了 AC1–AC13 的已有证据：`.trellis/tasks/archive/2026-11/11-20-collab-migration-deploy-drill/research/parent-integration-review.md`。
- 对照 C8 使用的新版基线 `b7b3d248a7462deedd0507275a0e6fd0a6ec8e0e`，核对时产品代码和部署配置无后续改动；后续变更为 Trellis 记录、README 与 TODOS。当前 README 的 SHA-256 为 `d1f43f9a32258a01acd7c54b20a939707438465d7ef1afedbc347eb44b2d093b`，与 C8 命令核验记录一致。
- 本次是文档、状态及既有证据核对，未重新执行全部测试或演练。C7/C8 记录的后端最终完整回归为 7,519 通过、76 跳过、19 xfailed；前端为 1,849 通过。跳过和 xfailed 不算通过，具体测试与隔离环境以原记录为准。

## 验收证据索引

以下文件均在 `.trellis/tasks/archive/2026-11/` 之下：

| 父计划验收项 | 既有证据 |
| --- | --- |
| AC1 存活任务及在途工作 | `11-20-mp-deploy-acceptance/research/verification.md` 的 AC1，链接至任务流、备份和在途工作记录。 |
| AC2 失联收尾 | 同一总记录的 AC2 及 `research/c7_task_acceptance/verification.md` 的强杀与接管记录。 |
| AC3 调度约束 | 同一总记录的 AC3，链接至任务流和 PostgreSQL 调度专项。 |
| AC4–AC5 跨进程取消和删除 | 同一总记录的 AC4–AC5，链接至 SQLite 与 PostgreSQL 两种形态的记录。 |
| AC6 跨进程流、撤权和断线 | 同一总记录的 AC6 及任务与实时流记录。 |
| AC7–AC9 设置、引擎外状态和排空 | 同一总记录的 AC7–AC9 及 `research/c7_operation_acceptance/verification.md`。 |
| AC10 并发迁移 | 同一总记录的 AC10 及 `research/c7_container_acceptance/verification.md`。 |
| AC11 SQLite HTTP 和忙锁 | 同一总记录的 AC11；结论限于各脚本实际运行的场景。 |
| AC12 单进程回归 | 同一总记录的 AC12，及 C8 父任务集成评审中的再次回归记录。 |
| AC13 旧版升级、排空及回滚 | `11-20-collab-migration-deploy-drill/research/verification.md` 和 `research/readme-independent-verification.md`。 |

真实跨主机共享目录和文件锁没有完成双主机验收；OrbStack 内置 Docker 的跨网络隔离测试未通过。未排空升级后的旧任务和未决提案处理仍是 `TODOS.md` 第 34 项。本次不能将隔离演练推广为生产环境验收。

## 归档前需处理的资料

父任务现有的以下七个文件在本次核对之前未被 Git 跟踪：

- `prd.md`、`task.json`、`implement.jsonl`、`check.jsonl`；
- `research/deployment-migration-rollback-background.md`、`research/in-process-state-outside-engine.md`、`research/task-engine-single-process-inventory.md`。

如果将父任务完整归档，这七个文件会随任务目录移动并纳入归档提交；`design.md` 和 `implement.md` 已由 Git 跟踪。本文件是本次新增的核对记录。

归档后需同步更新父任务设计与实施清单中指向已归档子任务的相对链接，以及 C1、C2、C4、C5、C6、C7、C8 的任务文档中指向父任务的链接或路径。历史提交计划和历史调研快照保留当时路径，不改写其历史事实。父任务 `branch` 原为空，且父任务未作为独立 PR 开发；归档时需按任务脚本提供的 `--skip-branch-validation` 处理，并核对归档后路径与 Git 状态。

候选改动已在独立临时副本中模拟应用和归档：18 个文件可干净应用，模拟归档后的 33 条 Markdown 链接及 10 条父任务清单引用均能找到目标；父任务 13 项验收标记与候选内容一致。这项检查不代替业务测试。

既有后端部署与数据库规范已包含多进程部署、网络隔离、`0043` 升级和快照回滚的约束；本次证据核对没有产生新的代码契约，暂不追加规范。
