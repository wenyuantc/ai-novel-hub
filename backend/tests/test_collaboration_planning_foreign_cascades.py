"""大纲删除与替换不得级联改变异常关联的其他作品。"""

import pytest
from sqlalchemy import func, select

from app.models import Chapter, ChapterPlan, Outline, OutlineSnapshot, Project
from app.models.planning import PlanningUnit
from tests.helpers import create_ai_config, create_project, run_task
from tests.test_collaboration_planning_access import _collaborators

CONFLICT = "大纲关联数据不一致，无法执行操作"


@pytest.mark.parametrize("entrypoint", ["delete", "import", "restore", "wizard"])
async def test_outline_replacement_rejects_foreign_references_atomically(
    app, client, make_client, fake_llm, entrypoint,
):
    base, _, editor, _, _ = await _collaborators(client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    other = await create_project(client, title="另一本")
    outline = await client.post(f"{base}/outlines", json={"title": "原大纲", "chapter_no": 1})
    assert outline.status_code == 201
    outline_id = outline.json()["id"]
    plans = await client.put(f"{base}/outlines/{outline_id}/plans", json={"plans": [{"title": "原细纲"}]})
    assert plans.status_code == 200
    plan_id = plans.json()[0]["id"]
    snapshot = await client.post(f"{base}/outline-snapshots", json={"note": "原快照"})
    assert snapshot.status_code == 201
    if entrypoint == "wizard":
        await create_ai_config(client)
    imported = {"format": "ainovel.outline", "version": 1, "volumes": [{"outlines": [{"title": "替换"}]}]}

    for relation in ("foreign_plan", "chapter_outline", "chapter_plan", "foreign_unit"):
        async with app.state.sessionmaker() as db:
            if relation == "foreign_plan":
                foreign_row = ChapterPlan(project_id=other["id"], outline_id=outline_id, seq=5, title="别书细纲")
            elif relation == "foreign_unit":
                foreign_row = PlanningUnit(project_id=other["id"], plan_id=plan_id)
            else:
                foreign_row = Chapter(project_id=other["id"], chapter_no=1, title="别书章节",
                                      outline_id=outline_id if relation == "chapter_outline" else None,
                                      plan_id=plan_id if relation == "chapter_plan" else None)
            db.add(foreign_row)
            await db.flush()
            foreign_id, model = foreign_row.id, type(foreign_row)
            expected_revision = (await db.get(Project, project_id)).planning_revision
            expected_snapshots = await db.scalar(select(func.count()).select_from(OutlineSnapshot).where(
                OutlineSnapshot.project_id == project_id,
            ))
            await db.commit()

        if entrypoint == "delete":
            response = await editor.delete(f"{base}/outlines/{outline_id}")
        elif entrypoint == "import":
            response = await editor.post(f"{base}/outlines/import", json={"mode": "replace", "data": imported})
        elif entrypoint == "restore":
            response = await editor.post(f"{base}/outline-snapshots/{snapshot.json()['id']}/restore")
        else:
            fake_llm.push('[{"title":"替换大纲"}]')
            task, events = await run_task(client, await client.post(f"{base}/wizard/outline"))
            assert events[-1][0] == "error" and task["status"] == "failed"
            assert events[-1][1]["detail"] == CONFLICT
        if entrypoint != "wizard":
            assert response.status_code == 409, response.text
            assert response.json()["detail"] == CONFLICT

        async with app.state.sessionmaker() as db:
            assert (await db.get(Outline, outline_id)).title == "原大纲"
            assert (await db.get(ChapterPlan, plan_id)).title == "原细纲"
            saved = await db.get(model, foreign_id)
            assert saved is not None
            if relation == "foreign_plan" or relation == "chapter_outline":
                assert saved.outline_id == outline_id
            else:
                assert saved.plan_id == plan_id
            assert (await db.get(Project, project_id)).planning_revision == expected_revision
            assert await db.scalar(select(func.count()).select_from(OutlineSnapshot).where(
                OutlineSnapshot.project_id == project_id,
            )) == expected_snapshots
            await db.delete(saved)
            await db.commit()


@pytest.mark.parametrize("relation", ["chapter", "unit"])
async def test_plan_sync_rejects_foreign_references_atomically(app, client, make_client, relation):
    base, _, editor, _, _ = await _collaborators(client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    other = await create_project(client, title="另一本")
    outline = await client.post(f"{base}/outlines", json={"title": "原大纲", "chapter_no": 1})
    assert outline.status_code == 201
    path = f"{base}/outlines/{outline.json()['id']}/plans"
    plans = await client.put(path, json={"plans": [{"title": "原细纲"}]})
    assert plans.status_code == 200
    plan_id = plans.json()[0]["id"]
    async with app.state.sessionmaker() as db:
        foreign = (Chapter(project_id=other["id"], chapter_no=1, title="别书章节", plan_id=plan_id)
                   if relation == "chapter" else PlanningUnit(project_id=other["id"], plan_id=plan_id))
        db.add(foreign)
        await db.flush()
        foreign_id, model = foreign.id, type(foreign)
        revision = (await db.get(Project, project_id)).planning_revision
        await db.commit()
    response = await editor.put(path, json={"plans": []})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "outline_reference_conflict"
    async with app.state.sessionmaker() as db:
        assert (await db.get(ChapterPlan, plan_id)).title == "原细纲"
        assert (await db.get(model, foreign_id)).plan_id == plan_id
        assert (await db.get(Project, project_id)).planning_revision == revision
        assert await db.scalar(select(func.count()).select_from(OutlineSnapshot).where(
            OutlineSnapshot.project_id == project_id,
        )) == 0


async def test_plan_sync_rejects_foreign_references_to_removed_chapter(app, client, make_client):
    base, _, editor, _, _ = await _collaborators(client, make_client)
    project_id = int(base.rsplit("/", 1)[-1])
    other = await create_project(client, title="另一本")
    outline = await client.post(f"{base}/outlines", json={"title": "原大纲", "chapter_no": 1})
    assert outline.status_code == 201
    path = f"{base}/outlines/{outline.json()['id']}/plans"
    plans = await client.put(path, json={"plans": [{"title": "原细纲"}]})
    assert plans.status_code == 200
    built = await client.post(f"{path}/build")
    assert built.status_code == 200, built.text
    chapter_id = built.json()["chapters"][0]["id"]
    async with app.state.sessionmaker() as db:
        foreign = PlanningUnit(project_id=other["id"], chapter_id=chapter_id)
        db.add(foreign)
        await db.flush()
        unit_id = foreign.id
        revision = (await db.get(Project, project_id)).planning_revision
        await db.commit()
    response = await editor.put(path, json={"plans": []})
    assert response.status_code == 409, response.text
    async with app.state.sessionmaker() as db:
        assert (await db.get(ChapterPlan, plans.json()[0]["id"])).chapter_id == chapter_id
        assert (await db.get(PlanningUnit, unit_id)).chapter_id == chapter_id
        assert await db.get(Chapter, chapter_id) is not None
        assert (await db.get(Project, project_id)).planning_revision == revision
        assert await db.scalar(select(func.count()).select_from(OutlineSnapshot).where(
            OutlineSnapshot.project_id == project_id,
        )) == 0
