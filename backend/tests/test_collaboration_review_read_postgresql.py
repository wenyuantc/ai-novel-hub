"""PostgreSQL 两连接和 RR 旧快照下的提案读取撤权。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.deps import get_db
from app.models import SceneReviewProposal, Task, User
from app.services import rich_document_review
from tests import pg_support
from tests.test_collaboration_review_read import make_review, review_request


@pytest.fixture
async def postgres_review_apps(tmp_path, fake_llm, monkeypatch, pg_schema_lease):
    url = pg_support.collab_test_url("AINOVEL_COLLAB_REVIEW_POSTGRES_TEST_URL", "collab_review", purpose="提案读取测试")
    async with pg_support.postgres_apps(
        pg_schema_lease, url, tmp_path, fake_llm, monkeypatch, count=2,
    ) as apps:
        yield apps


@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ"])
async def test_review_read_uses_fresh_snapshot_after_authorization_changes(
    postgres_review_apps, monkeypatch, isolation,
):
    owner_app, guest_app = postgres_review_apps
    if isolation == "REPEATABLE READ":
        # 人工维持旧请求事务快照；最终独立会话仍须看到另一事务已提交的撤权。
        rr_engine = guest_app.state.engine.execution_options(isolation_level="REPEATABLE READ")
        rr_factory = async_sessionmaker(
            rr_engine, expire_on_commit=False, info=guest_app.state.sessionmaker.kw["info"],
        )

        async def rr_db():
            async with rr_factory() as db:
                yield db

        guest_app.dependency_overrides[get_db] = rr_db

    original = rich_document_review._read_payload
    scenarios = ("revoke", "downgrade", "reinvite", "token", "task_uuid", "queued_at", "proposal_run", "unchanged")
    for index, transition in enumerate(scenarios):
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=owner_app), base_url="http://testserver") as owner,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=guest_app), base_url="http://testserver") as guest,
        ):
            suffix = f"{isolation.replace(' ', '-').lower()}-{index}"
            _, user, project, member, identity = await make_review(
                owner_app, owner, guest, username=f"review-{suffix}", owner_username=f"owner-{suffix}",
            )
            path, query = review_request(identity)
            assert (await guest.get(path, params=query)).status_code == 200
            reached, release = asyncio.Event(), asyncio.Event()

            async def blocked(db, proposal, *, reached=reached, release=release):
                payload = await original(db, proposal)
                reached.set()
                await release.wait()
                return payload

            monkeypatch.setattr(rich_document_review, "_read_payload", blocked)
            pending = asyncio.create_task(guest.get(path, params=query))
            try:
                await asyncio.wait_for(reached.wait(), 10)
                member_path = f"/api/v1/projects/{project['id']}/collaborators/{user['id']}"
                if transition in {"revoke", "reinvite"}:
                    result = await owner.delete(member_path, params={"expected_revision": member["revision"]})
                    assert result.status_code == 204, result.text
                    if transition == "reinvite":
                        invited = await owner.post(f"/api/v1/projects/{project['id']}/collaborators", json={
                            "username": f"review-{suffix}", "role": "editor",
                        })
                        assert invited.status_code == 201, invited.text
                        invite = invited.json()
                        accepted = await guest.post(
                            f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept",
                            json={"expected_revision": invite["revision"]},
                        )
                        assert accepted.status_code == 200, accepted.text
                elif transition == "downgrade":
                    result = await owner.patch(member_path, json={
                        "expected_revision": member["revision"], "role": "viewer",
                    })
                    assert result.status_code == 200, result.text
                elif transition != "unchanged":
                    async with owner_app.state.sessionmaker() as db:
                        if transition == "token":
                            await db.execute(update(User).where(User.id == user["id"]).values(
                                token_version=User.token_version + 1,
                            ))
                        elif transition == "proposal_run":
                            (await db.get(SceneReviewProposal, identity["proposal_id"])).run_key = str(uuid4())
                        else:
                            task = await db.get(Task, identity["task_id"])
                            if transition == "task_uuid":
                                task.statistics_key = str(uuid4())
                            else:
                                task.queued_at += timedelta(microseconds=1)
                        await db.commit()
                release.set()
                result = await asyncio.wait_for(pending, 10)
                expected = 200 if transition == "unchanged" else 401 if transition == "token" else 404
                assert result.status_code == expected, result.text
                if transition != "unchanged":
                    assert "需要保护的候选正文" not in result.text
            finally:
                release.set()
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                monkeypatch.setattr(rich_document_review, "_read_payload", original)
    guest_app.dependency_overrides.pop(get_db, None)
