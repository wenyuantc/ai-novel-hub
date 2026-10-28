"""Skill 读工具：清单只在系统提示词里列名称与说明，正文与参考资料按需分页读取。

slug 只在当前用户可见的 Skill（本人 + 全局）中解析，本人的优先；结果与其他工具一样作为不可信数据包装。
"""

from math import ceil

from pydantic import Field

from app.core.errors import BadRequestError
from app.services import skill_service

from .base import EmptyArgs, Tool, ToolCtx

PAGE_CHARS = 4000


class SkillListArgs(EmptyArgs):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


class SkillLoadArgs(EmptyArgs):
    slug: str = Field(min_length=1, max_length=48, description="Skill 标识（清单中的第一列）")
    page: int = Field(default=1, ge=1, description="正文页码，每页 4000 字")


class SkillReferenceArgs(EmptyArgs):
    slug: str = Field(min_length=1, max_length=48)
    index: int = Field(ge=1, description="参考资料序号（load_skill 返回的 index，从 1 开始）")
    page: int = Field(default=1, ge=1, description="页码，每页 4000 字")


def _page(text: str, page: int) -> tuple[str, int]:
    pages = max(1, ceil(len(text) / PAGE_CHARS))
    if page > pages:
        raise BadRequestError(f"页码超出范围（共 {pages} 页）")
    return text[(page - 1) * PAGE_CHARS : page * PAGE_CHARS], pages


async def list_skills(ctx: ToolCtx, args: SkillListArgs) -> dict:
    rows = await skill_service.visible_skills(ctx.db, ctx.user_id)
    start = (args.page - 1) * args.page_size
    return {
        "items": [
            {
                "slug": s.slug,
                "name": s.name,
                "description": s.description,
                "category": s.category,
                "triggers": list(s.triggers or []),
                "scope": "global" if s.owner_user_id is None else "mine",
            }
            for s in rows[start : start + args.page_size]
        ],
        "page": args.page,
        "pages": max(1, ceil(len(rows) / args.page_size)),
        "total": len(rows),
    }


async def load_skill(ctx: ToolCtx, args: SkillLoadArgs) -> dict:
    skill = await skill_service.by_slug(ctx.db, ctx.user_id, args.slug)
    body, pages = _page(skill.body, args.page)
    refs = await skill_service.references_of(ctx.db, skill.id)
    return {
        "slug": skill.slug,
        "name": skill.name,
        "description": skill.description,
        "body": body,
        "page": args.page,
        "pages": pages,
        "references": [{"index": i, "title": r.title, "chars": len(r.content)} for i, r in enumerate(refs, start=1)],
    }


async def read_reference(ctx: ToolCtx, args: SkillReferenceArgs) -> dict:
    skill = await skill_service.by_slug(ctx.db, ctx.user_id, args.slug)
    refs = await skill_service.references_of(ctx.db, skill.id)
    if args.index > len(refs):
        raise BadRequestError(f"参考资料序号超出范围（共 {len(refs)} 篇）")
    ref = refs[args.index - 1]
    content, pages = _page(ref.content, args.page)
    return {
        "slug": skill.slug,
        "index": args.index,
        "title": ref.title,
        "content": content,
        "page": args.page,
        "pages": pages,
    }


TOOLS = [
    Tool("list_skills", "分页列出可用的 Skill（写作工作流）", SkillListArgs, "read", list_skills),
    Tool("load_skill", "读取 Skill 的工作流正文（分页）与参考资料目录", SkillLoadArgs, "read", load_skill),
    Tool("read_skill_reference", "按序号分页读取 Skill 的一篇参考资料", SkillReferenceArgs, "read", read_reference),
]
