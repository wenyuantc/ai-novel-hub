from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.models import SerializationEdition, SerializationEntry, SerializationPublication
from tests.test_outline_pro import _outlines, _setup


@pytest.mark.parametrize("remove_via", ["chapter", "plan"])
async def test_serialization_deletion_keeps_only_published_editions(app, client, remove_via):
    project, base = await _setup(client, 1)
    outline = (await _outlines(client, base))[0]
    plans_path = f"{base}/outlines/{outline['id']}/plans"
    plans = (await client.put(plans_path, json={"plans": [{"title": "有历史"}, {"title": "无历史"}]})).json()
    chapters = (await client.post(f"{plans_path}/build")).json()["chapters"]
    async with app.state.sessionmaker() as db:
        entries = [SerializationEntry(project_id=project["id"], chapter_id=chapter["id"], stage="final",
                                      planned_date=date(2026, 11, 18)) for chapter in chapters]
        db.add_all(entries)
        await db.flush()
        published, unpublished, no_history = [
            SerializationEdition(entry_id=entry.id, seq=seq, chapter_no=7, title="发布时的章名",
                                 content=body, fingerprint="a" * 64)
            for entry, seq, body in ((entries[0], 1, "A"), (entries[0], 2, "B"), (entries[1], 1, "C"))
        ]
        db.add_all([published, unpublished, no_history])
        await db.flush()
        db.add_all([SerializationPublication(project_id=project["id"], edition_id=published.id, channel=channel,
                                             published_at=datetime.now(UTC)) for channel in ("起点", "番茄")])
        await db.commit()
        retained_id, removed_id, edition_id = entries[0].id, entries[1].id, published.id

    if remove_via == "plan":
        assert len(plans) == 2
        response = await client.put(plans_path, json={"plans": []})
        assert response.status_code == 200, response.text
    else:
        for chapter in chapters:
            response = await client.delete(f"{base}/chapters/{chapter['id']}")
            assert response.status_code == 204, response.text
    async with app.state.sessionmaker() as db:
        retained = await db.get(SerializationEntry, retained_id)
        assert (retained.project_id, retained.chapter_id, retained.planned_date) == (project["id"], None, None)
        assert await db.get(SerializationEntry, removed_id) is None
        editions = list(await db.scalars(select(SerializationEdition)))
        assert [(e.id, e.chapter_no, e.title, e.content) for e in editions] == [(edition_id, 7, "发布时的章名", "A")]
        assert set(await db.scalars(select(SerializationPublication.channel))) == {"起点", "番茄"}
