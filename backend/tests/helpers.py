import asyncio
import hashlib
import json
import time
from copy import deepcopy
from dataclasses import dataclass, field

import httpx
from cryptography.fernet import Fernet

from app.ai.providers import LLMProvider
from app.ai.providers.base import AIProviderError, GeneratedImage
from app.ai.tools import AgentEvent
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
    # embed：每次调用的 {model, texts}；embed_error 非空时抛出（一次性：抛出后清空，除非 embed_error_sticky）
    embed_calls: list[dict] = field(default_factory=list)
    embed_error: Exception | None = None
    embed_error_sticky: bool = False
    embed_dim: int = 64
    # 每次 chat_tools_stream 消费一项；可在事件之间插入异常/Gate 验证中断与取消。
    tool_script: list[list[AgentEvent | Exception | Gate] | Exception] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    images: list[GeneratedImage | Exception] = field(default_factory=list)
    image_calls: list[dict] = field(default_factory=list)

    def push(self, item: "str | list[str | Exception | Gate] | Exception") -> None:
        if isinstance(item, str):
            item = [item[i : i + 7] for i in range(0, len(item), 7)]
        self.responses.append(item)

    def push_tools(self, events: list[AgentEvent | Exception | Gate] | Exception) -> None:
        self.tool_script.append(events)

    def factory(self, provider: str, api_key: str, base_url: str | None, timeout: float) -> LLMProvider:
        fake = self

        class _Provider(LLMProvider):
            async def generate_image(self, prompt, model):
                fake.image_calls.append({"provider": provider, "model": model, "prompt": prompt})
                if provider == "anthropic":
                    return await super().generate_image(prompt, model)
                item = fake.images.pop(0) if fake.images else AIProviderError("未设置测试图片")
                if isinstance(item, Exception):
                    raise item
                return item

            async def chat_tools_stream(
                self, messages, model, tools, *, tool_choice="auto", max_tokens=None, thinking="off", temperature=None
            ):
                call = {
                    "provider": provider,
                    "api_key": api_key,
                    "base_url": base_url,
                    "model": model,
                    "messages": deepcopy(messages),
                    "tools": deepcopy(tools),
                    "tool_choice": tool_choice,
                    "max_tokens": max_tokens,
                    "thinking": thinking,
                    "temperature": temperature,
                }
                fake.calls.append(call)
                fake.tool_calls.append(call)
                item = fake.tool_script.pop(0) if fake.tool_script else [("text", {"text": "ok"})]
                if isinstance(item, Exception):
                    raise item
                completed = False
                has_finish = False
                try:
                    for event in item:
                        if isinstance(event, Gate):
                            event.reached.set()
                            await event.released.wait()
                            continue
                        if isinstance(event, Exception):
                            raise event
                        has_finish |= event[0] == "finish"
                        yield deepcopy(event)
                    if not has_finish:
                        yield "finish", {"reason": "stop"}
                    completed = True
                finally:
                    if not completed:
                        fake.aborted += 1

            async def list_models(self):
                fake.list_calls.append(
                    {"provider": provider, "api_key": api_key, "base_url": base_url, "timeout": timeout}
                )
                if isinstance(fake.models, Exception):
                    raise fake.models
                return [{"id": m["id"], "name": m.get("name")} for m in fake.models]

            async def embed(self, texts, model):
                fake.embed_calls.append({"provider": provider, "model": model, "texts": list(texts)})
                if fake.embed_error is not None:
                    err = fake.embed_error
                    if not fake.embed_error_sticky:
                        fake.embed_error = None
                    raise err
                return [fake_vector(t, fake.embed_dim) for t in texts]

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


def fake_vector(text: str, dim: int = 64) -> list[float]:
    """确定性的假向量：按字符二元组哈希到 dim 维计数（共享二元组越多，余弦越高），未归一化。"""
    vec = [0.0] * dim
    chars = [c for c in text if not c.isspace()]
    grams = ["".join(chars[i : i + 2]) for i in range(max(1, len(chars) - 1))] if chars else []
    for g in grams:
        h = int(hashlib.md5(g.encode()).hexdigest(), 16)
        vec[h % dim] += 1.0
    if not any(vec):
        vec[0] = 1.0
    return vec


def make_settings(tmp_path, **overrides) -> Settings:
    values = {
        # 每个测试独立的临时库文件（tmp_path 由 pytest 按测试单独创建）
        "database_url": f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        "jwt_secret": TEST_JWT_SECRET,
        "encryption_key": TEST_FERNET_KEY,
        "registration_enabled": True,
        "cookie_secure": False,
        "frontend_dist_dir": str(tmp_path / "no-dist"),
        # 自动分析会额外排队任务并消耗 FakeLLM 的预设输出：默认关闭，自动分析相关测试显式开启
        "auto_analyze_enabled": False,
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


def analysis_json(**overrides) -> str:
    """章节分析的模型输出（JSON 文本），字段可覆盖。"""
    data = {
        "summary": "林远在山门试炼中击败对手，却身受重伤。",
        "plot_points": [{"text": "山门试炼开始", "importance": 3}, {"text": "林远击败对手", "importance": 5}],
        "hooks": [{"type": "悬念", "strength": 8, "text": "神秘人现身"}],
        "conflict": {"level": 7, "types": ["人与人"]},
        "emotion": {"tone": "紧张", "start": "平静", "middle": "激烈", "end": "悬疑"},
        "character_changes": [],
        "relationship_changes": [],
        "career_changes": [],
        "organization_changes": [],
        "foreshadows": [{"action": "plant", "text": "神秘玉佩", "keyword": "玉佩", "expected_resolve_chapter": 10}],
        "scores": {"pacing": 7, "engagement": 8, "coherence": 6},
        "suggestions": [
            {"category": "节奏", "priority": "high", "text": "开头铺垫过长，压缩到三段以内"},
            {"category": "对话", "priority": "medium", "text": "对手的台词更有个性"},
            {"category": "描写", "priority": "low", "text": "补充试炼场的环境描写"},
        ],
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)
