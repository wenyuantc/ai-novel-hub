"""MCP 插件（远程 Streamable HTTP / SSE）。请求头只回显名称，不返回取值。"""

import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PLUGIN_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,100}$")
# 由传输层管理，或会破坏请求的头
_RESERVED_HEADERS = {
    "host",
    "content-length",
    "content-type",
    "transfer-encoding",
    "connection",
    "accept",
    "mcp-session-id",
    "mcp-protocol-version",
    "last-event-id",
}
MAX_HEADERS = 20
McpTransport = Literal["streamable_http", "sse"]


def check_plugin_slug(value: str) -> str:
    value = value.strip()
    if not PLUGIN_SLUG_RE.match(value):
        raise ValueError("插件标识只能使用小写字母、数字和连字符，以字母或数字开头，不超过 24 个字符")
    return value


def check_headers(value: dict[str, str] | None) -> dict[str, str] | None:
    if value is None:
        return None
    if len(value) > MAX_HEADERS:
        raise ValueError(f"请求头最多 {MAX_HEADERS} 个")
    cleaned: dict[str, str] = {}
    for name, raw in value.items():
        name = str(name).strip()
        if not _HEADER_NAME.match(name):
            raise ValueError("请求头名称不合法")
        if name.lower() in _RESERVED_HEADERS:
            raise ValueError(f"请求头 {name} 由系统管理，不能自定义")
        if not isinstance(raw, str) or len(raw) > 4096 or any(c in raw for c in "\r\n\x00"):
            raise ValueError(f"请求头 {name} 的取值不合法")
        cleaned[name] = raw.strip()
    return cleaned


class McpPluginCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    # 为空时由名称生成（只能得到小写字母、数字和连字符；生成失败时要求填写）
    slug: str | None = None
    transport: McpTransport = "streamable_http"
    url: str = Field(min_length=1, max_length=500)
    headers: dict[str, str] | None = None
    enabled: bool = True

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str | None) -> str | None:
        return None if value is None or not value.strip() else check_plugin_slug(value)

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return check_headers(value)


class McpPluginUpdate(BaseModel):
    """headers：不提交 = 保持原值；提交（含 null / {}）= 整体替换。"""

    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=100)
    slug: str | None = None
    transport: McpTransport | None = None
    url: str | None = Field(default=None, min_length=1, max_length=500)
    headers: dict[str, str] | None = None
    enabled: bool | None = None

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str | None) -> str | None:
        return None if value is None else check_plugin_slug(value)

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        return check_headers(value)


class McpImportRequest(BaseModel):
    """标准 mcpServers JSON：{"mcpServers": {name: {url | serverUrl, type? | transport?, headers?}}}。

    每一项的值在服务层逐项校验（不是对象、stdio、缺 url 等只让该项失败），这里不限定值的类型。
    """

    mcpServers: dict[str, Any] = Field(min_length=1, max_length=20)  # noqa: N815


class McpImportItem(BaseModel):
    name: str
    status: Literal["created", "error"]
    detail: str = ""
    plugin: "McpPluginOut | None" = None


class McpImportResult(BaseModel):
    items: list[McpImportItem]


class McpToolOut(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only_hint: bool | None
    schema_hash: str
    enabled: bool
    auto_confirm: bool


class McpPluginOut(BaseModel):
    id: int
    slug: str
    name: str
    transport: McpTransport
    url: str
    enabled: bool
    # 只回显请求头名称；取值加密存储，任何接口都不返回
    header_names: list[str]
    has_headers: bool
    tools: list[McpToolOut]
    tools_refreshed_at: datetime | None
    last_tested_at: datetime | None
    last_error: str
    created_at: datetime
    updated_at: datetime


class McpToolPrefUpdate(BaseModel):
    """工具名放在请求体里：MCP 工具名可以含 / 等字符，放进路径会被拆错。"""

    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=200)
    enabled: bool | None = None
    auto_confirm: bool | None = None


class McpCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpCallResult(BaseModel):
    text: str
    is_error: bool
    truncated: bool


McpImportItem.model_rebuild()
