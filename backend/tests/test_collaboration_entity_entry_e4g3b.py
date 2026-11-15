"""E4-g3b：accepted editor 的实体 AI（character.generate / organization.generate / career.generate /
relations.parse / relations.generate）。

HTTP 入口 POST /projects/{pid}/characters/generate、/organizations/generate、/careers/generate、
/relationships/generate、/characters/{cid}/parse-relations。实体任务只产出草稿：唯一持久化是 Task 自身的终态与
result（草稿由人工确认接口另行保存，阶段 D 按 content.edit 开放），终态在同一事务内按原 grant 锁内重验后写入，
因此撤权后不存在“已提交、待证明”的业务副作用，也没有收据收尾；终态核验失败经引擎 `_DRAFT_ONLY_KINDS` 改记
无结果失败。受理后领取前、领取后构建前、检索前、首次发送前、发送中、完成事件发布前与终态写入前撤权/降级/重邀/
停用/关闭作者模型授权都失败关闭：零后续发送、result 为空、任务不停在 running、活动资源键释放。模型/费用来源、
归属、私人风格与模板同 E4-f。检索（E6-d 翻转，裁决 1/9）：作者已授权且有向量配置时查询经 for_task 守卫用作者
共享向量（读作者旧键 provider:model:dim）；未授权编辑者只用关键词、作者向量模型零调用。
协作者 SSE 自 E5-f 起经受控管线开放（见 test_collaboration_stream_*、test_task_sse_scope），
本文件经任务
详情或数据库观察终态。
"""

import asyncio
import json

import pytest
from sqlalchemy import func, select, update

from app.ai.providers import AIProviderError
from app.models import (
    AIConfig,
    Character,
    Organization,
    Project,
    ProjectCollaborator,
    Relationship,
    RetrievalChunk,
    Task,
    User,
    WritingStyle,
)
from app.services import entity_ai_service, kb_service, project_ai_config, retrieval_service
from app.services.task_engine import engine as engine_module
from app.services.task_engine.registry import get_handler
from tests.helpers import Gate, create_ai_config, create_project, fake_vector, register, wait_task

MESSAGES = engine_module.COLLABORATION_FAILURE_MESSAGES
OWNER_KEY = "sk-owner-private-0101"
EDITOR_KEY = "sk-editor-own-0102"
OWNER_EMBED_KEY = "sk-owner-embed-0103"
STOPPED = (
    MESSAGES["collaboration_revoked"], MESSAGES["collaboration_model_revoked"],
    MESSAGES["collaboration_account_unavailable"], "任务输出授权已变化，已停止",
    "权限或模型授权已变化，已停止发送", "任务执行已停止",
)
KINDS = ("character.generate", "organization.generate", "career.generate", "relations.parse", "relations.generate")
KB_TEXT = "# 天剑宗\n\n天剑宗弟子遵守师承誓约，秘传剑阵名为北斗。"


def _answer(kind: str, *, name: str = "新角色甲") -> str:
    if kind == "character.generate":
        return json.dumps({"character": {"name": name, "profile": "剑修"}, "relationships": []}, ensure_ascii=False)
    if kind == "organization.generate":
        return json.dumps({"organization": {"name": "北斗剑阁"}, "members": []}, ensure_ascii=False)
    if kind == "career.generate":
        return json.dumps({"careers": [{"name": "阵师", "kind": "主修", "stages": ["一阶", "二阶"]}]},
                          ensure_ascii=False)
    if kind == "relations.parse":
        return json.dumps({"relationships": [{"target": "乙", "self_role": "徒弟", "target_role": "师父"}]},
                          ensure_ascii=False)
    return json.dumps({"relationships": []}, ensure_ascii=False)


@pytest.fixture
def settings_overrides():
    return {"allow_private_ai_endpoints": True, "auto_analyze_enabled": False}


class World:
    def __init__(self, project, chars, owner, guest, owner_config, editor_config, guest_client):
        self.project, self.chars, self.owner, self.guest = project, chars, owner, guest
        self.owner_config, self.editor_config, self.guest_client = owner_config, editor_config, guest_client

    @property
    def base(self) -> str:
        return f"/api/v1/projects/{self.project['id']}"


async def _world(owner, guest, *, role="editor", allow_owner_ai=False, accept=True, editor_model=True):
    owner_user = await register(owner, "e4g3b-owner")
    guest_user = await register(guest, "e4g3b-guest")
    project = await create_project(owner)
    base = f"/api/v1/projects/{project['id']}"
    chars = []
    for name, notes in (("甲", "师父是乙"), ("乙", "")):
        created = await owner.post(f"{base}/characters", json={"name": name, "relations_text": notes})
        assert created.status_code == 201, created.text
        chars.append(created.json()["id"])
    owner_config = await create_ai_config(owner, name="作者模型", model="owner-model", api_key=OWNER_KEY)
    editor_config = (await create_ai_config(guest, name="本人模型", model="editor-model", api_key=EDITOR_KEY)
                     if editor_model else None)
    invite = await owner.post(f"{base}/collaborators", json={
        "username": "e4g3b-guest", "role": role, "allow_owner_ai": allow_owner_ai,
    })
    assert invite.status_code == 201, invite.text
    if accept:
        accepted = await guest.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": invite.json()["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
    return World(project, chars, owner_user, guest_user, owner_config, editor_config, guest)


def _start(client, world, kind, **body):
    base = world.base
    if kind == "character.generate":
        return client.post(f"{base}/characters/generate", json=body)
    if kind == "organization.generate":
        return client.post(f"{base}/organizations/generate", json=body)
    if kind == "career.generate":
        return client.post(f"{base}/careers/generate", json={"count": 1, **body})
    if kind == "relations.parse":
        return client.post(f"{base}/characters/{world.chars[0]}/parse-relations", json=body)
    return client.post(f"{base}/relationships/generate", json={"character_ids": world.chars, "count": 1, **body})


async def _member(app, world):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world.project["id"], ProjectCollaborator.user_id == world.guest["id"],
        ))


async def _change(app, owner, world, change):
    member = await _member(app, world)
    url = f"{world.base}/collaborators/{world.guest['id']}"
    if change == "revoked":
        response = await owner.delete(url, params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text
    elif change == "viewer":
        response = await owner.patch(url, json={"expected_revision": member.revision, "role": "viewer"})
        assert response.status_code == 200, response.text
    elif change == "owner_ai":
        response = await owner.patch(url, json={"expected_revision": member.revision, "allow_owner_ai": False})
        assert response.status_code == 200, response.text
    elif change == "owner_ai_direct":
        # 直接关闭模型授权、不递增 revision：只能靠各检查点直接核对 allow_owner_ai。
        async with app.state.sessionmaker() as db:
            await db.execute(update(ProjectCollaborator).where(ProjectCollaborator.id == member.id).values(
                allow_owner_ai=False,
            ))
            await db.commit()
    elif change == "disabled":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == world.guest["id"]).values(is_active=False))
            await db.commit()
    elif change == "actor_token":
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == world.guest["id"]).values(
                token_version=User.token_version + 1,
            ))
            await db.commit()
    elif change == "reinvite":
        response = await owner.delete(url, params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text
        invite = await owner.post(f"{world.base}/collaborators", json={
            "username": "e4g3b-guest", "role": "editor", "allow_owner_ai": member.allow_owner_ai,
        })
        assert invite.status_code == 201, invite.text
        accepted = await world.guest_client.post(
            f"/api/v1/collaboration/invitations/project/{invite.json()['invitation_id']}/accept",
            json={"expected_revision": invite.json()["revision"]},
        )
        assert accepted.status_code == 200, accepted.text
    else:
        raise AssertionError(change)


def _elsewhere(app, owner, world, change, changes):
    """撤权在独立任务中执行（与真实服务器一致）：执行体被通知取消时不能打断撤权请求自身的收尾。"""
    async def run():
        work = asyncio.ensure_future(_change(app, owner, world, change))
        changes.append(work)
        await asyncio.shield(work)

    return run


async def _terminal(app, task_id, within=15.0):
    async with asyncio.timeout(within):
        while True:
            async with app.state.sessionmaker() as db:
                row = await db.get(Task, task_id)
                if row.status not in ("queued", "running") and row.execution_key is None:
                    return row
            await asyncio.sleep(0.02)


async def _task_count(app):
    async with app.state.sessionmaker() as db:
        return await db.scalar(select(func.count(Task.id)))


async def _entity_counts(app):
    async with app.state.sessionmaker() as db:
        return tuple([await db.scalar(select(func.count(model.id))) for model in (Character, Organization,
                                                                                 Relationship)])


async def _assert_released(app, client, world, fake_llm, kind="relations.parse"):
    """活动资源键（relations.parse 为 relparse:{cid}）已释放：作者可立即对同一角色发起解析并成功。"""
    fake_llm.push(_answer(kind))
    author = await _start(client, world, kind)
    assert author.status_code == 202, author.text
    row = await _terminal(app, author.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.owner["id"] and "_collaboration_grant" not in row.input


# 历史 shared:v2 空间键的字面形状（E6-f 删除生成原语）；只用来模拟「不属作者现用空间」的遗留向量。
LEGACY_SHARED_V2_SPACE = "shared:v2:" + "0" * 64


async def _index_owner_kb_in_shared_space(app, owner, world):
    """作者上传资料（尚无向量模型时为关键词索引），随后配置作者向量模型并把该分块写成非作者现用空间的
    历史 ``shared:v2`` 键（该空间键原语已于 E6-f 删除，生产无写入方；此处以字面键模拟遗留数据）。
    候选不属作者旧键空间，授权协作执行不发送查询向量、退回关键词。"""
    upload = await owner.post(f"{world.base}/kb/documents", files={
        "file": ("资料.md", KB_TEXT.encode(), "text/markdown"),
    })
    assert upload.status_code == 202, upload.text
    assert (await wait_task(owner, upload.json()["task"]))["status"] == "succeeded"
    await create_ai_config(owner, name="作者向量", model="owner-embed", model_type="embedding",
                           api_key=OWNER_EMBED_KEY)
    async with app.state.sessionmaker() as db:
        selection = await project_ai_config.resolve_shared_embedding(
            db, world.owner["id"], world.project["id"], cipher=app.state.cipher, allow_private=True,
        )
        assert selection.mode == "vector"
        vector = retrieval_service.normalize_vector(fake_vector("天剑宗师承"))
        row = await db.scalar(select(RetrievalChunk).where(
            RetrievalChunk.project_id == world.project["id"], RetrievalChunk.source == "kb",
        ))
        row.embedding = retrieval_service.encode_vector(vector)
        row.embedding_key = retrieval_service.make_embedding_key(LEGACY_SHARED_V2_SPACE, len(vector))
        row.version += 1
        await db.commit()
    retrieval_service.invalidate(world.project["id"])


async def _index_owner_kb_in_author_space(app, owner, world):
    """作者上传资料后配置作者向量模型，把全部分块写成作者旧键空间（provider:model:dim，与作者 / 授权编辑者
    索引任务产物同形，裁决 1）中的可召回向量；维度与 FakeLLM 一致，授权协作执行的查询向量可直接命中。"""
    upload = await owner.post(f"{world.base}/kb/documents", files={
        "file": ("资料.md", KB_TEXT.encode(), "text/markdown"),
    })
    assert upload.status_code == 202, upload.text
    assert (await wait_task(owner, upload.json()["task"]))["status"] == "succeeded"
    await create_ai_config(owner, name="作者向量", model="owner-embed", model_type="embedding",
                           api_key=OWNER_EMBED_KEY)
    async with app.state.sessionmaker() as db:
        config = await retrieval_service.default_embedding_config(db, world.owner["id"])
        for row in await db.scalars(select(RetrievalChunk).where(
            RetrievalChunk.project_id == world.project["id"], RetrievalChunk.source == "kb",
        )):
            vector = retrieval_service.normalize_vector(fake_vector(row.text))
            row.embedding = retrieval_service.encode_vector(vector)
            row.embedding_key = retrieval_service.make_embedding_key(
                retrieval_service.config_model_key(config), len(vector),
            )
            row.version += 1
        await db.commit()
    retrieval_service.invalidate(world.project["id"])


# ---------------- 成功路径、费用来源与归属 ----------------


@pytest.mark.parametrize("kind", KINDS)
async def test_editor_entity_ai_uses_own_model_and_author_shared_vectors(app, client, make_client, fake_llm, kind):
    """E6-d 翻转（原 test_editor_entity_ai_uses_own_model_and_keyword_retrieval 的零向量断言）：作者已授权且有
    向量配置时，协作者实体检索用受理冻结的作者共享向量（费用来源=作者，读作者旧键空间）；本人向量配置不参与。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    await create_ai_config(guest, name="本人向量", model="editor-embed", model_type="embedding")
    await _index_owner_kb_in_author_space(app, client, world)
    before = await _entity_counts(app)
    calls_before = len(fake_llm.calls)
    fake_llm.push(_answer(kind))
    created = await _start(guest, world, kind, instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    task = created.json()
    detail = await guest.get(f"/api/v1/tasks/{task['id']}", params={"task_key": task["task_key"]})
    assert detail.status_code == 200, detail.text
    row = await _terminal(app, task["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.guest["id"] and row.project_id == world.project["id"]
    assert row.input["_collaboration_grant"]["invitation_id"]
    # 本人有文本配置：即使作者授权开启也只用本人密钥。
    sent = fake_llm.calls[calls_before:]
    assert [call["api_key"] for call in sent] == [EDITOR_KEY]
    assert (row.input["_project_model"]["source"], row.input["_project_model"]["config_owner_id"]) == (
        "actor", world.guest["id"],
    )
    selection = row.input["_shared_embedding"]
    assert selection["mode"] == "vector" and selection["model"]["source"] == "owner"
    assert selection["model"]["config_owner_id"] == world.owner["id"]
    if kind == "relations.parse":
        assert fake_llm.embed_calls == []  # 不注入参考资料，不检索
    else:
        # 只调用作者向量模型做一次查询嵌入；混合检索命中作者资料。
        assert len(fake_llm.embed_calls) == 1 and fake_llm.embed_calls[0]["model"] == "owner-embed"
        assert "北斗" in str(sent[0]["messages"])
    if kind == "character.generate":
        assert row.result["draft"]["name"] == "新角色甲"
    elif kind == "organization.generate":
        assert row.result["draft"]["name"] == "北斗剑阁"
    elif kind == "career.generate":
        assert [d["name"] for d in row.result["drafts"]] == ["阵师"]
    elif kind == "relations.parse":
        assert row.result["character_id"] == world.chars[0] and row.resource_key == f"relparse:{world.chars[0]}"
    else:
        assert row.result["character_ids"] == world.chars
    # 只产出草稿：不写任何设定，也不派生其他任务（含向量索引任务）。
    assert await _entity_counts(app) == before
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(
            Task.id != row.id, Task.user_id == world.guest["id"],
        )) == 0
    # 已完成的草稿不因随后撤权回滚（Task 仍是本人历史）。
    await _change(app, client, world, "revoked")
    async with app.state.sessionmaker() as db:
        assert (await db.get(Task, row.id)).status == "succeeded"


@pytest.mark.parametrize("space", ["author", "shared_v2"])
async def test_entity_retrieval_reads_only_author_legacy_space(app, client, make_client, fake_llm, space):
    """未授权编辑者：作者旧键空间有可召回向量、本人也有向量配置，仍只走关键词、任何向量模型零调用。
    授权编辑者遇到非作者现用空间（shared:v2 键，裁决 1 后无生产写入方）：不发送查询向量，退回关键词。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=space == "shared_v2")
    await create_ai_config(guest, name="本人向量", model="editor-embed", model_type="embedding")
    if space == "author":
        await _index_owner_kb_in_author_space(app, client, world)
    else:
        await _index_owner_kb_in_shared_space(app, client, world)
    fake_llm.push(_answer("character.generate"))
    created = await _start(guest, world, "character.generate", instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.input["_shared_embedding"]["mode"] == ("keyword" if space == "author" else "vector")
    assert fake_llm.embed_calls == []
    assert "北斗" in str(fake_llm.calls[-1]["messages"])


@pytest.mark.parametrize("change", ["owner_ai_direct", "revoked", "viewer"])
async def test_editor_entity_vector_query_fails_closed_before_send(app, client, make_client, fake_llm, change):
    """查询向量已过 prepare_provider、尚未进入真实发送门禁时撤权 / 降级 / 直接改库关授权：零向量发送、
    零文本调用、无结果（无通知的直接改库由发送租约内重验拦截）。"""
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    await _index_owner_kb_in_author_space(app, client, world)
    gate = Gate()
    fake_llm.embed_before_gates = [gate]
    calls_before = len(fake_llm.calls)
    fake_llm.push(_answer("character.generate"))
    created = await _start(guest, world, "character.generate", instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    changes: list = []
    await _elsewhere(app, client, world, change, changes)()
    gate.release()
    row = await _terminal(app, created.json()["id"])
    assert row.status in ("failed", "cancelled"), row.status
    assert row.result is None
    assert fake_llm.embed_calls == [] and len(fake_llm.calls) == calls_before


async def test_editor_entity_owner_model_fallback_records_cost_source(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=False)
    fake_llm.push(_answer("character.generate"))
    created = await _start(guest, world, "character.generate")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    assert row.user_id == world.guest["id"]
    assert [call["api_key"] for call in fake_llm.calls] == [OWNER_KEY]
    model = row.input["_project_model"]
    assert (model["source"], model["config_owner_id"], model["config_id"]) == (
        "owner", world.owner["id"], world.owner_config["id"],
    )
    detail = await guest.get(f"/api/v1/tasks/{row.id}", params={"task_key": row.statistics_key})
    # 任务详情不泄露作者配置身份与私有绑定。
    assert detail.status_code == 200 and detail.json().get("ai_config_id") is None
    assert "_project_model" not in detail.text and OWNER_KEY not in detail.text


@pytest.mark.parametrize("case", ["no_model_no_grant", "broken_own_model", "explicit_owner_config"])
async def test_editor_entity_model_errors_never_fall_back(app, client, make_client, fake_llm, case):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=case != "no_model_no_grant",
                         editor_model=case != "no_model_no_grant")
    body = {}
    if case == "broken_own_model":
        # 本人配置损坏：即使作者授权开启也不回退作者配置。
        async with app.state.sessionmaker() as db:
            await db.execute(update(AIConfig).where(AIConfig.id == world.editor_config["id"]).values(
                api_key_enc="not-a-valid-token",
            ))
            await db.commit()
    elif case == "explicit_owner_config":
        body = {"ai_config_id": world.owner_config["id"]}
    for kind in KINDS:
        response = await _start(guest, world, kind, **body)
        assert response.status_code in ((400, 404) if case == "explicit_owner_config" else (400,)), response.text
    assert await _task_count(app) == 0 and fake_llm.calls == [] and fake_llm.embed_calls == []


async def test_editor_entity_hides_owner_private_style_and_template(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True)
    detail = (await client.get("/api/v1/prompt-templates/entity.character")).json()
    saved = await client.put("/api/v1/prompt-templates/entity.character", json={
        "system": detail["default_system"] + "\n作者私有模板标记壬癸", "user": detail["default_user"],
    })
    assert saved.status_code == 200, saved.text
    async with app.state.sessionmaker() as db:
        style = WritingStyle(user_id=world.owner["id"], name="作者私有风格", description="私有描述甲乙丙",
                             extra="作者私有要求丁戊己")
        db.add(style)
        await db.flush()
        await db.execute(update(Project).where(Project.id == world.project["id"]).values(default_style_id=style.id))
        await db.commit()
    fake_llm.push(_answer("character.generate"))
    created = await _start(guest, world, "character.generate")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded", row.error
    sent = str(fake_llm.calls[0]["messages"])
    assert fake_llm.calls[0]["api_key"] == EDITOR_KEY
    for secret in ("作者私有模板标记壬癸", "私有描述甲乙丙", "作者私有风格"):
        assert secret not in sent
    # 作者自己的生成仍使用作者模板与风格（正对照）。
    fake_llm.push(_answer("character.generate"))
    mine = await _start(client, world, "character.generate")
    assert mine.status_code == 202, mine.text
    assert (await _terminal(app, mine.json()["id"])).status == "succeeded"
    own = str(fake_llm.calls[1]["messages"])
    assert "作者私有模板标记壬癸" in own and "私有描述甲乙丙" in own


@pytest.mark.parametrize("case", ["viewer", "pending", "outsider", "revoked", "admin_outsider", "admin_viewer"])
async def test_non_editors_cannot_start_entity_ai(app, client, make_client, fake_llm, case):
    guest = make_client()
    world = await _world(
        client, guest, role="viewer" if case in ("viewer", "admin_viewer") else "editor",
        accept=case not in ("pending", "outsider", "admin_outsider"), allow_owner_ai=True,
    )
    if case in ("outsider", "admin_outsider", "revoked"):
        member = await _member(app, world)
        response = await client.delete(f"{world.base}/collaborators/{world.guest['id']}",
                                       params={"expected_revision": member.revision})
        assert response.status_code == 204, response.text
    if case.startswith("admin"):
        # 管理员人工读取/编辑权限不能旁路协作 AI。
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == world.guest["id"]).values(is_admin=True))
            await db.commit()
    expected = 403 if case in ("viewer", "admin_viewer") else 404
    for kind in KINDS:
        response = await _start(guest, world, kind)
        assert response.status_code == expected, (kind, response.text)
        assert "师父是乙" not in response.text
    assert await _task_count(app) == 0 and fake_llm.calls == [] and fake_llm.embed_calls == []


async def test_editor_cannot_target_characters_of_another_book(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    other = await create_project(client, title="外部作品")
    secret = []
    for name in ("外书秘密甲", "外书秘密乙"):
        created = await client.post(f"/api/v1/projects/{other['id']}/characters",
                                    json={"name": name, "relations_text": "外书秘密备注"})
        assert created.status_code == 201, created.text
        secret.append(created.json()["id"])
    parsed = await guest.post(f"{world.base}/characters/{secret[0]}/parse-relations", json={})
    assert parsed.status_code == 404 and "外书秘密" not in parsed.text, parsed.text
    mixed = await guest.post(f"{world.base}/relationships/generate",
                             json={"character_ids": [world.chars[0], secret[0]], "count": 1})
    assert mixed.status_code == 404 and "外书秘密" not in mixed.text, mixed.text
    # 外书本身：协作者无任何授权。
    outside = await guest.post(f"/api/v1/projects/{other['id']}/characters/generate", json={})
    assert outside.status_code == 404, outside.text
    assert await _task_count(app) == 0 and fake_llm.calls == []


async def _hide(client, world, ident, **values):
    url = f"{world.base}/setting-entries/character/{ident}/ai-settings"
    current = await client.get(url)
    assert current.status_code == 200, current.text
    response = await client.patch(url, json={
        "expected_ai_visibility_revision": current.json()["ai_visibility_revision"], **values,
    })
    assert response.status_code == 200, response.text


async def test_editor_entity_prompt_respects_owner_ai_visibility(app, client, make_client, fake_llm):
    """作者隐藏的字段、never 条目不进入协作者提示词；显式选中 never / 隐藏主字段的入口 400 且零任务。"""
    guest = make_client()
    world = await _world(client, guest)
    for ident, personality in zip(world.chars, ("甲隐藏性格密语", "乙公开性格"), strict=True):
        patched = await client.patch(f"{world.base}/characters/{ident}", json={"personality": personality})
        assert patched.status_code == 200, patched.text
    never = await client.post(f"{world.base}/characters", json={"name": "丙永不发送", "profile": "丙档案密语"})
    assert never.status_code == 201, never.text
    await _hide(client, world, world.chars[0], ai_hidden_builtin_fields=["personality"])
    await _hide(client, world, never.json()["id"], ai_mode="never")
    fake_llm.push(_answer("relations.generate"))
    created = await _start(guest, world, "relations.generate")
    assert created.status_code == 202, created.text
    assert (await _terminal(app, created.json()["id"])).status == "succeeded"
    fake_llm.push(_answer("character.generate"))
    created = await _start(guest, world, "character.generate", instruction="写一个与丙永不发送有关的人")
    assert created.status_code == 202, created.text
    assert (await _terminal(app, created.json()["id"])).status == "succeeded"
    relations_prompt, character_prompt = (str(call["messages"]) for call in fake_llm.calls)
    assert "乙公开性格" in relations_prompt  # 正对照：未隐藏字段照常发送。
    for prompt in (relations_prompt, character_prompt):
        assert "甲隐藏性格密语" not in prompt and "丙档案密语" not in prompt
    assert "丙永不发送" not in relations_prompt
    before = await _task_count(app)
    selected_never = await guest.post(f"{world.base}/relationships/generate", json={
        "character_ids": [world.chars[0], never.json()["id"]], "count": 1,
    })
    assert selected_never.status_code == 400, selected_never.text
    assert selected_never.json().get("code") == "setting_not_available"
    await _hide(client, world, world.chars[0], ai_hidden_builtin_fields=["personality", "relations_text"])
    parse_hidden = await _start(guest, world, "relations.parse")
    assert parse_hidden.status_code == 400 and "师父是乙" not in parse_hidden.text, parse_hidden.text
    assert parse_hidden.json().get("code") == "setting_not_available"
    assert await _task_count(app) == before and len(fake_llm.calls) == 2


async def test_editor_entity_ai_rejected_once_project_is_recycled(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    assert (await client.delete(world.base)).status_code == 204
    for kind in KINDS:
        response = await _start(guest, world, kind)
        assert response.status_code == 404 and "师父是乙" not in response.text, (kind, response.text)
    assert await _task_count(app) == 0 and fake_llm.calls == []


@pytest.mark.parametrize("change", ["revoked", "viewer", "reinvite", "owner_ai"])
async def test_change_between_preflight_and_admission_creates_no_task(app, client, make_client, fake_llm,
                                                                    monkeypatch, change):
    """锁外预检（ai.use 短读、模型、DNS）完成后、受理事务前授权变化：受理锁内按原 grant 重验，零任务零调用。"""
    from app.services.task_engine import admission as admission_module

    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai")
    original = admission_module.prepare_task_admission
    fired = []

    async def change_after_preflight(*args, **kwargs):
        proof = await original(*args, **kwargs)
        if not fired:
            fired.append(change)
            await _change(app, client, world, change)
        return proof

    monkeypatch.setattr(admission_module, "prepare_task_admission", change_after_preflight)
    response = await _start(guest, world, "relations.parse")
    assert fired == [change]
    assert response.status_code in (400, 403, 404, 409), response.text
    assert await _task_count(app) == 0 and fake_llm.calls == [] and fake_llm.embed_calls == []


# ---------------- 受理后领取前、构建前、检索前、发送前 ----------------


@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai", "owner_ai_direct", "disabled", "reinvite"])
async def test_change_after_admission_before_claim_fails_closed(app, client, make_client, fake_llm, change):
    await app.state.task_engine.stop()
    guest = make_client()
    owner_model = change.startswith("owner_ai")
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_model)
    created = await _start(guest, world, "relations.parse")
    assert created.status_code == 202, created.text
    await _change(app, client, world, change)
    await app.state.task_engine.start()
    row = await _terminal(app, created.json()["id"])
    expected = {"disabled": "collaboration_account_unavailable", "owner_ai_direct": "collaboration_model_revoked"}
    assert row.status == "failed" and row.error == MESSAGES[expected.get(change, "collaboration_revoked")]
    assert row.result is None and fake_llm.calls == []
    await _assert_released(app, client, world, fake_llm)


@pytest.mark.parametrize("stage", ["build", "retrieval", "send"])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct"])
async def test_change_after_claim_before_first_send_never_sends(app, client, make_client, fake_llm, monkeypatch,
                                                                stage, change):
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    if stage == "retrieval":
        upload = await client.post(f"{world.base}/kb/documents", files={
            "file": ("资料.md", KB_TEXT.encode(), "text/markdown"),
        })
        assert upload.status_code == 202, upload.text
        assert (await wait_task(client, upload.json()["task"]))["status"] == "succeeded"
    handler = get_handler("character.generate")
    original_build = type(handler).build_job
    original_knowledge = entity_ai_service._knowledge_for_execution
    original_job = entity_ai_service.build_entity_job
    original_search = kb_service.search_hits
    fired, changes, searched = asyncio.Event(), [], []
    revoke = _elsewhere(app, client, world, change, changes)

    async def before_build(self, ctx, task, **kwargs):
        if not fired.is_set():
            fired.set()
            await revoke()
        return await original_build(self, ctx, task, **kwargs)

    async def before_knowledge(*args, **kwargs):
        if not fired.is_set():
            fired.set()
            await revoke()
        return await original_knowledge(*args, **kwargs)

    async def after_job_build(*args, **kwargs):
        job = await original_job(*args, **kwargs)
        if not fired.is_set():
            fired.set()
            await revoke()
        return job

    async def recording_search(*args, **kwargs):
        searched.append(args[2] if len(args) > 2 else kwargs.get("query"))
        return await original_search(*args, **kwargs)

    monkeypatch.setattr(kb_service, "search_hits", recording_search)
    if stage == "build":
        monkeypatch.setattr(type(handler), "build_job", before_build)
    elif stage == "retrieval":
        monkeypatch.setattr(entity_ai_service, "_knowledge_for_execution", before_knowledge)
    else:
        monkeypatch.setattr(entity_ai_service, "build_entity_job", after_job_build)
    fake_llm.push(_answer("character.generate"))
    created = await _start(guest, world, "character.generate", instruction="天剑宗师承")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    await asyncio.gather(*changes)
    assert fired.is_set()
    assert row.status == "failed" and row.output_text == "" and row.result is None, row.error
    assert fake_llm.calls == [] and fake_llm.embed_calls == []
    if stage in ("build", "retrieval"):
        # 检索前锁内重验失败：关键词检索都未执行，不读取共享资料。
        assert searched == []


async def test_build_job_rejects_revoked_grant_without_notification(app, client, make_client, fake_llm, monkeypatch):
    """构建前的锁内重验本身即失败关闭（不依赖本进程通知取消执行体）。"""
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest)
    handler = get_handler("relations.parse")
    original_build = type(handler).build_job
    seen = []

    async def before_build(self, ctx, task, **kwargs):
        if not seen:
            seen.append(task.id)
            await _change(app, client, world, "revoked")
        return await original_build(self, ctx, task, **kwargs)

    monkeypatch.setattr(type(handler), "build_job", before_build)
    fake_llm.push(_answer("relations.parse"))
    created = await _start(guest, world, "relations.parse")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert seen and row.status == "failed" and row.result is None, row.error
    assert fake_llm.calls == []
    await _assert_released(app, client, world, fake_llm)


# ---------------- 发送中、完成发布与终态前 ----------------


@pytest.mark.parametrize("notify", [True, False])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai"])
async def test_change_during_send_discards_drafts(app, client, make_client, fake_llm, monkeypatch, change, notify):
    """notify=False 模拟跨实例/未收到通知：只能靠逐帧发布核验与终态锁内重验拒绝旧结果。"""
    if not notify:
        monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    owner_ai = change == "owner_ai"
    world = await _world(client, guest, allow_owner_ai=True, editor_model=not owner_ai)
    gate = Gate()
    fake_llm.push(['{"character":{"name":"旧授权', gate, '下的角色","profile":"秘密"},"relationships":[]}'])
    created = await _start(guest, world, "character.generate")
    assert created.status_code == 202, created.text
    await gate.wait_reached(10)
    await _change(app, client, world, change)
    gate.release()
    row = await _terminal(app, created.json()["id"])
    assert row.status == "failed" and row.error in STOPPED, row.error
    assert "下的角色" not in (row.output_text or "") and row.result is None
    assert len(fake_llm.calls) == 1


@pytest.mark.parametrize("hook", ["done_event", "finalization"])
@pytest.mark.parametrize("change", ["revoked", "viewer", "owner_ai_direct", "actor_token"])
async def test_change_before_publish_fails_closed_without_running_residue(app, client, make_client, fake_llm,
                                                                          monkeypatch, hook, change):
    """模型输出已完整、完成事件发布或终态写入前授权变化：实体草稿没有已提交的业务副作用，终态失败且无 result，
    不得停在 running，活动资源键（relparse:{cid}）释放、无项目清理失败残留。"""
    monkeypatch.setattr(app.state.task_engine, "notify_collaboration_change", lambda *a, **k: None)
    guest = make_client()
    world = await _world(client, guest, allow_owner_ai=True, editor_model=change != "owner_ai_direct")
    engine = app.state.task_engine
    engine_cls = type(engine)
    fired = []
    if hook == "done_event":
        original_event = engine_cls._event_task

        async def before_done(self, db, execution, current, event):
            if event == "done" and not fired:
                fired.append(hook)
                await asyncio.shield(asyncio.ensure_future(_change(app, client, world, change)))
            return await original_event(self, db, execution, current, event)

        monkeypatch.setattr(engine_cls, "_event_task", before_done)
    else:
        # 完成事件已核验发布、终态事务开始前：撤权不能放进终态事务内部（该事务已持写锁，会与撤权互等）。
        original_complete = engine_cls.complete_state

        async def before_final(self, task, **kwargs):
            if (not fired and task is not None and task.kind == "relations.parse"
                    and kwargs.get("status") == "succeeded"):
                fired.append(hook)
                await asyncio.shield(asyncio.ensure_future(_change(app, client, world, change)))
            return await original_complete(self, task, **kwargs)

        monkeypatch.setattr(engine_cls, "complete_state", before_final)
    fake_llm.push(_answer("relations.parse"))
    created = await _start(guest, world, "relations.parse")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired == [hook]
    assert row.status == "failed" and row.result is None, (row.status, row.error)
    assert row.error in STOPPED, row.error
    assert len(fake_llm.calls) == 1
    assert not engine.has_cleanup_failure(world.project["id"])
    await _assert_released(app, client, world, fake_llm)


@pytest.mark.parametrize("change", ["actor_token", "visibility", "entity_source"])
async def test_author_finalization_window_change_fails_closed(app, client, make_client, fake_llm, monkeypatch,
                                                              change):
    """作者路径受 `_DRAFT_ONLY_KINDS` 扩展影响的范围：完成事件已发布、终态事务前作者改密、AI 可见性版本变化或
    原提示词来源设定被修改（任何令作者 verify_finalization 抛错的变化）由「停在 running、占 relparse 资源键、
    登记清理失败」改为无结果 failed。窗口外的作者普通成功不受影响
    （见 test_author_entity_unchanged_alongside_editor）。"""
    guest = make_client()
    world = await _world(client, guest)
    engine = app.state.task_engine
    engine_cls = type(engine)
    original_complete = engine_cls.complete_state
    fired = []

    async def bump():
        async with app.state.sessionmaker() as db:
            if change == "actor_token":
                await db.execute(update(User).where(User.id == world.owner["id"]).values(
                    token_version=User.token_version + 1,
                ))
            elif change == "visibility":
                await db.execute(update(Project).where(Project.id == world.project["id"]).values(
                    ai_visibility_revision=Project.ai_visibility_revision + 1,
                ))
            else:
                await db.execute(update(Character).where(Character.id == world.chars[0]).values(
                    personality="发送后才改的性格",
                ))
            await db.commit()

    async def before_final(self, task, **kwargs):
        if (not fired and task is not None and task.kind == "relations.parse"
                and kwargs.get("status") == "succeeded"):
            fired.append(change)
            await asyncio.shield(asyncio.ensure_future(bump()))
        return await original_complete(self, task, **kwargs)

    monkeypatch.setattr(engine_cls, "complete_state", before_final)
    fake_llm.push(_answer("relations.parse"))
    created = await _start(client, world, "relations.parse")
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert fired == [change]
    assert row.status == "failed" and row.result is None and row.user_id == world.owner["id"], row.error
    expected = {
        "actor_token": "任务执行已停止",
        "visibility": "任务输出授权已变化，已停止",
        # 发送后原来源设定被人工修改：保留与完成事件核验一致的准确固定文案。
        "entity_source": "设定来源已变化，请重新发起",
    }[change]
    assert row.error == expected
    assert len(fake_llm.calls) == 1
    assert not engine.has_cleanup_failure(world.project["id"])
    if change == "actor_token":
        # 改密后原会话失效，资源键释放由协作者以本人模型发起同一解析来证明。
        fake_llm.push(_answer("relations.parse"))
        other = await _start(guest, world, "relations.parse")
        assert other.status_code == 202, other.text
        assert (await _terminal(app, other.json()["id"])).status == "succeeded"
    else:
        await _assert_released(app, client, world, fake_llm)


# ---------------- 重试与作者链路 ----------------


async def test_editor_retry_keeps_round_and_reinvite_blocks_old_retry(app, client, make_client, fake_llm):
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    created = await _start(guest, world, "character.generate")
    assert created.status_code == 202, created.text
    first = await _terminal(app, created.json()["id"])
    assert first.status == "failed"
    fake_llm.push(_answer("character.generate"))
    retried = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert retried.status_code == 202, retried.text
    second = await _terminal(app, retried.json()["id"])
    assert second.status == "succeeded", second.error
    assert second.user_id == world.guest["id"] and second.retry_of_id == first.id
    assert second.input["_collaboration_grant"] == first.input["_collaboration_grant"]
    assert [call["api_key"] for call in fake_llm.calls] == [EDITOR_KEY, EDITOR_KEY]
    # 再失败一次，然后撤权并重新邀请：旧轮次任务不能借新邀请重试。
    fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    third_created = await _start(guest, world, "character.generate")
    assert third_created.status_code == 202, third_created.text
    third = await _terminal(app, third_created.json()["id"])
    assert third.status == "failed"
    await _change(app, client, world, "reinvite")
    calls = len(fake_llm.calls)
    stale = await guest.post(f"/api/v1/tasks/{third.id}/retry", params={"task_key": third.statistics_key})
    assert stale.status_code in (400, 409), stale.text
    assert len(fake_llm.calls) == calls
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Task.id)).where(Task.retry_of_id == third.id)) == 0
    # viewer 不能重试本人旧任务。
    await _change(app, client, world, "viewer")
    blocked = await guest.post(f"/api/v1/tasks/{first.id}/retry", params={"task_key": first.statistics_key})
    assert blocked.status_code in (400, 403, 404, 409), blocked.text
    assert len(fake_llm.calls) == calls


@pytest.mark.parametrize("kind", KINDS)
async def test_author_entity_unchanged_alongside_editor(app, client, make_client, fake_llm, kind):
    guest = make_client()
    world = await _world(client, guest)
    fake_llm.push(_answer(kind))
    created = await _start(client, world, kind)
    assert created.status_code == 202, created.text
    row = await _terminal(app, created.json()["id"])
    assert row.status == "succeeded" and row.user_id == world.owner["id"], row.error
    assert "_collaboration_grant" not in row.input
    assert [call["api_key"] for call in fake_llm.calls] == [OWNER_KEY]
    assert (row.input["_project_model"]["source"], row.input["_project_model"]["config_owner_id"]) == (
        "actor", world.owner["id"],
    )
    if kind != "relations.parse":
        return
    # 同一角色的解析（relparse:{cid}）作者与协作者互斥：作者排队中时编辑者 409。
    await app.state.task_engine.stop()
    fake_llm.push(_answer(kind))
    queued = await _start(client, world, kind)
    assert queued.status_code == 202, queued.text
    conflict = await _start(guest, world, kind)
    assert conflict.status_code == 409, conflict.text
    await app.state.task_engine.start()
    assert (await _terminal(app, queued.json()["id"])).status == "succeeded"
    assert await _task_count(app) == 2


async def test_author_direct_build_still_requires_ownership(app, client, make_client):
    """无执行身份的直调（普通 guard_provider 分支）仍是严格作者路径：协作者无法进入。"""
    from app.core.errors import NotFoundError

    guest = make_client()
    world = await _world(client, guest)
    async with app.state.sessionmaker() as db:
        with pytest.raises(NotFoundError):
            await entity_ai_service.build_entity_job(
                db, user_id=world.guest["id"], kind="character.generate", project_id=world.project["id"],
                params={}, cipher=app.state.cipher, factory=app.state.provider_factory, ai_timeout=30,
                allow_private=True,
            )


async def test_author_agent_entity_confirmed_through_signed_proposal(app, client, fake_llm):
    """作者 Agent 的实体生成只能经签名提案确认（execute_admitted）受理，行为保持：确认后真实执行并产出草稿。"""
    from tests.helpers import wait_task as wait
    from tests.test_agent_runtime import call, send_task, setup_agent, turn_action

    project, base, url = await setup_agent(client, auto=True)
    fake_llm.push_tools([call("start_task", {"kind": "character.generate", "params": {"instruction": "冷静"}},
                              "start")])
    task_ref = await send_task(client, url)
    task = await wait(client, task_ref, statuses=("paused", "failed"))
    assert task["status"] == "paused", task
    fake_llm.push(_answer("character.generate", name="提案角色"))
    response = await client.post(base + f"/turns/{task_ref['id']}/confirm-all",
                                 json=await turn_action(client, url, task_ref, "confirm-all"))
    assert response.status_code == 200, response.text
    change = response.json()["changes"][-1]
    assert change["status"] == "executed", response.text
    child_id = change["result"]["task_id"]
    children = [item for item in (await client.get("/api/v1/tasks", params={"kind": "character.generate"})).json()
                if item["id"] == child_id]
    assert len(children) == 1
    done = await wait(client, children[0], within=15)
    assert done["status"] == "succeeded", done
    async with app.state.sessionmaker() as db:
        row = await db.get(Task, child_id)
        assert row.user_id == (await db.get(Project, project["id"])).user_id
        assert "_collaboration_grant" not in row.input and row.input["_project_model"]["source"] == "actor"
        assert row.result["draft"]["name"] == "提案角色"
