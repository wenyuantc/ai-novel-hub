"""将保存的剧本与分镜按镜号导出，渲染后复核作品导出权限。"""

from sqlalchemy import select

from app.core.errors import BadRequestError, NotFoundError
from app.plugins.drama.models import DramaCharacter, DramaLook, DramaProp, DramaScene
from app.plugins.drama.services.episodes import get_episode
from app.plugins.drama.services.shots import episode_shots
from app.plugins.state import require_enabled
from app.services.agent.export_reads import capture_document_access, verify_document_access
from app.services.document_export.model import DocumentModel, DocumentNode, DocumentTable
from app.services.document_export.service import render_document_attachment
from app.services.serialization_read import workbench_read_snapshot


async def _shot_nodes(db, series, episode):
    shots = await episode_shots(db, episode.id)
    looks = {
        lid: f"{character}（{look}）"
        for lid, character, look in await db.execute(
            select(DramaLook.id, DramaCharacter.name, DramaLook.name)
            .join(DramaCharacter, DramaLook.character_id == DramaCharacter.id)
            .where(DramaCharacter.series_id == series.id)
        )
    }
    props = dict((await db.execute(select(DramaProp.id, DramaProp.name).where(DramaProp.series_id == series.id))).all())
    scenes = {
        sid: f"{place} {time}".strip()
        for sid, place, time in await db.execute(
            select(DramaScene.id, DramaScene.location, DramaScene.time_of_day).where(DramaScene.series_id == series.id)
        )
    }
    rows = []
    for i, shot in enumerate(shots, 1):
        details = [shot.shot_size, f"{shot.duration_seconds} 秒", shot.atmosphere, scenes.get(shot.scene_id)]
        references = [looks[lid] for lid in shot.look_ids if lid in looks]
        references.extend(props[pid] for pid in shot.prop_ids if pid in props)
        if references:
            details.append("出镜素材：" + "、".join(references))
        rows.append((
            f"镜头 {i} {shot.title}".strip(), shot.description,
            "\n".join(item for item in details if item), shot.video_prompt,
        ))
    return [
        DocumentNode("heading", "分镜表", level=1),
        DocumentTable(("镜头", "画面与台词", "调度与素材", "视频提示词"), rows, "分镜表"),
    ]


async def export_episode(db, actor, eid, fmt, parts):
    selection = parts.split(",")
    if not set(selection) <= {"script", "shots"} or len(selection) != len(set(selection)):
        raise BadRequestError("请选择剧本或分镜表")
    series, _ = await get_episode(db, actor, eid, "export")
    pid = series.project_id
    # 相互引用的分镜、素材与剧本须来自同一事务快照，渲染时释放连接。
    async with workbench_read_snapshot(db, actor, pid) as reader:
        series, episode = await get_episode(reader, actor, eid, "export")
        if series.project_id != pid:
            raise NotFoundError("剧集不存在")
        capture = await capture_document_access(reader, actor, pid, [])
        title = f"{series.title} 第{episode.number}集 {episode.title}"
        model = DocumentModel(nodes=[DocumentNode("heading", title)])
        if "script" in selection:
            model.nodes.extend([DocumentNode("heading", "剧本", level=1), DocumentNode("text", episode.script)])
        if "shots" in selection:
            model.nodes.extend(await _shot_nodes(reader, series, episode))
    attachment = await render_document_attachment(model, fmt, title)
    await verify_document_access(db, capture)
    await require_enabled(db, "drama")
    return attachment
