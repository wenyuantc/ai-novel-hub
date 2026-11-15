"""富文档提案读取时以原任务与协作轮次重新核验。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update

from app.core.errors import ConflictError
from app.models import Chapter, SceneReviewProposal, Task, User
from app.schemas.rich_document import SceneDocument
from app.services import rich_document_review
from app.services.rich_document_proposal_mapping import build_scene_proposal
from tests.helpers import create_project, register


async def make_review(app, owner, guest, *, username: str = "review-guest", owner_username: str = "review-owner"):
    """只构造持久化测试事实，不通过尚未开放的协作 AI 路由造任务。"""
    await app.state.task_engine.stop()
    author = await register(owner, owner_username)
    user = await register(guest, username)
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    chapter = await owner.post(f"{base}/chapters", json={"chapter_no": 1, "title": "测试章", "content": "原文"})
    assert chapter.status_code == 201, chapter.text
    chapter_id = chapter.json()["id"]
    scenes = await owner.get(f"{base}/chapters/{chapter_id}/scenes")
    assert scenes.status_code == 200, scenes.text
    scene_id = scenes.json()["items"][0]["id"]
    invited = await owner.post(f"{base}/collaborators", json={"username": username, "role": "editor"})
    assert invited.status_code == 201, invited.text
    invitation = invited.json()
    accepted = await guest.post(
        f"/api/v1/collaboration/invitations/project/{invitation['invitation_id']}/accept",
        json={"expected_revision": invitation["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    member = accepted.json()
    source = SceneDocument(content="原文")
    plan = build_scene_proposal(source, "需要保护的候选正文")
    payload = {"mapping_version": 1, "scenes": [{
        "scene_id": scene_id, "original": source.model_dump(mode="json"), "candidate": plan.candidate,
        "template": plan.template, "unresolved_sections": plan.unresolved_sections, "format_loss": plan.format_loss,
    }]}
    async with app.state.sessionmaker() as db:
        task = Task(user_id=user["id"], project_id=project["id"], chapter_id=chapter_id,
                    kind="chapter.generate", status="paused", input={"_collaboration_grant": {
                        "invitation_id": member["invitation_id"], "revision": member["revision"],
                    }})
        db.add(task)
        await db.flush()
        proposal = SceneReviewProposal(task_id=task.id, chapter_id=chapter_id, task_key=task.statistics_key,
                                       run_key=str(uuid4()), token=str(uuid4()), content_revision=0,
                                       status="pending", payload={})
        db.add(proposal)
        await rich_document_review._write_payload(db, proposal, payload)
        await db.commit()
        identity = {"task_id": task.id, "task_key": task.statistics_key, "run_key": proposal.run_key,
                    "token": proposal.token, "proposal_id": proposal.id}
    return author, user, project, member, identity


def review_request(identity):
    path = f"/api/v1/tasks/{identity['task_id']}/rich-proposals/{identity['token']}"
    return path, {"task_key": identity["task_key"], "run_key": identity["run_key"]}


@pytest.mark.parametrize("transition", [
    "revoke", "downgrade", "revision", "reinvite", "token", "disable",
    "task_uuid", "queued_at", "task_status", "proposal_run", "proposal_status", "proposal_payload",
])
async def test_review_read_rechecks_after_interleaved_change(app, client, make_client, monkeypatch, transition):
    # 读事务保持旧快照时，另一个客户端仍可提交后续身份变更。
    async with app.state.engine.begin() as conn:
        await conn.execute(text("PRAGMA journal_mode=WAL"))
    guest = make_client()
    _, user, project, member, identity = await make_review(app, client, guest)
    path, query = review_request(identity)
    assert (await guest.get(path, params=query)).status_code == 200
    reached, release = asyncio.Event(), asyncio.Event()
    original = rich_document_review._read_payload

    async def blocked(db, proposal):
        payload = await original(db, proposal)
        reached.set()
        await release.wait()
        return payload

    monkeypatch.setattr(rich_document_review, "_read_payload", blocked)
    request = asyncio.create_task(guest.get(path, params=query))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        member_path = f"/api/v1/projects/{project['id']}/collaborators/{user['id']}"
        if transition in {"revoke", "reinvite"}:
            revoked = await client.delete(member_path, params={"expected_revision": member["revision"]})
            assert revoked.status_code == 204
            if transition == "reinvite":
                reinvited = await client.post(f"/api/v1/projects/{project['id']}/collaborators", json={
                    "username": "review-guest", "role": "editor",
                })
                assert reinvited.status_code == 201
                invite = reinvited.json()
                assert (await guest.post(
                    f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept",
                    json={"expected_revision": invite["revision"]},
                )).status_code == 200
        elif transition in {"downgrade", "revision"}:
            body = {"expected_revision": member["revision"]}
            body.update({"role": "viewer"} if transition == "downgrade" else {"allow_owner_ai": True})
            assert (await client.patch(member_path, json=body)).status_code == 200
        else:
            async with app.state.sessionmaker() as db:
                if transition in {"token", "disable"}:
                    values = (
                        {"token_version": User.token_version + 1} if transition == "token" else {"is_active": False}
                    )
                    await db.execute(update(User).where(User.id == user["id"]).values(**values))
                elif transition in {"task_uuid", "queued_at", "task_status"}:
                    task = await db.get(Task, identity["task_id"])
                    if transition == "task_uuid":
                        task.statistics_key = str(uuid4())
                    elif transition == "queued_at":
                        task.queued_at += timedelta(microseconds=1)
                    else:
                        task.status = "succeeded"
                else:
                    proposal = await db.get(SceneReviewProposal, identity["proposal_id"])
                    if transition == "proposal_run":
                        proposal.run_key = str(uuid4())
                    elif transition == "proposal_status":
                        proposal.status = "discarded"
                    else:
                        proposal.payload = {**proposal.payload, "sha256": "0" * 64}
                await db.commit()
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == (401 if transition in {"token", "disable"} else 404), response.text
        assert "需要保护的候选正文" not in response.text
    finally:
        release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)


async def test_review_role_and_foreign_chapter_boundaries(app, client, make_client, system_admin):
    guest, stranger = make_client(), make_client()
    author, user, project, member, identity = await make_review(app, client, guest)
    await register(stranger, "review-stranger")
    path, query = review_request(identity)
    assert (await guest.get(path, params=query)).status_code == 200
    for actor in (client, stranger, system_admin):
        assert (await actor.get(path, params=query)).status_code == 404
    async with app.state.sessionmaker() as db:
        (await db.get(User, user["id"])).is_admin = True
        await db.commit()
    downgraded = await client.patch(f"/api/v1/projects/{project['id']}/collaborators/{user['id']}", json={
        "expected_revision": member["revision"], "role": "viewer",
    })
    assert downgraded.status_code == 200
    assert (await guest.get(path, params=query)).status_code == 404  # 管理员人工权限不授予活动任务
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, identity["task_id"])
        task.status = "succeeded"
        proposal = await db.get(SceneReviewProposal, identity["proposal_id"])
        proposal.status = "applied"
        await db.commit()
    assert (await guest.get(path, params=query)).status_code == 200  # 仍可读取本人已结束记录
    async with app.state.sessionmaker() as db:
        other = await db.scalar(select(Chapter).where(Chapter.project_id == project["id"]))
        assert other is not None
        # 非本人任务仍不可被已接受成员读取。
        owner_task = Task(user_id=author["id"], project_id=project["id"], chapter_id=other.id,
                          kind="chapter.generate", status="paused")
        db.add(owner_task)
        await db.flush()
        source = SceneDocument(content="作者原文")
        plan = build_scene_proposal(source, "作者的私人候选")
        proposal = SceneReviewProposal(task_id=owner_task.id, chapter_id=other.id, task_key=owner_task.statistics_key,
                                       run_key=str(uuid4()), token=str(uuid4()), content_revision=0, payload={})
        db.add(proposal)
        await rich_document_review._write_payload(db, proposal, {"mapping_version": 1, "scenes": [{
            "scene_id": 1, "original": source.model_dump(mode="json"), "candidate": plan.candidate,
            "template": plan.template, "unresolved_sections": plan.unresolved_sections, "format_loss": plan.format_loss,
        }]})
        await db.commit()
        own_identity = {"task_id": owner_task.id, "task_key": owner_task.statistics_key,
                        "run_key": proposal.run_key, "token": proposal.token}
    own_path, own_query = review_request(own_identity)
    assert (await client.get(own_path, params=own_query)).status_code == 200
    assert (await guest.get(own_path, params=own_query)).status_code == 404

    foreign_project = await create_project(stranger, title="外书")
    foreign_chapter = await stranger.post(f"/api/v1/projects/{foreign_project['id']}/chapters", json={
        "chapter_no": 1, "title": "其他作品",
    })
    assert foreign_chapter.status_code == 201
    async with app.state.sessionmaker() as db:
        proposal = await db.get(SceneReviewProposal, identity["proposal_id"])
        proposal.chapter_id = foreign_chapter.json()["id"]
        await db.commit()
    assert (await guest.get(path, params=query)).status_code == 404


async def test_review_read_does_not_flush_unrelated_pending_changes(app, client, make_client):
    guest = make_client()
    _, user, project, _, identity = await make_review(app, client, guest)
    async with app.state.sessionmaker() as db:
        chapter = await db.scalar(select(Chapter).where(Chapter.project_id == project["id"]))
        chapter.title = "仅暂存标题"
        result = await rich_document_review.get_review(
            db, user["id"], identity["task_id"], identity["task_key"], identity["run_key"], identity["token"],
        )
        assert result["scenes"][0]["candidate"] == "需要保护的候选正文"
        assert chapter.title == "仅暂存标题" and db.is_modified(chapter)
        with db.no_autoflush:
            assert await db.scalar(select(Chapter.title).where(Chapter.id == chapter.id)) == "测试章"
        await db.rollback()


async def test_review_rejects_payload_scene_from_another_project(app, client, make_client):
    guest, stranger = make_client(), make_client()
    _, _, _, _, identity = await make_review(app, client, guest)
    await register(stranger, "foreign-review-owner")
    foreign = await create_project(stranger, title="其他作品")
    foreign_base = f"/api/v1/projects/{foreign['id']}"
    chapter = await stranger.post(f"{foreign_base}/chapters", json={"chapter_no": 1, "title": "秘密章"})
    assert chapter.status_code == 201
    scenes = await stranger.get(f"{foreign_base}/chapters/{chapter.json()['id']}/scenes")
    foreign_scene = scenes.json()["items"][0]["id"]
    source = SceneDocument(content="外书私人原文")
    plan = build_scene_proposal(source, "外书私人候选")
    async with app.state.sessionmaker() as db:
        proposal = await db.get(SceneReviewProposal, identity["proposal_id"])
        # 兼容早期单行提案，但其中的场景身份必须属于同一章。
        proposal.payload = {"mapping_version": 1, "scenes": [{
            "scene_id": foreign_scene, "original": source.model_dump(mode="json"), "candidate": plan.candidate,
            "template": plan.template, "unresolved_sections": plan.unresolved_sections, "format_loss": plan.format_loss,
        }]}
        await db.commit()
    path, query = review_request(identity)
    result = await guest.get(path, params=query)
    assert result.status_code == 404
    assert "外书私人" not in result.text


async def test_review_invalid_payload_does_not_bypass_concurrent_revocation(app, client, make_client, monkeypatch):
    guest = make_client()
    _, user, project, member, identity = await make_review(app, client, guest)
    path, query = review_request(identity)
    reached, release = asyncio.Event(), asyncio.Event()
    original = rich_document_review._read_payload

    async def blocked(db, proposal):
        await original(db, proposal)
        reached.set()
        await release.wait()
        raise ConflictError("提案损坏")

    monkeypatch.setattr(rich_document_review, "_read_payload", blocked)
    request = asyncio.create_task(guest.get(path, params=query))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        path_member = f"/api/v1/projects/{project['id']}/collaborators/{user['id']}"
        assert (await client.delete(path_member, params={"expected_revision": member["revision"]})).status_code == 204
        release.set()
        response = await asyncio.wait_for(request, 5)
        assert response.status_code == 404 and "提案损坏" not in response.text
    finally:
        release.set()
        if not request.done():
            request.cancel()
        await asyncio.gather(request, return_exceptions=True)

