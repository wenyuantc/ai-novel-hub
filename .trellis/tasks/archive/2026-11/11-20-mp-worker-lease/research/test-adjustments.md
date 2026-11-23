# 既有测试的前置调整

这些用例原先用停止调度的引擎执行单次领取，或在同一进程内直接调用恢复来模拟重启。增加执行者登记后，需要在测试中区分可领取的进程、仍健康的进程和已经停止的进程。以下为改动后的场景及保留的断言。文件均位于 `backend/tests/`。

## 单次领取

各用例保留手动控制的调度时序，在实际调用领取前用 `engine.lease.register()` 建立真实执行者登记。后台调度器仍停止，领取过程继续复核原任务和资源。对管理员取消、重用 ID 两个负例，同时解除本地停机标志，确保原断言确实经过待测的领取判断。

| 文件 | 测试或共享入口 | 原先验证的行为 |
| --- | --- | --- |
| test_task_execution_fencing.py | test_claim_confirmation_loss_never_adopts_another_execution；test_claim_confirmation_loss_accepts_its_own_committed_execution | 两种丢失提交确认的情况分别拒绝其他执行、继续自己的执行。 |
| test_task_execution_fencing.py | test_kb_claim_failure_rolls_back_task_running_transition | 同一事务回滚知识库文档占用和任务状态。 |
| test_task_abort_identity.py | test_old_failed_cleanup_never_releases_new_batch_claim | 重试清理只匹配捕获的旧章节占用。 |
| test_task_statistics_identity.py | test_stale_dispatch_identity_does_not_claim_replacement | 即使执行者可以领取，整数 ID 被复用后，旧统计 UUID 仍不能领取新任务。 |
| test_admin_task_cancellation.py | test_accepted_queued_holds_resource_until_abort_and_blocks_claim | 管理员受理取消时，正在收尾的任务保留资源、阻止其他领取。 |
| test_admin_task_adversarial.py | test_already_scanned_claim_loses_to_acceptance_and_atomically_releases_only_its_chapter_claim | 调度扫描先发生、管理员取消后提交时，迟到的领取不能恢复原章节占用。 |
| test_kb_index_admission_e6a.py | test_engine_claim_runs_valid_collaborator_index_and_closes_invalid | 有效协作索引正常执行，无效执行在派发时结束。 |
| test_kb_legacy_document_cleanup.py | claim_once：test_unproven_legacy_task_keeps_original_barrier、test_cross_project_document_is_not_cleaned、test_engine_does_not_clean_up_when_stopping_or_cancel_requested、test_direct_engine_claim_cleans_up_legacy_task、test_engine_cleanup_after_user_cancel_is_noop、test_null_queued_at_legacy_task_fails_closed | 用真实领取区分能清理的历史任务与跨项目、缺失凭据、已被取消、尚在停止的情况。 |
| test_agent_retry_execution_fencing.py | test_delayed_retry_rechecks_source_before_transferring_changes；test_retry_rejects_source_identity_replacement_after_queueing | 延迟到领取时再次核验原来源及外部调用，旧提案不会归属到新一轮。 |
| test_agent_turn_identity_runner.py | test_queued_retry_rejects_deleted_and_reused_source_id | 原任务整数 ID 重用时，重试拒绝新来源。 |
| test_agent_auto_external_fencing.py | test_queued_retry_with_new_uuid_rechecks_unknown_before_dispatch；test_queued_retry_cannot_rebind_source_with_changed_model_instance | 重试仍核验未知结果及完整模型实例。 |
| test_agent_auto_external_fencing.py | test_queued_agent_rejects_missing_or_replaced_original_actor；test_queued_agent_does_not_adopt_a_newer_token；test_queued_agent_rejects_model_id_reuse_even_with_same_fields | 领取时拒绝缺少快照、令牌替换和模型实例重用。 |
| test_chapter_claim_wiring.py | test_claim_failure_rolls_back_task_running_transition；test_claim_passes_original_running_identity_to_execution；test_unknown_claim_commit_requires_original_chapter_identity；test_unknown_claim_commit_accepts_only_own_persisted_claim | 章节占用激活失败仍回滚，成功或丢失提交确认时仍只接受自己的领取。 |
| test_chapter_claim_wiring.py | test_replaced_queued_claim_fails_old_task_without_touching_new_batch；test_unclaimed_old_queued_task_cannot_run_or_release_chapter；test_uncertain_save_retains_running_execution_and_claim；test_finish_only_clears_matching_admin_chapter_intent | 旧任务不得触碰新占用，写入结果未知仍保留原占用，管理员收尾只清匹配的意图。 |

## 恢复

| 文件 | 用例 | 调整与保留的断言 |
| --- | --- | --- |
| test_task_statistics.py | test_startup_recovery_records_once | 停旧引擎并布置旧库的无归属 running 行，启动引擎触发第一次恢复，再显式恢复一次。仍逐字比较两次统计的完成时刻。 |
| test_task_statistics_identity.py | test_recovery_snapshot_does_not_interrupt_replacement | 登记执行者，手动仅运行一轮恢复；后台调度器保持停止，以便检查收尾中换入的新任务没有被旧 UUID 影响。 |
| test_task_abort_identity.py | test_recovery_never_replays_unproven_running_analysis | 停机后构造未证实的分析记录，再启动引擎；任务仍保持运行、原执行键、原断点及待核对展示。 |
| test_collaboration_analysis_entry_e4g.py | test_analysis_receipt_completion_rejects_tampered_receipt；test_recover_completes_valid_analysis_receipt_left_running | 等实际执行体退出，再停止其登记并重新启动。损坏的收据保持屏障，完整收据仍能收尾且模型只调用一次。 |
| test_agent_mcp_skills.py | test_executing_external_failed_on_engine_restart | 将执行时长由零设为三分钟，覆盖已超过两分钟回收阈值的真实重启。仍拒绝重发、重试与继续。未过期调用另由 AC7 的双引擎用例覆盖。 |
| test_chapter_claim_wiring.py | test_recovery_retains_even_malformed_completion_receipt | 手工布置的无归属 running 行由重新启动的执行者恢复；损坏的完成收据仍完整保留。 |
| test_chapter_claim_author_wiring.py | test_unverifiable_committed_result_preserves_running_on_recovery | 先停拥有原任务的执行者，再在新登记下处理不可证结果；保留原 running、正文和屏障。 |
| test_collaboration_chapter_entry_e4f.py | test_receipt_completion_rejects_tampered_receipt；test_recover_completes_valid_receipt_left_running | stop/start 真实变更归属，坏收据拒绝收尾，完整收据完成且不重复生成。 |
| test_collaboration_wizard_entry_e4g3c.py | test_tampered_receipt_keeps_running_barrier_and_recover_refuses；test_recover_finalizes_committed_receipt_when_in_process_finalize_unavailable；test_recover_releases_provable_uncommitted_pending；test_recover_keeps_damaged_pending_unresolved | 完成原执行后用 stop/start 触发已停止执行者的接管。原四类断言分别覆盖损坏收据、有效收据、有效未决证据以及损坏未决证据。 |
| test_wizard_uncertain_cleanup.py | test_recovery_keeps_orphan_wizard_running_and_blocks_retry | 停机布置的无归属历史记录由 start 恢复；保持运行及待核对、阻止重试。 |
| test_batch_chapter_claim.py | test_recovery_of_batch_review_keeps_pending_without_model_replay | 在原执行者停机后，用已提交的真实提案收据重建崩溃时的 running 快照，再启动执行者。恢复后仍保留原候选、原文和一次模型调用。 |
| test_rich_document_proposals.py | test_pending_review_is_recovered_from_running_checkpoint_without_model_replay | 同样按停机、恢复轮次、启动接管的顺序准备单章富文档提案，断言待审核状态、收据和正文保持一致。 |

另将 `test_collaboration_wizard_entry_e4g3c.py::test_recover_finalizes_committed_receipt_when_in_process_finalize_unavailable` 的真实完成收据场景扩展为临时读取失败/正常读取两个分支。临时错误后仍持有本轮接管权，下次恢复可完成原收据；原有正常恢复断言继续保留。

## 续跑、终态清理及迟到写入

| 文件 | 用例 | 增补的行为检查 |
| --- | --- | --- |
| test_admin_task_cancellation.py | test_resume_starts_new_queued_clock | 恢复暂停任务后清空上一轮执行者与待核对标记，管理员仍可取消新的排队轮次。 |
| test_rich_document_proposals.py | test_batch_review_resumes_at_next_phase_without_regenerating_current_chapter | 接受批量富文档提案后先检查排队行清空旧归属，再启动新执行者，等待下一章实际执行并检查新归属。原正文与模型调用断言继续执行。 |
| test_agent_auto_external_fencing.py | test_old_cleanup_never_modifies_replaced_identity | 在原有身份替换参数中增加归属改变和原登记已围栏两种情况，旧智能体不可写外部调用结果。 |
| test_task_abort_identity.py | test_old_failed_cleanup_never_releases_new_batch_claim | 故障注入继续记录收到的旧凭据，并传递项目排空重试时要求的终态；原有新章节占用与两次调用身份断言保留。 |
| test_recycle_bin_tasks.py | test_running_abort_failure_keeps_terminal_state_and_next_delete_can_retry | 第二次删除沿用失败时捕获的章节凭据并显式匹配 cancelled。两次删除仍分别返回 409 和 204。 |

## SQLite 独立租约文件

以下运行态测试让两个实际的任务引擎登记租约。原任务轮次、业务资源和归属仍保存在主库，围栏使用 `worker_helpers.fence_worker(engine, mirror=False)` 让权威侧库拒绝旧执行者，主库的登记镜像尚未同步。这样旧执行者仍匹配主库的活跃登记和原任务归属，业务守卫必须实际验证侧库才能通过测试。手工布置与原执行者有关的业务凭据时沿用真实登记返回的 UUID。只改变归属的用例保持原执行者自身的租约有效，以单独检验任务归属比较。

| 文件 | 用例 | 保留的行为 |
| --- | --- | --- |
| test_agent_auto_external_fencing.py | test_old_cleanup_never_modifies_replaced_identity 的 worker、worker_fenced 分支 | 独立验证任务归属改变和原登记被围栏时，过期执行对象不能修改外部调用的已有结算凭据。 |
| test_agent_revocation_e4ie.py | test_only_current_recovery_worker_can_void_revoked_turn | 先验证仅有围栏、再验证归属移交，最终只有持有新登记的一方能作废提案。 |
| test_consistency_check.py | test_old_worker_cannot_close_running_consistency_check | 旧归属不能关闭运行记录；登记有效且主库归属匹配的新执行者能提交收尾。 |
| test_kb_index_claims.py | test_old_worker_cannot_release_an_indexing_document | 围栏未移交时保留索引资源；移交后可由新执行者释放；已提交的原终态仍可精确重试资源清理。 |
| test_task_event_guard.py | test_lost_worker_cannot_publish_flush_or_clear_success | 区分本进程失效、数据库中原登记被围栏、任务只更换归属；每种情形都阻止迟到事件、刷库和成功回调。 |
| test_task_worker_handler_fencing.py | test_old_chapter_cannot_release_or_log；test_old_cleanup_preserves_pending；test_old_batch_checkpoint_write_cannot_advance | 真实章节、分析和向导、批量断点的迟到写入均须拒绝，现有正文、占用、凭据和日志保持原事实。 |

## 作品备份的结构版本

0066 新增应用表和列，单书导出的 `app_schema` 随迁移头变为 0066，备份格式仍为 v16，以下既有测试维持精确版本断言。完整往返用例还检查执行者登记表不会进入作品导出。

- `test_agent_api.py` 的 `test_agent_versions_and_success_error_logs_survive_backup`。
- `test_batch_revision_transfer.py` 的 `test_batch_revision_transfer_roundtrip_and_undo`。
- `test_category_transfer.py` 的 `test_categories_v5_roundtrip_preserves_tags_rules_and_resets_both_revisions`。
- `test_chapter_claim_migration.py` 的 `test_single_book_backup_never_transfers_or_accepts_chapter_claim`。
- `test_consistency_impact_transfer.py` 的 `test_v13_roundtrip_remaps_impact_references`。
- `test_consistency_transfer.py` 的 `test_v12_roundtrip_remaps_evidence_and_drops_runs`。
- `test_kb_index_claims.py` 的 `test_book_backup_omits_claim_and_import_never_restores_injected_claim`。
- `test_mention_compat.py` 的 `test_world_aliases_new_and_old_backup_round_trip`。
- `test_notes.py` 的 `test_note_flags_backup_v9_roundtrip_v8_defaults_and_rejections`。
- `test_pen_names.py` 的 `test_backup_only_current_pen_name_and_restores_per_importer`。
- `test_project_cover.py` 的 `test_cover_backup_roundtrip_legacy_and_corruption`。
- `test_project_transfer.py` 的 `test_progressions_round_trip_preserves_historical_references`、`test_round_trip_all_entities`、`test_typed_v4_roundtrip_remaps_all_history_and_resets_revision`。
- `test_reader.py` 的 `test_progress_survives_backup_round_trip`。
- `test_rich_document_transfer.py` 的 `test_rich_backup_roundtrip_rebinds_scene_and_section_ids`。
- `test_scene_transfer.py` 的 `test_v7_scenes_raw_planning_history_and_retired_ids_roundtrip`。
- `test_serialization_transfer.py` 的 `test_serialization_transfer_roundtrip`。
- `test_series_transfer.py` 的 `test_series_backup_materializes_only_the_book_and_remaps_cast`。
- `test_setting_ai_visibility_formats.py` 的 `test_v6_roundtrip_remaps_links_and_does_not_export_or_copy_runtime_counters`。
- `test_setting_fields_categories_roundtrip.py` 的 `test_v5_roundtrip_replays_four_entities_fields_and_categories_and_isolates_projects`。

## 0045 历史迁移的任务表结构

全量回归发现，`test_0045_sqlite_identity_constraints_and_lossless_downgrade` 用当前 ORM 生成 0045 的预期任务表。0066 新增 `tasks.worker_key` 和 `tasks.recovered_at` 后，这个预期多出两列。测试现从预期中按名称剔除这两列，再与实际迁移到 0045 的任务表完整比较。列类型、可空性、默认值、主键、外键、唯一约束、检查约束及索引均参与比较。原测试仍验证索引身份约束、降级保护和成功降级后的文档原文、分块及外键完整性。

`test_0066_upgrade_downgrade_guards_and_roundtrip` 单独检查两列在 0066 可空、旧任务升级后两列值为空，成功降回 0065 后两列均消失。该用例同时覆盖运行中任务已有执行者时拒绝降级。

## 运行记录

- 修订 0045 的预期结构后，串行运行 `test_kb_index_identity_migration.py`、`test_task_worker_migration.py` 和 `test_migrations.py`，仅启用 SQLite 时 30 项通过、1 项 PostgreSQL 参数跳过（24.17 秒）。启用隔离 PostgreSQL 16.15 后，相同三个文件 31 项全部通过（34.91 秒）。
- 单次领取相关补充回归共七个文件，158 项通过（276.30 秒）。包含 `test_task_execution_fencing.py`、`test_kb_index_admission_e6a.py`、`test_kb_legacy_document_cleanup.py`、`test_agent_auto_external_fencing.py`、`test_agent_retry_execution_fencing.py`、`test_agent_turn_identity_runner.py`、`test_admin_task_adversarial.py`。
- 真实停机前置修订后的富文档单章/批量恢复、任务 UUID 恢复快照、无法证实的分析恢复（三种断点）及启动恢复统计共 7 项通过（11.56 秒）。
- 步骤 6 及同属父任务第 10.1–10.2 节的恢复、统计、MCP 等共 20 个文件，按文件分配两个 pytest worker。集成期间的首次运行共 574 项，563 项通过，6 项为已有严格预期失败（`test_agent_mcp_skills.py` 的 `AUTO_MCP_PENDING`），5 项失败，耗时 557.49 秒。
- 两个提案恢复失败来自旧前置仍将任务留给健康执行者，分别为单章富文档和批量富文档。另有向导有效未决凭据作者/协作者两个分支、`test_tasks.py::test_start_recovers_crashed_running_task` 在收尾代码合入前的测试进程中仍见 running。所有五项在修订后的相同工作区重新运行，5 项均通过（8.04 秒）。最终完整回归另记于 verification.md。
- SQLite 执行者登记改用独立租约文件后，“SQLite 独立租约文件”节列出的六个业务测试文件在真实文件库复跑，147 项通过（186.00 秒），六文件 Ruff 通过。围栏分支在权威文件已写入 fenced_at 且主库登记镜像仍未写入停止或围栏标记时执行；显式更换归属的分支使用另一实际登记的 worker_key。
