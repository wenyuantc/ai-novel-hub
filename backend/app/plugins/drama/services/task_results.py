"""业务表的终态写入及人工采纳；调用者持有作品写锁，控制整个事务。"""

from sqlalchemy import select

from app.core.errors import BadRequestError, ConflictError, NotFoundError
from app.models import Task
from app.plugins.drama.models import DramaLook, DramaProp, DramaScene, DramaShot
from app.plugins.drama.services.task_material import TaskRequest, material, revisions, subjects, validate_references
from app.services import project_access, task_service


async def apply_draft(db, request, series, episode, draft):
    from app.plugins.drama.services.assets import merge_extracted
    from app.plugins.drama.services.shots import replace_shots

    if request.kind == "drama.rewrite":
        episode.script = draft["script"]
        episode.script_skipped = False
        episode.revision += 1
    elif request.kind == "drama.extract":
        await merge_extracted(db, series, episode, draft)
    elif request.kind == "drama.storyboard":
        await replace_shots(db, series, episode, draft["shots"])
    else:
        # 目标可能在生成期间从本集解绑，覆盖保存也必须重新检查归属。
        current = await material(db, request, series, episode)
        validate_references(request, current, draft)
        models = {"look": DramaLook, "scene": DramaScene, "prop": DramaProp, "shot": DramaShot}
        for item in draft["prompts"]:
            row = await db.get(models[item["kind"]], item["id"])
            if item["kind"] == "shot":
                row.video_prompt = item["prompt"]
            else:
                row.image_prompt = item["prompt"]
            row.revision += 1
        series.revision += 1
        if episode is not None:
            episode.revision += 1
    await db.flush()


async def confirm(db, actor_id, task_id, task_key, *, series_id=None, episode_id=None):
    task = await task_service.get_owned_task(db, actor_id, task_id, expected_task_key=task_key)
    await project_access.require_project(db, actor_id, task.project_id, "content.edit", for_update=True)
    task = await db.scalar(
        select(Task)
        .where(Task.statistics_key == task_key, Task.user_id == actor_id, Task.id == task_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        task is None
        or task.status != "succeeded"
        or not task.kind.startswith("drama.")
        or not isinstance(task.result, dict)
    ):
        raise NotFoundError("短剧草稿不存在")
    request = TaskRequest.model_validate(task.input["request"])
    series, episode = await subjects(db, series_id=series_id, episode_id=episode_id)
    if (request.project_id, request.series_id, request.episode_id) != (
        series.project_id,
        series.id,
        episode.id if episode is not None else None,
    ):
        raise NotFoundError("短剧草稿不存在")
    if task.result.get("applied") is True:
        return {"applied": True}
    if task.result.get("current") != revisions(series, episode):
        raise ConflictError("短剧内容再次发生变化，请重新生成", extra={"code": "stale_revision"})
    draft = task.result.get("draft")
    if not isinstance(draft, dict):
        raise BadRequestError("草稿已失效")
    await apply_draft(db, request, series, episode, draft)
    task.result = {**task.result, "applied": True}
    await db.commit()
    return {"applied": True}
