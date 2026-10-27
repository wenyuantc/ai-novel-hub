"""拆书导入：上传 / 列表 / 校正 / 预估 / 开始导入的接口，以及 book.import 反推任务（FakeLLM）：
完整流程与统计、只写空字段、失败重试后暂停（继续 / 跳过 / 结束）、汇总跳过用代码合并结果、中断后续跑不重复调用、
深度分析在角色写入后入队、风格、模板覆盖与生成记录、执行时章节被删、互斥与回滚、跨用户隔离。"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.ai.prompts import registry
from app.ai.providers import AIProviderError
from app.core.errors import ConflictError
from app.core.upload_limit import MULTIPART_OVERHEAD
from app.models import BookImport, ChapterVersion, GenerationLog, Project, Task
from app.services import kb_parser, task_service
from app.services.book_import import reverse, splitter
from app.services.book_import import service as book_service
from tests.helpers import (
    Gate,
    analysis_json,
    create_ai_config,
    register,
    stream_task,
    wait_task,
)
from tests.kb_fixtures import make_docx, make_epub

API = "/api/v1/book-imports"
FILLER = "他走进雨夜，四下无人，只有远处的灯火在风中摇晃。"


@pytest.fixture(autouse=True)
def _small_batches(monkeypatch):
    # 每批约 300 字：第 1、2 章各一批，第 3 章（约 560 字）拆成两段分到相邻两批
    monkeypatch.setattr(reverse, "BATCH_CHARS", 300)
    monkeypatch.setattr(reverse, "RETRY_DELAYS", (0.0, 0.0))


def book_text() -> str:
    def body(n: int, lines: int) -> str:
        return "\n".join(f"林远与苏晴在第{n}章同行。{FILLER}" for _ in range(lines))

    return "\n\n".join(
        [
            "第一卷 风起",
            "第1章 雨夜",
            body(1, 7),
            "第2章 晨光",
            body(2, 7),
            "第二卷 云涌",
            "第3章 远行",
            body(3, 16),
        ]
    )


def digest(nos: list[int], summary: str = "", **entities) -> str:
    data = {"chapters": [{"chapter_no": n, "summary": summary or f"第{n}章摘要"} for n in nos]}
    data.update(entities)
    return json.dumps(data, ensure_ascii=False)


DIGESTS = [
    digest(
        [1],
        characters=[{"name": "林远", "aliases": ["小林"], "role_type": "主角", "profile": "少年剑客"}],
        organizations=[
            {"name": "天剑宗", "org_type": "宗门", "description": "北境大宗", "members": [{"name": "小林"}]}
        ],
        world=[{"title": "北境", "category": "地点", "content": "终年积雪"}],
        careers=[{"name": "剑修", "kind": "main", "stages": [{"name": "练气"}, {"name": "筑基"}]}],
        relationships=[{"a": "小林", "b": "苏晴", "role_a": "师兄", "role_b": "师妹", "category": "social"}],
    ),
    digest([2], characters=[{"name": "苏晴", "role_type": "重要配角"}, {"name": "林 远", "aliases": ["林师兄"]}]),
    digest([3], "上半"),
    digest([3], "下半"),
]

SYNTHESIZE = json.dumps(
    {
        "project": {
            "synopsis": "少年林远踏上修行之路。",
            "theme": "成长",
            "tags": ["仙侠", "成长", "这个标签明显超过十个字了"],
            "selling_points": "师徒与同门",
            "audience": "男频读者",
            "genre": "仙侠",
        },
        "characters": [
            {"name": "林远", "aliases": ["小林", "林师兄"], "role_type": "主角", "profile": "天剑宗弟子"},
            {"name": "苏晴", "role_type": "重要配角", "profile": "师妹"},
        ],
        "organizations": [
            {
                "name": "天剑宗",
                "org_type": "宗门",
                "description": "北境第一大宗",
                "members": [{"name": "林师兄", "position": "弟子"}],
            }
        ],
        "world": [{"title": "北境", "category": "place", "content": "终年积雪的雪原"}],
        "careers": [
            {"name": "剑修", "kind": "main", "description": "以剑入道", "stages": [{"name": "练气"}, {"name": "筑基"}]}
        ],
        "relationships": [
            {"a": "林远", "b": "苏晴", "role_a": "师兄", "role_b": "师妹", "category": "social", "description": "同门"}
        ],
    },
    ensure_ascii=False,
)

STYLE = json.dumps(
    {"name": "冷峻", "description": "冷静克制的叙事", "pov": "第三人称", "tone": "冷峻"}, ensure_ascii=False
)


def _prompt(call: dict) -> str:
    return "\n".join(m["content"] for m in call["messages"])


def _digest_calls(fake_llm) -> list[dict]:
    return [c for c in fake_llm.calls if "【本批章节】" in _prompt(c)]


async def upload(client, name: str = "星海.txt", data: bytes | None = None):
    content = data if data is not None else book_text().encode("gbk")
    return await client.post(API, files={"file": (name, content, "application/octet-stream")})


async def _setup(client) -> dict:
    await register(client)
    await create_ai_config(client)
    resp = await upload(client)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _start(client, rec: dict, **body):
    n = rec["chapter_count"]
    payload = {
        "revision": rec["revision"],
        "title": "星海",
        "genre": "玄幻",
        "import_range": [1, n],
        "reverse_range": [1, n],
        **body,
    }
    return client.post(f"{API}/{rec['id']}/start", json=payload)


async def _run(client, resp) -> tuple[dict, list]:
    assert resp.status_code == 202, resp.text
    events = await stream_task(client, resp.json()["task"]["id"])
    return events[-1][1], events


def _push_all(fake_llm, digests=DIGESTS, synthesize=SYNTHESIZE, style=STYLE) -> None:
    for d in digests:
        fake_llm.push(d)
    fake_llm.push(synthesize)
    fake_llm.push(style)


async def _project_data(client, pid: int) -> dict:
    base = f"/api/v1/projects/{pid}"
    return {
        "project": (await client.get(base)).json(),
        "outlines": (await client.get(f"{base}/outlines")).json(),
        "chapters": (await client.get(f"{base}/chapters")).json(),
        "characters": (await client.get(f"{base}/characters")).json(),
        "organizations": (await client.get(f"{base}/organizations")).json(),
        "world": (await client.get(f"{base}/world")).json(),
        "careers": (await client.get(f"{base}/careers")).json(),
        "relationships": (await client.get(f"{base}/relationships")).json(),
    }


# ---------------- 上传与解析记录 ----------------


async def test_upload_detail_list_and_chapter_text(client):
    rec = await _setup(client)
    assert rec["kind"] == "txt" and rec["book_title"] == "星海" and rec["revision"] == 0
    assert rec["chapter_count"] == 3 and rec["max_import_chapters"] == 3000 and rec["too_many"] is False
    assert [(c["index"], c["title"], c["volume_no"], c["volume_title"]) for c in rec["chapters"]] == [
        (1, "第1章 雨夜", 1, "第一卷 风起"),
        (2, "第2章 晨光", 1, "第一卷 风起"),
        (3, "第3章 远行", 2, "第二卷 云涌"),
    ]
    first = rec["chapters"][0]
    assert first["first_line"].startswith("林远与苏晴在第1章同行") and first["source_no"] == 1
    assert rec["warning_counts"] == {"short": 2, "long": 0, "dup_title": 0, "gap": 0}
    listed = (await client.get(API)).json()
    assert [(i["id"], i["chapter_count"]) for i in listed] == [(rec["id"], 3)] and listed[0]["total_chars"] > 0
    detail = (await client.get(f"{API}/{rec['id']}")).json()
    assert detail["chapters"] == rec["chapters"]
    text = (await client.get(f"{API}/{rec['id']}/chapters/3")).json()
    assert (
        text["title"] == "第3章 远行" and len(text["lines"]) == 16 and text["lines"][0].startswith("林远与苏晴在第3章")
    )
    assert (await client.get(f"{API}/{rec['id']}/chapters/9")).status_code == 404


async def test_upload_formats_and_rejections(client, monkeypatch):
    await register(client)
    body = "\n".join([FILLER] * 12)
    md = await upload(client, "a.md", f"# 第一章 雨夜\n\n{body}\n\n# 第二章 晨光\n\n{body}".encode())
    assert [c["title"] for c in md.json()["chapters"]] == ["第一章 雨夜", "第二章 晨光"]
    docx = make_docx([("H", "雨夜"), (None, body), ("H", "晨光"), (None, body)], styles={"H": "heading 2"})
    resp = await upload(client, "b.docx", docx)
    assert resp.status_code == 201 and [c["title"] for c in resp.json()["chapters"]] == ["雨夜", "晨光"]
    epub = make_epub(
        [("c1.xhtml", f"<h1>第一章 雨夜</h1><p>{body}</p>"), ("c2.xhtml", f"<h1>第二章</h1><p>{body}</p>")]
    )
    resp = await upload(client, "c.epub", epub)
    assert resp.status_code == 201 and resp.json()["kind"] == "epub" and resp.json()["chapter_count"] == 2
    big5 = await upload(client, "d.txt", f"第一章 天下大勢\n{'話說天下大勢，分久必合。' * 30}".encode("big5"))
    assert big5.json()["chapters"][0]["title"] == "第一章 天下大勢"
    for name in ("e.pdf", "f.json"):
        resp = await upload(client, name, b"%PDF-1.4")
        assert resp.status_code == 400 and resp.json()["detail"] == splitter.UNSUPPORTED_TYPE
    monkeypatch.setattr(splitter, "MAX_TEXT_CHARS", 100)
    resp = await upload(client, "g.txt", ("第一章 x\n" + "字" * 200).encode())
    assert resp.status_code == 400 and "无法导入" in resp.json()["detail"]


async def test_upload_body_limit_before_parsing(client):
    await register(client)
    resp = await upload(client, "big.txt", b"x" * (kb_parser.MAX_FILE_BYTES + MULTIPART_OVERHEAD + 1))
    assert resp.status_code == 400 and resp.json()["detail"] == kb_parser.TOO_LARGE


async def test_pending_limit_and_purge(app, client):
    await register(client)
    ids = [(await upload(client)).json()["id"] for _ in range(5)]
    resp = await upload(client)
    # 其他 409 不带修订号标记（前端靠 code 区分是否需要重新拉取）
    assert resp.status_code == 409 and "最多保留 5 个" in resp.json()["detail"] and "code" not in resp.json()
    async with app.state.sessionmaker() as db:
        await db.execute(update(BookImport).where(BookImport.id == ids[0]).values(expires_at=_past()))
        await db.commit()
    assert sorted(i["id"] for i in (await client.get(API)).json()) == ids[1:]
    assert (await client.get(f"{API}/{ids[0]}")).status_code == 404
    assert (await upload(client)).status_code == 201
    assert (await client.delete(f"{API}/{ids[1]}")).status_code == 204
    assert (await client.get(f"{API}/{ids[1]}")).status_code == 404


def _past() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=1)


async def test_ops_resplit_and_revision(client):
    rec = await _setup(client)
    url = f"{API}/{rec['id']}"
    resp = await client.post(f"{url}/ops", json={"op": "rename", "index": 1, "title": "第1章 夜雨", "revision": 0})
    assert (
        resp.status_code == 200 and resp.json()["revision"] == 1 and resp.json()["chapters"][0]["title"] == "第1章 夜雨"
    )
    # 修订号过期（重复点击）
    stale = await client.post(f"{url}/ops", json={"op": "merge_next", "index": 1, "revision": 0})
    assert stale.status_code == 409 and stale.json() == {
        "detail": "解析结果已被修改，请刷新后重试",
        "code": "stale_revision",
    }
    stale = await client.post(f"{url}/resplit", json={"mode": "auto", "revision": 0})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_revision"
    merged = (await client.post(f"{url}/ops", json={"op": "merge_next", "index": 1, "revision": 1})).json()
    assert merged["chapter_count"] == 2 and merged["revision"] == 2
    split = await client.post(f"{url}/ops", json={"op": "split", "index": 1, "line": 9, "revision": 2})
    assert split.status_code == 200 and split.json()["chapter_count"] == 3
    started = await client.post(f"{url}/ops", json={"op": "start_volume", "index": 2, "title": "插卷", "revision": 3})
    assert [c["volume_no"] for c in started.json()["chapters"]] == [1, 2, 3]
    bad = await client.post(f"{url}/ops", json={"op": "split", "index": 1, "line": 1, "revision": 4})
    assert bad.status_code == 400 and "第 1 行" in bad.json()["detail"]
    assert (await client.post(f"{url}/ops", json={"op": "nope", "index": 1, "revision": 4})).status_code == 422

    for pattern, message in (("(a+)+", "嵌套量词"), ("a" * 101, "422"), ("^不存在$", "没有匹配到任何标题")):
        resp = await client.post(f"{url}/resplit", json={"mode": "regex", "pattern": pattern, "revision": 4})
        if message == "422":
            assert resp.status_code == 422
        else:
            assert resp.status_code == 400 and message in resp.json()["detail"]
    regex = (await client.post(f"{url}/resplit", json={"mode": "regex", "pattern": r"^第\d章", "revision": 4})).json()
    # 正则重切：清空校正、不识别分卷
    assert regex["split_mode"] == "regex" and regex["split_pattern"] == r"^第\d章" and regex["revision"] == 5
    assert [(c["title"], c["volume_title"]) for c in regex["chapters"]] == [
        ("第1章 雨夜", ""),
        ("第2章 晨光", ""),
        ("第3章 远行", ""),
    ]
    auto = (await client.post(f"{url}/resplit", json={"mode": "auto", "revision": 5})).json()
    assert auto["split_mode"] == "auto" and auto["split_pattern"] is None
    assert auto["chapters"] == rec["chapters"]


async def test_estimate(client):
    rec = await _setup(client)
    resp = await client.post(
        f"{API}/{rec['id']}/estimate",
        json={"import_range": [1, 3], "reverse_range": [2, 3], "extract_style": True, "deep_analyze": True},
    )
    est = resp.json()
    assert est["import_chapters"] == 3 and est["batches"] == 3
    assert est["digest_input"] == 3 * (2000 + 1500) + est["reverse_chars"]
    assert est["synthesize_input"] == 40000 and est["style_input"] == 6000
    assert est["analyze_input"] == est["import_chars"] + 3 * 3000
    assert est["total_input"] == sum(
        est[k] for k in ("digest_input", "synthesize_input", "style_input", "analyze_input")
    )
    off = (
        await client.post(
            f"{API}/{rec['id']}/estimate",
            json={"import_range": [1, 1], "reverse_range": [1, 1], "extract_style": False, "deep_analyze": False},
        )
    ).json()
    assert off["style_input"] == 0 and off["analyze_input"] == 0 and off["batches"] == 1
    bad = await client.post(f"{API}/{rec['id']}/estimate", json={"import_range": [2, 3], "reverse_range": [1, 3]})
    assert bad.status_code == 400 and bad.json()["detail"] == "反推范围必须在导入范围之内"


async def test_start_validation_and_rollback(app, client, monkeypatch):
    rec = await _setup(client)
    for body, status, detail in (
        ({"import_range": [0, 3]}, 400, "导入范围不正确"),
        ({"import_range": [1, 4]}, 400, "导入范围不正确"),
        ({"reverse_range": [1, 4]}, 400, "反推范围必须在导入范围之内"),
        ({"revision": 7}, 409, "解析结果已被修改，请刷新后重试"),
        ({"title": "   "}, 400, "书名不能为空"),
    ):
        resp = await _start(client, rec, **body)
        assert (resp.status_code, resp.json()["detail"]) == (status, detail)
        assert (resp.json().get("code") == "stale_revision") == (status == 409)
    monkeypatch.setattr(splitter, "MAX_IMPORT_CHAPTERS", 2)
    resp = await _start(client, rec)
    assert resp.status_code == 400 and "一次最多导入 2 章" in resp.json()["detail"]
    monkeypatch.setattr(splitter, "MAX_IMPORT_CHAPTERS", 3000)

    async def boom(*_args, **_kwargs):
        raise ConflictError("模拟失败")

    monkeypatch.setattr(task_service, "create_task", boom)
    resp = await _start(client, rec)
    assert resp.status_code == 409 and resp.json()["detail"] == "模拟失败"
    monkeypatch.undo()
    # 没有可用的 AI 配置（validate_and_claim 在同一事务内失败）
    resp = await _start(client, rec, ai_config_id=99999)
    assert resp.status_code == 404
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count(Project.id))) == 0
        assert await db.scalar(select(func.count(ChapterVersion.id))) == 0
    assert (await client.get(f"{API}/{rec['id']}")).status_code == 200


# ---------------- 反推任务 ----------------


async def test_start_partial_range_renumbers_and_maps_reverse_range(app, client, fake_llm):
    """导入第 2–3 章、反推第 3 章：项目里重新编号为 1–2，任务的反推范围换算为新章号 [2, 2]，只通读这一章。"""
    rec = await _setup(client)
    fake_llm.push(DIGESTS[2])
    fake_llm.push(DIGESTS[3])
    fake_llm.push(SYNTHESIZE)
    resp = await _start(client, rec, import_range=[2, 3], reverse_range=[3, 3], extract_style=False)
    pid, task_id = resp.json()["project_id"], resp.json()["task"]["id"]
    async with app.state.sessionmaker() as db:
        task = await db.get(Task, task_id)
        assert task.input["request"]["reverse_chapter_nos"] == [2, 2]
    done, _ = await _run(client, resp)
    assert done["task"]["status"] == "succeeded"
    data = await _project_data(client, pid)
    assert [(c["chapter_no"], c["title"]) for c in data["chapters"]] == [(1, "第2章 晨光"), (2, "第3章 远行")]
    # 只通读了新第 2 章（原第 3 章，分两段），摘要只写到它
    calls = _digest_calls(fake_llm)
    assert len(calls) == 2 and all("【第2章 第3章 远行" in _prompt(c) for c in calls)
    assert [o["summary"] for o in data["outlines"]] == ["", "上半；下半"]


async def test_concurrent_op_with_same_revision_rejected(app, client):
    """两个请求读到同一修订号：先提交的生效，后提交的被条件 UPDATE 拦下（409 + code），不覆盖前者。"""
    rec = await _setup(client)
    async with app.state.sessionmaker() as db:
        user_id = await db.scalar(select(BookImport.user_id).where(BookImport.id == rec["id"]))
        record = await book_service.get_owned_import(db, user_id, rec["id"])
        # 另一个请求先完成了一次校正
        first = await client.post(
            f"{API}/{rec['id']}/ops", json={"op": "rename", "index": 1, "title": "甲", "revision": 0}
        )
        assert first.status_code == 200
        with pytest.raises(ConflictError) as exc:
            await book_service._save_chapters(db, record, 0, {"chapters": record.chapters[:1]})
        assert exc.value.extra == {"code": "stale_revision"}
    latest = (await client.get(f"{API}/{rec['id']}")).json()
    assert latest["revision"] == 1 and latest["chapter_count"] == 3 and latest["chapters"][0]["title"] == "甲"


async def test_full_flow(app, client, fake_llm):
    rec = await _setup(client)
    _push_all(fake_llm)
    resp = await _start(client, rec)
    pid = resp.json()["project_id"]
    assert resp.json()["task"]["kind"] == "book.import" and resp.json()["task"]["title"] == "拆书反推：《星海》"
    done, events = await _run(client, resp)
    task, result = done["task"], done["result"]
    assert task["status"] == "succeeded", task
    stats = result["stats"]
    assert {k: stats[k] for k in reverse.STAT_KEYS} == {
        "summaries": 3,
        "characters": 2,
        "organizations": 1,
        "world": 1,
        "careers": 1,
        "relations": 1,
        "analyses_queued": 0,
    }
    assert set(stats["project_fields"]) == {"synopsis", "theme", "tags", "selling_points", "audience"}
    assert result["batches_total"] == 4 and result["batches_done"] == 4 and result["phase"] == "done"
    assert [e[1] for e in events if e[0] == "batch_end"] == [
        {"index": i, "total": 4, "state": "done"} for i in range(4)
    ]
    # TaskOut.checkpoint 只给摘要
    assert task["checkpoint"]["stats"] == stats and "findings" not in task["checkpoint"]
    assert task["progress"] == 100

    calls = _digest_calls(fake_llm)
    assert len(calls) == 4 and "【第3章 第3章 远行（第1/2段）】" in _prompt(calls[2])
    # 已知角色名单传入后续批次
    assert "已知角色" not in _prompt(calls[0]) and "林远（小林）" in _prompt(calls[1])
    assert "林远（小林、林师兄）" in _prompt(calls[2])

    data = await _project_data(client, pid)
    project = data["project"]
    assert project["status"] == "writing" and project["genre"] == "玄幻"  # 已填的题材不被覆盖
    assert project["synopsis"] == "少年林远踏上修行之路。" and project["tags"] == ["仙侠", "成长"]
    assert project["wizard_progress"]["done"] == ["world", "careers", "characters", "outline"]
    assert [(o["chapter_no"], o["volume_no"], o["summary"]) for o in data["outlines"]] == [
        (1, 1, "第1章摘要"),
        (2, 1, "第2章摘要"),
        (3, 2, "上半；下半"),
    ]
    chapters = data["chapters"]
    assert [(c["chapter_no"], c["title"], c["status"]) for c in chapters] == [
        (1, "第1章 雨夜", "done"),
        (2, "第2章 晨光", "done"),
        (3, "第3章 远行", "done"),
    ]
    versions = (await client.get(f"/api/v1/projects/{pid}/chapters/{chapters[0]['id']}/versions")).json()
    assert [(v["seq"], v["source"]) for v in versions] == [(1, "import")]
    chars = {c["name"]: c for c in data["characters"]}
    assert set(chars) == {"林远", "苏晴"} and chars["林远"]["aliases"] == ["小林", "林师兄"]
    assert chars["林远"]["always_in_context"] is True
    org = data["organizations"][0]
    assert org["name"] == "天剑宗" and org["description"] == "北境第一大宗"
    members = (await client.get(f"/api/v1/projects/{pid}/organizations/{org['id']}/members")).json()
    assert [(m["character_name"], m["position"]) for m in members] == [("林远", "弟子")]
    assert [(w["title"], w["category"]) for w in data["world"]] == [("北境", "place")]
    assert [s["name"] for s in data["careers"][0]["stages"]] == ["练气", "筑基"]
    assert len(data["relationships"]) == 1 and data["relationships"][0]["description"] == "同门"
    # 风格：保存为「《书名》原文风格」并设为项目默认
    style = (await client.get(f"/api/v1/styles/{stats['style_id']}")).json()
    assert style["name"] == "《星海》原文风格" and style["description"] == "冷静克制的叙事"
    assert project["default_style_id"] == stats["style_id"]
    # 解析记录已删除；生成记录
    assert (await client.get(f"{API}/{rec['id']}")).status_code == 404
    async with app.state.sessionmaker() as db:
        kinds = list(await db.scalars(select(GenerationLog.kind).where(GenerationLog.project_id == pid)))
        assert sorted(kinds) == sorted(["book_import.digest"] * 4 + ["book_import.synthesize", "book_import.style"])
    logs = (await client.get(f"/api/v1/projects/{pid}/chapters/{chapters[0]['id']}/generation-logs")).json()
    assert [(log["kind"], log["status"], log["task_id"]) for log in logs] == [
        ("book_import.digest", "success", task["id"])
    ]
    assert logs[0]["params"]["prompt"] == {
        "key": "book_import.digest",
        "source": "default",
        "override_version_id": None,
    }


async def test_existing_data_not_overwritten(app, client, fake_llm):
    rec = await _setup(client)
    gate = Gate()
    fake_llm.push([gate, DIGESTS[0]])
    for d in DIGESTS[1:]:
        fake_llm.push(d)
    fake_llm.push(SYNTHESIZE)
    resp = await _start(client, rec, extract_style=False)
    pid = resp.json()["project_id"]
    base = f"/api/v1/projects/{pid}"
    await gate.wait_reached()
    # 通读期间用户手动建了同名角色（别名）、改了第 2 章大纲摘要、填了简介
    manual = (
        await client.post(f"{base}/characters", json={"name": "晴儿", "aliases": ["苏晴"], "profile": "手写"})
    ).json()
    outlines = (await client.get(f"{base}/outlines")).json()
    await client.patch(f"{base}/outlines/{outlines[1]['id']}", json={"summary": "手写摘要"})
    await client.patch(base, json={"synopsis": "我的简介", "tags": ["手写标签"]})
    await client.post(f"{base}/organizations", json={"name": "天剑宗", "description": "手写宗门"})
    gate.release()
    done, _ = await _run(client, resp)
    assert done["task"]["status"] == "succeeded"
    stats = done["result"]["stats"]
    assert stats["characters"] == 1 and stats["organizations"] == 0 and stats["summaries"] == 2
    assert stats["style_id"] is None
    assert "synopsis" not in stats["project_fields"] and "tags" not in stats["project_fields"]
    data = await _project_data(client, pid)
    assert data["project"]["synopsis"] == "我的简介" and data["project"]["tags"] == ["手写标签"]
    assert [o["summary"] for o in data["outlines"]] == ["第1章摘要", "手写摘要", "上半；下半"]
    chars = {c["name"]: c for c in data["characters"]}
    assert set(chars) == {"晴儿", "林远"} and chars["晴儿"]["profile"] == "手写" and manual["id"] == chars["晴儿"]["id"]
    assert [o["description"] for o in data["organizations"]] == ["手写宗门"]
    # 关系一端（苏晴）对应到手动角色「晴儿」的别名，照常写入
    assert len(data["relationships"]) == 1


async def test_existing_relation_not_overwritten(client, fake_llm):
    """两人之间已有关系（用户事先手写）时不再写入，也不改动原关系；角色同名（含别名）跳过。"""
    rec = await _setup(client)
    gate = Gate()
    fake_llm.push([gate, DIGESTS[0]])
    for d in DIGESTS[1:]:
        fake_llm.push(d)
    fake_llm.push(SYNTHESIZE)
    resp = await _start(client, rec, extract_style=False)
    base = f"/api/v1/projects/{resp.json()['project_id']}"
    await gate.wait_reached()
    lin = (await client.post(f"{base}/characters", json={"name": "林远"})).json()
    su = (await client.post(f"{base}/characters", json={"name": "晴儿", "aliases": ["苏晴"]})).json()
    created = await client.post(
        f"{base}/relationships",
        json={"a_id": lin["id"], "b_id": su["id"], "role_a": "仇人", "role_b": "仇人", "description": "手写关系"},
    )
    assert created.status_code == 201, created.text
    gate.release()
    done, _ = await _run(client, resp)
    assert done["task"]["status"] == "succeeded"
    assert done["result"]["stats"]["relations"] == 0 and done["result"]["stats"]["characters"] == 0
    # 已有关系是预期内的跳过，不是写入失败：不产生警告
    assert done["result"]["warnings"] == []
    rels = (await client.get(f"{base}/relationships")).json()
    assert [r["description"] for r in rels] == ["手写关系"]


@pytest.mark.parametrize("action", ["resume", "skip", "finish"])
async def test_failure_pauses_then_resume_skip_or_finish(app, client, fake_llm, action):
    rec = await _setup(client)
    fake_llm.push(DIGESTS[0])
    for _ in range(3):
        fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    resp = await _start(client, rec, extract_style=False)
    pid = resp.json()["project_id"]
    paused, _ = await _run(client, resp)
    task = paused["task"]
    assert task["status"] == "paused" and task["step_cursor"] == 1 and task["finished_at"] is None
    reason = paused["result"]["pause_reason"]
    assert reason == {"reason": "failed", "phase": "digest", "batch_index": 1, "error": "模型调用失败：额度不足"}
    assert task["checkpoint"]["pause_reason"] == reason
    assert len(_digest_calls(fake_llm)) == 4  # 1 次成功 + 第 2 批 3 次（自动重试 2 次）
    async with app.state.sessionmaker() as db:
        errors = await db.scalar(
            select(func.count(GenerationLog.id)).where(GenerationLog.project_id == pid, GenerationLog.status == "error")
        )
        assert errors == 3
    task_url = f"/api/v1/tasks/{task['id']}"

    if action == "finish":
        resp = await client.post(f"{task_url}/finish")
        assert resp.status_code == 200 and resp.json()["status"] == "succeeded"
        final = resp.json()
        async with app.state.sessionmaker() as db:
            row = await db.get(Task, task["id"])
            assert row.result["ended_early"] is True and row.result["batches_done"] == 1
        assert len((await client.get(f"/api/v1/projects/{pid}/chapters")).json()) == 3
        assert final["checkpoint"]["batches_done"] == 1
        return

    if action == "resume":
        for d in DIGESTS[1:]:
            fake_llm.push(d)
        fake_llm.push(SYNTHESIZE)
        resp = await client.post(f"{task_url}/resume")
        assert resp.status_code == 202
        events = await stream_task(client, task["id"])
        done = events[-1][1]
        assert done["task"]["status"] == "succeeded" and done["result"]["batches_done"] == 4
        assert done["result"]["stats"]["summaries"] == 3
        # 第 1 批没有重新调用
        assert sum("【第1章" in _prompt(c) for c in _digest_calls(fake_llm)) == 1
        return

    for d in DIGESTS[2:]:
        fake_llm.push(d)
    fake_llm.push(SYNTHESIZE)
    resp = await client.post(f"{API}/tasks/{task['id']}/skip")
    assert resp.status_code == 202, resp.text
    events = await stream_task(client, task["id"])
    done = events[-1][1]
    result = done["result"]
    assert done["task"]["status"] == "succeeded"
    assert result["batches_done"] == 3 and result["batches_skipped"] == 1
    assert result["skipped"] == [{"stage": "digest", "batch_index": 1, "chapter_nos": [2]}]
    outlines = (await client.get(f"/api/v1/projects/{pid}/outlines")).json()
    assert [o["summary"] for o in outlines] == ["第1章摘要", "", "上半；下半"]


async def test_skip_requires_failed_pause(client, fake_llm):
    rec = await _setup(client)
    _push_all(fake_llm)
    done, _ = await _run(client, await _start(client, rec))
    resp = await client.post(f"{API}/tasks/{done['task']['id']}/skip")
    assert resp.status_code == 409 and resp.json()["detail"] == reverse.SKIP_CONFLICT


async def test_synthesize_failure_skip_uses_merged_findings(client, fake_llm):
    rec = await _setup(client)
    for d in DIGESTS:
        fake_llm.push(d)
    for _ in range(3):
        fake_llm.push("这不是 JSON")
    resp = await _start(client, rec, extract_style=False)
    pid = resp.json()["project_id"]
    paused, _ = await _run(client, resp)
    assert paused["task"]["status"] == "paused"
    reason = paused["result"]["pause_reason"]
    assert reason["phase"] == "synthesize" and reason["batch_index"] is None and "AI 输出解析失败" in reason["error"]
    resp = await client.post(f"{API}/tasks/{paused['task']['id']}/skip")
    assert resp.status_code == 202
    done = (await stream_task(client, paused["task"]["id"]))[-1][1]
    assert done["task"]["status"] == "succeeded"
    assert done["result"]["skipped"] == [{"stage": "synthesize", "chapter_nos": []}]
    data = await _project_data(client, pid)
    # 代码合并的结果：角色按别名合并，项目字段留空
    chars = {c["name"]: c for c in data["characters"]}
    assert set(chars) == {"林远", "苏晴"} and chars["林远"]["aliases"] == ["小林", "林师兄"]
    assert data["project"]["synopsis"] == "" and [o["name"] for o in data["organizations"]] == ["天剑宗"]
    assert [r["description"] for r in data["relationships"]] == [""]


async def test_interrupt_then_retry_does_not_repeat_done_batches(app, client, fake_llm):
    rec = await _setup(client)
    gate = Gate()
    fake_llm.push(DIGESTS[0])
    fake_llm.push([gate, DIGESTS[1]])
    task_id = (await _start(client, rec, extract_style=False)).json()["task"]["id"]
    await gate.wait_reached()
    engine = app.state.task_engine
    await engine.stop()
    task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
    assert task["status"] == "interrupted" and task["step_cursor"] == 1
    assert task["checkpoint"]["batches_done"] == 1
    await engine.start()
    for d in DIGESTS[1:]:
        fake_llm.push(d)
    fake_llm.push(SYNTHESIZE)
    resp = await client.post(f"/api/v1/tasks/{task_id}/retry")
    assert resp.status_code == 202
    done = (await stream_task(client, resp.json()["id"]))[-1][1]
    assert done["task"]["status"] == "succeeded" and done["task"]["retry_of_id"] == task_id
    assert done["result"]["stats"]["summaries"] == 3
    prompts = [_prompt(c) for c in _digest_calls(fake_llm)]
    assert sum("【第1章" in p for p in prompts) == 1 and sum("【第2章" in p for p in prompts) == 2


@pytest.mark.parametrize("settings_overrides", [{"task_max_per_user": 1}])
async def test_deep_analyze_queued_after_characters(app, client, fake_llm):
    rec = await _setup(client)
    _push_all(fake_llm)
    for _ in range(3):
        fake_llm.push(analysis_json())
    done, _ = await _run(client, await _start(client, rec, deep_analyze=True))
    assert done["task"]["status"] == "succeeded" and done["result"]["stats"]["analyses_queued"] == 3
    pid = done["task"]["project_id"]
    async with app.state.sessionmaker() as db:
        ids = list(
            await db.scalars(
                select(Task.id).where(Task.project_id == pid, Task.kind == "chapter.analyze").order_by(Task.id)
            )
        )
    assert len(ids) == 3
    for tid in ids:
        assert (await wait_task(client, tid))["status"] == "succeeded"
    analysis_calls = [c for c in fake_llm.calls if "【输出 JSON 结构】" in _prompt(c)]
    # 分析时角色已经写入：提示词里有本章涉及角色的当前状态
    assert len(analysis_calls) == 3 and all("林远" in _prompt(c) and "苏晴" in _prompt(c) for c in analysis_calls)


async def test_style_keeps_existing_default_and_failure_only_warns(app, client, fake_llm):
    rec = await _setup(client)
    gate = Gate()
    fake_llm.push([gate, DIGESTS[0]])
    for d in DIGESTS[1:]:
        fake_llm.push(d)
    fake_llm.push(SYNTHESIZE)
    fake_llm.push(STYLE)
    resp = await _start(client, rec)
    pid = resp.json()["project_id"]
    await gate.wait_reached()
    preset = next(s for s in (await client.get("/api/v1/styles")).json() if s["preset_key"])
    await client.patch(f"/api/v1/projects/{pid}", json={"default_style_id": preset["id"]})
    gate.release()
    done, _ = await _run(client, resp)
    assert done["result"]["stats"]["style_id"] is not None
    assert (await client.get(f"/api/v1/projects/{pid}")).json()["default_style_id"] == preset["id"]

    # 第二本：风格提取三次都失败 → 只记警告，任务照常完成；同名风格加序号
    rec2 = (await upload(client)).json()
    for d in DIGESTS:
        fake_llm.push(d)
    fake_llm.push(SYNTHESIZE)
    for _ in range(3):
        fake_llm.push(AIProviderError("模型调用失败：超时"))
    done2, _ = await _run(client, await _start(client, rec2))
    assert done2["task"]["status"] == "succeeded" and done2["result"]["stats"]["style_id"] is None
    assert "写作风格提取失败：模型调用失败：超时" in done2["result"]["warnings"]
    rec3 = (await upload(client)).json()
    _push_all(fake_llm)
    done3, _ = await _run(client, await _start(client, rec3))
    style = (await client.get(f"/api/v1/styles/{done3['result']['stats']['style_id']}")).json()
    assert style["name"] == "《星海》原文风格（2）"


async def test_prompt_override_applies(app, client, fake_llm):
    rec = await _setup(client)
    tpl = registry.TEMPLATES["book_import.digest"]
    resp = await client.put(
        "/api/v1/prompt-templates/book_import.digest", json={"system": tpl.system, "user": tpl.user + "\n【覆盖标记】"}
    )
    assert resp.status_code == 200, resp.text
    items = {i["key"]: i for i in (await client.get("/api/v1/prompt-templates")).json()}
    assert items["book_import.synthesize"]["category"] == "book_import"
    _push_all(fake_llm)
    done, _ = await _run(client, await _start(client, rec))
    assert done["task"]["status"] == "succeeded"
    assert all(_prompt(c).endswith("【覆盖标记】") for c in _digest_calls(fake_llm))
    async with app.state.sessionmaker() as db:
        log = await db.scalar(select(GenerationLog).where(GenerationLog.kind == "book_import.digest").limit(1))
        assert log.params["prompt"]["source"] == "override"


async def test_deleted_chapter_skipped_with_warning(client, fake_llm):
    rec = await _setup(client)
    gate = Gate()
    fake_llm.push([gate, DIGESTS[0]])
    fake_llm.push(DIGESTS[2])
    fake_llm.push(DIGESTS[3])
    fake_llm.push(SYNTHESIZE)
    resp = await _start(client, rec, extract_style=False)
    pid = resp.json()["project_id"]
    await gate.wait_reached()
    chapters = (await client.get(f"/api/v1/projects/{pid}/chapters")).json()
    assert (await client.delete(f"/api/v1/projects/{pid}/chapters/{chapters[1]['id']}")).status_code == 204
    gate.release()
    done, _ = await _run(client, resp)
    assert done["task"]["status"] == "succeeded"
    assert "第 2 章已被删除，通读时已跳过" in done["result"]["warnings"]
    assert done["result"]["batches_done"] == 4 and len(_digest_calls(fake_llm)) == 3


async def test_second_import_task_rejected(client, fake_llm):
    rec = await _setup(client)
    for _ in range(3):
        fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    paused, _ = await _run(client, await _start(client, rec))
    assert paused["task"]["status"] == "paused"
    rec2 = (await upload(client)).json()
    resp = await _start(client, rec2)
    assert resp.status_code == 409 and resp.json()["detail"] == reverse.OPEN_CONFLICT and "code" not in resp.json()
    assert (await client.get(f"{API}/{rec2['id']}")).status_code == 200
    assert len((await client.get("/api/v1/projects")).json()) == 1
    # 结束后可以再发起
    await client.post(f"/api/v1/tasks/{paused['task']['id']}/finish")
    _push_all(fake_llm)
    done, _ = await _run(client, await _start(client, rec2))
    assert done["task"]["status"] == "succeeded"


async def test_cross_user_isolation(client, make_client, fake_llm):
    rec = await _setup(client)
    for _ in range(3):
        fake_llm.push(AIProviderError("模型调用失败：额度不足"))
    resp = await upload(client)
    other = resp.json()
    paused, _ = await _run(client, await _start(client, other))
    bob = make_client()
    await register(bob, "bob")
    await create_ai_config(bob)
    url = f"{API}/{rec['id']}"
    checks = [
        bob.get(url),
        bob.get(f"{url}/chapters/1"),
        bob.post(f"{url}/ops", json={"op": "delete", "index": 1, "revision": 0}),
        bob.post(f"{url}/resplit", json={"mode": "auto", "revision": 0}),
        bob.post(f"{url}/estimate", json={"import_range": [1, 1], "reverse_range": [1, 1]}),
        bob.post(f"{url}/start", json={"revision": 0, "title": "x", "import_range": [1, 1], "reverse_range": [1, 1]}),
        bob.delete(url),
    ]
    for coro in checks:
        resp = await coro
        assert resp.status_code == 404 and resp.json()["detail"] == "解析记录不存在", resp.text
    resp = await bob.post(f"{API}/tasks/{paused['task']['id']}/skip")
    assert resp.status_code == 404 and resp.json()["detail"] == "任务不存在"
    assert (await bob.get(API)).json() == []


async def test_book_templates_render_and_locked_vars(client):
    from app.ai.prompts import build_book_digest_messages, build_book_synthesize_messages
    from app.ai.prompts.formats import BOOK_DIGEST_FORMAT, BOOK_SYNTHESIZE_FORMAT

    digest_msgs = build_book_digest_messages(
        project_info="书名：《星海》", known_characters="", chapters_text="【第1章 雨夜】\n正文"
    )
    assert [m["role"] for m in digest_msgs] == ["system", "user"]
    user = digest_msgs[-1]["content"]
    assert user.endswith(BOOK_DIGEST_FORMAT) and "已知角色" not in user and "【第1章 雨夜】\n正文" in user
    synth = build_book_synthesize_messages(
        project_info="书名：《星海》",
        summaries="第1章 雨夜：摘要",
        characters="",
        organizations="",
        world="",
        careers="",
        relations="",
    )[-1]["content"]
    assert (
        synth.endswith(BOOK_SYNTHESIZE_FORMAT)
        and "【章节摘要】\n第1章 雨夜：摘要" in synth
        and "【角色草稿" not in synth
    )

    await register(client)
    detail = (await client.get("/api/v1/prompt-templates/book_import.digest")).json()
    assert {b["name"] for b in detail["locked"]} == {"json_rule", "output_format"}
    tpl = registry.TEMPLATES["book_import.synthesize"]
    resp = await client.put(
        "/api/v1/prompt-templates/book_import.synthesize",
        json={"system": tpl.system, "user": tpl.user.replace("{{ output_format }}", "")},
    )
    assert resp.status_code == 400 and "output_format" in resp.json()["detail"]
