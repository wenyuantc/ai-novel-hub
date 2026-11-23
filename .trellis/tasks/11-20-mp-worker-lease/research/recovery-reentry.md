# Research: 接管者在清理提交后失联的恢复结果

- Query: C1 设计第 9.2 节。接管者 B 清理资源后、写入 Task 终态前退出，接管者 C 重跑原恢复流程能否得到与 B 一次成功相同的资源、任务和统计状态？
- Scope: internal。检查 `backend/app/services/task_engine/engine.py` 启动恢复各分支，以及 handler 调用的资源归还方法。在项目 SQLite 测试夹具中做单章与向导故障注入。
- Date: 2026-11-23

## Findings

### 两个经实测确认的非幂等分支

| 真实任务与停止时机 | 一次完整恢复 | B 清理后退出，C 接续恢复 |
| --- | --- | --- |
| 单章生成，B 已将章节退回草稿，同时清除了任务中的原章节占用指针 | 章节 draft，任务 interrupted，执行键清空，作品无清理失败记录 | 章节 draft，任务 interrupted，执行键仍在，作品有清理失败记录 |
| 角色向导，B 已验证没有完成收据并移除本轮未决标记 | 任务 interrupted，执行键清空，作品内容未变 | 任务 running，执行键仍在，C 标记待核对，作品内容未变 |

单章的 `ChapterHandler.on_abort` 在自己的数据库会话提交（`backend/app/services/task_engine/handlers/chapter.py:422-440`）。同一提交清除章节占用和 Task.input 中的 `_chapter_claim`（`backend/app/services/chapter_claim.py:443-486`）。此后 C 仍能读到 running，但 `capture_running_claim` 按契约拒绝缺少指针的输入（`backend/app/services/task_engine/handlers/chapter.py:65-86`）。恢复将清理失败登记至作品，调用 `complete_state(abort_ok=False)`，留下执行键（`backend/app/services/task_engine/engine.py:544-567,2703-2708`）。用户重试会因该键收到冲突，删除作品也会被阻止（`backend/app/services/task_service.py:679-682,1642-1645,1671-1684`）。

向导的 `release_uncommitted_pending` 验证原轮次的未决凭据，清除唯一可证明未提交的标记（`backend/app/services/wizard_service.py:388-422`），`_release_wizard_pending` 单独提交（`backend/app/services/task_engine/engine.py:2330-2344`）。C 重读时只有空断点，无法区分此前已验证未提交与原本凭据丢失，必须走“待核对”分支（`engine.py:457-478`）。

单章实验使用同目录的 `chapter_recovery_probe.py`。基线版本通过真实 HTTP 接受和真实 Task CAS 领取准备单章，替换模型执行以保留运行中状态。B 的 `complete_state` 入口模拟进程停止，确认 B 的独立清理已经提交，再用全新 TaskEngine C 从数据库恢复。另设健康对照。执行结果为 `2 passed in 1.81s`。在 `backend/` 运行：

```sh
env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8 PYTHONDONTWRITEBYTECODE=1 uv run python -m pytest -c pyproject.toml -p tests.conftest -q -s ../.trellis/tasks/11-20-mp-worker-lease/research/chapter_recovery_probe.py
```

向导实验由主会话独立运行，复用 `backend/tests/test_collaboration_wizard_entry_e4g3c.py` 的角色向导和失败模型。断点在 `_release_wizard_pending` 提交返回之后，新 TaskEngine 对同一个数据库重跑 `recover()`；对照与故障组共 `2 passed in 3.41s`。两个故障注入都是基线诊断，不是对新设计的验收。

在上面的命令中将脚本名替换为 `wizard_recovery_probe.py` 可单独复跑向导。修改前另运行 `test_task_execution_identity.py`、`test_tasks.py`、`test_chapter_claim_wiring.py`、`test_batch_chapter_claim.py` 四个文件，结果为 128 项通过。

### 其他恢复分支

| 分支 | 当前代码及接管者重新进入时的结果 |
| --- | --- |
| 批量生成，已有当前章节 | `handlers/batch.py:323-355`、`batch_service.py:694-705,730-749`。同一事务还原章节并将持久断点的 current 置空（`chapter_claim.py:455-485`）。C 重建当前占用得到 None，可走普通中断。断点存在完成收据或指向分析步骤时走专门证明，不能把不同状态一律视为无占用。 |
| 知识库文档索引 | `kb_index_service.py:486-495,1949-1963`。只处理绑定至本轮且仍 pending/indexing 的文档，清理后状态 failed 且占用字段已空，重复清理无匹配行。C 仍可正常终结 Task。其他索引模式不还原文档。 |
| 智能体协作轮次 | `handlers/agent.py:45-88,747-759`、`agent/service.py:91-120`。失权时只改变同轮次仍 proposed、且无未知外部执行标记的提案。同一提案已 failed，C 不会重复改变。已有外部执行调用的独立清理另属 stale-only 设计。 |
| 一致性检查 | `handlers/consistency.py:150-162`、`consistency/check.py:479-499`。恢复令仍为 running 的检查记录进入 interrupted。第二次更新不命中。此处对旧执行体仅凭 task_id 关闭记录的风险属于日常写入围栏审计。 |
| 分析、向导、单章已有完成收据 | `engine.py:448-467,511-533,2112-2170,2187-2253,2261-2312`。受限恢复核验收据，终态和统计在同一事务写入。在提交前退出可继续核验，成功提交后不再满足 running。C1 仍需在这些专门的收据写入中校验接管者身份。 |
| 已有富文档提案、批量已有可证明的分析断点 | `engine.py:479-540,2596-2651,2730-2742`。持久提案或收据可重新验证。提交前退出不会单独清除它们。当任务进入 paused / interrupted 则退出运行中候选。 |
| 不可证明的分析、向导、单章与批量证据 | `engine.py:472-478,536-540`。原代码只记内存，按 C1 设计应由持有者在一个条件更新中同时交回 worker_key 并记录 recovered_at，C 不能因为凭据缺失而自动认定业务未执行。 |
| 大纲、设定、场景重组、拆书、影响评估 | `handlers/{outline,entities,scene_restructure,book_import,consistency}.py` 的对应 on_abort 无资源写入；清理和终态之间没有上述凭据丢失问题。 |

### 对评审点 A 的结论

原恢复分支原样复用无法满足 AC4。增加 worker_key 的写入条件可以拒绝旧进程，它无法补回已经删去的业务证据。涉及清理和终态两个事务的结构，须按 `implement.md`「风险文件与回退点」回到设计；现有结论不足以勾选 AC4。

建议将单章的“验证并释放章节占用”、向导的“验证并释放未决标记”分别作为接管终态事务内的专门步骤，复用 `release_chapter_claim(db, ...)` 与 `release_uncommitted_pending(db, ...)`。在同一事务持锁核对接管者、原任务轮次和业务证据，清理、写终态、记录统计一起提交。失败时回滚全部写入并保留隔离旧任务的现有语义，不可因章节已经是 draft 或标记不存在就推断已经安全清理。已有同事务处理可参考 `engine.py:2656-2676,2730-2742` 批量未知结果分支和 `tests/test_chapter_claim_wiring.py:53-84` 排队取消回滚断言。若继续采用独立清理事务，则必须在该事务中写入持久且可锁内核验的本轮清理收据，并定义兼容旧数据、消费和清理时机。

新用例应分别在清理前、清理后终态前、整体提交后确认丢失三处故障注入，比较 Task 执行键、章节占用、待核对标记、再次重试能力和统计。用第二次新接管者完成的终态验证设计，单独验证“认领后尚未开始清理即死”不足以覆盖这两个反例。

## Caveats / Not Found

- 静态源码基于 0065；仅前两条分支做了 SQLite 故障注入。未执行 PostgreSQL 租约、真实操作系统进程强杀或现有方案中的全量验收，本研究不替代这些测试。
- 表中可重入结论只分析原接管者已经停止、接管交替的场景。旧执行者恢复运行时，所有有副作用的路径仍需持久执行者身份与锁内核验。设计中仅记录“已认领”的内存集合不能提供持久重入证据。
- 相关规范为 `.trellis/spec/backend/error-handling.md` 第 6 节、`.trellis/spec/backend/admin-task-monitoring.md` 和 `.trellis/spec/backend/batch-generate.md`。没有使用外部资料。
