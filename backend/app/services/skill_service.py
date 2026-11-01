"""Skill：用户私有 + 管理员全局两级。

- 可见 = 本人的 + 全局；所有按 id 的读取都经 get_visible_skill，不可见一律 404。
- 写：本人的 Skill 本人可写；全局 Skill 只有管理员可写，其他人可见但写操作 403（403 只用于「可见但不可写」）。
- 导入 / 导出：SKILL.md（frontmatter + 正文），有参考资料时为 zip（SKILL.md + references/*.md）。
- 触发词：只匹配消息开头的 `/词`（后接空白或结束），大小写不敏感，本人的 Skill 优先于全局。
"""

import io
import posixpath
import re
import zipfile
from typing import Any

from pydantic import ValidationError
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.models import Skill, SkillReference, User
from app.schemas.skill import (
    MAX_REFERENCES,
    SkillBrief,
    SkillCreate,
    SkillOut,
    SkillReferenceIn,
    SkillReferenceOut,
    SkillScope,
    SkillUpdate,
)
from app.services import authorization_service as authz
from app.services.audit_writer import AuditOperation
from app.skills import SkillFormatError, builtin_fields, dump_skill_md, parse_skill_md

MAX_SKILLS_PER_OWNER = 100
# 生成任务与智能体强制启用时快照的正文上限
SNAPSHOT_LIMIT = 20000
MAX_UPLOAD_BYTES = 2 * 1024 * 1024
MAX_UNZIPPED_BYTES = 5 * 1024 * 1024
TOO_LARGE = "Skill 文件过大，上限为 2MB"
NOT_FOUND = "Skill 不存在"
GLOBAL_READONLY = "全局 Skill 只有管理员可以修改，可以复制为自己的副本后修改"
_TRIGGER = re.compile(r"^/(\S+)(?:\s|$)")
_REF_PREFIX = re.compile(r"^\d{2}-")


def visible_clause(user_id: int):
    return or_(Skill.owner_user_id == user_id, Skill.owner_user_id.is_(None))


def _scope(skill: Skill) -> SkillScope:
    return "global" if skill.owner_user_id is None else "mine"


def editable(user: User, skill: Skill, *, global_update: bool = False) -> bool:
    return skill.owner_user_id == user.id or (skill.owner_user_id is None and global_update)


async def _references(db: AsyncSession, skill_id: int) -> list[SkillReference]:
    return list(
        await db.scalars(
            select(SkillReference)
            .where(SkillReference.skill_id == skill_id)
            .order_by(SkillReference.sort, SkillReference.id)
        )
    )


def _brief(user: User, skill: Skill, refs: list[SkillReference], *, global_update: bool = False) -> dict[str, Any]:
    return {
        "id": skill.id,
        "scope": _scope(skill),
        "slug": skill.slug,
        "name": skill.name,
        "description": skill.description,
        "category": skill.category,
        "triggers": list(skill.triggers or []),
        "builtin_key": skill.builtin_key,
        "source_skill_id": skill.source_skill_id,
        "editable": editable(user, skill, global_update=global_update),
        "body_chars": len(skill.body),
        "reference_titles": [r.title for r in refs],
        "created_at": skill.created_at,
        "updated_at": skill.updated_at,
    }


async def to_out(db: AsyncSession, user: User, skill: Skill) -> SkillOut:
    refs = await _references(db, skill.id)
    return SkillOut(
        **_brief(user, skill, refs, global_update=await authz.allows(db, user, "global_skills.update")),
        body=skill.body,
        references=[SkillReferenceOut.model_validate(r) for r in refs],
    )


async def visible_skills(db: AsyncSession, user_id: int) -> list[Skill]:
    """本人的在前（按 id），全局在后。"""
    rows = await db.scalars(select(Skill).where(visible_clause(user_id)))
    return sorted(rows, key=lambda s: (s.owner_user_id is None, s.id))


async def list_skills(db: AsyncSession, user: User, scope: str = "all") -> list[SkillBrief]:
    stmt = select(Skill)
    if scope == "mine":
        stmt = stmt.where(Skill.owner_user_id == user.id)
    elif scope == "global":
        stmt = stmt.where(Skill.owner_user_id.is_(None))
    else:
        stmt = stmt.where(visible_clause(user.id))
    rows = sorted(await db.scalars(stmt), key=lambda s: (s.owner_user_id is None, s.id))
    titles: dict[int, list[str]] = {}
    if rows:
        for ref in await db.scalars(
            select(SkillReference)
            .where(SkillReference.skill_id.in_([s.id for s in rows]))
            .order_by(SkillReference.sort, SkillReference.id)
        ):
            titles.setdefault(ref.skill_id, []).append(ref.title)
    global_update = await authz.allows(db, user, "global_skills.update")
    return [SkillBrief(**{**_brief(user, s, [], global_update=global_update),
                         "reference_titles": titles.get(s.id, [])}) for s in rows]


async def get_visible_skill(db: AsyncSession, user_id: int, skill_id: int) -> Skill:
    skill = await db.scalar(
        select(Skill).where(Skill.id == skill_id, visible_clause(user_id)).execution_options(populate_existing=True)
    )
    if skill is None:
        raise NotFoundError(NOT_FOUND)
    return skill


async def get_writable_skill(
    db: AsyncSession, user: User, skill_id: int, *, permission: str = "global_skills.update",
) -> Skill:
    skill = await get_visible_skill(db, user.id, skill_id)
    if skill.owner_user_id is None:
        await authz.require(db, user, permission, write=True)
        skill = await get_visible_skill(db, user.id, skill_id)
    elif skill.owner_user_id != user.id:
        raise ForbiddenError(GLOBAL_READONLY)
    return skill


async def _check_quota(db: AsyncSession, owner_id: int | None) -> None:
    count = await db.scalar(
        select(func.count(Skill.id)).where(
            Skill.owner_user_id.is_(None) if owner_id is None else Skill.owner_user_id == owner_id
        )
    )
    if (count or 0) >= MAX_SKILLS_PER_OWNER:
        raise BadRequestError(f"最多保存 {MAX_SKILLS_PER_OWNER} 个 Skill")


async def _owner_for(db: AsyncSession, user: User, scope: SkillScope) -> int | None:
    if scope == "global":
        await authz.require(db, user, "global_skills.create", write=True)
        return None
    return user.id


def _is_slug_conflict(exc: IntegrityError) -> bool:
    original = exc.orig
    if str(original) in {
        "UNIQUE constraint failed: skills.slug",
        "UNIQUE constraint failed: skills.owner_user_id, skills.slug",
    }:
        return True
    return any(
        getattr(error, "sqlstate", None) == "23505"
        and getattr(error, "constraint_name", None) in {"uq_skills_owner_user_id", "uq_skills_global_slug"}
        for error in (original, getattr(original, "__cause__", None))
    )


async def _flush_unique(db: AsyncSession) -> None:
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        if not _is_slug_conflict(exc):
            raise
        raise ConflictError("标识已被使用，请换一个") from exc


async def _commit_unique(db: AsyncSession) -> None:
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if not _is_slug_conflict(exc):
            raise
        raise ConflictError("标识已被使用，请换一个") from exc


def _set_references(db: AsyncSession, skill_id: int, refs: list[SkillReferenceIn]) -> None:
    for index, ref in enumerate(refs):
        db.add(SkillReference(skill_id=skill_id, title=ref.title, content=ref.content, sort=index))


async def _create_skill(db: AsyncSession, user: User, req: SkillCreate, audit: AuditOperation) -> SkillOut:
    audit.values["target_label"] = req.name
    owner = await _owner_for(db, user, req.scope)
    await _check_quota(db, owner)
    skill = Skill(
        owner_user_id=owner,
        slug=req.slug,
        name=req.name,
        description=req.description,
        category=req.category,
        triggers=req.triggers,
        body=req.body,
    )
    db.add(skill)
    await _flush_unique(db)
    _set_references(db, skill.id, req.references)
    audit.target(skill)
    audit.summary.changed_fields = ["slug", "name", "body", "references"]
    audit.summary.body_chars_after = len(skill.body)
    audit.summary.reference_count_after = len(req.references)
    await audit.commit(_commit_unique, flush_fn=_flush_unique)
    await db.refresh(skill)
    return await to_out(db, user, skill)


async def update_skill(db: AsyncSession, user: User, skill_id: int, req: SkillUpdate) -> SkillOut:
    async with AuditOperation(db, user, "skill.update", "skill", skill_id) as audit:
        skill = await get_writable_skill(db, user, skill_id)
        audit.target(skill)
        audit.track(skill, req.model_fields_set)
        for key, value in req.model_dump(exclude_unset=True).items():
            if value is None:
                continue
            if key == "name":
                value = value.strip()
                if not value:
                    raise BadRequestError("名称不能为空")
            setattr(skill, key, value)
        await audit.commit(_commit_unique, flush_fn=_flush_unique)
        await db.refresh(skill)
        return await to_out(db, user, skill)


async def replace_references(db: AsyncSession, user: User, skill_id: int, refs: list[SkillReferenceIn]) -> SkillOut:
    async with AuditOperation(db, user, "skill.references.replace", "skill", skill_id) as audit:
        skill = await get_writable_skill(db, user, skill_id, permission="global_skills.references")
        audit.target(skill)
        if audit.enabled:
            audit.summary.reference_count_before = len(await _references(db, skill.id))
            audit.summary.reference_count_after = len(refs)
            if audit.summary.reference_count_before or audit.summary.reference_count_after:
                audit.summary.changed_fields = ["references"]
        await db.execute(delete(SkillReference).where(SkillReference.skill_id == skill.id))
        _set_references(db, skill.id, refs)
        # 参考资料变化也算 Skill 更新（前端据此显示最近修改时间）
        skill.updated_at = func.now()
        await audit.commit()
        await db.refresh(skill)
        return await to_out(db, user, skill)


async def delete_skill(db: AsyncSession, user: User, skill_id: int) -> None:
    async with AuditOperation(db, user, "skill.delete", "skill", skill_id) as audit:
        skill = await get_writable_skill(db, user, skill_id, permission="global_skills.delete")
        audit.target(skill)
        await db.delete(skill)
        await audit.commit()


async def _free_slug(db: AsyncSession, owner_id: int | None, base: str) -> str:
    taken = set(
        await db.scalars(
            select(Skill.slug).where(
                Skill.owner_user_id.is_(None) if owner_id is None else Skill.owner_user_id == owner_id
            )
        )
    )
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f"-{n}"
        candidate = base[: 48 - len(suffix)].rstrip("-") + suffix
        if candidate not in taken:
            return candidate
        n += 1


async def copy_skill(db: AsyncSession, user: User, skill_id: int) -> SkillOut:
    source = await get_visible_skill(db, user.id, skill_id)
    await _check_quota(db, user.id)
    refs = await _references(db, source.id)
    skill = Skill(
        owner_user_id=user.id,
        slug=await _free_slug(db, user.id, source.slug),
        name=source.name,
        description=source.description,
        category=source.category,
        triggers=list(source.triggers or []),
        body=source.body,
        source_skill_id=source.id,
    )
    db.add(skill)
    await db.flush()
    _set_references(db, skill.id, [SkillReferenceIn(title=r.title, content=r.content) for r in refs])
    await _commit_unique(db)
    await db.refresh(skill)
    return await to_out(db, user, skill)


async def restore_builtin(db: AsyncSession, user: User, skill_id: int) -> SkillOut:
    async with AuditOperation(db, user, "skill.restore_builtin", "skill", skill_id) as audit:
        skill = await get_visible_skill(db, user.id, skill_id)
        audit.target(skill)
        await authz.require(db, user, "global_skills.restore_builtin", write=True)
        if not skill.builtin_key:
            raise BadRequestError("该 Skill 不是内置 Skill")
        audit.track(skill, ["slug", "name", "description", "category", "triggers", "body"])
        fields = builtin_fields(skill.builtin_key)
        for key in ("slug", "name", "description", "category", "triggers", "body"):
            setattr(skill, key, fields[key])
        if audit.enabled:
            audit.summary.reference_count_before = len(await _references(db, skill.id))
            audit.summary.reference_count_after = 0
            if audit.summary.reference_count_before or audit.summary.reference_count_after:
                audit.summary.changed_fields = ["references"]
        await db.execute(delete(SkillReference).where(SkillReference.skill_id == skill.id))
        await audit.commit(_commit_unique, flush_fn=_flush_unique)
        await db.refresh(skill)
        return await to_out(db, user, skill)


# ---------------- 导入 / 导出 ----------------


def _from_markdown(text: str, refs: list[SkillReferenceIn]) -> SkillCreate:
    meta, body = parse_skill_md(text)
    slug = meta.get("name")
    description = meta.get("description")
    if not isinstance(slug, str) or not slug.strip():
        raise SkillFormatError("frontmatter 缺少 name（作为标识）")
    # description 字段必须出现（可以为空串：导出没有说明的 Skill 时写出 description: ''，保证往返一致）
    if not isinstance(description, str):
        raise SkillFormatError("frontmatter 缺少 description")
    triggers = meta.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [triggers]
    # 只接受标量：YAML 别名可以构造指数级嵌套的列表，对它 str() 会耗尽内存
    if not isinstance(triggers, list) or len(triggers) > 50 or not all(_scalar(t) for t in triggers):
        raise SkillFormatError("frontmatter 的 triggers 应为字符串列表")
    display = meta.get("display_name") or slug
    category = meta.get("category") or ""
    if not _scalar(display) or not _scalar(category):
        raise SkillFormatError("frontmatter 的 display_name 与 category 应为字符串")
    return SkillCreate(
        slug=slug.strip(),
        name=str(display),
        description=description.strip(),
        category=str(category),
        triggers=[str(t) for t in triggers],
        body=body,
        references=refs,
    )


def _scalar(value: Any) -> bool:
    return isinstance(value, str | int | float) and not isinstance(value, bool)


def _decode(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SkillFormatError(f"{name} 不是 UTF-8 编码的文本") from exc


def _safe_member(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or re.match(r"^[A-Za-z]:", name):
        return False
    return ".." not in name.split("/")


def _from_zip(data: bytes) -> SkillCreate:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillFormatError("不是有效的 zip 文件") from exc
    with archive:
        infos = [i for i in archive.infolist() if not i.is_dir()]
        for info in infos:
            if not _safe_member(info.filename):
                raise SkillFormatError(f"压缩包包含不安全的路径：{info.filename[:100]}")
        candidates = [i for i in infos if posixpath.basename(i.filename) == "SKILL.md"]
        candidates = [i for i in candidates if i.filename.count("/") <= 1]
        if len(candidates) != 1:
            raise SkillFormatError("压缩包须在根目录或单层目录下恰好包含一个 SKILL.md")
        base = posixpath.dirname(candidates[0].filename)
        prefix = f"{base}/references/" if base else "references/"
        ref_infos = sorted(
            (
                i
                for i in infos
                if i.filename.startswith(prefix) and i.filename.endswith(".md") and "/" not in i.filename[len(prefix) :]
            ),
            key=lambda i: i.filename,
        )
        if len(ref_infos) > MAX_REFERENCES:
            raise SkillFormatError(f"参考资料最多 {MAX_REFERENCES} 篇")
        # 先按声明大小拦截，读取时再按实际字节计数（声明值可以伪造）
        if sum(i.file_size for i in [candidates[0], *ref_infos]) > MAX_UNZIPPED_BYTES:
            raise SkillFormatError("压缩包解压后超过 5MB")
        budget = MAX_UNZIPPED_BYTES

        def read(info: zipfile.ZipInfo) -> bytes:
            nonlocal budget
            with archive.open(info) as fh:
                chunk = fh.read(budget + 1)
            if len(chunk) > budget:
                raise SkillFormatError("压缩包解压后超过 5MB")
            budget -= len(chunk)
            return chunk

        skill_text = _decode(read(candidates[0]), "SKILL.md")
        refs = []
        for info in ref_infos:
            title = _REF_PREFIX.sub("", posixpath.basename(info.filename)[: -len(".md")])
            refs.append(SkillReferenceIn(title=title, content=_decode(read(info), info.filename)))
    return _from_markdown(skill_text, refs)


def parse_upload(filename: str, data: bytes) -> SkillCreate:
    if len(data) > MAX_UPLOAD_BYTES:
        raise BadRequestError(TOO_LARGE)
    try:
        if data[:4] == b"PK\x03\x04" or filename.lower().endswith(".zip"):
            return _from_zip(data)
        return _from_markdown(_decode(data, "SKILL.md"), [])
    except SkillFormatError as exc:
        raise BadRequestError(f"Skill 导入失败：{exc}") from exc
    except ValidationError as exc:
        reasons = "；".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {str(e['msg']).removeprefix('Value error, ')}"
            for e in exc.errors(include_input=False)
        )
        raise BadRequestError(f"Skill 导入失败：{reasons}") from exc


async def import_skill(db: AsyncSession, user: User, filename: str, data: bytes, scope: SkillScope) -> SkillOut:
    async with AuditOperation(db, user, "skill.import", "skill", enabled=scope == "global") as audit:
        req = parse_upload(filename, data)
        return await _create_skill(db, user, req.model_copy(update={"scope": scope}), audit)


async def export_skill(db: AsyncSession, user: User, skill_id: int) -> tuple[str, str, bytes]:
    """返回 (文件名, media type, 内容)。"""
    skill = await get_visible_skill(db, user.id, skill_id)
    refs = await _references(db, skill.id)
    meta: dict[str, Any] = {"name": skill.slug, "display_name": skill.name, "description": skill.description}
    if skill.category:
        meta["category"] = skill.category
    if skill.triggers:
        meta["triggers"] = list(skill.triggers)
    text = dump_skill_md(meta, skill.body)
    if not refs:
        return f"{skill.slug}.SKILL.md", "text/markdown; charset=utf-8", text.encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{skill.slug}/SKILL.md", text)
        for index, ref in enumerate(refs, start=1):
            # 序号前缀保证导入时按原顺序排列，导入时去掉
            archive.writestr(f"{skill.slug}/references/{index:02d}-{ref.title}.md", ref.content)
    return f"{skill.slug}.zip", "application/zip", buffer.getvalue()


# ---------------- 智能体与生成任务 ----------------


async def by_slug(db: AsyncSession, user_id: int, slug: str) -> Skill:
    rows = await db.scalars(select(Skill).where(Skill.slug == slug, visible_clause(user_id)))
    found = sorted(rows, key=lambda s: s.owner_user_id is None)
    if not found:
        raise NotFoundError(NOT_FOUND)
    return found[0]


async def references_of(db: AsyncSession, skill_id: int) -> list[SkillReference]:
    return await _references(db, skill_id)


async def match_trigger(db: AsyncSession, user_id: int, text: str) -> Skill | None:
    match = _TRIGGER.match(text.lstrip())
    if match is None:
        return None
    word = match.group(1).casefold()
    for skill in await visible_skills(db, user_id):
        if word in {t.casefold() for t in skill.triggers or []}:
            return skill
    return None


def snapshot_of(skill: Skill) -> dict[str, Any]:
    return {"id": skill.id, "name": skill.name, "body": skill.body[:SNAPSHOT_LIMIT]}


async def snapshot(db: AsyncSession, user_id: int, skill_id: int | None) -> dict[str, Any] | None:
    """生成任务创建时快照 Skill 正文（不可见 → 404）；之后编辑 Skill 不影响已有任务。"""
    if skill_id is None:
        return None
    return snapshot_of(await get_visible_skill(db, user_id, skill_id))


def skill_log(snap: dict[str, Any] | None) -> dict[str, Any] | None:
    """生成记录 params.skill：只记 id 与名称。"""
    return {"id": snap["id"], "name": snap["name"]} if snap else None


async def create_skill(db: AsyncSession, user: User, req: SkillCreate) -> SkillOut:
    async with AuditOperation(db, user, "skill.create", "skill", enabled=req.scope == "global") as audit:
        return await _create_skill(db, user, req, audit)


async def reject_oversized_import(db: AsyncSession, user: User, scope: SkillScope) -> None:
    """保留路由的 2MB 守卫，仅对管理员的全局导入记录安全拒绝。"""
    async with AuditOperation(db, user, "skill.import", "skill", enabled=scope == "global"):
        raise BadRequestError(TOO_LARGE)
