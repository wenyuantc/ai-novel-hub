"""E5-a：任务读写面的身份 × 操作 × 状态 × 授权轮次矩阵，以及批量审核点的协作者 finish/采纳。

身份：作者、原轮次 editor、降为 viewer、待接受、已拒绝、已撤权、重邀后旧轮次、非成员、仅管理员（只读 / 可写）、
管理员兼 viewer / editor。任务均为该身份本人的任务（作者任务无协作轮次）；另核对跨身份访问一律 404。
引擎停止，任务行为直接构造的持久事实（不调用模型）；SSE 不在本片范围。
"""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.ai.providers.base import AIProviderError
from app.models import Chapter, ChapterVersion, GenerationLog, SceneReviewProposal, Task, User
from app.services.collaboration_task_access import GRANT_KEY
from tests.helpers import create_project, register, task_params
from tests.rbac_helpers import assign_roles, create_role
from tests.test_collaboration_batch_entry_e4g2 import _assert_untouched, _change, _start, _terminal, _world
from tests.test_rich_document_api import KEY, add_section, save_payload

STATES = ("queued", "running", "paused", "failed")
FULL = {"owner", "editor", "admin_editor"}
FINISHED_ONLY = {"viewer", "admin_viewer", "reinvited"}
HIDDEN = {"pending", "declined", "revoked", "stranger", "admin_ro", "admin_rw"}
IDENTITIES = sorted(FULL | FINISHED_ONLY | HIDDEN)


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


class Surface:
    def __init__(self, app, project):
        self.app, self.project = app, project
        self.clients: dict[str, object] = {}
        self.users: dict[str, dict] = {}
        self.grants: dict[str, dict | None] = {}

    async def task(self, name: str, state: str) -> tuple[int, str]:
        payload = {"request": {"project_id": self.project["id"]}, "_task_actor_token_version": 0}
        grant = self.grants[name]
        if grant is not None:
            payload[GRANT_KEY] = grant
        values = {"status": state, "output_text": f"私有输出-{name}-{state}"}
        if state == "running":
            values["execution_key"] = str(uuid4())
        if state == "paused":
            values["result"] = {"paused": True}
        if state == "failed":
            values["error"] = "失败"
        async with self.app.state.sessionmaker() as db:
            row = Task(user_id=self.users[name]["id"], project_id=self.project["id"], kind="outline.continue",
                       title=f"{name}-{state}", input=payload, **values)
            db.add(row)
            await db.commit()
            return row.id, row.statistics_key

    async def row(self, task_id: int) -> Task | None:
        async with self.app.state.sessionmaker() as db:
            return await db.get(Task, task_id)


async def _accept(client, invite):
    accepted = await client.post(
        f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/accept",
        json={"expected_revision": invite["revision"]},
    )
    assert accepted.status_code == 200, accepted.text
    return accepted.json()


async def _surface(app, owner, make_client) -> Surface:
    await app.state.task_engine.stop()
    owner_user = await register(owner, "sf-owner")
    project = await create_project(owner)
    surface = Surface(app, project)
    surface.clients["owner"], surface.users["owner"], surface.grants["owner"] = owner, owner_user, None
    base = f"/api/v1/projects/{project['id']}/collaborators"
    for name in IDENTITIES:
        if name == "owner":
            continue
        client = make_client()
        surface.clients[name] = client
        surface.users[name] = await register(client, f"sf-{name}")
        surface.grants[name] = {"invitation_id": str(uuid4()), "revision": 1}
    async with app.state.sessionmaker() as db:
        for name in ("admin_rw", "admin_viewer", "admin_editor"):
            (await db.get(User, surface.users[name]["id"])).is_admin = True
        await db.commit()
    role = await create_role(surface.clients["admin_rw"], "只读作品管理", ["projects.read"])
    await assign_roles(surface.clients["admin_rw"], surface.users["admin_ro"]["id"], [role["id"]])
    for name in ("editor", "viewer", "revoked", "reinvited", "admin_viewer", "admin_editor", "pending", "declined"):
        invited = await owner.post(base, json={"username": f"sf-{name}", "role": "editor"})
        assert invited.status_code == 201, invited.text
        invite = invited.json()
        if name == "pending":
            surface.grants[name] = {"invitation_id": invite["invitation_id"], "revision": invite["revision"]}
            continue
        if name == "declined":
            declined = await surface.clients[name].post(
                f"/api/v1/collaboration/invitations/project/{invite['invitation_id']}/decline",
                json={"expected_revision": invite["revision"]},
            )
            assert declined.status_code == 200, declined.text
            surface.grants[name] = {"invitation_id": invite["invitation_id"], "revision": invite["revision"]}
            continue
        member = await _accept(surface.clients[name], invite)
        # 任务在原轮次受理：之后的降级 / 撤权 / 重邀都使其失效。
        surface.grants[name] = {"invitation_id": member["invitation_id"], "revision": member["revision"]}
        member_url = f"{base}/{surface.users[name]['id']}"
        if name in ("viewer", "admin_viewer"):
            changed = await owner.patch(member_url, json={"expected_revision": member["revision"], "role": "viewer"})
            assert changed.status_code == 200, changed.text
        elif name in ("revoked", "reinvited"):
            revoked = await owner.delete(member_url, params={"expected_revision": member["revision"]})
            assert revoked.status_code == 204, revoked.text
            if name == "reinvited":
                again = await owner.post(base, json={"username": f"sf-{name}", "role": "editor"})
                assert again.status_code == 201, again.text
                new = await _accept(surface.clients[name], again.json())
                assert new["invitation_id"] != member["invitation_id"]
    return surface


def _visible(name: str, state: str) -> bool:
    return name in FULL or (name in FINISHED_ONLY and state == "failed")


# ---------------- 读：list / detail / output / clear ----------------


@pytest.mark.parametrize("name", IDENTITIES)
async def test_read_surface_matrix(app, client, make_client, name):
    surface = await _surface(app, client, make_client)
    actor = surface.clients[name]
    created = {state: await surface.task(name, state) for state in STATES}
    listed = await actor.get("/api/v1/tasks", params={"project_id": surface.project["id"]})
    assert listed.status_code == 200, listed.text
    listed_ids = {row["id"] for row in listed.json()}
    for state, identity in created.items():
        visible = _visible(name, state)
        assert (identity[0] in listed_ids) is visible, (name, state)
        detail = await actor.get(f"/api/v1/tasks/{identity[0]}", params=task_params(identity))
        output = await actor.get(f"/api/v1/tasks/{identity[0]}/output", params=task_params(identity))
        if visible:
            assert detail.status_code == 200 and detail.json()["status"] == state, detail.text
            assert output.status_code == 200 and output.json()["text"] == f"私有输出-{name}-{state}"
            assert GRANT_KEY not in detail.text
        else:
            assert detail.status_code == output.status_code == 404, (name, state, detail.text)
            assert "私有输出" not in detail.text + output.text
    # 他人的任务（作者 ↔ 编辑者）在任何身份下都不可读，也不出现在列表中。
    other = "editor" if name != "editor" else "owner"
    foreign = await surface.task(other, "failed")
    for path in ("", "/output"):
        response = await actor.get(f"/api/v1/tasks/{foreign[0]}{path}", params=task_params(foreign))
        assert response.status_code == 404 and "私有输出" not in response.text
    assert foreign[0] not in {row["id"] for row in (await actor.get("/api/v1/tasks")).json()}
    # 批量清理只删除本人可见的已结束任务；不可见者 0 条且记录保留。
    cleared = await actor.delete("/api/v1/tasks", params={"project_id": surface.project["id"]})
    assert cleared.status_code == 200, cleared.text
    finished_id = created["failed"][0]
    assert cleared.json()["deleted"] == (1 if _visible(name, "failed") else 0)
    assert (await surface.row(finished_id) is None) is _visible(name, "failed")
    assert await surface.row(foreign[0]) is not None
    for state in ("queued", "running", "paused"):
        assert await surface.row(created[state][0]) is not None


# ---------------- 写：cancel / resume / finish / delete / retry ----------------


# (状态码, 操作后的数据库状态)；None 表示状态不变。
FULL_EXPECTED = {
    ("cancel", "queued"): (200, "cancelled"), ("cancel", "running"): (200, "cancelled"),
    ("cancel", "paused"): (200, "cancelled"), ("cancel", "failed"): (409, None),
    ("resume", "queued"): (409, None), ("resume", "running"): (409, None),
    ("resume", "paused"): (202, "queued"), ("resume", "failed"): (409, None),
    ("finish", "queued"): (409, None), ("finish", "running"): (409, None),
    ("finish", "paused"): (200, "succeeded"), ("finish", "failed"): (409, None),
    ("delete", "queued"): (409, None), ("delete", "running"): (409, None),
    ("delete", "paused"): (409, None), ("delete", "failed"): (204, "deleted"),
}


async def _write(actor, op, identity):
    task_id = identity[0]
    if op == "delete":
        return await actor.delete(f"/api/v1/tasks/{task_id}", params=task_params(identity))
    return await actor.post(f"/api/v1/tasks/{task_id}/{op}", params=task_params(identity))


@pytest.mark.parametrize("name", IDENTITIES)
async def test_write_surface_matrix(app, client, make_client, name):
    surface = await _surface(app, client, make_client)
    actor = surface.clients[name]
    for op in ("cancel", "resume", "finish", "delete", "retry"):
        for state in STATES:
            identity = await surface.task(name, state)
            before = await surface.row(identity[0])
            response = await _write(actor, op, identity)
            after = await surface.row(identity[0])
            assert "私有输出" not in response.text
            if op == "retry":
                # 合成任务缺原模型绑定：可见的已结束任务被受控重试按原绑定拒绝（400），其余可见状态 409；
                # 不可见一律 404。任何身份都不能凭重试新建任务。
                expected = (400 if state == "failed" else 409) if _visible(name, state) else 404
                assert response.status_code == expected, (name, state, response.text)
                async with app.state.sessionmaker() as db:
                    assert await db.scalar(select(func.count(Task.id)).where(Task.retry_of_id == identity[0])) == 0
                assert after.status == before.status
                continue
            if name in FULL:
                code, status = FULL_EXPECTED[(op, state)]
            elif name in FINISHED_ONLY and state == "failed":
                # 降级 / 旧轮次仍可删除本人已结束记录；控制类操作要求原轮次 editor。
                code, status = (204, "deleted") if op == "delete" else (404, None)
            else:
                code, status = 404, None
            assert response.status_code == code, (name, op, state, response.text)
            if status == "deleted":
                assert after is None
            elif status is None:
                assert (after.status, after.error, after.queued_at) == (before.status, before.error, before.queued_at)
            else:
                assert after.status == status, (name, op, state, after.status)


async def test_cross_identity_task_control_is_hidden(app, client, make_client):
    """成员不能控制作者或其他成员的任务；管理员人工权限不授予个人任务控制或读取。"""
    surface = await _surface(app, client, make_client)
    targets = {name: {state: await surface.task(name, state) for state in STATES} for name in ("owner", "editor")}
    for name in IDENTITIES:
        actor = surface.clients[name]
        for target, tasks in targets.items():
            if target == name:
                continue
            for state, identity in tasks.items():
                for op in ("cancel", "resume", "finish", "delete"):
                    response = await _write(actor, op, identity)
                    assert response.status_code == 404, (name, target, op, state, response.text)
                    assert (await surface.row(identity[0])).status == state


async def test_admin_operations_can_cancel_but_never_read_output(app, client, make_client):
    """管理员按运维权限取消他人任务保持现状：只读元数据、可受理取消，不能读输出或经个人任务接口控制。"""
    surface = await _surface(app, client, make_client)
    admin = surface.clients["admin_rw"]
    identity = await surface.task("editor", "queued")
    task_id, task_key = identity
    meta = await admin.get(f"/api/v1/admin/tasks/{task_key}")
    assert meta.status_code == 200, meta.text
    assert "私有输出" not in meta.text and "output_text" not in meta.json() and "input" not in meta.json()
    assert meta.json()["owner_user_id"] == surface.users["editor"]["id"]
    for path in (f"/api/v1/tasks/{task_id}", f"/api/v1/tasks/{task_id}/output"):
        assert (await admin.get(path, params=task_params(identity))).status_code == 404
    cancelled = await admin.post(f"/api/v1/admin/tasks/{task_key}/cancel",
                                 json={"confirm": True, "reason": "运维处理"})
    assert cancelled.status_code == 202, cancelled.text
    row = await surface.row(task_id)
    assert row.admin_cancel_operation_id is not None
    # 取消受理后仍不能读输出；编辑者本人仍可读自己的任务，但控制类操作在管理员意图存在时安全拒绝。
    assert (await admin.get(f"/api/v1/tasks/{task_id}/output", params=task_params(identity))).status_code == 404
    editor = surface.clients["editor"]
    assert (await editor.get(f"/api/v1/tasks/{task_id}/output", params=task_params(identity))).status_code == 200
    assert (await editor.post(f"/api/v1/tasks/{task_id}/resume", params=task_params(identity))).status_code == 409
    # 只读管理员（projects.read）连任务元数据都没有权限。
    assert (await surface.clients["admin_ro"].get(f"/api/v1/admin/tasks/{task_key}")).status_code == 403


# ---------------- 批量审核点：协作者 finish 与富文档提案采纳 ----------------


async def _rich_batch(app, owner, guest, fake_llm):
    """编辑者对含富文档分节的章节发起批量：第一章产出待审核提案，任务停在审核点。"""
    world = await _world(owner, guest, count=2)
    chapter = world.chapters[0]
    saved = await owner.patch(f"{world.base}/chapters/{chapter['id']}", json={
        "content": "原来的章节", "expected_content_revision": chapter["content_revision"],
    })
    assert saved.status_code == 200, saved.text
    scene = (await owner.get(f"{world.base}/chapters/{chapter['id']}/scenes")).json()["items"][0]
    document_url = f"{world.base}/chapters/{chapter['id']}/scenes/{scene['id']}/document"
    body = save_payload((await owner.get(document_url)).json())
    add_section(body)
    source = await owner.put(document_url, json=body)
    assert source.status_code == 200, source.text
    fake_llm.push("新生成的候选")
    created = await _start(guest, world, include_written=True)
    assert created.status_code == 202, created.text
    task_id = created.json()["id"]
    async with asyncio.timeout(15):
        while True:
            response = await guest.get(f"/api/v1/tasks/{task_id}", params=task_params(created.json()))
            assert response.status_code == 200, response.text
            if response.json()["status"] not in ("queued", "running"):
                break
            await asyncio.sleep(0.02)
    task = response.json()
    assert task["status"] == "paused" and task["rich_review"]["token"], task
    return world, scene, document_url, source.json(), task


def _accept_body(task, scene):
    review = task["rich_review"]
    return {"task_key": review["task_key"], "run_key": review["run_key"],
            "sections": [{"scene_id": scene["id"], "section_key": KEY, "anchor": 2}], "confirm_format_loss": True}


async def test_editor_accepts_batch_review_proposal_and_batch_continues(app, client, make_client, fake_llm):
    guest = make_client()
    world, scene, document_url, _source, task = await _rich_batch(app, client, guest, fake_llm)
    review = task["rich_review"]
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{review['token']}"
    query = {"task_key": review["task_key"], "run_key": review["run_key"]}
    read = await guest.get(path, params=query)
    assert read.status_code == 200, read.text
    assert (await client.get(path, params=query)).status_code == 404  # 作者不能读编辑者的私人提案
    assert (await client.post(f"{path}/accept", json=_accept_body(task, scene))).status_code == 404
    fake_llm.push("采纳后继续的第二章")
    accepted = await guest.post(f"{path}/accept", json=_accept_body(task, scene))
    assert accepted.status_code == 200, accepted.text
    assert (await client.get(document_url)).json()["document"]["content"] == "新生成的候选"
    async with asyncio.timeout(15):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task["id"])
            if row.status not in ("queued", "running"):
                break
            await asyncio.sleep(0.02)
    assert row.status == "succeeded" and row.user_id == world.guest["id"], (row.status, row.error)
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == review["token"]))
        assert proposal.status == "applied"
        logs = list(await db.scalars(select(GenerationLog).where(GenerationLog.task_id == task["id"])))
        assert logs and all(log.actor_user_id == world.guest["id"] for log in logs)
        versions = list(await db.scalars(select(ChapterVersion).where(
            ChapterVersion.chapter_id == world.chapters[0]["id"], ChapterVersion.generation_log_id.is_not(None),
        )))
        assert versions
        second = await db.get(Chapter, world.chapters[1]["id"])
        assert second.content == "采纳后继续的第二章"


@pytest.mark.parametrize("change", ["none", "viewer", "revoked", "reinvite"])
async def test_editor_finish_batch_review_point(app, client, make_client, fake_llm, change):
    """协作者 /finish：原轮次 editor 可提前结束（丢弃提案、批量记 succeeded）；授权变化后 404 且不写正文。"""
    guest = make_client()
    world, _scene, document_url, source, task = await _rich_batch(app, client, guest, fake_llm)
    engine = app.state.task_engine
    engine.collaboration_scan_interval = 3600
    if change != "none":
        await engine.stop()  # 让 /finish 与授权变化的先后确定：系统收尾见 paused_cleanup 文件
        await _change(app, client, world, change)
    review = task["rich_review"]
    identity = (task["id"], task["task_key"])
    finished = await guest.post(f"/api/v1/tasks/{task['id']}/finish", params=task_params(identity), json={
        "task_key": review["task_key"], "run_key": review["run_key"], "token": review["token"],
    })
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, task["id"])
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == review["token"]))
    if change == "none":
        assert finished.status_code == 200, finished.text
        assert finished.json()["status"] == "succeeded" and row.result["review_discarded"] is True
        assert proposal.status == "discarded"
    else:
        assert finished.status_code == 404, finished.text
        assert row.status == "paused" and proposal.status == "pending"
    assert (await client.get(document_url)).json() == source
    await _assert_untouched(app, world.chapters[1]["id"])
    assert len(fake_llm.calls) == 1
    # 作者无论如何都不能结束编辑者的任务。
    assert (await client.post(f"/api/v1/tasks/{task['id']}/finish", params=task_params(identity))).status_code == 404


@pytest.mark.parametrize("change", ["viewer", "revoked", "reinvite"])
async def test_editor_cannot_accept_batch_review_after_change(app, client, make_client, fake_llm, change):
    guest = make_client()
    world, scene, document_url, source, task = await _rich_batch(app, client, guest, fake_llm)
    engine = app.state.task_engine
    await engine.stop()  # 先测本人采纳被拒，再测系统收尾
    engine.collaboration_scan_interval = 3600
    await _change(app, client, world, change)
    review = task["rich_review"]
    path = f"/api/v1/tasks/{task['id']}/rich-proposals/{review['token']}"
    accepted = await guest.post(f"{path}/accept", json=_accept_body(task, scene))
    assert accepted.status_code == 404, accepted.text
    assert (await client.get(document_url)).json() == source
    await engine.start()
    async with asyncio.timeout(15):
        while True:
            row = await _task_row(app, task["id"])
            if row.status != "paused":
                break
            await asyncio.sleep(0.02)
    assert row.status == "failed"
    async with app.state.sessionmaker() as db:
        proposal = await db.scalar(select(SceneReviewProposal).where(SceneReviewProposal.token == review["token"]))
    assert proposal.status == "discarded" and row.checkpoint["rich_review"]["phase"] == "discarded"
    assert (await client.get(document_url)).json() == source
    assert len(fake_llm.calls) == 1
    started = await _start(client, world, first=1, last=1)  # 作者不再被批量互斥卡住
    assert started.status_code == 202, started.text


async def _task_row(app, task_id):
    async with app.state.sessionmaker() as db:
        return await db.get(Task, task_id)


@pytest.mark.parametrize("change", ["none", "viewer", "revoked", "reinvite"])
async def test_stale_member_retry_rejected_before_provider_preflight(app, client, make_client, fake_llm, monkeypatch,
                                                                     change):
    """受控重试的原模型短读预检先于锁外 provider/DNS 预检拒绝降级、撤权与旧轮次：不构造 provider、不建任务。

    正对照：原轮次 editor 重试会走到 provider 预检并受理。
    """
    from app.services import ai_config_service, batch_service

    monkeypatch.setattr(batch_service, "RETRY_DELAYS", (0.0, 0.0))
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push("重试前的第一章")
    for _ in range(3):
        fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    created = await _start(guest, world)
    assert created.status_code == 202, created.text
    first = await _terminal(app, created.json()["id"])
    assert first.status == "failed" and first.step_cursor == 1
    built: list[int] = []
    original = ai_config_service.build_provider

    async def counting(*args, **kwargs):
        built.append(1)
        return await original(*args, **kwargs)

    monkeypatch.setattr(ai_config_service, "build_provider", counting)
    if change != "none":
        await _change(app, client, world, change)
    calls = len(fake_llm.calls)
    if change == "none":
        fake_llm.push("重试补上的第二章")
    retried = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    async with app.state.sessionmaker() as db:
        children = await db.scalar(select(func.count(Task.id)).where(Task.retry_of_id == first.id))
    if change == "none":
        assert retried.status_code == 202, retried.text
        assert built and children == 1
        assert (await _terminal(app, retried.json()["id"])).status == "succeeded"
        return
    assert retried.status_code in (400, 403, 404), retried.text
    assert built == [] and children == 0
    assert len(fake_llm.calls) == calls
