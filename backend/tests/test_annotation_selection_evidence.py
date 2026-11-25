from uuid import uuid4

from app.services.scene_service import content_fingerprint
from tests.test_scenes import setup_scene


async def test_plain_http_selection_checks_exact_text_and_reuses_hash_receipt(client):
    phrase = "🙂相同\r\n原句"
    _, _, url, scene = await setup_scene(client, phrase + phrase)
    path = f"{url}/annotations"
    body = {
        "scene_id": scene["id"], "expected_content_revision": 0, "start": len(phrase), "end": len(phrase) * 2,
        "operation_id": str(uuid4()), "body": "这里的第二句",
    }
    assert (await client.post(path, json=body)).status_code == 422
    digest = content_fingerprint(phrase)
    ambiguous = {**body, "selected_text": phrase, "selection_hash": digest}
    assert (await client.post(path, json=ambiguous)).status_code == 422
    assert (await client.post(path, json={**body, "selected_text": phrase.replace("\r\n", "\n")})).status_code == 409
    succeeded = await client.post(path, json={**body, "selected_text": phrase})
    assert succeeded.status_code == 201, succeeded.text
    thread = succeeded.json()
    assert thread["start"] == len(phrase) and thread["selection_hash"] == digest
    assert (await client.post(path, json={**body, "selection_hash": digest})).json() == thread
    assert (await client.post(path, json={**body, "selected_text": phrase + "！"})).status_code == 409
    assert (await client.get(path)).json()["total"] == 1
