"""工坊快照审核、可见性与原子计数。所有写操作在同一请求事务完成。"""

import json
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import String, cast, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.prompts import registry
from app.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from app.models import (
    PromptOverrideVersion,
    PromptTemplateOverride,
    User,
    WorkshopImport,
    WorkshopItem,
    WorkshopLike,
    WorkshopSubmission,
    WritingStyle,
)
from app.models.style_presets import PRESET_STYLES
from app.schemas.workshop import (
    WorkshopApprove,
    WorkshopImportOut,
    WorkshopItemList,
    WorkshopItemOut,
    WorkshopSubmissionList,
    WorkshopSubmissionOut,
    WorkshopSubmit,
)
from app.services import authorization_service as authz
from app.services import prompt_service, style_service
from app.services.audit_writer import AuditOperation

CONTENT_FIELDS = ("kind", "prompt_key", "name", "description", "category", "tags", "content", "is_anonymous")
MAX_PENDING = 20
MAX_BODY_BYTES = 1024 * 1024


def _author(
    author_id: int | None, username: str | None, anonymous: bool, viewer: User, *, moderator: bool = False,
) -> str | None:
    if author_id is None:
        return "已注销用户"
    if anonymous and not moderator and viewer.id != author_id:
        return None
    return username


async def _item_outputs(db: AsyncSession, viewer: User, items: list[WorkshopItem]) -> list[WorkshopItemOut]:
    moderator = await authz.allows(db, viewer, "workshop.read")
    ids = [i.id for i in items]
    author_ids = {i.author_id for i in items if i.author_id is not None}
    names = dict((await db.execute(select(User.id, User.username).where(User.id.in_(author_ids)))).all())
    liked = set(
        (
            await db.scalars(
                select(WorkshopLike.item_id).where(WorkshopLike.user_id == viewer.id, WorkshopLike.item_id.in_(ids))
            )
        ).all()
    )
    imported = set(
        (
            await db.scalars(
                select(WorkshopImport.item_id).where(
                    WorkshopImport.user_id == viewer.id, WorkshopImport.item_id.in_(ids)
                )
            )
        ).all()
    )
    return [
        WorkshopItemOut(
            **{f: getattr(item, f) for f in CONTENT_FIELDS},
            id=item.id,
            author_name=_author(
                item.author_id, names.get(item.author_id), item.is_anonymous, viewer, moderator=moderator
            ),
            author_username=names.get(item.author_id) if moderator else None,
            is_mine=item.author_id == viewer.id,
            is_official=item.is_official,
            status=item.status,
            hidden_by=item.hidden_by,
            like_count=item.like_count,
            download_count=item.download_count,
            liked=item.id in liked,
            imported=item.id in imported,
            published_at=item.published_at,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )
        for item in items
    ]


async def _submission_outputs(
    db: AsyncSession, viewer: User, rows: list[WorkshopSubmission]
) -> list[WorkshopSubmissionOut]:
    moderator = await authz.allows(db, viewer, "workshop.read")
    names = dict(
        (await db.execute(select(User.id, User.username).where(User.id.in_({s.submitter_id for s in rows})))).all()
    )
    return [
        WorkshopSubmissionOut(
            **{f: getattr(s, f) for f in CONTENT_FIELDS},
            id=s.id,
            item_id=s.item_id,
            author_name=_author(s.submitter_id, names.get(s.submitter_id), s.is_anonymous, viewer, moderator=moderator),
            author_username=names.get(s.submitter_id) if moderator else None,
            status=s.status,
            review_note=s.review_note,
            reviewed_at=s.reviewed_at,
            created_at=s.created_at,
            updated_at=s.updated_at,
        )
        for s in rows
    ]


async def _get_item(
    db: AsyncSession, viewer: User, item_id: int, *, owned: bool = False, lock: bool = False
) -> WorkshopItem:
    stmt = select(WorkshopItem).where(WorkshopItem.id == item_id)
    if owned:
        stmt = stmt.where(WorkshopItem.author_id == viewer.id)
    elif not await authz.allows(db, viewer, "workshop.read"):
        stmt = stmt.where(or_(WorkshopItem.status == "active", WorkshopItem.author_id == viewer.id))
    if lock:
        stmt = stmt.with_for_update()
    item = await db.scalar(stmt.execution_options(populate_existing=True))
    if item is None:
        raise NotFoundError("条目不存在")
    return item


async def get_item(db: AsyncSession, viewer: User, item_id: int) -> WorkshopItemOut:
    return (await _item_outputs(db, viewer, [await _get_item(db, viewer, item_id)]))[0]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def list_items(
    db: AsyncSession,
    viewer: User,
    *,
    mine: bool = False,
    kind: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    prompt_key: str | None = None,
    q: str | None = None,
    sort: Literal["newest", "likes", "downloads"] = "newest",
    page: int = 1,
    page_size: int = 20,
) -> WorkshopItemList:
    filters = [WorkshopItem.author_id == viewer.id] if mine else []
    if not mine and not await authz.allows(db, viewer, "workshop.read"):
        filters.append(WorkshopItem.status == "active")
    for column, value in (
        (WorkshopItem.kind, kind),
        (WorkshopItem.category, category),
        (WorkshopItem.prompt_key, prompt_key),
    ):
        if value is not None:
            filters.append(column == value)
    if tag is not None:
        # SQLite JSON 的 ensure_ascii 与 PG JSON 文本表示不同，两种精确字符串表示都匹配。
        tags = {json.dumps(tag.strip(), ensure_ascii=ascii_only) for ascii_only in (True, False)}
        filters.append(or_(*(cast(WorkshopItem.tags, String).like(f"%{_escape_like(t)}%", escape="\\") for t in tags)))
    if q:
        pattern = f"%{_escape_like(q.strip())}%"
        filters.append(
            or_(WorkshopItem.name.ilike(pattern, escape="\\"), WorkshopItem.description.ilike(pattern, escape="\\"))
        )
    total = int(await db.scalar(select(func.count(WorkshopItem.id)).where(*filters)) or 0)
    order = {
        "newest": WorkshopItem.published_at,
        "likes": WorkshopItem.like_count,
        "downloads": WorkshopItem.download_count,
    }[sort]
    items = list(
        (
            await db.scalars(
                select(WorkshopItem)
                .where(*filters)
                .order_by(order.desc(), WorkshopItem.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    return WorkshopItemList(items=await _item_outputs(db, viewer, items), total=total)


async def _get_submission(
    db: AsyncSession, viewer: User, submission_id: int, *, admin: bool = False
) -> WorkshopSubmission:
    stmt = select(WorkshopSubmission).where(WorkshopSubmission.id == submission_id)
    if not admin:
        stmt = stmt.where(WorkshopSubmission.submitter_id == viewer.id)
    row = await db.scalar(stmt)
    if row is None:
        raise NotFoundError("提交不存在")
    return row


async def get_submission(db: AsyncSession, viewer: User, submission_id: int) -> WorkshopSubmissionOut:
    row = await _get_submission(db, viewer, submission_id, admin=await authz.allows(db, viewer, "workshop.read"))
    return (await _submission_outputs(db, viewer, [row]))[0]


async def list_submissions(
    db: AsyncSession,
    viewer: User,
    *,
    admin: bool = False,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> WorkshopSubmissionList:
    if admin:
        await authz.require(db, viewer, "workshop.read")
    filters = [] if admin else [WorkshopSubmission.submitter_id == viewer.id]
    if status:
        filters.append(WorkshopSubmission.status == status)
    total = int(await db.scalar(select(func.count(WorkshopSubmission.id)).where(*filters)) or 0)
    rows = list(
        (
            await db.scalars(
                select(WorkshopSubmission)
                .where(*filters)
                .order_by(WorkshopSubmission.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
    )
    return WorkshopSubmissionList(items=await _submission_outputs(db, viewer, rows), total=total)


def _clean_content(data: WorkshopSubmit) -> dict[str, str]:
    if len(data.model_dump_json().encode("utf-8")) > MAX_BODY_BYTES:
        raise BadRequestError("提交内容过大，上限为 1MB")
    if data.kind == "prompt":
        tpl = registry.TEMPLATES.get(data.prompt_key)
        if tpl is None:
            raise BadRequestError("提示词模板不存在，无法分享")
        if set(data.content) - {"system", "user"} or "user" not in data.content:
            raise BadRequestError("模板内容须包含 user，只能包含 system 和 user")
        system, user = data.content.get("system", ""), data.content["user"]
        prompt_service.ensure_valid(tpl, system, user)
        if (system, user) == (tpl.system, tpl.user):
            raise BadRequestError("与系统默认完全相同的模板不能分享")
        return {"system": system, "user": user}
    if data.prompt_key is not None:
        raise BadRequestError("写作风格不能指定模板 key")
    if set(data.content) - style_service.FIELD_LIMITS.keys():
        raise BadRequestError("风格内容含未知字段")
    for field, value in data.content.items():
        if len(value) > style_service.FIELD_LIMITS[field]:
            raise BadRequestError(f"风格字段 {field} 超过 {style_service.FIELD_LIMITS[field]} 字")
    values = style_service.clean_fields({f: data.content.get(f, "") for f in style_service.FIELD_LIMITS})
    if any(
        values == style_service.clean_fields({f: preset.get(f, "") for f in style_service.FIELD_LIMITS})
        for preset in PRESET_STYLES
    ):
        raise BadRequestError("内置预设风格不能原样分享到工坊，请修改内容后再提交")
    return values


async def submit(
    db: AsyncSession, viewer: User, data: WorkshopSubmit, *, official: bool = False,
) -> WorkshopSubmissionOut:
    publishing = official or await authz.allows(db, viewer, "workshop.publish")
    if publishing:
        await authz.require(db, viewer, "workshop.publish", write=True)
    async with AuditOperation(db, viewer, "workshop.publish", "workshop_submission", None, enabled=publishing) as audit:
        audit.values["target_label"] = data.name
        values = data.model_dump(exclude={"item_id"})
        values["content"] = _clean_content(data)
        values["description"] = data.description.strip()
        # 同一用户串行校验待审核上限；pending item 唯一索引另兜底并发更新。
        await db.scalar(select(User.id).where(User.id == viewer.id).with_for_update())
        if data.item_id is not None:
            item = await _get_item(db, viewer, data.item_id, owned=True, lock=True)
            if (item.kind, item.prompt_key) != (data.kind, data.prompt_key):
                raise BadRequestError("更新不能改变条目类型或模板 key")
            pending = await db.scalar(
                select(WorkshopSubmission.id).where(
                    WorkshopSubmission.item_id == item.id, WorkshopSubmission.status == "pending"
                )
            )
            if pending is not None:
                raise ConflictError("该条目已有待审核更新")
        count = await db.scalar(
            select(func.count(WorkshopSubmission.id)).where(
                WorkshopSubmission.submitter_id == viewer.id, WorkshopSubmission.status == "pending"
            )
        )
        if count >= MAX_PENDING and not publishing:
            raise BadRequestError(f"待审核提交最多 {MAX_PENDING} 条")
        row = WorkshopSubmission(**values, item_id=data.item_id, submitter_id=viewer.id)
        db.add(row)
        try:
            await db.flush()
            if publishing:
                audit.track(row, ["status"])
                await _approve(db, viewer, row, WorkshopApprove(), official=True, audit=audit)
            audit.target(row)
            await audit.owner(row.submitter_id)
            audit.summary.changed_fields = ["name", "description", "category", "tags", "content"]
            await audit.commit()
        except IntegrityError as exc:
            await db.rollback()
            reason = str(exc.orig)
            if (
                "uq_workshop_pending_item" not in reason
                and "UNIQUE constraint failed: workshop_submissions.item_id" not in reason
            ):
                raise
            raise ConflictError("该条目已有待审核更新，请刷新后重试") from exc
        await db.refresh(row)
        return (await _submission_outputs(db, viewer, [row]))[0]


async def _transition(db: AsyncSession, viewer: User, row: WorkshopSubmission, status: str, note: str = "") -> None:
    result = await db.execute(
        update(WorkshopSubmission)
        .where(WorkshopSubmission.id == row.id, WorkshopSubmission.status == "pending")
        .values(
            status=status,
            review_note=note,
            reviewer_id=viewer.id if status != "cancelled" else None,
            reviewed_at=datetime.now(UTC),
        )
    )
    if result.rowcount != 1:
        raise ConflictError("只有待审核提交可以执行此操作")


async def _approve(
    db: AsyncSession,
    viewer: User,
    row: WorkshopSubmission,
    data: WorkshopApprove,
    *,
    official: bool = False,
    audit: AuditOperation | None = None,
) -> WorkshopItem:
    # 删除条目会在 item 锁下级联删除提交；更新审核也必须先 item 后 submission，避免 PG 锁顺序反转。
    item = await _get_item(db, viewer, row.item_id, lock=True) if row.item_id is not None else None
    await _transition(db, viewer, row, "approved")
    values = {f: getattr(row, f) for f in CONTENT_FIELDS}
    if data.category is not None:
        values["category"] = data.category
    if data.tags is not None:
        values["tags"] = data.tags
    row.category, row.tags = values["category"], values["tags"]
    if audit is not None and audit.enabled:
        audit.summary.body_chars_before = sum(len(value) for value in item.content.values()) if item else 0
        audit.summary.body_chars_after = sum(len(value) for value in values["content"].values())
        if item is None or item.content != values["content"]:
            audit.summary.changed_fields.append("content")
    if item is None:
        item = WorkshopItem(**values, author_id=row.submitter_id, is_official=official)
        db.add(item)
        await db.flush()
        row.item_id = item.id
    else:
        # 不读改写计数；正文更新与 hide/unhide 在条目行锁下串行。
        for field, value in values.items():
            setattr(item, field, value)
        if official:
            item.is_official = True
    return item


async def approve(db: AsyncSession, viewer: User, submission_id: int, data: WorkshopApprove) -> WorkshopSubmissionOut:
    await authz.require(db, viewer, "workshop.approve", write=True)
    async with AuditOperation(db, viewer, "workshop.approve", "workshop_submission", submission_id) as audit:
        row = await _get_submission(db, viewer, submission_id, admin=True)
        audit.target(row)
        await audit.owner(row.submitter_id)
        audit.track(row, ["status", "category", "tags"])
        await _approve(db, viewer, row, data, audit=audit)
        await db.flush()
        await db.refresh(row)
        output = (await _submission_outputs(db, viewer, [row]))[0]
        # 锁释放前生成响应，避免提交后管理员删除条目使 refresh 命中已级联删除的提交。
        await audit.commit()
        return output


async def reject(db: AsyncSession, viewer: User, submission_id: int, note: str) -> WorkshopSubmissionOut:
    await authz.require(db, viewer, "workshop.reject", write=True)
    async with AuditOperation(db, viewer, "workshop.reject", "workshop_submission", submission_id) as audit:
        row = await _get_submission(db, viewer, submission_id, admin=True)
        audit.target(row)
        await audit.owner(row.submitter_id)
        audit.track(row, ["status"])
        await _transition(db, viewer, row, "rejected", note)
        await audit.commit()
        await db.refresh(row)
        return (await _submission_outputs(db, viewer, [row]))[0]


async def withdraw(db: AsyncSession, viewer: User, submission_id: int) -> WorkshopSubmissionOut:
    row = await _get_submission(db, viewer, submission_id)
    await _transition(db, viewer, row, "cancelled")
    await db.commit()
    await db.refresh(row)
    return (await _submission_outputs(db, viewer, [row]))[0]


async def set_visibility(db: AsyncSession, viewer: User, item_id: int, *, hidden: bool) -> WorkshopItemOut:
    permission = "workshop.hide" if hidden else "workshop.unhide"
    moderator = await authz.allows(db, viewer, permission)
    if moderator:
        await authz.require(db, viewer, permission, write=True)
    async with AuditOperation(
        db, viewer, permission, "workshop_item", item_id, enabled=moderator,
    ) as audit:
        item = await _get_item(db, viewer, item_id, owned=not moderator, lock=True)
        audit.target(item)
        await audit.owner(item.author_id)
        audit.track(item, ["status", "hidden_by"])
        if not hidden and item.hidden_by == "admin" and not moderator:
            raise ForbiddenError("该条目已被管理员下架")
        if hidden:
            item.status = "hidden"
            if moderator or item.hidden_by != "admin":
                item.hidden_by = "admin" if moderator else "author"
        else:
            item.status, item.hidden_by = "active", None
        await audit.commit()
        await db.refresh(item)
        return (await _item_outputs(db, viewer, [item]))[0]


async def patch_metadata(db: AsyncSession, viewer: User, item_id: int, data: WorkshopApprove) -> WorkshopItemOut:
    await authz.require(db, viewer, "workshop.update", write=True)
    async with AuditOperation(db, viewer, "workshop.update", "workshop_item", item_id) as audit:
        item = await _get_item(db, viewer, item_id, lock=True)
        audit.target(item)
        await audit.owner(item.author_id)
        audit.track(item, ["category", "tags"])
        if data.category is not None:
            item.category = data.category
        if data.tags is not None:
            item.tags = data.tags
        await audit.commit()
        await db.refresh(item)
        return (await _item_outputs(db, viewer, [item]))[0]


async def delete_item(db: AsyncSession, viewer: User, item_id: int) -> None:
    await authz.require(db, viewer, "workshop.delete", write=True)
    async with AuditOperation(db, viewer, "workshop.delete", "workshop_item", item_id) as audit:
        item = await _get_item(db, viewer, item_id, lock=True)
        audit.target(item)
        await audit.owner(item.author_id)
        await db.delete(item)
        await audit.commit()


async def _insert_once(
    db: AsyncSession, model: type[WorkshopLike] | type[WorkshopImport], item_id: int, user_id: int
) -> bool:
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    result = await db.execute(insert(model).values(item_id=item_id, user_id=user_id).on_conflict_do_nothing())
    return result.rowcount == 1


async def like(db: AsyncSession, viewer: User, item_id: int, *, liked: bool) -> WorkshopItemOut:
    await _get_item(db, viewer, item_id, lock=True)
    if liked:
        changed = await _insert_once(db, WorkshopLike, item_id, viewer.id)
    else:
        result = await db.execute(
            delete(WorkshopLike).where(WorkshopLike.item_id == item_id, WorkshopLike.user_id == viewer.id)
        )
        changed = result.rowcount == 1
    if changed:
        await db.execute(
            update(WorkshopItem)
            .where(WorkshopItem.id == item_id)
            .values(like_count=WorkshopItem.like_count + (1 if liked else -1))
        )
    await db.commit()
    return await get_item(db, viewer, item_id)


async def import_item(db: AsyncSession, viewer: User, item_id: int) -> WorkshopImportOut:
    try:
        return await _import_item(db, viewer, item_id)
    except IntegrityError as exc:
        await db.rollback()
        raise ConflictError("模板正在被另一处保存，请刷新后重试") from exc


async def _import_item(db: AsyncSession, viewer: User, item_id: int) -> WorkshopImportOut:
    await db.scalar(select(User.id).where(User.id == viewer.id).with_for_update())
    item = await _get_item(db, viewer, item_id, lock=True)
    style_id, prompt_key = None, None
    if item.kind == "style":
        taken = set((await db.scalars(select(WritingStyle.name).where(WritingStyle.user_id == viewer.id))).all())
        name = style_service.unique_name(item.name, taken, "（工坊）")
        style = WritingStyle(user_id=viewer.id, name=name, **item.content)
        db.add(style)
        await db.flush()
        style_id = style.id
    else:
        tpl = registry.TEMPLATES.get(item.prompt_key)
        if tpl is None:
            raise BadRequestError("系统已不存在该模板，无法导入")
        system, user = item.content["system"], item.content["user"]
        prompt_service.ensure_valid(tpl, system, user)
        row = await db.scalar(
            select(PromptTemplateOverride)
            .where(PromptTemplateOverride.user_id == viewer.id, PromptTemplateOverride.key == tpl.key)
            .with_for_update()
        )
        if row is not None:
            await prompt_service.ensure_current_version(db, row)
        if row is None:
            row, _ = await prompt_service.apply_save(db, viewer.id, tpl, tpl.system, tpl.user, "工坊导入前的系统默认")
        elif not row.enabled:
            db.add(
                PromptOverrideVersion(
                    override_id=row.id, system=tpl.system, user=tpl.user, note="工坊导入前生效的系统默认"
                )
            )
            await db.flush()
        row, _ = await prompt_service.apply_save(
            db, viewer.id, tpl, system, user, f"从工坊导入：{item.name}", force_version=True
        )
        row.enabled = True
        prompt_key = tpl.key
    if await _insert_once(db, WorkshopImport, item.id, viewer.id):
        await db.execute(
            update(WorkshopItem)
            .where(WorkshopItem.id == item.id)
            .values(download_count=WorkshopItem.download_count + 1)
        )
    await db.commit()
    await db.refresh(item)
    return WorkshopImportOut(style_id=style_id, prompt_key=prompt_key, download_count=item.download_count)
