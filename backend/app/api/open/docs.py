"""仅由实际开放路由生成的 OpenAPI 与 Swagger UI。"""

from fastapi import FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse

from app.api.open import router
from app.services.open_api.catalog import PREFIX

DESCRIPTION = """使用平台 **API Key 管理** 页面签发的 Key，通过 `X-API-Key` 请求头调用。
Key 仅可读取已授权小说；文档可匿名查看，实际数据请求始终校验密钥，Cookie / 登录 JWT 不替代 Key。

```bash
curl 'https://your-host/api/open/v1/novels?page=1&page_size=20' \\
  -H 'X-API-Key: YOUR_API_KEY'
```

- 列表返回 `items / total / page / page_size`，默认每页 20 条，最多 100 条；每类接口说明自己的稳定排序。
- 时间为 UTC ISO 8601；章节 `updated_after` 为严格大于指定时间的**行更新筛选**，不含删除事件，
  也不保证捕获关联设定的变更。需要定期全量目录比对，不能将其视为完整增量同步。
- 小说 `updated_at` 是元数据更新时间，`content_updated_at` 是现存章节行的最近更新时间。
- 人物、组织、关系等返回当前保存设定，可能包含后续剧情；没有按阅读章节回溯状态。
- 目录包含空章和生成中章节；正文为空时返回空字符串。全文导出跳过空章和生成中章节，
  EPUB 跳过空章的提示经 `X-Export-Warnings`（UTF-8 百分号编码）返回。
- 默认每 Key 每分钟 120 次，部署可调整；固定分钟窗口边界允许突发。超额返回 429 与 `Retry-After` 秒数。
- Key 与登录会话独立；登出、修改密码不自动撤销 Key。过期、撤销、账号禁用或删除后不可访问。
"""


async def openapi_schema(request: Request) -> JSONResponse:
    app = request.app
    schema = getattr(app.state, "open_api_schema", None)
    if schema is None:
        schema = get_openapi(
            title="AI 小说平台 · 开放读取 API",
            version="1.0.0",
            description=DESCRIPTION,
            routes=router.routes,
        )
        app.state.open_api_schema = schema
    root = request.scope.get("root_path", "").rstrip("/")
    # 每实例缓存不包含请求的部署前缀，避免不同 root_path 互相污染。
    return JSONResponse({**schema, "servers": [{"url": root or "/"}]})


async def swagger_docs(request: Request) -> HTMLResponse:
    root = request.scope.get("root_path", "").rstrip("/")
    return get_swagger_ui_html(
        openapi_url=root + PREFIX + "/openapi.json",
        title="AI 小说平台 · 开放接口文档",
        swagger_ui_parameters={"persistAuthorization": False, "displayRequestDuration": True},
    )


def install_docs(app: FastAPI) -> None:
    # 框架会缓存handler分类；模块级函数不能通过闭包保留已关闭的应用。
    app.add_api_route(PREFIX + "/openapi.json", openapi_schema, methods=["GET"], include_in_schema=False)
    app.add_api_route(PREFIX + "/docs", swagger_docs, methods=["GET"], include_in_schema=False)
