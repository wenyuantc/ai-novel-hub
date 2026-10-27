import logging
import re
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
from app.core.upload_limit import MULTIPART_OVERHEAD, UploadLimitMiddleware
from app.services import auth_service, kb_parser, style_service
from app.services.task_engine.engine import TaskEngine
from app.services.task_engine.registry import TaskContext

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


def create_app(
    settings: Settings | None = None,
    provider_factory: ProviderFactory | None = None,
    *,
    start_task_engine: bool = True,
) -> FastAPI:
    """start_task_engine：lifespan 中是否启停后台任务引擎（测试不触发 lifespan，由 fixture 显式 start/stop）。"""
    settings = settings or get_settings()
    engine = create_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        # 表结构由 alembic upgrade head 先行建立（Docker 入口已保证）
        await auth_service.bootstrap_admin(app_.state.sessionmaker, settings)
        # 按 preset_key 同步内置写作风格（代码里改了预设，库里跟着更新）
        await style_service.sync_presets(app_.state.sessionmaker)
        if start_task_engine:
            # 先恢复（遗留 running → interrupted），再开始调度 queued 任务
            await app_.state.task_engine.start()
        try:
            yield
        finally:
            if start_task_engine:
                await app_.state.task_engine.stop()
            await engine.dispose()

    app = FastAPI(title="AI 小说平台", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    app.state.cipher = SecretCipher(settings.encryption_key)
    app.state.provider_factory = provider_factory or create_provider
    app.state.task_engine = TaskEngine(
        TaskContext(
            sessionmaker=app.state.sessionmaker,
            settings=settings,
            cipher=app.state.cipher,
            factory=app.state.provider_factory,
        )
    )

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # 知识库上传：在 multipart 解析（写临时文件）之前拦截超大请求体
    app.add_middleware(
        UploadLimitMiddleware,
        rules=[
            (
                "POST",
                re.compile(r"/api/v1/projects/\d+/kb/documents"),
                kb_parser.MAX_FILE_BYTES + MULTIPART_OVERHEAD,
                kb_parser.TOO_LARGE,
            )
        ],
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
