"""历史场景规划必须在投影及恢复前确认引用属于当前作品。"""

from copy import deepcopy

import pytest
from sqlalchemy import func, select

from app.core.errors import ConflictError
from app.models import (
    Career,
    Chapter,
    ChapterVersion,
    Character,
    Organization,
    SettingTimeline,
    WorldSetting,
)
from app.models.planning import PlanningTag, PlanningUnit, PlanningUnitCharacter
from app.models.scenes import Scene
from app.schemas.chapter_version import VersionRestore
from app.schemas.planning import ScenePlanningRaw
from app.services.chapter_version_service import restore_version
from tests.helpers import create_project
from tests.test_collaboration_content_access import _content_setup


async def _seed_snapshot(app, client, make_client, kind, *, foreign=True):
    (owner, _, _, project, chapter, scene, _, url, viewer, editor, _) = await _content_setup(client, make_client)
    other = await create_project(client)
    async with app.state.sessionmaker() as db:
        unit = PlanningUnit(project_id=project['id'], scene_id=scene['id'], source_level='scene')
        db.add(unit)
        target_project = other['id'] if foreign else project['id']
        raw = ScenePlanningRaw().model_dump(mode='json')
        if kind in {'pov', 'character'}:
            target = Character(project_id=target_project, name='隐藏角色')
        elif kind == 'tag':
            target = PlanningTag(project_id=target_project, name='隐藏标签')
        elif kind == 'world':
            target = WorldSetting(project_id=target_project, title='隐藏世界')
        elif kind == 'organization':
            target = Organization(project_id=target_project, name='隐藏组织')
        elif kind == 'career':
            target = Career(project_id=target_project, name='隐藏职业')
        elif kind == 'relationship':
            a, b = Character(project_id=target_project, name='甲'), Character(project_id=target_project, name='乙')
            db.add_all([a, b])
            await db.flush()
            target = SettingTimeline(project_id=target_project, subject_kind='relationship',
                                     character_id=a.id, related_character_id=b.id)
        elif kind == 'unit':
            target = PlanningUnit(project_id=target_project, source_level='scene')
        elif kind == 'scene':
            other_chapter = Chapter(project_id=target_project, chapter_no=1, title='隐藏章节')
            db.add(other_chapter)
            await db.flush()
            target = Scene(chapter_id=other_chapter.id, title='隐藏场景', content='私密内容',
                           summary='', sort=0, separator_before='', active=True)
        else:
            raise AssertionError(kind)
        db.add(target)
        await db.flush()
        version = await db.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter['id'])
                                  .order_by(ChapterVersion.seq.desc()).limit(1))
        snapshot = deepcopy(version.scene_snapshot)
        item = snapshot['scenes'][0]
        if kind == 'pov':
            raw['modes']['pov'] = 'override'
            raw['pov_character_id'] = target.id
        elif kind == 'tag':
            raw['modes']['tags'] = 'override'
            raw['tag_ids'] = [target.id]
        elif kind in {'character', 'world', 'organization', 'career', 'relationship'}:
            raw['modes'][kind] = 'override'
            raw['references'] = [{'kind': kind, ('timeline_id' if kind == 'relationship' else 'id'): target.id}]
        elif kind == 'scene':
            item['id'] = target.id
        item['planning'] = {'unit_id': target.id if kind == 'unit' else unit.id, 'binding_epoch': 0, 'raw': raw}
        version.scene_snapshot = snapshot
        await db.commit()
        version_id, target_id = version.id, target.id
    return owner, project, chapter, url, viewer, editor, version_id, target_id, snapshot


@pytest.mark.parametrize('kind', ['pov', 'tag', 'character', 'world', 'organization', 'career',
                                   'relationship', 'unit', 'scene'])
async def test_foreign_history_is_anonymous_and_cannot_be_restored(app, client, make_client, kind):
    owner, project, chapter, url, viewer, editor, version_id, _, snapshot = await _seed_snapshot(
        app, client, make_client, kind,
    )
    endpoint = f'{url}/versions/{version_id}'
    for response in (
        await viewer.get(endpoint),
        await viewer.get(f'{endpoint}/restore-preview', params={'expected_content_revision': 0}),
        await editor.post(f'{endpoint}/restore', json={'expected_content_revision': 0}),
    ):
        assert response.status_code == 409, (kind, response.text)
        assert response.json()['code'] == 'scene_reference_invalid'
        assert response.json() == {'detail': '场景历史关联数据不一致，无法操作', 'code': 'scene_reference_invalid'}
    async with app.state.sessionmaker() as db:
        before = await db.scalar(select(func.count()).select_from(ChapterVersion).where(
            ChapterVersion.chapter_id == chapter['id'],
        ))
        with pytest.raises(ConflictError) as exc:
            await restore_version(db, owner['id'], project['id'], chapter['id'], version_id,
                                  VersionRestore(expected_content_revision=0))
        assert exc.value.extra['code'] == 'scene_reference_invalid'
        await db.commit()  # 内部调用者捕获异常后提交也不能写入部分恢复。
        assert (await db.get(Chapter, chapter['id'])).content_revision == 0
        assert (await db.get(ChapterVersion, version_id)).scene_snapshot == snapshot
        assert await db.scalar(select(func.count()).select_from(ChapterVersion).where(
            ChapterVersion.chapter_id == chapter['id'],
        )) == before
        assert await db.scalar(select(func.count()).select_from(PlanningUnitCharacter)) == 0


async def test_same_project_history_remains_visible_and_restorable(app, client, make_client):
    _, _, chapter, url, viewer, editor, version_id, character_id, snapshot = await _seed_snapshot(
        app, client, make_client, 'character', foreign=False,
    )
    endpoint = f'{url}/versions/{version_id}'
    detail = await viewer.get(endpoint)
    assert detail.status_code == 200, detail.text
    assert detail.json()['scene_snapshot'] == snapshot
    preview = await viewer.get(f'{endpoint}/restore-preview', params={'expected_content_revision': 0})
    assert preview.status_code == 200, preview.text
    restored = await editor.post(f'{endpoint}/restore', json={'expected_content_revision': 0})
    assert restored.status_code == 200, restored.text
    async with app.state.sessionmaker() as db:
        unit_id = snapshot['scenes'][0]['planning']['unit_id']
        assert await db.scalar(select(PlanningUnitCharacter.character_id).where(
            PlanningUnitCharacter.unit_id == unit_id,
        )) == character_id
        assert (await db.get(Chapter, chapter['id'])).content_revision == 1


@pytest.mark.parametrize('kind', ['section', 'marker'])
async def test_foreign_document_history_identity_is_never_exposed_or_restored(app, client, make_client, kind):
    from uuid import uuid4

    from app.models import Project, SceneMarker, SceneSection
    from app.schemas.rich_document import DocumentMarker, DocumentSection, SceneDocument

    owner, project, chapter, url, viewer, editor, version_id, _, snapshot = await _seed_snapshot(
        app, client, make_client, 'character', foreign=False,
    )
    key = uuid4()
    async with app.state.sessionmaker() as db:
        foreign_id = await db.scalar(select(Project.id).where(Project.id != project['id']))
        foreign_chapter = Chapter(project_id=foreign_id, chapter_no=1, title='他书')
        db.add(foreign_chapter)
        await db.flush()
        foreign_scene = Scene(chapter_id=foreign_chapter.id, title='他书场景', content='密文', summary='',
                              sort=0, separator_before='', active=True)
        db.add(foreign_scene)
        await db.flush()
        item = snapshot['scenes'][0]
        if kind == 'section':
            db.add(SceneSection(scene_id=foreign_scene.id, logical_key=str(key), anchor=0, sort=0,
                                title='别书区块', content='私密内容'))
            document = SceneDocument(content=item['content'], sections=[
                DocumentSection(key=key, anchor=0, order=0, title='别书区块', content='私密内容'),
            ])
        else:
            db.add(SceneMarker(scene_id=foreign_scene.id, logical_key=str(key), status='active', color='yellow',
                               start_offset=0, end_offset=1))
            document = SceneDocument(content=item['content'], markers=[
                DocumentMarker(key=key, status='active', color='yellow', start=0, end=1),
            ])
        snapshot['schema_version'] = 2
        item['document'] = document.model_dump(mode='json', exclude={'content'})
        (await db.get(ChapterVersion, version_id)).scene_snapshot = snapshot
        await db.commit()
    endpoint = f'{url}/versions/{version_id}'
    for response in (
        await viewer.get(endpoint),
        await viewer.get(f'{endpoint}/restore-preview', params={'expected_content_revision': 0}),
        await editor.post(f'{endpoint}/restore', json={'expected_content_revision': 0}),
    ):
        assert response.status_code == 409, response.text
        assert response.json() == {'detail': '场景历史关联数据不一致，无法操作', 'code': 'scene_reference_invalid'}
    async with app.state.sessionmaker() as db:
        with pytest.raises(ConflictError):
            await restore_version(db, owner['id'], project['id'], chapter['id'], version_id,
                                  VersionRestore(expected_content_revision=0))
        await db.commit()
        saved = await db.get(Scene, item['id'])
        assert saved.active is True and saved.content == item['content']
        assert (await db.get(Chapter, chapter['id'])).content_revision == 0


@pytest.mark.parametrize('damage', ['unknown_field', 'missing_reference'])
async def test_unverifiable_history_fails_closed_without_exposing_input(app, client, make_client, damage):
    from app.services.chapter_version_snapshot import validate_history_snapshot

    _, project, chapter, url, viewer, _, version_id, _, snapshot = await _seed_snapshot(
        app, client, make_client, 'character', foreign=False,
    )
    raw = snapshot['scenes'][0]['planning']['raw']
    if damage == 'unknown_field':
        raw['private_extra'] = '不应显示的原始内容'
    else:
        raw['references'][0]['id'] = 2_147_483_647
    async with app.state.sessionmaker() as db:
        (await db.get(ChapterVersion, version_id)).scene_snapshot = snapshot
        await db.commit()
        with pytest.raises(ConflictError) as error:
            await validate_history_snapshot(db, project['id'], chapter['id'], snapshot)
        assert error.value.__suppress_context__
    response = await viewer.get(f'{url}/versions/{version_id}')
    assert response.status_code == 409
    assert response.json() == {'detail': '场景历史关联数据不一致，无法操作', 'code': 'scene_reference_invalid'}
    assert '不应显示的原始内容' not in response.text


async def test_history_validates_many_same_project_references_in_batches(app, client, make_client):
    _, project, _, url, viewer, editor, version_id, _, snapshot = await _seed_snapshot(
        app, client, make_client, 'character', foreign=False,
    )
    async with app.state.sessionmaker() as db:
        characters = [Character(project_id=project['id'], name=f'角色-{index}') for index in range(401)]
        db.add_all(characters)
        await db.flush()
        ids = [character.id for character in characters]
        snapshot['scenes'][0]['planning']['raw']['references'] = [{'kind': 'character', 'id': value} for value in ids]
        (await db.get(ChapterVersion, version_id)).scene_snapshot = snapshot
        await db.commit()
    endpoint = f'{url}/versions/{version_id}'
    detail = await viewer.get(endpoint)
    assert detail.status_code == 200 and detail.json()['scene_snapshot'] == snapshot
    restored = await editor.post(f'{endpoint}/restore', json={'expected_content_revision': 0})
    assert restored.status_code == 200, restored.text
    async with app.state.sessionmaker() as db:
        unit_id = snapshot['scenes'][0]['planning']['unit_id']
        saved = set(await db.scalars(select(PlanningUnitCharacter.character_id).where(
            PlanningUnitCharacter.unit_id == unit_id,
        )))
        assert saved == set(ids)
