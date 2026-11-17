"""一致性检查服务层测试的公共搭建：隔离作品、章节、FakeLLM 模型目标与直接写入的台账。"""

import hashlib
import json

from app.models import ConsistencyLedger, ConsistencyRun
from app.services.consistency.model_call import ModelTarget
from app.services.mentions.matcher import text_hash
from tests.helpers import create_project, register

API = "/api/v1/projects"


async def seed(client, contents: dict[int, str], username: str = "alice") -> tuple[dict, dict, dict[int, dict]]:
    """注册并建作品与章节；contents = {章号: 正文}。返回 (user, project, {章号: 章节})。"""
    user = await register(client, username)
    project = await create_project(client)
    chapters = {}
    for number, content in contents.items():
        resp = await client.post(f"{API}/{project['id']}/chapters",
                                 json={"title": f"章{number}", "chapter_no": number, "content": content})
        assert resp.status_code == 201, resp.text
        chapters[number] = resp.json()
    return user, project, chapters


def target(fake_llm) -> ModelTarget:
    return ModelTarget(provider=fake_llm.factory("openai_compat", "sk-test", None, 30.0), model="fake-model")


def extraction(*facts: dict) -> str:
    return json.dumps({"facts": list(facts)}, ensure_ascii=False)


def comparison(*issues: dict) -> str:
    return json.dumps({"issues": list(issues)}, ensure_ascii=False)


def fact(fact_id: str, content: str, quote: str, *, category: str = "item", subject: str = "佩剑",
         statement: str | None = None) -> dict:
    start = content.index(quote)
    return {"id": fact_id, "category": category, "subject": subject, "subject_key": subject,
            "statement": statement or quote, "time_hint": "", "quote": quote, "start": start,
            "end": start + len(quote)}


async def add_ledger(db, project_id: int, chapter: dict, facts: list[dict], *, fresh: bool = True) -> None:
    """直接写入台账；fresh=False 时来源指纹与当前正文不符（过期）。章节没有作者区块时指纹即正文 sha256。"""
    source = hashlib.sha256(chapter["content"].encode()).hexdigest() if fresh else "0" * 64
    db.add(ConsistencyLedger(project_id=project_id, chapter_id=chapter["id"], source_hash=source,
                             text_hash=text_hash(chapter["content"]), facts=facts, model="fake-model"))


async def new_run(app, project_id: int, chapter_ids: list[int]) -> int:
    async with app.state.sessionmaker() as db:
        run = ConsistencyRun(project_id=project_id, scope={"mode": "range", "chapter_ids": chapter_ids})
        db.add(run)
        await db.commit()
        return run.id
