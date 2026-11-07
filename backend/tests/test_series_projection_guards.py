"""采用后的本书条目：所有旧入口都不能改写共享基础。"""

from dataclasses import dataclass

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError
from app.models import (
    ChapterPlan,
    Character,
    OrganizationMember,
    Project,
    Relationship,
    Series,
    SeriesBook,
    SeriesEntry,
    SeriesEntryUse,
    SettingTimeline,
)
from app.schemas.progression import BaselineUpdate
from app.services import (
    outline_snapshot_service,
    project_lifecycle,
    setting_progression_service,
    story_service,
    wizard_service,
)
from app.services.agent import effects
from app.services.book_import import reverse
from tests.helpers import create_project, register


@dataclass
class Book:
    pid: int
    owner_id: int
    character_id: int
    world_id: int
    local_id: int

    @property
    def url(self):
        return f"/api/v1/projects/{self.pid}"


@pytest.fixture
async def book(client, app):
    user = await register(client)
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        character = await story_service.new_character(db, project["id"], {
            "name": "林青", "aliases": ["阿青"], "profile": "共同简介", "ai_mode": "detected",
        })
        world = await story_service.new_world(db, project["id"], {
            "title": "白塔", "category": "place", "content": "共同说明", "ai_mode": "detected",
        })
        local = await story_service.new_character(db, project["id"], {"name": "本书角色"})
        await db.commit()
        return Book(project["id"], user["id"], character.id, world.id, local.id)


async def _bind(app, book):
    async with app.state.sessionmaker() as db:
        series = Series(owner_id=book.owner_id, name="系列")
        db.add(series)
        await db.flush()
        db.add(SeriesBook(project_id=book.pid, series_id=series.id, owner_id=book.owner_id))
        await db.flush()
        for kind, name, identity in (("character", "林青", book.character_id), ("world", "白塔", book.world_id)):
            entry = SeriesEntry(series_id=series.id, kind=kind, display_name=name, data={}, ai_settings={})
            db.add(entry)
            await db.flush()
            column = "character_id" if kind == "character" else "world_setting_id"
            db.add(SeriesEntryUse(
                project_id=book.pid, series_id=series.id, series_entry_id=entry.id, kind=kind, **{column: identity},
            ))
        await db.commit()


async def _character_timeline(db, book):
    return await db.scalar(select(SettingTimeline).where(
        SettingTimeline.project_id == book.pid,
        SettingTimeline.subject_kind == "character",
        SettingTimeline.character_id == book.character_id,
    ))


def _assert_managed(response):
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "series_setting_managed"


async def test_projection_protects_shared_fields_but_not_book_state_or_links(client, app, book):
    await _bind(app, book)
    character = f"{book.url}/characters/{book.character_id}"
    world = f"{book.url}/world/{book.world_id}"
    for path, values in (
        (character, {"name": "改共享名"}), (character, {"profile": "改共享简介"}),
        (world, {"aliases": ["改共享别名"]}), (world, {"content": "改共享说明"}),
    ):
        _assert_managed(await client.patch(path, json=values))
    _assert_managed(await client.delete(character))
    _assert_managed(await client.delete(world))

    async with app.state.sessionmaker() as db:
        timeline = await _character_timeline(db, book)
        timeline_url = f"{book.url}/setting-timelines/{timeline.id}"
        revision = timeline.revision
    _assert_managed(await client.patch(f"{timeline_url}/baseline", json={
        "expected_revision": revision, "changes": {"profile": "改共享基线"},
    }))
    saved = await client.patch(f"{timeline_url}/baseline", json={
        "expected_revision": revision, "changes": {"current_state": "本书受伤"},
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["baseline"]["profile"] == "共同简介"
    assert (await client.get(character)).json()["current_state"] == "本书受伤"

    ai_url = f"{book.url}/setting-entries/character/{book.character_id}/ai-settings"
    settings = (await client.get(ai_url)).json()
    expected = settings["ai_visibility_revision"]
    _assert_managed(await client.patch(ai_url, json={
        "expected_ai_visibility_revision": expected, "ai_mode": "never",
    }))
    linked = await client.patch(ai_url, json={
        "expected_ai_visibility_revision": expected, "links": [{"kind": "character", "id": book.local_id}],
    })
    assert linked.status_code == 200, linked.text
    assert linked.json()["ai_mode"] == "detected"
    assert linked.json()["links"][0]["reference"]["id"] == book.local_id


async def test_pre_series_agent_effect_cannot_restore_an_older_base(app, book):
    async with app.state.sessionmaker() as db:
        before = await effects.snapshot(db, book.pid)
        timeline = await _character_timeline(db, book)
        timeline.baseline = {**timeline.baseline, "profile": "新简介"}
        (await db.get(Character, book.character_id)).profile = "新简介"
        changes = effects.diff(before, await effects.snapshot(db, book.pid))
        await db.commit()
    await _bind(app, book)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError) as caught:
            await effects.undo(db, book.pid, changes)
        assert caught.value.extra["code"] == "undo_conflict"
        assert "共享基础" in str(caught.value.extra["conflicts"])
        assert (await _character_timeline(db, book)).baseline["profile"] == "新简介"


async def test_projection_agent_undo_can_restore_book_event(client, app, book):
    await _bind(app, book)
    async with app.state.sessionmaker() as db:
        timeline = await _character_timeline(db, book)
        tid, revision = timeline.id, timeline.revision
        before = await effects.snapshot(db, book.pid)
    saved = await client.post(f"{book.url}/setting-timelines/{tid}/progressions", json={
        "expected_revision": revision,
        "field_key": "profile",
        "operation": "replace",
        "value": "单书变化",
        "anchor_kind": "planned",
        "chapter_no": 1,
        "boundary": "end",
    })
    assert saved.status_code == 201, saved.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(Character, book.character_id)).profile == "单书变化"
        changes = effects.diff(before, await effects.snapshot(db, book.pid))
        await effects.undo(db, book.pid, changes)
        await db.commit()
        assert (await db.get(Character, book.character_id)).profile == "共同简介"
        assert (await _character_timeline(db, book)).baseline["profile"] == "共同简介"


async def test_book_baseline_write_merges_latest_shared_keys_not_orm_cache(app, book):
    await _bind(app, book)
    async with app.state.sessionmaker() as reader, app.state.sessionmaker() as writer:
        stale = await _character_timeline(reader, book)
        await reader.commit()
        await project_lifecycle.lock_active(writer, book.pid)
        latest = await _character_timeline(writer, book)
        await setting_progression_service._apply_series_baseline(writer, latest, {"profile": "新共享简介"})
        revision = latest.revision
        await writer.commit()
        assert stale.baseline["profile"] == "共同简介" and stale.revision < revision
        await project_lifecycle.lock_active(reader, book.pid)
        await setting_progression_service.apply_baseline(
            reader, stale, BaselineUpdate(expected_revision=revision, changes={"current_state": "本书状态"}),
        )
        await reader.commit()
        assert stale.baseline["profile"] == "新共享简介"
        assert stale.baseline["current_state"] == "本书状态"


async def test_wizard_queued_before_binding_cannot_replace_shared_projection(client, app, book):
    await _bind(app, book)
    _assert_managed(await client.post(f"{book.url}/wizard/characters", json={}))
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError) as caught:
            await wizard_service.save_wizard_result(db, book.pid, "characters", '[{"name": "覆盖"}]')
        assert caught.value.extra["code"] == "series_setting_managed"
        assert await db.get(Character, book.character_id) is not None


async def test_card_rename_can_import_but_skip_does_not_attach_bound_character(client, app, book):
    await _bind(app, book)
    card = {
        "format": "ainovel.cards",
        "version": 1,
        "characters": [{"name": "林青"}, {"name": "本书角色"}],
        "relationships": [{"a_name": "林青", "b_name": "本书角色", "role_a": "朋友", "role_b": "朋友"}],
        "organizations": [{"name": "宗门", "members": [{"character_name": "林青"}]}],
    }
    preview = await client.post(f"{book.url}/cards/import/preview", json={"data": card})
    assert preview.status_code == 200, preview.text
    assert preview.json()["relationships"][0]["status"] == "unresolved"
    skipped = await client.post(f"{book.url}/cards/import", json={"data": card})
    assert skipped.status_code == 200, skipped.text
    assert skipped.json()["characters"][0]["id"] is None
    assert skipped.json()["relationships_created"] == skipped.json()["members_created"] == 0
    renamed = await client.post(f"{book.url}/cards/import", json={
        "data": card, "resolutions": {"characters": {"林青": "rename"}},
    })
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["characters"][0]["id"] != book.character_id
    assert renamed.json()["relationships_created"] == renamed.json()["members_created"] == 1


async def test_reverse_import_does_not_attach_bound_alias_but_can_attach_local(app, book):
    await _bind(app, book)
    async with app.state.sessionmaker() as db:
        project: Project = await project_lifecycle.lock_active(db, book.pid)
        await reverse.apply_final(db, project, {
            "characters": [{"name": "阿青"}, {"name": "后续角色"}],
            "organizations": [{"name": "宗门", "members": [{"name": "阿青"}, {"name": "本书角色"}]}],
            "relations": [
                {"a": "阿青", "b": "本书角色", "role_a": "朋友"},
                {"a": "后续角色", "b": "本书角色", "role_a": "朋友"},
            ],
        }, reverse.new_checkpoint([]))
        await db.commit()
        assert list(await db.scalars(select(OrganizationMember.character_id))) == [book.local_id]
        rel = (await db.scalars(select(Relationship))).one()
        assert book.local_id in (rel.a_id, rel.b_id) and book.character_id not in (rel.a_id, rel.b_id)


async def test_outline_import_does_not_guess_series_identity(client, app, book):
    await _bind(app, book)
    data = {
        "format": "ainovel.outline", "version": 1,
        "volumes": [{
            "volume_no": 1, "outlines": [{
                "chapter_no": 1, "title": "新章",
                "plans": [{"title": "场景", "characters": ["阿青", "本书角色"]}],
            }],
        }],
    }
    preview = await client.post(f"{book.url}/outlines/import/preview", json={"data": data})
    assert preview.status_code == 200, preview.text
    assert preview.json()["unresolved_characters"] == ["阿青"]
    imported = await client.post(f"{book.url}/outlines/import", json={"data": data, "mode": "append"})
    assert imported.status_code == 200, imported.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(ChapterPlan.character_ids)) == [book.local_id]


async def test_snapshot_rechecks_local_ids_and_keeps_existing_series_reference(client, app, book):
    await _bind(app, book)
    outline = await client.post(f"{book.url}/outlines", json={"title": "新章"})
    assert outline.status_code == 201, outline.text
    saved = await client.put(f"{book.url}/outlines/{outline.json()['id']}/plans", json={
        "plans": [{"title": "场景", "character_ids": [book.character_id, book.local_id]}],
    })
    assert saved.status_code == 200, saved.text
    async with app.state.sessionmaker() as db:
        snapshot = await outline_snapshot_service.create_manual(db, book.owner_id, book.pid, "旧快照")
    deleted = await client.delete(f"{book.url}/characters/{book.local_id}")
    assert deleted.status_code == 204, deleted.text
    async with app.state.sessionmaker() as db:
        restored = await outline_snapshot_service.restore_snapshot(db, book.owner_id, book.pid, snapshot.id)
        assert any("不再属于本书" in warning for warning in restored.warnings)
        assert await db.scalar(select(ChapterPlan.character_ids)) == [book.character_id]
