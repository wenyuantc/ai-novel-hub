"""业务异常与统一错误响应 {"detail": "..."}。"""

import logging

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    status_code = 400

    def __init__(self, detail: str, *, extra: dict | None = None):
        super().__init__(detail)
        self.detail = detail
        # 附加到响应体的结构化字段（例如模板校验的出错位置 errors、删除风格时的引用数 references）
        self.extra = extra


class BadRequestError(AppError):
    status_code = 400


class UnauthorizedError(AppError):
    status_code = 401


class ForbiddenError(AppError):
    status_code = 403


class NotFoundError(AppError):
    status_code = 404


class ConflictError(AppError):
    status_code = 409


_HTTP_DEFAULT_DETAILS = {
    404: "资源不存在",
    405: "请求方法不允许",
}


def _format_validation_errors(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path"))
        msg = err.get("msg", "")
        # 自定义 ValueError 的消息形如 "Value error, xxx"
        msg = msg.removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "请求参数校验失败：" + "；".join(parts)


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        content = {**(exc.extra or {}), "detail": exc.detail}
        return JSONResponse(status_code=exc.status_code, content=jsonable_encoder(content))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 只保留可安全序列化的字段：ctx 可能含 ValueError 实例（无法 JSON 序列化），
        # input 会把请求原文（例如密码）回显到响应里
        errors = [{k: v for k, v in err.items() if k in ("type", "loc", "msg")} for err in exc.errors()]
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder({"detail": _format_validation_errors(exc), "errors": errors}),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if not isinstance(detail, str) or detail in ("Not Found", "Method Not Allowed"):
            detail = _HTTP_DEFAULT_DETAILS.get(exc.status_code, str(detail))
        return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=exc.headers)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理的异常", exc_info=exc)
        return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})
