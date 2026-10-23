import json
from dataclasses import dataclass, field

import httpx
from cryptography.fernet import Fernet

from app.ai.providers import LLMProvider
from app.core.config import Settings

TEST_FERNET_KEY = Fernet.generate_key().decode()
TEST_JWT_SECRET = "test-jwt-secret-0123456789abcdef"


@dataclass
class FakeLLM:
    """可编程的假模型：按队列依次返回预设分片（或抛出异常），并记录每次调用。"""

    responses: list[list[str] | Exception] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def push(self, item: str | list[str] | Exception) -> None:
        if isinstance(item, str):
            item = [item[i : i + 7] for i in range(0, len(item), 7)]
        self.responses.append(item)

    def factory(self, provider: str, api_key: str, base_url: str | None, timeout: float) -> LLMProvider:
        fake = self

        class _Provider(LLMProvider):
            async def generate_stream(self, messages, model, *, temperature=None, max_tokens=None):
                fake.calls.append(
                    {
                        "provider": provider,
                        "api_key": api_key,
                        "base_url": base_url,
                        "model": model,
                        "messages": messages,
                    }
                )
                item = fake.responses.pop(0) if fake.responses else ["ok"]
                if isinstance(item, Exception):
                    raise item
                for chunk in item:
                    if isinstance(chunk, Exception):
                        raise chunk
                    yield chunk

        return _Provider(api_key=api_key, base_url=base_url, timeout=timeout)


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        # 每个测试独立的临时库文件（tmp_path 由 pytest 按测试单独创建）
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        "jwt_secret": TEST_JWT_SECRET,
        "encryption_key": TEST_FERNET_KEY,
        "registration_enabled": True,
        "cookie_secure": False,
        "frontend_dist_dir": str(tmp_path / "no-dist"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


async def register(client: httpx.AsyncClient, username: str = "alice", password: str = "secret123") -> dict:
    resp = await client.post("/api/v1/auth/register", json={"username": username, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_project(client: httpx.AsyncClient, **kw) -> dict:
    body = {"title": "星海纪元", "genre": "科幻", "premise": "人类首次接触外星文明", "style": "冷峻克制"}
    body.update(kw)
    resp = await client.post("/api/v1/projects", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_ai_config(client: httpx.AsyncClient, **kw) -> dict:
    body = {
        "name": "测试模型",
        "provider": "openai_compat",
        "base_url": "https://api.example.com/v1/",
        "api_key": "sk-test-abcdefgh1234",
        "model": "gpt-test",
    }
    body.update(kw)
    resp = await client.post("/api/v1/ai-configs", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for frame in text.split("\n\n"):
        event, data = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event:
            events.append((event, data))
    return events
