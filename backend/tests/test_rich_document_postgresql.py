"""真实 PostgreSQL：迁移、并发、阅读导出、标记、提案与模型发送屏障。"""

import asyncio
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import sqlalchemy.ext.asyncio as sa_async
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.models import Chapter, SceneSection
from tests.test_marker_lifecycle import (
    test_cannot_restore_source_tombstone_after_real_cross_scene_relink as _marker_no_duplicate_relink,
)
from tests.test_marker_lifecycle import (
    test_cross_chapter_relink_and_history_restore_are_serialized as _marker_restore_race,
)
from tests.test_marker_lifecycle import (
    test_cross_chapter_relink_checks_both_versions_and_crlf as _marker_cross_chapter,
)
from tests.test_marker_lifecycle import (
    test_cross_chapter_relink_then_restore_source_history as _marker_restore_cross_chapter,
)
from tests.test_marker_lifecycle import (
    test_cross_scene_relink_scrubs_source_and_preserves_historical_restore as _marker_relink_scrub,
)
from tests.test_marker_lifecycle import (
    test_delete_retired_marker_and_restore_version as _marker_delete_retired,
)
from tests.test_marker_lifecycle import (
    test_delete_stale_scrubs_backup_but_version_remains_restorable as _marker_delete_scrub,
)
from tests.test_marker_lifecycle import (
    test_existing_cross_chapter_double_live_backup_roundtrip as _marker_legacy_double_live,
)
from tests.test_marker_lifecycle import (
    test_history_restore_allows_unrelated_same_uuid_in_another_chapter as _marker_unrelated_history,
)
from tests.test_marker_lifecycle import (
    test_history_restore_scrubs_removed_marker_and_can_restore_later_version as _marker_history_scrub,
)
from tests.test_marker_lifecycle import (
    test_relink_retired_marker_preserves_scene_history as _marker_relink_retired,
)
from tests.test_marker_lifecycle import (
    test_same_scene_stale_relink_and_explicit_active_delete as _marker_same_scene,
)
from tests.test_marker_lifecycle import (
    test_split_and_merge_scrub_migrated_marker_tombstones as _marker_split_merge_scrub,
)
from tests.test_marker_lifecycle import (
    test_split_merge_preserves_active_marker_metadata_and_history as _marker_split_merge_metadata,
)
from tests.test_rich_document_analysis import (
    test_analysis_legacy_baseline_visible_sources_and_format_only_stability as _analysis_freshness,
)
from tests.test_rich_document_analysis import (
    test_shared_fingerprint_matches_pure_projection_for_multi_scene_unicode as _multi_scene_fingerprint,
)
from tests.test_rich_document_api import add_section, save_payload
from tests.test_rich_document_api import (
    test_explicit_marker_restore_reuses_same_scene_identity_and_rejects_stale_revision as _marker_explicit_restore,
)
from tests.test_rich_document_api import (
    test_explicit_section_restore_preserves_identity_and_section_marker as _section_explicit_restore,
)
from tests.test_rich_document_api import (
    test_history_restore_requires_confirmation_and_is_itself_reversible as _history_restore,
)
from tests.test_rich_document_api import (
    test_legacy_backup_without_draft_excerpt_fields_still_imports as _marker_legacy_excerpt,
)
from tests.test_rich_document_api import (
    test_long_marker_growth_invalidation_truncation_backup_and_history as _long_marker,
)
from tests.test_rich_document_api import (
    test_marker_restore_allows_unrelated_same_uuid_but_rejects_live_lineage as _marker_restore_lineage,
)
from tests.test_rich_document_api import (
    test_marker_saved_baseline_and_unverified_draft_excerpt_survive_history_backup as _marker_draft_excerpt,
)
from tests.test_rich_document_api import (
    test_section_marker_draft_excerpt_keeps_section_baseline_and_survives_backup as _section_marker_draft_excerpt,
)
from tests.test_rich_document_api import (
    test_section_restore_allows_unrelated_uuid_but_rejects_live_lineage as _section_restore_lineage,
)
from tests.test_rich_document_dispatch import (
    test_moving_enabled_section_obeys_actual_send_lease as _section_move_visibility,
)
from tests.test_rich_document_dispatch import (
    test_redirect_does_not_replay_old_enabled_section_after_tightening as _redirect_visibility,
)
from tests.test_rich_document_dispatch import (
    test_tightening_waits_for_real_send_headers_then_blocks_stale_retry as _send_visibility,
)
from tests.test_rich_document_history import (
    test_history_blocks_cross_chapter_section_lineage_and_preserves_data as _section_history_lineage,
)
from tests.test_rich_document_legacy_guard import (
    test_merge_rejects_unrelated_marker_identity_even_if_text_and_color_match as _unrelated_merge,
)
from tests.test_rich_document_legacy_guard import (
    test_split_and_merge_keep_earlier_structure_without_relocation as _left_split_merge,
)
from tests.test_rich_document_legacy_guard import (
    test_split_and_merge_move_right_format_or_marker_with_history as _right_split_merge,
)
from tests.test_rich_document_legacy_guard import (
    test_split_and_merge_right_section_with_dependent_marker_and_visibility as _right_section_split_merge,
)
from tests.test_rich_document_proposals import (
    test_batch_review_analysis_committed_before_checkpoint_is_not_sent_again as _batch_analysis_commit,
)
from tests.test_rich_document_proposals import (
    test_batch_review_analysis_interrupted_in_flight_is_not_automatically_resent as _batch_analysis_interrupt,
)
from tests.test_rich_document_proposals import (
    test_batch_review_resumes_at_next_phase_without_regenerating_current_chapter as _batch_review,
)
from tests.test_rich_document_proposals import (
    test_cancel_and_accept_compete_without_partial_write as _review_cancel_race,
)
from tests.test_rich_document_proposals import (
    test_cancelling_old_paused_proposal_does_not_release_new_task_chapter as _review_old_cancel,
)
from tests.test_rich_document_proposals import (
    test_edit_during_review_keeps_stale_proposal_readable_but_cannot_apply as _review_stale,
)
from tests.test_rich_document_proposals import (
    test_generic_termination_discards_review_without_faking_adoption as _review_terminate,
)
from tests.test_rich_document_proposals import (
    test_pending_review_is_recovered_from_running_checkpoint_without_model_replay as _review_recover,
)
from tests.test_rich_document_proposals import (
    test_proposal_chunks_round_trip_and_corruption_never_applies as _review_chunks,
)
from tests.test_rich_document_proposals import (
    test_proposal_requires_confirmation_then_atomically_applies_once as _review_accept,
)
from tests.test_rich_document_proposals import (
    test_review_accept_rechecks_account_identity_after_authentication as _review_auth,
)
from tests.test_rich_document_render import (
    test_multiscene_projection_keeps_legacy_crlf_seam_and_formats_only_real_text as _reader_crlf_seam,
)
from tests.test_rich_document_render import (
    test_reader_markdown_epub_share_format_without_author_material as _reader_export,
)
from tests.test_rich_document_rewrite_confirmation import (
    test_format_confirmation_cannot_bypass_unmapped_section_anchor as _prose_format_anchor,
)
from tests.test_rich_document_rewrite_confirmation import (
    test_legacy_chapter_format_confirmation_requires_a_boolean as _prose_format_boolean,
)
from tests.test_rich_document_rewrite_confirmation import (
    test_legacy_chapter_format_confirmation_requires_bound_preview_and_versions as _prose_format_legacy,
)
from tests.test_rich_document_rewrite_confirmation import (
    test_scene_rewrite_confirmation_rejects_concurrent_document_change as _prose_format_concurrent,
)
from tests.test_rich_document_rewrite_confirmation import (
    test_scene_rewrite_format_loss_confirmation_is_atomic_and_preserves_safe_content as _prose_format_review,
)
from tests.test_rich_document_section_ai import (
    test_closed_section_only_confirmed_selection_leaves_for_one_operation as _section_selection,
)
from tests.test_rich_document_section_ai import (
    test_closed_section_rejects_stale_selection_and_revalidates_before_apply as _section_stale,
)
from tests.test_rich_document_section_ai import (
    test_confirmation_uuid_concurrent_replay_cannot_send_twice as _section_replay,
)
from tests.test_rich_document_section_ai import (
    test_grant_rechecked_after_preparation_before_first_model_send as _section_visibility,
)
from tests.test_rich_document_section_ai import (
    test_only_true_boolean_is_explicit_confirmation as _section_confirmation_type,
)
from tests.test_rich_document_section_ai import (
    test_real_redirect_does_not_send_confirmed_section_selection_twice as _section_redirect,
)
from tests.test_rich_document_section_ai import (
    test_section_rewrite_format_loss_requires_confirmation_and_preserves_unaffected as _section_format_review,
)
from tests.test_rich_document_section_ai import (
    test_section_send_rechecks_authenticated_actor_before_first_http_send as _section_auth,
)
from tests.test_rich_document_timeline import (
    test_chinese_title_change_invalidates_snapshot_even_without_chapter_revision as _timeline_title,
)
from tests.test_rich_document_timeline import (
    test_later_page_rejects_stale_or_missing_whole_book_snapshot as _timeline_pages,
)
from tests.test_rich_document_timeline import (
    test_retired_scene_markers_remain_findable_and_reactivate_after_restore as _retired_timeline,
)
from tests.test_rich_document_timeline import (
    test_same_anchor_sections_precede_following_prose_and_keep_stale_order as _timeline_order,
)
from tests.test_rich_document_timeline import (
    test_timeline_rejects_mixed_page_and_total_after_another_chapter_changes as _timeline_race,
)
from tests.test_rich_document_transfer import (
    test_rich_backup_rejects_invalid_unverified_draft_excerpt as _invalid_draft_backup,
)
from tests.test_scene_transfer import (
    test_marker_split_lineage_survives_backup_and_rejects_corrupt_origin as _marker_lineage_backup,
)
from tests.test_scene_transfer import (
    test_section_split_lineage_survives_backup_and_rejects_corrupt_origin as _section_lineage_backup,
)
from tests.test_scene_transfer import (
    test_split_history_restore_backup_keeps_retired_scene_material as _retired_scene_history_backup,
)
from tests.test_scenes import setup_scene
from tests.test_scenes_postgresql import (
    _test_url,
    postgres_scenes_app,  # noqa: F401
)


@pytest.fixture
def rich_pg_app(postgres_scenes_app):  # noqa: F811
    return postgres_scenes_app


@pytest.fixture
async def pg_client(rich_pg_app):
    app = rich_pg_app
    await app.state.task_engine.start()
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client
    finally:
        await app.state.task_engine.stop()


@pytest.fixture
async def pg_make_client(rich_pg_app):
    clients = []

    def make_client():
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=rich_pg_app), base_url="http://testserver")
        clients.append(client)
        return client

    yield make_client
    for client in clients:
        await client.aclose()


async def test_pg_document_writers_compete_without_partial_structure(rich_pg_app, pg_client):
    app = rich_pg_app
    _project, chapter, url, scene = await setup_scene(pg_client, "甲\r\n乙🙂")
    endpoint = f"{url}/scenes/{scene['id']}/document"
    initial = (await pg_client.get(endpoint)).json()
    first = save_payload(initial)
    add_section(first, enabled=True)
    second = deepcopy(first)
    second_key = str(uuid4())
    second["document"]["sections"][0].update(key=second_key, title="另一备选", content="另一份资料")
    second["add_section_keys"] = [second_key]

    results = await asyncio.gather(*(pg_client.put(endpoint, json=body) for body in (first, second)))
    assert sorted(result.status_code for result in results) == [200, 409]
    success = next(result for result in results if result.status_code == 200).json()
    conflict = next(result for result in results if result.status_code == 409).json()
    assert conflict["code"] == "stale_scene_revision"
    assert success["content_revision"] == 1
    async with app.state.sessionmaker() as db:
        chapter_row = await db.get(Chapter, chapter["id"])
        sections = list(await db.scalars(select(SceneSection).where(SceneSection.scene_id == scene["id"])))
        assert chapter_row is not None and chapter_row.content == "甲\r\n乙🙂"
        assert chapter_row.content_revision == 1
        assert len(sections) == 1 and sections[0].logical_key == success["document"]["sections"][0]["key"]


async def test_pg_fingerprint_streams_multiscene_unicode(rich_pg_app, pg_client):
    await _multi_scene_fingerprint(rich_pg_app, pg_client)


async def test_pg_reader_and_exports_hide_author_material(pg_client, pg_make_client):
    await _reader_export(pg_client, pg_make_client)


async def test_pg_reader_projects_formatted_scene_across_crlf_seam(pg_client):
    await _reader_crlf_seam(pg_client)


async def test_pg_analysis_freshness_tracks_only_ai_visible_material(rich_pg_app, pg_client, fake_llm):
    await _analysis_freshness(rich_pg_app, pg_client, fake_llm)


async def test_pg_review_acceptance_is_atomic_and_idempotent(rich_pg_app, pg_client, fake_llm):
    await _review_accept(rich_pg_app, pg_client, fake_llm)


async def test_pg_review_chunk_corruption_never_applies(rich_pg_app, pg_client, fake_llm, monkeypatch):
    await _review_chunks(rich_pg_app, pg_client, fake_llm, monkeypatch)


async def test_pg_batch_review_resumes_without_regeneration(rich_pg_app, pg_client, fake_llm):
    await _batch_review(rich_pg_app, pg_client, fake_llm)


async def test_pg_batch_review_analysis_commit_survives_recovery(rich_pg_app, pg_client, fake_llm, monkeypatch):
    rich_pg_app.state.settings.auto_analyze_enabled = True
    await _batch_analysis_commit(rich_pg_app, pg_client, fake_llm, monkeypatch)


async def test_pg_batch_review_analysis_interrupt_does_not_resend(rich_pg_app, pg_client, fake_llm):
    rich_pg_app.state.settings.auto_analyze_enabled = True
    await _batch_analysis_interrupt(rich_pg_app, pg_client, fake_llm)


async def test_pg_review_recovers_without_model_replay(rich_pg_app, pg_client, fake_llm):
    await _review_recover(rich_pg_app, pg_client, fake_llm)


async def test_pg_review_remains_readable_but_rejects_stale_source(rich_pg_app, pg_client, fake_llm):
    await _review_stale(rich_pg_app, pg_client, fake_llm)


@pytest.mark.parametrize("action", ["cancel", "finish"])
async def test_pg_review_termination_does_not_adopt(rich_pg_app, pg_client, fake_llm, action):
    await _review_terminate(rich_pg_app, pg_client, fake_llm, action)


async def test_pg_old_review_cancellation_keeps_new_generation_locked(rich_pg_app, pg_client, fake_llm):
    await _review_old_cancel(rich_pg_app, pg_client, fake_llm)


async def test_pg_review_cancel_accept_race_never_partially_writes(rich_pg_app, pg_client, fake_llm):
    await _review_cancel_race(rich_pg_app, pg_client, fake_llm)


@pytest.mark.parametrize("change", ["disabled", "token_version"])
async def test_pg_review_revalidates_actor_before_accept(rich_pg_app, pg_client, fake_llm, monkeypatch, change):
    await _review_auth(rich_pg_app, pg_client, fake_llm, monkeypatch, change)


async def test_pg_rich_history_restore_is_reversible(rich_pg_app, pg_client):
    await _history_restore(rich_pg_app, pg_client)


async def test_pg_long_marker_growth_and_truncated_history_roundtrip(pg_client):
    await _long_marker(pg_client)


async def test_pg_marker_draft_excerpt_survives_history_and_backup(pg_client):
    await _marker_draft_excerpt(pg_client)


async def test_pg_legacy_backup_defaults_missing_draft_excerpt(pg_client):
    await _marker_legacy_excerpt(pg_client)


async def test_pg_section_marker_draft_excerpt_survives_backup(pg_client):
    await _section_marker_draft_excerpt(pg_client)


@pytest.mark.parametrize("changes", [
    {"draft_excerpt": 123, "draft_last_start": 0},
    {"draft_excerpt": "片段", "draft_last_start": "无效"},
    {"draft_excerpt": None, "draft_excerpt_truncated": True},
    {"draft_excerpt": "x" * 5001, "draft_last_start": 0},
])
async def test_pg_backup_rejects_invalid_draft_excerpt(rich_pg_app, pg_client, changes):
    await _invalid_draft_backup(rich_pg_app, pg_client, changes)


async def test_pg_relink_retired_marker_preserves_history(pg_client):
    await _marker_relink_retired(pg_client)


async def test_pg_delete_retired_marker_remains_recoverable(pg_client):
    await _marker_delete_retired(pg_client)


async def test_pg_explicit_marker_restore_requires_same_scene_and_cas(rich_pg_app, pg_client):
    await _marker_explicit_restore(rich_pg_app, pg_client)


async def test_pg_explicit_section_restore_preserves_marker_and_identity(rich_pg_app, pg_client):
    await _section_explicit_restore(rich_pg_app, pg_client)


async def test_pg_section_restore_rejects_live_lineage(rich_pg_app, pg_client):
    await _section_restore_lineage(rich_pg_app, pg_client)


async def test_pg_marker_restore_checks_lineage_not_unrelated_uuid(rich_pg_app, pg_client):
    await _marker_restore_lineage(rich_pg_app, pg_client)


async def test_pg_moved_marker_source_cannot_be_resurrected_by_history(pg_client, pg_make_client):
    await _marker_no_duplicate_relink(pg_client, pg_make_client)


async def test_pg_cross_chapter_relink_checks_versions_and_crlf(pg_client):
    await _marker_cross_chapter(pg_client)


async def test_pg_marker_delete_scrubs_backup_but_history_recovers(pg_client):
    await _marker_delete_scrub(pg_client)


async def test_pg_marker_relink_scrubs_backup_but_history_recovers(pg_client):
    await _marker_relink_scrub(pg_client)


async def test_pg_marker_history_scrubs_backup_but_history_recovers(pg_client):
    await _marker_history_scrub(pg_client)


async def test_pg_marker_split_merge_scrubs_tombstones(rich_pg_app, pg_client):
    await _marker_split_merge_scrub(rich_pg_app, pg_client)


async def test_pg_marker_split_merge_keeps_active_metadata(pg_client):
    await _marker_split_merge_metadata(pg_client)


@pytest.mark.parametrize("siblings", [False, True])
async def test_pg_cross_chapter_section_history_blocks_lineage(rich_pg_app, pg_client, siblings):
    await _section_history_lineage(rich_pg_app, pg_client, siblings)


async def test_pg_cross_chapter_history_blocks_new_double_live(pg_client):
    await _marker_restore_cross_chapter(pg_client)


async def test_pg_cross_chapter_history_allows_unrelated_uuid(pg_client):
    await _marker_unrelated_history(pg_client)


async def test_pg_cross_chapter_history_and_relink_serialize(pg_client):
    await _marker_restore_race(pg_client)


async def test_pg_legacy_cross_chapter_double_live_backup_roundtrips(rich_pg_app, pg_client):
    await _marker_legacy_double_live(rich_pg_app, pg_client)


@pytest.mark.parametrize("kind", ["section", "marker"])
async def test_pg_retired_scene_history_backup_roundtrips(rich_pg_app, pg_client, kind):
    await _retired_scene_history_backup(rich_pg_app, pg_client, kind)


async def test_pg_same_scene_stale_relink_and_active_delete(pg_client):
    await _marker_same_scene(pg_client)


@pytest.mark.parametrize("kind", ["format", "section", "active_marker"])
async def test_pg_left_rich_content_split_merge(rich_pg_app, pg_client, kind):
    await _left_split_merge(rich_pg_app, pg_client, kind)


@pytest.mark.parametrize("kind", ["format", "active_marker"])
async def test_pg_right_rich_content_split_merge_history(rich_pg_app, pg_client, kind):
    await _right_split_merge(rich_pg_app, pg_client, kind)


async def test_pg_right_section_split_merge_keeps_marker_and_visibility(rich_pg_app, pg_client):
    await _right_section_split_merge(rich_pg_app, pg_client)


async def test_pg_merge_rejects_unrelated_marker_identity(rich_pg_app, pg_client):
    await _unrelated_merge(rich_pg_app, pg_client)


async def test_pg_marker_lineage_backup_and_invalid_reference(rich_pg_app, pg_client):
    await _marker_lineage_backup(rich_pg_app, pg_client)


async def test_pg_section_lineage_backup_and_invalid_reference(rich_pg_app, pg_client):
    await _section_lineage_backup(rich_pg_app, pg_client)


async def test_pg_visibility_change_waits_for_actual_send(rich_pg_app, pg_client, monkeypatch):
    await _send_visibility(rich_pg_app, pg_client, monkeypatch)


async def test_pg_redirect_never_replays_revoked_payload(rich_pg_app, pg_client, monkeypatch):
    await _redirect_visibility(rich_pg_app, pg_client, monkeypatch)


@pytest.mark.parametrize("operation", ["split", "merge"])
async def test_pg_moving_visible_section_respects_send_lease(rich_pg_app, pg_client, monkeypatch, operation):
    await _section_move_visibility(rich_pg_app, pg_client, monkeypatch, operation)


async def test_pg_explicit_selection_only_sends_confirmed_text(pg_client, fake_llm):
    await _section_selection(pg_client, fake_llm)


async def test_pg_section_rewrite_requires_format_loss_confirmation(pg_client, fake_llm):
    await _section_format_review(pg_client, fake_llm)


async def test_pg_prose_rewrite_requires_format_loss_confirmation(pg_client, fake_llm):
    await _prose_format_review(pg_client, fake_llm)


async def test_pg_prose_confirmation_cannot_bypass_section_anchor(pg_client, fake_llm):
    await _prose_format_anchor(pg_client, fake_llm)


async def test_pg_prose_confirmation_rejects_concurrent_change(pg_client, fake_llm):
    await _prose_format_concurrent(pg_client, fake_llm)


async def test_pg_legacy_prose_confirmation_requires_bound_preview(pg_client, fake_llm):
    await _prose_format_legacy(pg_client, fake_llm)


@pytest.mark.parametrize("invalid", [1, "true"])
async def test_pg_legacy_prose_confirmation_requires_boolean(pg_client, fake_llm, invalid):
    await _prose_format_boolean(pg_client, fake_llm, invalid)


@pytest.mark.parametrize("confirmed", [False, 1, "true"])
async def test_pg_confirmation_requires_real_boolean(pg_client, fake_llm, confirmed):
    await _section_confirmation_type(pg_client, fake_llm, confirmed)


async def test_pg_section_selection_revalidates_after_edit(pg_client, fake_llm):
    await _section_stale(pg_client, fake_llm)


async def test_pg_confirmation_uuid_race_sends_at_most_once(pg_client, fake_llm):
    await _section_replay(pg_client, fake_llm)


async def test_pg_explicit_selection_redirect_sends_at_most_once(rich_pg_app, pg_client):
    await _section_redirect(rich_pg_app, pg_client)


async def test_pg_explicit_selection_rechecks_grant_before_send(rich_pg_app, pg_client, fake_llm):
    await _section_visibility(rich_pg_app, pg_client, fake_llm)


@pytest.mark.parametrize("change", ["disabled", "token_version"])
async def test_pg_explicit_selection_rechecks_actor_before_send(rich_pg_app, pg_client, fake_llm, monkeypatch, change):
    await _section_auth(rich_pg_app, pg_client, fake_llm, monkeypatch, change)


async def test_pg_timeline_orders_same_anchor_sections_before_prose(pg_client):
    await _timeline_order(pg_client)


async def test_pg_retired_scene_marker_remains_recoverable(pg_client):
    await _retired_timeline(pg_client)


async def test_pg_timeline_detects_concurrent_change_in_other_chapter(pg_client, monkeypatch):
    await _timeline_race(pg_client, monkeypatch)


async def test_pg_timeline_rejects_stale_page_snapshot(pg_client):
    await _timeline_pages(pg_client)


async def test_pg_chinese_title_change_invalidates_page_snapshot(rich_pg_app, pg_client):
    await _timeline_title(rich_pg_app, pg_client)


def test_pg_real_0039_upgrade_preserves_data_and_rejects_lossy_downgrade(monkeypatch):
    url = _test_url()
    schema = f"rich_migrate_{uuid4().hex}"

    async def schema_statement(statement):
        engine = create_async_engine(url)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    async def statements(items):
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        try:
            async with engine.begin() as connection:
                values = []
                for statement, parameters in items:
                    result = await connection.execute(text(statement), parameters)
                    values.append(result.scalar() if result.returns_rows else None)
                return values
        finally:
            await engine.dispose()

    asyncio.run(schema_statement(f'CREATE SCHEMA "{schema}"'))
    original_factory = sa_async.async_engine_from_config

    def isolated_factory(*args, **kwargs):
        return original_factory(*args, **kwargs, connect_args={"server_settings": {"search_path": schema}})

    monkeypatch.setattr(sa_async, "async_engine_from_config", isolated_factory)
    backend = Path(__file__).resolve().parents[1]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["configure_logger"] = False
    original = " 甲😀\r\n\n乙e\u0301\r尾 "
    try:
        command.upgrade(config, "0039")
        asyncio.run(statements([
            ("INSERT INTO users(id,username,password_hash) VALUES(1,'legacy','hash')", {}),
            ("INSERT INTO projects(id,user_id,title,genre,premise,style,status) "
             "VALUES(1,1,'旧书','','','','planning')", {}),
            ("INSERT INTO chapters(id,project_id,chapter_no,title,content,status,word_count,content_revision) "
             "VALUES(1,1,1,'旧章',:body,'draft',8,7)", {"body": original}),
            ("INSERT INTO scenes(id,chapter_id,content,sort,active) "
             "VALUES(1,1,:body,0,true),(2,1,'其他场景',1,false)", {"body": original}),
        ]))
        command.upgrade(config, "0040")
        assert asyncio.run(statements([
            ("SELECT content FROM scenes WHERE id=1", {}),
            ("SELECT content_revision FROM chapters WHERE id=1", {}),
            ("SELECT count(*) FROM scene_documents", {}),
        ])) == [original, 7, 0]
        command.downgrade(config, "0039")
        command.upgrade(config, "0040")
        asyncio.run(statements([
            ("INSERT INTO scene_sections(id,scene_id,logical_key,anchor,sort,title,content) "
             "VALUES(10,1,'645d44c0-cc16-4949-973b-542022da8421',1,0,'备选','保留')", {}),
        ]))
        with pytest.raises(IntegrityError):
            asyncio.run(statements([
                ("INSERT INTO scene_markers(scene_id,logical_key,section_id,status,color,start_offset,end_offset) "
                 "VALUES(2,'d4baf738-d1ca-4d51-a9cc-22fe47213105',10,'active','yellow',0,1)", {}),
            ]))
        with pytest.raises(RuntimeError, match="不能无损降级"):
            command.downgrade(config, "0039")
        assert asyncio.run(statements([
            ("SELECT content FROM scene_sections WHERE id=10", {}),
            ("SELECT version_num FROM alembic_version", {}),
        ])) == ["保留", "0040"]
    finally:
        asyncio.run(schema_statement(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
