# 技术设计

## 边界

默认只改 `backend/tests/`。只有在 B 的窗口审查证明产品存在竞争时才改 `backend/app/services/task_engine/engine.py` 或 `wizard_service`，且改动须先回到规划向用户说明。不改 FakeLLM 的出队机制，不改调度、失效关闭与收据核验逻辑。

## A：`test_resource_conflicts`

现状时序（`backend/tests/test_tasks.py:495-527`）：生成任务与世界观向导都卡在各自的 Gate 上，此时提交角色向导，随后同时放行。两处竞争：

1. 生成任务与世界观向导谁先调用模型未受控，FakeLLM 按调用顺序出队，可能错配。
2. 角色向导写入新角色与生成任务收尾的先后未受控，前者先提交则后者以 `scene_context_changed` 失败关闭。

修法是把先后交给用例控制，不改断言意图：

- 提交生成任务后先 `await gate.wait_reached()`，再推入并提交世界观向导。此时互斥断言（generate／polish 返回 409）仍在生成任务运行中做出。
- 「不同类向导互不影响」只需要世界观向导在运行，不需要生成任务在途。因此先放行 `gate` 并等生成任务 succeeded，再提交角色向导；世界观向导此时仍卡在 `wgate` 上，断言含义不变。
- 之后放行 `wgate`，其余断言照旧。

实施时先确认一点：向导新增设定使同作品在途生成任务失效，是否在规范或既有用例中有明确依据（`.trellis/spec/backend/scenes.md`、`setting-ai-visibility.md`）。有依据则按上述只修测试；若发现这是未预期的过度失效，停下回到规划。

备选方案是在用例里屏蔽失效检查，否决：会让用例脱离真实行为。

## B：向导撤权用例

链路（`backend/app/services/task_engine/engine.py`）：用例在 `_event_task` 发布 done 事件前执行撤权；撤权提交后 `notify_collaboration_change`（2893 行）为运行中的任务创建 `_check_collaboration_runs` 复查任务；复查经数据库确认授权失效后按执行键取消执行体；执行体被取消后由 `_complete_from_wizard_receipt`（2261 行）凭收据收尾为 succeeded 并释放 `wizard:{pid}:characters`。

复查任务是独立协程，取消落点由调度决定。需要审查并逐一验证的窗口：

| 窗口 | 取消落点 | 预期 |
| --- | --- | --- |
| W1 | done 事件发布前（撤权钩子返回后立即） | 凭收据收尾 |
| W2 | done 事件发布中或发布后、终态事务开始前 | 凭收据收尾 |
| W3 | 终态事务进行中（`complete_state` 内） | 正常终态或凭收据收尾，二者之一，不留 running |
| W4 | 执行体已进入 `_finalizing` 或已结束，复查扑空 | 复查跳过，任务已是终态 |
| W5 | 复查读库与执行体终态提交并发（SQLite 写锁等待） | 不死锁、不超时停在 running |

另有三个非产品竞争的候选原因，一并验证：

- H1 用例等待超时：`_terminal` 上限 15 秒（`tests/test_collaboration_batch_entry_e4g2.py:167`），`_assert_released` 再启动一轮向导；完整回归加浏览器验收的负载下可能超时。
- H2 测试非隔离：前序用例遗留的引擎后台任务、monkeypatch 残留（11-16 Word 导出任务记录过对 handler 实例打补丁后遗留绑定方法的先例）。
- H3 断言假设了单一路径：用例要求 `row.error == NOTE`，若取消落在 W3 之后任务走了正常成功路径，error 不是 NOTE，断言失败但产品正确。

验证手段：对每个窗口，用 monkeypatch 钩子把取消固定在该落点（沿用同文件 `_hook_after_commit` 的做法），断言任务终态、`_wizard_completion` 收据、资源释放与无清理失败残留。已有用例覆盖的窗口直接引用，不重复写。H3 若成立，属于测试断言过严，按产品契约修正断言并说明依据。

诊断输出：用例失败时附带任务的 status、error、execution_key 是否为空、checkpoint 中 `_wizard_pending`／`_wizard_completion` 是否存在、fired 列表与 `has_cleanup_failure`。做成该文件内的小辅助函数，只在断言消息中使用，不打印任务输入或正文。

## 兼容与回滚

只动测试时无兼容问题，回滚即还原测试文件。若动产品代码，新增回归用例须在修复前失败，并跑后端完整回归。
