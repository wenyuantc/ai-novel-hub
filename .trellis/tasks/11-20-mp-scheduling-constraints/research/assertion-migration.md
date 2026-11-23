# 原调度内存断言的迁移

旧行号取自 C1 收口后的 `a16eb2a`。表中涉及配额页的用例通过有权限的管理员调用真实 HTTP 接口。引擎用来保护本地取消游标的标记仍由对应取消测试观察。

| 原文件、用例与行号 | 现在观察的行为及原因 |
| --- | --- |
| `test_rbac_concurrency.py` · `test_candidate_rechecks_lowered_quota_and_claim_holds_commit_return_gap`，229、264、274 | 两个候选尚未领取时 A 的配额页读 0。A 已提交但回执尚未返回时，B 的配额页应读 1。原测试在此时读本机字典得到 0。回执返回后 A 同样读 1，后项保持排队且模型只执行一次。首次运行 B 的断言实际得到 0，复现旧实现。 |
| 同文件 · `test_database_terminal_state_does_not_release_post_success_execution_slot`，331、339 | 首项已经落成成功，但把原执行体停在成功回调。两个应用分别读出 1。分别设置用户额度 1、不同用户共享全局额度 1，以及两级均为 2 但同项目章节分析串行三种情形。B 实际尝试后项的门内领取后，该项仍处于排队，第二个模型尚未执行。放行回调后后项进入模型，所属用户的配额页读 1。 |
| 同文件 · `test_unknown_settings_commit_pauses_admission_until_confirmed_publish`，441、448 | 设置提交结果未知时，独立数据库会话看到 0 条执行占用，没有模型调用，任务仍排队。重新确认设置后通过配额页读 1 并验证只执行一次。未知窗口的独立查询仅查占用，该观察不依赖故障中的设置服务。 |
| 同文件 · `test_cancel_in_claim_gap_defers_signal_until_safe_finalize`，535、560 | 注入的两个窗口均在真实提交之后，配额页均读 1。继续保留本机 `_claiming` 和原执行游标的断言，覆盖本人取消、关停及各自的终态。排空后配额页读 0。 |
| `test_user_quotas.py` · `test_task_zero_queues_then_raise_runs_and_lower_does_not_kill`，146、153 | 限额为 0 时 API 的已用为 0，模型调用为零。调到 2 后两个模型都到达屏障，GET 读 2，再降到 0 后 PATCH 的回包也读 2，正在执行的模型均继续完成。 |
| 同文件 · `test_unlimited_task_quota_still_respects_global_cap`，181、185 | 个人不限、全局上限为 1，在第一项及下一项各自进入模型后，管理员 API 都读 1。 |
| 同文件 · `test_explicit_task_limit_replaces_default_and_inherit_uses_latest_default`，228、239 | 用户显式额度生效时，两个实际模型调用对应 API 已用 2。切为继承后第三个模型进入，API 已用 3，原有任务未被取消。 |
| `test_tasks.py` · `test_serial_key_after_restart`，859 | 停机后从独立会话查询原串行键已归还，重启后同键第二章能执行，被中断的第一章也能走重试入口。用持久事实和真实领取代替本地字典是否为空。 |
| `test_task_worker_lease.py` · `test_uncertain_task_is_visible_from_both_engines_without_repeated_cleanup`，349 | 两端会话查询实际占用均为 0，同时保留恢复时间和空执行者身份的检查。后续真实同键任务能够保存正文。 |
| `test_admin_chapter_claim_cancellation.py` · `test_admin_waits_for_original_running_executor_to_stop`，361、369–391 | 分别保留领取中、收尾中、原执行身份尚在三个场景。原 `_admitted` 的本地保护由已有的 `_running_execution_keys` 承担。三个游标都释放后，管理员才能清理资源并结束取消操作。另由上述用例证明全站名额数。 |

另外两处测试原先直接修改内存设置。`test_analysis.py::test_enqueue_auto_dedup_queued_but_not_running` 通过管理员设置接口在运行中放宽限额。`test_task_statistics_integration.py::test_direct_auto_analysis_and_memory_enqueue_record_new_tasks` 在创建应用时指定测试需要的部署默认值。

补充的 `test_uncommitted_candidates_keep_global_position_in_list_detail_and_progress` 在三个真实入队任务进入领取事务前暂停。另一用户的任务占第 0 位，两端的列表和详情对后两项均给出 1、2，原实例的订阅进度帧给出相同位次。另一用户的任务详情仍受可见性限制。`test_quota_counts_frozen_actor_slots_once_after_task_pruning` 在任务行已不存在时核对冻结用户的占用，覆盖多个角色关联、两个用户和两个实例上的默认限额。
