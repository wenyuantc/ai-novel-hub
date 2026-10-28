from fastapi import APIRouter

from app.api.v1 import (
    admin,
    agent,
    ai_configs,
    analysis,
    auth,
    batch,
    book_imports,
    cards,
    careers,
    chapter_versions,
    chapters,
    character_actions,
    foreshadows,
    inspiration,
    kb,
    memories,
    organizations,
    outline_pro,
    projects,
    prompt_templates,
    reader,
    relationships,
    story,
    styles,
    tasks,
    transfer,
    wizard,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(agent.router)
api_router.include_router(admin.router)
api_router.include_router(ai_configs.router)
# 固定路径 /projects/import... 先于 /projects/{project_id}
api_router.include_router(transfer.router)
api_router.include_router(projects.router)
api_router.include_router(reader.router)
# 固定路径（with-relations / generate）先于通用 CRUD 的 /{item_id} 注册
api_router.include_router(character_actions.router)
# 大纲进阶的固定路径（/outlines/tree、/outlines/export ...）先于 /outlines/{item_id}
api_router.include_router(outline_pro.router)
api_router.include_router(story.world_router)
api_router.include_router(story.characters_router)
api_router.include_router(story.outlines_router)
api_router.include_router(organizations.router)
api_router.include_router(relationships.router)
api_router.include_router(cards.router)
api_router.include_router(careers.router)
api_router.include_router(foreshadows.router)
api_router.include_router(chapters.router)
api_router.include_router(batch.router)
api_router.include_router(chapter_versions.router)
api_router.include_router(analysis.router)
api_router.include_router(wizard.router)
api_router.include_router(tasks.router)
api_router.include_router(prompt_templates.router)
api_router.include_router(styles.router)
api_router.include_router(inspiration.router)
api_router.include_router(kb.router)
api_router.include_router(memories.router)
api_router.include_router(book_imports.router)
