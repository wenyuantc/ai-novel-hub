"""Skill 库。固定路径（/import）先于 /{skill_id} 注册。"""

from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Query, Response, UploadFile, status

from app.core.deps import CurrentUser, DbDep
from app.core.errors import BadRequestError
from app.schemas.skill import SkillBrief, SkillCreate, SkillOut, SkillReferencesUpdate, SkillScope, SkillUpdate
from app.services import skill_service as svc

router = APIRouter(prefix="/skills", tags=["skills"])


@router.get("", response_model=list[SkillBrief])
async def list_skills(
    user: CurrentUser, db: DbDep, scope: Literal["mine", "global", "all"] = Query(default="all")
) -> list[SkillBrief]:
    return await svc.list_skills(db, user, scope)


@router.post("", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
async def create_skill(body: SkillCreate, user: CurrentUser, db: DbDep) -> SkillOut:
    return await svc.create_skill(db, user, body)


@router.post("/import", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
async def import_skill(
    user: CurrentUser,
    db: DbDep,
    file: Annotated[UploadFile, File(description="SKILL.md 或 zip（SKILL.md + references/*.md），≤ 2MB")],
    scope: Annotated[SkillScope, Form()] = "mine",
) -> SkillOut:
    data = await file.read(svc.MAX_UPLOAD_BYTES + 1)
    if len(data) > svc.MAX_UPLOAD_BYTES:
        raise BadRequestError(svc.TOO_LARGE)
    return await svc.import_skill(db, user, file.filename or "", data, scope)


@router.get("/{skill_id}", response_model=SkillOut)
async def get_skill(skill_id: int, user: CurrentUser, db: DbDep) -> SkillOut:
    return await svc.to_out(db, user, await svc.get_visible_skill(db, user.id, skill_id))


@router.patch("/{skill_id}", response_model=SkillOut)
async def update_skill(skill_id: int, body: SkillUpdate, user: CurrentUser, db: DbDep) -> SkillOut:
    return await svc.update_skill(db, user, skill_id, body)


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(skill_id: int, user: CurrentUser, db: DbDep) -> Response:
    await svc.delete_skill(db, user, skill_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{skill_id}/references", response_model=SkillOut)
async def replace_references(skill_id: int, body: SkillReferencesUpdate, user: CurrentUser, db: DbDep) -> SkillOut:
    return await svc.replace_references(db, user, skill_id, body.references)


@router.post("/{skill_id}/copy", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
async def copy_skill(skill_id: int, user: CurrentUser, db: DbDep) -> SkillOut:
    return await svc.copy_skill(db, user, skill_id)


@router.post("/{skill_id}/restore-builtin", response_model=SkillOut)
async def restore_builtin(skill_id: int, user: CurrentUser, db: DbDep) -> SkillOut:
    return await svc.restore_builtin(db, user, skill_id)


@router.get("/{skill_id}/export")
async def export_skill(skill_id: int, user: CurrentUser, db: DbDep) -> Response:
    filename, media_type, content = await svc.export_skill(db, user, skill_id)
    ascii_name = "skill.zip" if filename.endswith(".zip") else "SKILL.md"
    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"
        },
    )
