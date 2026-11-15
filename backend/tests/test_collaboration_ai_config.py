"""阶段 E 内部模型来源与共享向量策略：不调用生产 AI 入口。"""

from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text, update

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError
from app.models import AIConfig, Project, ProjectCollaborator, User
from app.schemas.ai_config import AIConfigCreate, AIConfigUpdate
from app.services import ai_config_service, project_ai_config
from app.services.authorization_service import AuthenticatedActor


@pytest.fixture
async def model_world(app):
    async with app.state.sessionmaker() as db:
        owner = User(username="model-owner", password_hash="test")
        editor = User(username="model-editor", password_hash="test")
        viewer = User(username="model-viewer", password_hash="test", is_admin=True)
        stranger = User(username="model-stranger", password_hash="test", is_admin=True)
        db.add_all((owner, editor, viewer, stranger))
        await db.flush()
        project = Project(user_id=owner.id, title="协作作品")
        db.add(project)
        await db.flush()
        editor_member = ProjectCollaborator(
            project_id=project.id, user_id=editor.id, role="editor", status="accepted", allow_owner_ai=False,
        )
        viewer_member = ProjectCollaborator(
            project_id=project.id, user_id=viewer.id, role="viewer", status="accepted", allow_owner_ai=True,
        )
        configs = [
            AIConfig(user_id=owner.id, name="owner-text", provider="openai_compat", model="owner-text",
                     model_type="text", is_default=True, api_key_enc=app.state.cipher.encrypt("owner-secret")),
            AIConfig(user_id=owner.id, name="owner-image", provider="openai_compat", model="owner-image",
                     model_type="image", is_default=True, api_key_enc=app.state.cipher.encrypt("owner-secret")),
            AIConfig(user_id=owner.id, name="owner-vector", provider="openai_compat", model="owner-vector",
                     model_type="embedding", is_default=True, api_key_enc=app.state.cipher.encrypt("owner-secret")),
        ]
        db.add_all([editor_member, viewer_member, *configs])
        await db.commit()
        return {
            "owner": owner.id, "editor": editor.id, "viewer": viewer.id, "stranger": stranger.id,
            "project": project.id, "owner_text": configs[0].id, "owner_image": configs[1].id,
            "owner_embedding": configs[2].id, "invitation": editor_member.invitation_id,
        }


async def _config(app, user_id, model_type, *, default=True, base_url=None, encrypted=None):
    async with app.state.sessionmaker() as db:
        cfg = AIConfig(
            user_id=user_id, name="actor-config", provider="openai_compat", model="actor-model",
            model_type=model_type, is_default=default, base_url=base_url,
            api_key_enc=encrypted or app.state.cipher.encrypt("actor-secret"),
        )
        db.add(cfg)
        await db.commit()
        return cfg.id


async def _member_change(app, world, **values):
    async with app.state.sessionmaker() as db:
        member = await db.scalar(select(ProjectCollaborator).where(
            ProjectCollaborator.project_id == world["project"], ProjectCollaborator.user_id == world["editor"],
        ))
        for key, value in values.items():
            setattr(member, key, value)
        member.revision += 1
        await db.commit()


async def _resolve(app, world, actor, model_type="text", **kw):
    async with app.state.sessionmaker() as db:
        return await project_ai_config.resolve_project_model(
            db, actor, world["project"], model_type, cipher=app.state.cipher, **kw,
        )


async def _revalidate(app, binding):
    async with app.state.sessionmaker() as db:
        return await project_ai_config.revalidate_project_model(db, binding, cipher=app.state.cipher)


async def _embedding(app, world, actor, **kw):
    async with app.state.sessionmaker() as db:
        return await project_ai_config.resolve_shared_embedding(
            db, actor, world["project"], cipher=app.state.cipher, **kw,
        )


@pytest.mark.parametrize("change", ["revoke_token", "disable"])
async def test_model_config_mutations_revalidate_original_authentication(app, model_world, change):
    """配置门禁等待期间撤销身份时，旧请求不能写入任何配置。"""
    w = model_world
    async with app.state.sessionmaker() as db:
        user = await db.get(User, w["owner"])
        original_version = user.token_version
    async with app.state.sessionmaker() as db:
        values = {"token_version": original_version + 1} if change == "revoke_token" else {"is_active": False}
        await db.execute(update(User).where(User.id == w["owner"]).values(**values))
        await db.commit()

    for action in ("create", "update", "set_default", "delete"):
        async with app.state.sessionmaker() as db:
            db.info["authenticated_actor"] = AuthenticatedActor(w["owner"], original_version)
            with pytest.raises(UnauthorizedError):
                if action == "create":
                    await ai_config_service.create_config(db, app.state.cipher, w["owner"], AIConfigCreate(
                        name="unauthorized", provider="openai_compat", model="unsafe", model_type="embedding",
                    ))
                elif action == "update":
                    await ai_config_service.update_config(db, app.state.cipher, w["owner"], w["owner_text"],
                                                          AIConfigUpdate(model="unauthorized"))
                elif action == "set_default":
                    await ai_config_service.set_default(db, w["owner"], w["owner_text"])
                else:
                    await ai_config_service.delete_config(db, w["owner"], w["owner_text"])
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AIConfig.id).where(AIConfig.name == "unauthorized")) is None
        config = await db.get(AIConfig, w["owner_text"])
        assert config is not None and config.model == "owner-text" and config.is_default


async def test_owner_editor_viewer_admin_and_explicit_personal_config(app, model_world):
    w = model_world
    owner = await _resolve(app, w, w["owner"])
    assert (owner.source, owner.config_id, owner.config_owner_id, owner.grant_identity) == (
        "actor", w["owner_text"], w["owner"], None,
    )
    with pytest.raises(BadRequestError, match="尚未配置文本模型"):
        await _resolve(app, w, w["editor"])
    with pytest.raises(ForbiddenError):
        await _resolve(app, w, w["viewer"])
    with pytest.raises(NotFoundError):
        await _resolve(app, w, w["stranger"])
    own = await _config(app, w["editor"], "text")
    chosen = await _resolve(app, w, w["editor"], config_id=own)
    assert (chosen.source, chosen.config_id, chosen.actor_id, chosen.project_id) == (
        "actor", own, w["editor"], w["project"],
    )
    assert chosen.grant_identity == (w["invitation"], 1)
    await _revalidate(app, chosen)
    with pytest.raises(NotFoundError):
        await _resolve(app, w, w["editor"], config_id=w["owner_text"])
    with pytest.raises(NotFoundError):
        await _resolve(app, w, w["editor"], config_id=999999)
    wrong_type = await _config(app, w["editor"], "image")
    with pytest.raises(BadRequestError, match="所选配置不是文本模型"):
        await _resolve(app, w, w["editor"], config_id=wrong_type)
    with pytest.raises(ValueError):
        await _resolve(app, w, w["owner"], "embedding")


@pytest.mark.parametrize("config_id", [True, False, 0, -1, 2**31, "1"])
async def test_explicit_config_id_requires_positive_database_integer(app, model_world, config_id):
    # bool/字符串不能经 SQLAlchemy 转换为本人恰好存在的配置 ID。
    with pytest.raises(NotFoundError):
        await _resolve(app, model_world, model_world["owner"], config_id=config_id)


async def test_default_latest_personal_before_authorized_fallback(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    fallback = await _resolve(app, w, w["editor"])
    assert (fallback.source, fallback.config_id, fallback.config_owner_id) == ("owner", w["owner_text"], w["owner"])
    assert fallback.grant_identity == (w["invitation"], 2)
    assert str(w["owner_text"]) not in repr(fallback)
    await _revalidate(app, fallback)
    first = await _config(app, w["editor"], "text", default=False)
    latest = await _config(app, w["editor"], "text", default=False)
    await _revalidate(app, fallback)
    assert (await _resolve(app, w, w["editor"])).config_id == latest
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == first).values(is_default=True))
        await db.commit()
    chosen = await _resolve(app, w, w["editor"])
    assert (chosen.source, chosen.config_id) == ("actor", first)
    image = await _resolve(app, w, w["editor"], "image")
    assert (image.source, image.config_id) == ("owner", w["owner_image"])


@pytest.mark.parametrize("invalid", ["cipher", "bad_url", "blocked_literal"])
async def test_invalid_personal_config_fails_without_owner_fallback(app, model_world, invalid):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    own = await _config(
        app, w["editor"], "text", encrypted="invalid-ciphertext" if invalid == "cipher" else None,
        base_url={"cipher": None, "bad_url": "not-a-url", "blocked_literal": "http://127.0.0.1:8080"}[invalid],
    )
    with pytest.raises(BadRequestError):
        await _resolve(app, w, w["editor"])
    with pytest.raises(BadRequestError):
        await _resolve(app, w, w["editor"], config_id=own)


@pytest.mark.parametrize("change", ["revoke", "downgrade", "reinvite", "grant_off", "disable_actor"])
async def test_revalidation_rejects_changed_identity_role_and_owner_grant(app, model_world, change):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    binding = await _resolve(app, w, w["editor"])
    if change == "revoke":
        await _member_change(app, w, status="revoked")
    elif change == "downgrade":
        await _member_change(app, w, role="viewer")
    elif change == "reinvite":
        await _member_change(app, w, invitation_id=str(uuid4()))
    elif change == "grant_off":
        await _member_change(app, w, allow_owner_ai=False)
    else:
        async with app.state.sessionmaker() as db:
            await db.execute(update(User).where(User.id == w["editor"]).values(is_active=False))
            await db.commit()
    with pytest.raises((ForbiddenError, NotFoundError, UnauthorizedError)):
        await _revalidate(app, binding)
    if change == "grant_off":
        await _member_change(app, w, allow_owner_ai=True)
        with pytest.raises(ForbiddenError):
            await _revalidate(app, binding)


async def test_deleted_changed_or_transferred_config_never_switches_key(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    first = await _config(app, w["editor"], "text")
    binding = await _resolve(app, w, w["editor"])
    assert binding.config_id == first
    async with app.state.sessionmaker() as db:
        await db.execute(delete(AIConfig).where(AIConfig.id == first))
        await db.commit()
    with pytest.raises((BadRequestError, NotFoundError)):
        await _revalidate(app, binding)
    assert (await _resolve(app, w, w["editor"])).source == "owner"
    second = await _config(app, w["editor"], "text")
    binding2 = await _resolve(app, w, w["editor"])
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == second).values(
            api_key_enc=app.state.cipher.encrypt("new"),
        ))
        await db.commit()
    with pytest.raises((BadRequestError, ForbiddenError)):
        await _revalidate(app, binding2)
    binding3 = await _resolve(app, w, w["editor"])
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == second).values(user_id=w["owner"]))
        await db.commit()
    with pytest.raises((BadRequestError, ForbiddenError, NotFoundError)):
        await _revalidate(app, binding3)


async def test_shared_embedding_keyword_and_authorized_owner_only(app, model_world):
    w = model_world
    own_vector = await _config(app, w["editor"], "embedding")
    owner = await _embedding(app, w, w["owner"])
    assert owner.mode == "vector" and owner.model.config_id == w["owner_embedding"]
    keyword = await _embedding(app, w, w["editor"])
    assert (keyword.mode, keyword.reason, keyword.model) == ("keyword", "owner_embedding_not_allowed", None)
    async with app.state.sessionmaker() as db:
        await project_ai_config.revalidate_shared_embedding(db, keyword, cipher=app.state.cipher)
    with pytest.raises(ForbiddenError) as denied:
        await _embedding(app, w, w["editor"], require_vector=True)
    assert denied.value.extra["code"] == "owner_embedding_not_allowed"
    with pytest.raises(ForbiddenError):
        await _embedding(app, w, w["viewer"])
    await _member_change(app, w, allow_owner_ai=True)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ForbiddenError):
            await project_ai_config.revalidate_shared_embedding(db, keyword, cipher=app.state.cipher)
    shared = await _embedding(app, w, w["editor"])
    assert shared.mode == "vector" and shared.model.source == "owner"
    assert shared.model.config_id == w["owner_embedding"] and shared.model.config_id != own_vector
    async with app.state.sessionmaker() as db:
        await project_ai_config.revalidate_shared_embedding(db, shared, cipher=app.state.cipher)
    await _member_change(app, w, allow_owner_ai=False)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ForbiddenError):
            await project_ai_config.revalidate_shared_embedding(db, shared, cipher=app.state.cipher)


async def test_shared_embedding_missing_and_deleted_binding(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    original = await _embedding(app, w, w["editor"])
    async with app.state.sessionmaker() as db:
        await db.execute(delete(AIConfig).where(AIConfig.id == w["owner_embedding"]))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises((BadRequestError, NotFoundError)):
            await project_ai_config.revalidate_shared_embedding(db, original, cipher=app.state.cipher)
    keyword = await _embedding(app, w, w["editor"])
    assert keyword.mode == "keyword" and keyword.reason == "embedding_config_missing" and keyword.model is None
    with pytest.raises(BadRequestError, match="尚未配置向量模型"):
        await _embedding(app, w, w["editor"], require_vector=True)


async def test_shared_vector_binding_expires_when_default_or_latest_changes(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    old = await _embedding(app, w, w["editor"])
    second = await _config(app, w["owner"], "embedding", default=False)
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == w["owner_embedding"]).values(is_default=False))
        await db.execute(update(AIConfig).where(AIConfig.id == second).values(is_default=True))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(BadRequestError, match="项目向量模型已变化"):
            await project_ai_config.revalidate_shared_embedding(db, old, cipher=app.state.cipher)
    selected = await _embedding(app, w, w["editor"])
    assert selected.model.config_id == second
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == second).values(is_default=False))
        await db.commit()
    old_latest = await _embedding(app, w, w["editor"])
    latest = await _config(app, w["owner"], "embedding", default=False)
    async with app.state.sessionmaker() as db:
        with pytest.raises(BadRequestError, match="项目向量模型已变化"):
            await project_ai_config.revalidate_shared_embedding(db, old_latest, cipher=app.state.cipher)
    assert (await _embedding(app, w, w["editor"])).model.config_id == latest


async def test_text_binding_does_not_switch_when_default_changes(app, model_world):
    w = model_world
    binding = await _resolve(app, w, w["owner"])
    newer = await _config(app, w["owner"], "text", default=False)
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == binding.config_id).values(is_default=False))
        await db.execute(update(AIConfig).where(AIConfig.id == newer).values(is_default=True))
        await db.commit()
    await _revalidate(app, binding)
    assert (await _resolve(app, w, w["owner"])).config_id == newer


async def test_resolution_no_network_commit_or_autoflush(app, model_world, monkeypatch):
    w = model_world
    from app.ai import endpoint_guard

    async def no_dns(*_args):
        raise AssertionError("模型解析不应解析 DNS 或发请求")

    monkeypatch.setattr(endpoint_guard, "_resolve", no_dns)
    async with app.state.sessionmaker() as setup:
        await setup.execute(update(AIConfig).where(AIConfig.id == w["owner_text"]).values(
            base_url="https://example.test/v1",
        ))
        await setup.commit()
    async with app.state.sessionmaker() as db:
        pending = Project(user_id=w["owner"], title="未提交作品")
        db.add(pending)

        async def no_commit():
            raise AssertionError("模型解析不应提交")

        monkeypatch.setattr(db, "commit", no_commit)
        selected = await project_ai_config.resolve_project_model(
            db, w["owner"], w["project"], "text", cipher=app.state.cipher,
        )
        await project_ai_config.revalidate_project_model(db, selected, cipher=app.state.cipher)
        vector = await project_ai_config.resolve_shared_embedding(db, w["owner"], w["project"], cipher=app.state.cipher)
        await project_ai_config.revalidate_shared_embedding(db, vector, cipher=app.state.cipher)
        assert pending.id is None and pending in db.new
        await db.rollback()


async def test_provider_failure_never_reselects_another_config(app, model_world, monkeypatch):
    from app.ai.providers import AIProviderError
    from app.services import ai_config_service

    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    own = await _config(app, w["editor"], "text")
    calls = []
    original = project_ai_config._default_config

    async def observe(db, user_id, model_type):
        calls.append(user_id)
        return await original(db, user_id, model_type)

    monkeypatch.setattr(project_ai_config, "_default_config", observe)
    binding = await _resolve(app, w, w["editor"])
    assert binding.config_id == own and calls == [w["editor"]]

    def failing_provider(*_args):
        raise AIProviderError("模拟提供方校验失败")

    async with app.state.sessionmaker() as db:
        cfg = await db.get(AIConfig, binding.config_id)
        with pytest.raises(BadRequestError, match="模拟提供方校验失败"):
            await ai_config_service.build_provider(cfg, app.state.cipher, failing_provider, 1, allow_private=True)
        await project_ai_config.revalidate_project_model(db, binding, cipher=app.state.cipher)
    assert calls == [w["editor"]]


async def test_actor_binding_revision_and_private_author_error(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    await _config(app, w["editor"], "text")
    personal = await _resolve(app, w, w["editor"])
    await _member_change(app, w, allow_owner_ai=False)
    with pytest.raises(ForbiddenError):
        await _revalidate(app, personal)
    await _member_change(app, w, allow_owner_ai=True)
    async with app.state.sessionmaker() as db:
        await db.execute(delete(AIConfig).where(AIConfig.user_id == w["editor"]))
        await db.execute(update(AIConfig).where(AIConfig.id == w["owner_text"]).values(
            api_key_enc="bad-token", base_url="http://private-author.example",
        ))
        await db.commit()
    with pytest.raises(BadRequestError) as invalid:
        await _resolve(app, w, w["editor"])
    assert invalid.value.detail == "作者共享模型配置不可用，请联系作者"
    assert "private-author" not in str(invalid.value)


async def test_keyword_mode_does_not_inspect_author_config(app, model_world, monkeypatch):
    w = model_world

    async def forbidden_lookup(*_args):
        raise AssertionError("未授权时不应读取作者向量配置")

    monkeypatch.setattr(project_ai_config, "_default_config", forbidden_lookup)
    selection = await _embedding(app, w, w["editor"])
    assert selection.mode == "keyword" and selection.model is None
    with pytest.raises(ForbiddenError) as denied:
        await _embedding(app, w, w["editor"], require_vector=True)
    assert denied.value.extra["code"] == "owner_embedding_not_allowed"


async def test_late_dns_validation_failure_does_not_reselect(app, model_world, monkeypatch):
    from app.ai import endpoint_guard
    from app.services import ai_config_service

    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    own = await _config(app, w["editor"], "text", base_url="https://unresolved.example.test")
    binding = await _resolve(app, w, w["editor"])
    assert binding.source == "actor" and binding.config_id == own
    calls = []

    async def failed_dns(host, port):
        calls.append((host, port))
        raise OSError("no DNS")

    monkeypatch.setattr(endpoint_guard, "_resolve", failed_dns)
    async with app.state.sessionmaker() as db:
        cfg = await db.get(AIConfig, binding.config_id)
        with pytest.raises(BadRequestError, match="无法解析 Base URL"):
            await ai_config_service.build_provider(cfg, app.state.cipher, app.state.provider_factory, 1,
                                                   allow_private=False)
        await project_ai_config.revalidate_project_model(db, binding, cipher=app.state.cipher)
    assert calls == [("unresolved.example.test", 443)]


async def test_sqlite_config_lock_conflict_rolls_back_safely(app, model_world):
    w = model_world
    binding = await _resolve(app, w, w["owner"])
    async with app.state.sessionmaker() as holder, app.state.sessionmaker() as loser:
        await loser.execute(text("PRAGMA busy_timeout = 50"))
        await holder.execute(update(AIConfig).where(AIConfig.id == binding.config_id).values(model="new-model"))
        pending = Project(user_id=w["owner"], title="不能提前刷新的项目")
        loser.add(pending)
        with pytest.raises(ConflictError) as busy:
            await project_ai_config.revalidate_project_model(loser, binding, cipher=app.state.cipher)
        assert busy.value.extra["code"] == "collaboration_busy"
        assert pending not in loser and pending.id is None
        await holder.commit()
    with pytest.raises(BadRequestError, match="模型配置已失效"):
        await _revalidate(app, binding)


async def test_sqlite_shared_vector_guard_blocks_owner_config_create(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    selection = await _embedding(app, w, w["editor"])
    body = AIConfigCreate(name="later-vector", provider="openai_compat", model="later", model_type="embedding")
    async with app.state.sessionmaker() as holder, app.state.sessionmaker() as writer:
        await writer.execute(text("PRAGMA busy_timeout = 50"))
        await project_ai_config.revalidate_shared_embedding(holder, selection, cipher=app.state.cipher)
        pending = Project(user_id=w["owner"], title="冲突回滚")
        writer.add(pending)
        with pytest.raises(ConflictError) as busy:
            await ai_config_service.create_config(writer, app.state.cipher, w["owner"], body)
        assert busy.value.extra["code"] == "collaboration_busy" and pending not in writer and pending.id is None
        await holder.rollback()
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(AIConfig.id).where(AIConfig.name == "later-vector")) is None
        created = await ai_config_service.create_config(db, app.state.cipher, w["owner"], body)
        assert created.model_type == "embedding" and created.is_default is False


@pytest.mark.parametrize("action", ["update", "set_default", "delete"])
async def test_sqlite_config_mutators_respect_shared_vector_guard(app, model_world, action):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    second = await _config(app, w["owner"], "embedding", default=False)
    selection = await _embedding(app, w, w["editor"])
    async with app.state.sessionmaker() as holder, app.state.sessionmaker() as writer:
        await writer.execute(text("PRAGMA busy_timeout = 50"))
        await project_ai_config.revalidate_shared_embedding(holder, selection, cipher=app.state.cipher)
        with pytest.raises(ConflictError) as busy:
            if action == "update":
                await ai_config_service.update_config(
                    writer, app.state.cipher, w["owner"], w["owner_embedding"], AIConfigUpdate(model="changed"),
                )
            elif action == "set_default":
                await ai_config_service.set_default(writer, w["owner"], second)
            else:
                await ai_config_service.delete_config(writer, w["owner"], w["owner_embedding"])
        assert busy.value.extra["code"] == "collaboration_busy"
        await holder.rollback()
    async with app.state.sessionmaker() as db:
        current = await db.get(AIConfig, w["owner_embedding"])
        assert current is not None and current.model == "owner-vector" and current.is_default is True


async def test_binding_roundtrip_owner_and_editor_sources(app, model_world):
    w = model_world
    author = await _resolve(app, w, w["owner"])
    await _member_change(app, w, allow_owner_ai=True)
    fallback = await _resolve(app, w, w["editor"])
    await _config(app, w["editor"], "text")
    personal = await _resolve(app, w, w["editor"])
    vector = await _embedding(app, w, w["editor"])
    for binding in (author, fallback, personal, vector.model):
        data = project_ai_config.serialize_project_model(binding)
        assert set(data) == {
            "version", "project_id", "actor_id", "project_owner_id", "model_type", "source", "config_id",
            "config_owner_id", "grant_identity", "config_fingerprint", "config_instance_key",
        }
        assert data["version"] == 2
        assert str(UUID(data["config_instance_key"])) == data["config_instance_key"]
        assert UUID(data["config_instance_key"]).version == 4
        assert data["config_instance_key"] == binding.config_instance_key
        assert len(data["config_fingerprint"]) == 64
        assert "owner-secret" not in str(data) and "actor-secret" not in str(data)
        restored = project_ai_config.deserialize_project_model(
            data, actor_id=binding.actor_id, project_id=w["project"], project_owner_id=w["owner"],
        )
        assert restored == binding and "secret" not in repr(restored)
        await _revalidate(app, restored)


async def test_binding_deserializer_rejects_tampering_and_wrong_identity(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    binding = await _resolve(app, w, w["editor"])
    valid = project_ai_config.serialize_project_model(binding)

    def changed(key, value):
        data = deepcopy(valid)
        data[key] = value
        return data

    invalid = [
        None, "untrusted", [], changed("version", True), changed("version", 1), changed("version", 3),
        changed("actor_id", True), changed("actor_id", w["owner"]), changed("actor_id", 2**100),
        changed("project_id", True), changed("project_id", w["project"] + 1),
        changed("project_owner_id", w["editor"]), changed("config_id", True), changed("config_id", 0),
        changed("config_owner_id", w["editor"]), changed("source", "unknown"), changed("model_type", "audio"),
        changed("config_fingerprint", "A" * 64), changed("config_fingerprint", "x" * 64),
        changed("config_fingerprint", "a" * 63), changed("grant_identity", None),
        changed("config_instance_key", None), changed("config_instance_key", True),
        changed("config_instance_key", "invalid"), changed("config_instance_key", valid["config_instance_key"].upper()),
        changed("grant_identity", {"invitation_id": w["invitation"], "revision": True}),
        changed("grant_identity", {"invitation_id": w["invitation"], "revision": 0}),
        changed("grant_identity", {"invitation_id": "bad-uuid", "revision": 1}),
        changed("grant_identity", {"invitation_id": w["invitation"], "revision": 1, "extra": "secret"}),
        changed("grant_identity", {"invitation_id": w["invitation"].upper(), "revision": 1}),
        {**valid, "extra": "private-secret"}, {key: value for key, value in valid.items() if key != "source"},
    ]
    for payload in invalid:
        with pytest.raises(BadRequestError) as error:
            project_ai_config.deserialize_project_model(
                payload, actor_id=w["editor"], project_id=w["project"], project_owner_id=w["owner"],
            )
        assert error.value.detail == "模型绑定无效或已失效"
        assert "secret" not in repr(error.value)
    with pytest.raises(BadRequestError):
        project_ai_config.serialize_project_model(replace(binding, config_owner_id=w["editor"]))
    with pytest.raises(BadRequestError):
        project_ai_config.deserialize_project_model(
            valid, actor_id=w["editor"], project_id=w["project"] + 1, project_owner_id=w["owner"],
        )
    # 另一个格式合法的 UUID 也不能在数据库重验时冒充原实例。
    forged = project_ai_config.deserialize_project_model(
        changed("config_instance_key", str(uuid4())), actor_id=w["editor"], project_id=w["project"],
        project_owner_id=w["owner"],
    )
    with pytest.raises(BadRequestError, match="模型配置已失效"):
        await _revalidate(app, forged)


async def test_binding_legacy_author_only_and_db_revalidation(app, model_world):
    w = model_world
    absent = project_ai_config.PROJECT_MODEL_ABSENT
    assert project_ai_config.deserialize_project_model(
        absent, actor_id=w["owner"], project_id=w["project"], project_owner_id=w["owner"], allow_legacy_owner=True,
    ) is None
    for payload, actor, allow in (
        (None, w["owner"], True), (absent, w["owner"], False), (absent, w["editor"], True),
    ):
        with pytest.raises(BadRequestError):
            project_ai_config.deserialize_project_model(
                payload, actor_id=actor, project_id=w["project"], project_owner_id=w["owner"],
                allow_legacy_owner=allow,
            )
    await _member_change(app, w, allow_owner_ai=True)
    binding = await _resolve(app, w, w["editor"])
    payload = project_ai_config.serialize_project_model(binding)
    # 结构合法不等于可信：配置 ID 被换成作者图片配置，数据库重验仍须拒绝。
    payload["config_id"] = w["owner_image"]
    forged = project_ai_config.deserialize_project_model(
        payload, actor_id=w["editor"], project_id=w["project"], project_owner_id=w["owner"],
    )
    with pytest.raises(BadRequestError, match="模型配置已失效"):
        await _revalidate(app, forged)
    # 序列化绑定不能让协作者个人向量模型写入共享向量。
    personal_vector = replace(binding, source="actor", config_owner_id=w["editor"], model_type="embedding")
    with pytest.raises(BadRequestError):
        project_ai_config.serialize_project_model(personal_vector)


@pytest.mark.parametrize("source", ["owner", "editor"])
async def test_v1_model_and_vector_bindings_without_instance_evidence_are_rejected(app, model_world, source):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    actor = w[source]
    model = await _resolve(app, w, actor)
    old_model = project_ai_config.serialize_project_model(model)
    old_model["version"] = 1
    old_model.pop("config_instance_key")
    with pytest.raises(BadRequestError):
        project_ai_config.deserialize_project_model(
            old_model, actor_id=actor, project_id=w["project"], project_owner_id=w["owner"],
            allow_legacy_owner=True,
        )

    vector = await _embedding(app, w, actor)
    old_vector = project_ai_config.serialize_shared_embedding(vector)
    old_vector["version"] = 1
    old_vector["model"]["version"] = 1
    old_vector["model"].pop("config_instance_key")
    with pytest.raises(BadRequestError):
        project_ai_config.deserialize_shared_embedding(
            old_vector, actor_id=actor, project_id=w["project"], project_owner_id=w["owner"],
            task_grant=_task_grant(vector), allow_legacy_owner=True,
        )


def _task_grant(binding):
    if binding.grant_identity is None:
        return project_ai_config.TASK_GRANT_ABSENT
    invitation, revision = binding.grant_identity
    return {"invitation_id": invitation, "revision": revision}


def _read_shared(payload, w, actor, task_grant, **kwargs):
    return project_ai_config.deserialize_shared_embedding(
        payload, actor_id=actor, project_id=w["project"], project_owner_id=w["owner"],
        task_grant=task_grant, **kwargs,
    )


async def test_shared_embedding_serialization_roundtrips_all_sources(app, model_world):
    w = model_world
    owner = await _embedding(app, w, w["owner"])
    no_grant = await _embedding(app, w, w["editor"])
    await _member_change(app, w, allow_owner_ai=True)
    granted = await _embedding(app, w, w["editor"])
    async with app.state.sessionmaker() as db:
        await db.execute(delete(AIConfig).where(AIConfig.id == w["owner_embedding"]))
        await db.commit()
    missing_author = await _embedding(app, w, w["owner"])
    missing_editor = await _embedding(app, w, w["editor"])
    for selection in (owner, no_grant, granted, missing_author, missing_editor):
        data = project_ai_config.serialize_shared_embedding(selection)
        assert set(data) == {
            "version", "project_id", "actor_id", "project_owner_id", "grant_identity", "mode", "reason", "model",
        }
        assert data["version"] == 2
        assert "owner-secret" not in str(data) and "actor-secret" not in str(data)
        restored = _read_shared(data, w, selection.actor_id, _task_grant(selection))
        assert restored == selection and repr(restored) == "<ResolvedSharedEmbedding>"
        assert (data["model"] is None) == (selection.mode == "keyword")
        if selection.mode == "vector":
            assert data["model"]["model_type"] == "embedding"
            assert data["model"]["version"] == 2
            assert data["model"]["config_instance_key"] == selection.model.config_instance_key
        else:
            assert data["reason"] in ("owner_embedding_not_allowed", "embedding_config_missing")


async def test_shared_embedding_deserializer_rejects_tampered_fields_and_grants(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    selection = await _embedding(app, w, w["editor"])
    original = project_ai_config.serialize_shared_embedding(selection)
    grant = _task_grant(selection)

    def changed(key, value):
        payload = deepcopy(original)
        payload[key] = value
        return payload

    wrong_model = deepcopy(original["model"])
    wrong_model["model_type"] = "text"
    wrong_nested_grant = deepcopy(original["model"])
    wrong_nested_grant["grant_identity"]["revision"] += 1
    wrong_owner = deepcopy(original["model"])
    wrong_owner["config_owner_id"] = w["editor"]
    wrong_fingerprint = deepcopy(original["model"])
    wrong_fingerprint["config_fingerprint"] = "private-secret"
    wrong_instance = deepcopy(original["model"])
    wrong_instance["config_instance_key"] = "invalid-instance"
    old_nested_version = deepcopy(original["model"])
    old_nested_version["version"] = 1
    null_nested_grant = deepcopy(original["model"])
    null_nested_grant["grant_identity"] = None
    null_nested_config = deepcopy(original["model"])
    null_nested_config["config_id"] = None
    model_extra = {**original["model"], "api_key": "private-secret"}
    values = [
        None, [], "invalid", changed("version", True), changed("version", 1), changed("version", 3),
        changed("project_id", True),
        changed("project_id", w["project"] + 1), changed("actor_id", True), changed("actor_id", w["owner"]),
        changed("project_owner_id", w["editor"]), changed("mode", True), changed("mode", "unknown"),
        changed("reason", "embedding_config_missing"), changed("model", None), changed("model", wrong_model),
        changed("model", wrong_nested_grant), changed("model", wrong_owner), changed("model", wrong_fingerprint),
        changed("model", wrong_instance), changed("model", old_nested_version),
        changed("model", null_nested_grant), changed("model", null_nested_config), changed("model", model_extra),
        changed("grant_identity", None), changed("grant_identity", {**grant, "extra": "private-secret"}),
        changed("grant_identity", {**grant, "revision": True}), changed("grant_identity", {**grant, "revision": 0}),
        changed("grant_identity", {**grant, "invitation_id": "bad-uuid"}),
        {**original, "extra": "private-secret"},
        *({key: val for key, val in original.items() if key != omitted} for omitted in original),
        *(changed("model", {key: val for key, val in original["model"].items() if key != omitted})
          for omitted in original["model"]),
    ]
    for payload in values:
        with pytest.raises(BadRequestError) as error:
            _read_shared(payload, w, w["editor"], grant)
        assert error.value.detail == "模型绑定无效或已失效" and "private-secret" not in repr(error.value)
    for malformed in (
        project_ai_config.TASK_GRANT_ABSENT, None, {}, {"invitation_id": grant["invitation_id"]},
        {"revision": grant["revision"]}, {**grant, "revision": True}, {**grant, "revision": 0},
        {**grant, "revision": 1.0}, {**grant, "revision": 2**31},
        {**grant, "revision": grant["revision"] + 1}, {**grant, "invitation_id": str(uuid4())},
        {**grant, "invitation_id": grant["invitation_id"].upper()},
        {**grant, "extra": "private-secret"},
    ):
        with pytest.raises(BadRequestError):
            _read_shared(original, w, w["editor"], malformed)
    with pytest.raises(BadRequestError):
        _read_shared(original, w, w["owner"], project_ai_config.TASK_GRANT_ABSENT)
    with pytest.raises(BadRequestError):
        project_ai_config.serialize_shared_embedding(replace(selection, reason="embedding_config_missing"))


async def test_deserialized_shared_vector_never_switches_to_new_default(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    selected = await _embedding(app, w, w["editor"])
    payload = project_ai_config.serialize_shared_embedding(selected)
    restored = _read_shared(payload, w, w["editor"], _task_grant(selected))
    newer = await _config(app, w["owner"], "embedding", default=False)
    async with app.state.sessionmaker() as db:
        await db.execute(update(AIConfig).where(AIConfig.id == w["owner_embedding"]).values(is_default=False))
        await db.execute(update(AIConfig).where(AIConfig.id == newer).values(is_default=True))
        await db.commit()
    async with app.state.sessionmaker() as db:
        with pytest.raises(BadRequestError, match="项目向量模型已变化"):
            await project_ai_config.revalidate_shared_embedding(db, restored, cipher=app.state.cipher)
    assert restored.model.config_id == w["owner_embedding"]
    assert (await _embedding(app, w, w["editor"])).model.config_id == newer


async def test_shared_embedding_keyword_payload_cannot_change_mode(app, model_world):
    w = model_world
    await _member_change(app, w, allow_owner_ai=True)
    async with app.state.sessionmaker() as db:
        await db.execute(delete(AIConfig).where(AIConfig.id == w["owner_embedding"]))
        await db.commit()
    keyword = await _embedding(app, w, w["editor"])
    data = project_ai_config.serialize_shared_embedding(keyword)
    restored = _read_shared(data, w, w["editor"], _task_grant(keyword))
    assert (restored.mode, restored.reason, restored.model) == ("keyword", "embedding_config_missing", None)
    await _config(app, w["owner"], "embedding")
    async with app.state.sessionmaker() as db:
        await project_ai_config.revalidate_shared_embedding(db, restored, cipher=app.state.cipher)
    assert restored.mode == "keyword" and restored.model is None
    for value in (None, "owner_embedding_not_allowedX", True):
        invalid = {**data, "reason": value}
        with pytest.raises(BadRequestError):
            _read_shared(invalid, w, w["editor"], _task_grant(keyword))
    with pytest.raises(BadRequestError):
        _read_shared({**data, "model": {}}, w, w["editor"], _task_grant(keyword))
    await _member_change(app, w, allow_owner_ai=False)
    await _member_change(app, w, allow_owner_ai=True)
    async with app.state.sessionmaker() as db:
        with pytest.raises(ForbiddenError):
            await project_ai_config.revalidate_shared_embedding(db, restored, cipher=app.state.cipher)


async def test_shared_embedding_absent_legacy_is_owner_only_and_never_null(app, model_world):
    w = model_world
    absent = project_ai_config.SHARED_EMBEDDING_ABSENT
    no_grant = project_ai_config.TASK_GRANT_ABSENT
    assert _read_shared(absent, w, w["owner"], no_grant, allow_legacy_owner=True) is None
    for payload, actor, grant, allow in (
        (absent, w["owner"], no_grant, False),
        (None, w["owner"], no_grant, True),
        (absent, w["owner"], None, True),
        (absent, w["editor"], no_grant, True),
        (absent, w["editor"], {"invitation_id": w["invitation"], "revision": 1}, True),
    ):
        with pytest.raises(BadRequestError):
            _read_shared(payload, w, actor, grant, allow_legacy_owner=allow)
    owner = await _embedding(app, w, w["owner"])
    payload = project_ai_config.serialize_shared_embedding(owner)
    for grant in (None, {}, {"invitation_id": w["invitation"], "revision": 1}):
        with pytest.raises(BadRequestError):
            _read_shared(payload, w, w["owner"], grant)
    impossible = replace(owner, mode="keyword", reason="owner_embedding_not_allowed", model=None)
    with pytest.raises(BadRequestError):
        project_ai_config.serialize_shared_embedding(impossible)
