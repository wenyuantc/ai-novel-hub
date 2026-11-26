import hmac
import logging
import re
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.ai.images import MAX_COVER_BYTES
from app.ai.images import TOO_LARGE as COVER_TOO_LARGE
from app.ai.providers import ProviderFactory, create_provider
from app.api.open import router as open_api_router
from app.api.open.docs import install_docs as install_open_api_docs
from app.api.v1 import api_router
from app.core.config import Settings, get_settings
from app.core.database import create_engine, create_sessionmaker
from app.core.errors import register_error_handlers
from app.core.oauth_access_log import redact_oauth_callback
from app.core.security import SecretCipher
from app.core.upload_limit import MULTIPART_OVERHEAD, UploadLimitMiddleware
from app.services import (
    admin_dashboard_service,
    ai_usage_recording,
    auth_service,
    authorization_service,
    kb_parser,
    recycle_bin_service,
    skill_service,
    style_service,
    task_statistics_service,
)
from app.services.backups.runtime import BackupRuntime
from app.services.backups.service import BackupManager
from app.services.book_import import service as book_import_service
from app.services.project_transfer import importer as transfer_importer
from app.services.project_transfer import preview_store
from app.services.system_settings_service import SystemSettingsManager
from app.services.task_engine.engine import TaskEngine
from app.services.task_engine.registry import TaskContext
from app.services.task_engine.request_worker import RequestWorkerScope

logger = logging.getLogger(__name__)


class _BackupResponseHeaders:
    """局部敏感响应边界；纯ASGI保持现有授权屏障的请求Task身份。"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        is_backup = path == "/api/v1/admin/backups" or path.startswith("/api/v1/admin/backups/")
        if scope["type"] != "http" or not is_backup:
            await self.app(scope, receive, send)
            return
        started = False

        async def safe_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-store"
                headers["X-Content-Type-Options"] = "nosniff"
                started = True
            await send(message)

        try:
            await self.app(scope, receive, safe_send)
        except Exception:
            logger.error("备份接口发生未确认错误")
            if started:
                # 正文已开始不可改写响应；只传播固定异常，隐藏第三方/SQL原文。
                raise RuntimeError("备份附件传输已中断") from None
            response = JSONResponse(status_code=500, content={"detail": "备份操作暂时不可用，请刷新核实"})
            await response(scope, receive, safe_send)


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
    settings = settings if settings is not None else get_settings().model_copy(deep=True)
    logging.getLogger("uvicorn.access").addFilter(redact_oauth_callback)
    engine = create_engine(settings.database_url)

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(engine.dispose)
            cleanup.push_async_callback(app_.state.system_settings.close)
            # 表结构由 alembic upgrade head 先行建立。启动中途出错也要收回已经启动的服务。
            await authorization_service.initialize(app_.state.sessionmaker)
            await app_.state.system_settings.load()
            await auth_service.bootstrap_admin(app_.state.sessionmaker, settings)
            await style_service.sync_presets(app_.state.sessionmaker)
            await book_import_service.purge_expired_all(app_.state.sessionmaker)
            # 先绑定真实地址，让随后所有 worker 代次在登记事务中通告同一监听。
            cleanup.push_async_callback(app_.state.task_engine.peer.stop)
            await app_.state.task_engine.peer.start()
            cleanup.push_async_callback(app_.state.task_engine.request_worker.stop)
            await app_.state.task_engine.request_worker.start()
            await task_statistics_service.initialize_tracking(app_.state.sessionmaker)
            if start_task_engine:
                cleanup.push_async_callback(app_.state.task_engine.stop)
                await app_.state.task_engine.start()
            # 成功启动后先关闭内部流，再释放两份租约。较早的回调也覆盖部分启动失败。
            cleanup.push_async_callback(app_.state.task_engine.peer.stop)
            cleanup.push_async_callback(app_.state.recycle_bin_cleaner.stop)
            await app_.state.recycle_bin_cleaner.start()
            cleanup.push_async_callback(app_.state.dashboard_sampler.stop)
            await app_.state.dashboard_sampler.start()
            cleanup.push_async_callback(app_.state.backup_runtime.stop)
            cleanup.push_async_callback(app_.state.backups.close)
            await app_.state.backup_runtime.start()
            async with (
                ai_usage_recording.usage_maintenance(app_.state.sessionmaker),
                preview_store.preview_maintenance(app_.state.sessionmaker),
            ):
                yield

    app = FastAPI(title="AI 小说平台", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.sessionmaker = create_sessionmaker(engine)
    # 预览版本只使用域分离的派生密钥；会话不保存 JWT 原密钥。
    app.state.sessionmaker.configure(info={
        **app.state.sessionmaker.kw.get("info", {}),
        "book_search_hmac_key": hmac.digest(settings.jwt_secret.encode(), b"ainovel/book-search/v1", "sha256"),
        "agent_view_hmac_key": hmac.digest(settings.jwt_secret.encode(), b"ainovel/agent-view-key/v1", "sha256"),
    })
    app.state.cipher = SecretCipher(settings.encryption_key)
    app.state.backups = BackupManager(settings, app.state.cipher, app.state.sessionmaker)
    app.state.provider_factory = provider_factory or create_provider
    app.state.system_settings = SystemSettingsManager(
        settings, app.state.sessionmaker, lambda: app.state.task_engine.notify(),
        poll_interval=TaskEngine.poll_interval,
    )
    app.state.task_engine = TaskEngine(
        TaskContext(
            sessionmaker=app.state.sessionmaker,
            settings=settings,
            cipher=app.state.cipher,
            factory=app.state.provider_factory,
        )
    )
    app.state.backup_runtime = BackupRuntime(app.state.backups, worker=app.state.task_engine.request_worker)
    app.state.backups.notify = app.state.backup_runtime.notify
    app.state.backups.tool_readiness = app.state.backup_runtime.tool_readiness

    app.state.system_settings.notify_peers = app.state.task_engine.peer_wakes.settings
    app.state.task_engine.peer_wakes.refresh_settings = app.state.system_settings.request_refresh
    app.state.recycle_bin_cleaner = recycle_bin_service.RecycleBinCleaner(app.state.task_engine)
    app.state.dashboard_sampler = admin_dashboard_service.DashboardSampler(app.state.sessionmaker)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            # 已允许的跨域调用方需要读取下载信息、导出提示和开放接口的限流等待时间。
            expose_headers=["Content-Disposition", "X-Export-Warnings", "Retry-After"],
        )

    # 知识库上传 / 项目备份导入 / 拆书导入：在 multipart 解析（写临时文件）之前拦截超大请求体
    app.add_middleware(
        UploadLimitMiddleware,
        rules=[
            ("POST", re.compile(r"/api/v1/projects/\d+/novel-template-preview"),
             1024 * 1024, "模板选择请求过大"),
            ("POST", re.compile(r"/api/v1/novel-templates"), 1024 * 1024, "模板选择请求过大"),
            ("PUT", re.compile(r"/api/v1/novel-templates/\d+/snapshot"), 1024 * 1024, "模板选择请求过大"),
            (
                "POST", re.compile(
                    r"/api/v1/(projects/\d+/settings/(character|world|organization|career)/\d+|"
                    r"series/\d+/entries/\d+)/thumbnail/ai/generate"
                ),
                64 * 1024, "生成请求过大",
            ),
            (
                "POST", re.compile(
                    r"/api/v1/(projects/\d+/settings/(character|world|organization|career)/\d+|"
                    r"series/\d+/entries/\d+)/thumbnail/ai/apply"
                ),
                768 * 1024, "生成图片请求过大",
            ),
            (
                "PUT", re.compile(
                    r"/api/v1/(projects/\d+/settings/(character|world|organization|career)/\d+|"
                    r"series/\d+/entries/\d+)/thumbnail"
                ),
                MAX_COVER_BYTES + MULTIPART_OVERHEAD, "图片过大，上限为 10MB",
            ),
            (
                "PUT", re.compile(r"/api/v1/projects/\d+/cover"),
                MAX_COVER_BYTES + MULTIPART_OVERHEAD, COVER_TOO_LARGE,
            ),
            ("POST", re.compile(r"/api/v1/workshop/(submissions|admin/items)"),
             1024 * 1024, "提交内容过大，上限为 1MB"),
            (
                "POST", re.compile(r"/api/v1/projects/\d+/cards/import(/preview)?"),
                16 * 1024 * 1024 + MULTIPART_OVERHEAD, "设定卡内容过大，上限为 16MB",
            ),
            (
                "POST",
                re.compile(r"/api/v1/projects/\d+/kb/documents"),
                kb_parser.MAX_FILE_BYTES + MULTIPART_OVERHEAD,
                kb_parser.TOO_LARGE,
            ),
            # 拆书导入上传
            (
                "POST",
                re.compile(r"/api/v1/book-imports"),
                kb_parser.MAX_FILE_BYTES + MULTIPART_OVERHEAD,
                kb_parser.TOO_LARGE,
            ),
            # Skill 导入（SKILL.md / zip）
            (
                "POST",
                re.compile(r"/api/v1/skills/import"),
                skill_service.MAX_UPLOAD_BYTES + MULTIPART_OVERHEAD,
                skill_service.TOO_LARGE,
            ),
            # 项目备份导入（预览 / 直接上传导入）
            (
                "POST",
                re.compile(r"/api/v1/projects/import(/preview)?"),
                transfer_importer.MAX_BYTES + MULTIPART_OVERHEAD,
                transfer_importer.TOO_LARGE,
            ),
        ],
    )

    register_error_handlers(app)

    app.add_middleware(_BackupResponseHeaders)
    app.add_middleware(RequestWorkerScope, worker=app.state.task_engine.request_worker)

    @app.get("/healthz", tags=["health"])
    async def healthz() -> dict:
        return {"status": "ok"}

    app.include_router(api_router)
    app.include_router(open_api_router)
    install_open_api_docs(app)
    _mount_frontend(app, settings.frontend_dist_dir)
    return app


def _build_default_app() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return create_app()


app = _build_default_app()
