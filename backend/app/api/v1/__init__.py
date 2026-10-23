from fastapi import APIRouter

from app.api.v1 import admin, ai_configs, auth, chapter_versions, chapters, projects, story, tasks, wizard

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(auth.router)
api_router.include_router(admin.router)
api_router.include_router(ai_configs.router)
api_router.include_router(projects.router)
api_router.include_router(story.world_router)
api_router.include_router(story.characters_router)
api_router.include_router(story.outlines_router)
api_router.include_router(chapters.router)
api_router.include_router(chapter_versions.router)
api_router.include_router(wizard.router)
api_router.include_router(tasks.router)
