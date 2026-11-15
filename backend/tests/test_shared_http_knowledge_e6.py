"""同步项目上下文使用原协作者授权及作者共享向量空间。

E6-d 转正（原 strict xfail）：``kb_service.knowledge_for_http`` 已实现。按 E6 裁决 1，作者与授权编辑者
统一读写作者旧键 ``provider:model:dim``；原 fixture 把索引行改写成 ``shared:v2`` 键的步骤删除——
旧键 ``openai_compat:owner-embedding:2`` 本就是作者当前模型的空间，授权时查询向量直接命中。
"""

import pytest

from app.core.deps import CurrentUser, DbDep, EmbedEnvDep
from app.services import collaboration_ai_guard, kb_service, project_ai_config
from tests.test_ai_visibility_dispatch import _server
from tests.test_collaboration_shared_index_e6 import _real_clients, _world


def _mount(app):
    @app.post("/_test_shared_knowledge/{project_id}")
    async def knowledge(project_id: int, user: CurrentUser, db: DbDep, env: EmbedEnvDep, body: dict):
        return {"knowledge": await kb_service.knowledge_for_http(
            db, user_id=user.id, project_id=project_id, query=body["query"], env=env,
        )}


@pytest.mark.parametrize("enabled", [False, True])
async def test_http_context_fixes_actor_and_author_vector_selection(app, enabled):
    _mount(app)
    payload = b'{"object":"list","data":[{"object":"embedding","index":0,"embedding":[1.0,0.0]}]}'
    async with _server(mime="application/json", payload=payload) as (url, _, headers, body, requests):
        headers.set()
        body.set()
        async with _real_clients(app) as (owner, editor):
            world = await _world(app, owner, editor, url)
            if enabled:
                changed = await owner.patch(f"{world['base']}/collaborators/{world['editor']}", json={
                    "expected_revision": world["member"]["revision"], "allow_owner_ai": True,
                })
                assert changed.status_code == 200, changed.text
                async with app.state.sessionmaker() as db:
                    selection = await project_ai_config.resolve_shared_embedding(
                        db, world["editor"], world["project"], cipher=app.state.cipher, allow_private=True,
                    )
                    assert selection.model.config_owner_id != world["editor"]
            response = await editor.post(f"/_test_shared_knowledge/{world['project']}", json={
                "query": "旧有向量保留",
            })
            assert response.status_code == 200, response.text
            assert "旧有向量保留" in response.json()["knowledge"]
            assert len(requests) == (1 if enabled else 0)
            # 授权时只用作者的向量配置（协作者本人也配置了 editor-embedding，绝不被采用）。
            assert all(b"owner-embedding" in request and b"editor-embedding" not in request for request in requests)


async def test_http_context_never_sends_after_original_embedding_grant_changes(app, monkeypatch):
    _mount(app)
    async with _server(mime="application/json", payload=b'{}') as (url, _, headers, body, requests):
        headers.set()
        body.set()
        async with _real_clients(app) as (owner, editor):
            world = await _world(app, owner, editor, url)
            enabled = await owner.patch(f"{world['base']}/collaborators/{world['editor']}", json={
                "expected_revision": world["member"]["revision"], "allow_owner_ai": True,
            })
            assert enabled.status_code == 200, enabled.text
            original = collaboration_ai_guard.SendGuard.prepare_provider

            async def revoke_before_send(guard, *args, **kwargs):
                bound = await original(guard, *args, **kwargs)
                revoked = await owner.patch(f"{world['base']}/collaborators/{world['editor']}", json={
                    "expected_revision": enabled.json()["revision"], "allow_owner_ai": False,
                })
                assert revoked.status_code == 200, revoked.text
                return bound

            monkeypatch.setattr(collaboration_ai_guard.SendGuard, "prepare_provider", revoke_before_send)
            response = await editor.post(f"/_test_shared_knowledge/{world['project']}", json={
                "query": "旧有向量保留",
            })
            assert response.status_code in {403, 409}, response.text
            assert "旧有向量保留" not in response.text and requests == []
