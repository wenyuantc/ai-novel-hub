"""在隔离 PostgreSQL schema 验证模板抓取的字段、图片及章纲投影。"""

import httpx
import pytest
from sqlalchemy import event, update

from app.core.errors import BadRequestError
from app.models import ChapterPlan, Outline
from app.models.setting_images import SettingImageResource
from app.schemas.novel_template_snapshot import TemplateSelection
from app.services.novel_template_snapshot import capture
from tests.helpers import create_project, register
from tests.test_novel_template_snapshot import _assert_current_projection_matches_timeline_order
from tests.test_novel_template_snapshot import (
    test_current_projection_matches_existing_resolver_across_multiple_timelines as _assert_multiple_timelines,
)
from tests.test_novel_template_snapshot import (
    test_current_projection_skips_large_overwritten_history as _assert_large_history,
)
from tests.test_novel_template_snapshot import (
    test_current_projection_streams_only_effective_events_and_rejects_empty_append as _assert_streamed_history,
)
from tests.test_novel_template_snapshot import test_plan_json_budget_accepts_normal_escaped_unicode as _assert_plan_utf8
from tests.test_novel_template_snapshot import (
    test_plan_json_budget_is_checked_before_loading_values as _assert_plan_size,
)
from tests.test_series_postgresql import postgres_series_app as postgres_series_app
from tests.test_setting_fields_categories_roundtrip import _seed
from tests.test_setting_thumbnails import image_data


async def test_postgresql_capture_config_scoped_fields_plans_and_image(postgres_series_app):
    app = postgres_series_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        project, index = await _seed(client)
        entries = {kind: entry["reference"]["id"] for kind, entry in index["entries"].items()}
        thumbnail = f"{index['base']}/settings/character/{entries['character']}/thumbnail"
        uploaded = await client.put(
            thumbnail,
            files={"file": ("cover.png", image_data(), "image/png")},
            data={"expected_version": ""},
        )
        assert uploaded.status_code == 200, uploaded.text
        async with app.state.sessionmaker() as db:
            outline = Outline(project_id=project["id"], title="计划")
            db.add(outline)
            await db.flush()
            db.add(
                ChapterPlan(
                    project_id=project["id"],
                    outline_id=outline.id,
                    seq=1,
                    title="细纲",
                    key_events=["重逢"],
                    character_ids=[entries["character"]],
                )
            )
            await db.commit()
            result = await capture(
                db,
                project["id"],
                TemplateSelection(
                    character_ids=[entries["character"]],
                    world_ids=[entries["world"]],
                    organization_ids=[entries["organization"]],
                    career_ids=[entries["career"]],
                    outline_ids=[outline.id],
                ),
            )
        assert result.snapshot is not None
        assert len(result.snapshot.field_definitions) == 4
        assert len(result.snapshot.categories) == 2
        assert any(field.value == "林岚新口令" for field in result.snapshot.characters[0].fields)
        assert result.snapshot.outlines[0].plans[0].character_refs == [result.snapshot.characters[0].ref]
        assert list(result.images) == [result.snapshot.characters[0].ref]
        assert result.counts["plans"] == 1 and result.counts["images"] == 1


async def test_postgresql_image_size_guard_never_reads_invalid_blob(postgres_series_app):
    app = postgres_series_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await register(client)
        project = await create_project(client)
        base = f"/api/v1/projects/{project['id']}"
        response = await client.post(f"{base}/characters", json={"name": "主角"})
        assert response.status_code == 201, response.text
        char_id = response.json()["id"]
        uploaded = await client.put(
            f"{base}/settings/character/{char_id}/thumbnail",
            files={"file": ("cover.png", image_data(), "image/png")},
            data={"expected_version": ""},
        )
        assert uploaded.status_code == 200, uploaded.text
        selection = TemplateSelection(character_ids=[char_id])
        statements = []

        def record(_conn, _cursor, statement, _params, _context, _many):
            statements.append(" ".join(statement.lower().split()))

        event.listen(app.state.engine.sync_engine, "before_cursor_execute", record)
        try:
            async with app.state.sessionmaker() as db:
                assert (await capture(db, project["id"], selection)).snapshot is not None
            binary_queries = [
                s for s in statements if "setting_image_resources.mime_type, setting_image_resources.data" in s
            ]
            assert len(binary_queries) == 1
            assert "octet_length(setting_image_resources.data)" in binary_queries[0]
            assert "setting_image_resources.byte_size =" in binary_queries[0]
            statements.clear()
            async with app.state.sessionmaker() as db:
                await db.execute(
                    update(SettingImageResource)
                    .where(SettingImageResource.id == uploaded.json()["version"])
                    .values(
                        byte_size=1,
                        data=b"x" * (512 * 1024 + 1),
                    )
                )
                await db.commit()
            statements.clear()
            async with app.state.sessionmaker() as db:
                with pytest.raises(BadRequestError) as exc:
                    await capture(db, project["id"], selection)
            assert exc.value.extra["code"] == "invalid_novel_template"
            assert any("octet_length(setting_image_resources.data)" in s for s in statements)
            assert not any("setting_image_resources.mime_type, setting_image_resources.data" in s for s in statements)
        finally:
            event.remove(app.state.engine.sync_engine, "before_cursor_execute", record)


async def test_postgresql_current_projection_matches_existing_resolver(postgres_series_app):
    app = postgres_series_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await _assert_current_projection_matches_timeline_order(app, client)


async def test_postgresql_current_projection_skips_large_overwritten_history(postgres_series_app):
    app = postgres_series_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await _assert_large_history(app, client)


async def test_postgresql_current_projection_multiple_timelines(postgres_series_app):
    app = postgres_series_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await _assert_multiple_timelines(app, client)


async def test_postgresql_current_projection_streams_only_effective_events(postgres_series_app, monkeypatch):
    app = postgres_series_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await _assert_streamed_history(app, client, monkeypatch)


@pytest.mark.parametrize("column", ["key_events", "character_ids"])
async def test_postgresql_plan_json_size_guard(postgres_series_app, monkeypatch, column):
    app = postgres_series_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await _assert_plan_size(app, client, monkeypatch, column)


async def test_postgresql_plan_json_size_guard_accepts_unicode(postgres_series_app, monkeypatch):
    app = postgres_series_app
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        await _assert_plan_utf8(app, client, monkeypatch)
