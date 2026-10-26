from fastapi import APIRouter

from app.api.v1 import (
    admin,
    ai_configs,
    auth,
    cards,
    careers,
    chapter_versions,
    chapters,
    character_actions,
    organizations,
    projects,
    relationships,
    story,
    tasks,
    wizard,
)

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(ai_configs.router)
api_router.include_router(projects.router)
# 固定路径（with-relations / generate）先于通用 CRUD 的 /{item_id} 注册
api_router.include_router(character_actions.router)
api_router.include_router(story.world_router)
api_router.include_router(story.characters_router)
api_router.include_router(story.outlines_router)
api_router.include_router(organizations.router)
api_router.include_router(relationships.router)
api_router.include_router(cards.router)
api_router.include_router(careers.router)
api_router.include_router(chapters.router)
api_router.include_router(chapter_versions.router)
api_router.include_router(wizard.router)
api_router.include_router(tasks.router)
