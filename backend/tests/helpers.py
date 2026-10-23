import asyncio
import json
import time
from dataclasses import dataclass, field

import httpx
from cryptography.fernet import Fernet

from app.ai.providers import LLMProvider
from app.core.config import Settings

TEST_FERNET_KEY = Fernet.generate_key().decode()
TEST_JWT_SECRET = "test-jwt-secret-0123456789abcdef"


@dataclass(eq=False)
class Gate:
    """FakeLLM 分片中的阻塞点：流到达这里时置 reached，等 release() 后才继续产出后续分片。"""

    reached: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    def release(self) -> None:
        self.released.set()

    async def wait_reached(self, within: float = 5.0) -> None:
        await asyncio.wait_for(self.reached.wait(), within)


@dataclass
class FakeLLM:
    """可编程的假模型：按队列依次返回预设分片（或抛出异常），并记录每次调用。"""

    responses: list[list[str] | Exception] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)
    # list_models 的预设结果（或异常），以及每次调用的参数
    models: list[dict] | Exception = field(default_factory=list)
    list_calls: list[dict] = field(default_factory=list)
    # 未产出完就被关闭（取消 / 断开）的流数量
    aborted: int = 0

    def push(self, item: "str | list[str | Exception | Gate] | Exception") -> None:
        if isinstance(item, str):
            item = [item[i : i + 7] for i in range(0, len(item), 7)]
        self.responses.append(item)

    def factory(self, provider: str, api_key: str, base_url: str | None, timeout: float) -> LLMProvider:
        fake = self

        class _Provider(LLMProvider):
            async def list_models(self):
                fake.list_calls.append(
                    {"provider": provider, "api_key": api_key, "base_url": base_url, "timeout": timeout}
                )
                if isinstance(fake.models, Exception):
                    raise fake.models
                return [{"id": m["id"], "name": m.get("name")} for m in fake.models]

            async def generate_stream(self, messages, model, *, temperature=None, max_tokens=None, thinking="off"):
                fake.calls.append(
                    {
                        "provider": provider,
                        "api_key": api_key,
                        "base_url": base_url,
                        "model": model,
                        "messages": messages,
                        "max_tokens": max_tokens,
                        "thinking": thinking,
                    }
                )
                item = fake.responses.pop(0) if fake.responses else ["ok"]
                if isinstance(item, Exception):
                    raise item
                completed = False
                try:
                    for chunk in item:
                        if isinstance(chunk, Gate):
                            chunk.reached.set()
                            await chunk.released.wait()
                            continue
                        if isinstance(chunk, Exception):
                            raise chunk
                        yield chunk
                    completed = True
                finally:
                    if not completed:
                        fake.aborted += 1

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


TERMINAL_TASK_STATUSES = ("succeeded", "failed", "cancelled", "interrupted")


async def wait_task(client: httpx.AsyncClient, task_id: int, within: float = 5.0, statuses=TERMINAL_TASK_STATUSES):
    """轮询任务直到进入指定状态（默认任一终态），返回 TaskOut。"""
    deadline = time.monotonic() + within
    while True:
        resp = await client.get(f"/api/v1/tasks/{task_id}")
        assert resp.status_code == 200, resp.text
        task = resp.json()
        if task["status"] in statuses:
            return task
        if time.monotonic() > deadline:
            raise AssertionError(f"任务 {task_id} 未在 {within}s 内进入 {statuses}：{task}")
        await asyncio.sleep(0.01)


async def wait_until(predicate, within: float = 5.0) -> None:
    deadline = time.monotonic() + within
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待条件超时")
        await asyncio.sleep(0.005)


async def stream_task(client: httpx.AsyncClient, task_id: int, within: float = 10.0) -> list[tuple[str, dict]]:
    """订阅任务流直到 done / error（ASGITransport 会缓冲整个响应），返回解析后的事件。"""
    resp = await asyncio.wait_for(client.get(f"/api/v1/tasks/{task_id}/stream"), within)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    return parse_sse(resp.text)


async def run_task(client: httpx.AsyncClient, resp: httpx.Response) -> tuple[dict, list[tuple[str, dict]]]:
    """创建任务的响应（202）→ 订阅至结束，返回 (最终 TaskOut, 事件)。"""
    assert resp.status_code == 202, resp.text
    events = await stream_task(client, resp.json()["id"])
    return events[-1][1]["task"], events


def streamed_text(events: list[tuple[str, dict]]) -> str:
    """快照文本 + 后续 delta：应等于完整输出。"""
    snapshot = next(d["text"] for e, d in events if e == "snapshot")
    return snapshot + "".join(d["text"] for e, d in events if e == "delta")


def final_event(events: list[tuple[str, dict]]) -> tuple[str, dict]:
    """终止事件（done / error）去掉 task 字段，便于与旧 SSE 断言对照。"""
    event, data = events[-1]
    return event, {k: v for k, v in data.items() if k != "task"}


async def stream_with_gate(app, client: httpx.AsyncClient, task_id: int, gate: Gate) -> httpx.Response:
    """任务阻塞在 gate 时先挂上订阅，再放行：保证全部 delta 都经由流到达。"""
    await gate.wait_reached()
    pending = asyncio.ensure_future(client.get(f"/api/v1/tasks/{task_id}/stream"))
    engine = app.state.task_engine
    await wait_until(lambda: (h := engine.hubs.get(task_id)) is not None and h.subscriber_count > 0)
    gate.release()
    return await asyncio.wait_for(pending, 10)
