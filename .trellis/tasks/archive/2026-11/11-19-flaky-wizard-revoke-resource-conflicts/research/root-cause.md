# 根因记录（2026-11-19）

所有 pytest 在 `backend/` 下以 `env LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8` 运行。

## A. `tests/test_tasks.py::test_resource_conflicts`

### 失败现场

修复前：原用例单独运行 25 次失败 1 次；仓库外诊断用例（`/tmp/diag_rc/test_diag_rc.py`，与原用例同时序，失败时打印任务错误与假模型调用）8 次内失败 2 次。两次失败的现场：

| 任务 | 状态 | 错误 |
| --- | --- | --- |
| 章节生成 | failed | 场景或相关设定已变化，请核对后重新生成（`scene_context_changed`） |
| 世界观向导 | failed | 一次为「未解析到任何世界观条目」（预期），一次为「模型输出不是合法的 JSON」（取错响应） |
| 角色向导 | succeeded | |

### 根因

两处都是用例时序，产品行为符合设计。

1. **在途生成被向导作废。** 用例在生成任务卡在 Gate 上时提交角色向导，再同时放行生成任务与世界观向导。角色向导写入新角色若先提交，作品的 AI 可见性快照随之变化，生成任务在采纳时复核快照失败，按设计失效关闭。先后由调度决定。
2. **假模型响应错配。** 用例只等世界观向导到达 Gate，没先等生成任务取走自己的响应；FakeLLM 按调用顺序出队，世界观向导偶尔先调用模型，拿走了 `[gate, "新正文"]`。

### 产品依据（确认失效关闭是既定行为）

- `.trellis/spec/backend/scenes.md:207`：读取或 AI 快照变化 → 409 `scene_context_changed`，流内为明确失败。
- `.trellis/spec/backend/setting-ai-visibility.md:26`：构建前读 `VisibilityStamp`，每次发送从独立事务验戳，不能给旧负载重签。
- `backend/app/services/scene_ai_context.py:83-107`：`require_stamp` 比对场景快照与可见性快照。
- `tests/test_wizard_visibility_fencing.py:37-50`：向导写入与已有设定竞争名称的新条目会使可见性 epoch 加一。
- 先例：`test_user_quotas` 曾因交错建章使在途任务失效、Gate 消费错位，当时同样只改用例顺序（`.trellis/tasks/archive/2026-11/11-07-independent-scenes/research/full-suite-ai-fixes.md`）。

### 修复

只改测试（`backend/tests/test_tasks.py:495-529`）：提交生成任务后先等它到达 Gate，再做互斥断言并提交世界观向导；放行生成任务并等其成功后，再在世界观向导仍卡住时提交角色向导。四条断言意图（章节生成与润色互斥、同类向导互斥、不同类向导互不影响、结束后可再次提交）全部保留。

### 验证

修复后 4 路并发共 200 次全部通过（见文末「验证汇总」）。

## B. `tests/test_collaboration_wizard_entry_e4g3c.py::test_revocation_notification_after_commit_still_finalizes_from_receipt`

### 复现尝试

- 规划阶段：8 路并发各 12 次，共 96 次，未复现。
- 实施阶段：8 路并发共 200 次，同时循环运行 `tests/test_tasks.py` 与 `tests/test_wizard.py` 作负载，结果见「验证汇总」。

原失败（2026-11-17，第 14 项完整回归）没有留存断言或错误信息。按用户决定 D1，以审查加诊断收口。

### 链路

用例在 done 事件核验前（`_event_task` 钩子）通过 HTTP 撤权。撤权提交后 `notify_collaboration_change`（`backend/app/services/task_engine/engine.py:2893`）为运行中的协作任务创建 `_check_collaboration_runs` 复查协程；复查经数据库确认授权失效后，登记 `_collaboration_revoked` 并调用 `cancel_running` 取消执行体。执行体收尾时（`_finalize`，1716 行起）凭收据调用 `_complete_from_wizard_receipt`（2261 行）记为 succeeded、error 为 `WIZARD_RECEIPT_FINALIZED_NOTE`，并释放 `wizard:{pid}:characters`。

审查中确认的两条保护：

- done 事件核验在独立协程中执行并受 `asyncio.shield` 保护（1636-1645 行），取消只落在执行体上，执行体等核验结束后再传递取消。
- 执行体进入 `_finalize` 时登记 `_finalizing`（1731 行）；复查在开始时（2928 行）和 `cancel_running` 内（2853 行）都会跳过收尾中的任务，取消不会打断收尾事务。

### 取消落点与验证

| 窗口 | 落点 | 结论 | 验证用例 |
| --- | --- | --- | --- |
| W1 | done 事件核验进行中 | 核验因撤权失败，执行体的取消被核验异常取代，按收据收尾 | 新增 `test_revocation_cancel_window_during_done_verification`，断言复查确已对执行体发出取消 |
| W2 | done 已核验并写入执行体结果，收尾前 | 视作 done 后才收到取消，按成功提交；终态核验按原 grant 拒绝发布，按收据收尾 | 新增 `test_revocation_cancel_window_after_done_published`，断言取消确实落在该处 |
| W3 | 执行体收尾中（终态事务前） | 复查跳过，不取消；终态核验拒绝发布，按收据收尾 | 新增 `test_revocation_cancel_window_during_finalization`，断言复查已结束且未发出取消；无通知的同落点已由 `test_change_after_commit_finalizes_from_receipt[finalization-*]` 覆盖 |
| W4 | 执行体已结束 | 复查无运行中目标，成功终态与结果不变 | 新增 `test_revocation_after_terminal_leaves_task_untouched` |
| W5 | 复查读库与收尾写库并发 | 复查只读；SQLite 下读不阻塞写，收尾路径的数据库异常会让 `_complete_from_wizard_receipt` 返回 None 并失败关闭 | 未单独构造；单进程测试中收尾时没有其他写者，见下方 H 项 |

四个新增用例都断言：终态 succeeded 且 error 为 NOTE（W4 为正常成功）、结果等于收据、执行键清空、无待定标记、内容已写入、无清理失败、无未决收据、`wizard:{pid}:characters` 已释放。

### 非产品竞争的候选原因

- **H1 等待超时**：`_terminal` 上限 15 秒（`tests/test_collaboration_entity_entry_e4g3b.py:184`）。同日 14:08 提交的 `af3137b` 记录了全量运行中映射器语句缓存累积导致单次 GC 暂停长达数十秒、带固定等待预算的用例随机超时，并已修复。第 14 项那次完整回归「启动于缺陷修复之前」，但无法从记录确认它启动于 `af3137b` 之前还是之后，因此只能列为最可能的候选，不能定论。
- **H2 测试非隔离**：`app` 夹具按用例新建应用、数据库与任务引擎，结束时停止引擎（`backend/tests/conftest.py:100-126`）；新增用例对引擎打补丁时均替换在类上，避免实例上残留绑定方法。未发现跨用例残留的路径。
- **H3 断言只认单一路径**：用例要求 error 为 NOTE。撤权在 done 核验之前完成，正常成功（error 为空）需要撤权晚于终态提交，即 W4，而 W4 不会出现在该用例的时序里。不成立。

### 结论

审查的四个落点都能正确收尾，产品侧未发现竞争。原失败未复现，不能保证它就是已覆盖的落点之一；最可能的解释是 H1，但缺证据。

为便于下次定位，原用例与新增用例在断言失败时都会输出任务状态、错误、执行键是否清空、待定与完成收据标记、清理失败标记（`_diagnose`），不含任务输入或正文。若下次是超时，会表现为 `TimeoutError` 而非断言失败，可直接区分 H1。

## 验证汇总

| 项 | 条件 | 结果 |
| --- | --- | --- |
| `test_resource_conflicts`（修复后） | 4 路并发，共 200 次 | 200 通过 |
| `test_revocation_notification_after_commit_still_finalizes_from_receipt` | 8 路并发共 200 次，同时循环运行 `tests/test_tasks.py` 与 `tests/test_wizard.py` 作负载 | 200 通过，未复现 |
| 新增 4 个落点用例 | 单独运行 | 通过 |
| `tests/test_tasks.py` + `tests/test_collaboration_wizard_entry_e4g3c.py` 整文件 | 无负载 | 131 通过 |
| 同上 | 与上一行并发负载同时运行（同机 9 个测试进程） | 130 通过、1 失败：`test_tasks.py::test_global_limit` |
| `test_global_limit` | 负载结束后单独运行 20 次 | 20 通过 |
| `uv run ruff check .` | | 通过 |

`test_global_limit` 本任务未改动。它在提交两个排队任务后固定 `sleep(0.05)` 再断言排队位置，重负载下该假设可能不成立；失败输出被截断，未取得断言详情，未做根因分析，记入 TODOS 第 20 项作为新发现。

只改了测试，未改产品代码，按精简回归策略未跑后端完整回归。
