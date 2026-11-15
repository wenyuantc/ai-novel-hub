"""独立 PostgreSQL 上验证上下文渲染后的版本和成员权限重读。"""

import asyncio
from contextlib import suppress

import httpx
import pytest
from sqlalchemy import update

from app.models import Chapter, Project
from app.services import context_builder
from tests import pg_support
from tests.test_collaboration_access_postgresql import _setup


@pytest.fixture
async def postgres_content_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="内容测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


@pytest.mark.parametrize('granularity', ['chapter', 'scene'])
async def test_postgres_setting_context_rechecks_after_concurrent_changes(
    postgres_content_apps, monkeypatch, granularity,
):
    owner_app, guest_app = postgres_content_apps
    async with (
        httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url='http://testserver') as owner,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url='http://testserver') as guest,
    ):
        project, guest_user, base = await _setup(owner, guest)
        chapter = await owner.post(f'{base}/chapters', json={'title': '第一章', 'content': '正文'})
        assert chapter.status_code == 201, chapter.text
        url = f"{base}/chapters/{chapter.json()['id']}"
        if granularity == 'scene':
            scenes = await owner.get(f'{url}/scenes')
            assert scenes.status_code == 200, scenes.text
            url += f"/scenes/{scenes.json()['items'][0]['id']}"
        url += '/setting-context'
        assert (await guest.get(url)).status_code == 200
        original = context_builder.build_setting_context
        for change in ('planning_revision', 'ai_visibility_revision', 'content_revision', 'revoke'):
            ready, release = asyncio.Event(), asyncio.Event()

            async def gated(*args, _ready=ready, _release=release, **kwargs):
                value = await original(*args, **kwargs)
                _ready.set()
                await _release.wait()
                return value

            monkeypatch.setattr(context_builder, 'build_setting_context', gated)
            pending = asyncio.create_task(guest.get(url))
            try:
                await asyncio.wait_for(ready.wait(), 15)
                if change == 'revoke':
                    response = await owner.delete(f"{base}/collaborators/{guest_user['id']}", params={
                        'expected_revision': 2,
                    })
                    assert response.status_code == 204, response.text
                else:
                    async with owner_app.state.sessionmaker() as db:
                        model, identity = ((Chapter, chapter.json()['id']) if change == 'content_revision'
                                           else (Project, project['id']))
                        field = getattr(model, change)
                        await db.execute(update(model).where(model.id == identity).values({change: field + 1}))
                        await db.commit()
                release.set()
                response = await asyncio.wait_for(pending, 15)
                if change == 'revoke':
                    assert response.status_code == 404, response.text
                else:
                    expected = {'planning_revision': 'planning_context_changed',
                                'ai_visibility_revision': 'ai_visibility_changed',
                                'content_revision': 'stale_scene_revision'}[change]
                    assert response.status_code == 409 and response.json()['code'] == expected, response.text
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                    with suppress(asyncio.CancelledError):
                        await pending
