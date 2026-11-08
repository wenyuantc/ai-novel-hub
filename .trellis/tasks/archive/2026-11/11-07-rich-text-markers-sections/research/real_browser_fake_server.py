"""仅供本任务的隔离浏览器探针：临时 SQLite + 受控 FakeLLM，不连接用户数据。"""

import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from cryptography.fernet import Fernet
from fastapi import HTTPException, Request

from tests.helpers import TEST_JWT_SECRET, FakeLLM, make_settings

directory = Path(os.environ["AINOVEL_E2E_DIR"]).resolve()
temp_root = Path("/tmp").resolve()
if not directory.is_relative_to(temp_root) or not directory.name.startswith("ainovel-e2e-"):
    raise RuntimeError("浏览器测试只允许使用本任务专属的临时目录")
token = (directory / "probe-token").read_text().strip()
if len(token) < 24:
    raise RuntimeError("缺少隔离探针令牌")
key_path = directory / "encryption-key"
if not key_path.exists():
    descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as file:
        file.write(Fernet.generate_key().decode())
encryption_key = key_path.read_text().strip()

# app.main 的模块级默认实例也必须指向隔离库，绝不能读取用户库。
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{directory / 'test.db'}"
os.environ["JWT_SECRET"] = TEST_JWT_SECRET
os.environ["ENCRYPTION_KEY"] = encryption_key
os.environ["FRONTEND_DIST_DIR"] = str(directory / "no-dist")

from app.ai import endpoint_guard  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Base  # noqa: E402


async def _fake_dns(_host: str, _port: int) -> list[str]:
    # FakeLLM 不发外网；固定公网地址仅供真实 URL 校验流程使用。
    return ["93.184.216.34"]


endpoint_guard._resolve = _fake_dns
fake = FakeLLM()
app = create_app(make_settings(directory, encryption_key=encryption_key), provider_factory=fake.factory)
original_lifespan = app.router.lifespan_context


@asynccontextmanager
async def _lifespan(application):
    async with application.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with original_lifespan(application):
        yield


app.router.lifespan_context = _lifespan


def _verify(request: Request) -> None:
    if not hmac.compare_digest(request.headers.get("x-codex-probe-token", ""), token):
        raise HTTPException(status_code=404)


@app.post("/api/__codex_probe/responses", include_in_schema=False)
async def queue_responses(request: Request):
    _verify(request)
    body = await request.json()
    items = body.get("items") if isinstance(body, dict) else None
    if (not isinstance(items, list) or len(items) > 50
            or not all(isinstance(item, str) and len(item) <= 100_000 for item in items)):
        raise HTTPException(status_code=400)
    for item in items:
        fake.push(item)
    return {"queued": len(fake.responses)}


@app.get("/api/__codex_probe/calls", include_in_schema=False)
async def captured_calls(request: Request):
    _verify(request)
    return {"calls": [{"model": call["model"], "messages": call["messages"]} for call in fake.calls]}


@app.post("/api/__codex_probe/reset", include_in_schema=False)
async def clear_calls(request: Request):
    _verify(request)
    fake.calls.clear()
    fake.responses.clear()
    return {"ok": True}


if __name__ == "__main__":
    port = int(os.environ.get("AINOVEL_E2E_PORT", "18766"))
    if not 1024 <= port <= 65535:
        raise RuntimeError("非法端口")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
