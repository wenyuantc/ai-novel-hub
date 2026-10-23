import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.ai.providers import ProviderFactory, create_provider
from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.database import create_engine, create_sessionmaker
from app.core.errors import register_error_handlers
from app.core.security import SecretCipher
from app.services import auth_service

logger = logging.getLogger(__name__)


def _mount_frontend(app: FastAPI, dist_dir: str) -> None:
    dist = Path(dist_dir).resolve()
    index = dist / "index.html"
    if not index.is_file():
        logger.info("未找到前端构建产物 %s，跳过静态文件挂载", dist)
        return
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa_fallback(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            return JSONResponse(status_code=404, content={"detail": "接口不存在"})
        candidate = (dist / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(index)


def create_app(settings: Settings | None = None, provider_factory: ProviderFactory | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = create_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        # 表结构由 alembic upgrade head 先行建立（Docker 入口已保证）
        await auth_service.bootstrap_admin(app_.state.sessionmaker, settings)
        yield
        await engine.dispose()

    app = FastAPI(title="AI 小说平台", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    app.state.cipher = SecretCipher(settings.encryption_key)
    app.state.provider_factory = provider_factory or create_provider

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_error_handlers(app)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict:
        return {"status": "ok"}

    app.include_router(api_router)
    _mount_frontend(app, settings.frontend_dist_dir)
    return app


def _build_default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


app = _build_default_app()
