from app.core.errors import BadRequestError, ConflictError
from app.plugins.drama.services.task_material import subjects
from app.services import authorization_service as authz
from app.services import project_access, project_ai_config, task_service
from app.services.task_engine.admission import prepare_task_admission


async def start(db, engine, actor_id, data, *, episode_id=None, series_id=None):
    actor = authz.actor_snapshot(db, actor_id)
    if not isinstance(actor, authz.AuthenticatedActor) or db.new or db.dirty or db.deleted:
        raise ConflictError("原认证身份无法验证", extra={"code": "task_actor_unavailable"})
    if episode_id is None and data.kind != "drama.prompts":
        raise BadRequestError("该任务需从本集发起")
    series, episode = await subjects(db, episode_id=episode_id, series_id=series_id)
    project_id = series.project_id
    await project_access.require_project(db, actor_id, project_id, "ai.use")
    params = {
        **data.model_dump(mode="json"),
        "project_id": project_id,
        "series_id": series.id,
        "episode_id": episode.id if episode else None,
    }
    await db.rollback()
    ctx = engine.ctx
    model = await project_ai_config.preflight_project_model(
        ctx.sessionmaker,
        actor,
        project_id,
        "text",
        config_id=data.ai_config_id,
        cipher=ctx.cipher,
        allow_private=ctx.settings.allow_private_ai_endpoints,
        for_task_admission=True,
    )
    proof = await prepare_task_admission(ctx, actor, data.kind, params, model_preflight=model)
    return await task_service.create_task(db, engine, actor_id, data.kind, params, admission=proof)
