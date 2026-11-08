"""按作者读取提示词组件（供模板覆盖、Skill、工坊与组件库共用；只读，不提交）。

组件只属于作者本人：所有查询都按 user_id 过滤，他人的组件既不可见也不能被引用。
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.components import ref_slugs
from app.models import PromptComponent


async def contents(db: AsyncSession, user_id: int | None) -> dict[str, str]:
    """{标识: 内容}；user_id 为空（全局资源）时没有可引用的组件。"""
    if user_id is None:
        return {}
    rows = await db.execute(
        select(PromptComponent.slug, PromptComponent.content).where(PromptComponent.user_id == user_id)
    )
    return {slug: content for slug, content in rows}


async def refs(db: AsyncSession, user_id: int | None, *texts: str) -> list[dict[str, Any]]:
    """文本中引用的、本人确实存在的组件 [{id, slug, name}]，按首次出现顺序。"""
    slugs: list[str] = []
    for text in texts:
        for slug in ref_slugs(text):
            if slug not in slugs:
                slugs.append(slug)
    if not slugs or user_id is None:
        return []
    rows = await db.execute(
        select(PromptComponent.id, PromptComponent.slug, PromptComponent.name).where(
            PromptComponent.user_id == user_id, PromptComponent.slug.in_(slugs)
        )
    )
    found = {slug: {"id": cid, "slug": slug, "name": name} for cid, slug, name in rows}
    return [found[s] for s in slugs if s in found]
