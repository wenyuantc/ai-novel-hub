"""人工设定预览的协作读取、原服务边界和快照完整性。"""

import pytest
from sqlalchemy import event, update

from app.core.errors import NotFoundError
from app.models import Chapter, Project, SettingTimeline, WorldSetting
from app.models.planning import PlanningUnit, PlanningUnitWorld
from app.services import context_builder, planning_context_service, setting_progression_service
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role
from tests.test_collaboration_content_access import _content_setup


async def _seed(app, client, make_client):
    values = await _content_setup(client, make_client)
    owner, viewer_user, editor_user, project, chapter, scene, base, url, viewer, editor, stranger = values
    other = await create_project(client, title="他书")
    async with app.state.sessionmaker() as db:
        public = WorldSetting(project_id=project['id'], title='本书山门', content='同书受保护设定', ai_mode='always')
        hidden = WorldSetting(project_id=project['id'], title='本书禁用设定',
                              content='绝不应出现的设定', ai_mode='never')
        foreign = WorldSetting(project_id=other['id'], title='他书山门', content='另一书私有设定', ai_mode='always')
        db.add_all([public, hidden, foreign])
        await db.flush()
        chapter_unit = PlanningUnit(project_id=project['id'], chapter_id=chapter['id'], source_level='chapter')
        scene_unit = PlanningUnit(project_id=project['id'], scene_id=scene['id'], source_level='scene',
                                  world_mode='override')
        db.add_all([chapter_unit, scene_unit])
        await db.flush()
        db.add_all([
            PlanningUnitWorld(unit_id=chapter_unit.id, world_id=public.id),
            PlanningUnitWorld(unit_id=scene_unit.id, world_id=public.id),
            SettingTimeline(project_id=project['id'], subject_kind='world', world_setting_id=public.id,
                            baseline={'content': '同书受保护设定'}, baseline_status='legacy_unverified'),
            SettingTimeline(project_id=other['id'], subject_kind='world', world_setting_id=foreign.id,
                            baseline={'content': '另一书私有设定'}, baseline_status='legacy_unverified'),
        ])
        await db.commit()
        ids = {'chapter_unit': chapter_unit.id, 'scene_unit': scene_unit.id, 'foreign_world': foreign.id}
    paths = (f'{url}/setting-context', f"{url}/scenes/{scene['id']}/setting-context")
    return values, other, ids, paths


async def test_setting_context_actor_matrix_scopes_data_and_does_not_send_or_write(
    app, client, make_client, system_admin, fake_llm,
):
    values, other, _, paths = await _seed(app, client, make_client)
    _, viewer_user, editor_user, project, chapter, scene, _, _, viewer, editor, stranger = values
    manager = make_client()
    manager_user = await register(manager, 'setting-context-manager')
    role = await create_role(system_admin, '设定上下文只读', ['projects.read'])
    await assign_roles(system_admin, manager_user['id'], [role['id']])
    other_chapter = await client.post(f"/api/v1/projects/{other['id']}/chapters", json={'title': '他书章'})
    assert other_chapter.status_code == 201, other_chapter.text
    other_id = other_chapter.json()['id']
    other_scene_page = await client.get(f"/api/v1/projects/{other['id']}/chapters/{other_id}/scenes")
    other_scene_id = other_scene_page.json()['items'][0]['id']
    same_book = await client.post(f"/api/v1/projects/{project['id']}/chapters", json={'title': '同书另一章'})
    assert same_book.status_code == 201, same_book.text
    same_id = same_book.json()['id']
    same_scene_page = await client.get(f"/api/v1/projects/{project['id']}/chapters/{same_id}/scenes")
    same_scene_id = same_scene_page.json()['items'][0]['id']
    statements = []

    def collect(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    event.listen(app.state.engine.sync_engine, 'before_cursor_execute', collect)
    try:
        expected = []
        for actor in (client, viewer, editor, manager, system_admin):
            responses = [await actor.get(path) for path in paths]
            assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
            payloads = [response.json() for response in responses]
            if not expected:
                expected = payloads
            assert payloads == expected
            for payload in payloads:
                assert '同书受保护设定' in payload['text']
                assert '绝不应出现的设定' not in payload['text']
                assert '另一书私有设定' not in payload['text']
                assert payload['legacy_unverified_count'] == 1
            assert payloads[0]['position']['chapter_id'] == chapter['id']
            assert payloads[1]['position']['scene_id'] == scene['id']
        for path in paths:
            assert (await stranger.get(path)).status_code == 404
            assert (await make_client().get(path)).status_code == 401
        cross_chapter = f"/api/v1/projects/{project['id']}/chapters/{other_id}/setting-context"
        assert (await viewer.get(cross_chapter)).status_code == 404
        cross_scene = (f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
                       f"/scenes/{other_scene_id}/setting-context")
        assert (await viewer.get(cross_scene)).status_code == 404
        wrong_chapter = (f"/api/v1/projects/{project['id']}/chapters/{chapter['id']}"
                         f"/scenes/{same_scene_id}/setting-context")
        assert (await viewer.get(wrong_chapter)).status_code == 404
    finally:
        event.remove(app.state.engine.sync_engine, 'before_cursor_execute', collect)
    assert not [statement for statement in statements if statement.lstrip().startswith(('insert', 'update', 'delete'))]
    assert not any('retrieval_chunks' in statement or 'kb_chunks' in statement for statement in statements)
    assert not fake_llm.calls and not fake_llm.embed_calls

    # 管理能力与协作身份重叠时，仍能读取，不能因角色优先级改变投影。
    await assign_roles(system_admin, editor_user['id'], [role['id']])
    assert [(await editor.get(path)).json() for path in paths] == expected
    async with app.state.sessionmaker() as db:
        # 未显式 opt-in 的内部服务继续遵守旧 manageable 语义。
        with pytest.raises(NotFoundError):
            await setting_progression_service.count_unverified_timelines(db, viewer_user['id'], project['id'])
        with pytest.raises(NotFoundError):
            await planning_context_service.scene_setting_context(
                db, viewer_user['id'], project['id'], chapter['id'], scene['id'],
            )
        assert await setting_progression_service.count_unverified_timelines(
            db, viewer_user['id'], project['id'], manual=True,
        ) == 1


@pytest.mark.parametrize('granularity', ['chapter', 'scene'])
async def test_setting_context_does_not_expose_corrupt_foreign_planning_references(
    app, client, make_client, granularity,
):
    values, _, ids, paths = await _seed(app, client, make_client)
    viewer = values[8]
    async with app.state.sessionmaker() as db:
        db.add(PlanningUnitWorld(unit_id=ids[f'{granularity}_unit'], world_id=ids['foreign_world']))
        await db.commit()
    response = await viewer.get(paths[0 if granularity == 'chapter' else 1])
    assert response.status_code == (200 if granularity == 'chapter' else 409), response.text
    assert '另一书私有设定' not in response.text
    assert '他书山门' not in response.text
    if response.status_code == 409:
        assert response.json()['code'] == 'scene_reference_invalid'


@pytest.mark.parametrize('granularity', ['chapter', 'scene'])
@pytest.mark.parametrize('revision_field', ['planning_revision', 'ai_visibility_revision', 'content_revision'])
async def test_setting_context_rechecks_snapshot_after_rendering(app, client, make_client, monkeypatch,
                                                        granularity, revision_field):
    values, _, _, paths = await _seed(app, client, make_client)
    viewer = values[8]
    original = context_builder.build_setting_context

    async def moved(db, project, chapter, **kwargs):
        result = await original(db, project, chapter, **kwargs)
        model, identity = (Chapter, chapter.id) if revision_field == 'content_revision' else (Project, project.id)
        field = getattr(model, revision_field)
        await db.execute(update(model).where(model.id == identity).values({revision_field: field + 1}))
        return result

    monkeypatch.setattr(context_builder, 'build_setting_context', moved)
    response = await viewer.get(paths[0 if granularity == 'chapter' else 1])
    assert response.status_code == 409, response.text
    expected = {'planning_revision': 'planning_context_changed', 'ai_visibility_revision': 'ai_visibility_changed',
                'content_revision': 'stale_scene_revision'}[revision_field]
    assert response.json()['code'] == expected
