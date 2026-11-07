"""三个真实 SDK 的真实本地 HTTP 发送点检查场景快照，不只检查 FakeLLM。"""

import pytest

from app.services.ai_dispatch import guard_provider
from app.services.scene_ai_context import SceneContextChanged, read_stamp, require_stamp
from tests.test_ai_visibility_dispatch import PROVIDERS, _collect, _server, _sse
from tests.test_scene_planning import scene_setup


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
async def test_real_sdk_scene_snapshot_gate_stops_changed_content_before_any_http(app, client, kind):
    project, _, chapter, url, scenes = await scene_setup(client, count=1)
    async with app.state.sessionmaker() as db:
        stamp = await read_stamp(db, project["id"], chapter["id"])
    saved = await client.patch(f"{url}/{scenes[0]['id']}", json={"expected_content_revision": 0, "content": "作者改稿"})
    assert saved.status_code == 200
    async with _server(payload=_sse(kind)) as (base, _, headers, body, requests):
        headers.set()
        body.set()
        provider = guard_provider(
            PROVIDERS[kind]("local-test-key", f"{base}/v1/" if kind == "openai" else base, 5),
            stamp.visibility,
            app.state.sessionmaker,
            verify=lambda db: require_stamp(db, stamp, sending=True),
        )
        with pytest.raises(SceneContextChanged) as error:
            await _collect(provider.generate_stream([{"role": "user", "content": "旧场景负载不可发出"}], "test-model"))
        assert error.value.extra == {"code": "scene_context_changed"}
        assert requests == []
