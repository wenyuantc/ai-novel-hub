"""同一条审稿讨论的阅读、书稿文件与模型出站隔离。"""

import hashlib
import io
import json
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

from tests.helpers import create_ai_config, parse_sse
from tests.test_scenes import setup_scene

PROSE = "旧车站的灯一直亮着。"
COMMENT = "ANNOTATION_ONLY_73BA"
REPLY = "REPLY_ONLY_D79F"


def assert_prose_only(text):
    assert PROSE in text
    assert COMMENT not in text
    assert REPLY not in text


def check_book_archive(content, body_file):
    # 同时检查压缩包其他成员，避免正文干净、附带的批注或元数据仍泄露。
    with ZipFile(io.BytesIO(content)) as book:
        for name in book.namelist():
            data = book.read(name)
            assert COMMENT.encode() not in data, name
            assert REPLY.encode() not in data, name
        root = ElementTree.fromstring(book.read(body_file))
        assert_prose_only("".join(root.itertext()))


async def test_annotation_and_reply_stay_out_of_reader_exports_and_model(client, fake_llm):
    project, chapter, chapter_url, scene = await setup_scene(client, PROSE)
    project_url = f"/api/v1/projects/{project['id']}"
    threads_url = f"{chapter_url}/annotations"
    created = await client.post(threads_url, json={
        "scene_id": scene["id"], "expected_content_revision": chapter["content_revision"],
        "start": 0, "end": len(PROSE), "selection_hash": hashlib.sha256(PROSE.encode()).hexdigest(),
        "body": COMMENT, "operation_id": str(uuid4()),
    })
    assert created.status_code == 201, created.text
    messages_url = f"{threads_url}/{created.json()['id']}/messages"
    reply = await client.post(messages_url, json={"body": REPLY, "operation_id": str(uuid4())})
    assert reply.status_code == 201, reply.text
    discussion = await client.get(messages_url)
    assert discussion.status_code == 200, discussion.text
    assert [row["body"] for row in discussion.json()["items"]] == [COMMENT, REPLY]
    backup = await client.get(f"{project_url}/backup")
    assert backup.status_code == 200, backup.text
    saved_messages = backup.json()["tables"]["annotation_messages"]
    assert [row["body"] for row in saved_messages] == [COMMENT, REPLY]

    reader = await client.get(f"{project_url}/reader/chapters/{chapter['id']}")
    assert reader.status_code == 200, reader.text
    assert_prose_only(json.dumps(reader.json(), ensure_ascii=False))

    for fmt in ("txt", "md", "docx"):
        manuscript = await client.post(f"{project_url}/export-document", json={"format": fmt})
        assert manuscript.status_code == 200, manuscript.text
        if fmt == "docx":
            check_book_archive(manuscript.content, "word/document.xml")
        else:
            assert_prose_only(manuscript.text)
    epub = await client.get(f"{project_url}/export", params={"format": "epub"})
    assert epub.status_code == 200, epub.text
    check_book_archive(epub.content, "OEBPS/ch-1.xhtml")

    await create_ai_config(client)
    revisions = (await client.get(f"{chapter_url}/scenes")).json()
    fake_llm.push("灯还亮着。")
    rewrite = await client.post(f"{chapter_url}/scenes/{scene['id']}/rewrite", json={
        "op": "rewrite", "start": 0, "end": len(PROSE),
        "expected_content_revision": revisions["content_revision"],
        "expected_planning_revision": revisions["planning_revision"],
    })
    assert rewrite.status_code == 200, rewrite.text
    event, result = parse_sse(rewrite.text)[-1]
    assert event == "done" and result["original"] == PROSE
    assert_prose_only(json.dumps([call["messages"] for call in fake_llm.calls], ensure_ascii=False))
