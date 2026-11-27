# 隔离验证计划

本轮使用 C8 的旧版本、真实应用造数和假模型。每个场景使用独立克隆，核对版本、快照摘要和实例归属。以下保留批准时的验证清单，实际执行结果和证据以文末索引为准。

1. 在 SQLite、PostgreSQL 各准备已排空对照、旧章节 queued 在线快照、旧章节 running 强停冷快照、旧智能体 paused 正常退出快照；另保存旧版取消后仍有 proposed 的副本。记录版本、时间、任务、消息、原提案、模型／工具调用基线。按 [C8 脚本与执行说明](../archive/2026-11/11-20-collab-migration-deploy-drill/research/verification.md) 核实旧进程确已退出。
2. 分开验证排队章节和中断章节。对排队个案保留取消受理记录及实际状态。对中断个案按 design.md 的全部条件检查能否只恢复章节状态。加一个已有后继任务／新占用的反例，预期维护者停止，不调用复位。核对原任务、正文修订、场景、版本、新旧占用和模型调用；重启后再次核对。缺少原状态的副本必须保留 generating。
3. 对旧版网页取消、升级后取消重发分别跟踪同一原提案。检查内容摘要、目标业务记录及副作用，分开记录新模型调用。在已结束或缺原轮次键的个案中，预期无法确认／拒绝原提案，不产生额外任务、正文写入或外部调用。若研究旧版重试的特殊行为，须在独立副本逐条比较同轮全部消息和提案的归属。
4. 在空目标恢复升级前快照，验证版本、账号、密钥、作品、旧任务和原提案，列出新版独有数据。若恢复后仍有遗留，记录停止原因，保留源库。无快照个案以留存证据和停止写处置通过验收。
5. 记录每项的数据库、版本、身份、前后状态、观察时长、是否重启及证据位置。只对实测通过的组合更新现场说明；差异或未知保留在清单中。

## 本次已运行的当前代码回归

基线 ac97d4d，2026-11-27。以下命令分别在 backend 和 frontend 目录执行。后端 5 个参数化实例通过，前端 2 个文件共 36 项通过。覆盖无旧占用时管理员取消保留待处理、现代章节复位保留内容及后继占用、无法证明原消息或提案来源时拒绝重试，以及未知章节与旧提案的界面行为。这些测试使用项目夹具，不代替上面的双库旧快照验证。

```sh
.venv/bin/python -m pytest -q \
  tests/test_chapter_claim_core.py::test_manual_reset_invalidates_old_claim_without_modifying_content \
  tests/test_chapter_claim_author_wiring.py::test_author_reset_then_old_abort_preserves_new_batch_claim \
  tests/test_admin_chapter_claim_cancellation.py::test_admin_unproven_legacy_claim_is_not_guessed \
  tests/test_agent_turn_identity_runner.py::test_retry_rejects_unproven_source_without_backfilling
```

```sh
./node_modules/.bin/vitest run src/pages/project/ChapterEditorPage.taskScope.test.ts src/components/agent/agentUI.test.ts
```

## 执行结果

| 验证项 | 实际结果与证据 |
| --- | --- |
| 排空、排队、中断章、未知原状态及后继占用 | 两库各跑独立 draft 和 done 轮次。额外保留了接管后的项目级清理失败记录，单独证明写入方已退出，再验证章节复位。[章节结果、限制和复跑命令](research/chapter-results.md) |
| 旧版取消、升级后取消并同会话发新消息 | 两库各跑两个场景，每库 11 次原行核对，涵盖程序重启。[提案结果和复跑命令](research/proposal-verification.md) |
| 排空快照独立回退 | 两库分别验证原章节、原任务、原提案、密钥可用，以及新版新增作品只在新版库中。[恢复结果和复跑命令](research/restore.md) |

本轮使用真实 HTTP 业务入口、仅监听本机的假模型及临时数据库。旧版网页证据来自 C8。无可用快照的现场只做只读核对；这条停止条件没有对应的写入实验。
