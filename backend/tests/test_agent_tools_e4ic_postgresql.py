"""E4i-c 在真实 PostgreSQL（Agent 会话 REPEATABLE READ、真实行锁 / advisory 门禁）上的关键路径：

- 编辑者工具写入经协作者分支成功；降级为 viewer 后沿用旧轮次的同一写入以授权失效终止、作品不变；
- 授权编辑者轮次在受理时冻结作者共享向量选择，检索工具经 for_task 守卫用作者向量；未授权编辑者零作者向量调用。
"""

import httpx
import pytest
from sqlalchemy import select

from app.models import Chapter, Character, Task
from app.services.agent import transactions
from tests import pg_support
from tests.test_agent_access_e4ia import _set_role
from tests.test_agent_tools_e4ic import _call, _editor_world, _retrieval_world, _search_turn, _seed


@pytest.fixture
async def postgres_e4ic_app(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url(purpose="Agent 工具协作测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=1,
    ) as apps:
        app = apps[0]
        await app.state.task_engine.start()
        yield app


def _clients(app):
    transport = httpx.ASGITransport(app=app)
    return (httpx.AsyncClient(transport=transport, base_url="http://testserver"),
            httpx.AsyncClient(transport=transport, base_url="http://testserver"))


async def test_postgresql_editor_tool_write_and_downgraded_old_grant(postgres_e4ic_app):
    app = postgres_e4ic_app
    owner_client, editor_client = _clients(app)
    async with owner_client, editor_client:
        world = await _editor_world(app, owner_client, editor_client)
        seeded = await _seed(app, owner_client, world.owner["id"], world.project["id"])
        pid, editor_id = world.project["id"], world.editor["id"]
        created = await _call(app, editor_id, pid, world.grant, "create_character", {"name": "协作角色"})
        assert "error" not in created, created
        renamed = await _call(app, editor_id, pid, world.grant, "update_chapter_meta",
                              {"id": seeded.chapter, "title": "协作标题"}, locked=True)
        assert "error" not in renamed, renamed
        await _set_role(owner_client, pid, editor_id, world.member["revision"], "viewer")
        for locked in (False, True):
            with pytest.raises(transactions.AgentAuthorizationChangedError):
                await _call(app, editor_id, pid, world.grant, "create_character", {"name": "越权角色"},
                            locked=locked)
    async with app.state.sessionmaker() as db:
        names = set(await db.scalars(select(Character.name).where(Character.project_id == pid)))
        assert "协作角色" in names and "越权角色" not in names
        assert (await db.get(Chapter, seeded.chapter)).title == "协作标题"


async def test_postgresql_retrieval_freeze_and_keyword_only(postgres_e4ic_app, fake_llm):
    app = postgres_e4ic_app
    owner_client, editor_client = _clients(app)
    async with owner_client, editor_client:
        world = await _retrieval_world(app, owner_client, editor_client, allow_owner_ai=True)
        row, data, _, embeds = await _search_turn(app, fake_llm, world)
        assert row.status == "succeeded", row.error
        assert row.input["_shared_embedding"]["mode"] == "vector"
        assert [(item["model"], item["texts"]) for item in embeds] == [("owner-embed", ["天剑宗师承"])]
        assert data["mode"] == "hybrid" and data["hits"], data
        # 作者关闭授权（递增成员版本）后新发起的轮次冻结为关键词：零作者向量调用。
        response = await owner_client.patch(
            f"/api/v1/projects/{world.project['id']}/collaborators/{world.editor['id']}",
            json={"expected_revision": world.member["revision"], "allow_owner_ai": False},
        )
        assert response.status_code == 200, response.text
        keyword_row, keyword_data, _, keyword_embeds = await _search_turn(app, fake_llm, world)
    assert keyword_row.status == "succeeded", keyword_row.error
    assert keyword_row.input["_shared_embedding"]["mode"] == "keyword"
    assert keyword_embeds == [] and keyword_data["mode"] == "keyword"
    async with app.state.sessionmaker() as db:
        stored = await db.get(Task, keyword_row.id)
        assert stored.user_id == world.editor["id"]
