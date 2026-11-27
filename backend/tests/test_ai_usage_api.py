"""用量的 HTTP 投影、RBAC、价格版本，以及北京时间预算边界。"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from app.models import AIConfig, Chapter, ProjectCollaborator
from app.models.ai_usage import NANOS_PER_UNIT, AiUsageCall
from app.schemas.ai_usage import ModelPriceUpdate
from app.services import ai_usage_query
from tests.helpers import create_project, register
from tests.rbac_helpers import assign_roles, create_role

_API = "/api/v1"
_ACCOUNTS = f"{_API}/ai-usage/budgets"
_ADMIN = f"{_API}/admin/ai-usage"
_JANUARY = datetime(2027, 1, 1, 0, 0, tzinfo=UTC) - timedelta(hours=8)


def _usage(actor_id: int, payer_id: int, *, started_at: datetime = _JANUARY,
           project_id: int | None = None, chapter_id: int | None = None, author_paid: bool = False,
           model: str = "personal", amount: str | None = "1", currency: str = "CNY",
           input_tokens: int | None = 20, price_period: str | None = None,
           cached_input_tokens: int | None = None, cached_input_rate: int | None = None) -> AiUsageCall:
    return AiUsageCall(
        id=str(uuid4()), actor_id=actor_id, payer_id=payer_id, started_at=started_at,
        finished_at=started_at + timedelta(seconds=1), project_id=project_id, had_project=project_id is not None,
        chapter_id=chapter_id, author_paid=author_paid, model_type="text", provider="openai_compat",
        model=model, kind="chapter.generate", status="succeeded", sent=True,
        input_tokens=input_tokens, input_source="provider" if input_tokens is not None else "unknown",
        cached_input_tokens=cached_input_tokens,
        output_tokens=0, output_source="provider", currency=currency if amount is not None else None,
        price_input_nano=3 * NANOS_PER_UNIT if amount is not None else None,
        price_cached_input_nano=None if cached_input_rate is None else cached_input_rate * NANOS_PER_UNIT,
        price_period=price_period,
        amount_nano=None if amount is None else int(Decimal(amount) * NANOS_PER_UNIT),
        pricing_status="missing_price" if amount is None else "priced",
    )


async def test_admin_all_calls_and_shared_summary_scope(app, client, make_client, system_admin):
    alice = await register(client)
    bob_client = make_client()
    bob = await register(bob_client, "bob")
    project = await create_project(client)
    async with app.state.sessionmaker() as db:
        db.add_all([
            _usage(alice["id"], alice["id"], project_id=project["id"], amount="0.000000003", price_period="peak",
                   cached_input_tokens=8, cached_input_rate=0),
            _usage(bob["id"], bob["id"], amount="90", currency="USD", cached_input_tokens=0),
            _usage(bob["id"], bob["id"], amount=None, input_tokens=None),
        ])
        await db.commit()

    assert (await client.get(_ADMIN)).status_code == 403
    delegated = await create_role(system_admin, "用量只读", ["ai_usage.read"])
    await assign_roles(system_admin, bob["id"], [delegated["id"]])
    response = await bob_client.get(_ADMIN, params={"page_size": 1})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    page = response.json()
    assert page["tracking_since"] is not None
    assert page["total"] == 3 and len(page["items"]) == 1
    assert page["summary"]["input_tokens"] == 40
    assert page["summary"]["cached_input_tokens"] == 8
    assert page["summary"]["unknown_usage_calls"] == page["summary"]["unpriced_calls"] == 1
    assert {row["currency"]: Decimal(row["amount"]) for row in page["summary"]["costs"]} == {
        "CNY": Decimal("0.000000003"), "USD": Decimal("90"),
    }

    # 空页依然使用相同的筛选口径，而且 `from` 是含端点，`to` 排除端点。
    empty = await bob_client.get(_ADMIN, params={"actor_id": bob["id"], "page": 3, "page_size": 1,
                                               "from": _JANUARY.isoformat()})
    assert empty.status_code == 200, empty.text
    assert empty.json()["total"] == 2 and empty.json()["items"] == []
    until = await bob_client.get(_ADMIN, params={"to": _JANUARY.isoformat()})
    assert until.status_code == 200, until.text
    assert until.json()["total"] == 0
    invalid = await bob_client.get(_ADMIN, params={"from": _JANUARY.isoformat(), "to": _JANUARY.isoformat()})
    assert invalid.status_code == 422

    all_items = (await bob_client.get(_ADMIN)).json()["items"]
    assert {row["project_state"] for row in all_items} == {"linked", "none"}
    assert {row["actor_name"] for row in all_items} == {"alice", "bob"}
    alice_call = next(row for row in all_items if row["actor_id"] == alice["id"])
    assert alice_call["price_period"] == "peak"
    assert alice_call["cached_input_tokens"] == 8 and alice_call["cached_input_per_million"] == "0"
    assert {row["cached_input_tokens"] for row in all_items if row["actor_id"] == bob["id"]} == {0, None}
    assert not ({"prompt", "messages", "base_url", "api_key", "config_instance_key"} & all_items[0].keys())


async def test_collaboration_price_redaction_precedes_model_filter(app, client, make_client):
    owner = await register(client)
    editor_client = make_client()
    editor = await register(editor_client, "editor")
    project = await create_project(client, title="协作作品")
    project_id = project["id"]
    async with app.state.sessionmaker() as db:
        member = ProjectCollaborator(project_id=project_id, user_id=editor["id"], role="editor", status="accepted")
        chapter = Chapter(project_id=project_id, chapter_no=1)
        db.add_all([member, chapter])
        await db.flush()
        db.add_all([
            _usage(editor["id"], editor["id"], project_id=project_id, chapter_id=chapter.id, model="editor-model"),
            _usage(editor["id"], owner["id"], project_id=project_id, chapter_id=chapter.id,
                   author_paid=True, model="owner-secret-model", price_period="off_peak",
                   cached_input_tokens=6, cached_input_rate=1),
        ])
        await db.commit()
        member_id = member.id
        chapter_id = chapter.id

    project_url = f"{_API}/projects/{project_id}/ai-usage"
    author_page = (await client.get(project_url)).json()
    assert author_page["total"] == 1 and author_page["items"][0]["model"] == "owner-secret-model"
    assert author_page["items"][0]["price_period"] == "off_peak"
    assert author_page["items"][0]["cached_input_per_million"] == "1"
    editor_page = (await editor_client.get(project_url)).json()
    assert editor_page["total"] == 2 and editor_page["summary"]["unpriced_calls"] == 0
    hidden = next(row for row in editor_page["items"] if row["billing_source"] == "author")
    assert hidden["input_tokens"] == 20 and hidden["output_tokens"] == 0
    assert hidden["cached_input_tokens"] == editor_page["summary"]["cached_input_tokens"] == 6
    assert hidden["pricing_status"] == "private"
    assert all(hidden[key] is None for key in ("provider", "model", "estimated_cost", "currency",
                                              "payer_id", "payer_name", "input_per_million",
                                              "cached_input_per_million", "price_period"))
    selected = (await editor_client.get(project_url, params={"model": "owner-secret-model"})).json()
    assert selected["total"] == 0 and selected["summary"]["costs"] == []
    assert (await editor_client.get(f"{project_url}/budgets")).status_code == 404
    pinned = await editor_client.get(project_url, params={"chapter_id": chapter_id})
    chapter_url = f"{_API}/projects/{project_id}/chapters/{chapter_id}/ai-usage"
    assert pinned.status_code == 200 and pinned.json() == (await editor_client.get(chapter_url)).json()

    async with app.state.sessionmaker() as db:
        (await db.get(ProjectCollaborator, member_id)).status = "revoked"
        await db.commit()
    assert (await editor_client.get(project_url)).status_code == 404
    existing = await editor_client.get(f"{_API}/ai-usage", params={"chapter_id": chapter_id})
    missing = await editor_client.get(f"{_API}/ai-usage", params={"chapter_id": 1_000_000})
    assert existing.status_code == missing.status_code == 404 and existing.json() == missing.json()
    history = (await editor_client.get(f"{_API}/ai-usage")).json()
    assert history["total"] == 2
    assert all(row["project_id"] is None and row["project_title"] is None and row["project_state"] == "private"
               for row in history["items"])


async def test_monthly_and_project_budget_totals(app, client, make_client):
    owner = await register(client)
    editor_client = make_client()
    editor = await register(editor_client, "editor")
    project = await create_project(client)
    project_id = project["id"]
    project_budget = f"{_API}/projects/{project_id}/ai-usage/budgets"
    for url in (_ACCOUNTS, project_budget):
        put = await client.put(f"{url}/CNY", json={"limit": "10", "threshold_percent": 80})
        assert put.status_code == 200, put.text
    denied = await editor_client.put(f"{project_budget}/CNY", json={"limit": "12", "threshold_percent": 80})
    assert denied.status_code == 404
    async with app.state.sessionmaker() as db:
        db.add_all([
            _usage(editor["id"], owner["id"], project_id=project_id, author_paid=True, amount="3",
                   started_at=_JANUARY - timedelta(microseconds=1)),
            _usage(editor["id"], owner["id"], project_id=project_id, author_paid=True, amount="8"),
            _usage(editor["id"], editor["id"], project_id=project_id, amount="9"),
            _usage(owner["id"], owner["id"], amount="1"),
            _usage(owner["id"], owner["id"], amount=None),
        ])
        await db.commit()
        monthly, = await ai_usage_query.budget_progress(db, owner["id"], now=_JANUARY)
        whole_book, = await ai_usage_query.budget_progress(db, owner["id"], project_id=project_id, now=_JANUARY)
    assert (monthly.period_start, monthly.unpriced_calls, monthly.state) == (_JANUARY, 1, "near")
    assert Decimal(monthly.spent) == Decimal("9")
    assert (whole_book.period_start, whole_book.unpriced_calls, whole_book.state) == (None, 0, "exceeded")
    assert Decimal(whole_book.spent) == Decimal("11")

    # 无预算正文不清除预算，显式 null 才清除。
    assert (await client.put(f"{project_budget}/CNY")).status_code == 400
    clear = await client.put(f"{project_budget}/CNY", content="null", headers={"Content-Type": "application/json"})
    assert clear.status_code == 200 and clear.json() is None
    assert (await client.get(project_budget)).json() == []


async def test_own_model_price_version_and_editor_identity(app, client, make_client):
    owner = await register(client)
    other = make_client()
    await register(other, "other")
    cfg = await client.post(f"{_API}/ai-configs", json={"name": "自用", "provider": "openai_compat", "model": "test"})
    assert cfg.status_code == 201, cfg.text
    url = f"{_API}/ai-configs/{cfg.json()['id']}"
    initial = (await client.get(f"{url}/price")).json()
    assert initial["price"] is None
    assert (await other.get(f"{url}/price")).status_code == 404
    body = {
        "expected_version": initial["version"],
        "price": {"currency": "CNY", "input_per_million": "0.000000001", "output_per_million": "0"},
    }
    stale_actor = await client.put(f"{url}/price", headers={"X-Editor-Actor-Id": str(owner["id"] + 1)}, json=body)
    assert stale_actor.status_code == 409 and stale_actor.json()["code"] == "editor_actor_changed"
    written = await client.put(f"{url}/price", json=body)
    assert written.status_code == 200, written.text
    assert Decimal(written.json()["price"]["input_per_million"]) == Decimal("0.000000001")
    assert Decimal(written.json()["price"]["output_per_million"]) == 0
    assert written.json()["price"]["off_peak"] is None
    async with app.state.sessionmaker() as db:
        config = await db.get(AIConfig, cfg.json()["id"])
        serialized = json.dumps([
            config.instance_key, config.provider, config.model, config.model_type,
            {"currency": "CNY", "input_per_million": "0.000000001", "output_per_million": "0", "per_image": None},
        ], ensure_ascii=False)
    assert written.json()["version"] == sha256(serialized.encode()).hexdigest()
    assert (await client.put(f"{url}/price", json=body)).status_code == 409
    assert (await client.patch(url, json={"model": "test-updated"})).status_code == 200
    assert (await client.get(f"{url}/price")).json()["price"] is None
    stale_model = await client.put(f"{url}/price", json={**body, "expected_version": written.json()["version"]})
    assert stale_model.status_code == 409 and stale_model.json()["code"] == "model_price_changed"


async def test_scheduled_price_round_trip_and_legacy_update(app, client):
    await register(client)
    config = await client.post(f"{_API}/ai-configs", json={
        "name": "分时", "provider": "openai_compat", "model": "scheduled-text",
    })
    assert config.status_code == 201, config.text
    url = f"{_API}/ai-configs/{config.json()['id']}/price"
    initial = (await client.get(url)).json()
    base = {"currency": "CNY", "input_per_million": "2", "output_per_million": "6"}
    off_peak = {"start": "23:00", "end": "07:00", "input_per_million": "0", "output_per_million": "0.125"}
    scheduled = await client.put(url, json={
        "expected_version": initial["version"], "price": {**base, "off_peak": off_peak},
    })
    assert scheduled.status_code == 200, scheduled.text
    snapshot = scheduled.json()
    assert snapshot == (await client.get(url)).json()
    legacy_off_peak = {**off_peak, "timezone": "Asia/Shanghai", "per_image": None}
    assert snapshot["price"]["off_peak"] == {**legacy_off_peak, "cached_input_per_million": None}
    async with app.state.sessionmaker() as db:
        stored = await db.get(AIConfig, config.json()["id"])
        serialized = json.dumps([
            stored.instance_key, stored.provider, stored.model, stored.model_type,
            {**base, "per_image": None, "off_peak": {
                "timezone": "Asia/Shanghai", **off_peak, "per_image": None,
            }},
        ], ensure_ascii=False)
    assert snapshot["version"] == sha256(serialized.encode()).hexdigest()

    new_zone = {**off_peak, "timezone": "America/Los_Angeles"}
    updated = await client.put(url, json={
        "expected_version": snapshot["version"], "price": {**base, "off_peak": new_zone},
    })
    assert updated.status_code == 200, updated.text
    version = updated.json()["version"]
    assert version != snapshot["version"]
    stale = await client.put(url, json={"expected_version": snapshot["version"], "price": base})
    assert stale.status_code == 409 and stale.json()["code"] == "model_price_changed"

    preserved = await client.put(url, json={
        "expected_version": version, "price": {**base, "output_per_million": "7"},
    })
    assert preserved.status_code == 200, preserved.text
    assert preserved.json()["price"]["off_peak"] == {**new_zone, "per_image": None, "cached_input_per_million": None}
    version = preserved.json()["version"]
    changed_currency = await client.put(url, json={
        "expected_version": version, "price": {**base, "currency": "USD"},
    })
    assert changed_currency.status_code == 400
    assert (await client.get(url)).json() == preserved.json()

    cleared = await client.put(url, json={
        "expected_version": version, "price": {**base, "currency": "USD", "off_peak": None},
    })
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["price"]["currency"] == "USD" and cleared.json()["price"]["off_peak"] is None


async def test_invalid_scheduled_prices_preserve_current_configuration(client):
    await register(client)
    configuration = await client.post(f"{_API}/ai-configs", json={
        "name": "分时报价", "provider": "openai_compat", "model": "scheduled-embedding", "model_type": "embedding",
    })
    assert configuration.status_code == 201, configuration.text
    url = f"{_API}/ai-configs/{configuration.json()['id']}/price"
    initial = (await client.get(url)).json()
    base = {"currency": "CNY", "input_per_million": "2"}
    off_peak = {"start": "22:00", "end": "07:00", "input_per_million": "0"}
    invalid = [
        ({"start": "2:00"}, 422), ({"end": "24:00"}, 422), ({"end": "22:00"}, 400),
        ({"timezone": "Not/AZone"}, 400), ({"timezone": "localtime"}, 400),
        ({"per_image": "1"}, 400), ({"input_per_million": "1000001"}, 400),
        ({"currency": "USD"}, 422), ({"cached_input_per_million": "0"}, 400),
    ]
    for changes, status in invalid:
        rejected = await client.put(url, json={
            "expected_version": initial["version"], "price": {**base, "off_peak": {**off_peak, **changes}},
        })
        assert rejected.status_code == status, rejected.text
    assert (await client.get(url)).json() == initial
    forbidden = await client.put(url, json={
        "expected_version": initial["version"], "price": {**base, "cached_input_per_million": "0"},
    })
    assert forbidden.status_code == 400, forbidden.text

    saved = await client.put(url, json={
        "expected_version": initial["version"], "price": {**base, "off_peak": off_peak},
    })
    assert saved.status_code == 200, saved.text
    assert saved.json()["price"]["off_peak"]["input_per_million"] == "0"


async def test_additional_off_peak_windows_round_trip_and_preserve_legacy_version(client):
    await register(client)
    config = await client.post(f"{_API}/ai-configs", json={
        "name": "多个空闲时段", "provider": "openai_compat", "model": "scheduled-text",
    })
    assert config.status_code == 201, config.text
    url = f"{_API}/ai-configs/{config.json()['id']}/price"
    initial = (await client.get(url)).json()
    off_peak = {
        "start": "23:00", "end": "07:00", "input_per_million": "1",
        "output_per_million": "3", "cached_input_per_million": "0",
    }
    base = {"currency": "CNY", "input_per_million": "2", "output_per_million": "6", "off_peak": off_peak}
    single = await client.put(url, json={"expected_version": initial["version"], "price": base})
    assert single.status_code == 200, single.text
    single_version = single.json()["version"]
    extra = [{"start": "13:00", "end": "15:00"}, {"start": "15:00", "end": "16:00"}]
    multiple = await client.put(url, json={
        "expected_version": single_version, "price": {**base, "off_peak": {**off_peak, "additional_windows": extra}},
    })
    assert multiple.status_code == 200, multiple.text
    saved = multiple.json()
    assert saved["version"] != single_version
    assert saved["price"]["off_peak"]["additional_windows"] == extra
    assert saved["price"]["off_peak"]["cached_input_per_million"] == "0"
    assert (await client.get(url)).json() == saved

    stale = await client.put(url, json={"expected_version": single_version, "price": base})
    assert stale.status_code == 409 and stale.json()["code"] == "model_price_changed"
    legacy = await client.put(url, json={"expected_version": saved["version"], "price": base})
    assert legacy.status_code == 200, legacy.text
    assert legacy.json() == saved

    for windows, status in (
        ([{"start": "06:00", "end": "08:00"}], 400),
        ([{"start": "09:00", "end": "11:00"}, {"start": "10:00", "end": "12:00"}], 400),
        ([{"start": "13:00", "end": "13:00"}], 400),
        ([{"start": "13:00", "end": "24:00"}], 422),
        ([{"start": "13:00", "end": "14:00", "input_per_million": "2"}], 422),
        (None, 422),
    ):
        rejected = await client.put(url, json={
            "expected_version": saved["version"],
            "price": {**base, "off_peak": {**off_peak, "additional_windows": windows}},
        })
        assert rejected.status_code == status, rejected.text
    assert (await client.get(url)).json() == saved

    clear_request = ModelPriceUpdate.model_validate({
        "expected_version": saved["version"],
        "price": {**base, "off_peak": {**off_peak, "additional_windows": []}},
    }).model_dump(exclude_unset=True)
    assert clear_request["price"]["off_peak"]["additional_windows"] == []
    cleared = await client.put(url, json=clear_request)
    assert cleared.status_code == 200, cleared.text
    assert cleared.json() == single.json()


async def test_cached_model_rates_keep_versioned_updates_and_call_history(app, client):
    owner = await register(client)
    config = await client.post(f"{_API}/ai-configs", json={
        "name": "缓存命中价", "provider": "openai_compat", "model": "cached-text",
    })
    assert config.status_code == 201, config.text
    url = f"{_API}/ai-configs/{config.json()['id']}/price"
    snapshot = (await client.get(url)).json()
    base = {"currency": "CNY", "input_per_million": "3", "output_per_million": "5"}
    off_peak = {"start": "23:00", "end": "07:00", "input_per_million": "2", "output_per_million": "4"}

    fixed = await client.put(url, json={
        "expected_version": snapshot["version"], "price": {**base, "cached_input_per_million": "0"},
    })
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["price"]["cached_input_per_million"] == "0"
    assert (await client.get(url)).json() == fixed.json()
    version = fixed.json()["version"]

    scheduled = await client.put(url, json={
        "expected_version": version, "price": {
            **base, "off_peak": {**off_peak, "cached_input_per_million": "1"},
        },
    })
    assert scheduled.status_code == 200, scheduled.text
    assert scheduled.json()["price"]["cached_input_per_million"] == "0"
    assert scheduled.json()["price"]["off_peak"]["cached_input_per_million"] == "1"
    version = scheduled.json()["version"]

    async with app.state.sessionmaker() as db:
        db.add(_usage(
            owner["id"], owner["id"], model="cached-text", amount="0.00004",
            price_period="off_peak", cached_input_tokens=10, cached_input_rate=1,
        ))
        await db.commit()

    for legacy in (base, {**base, "off_peak": off_peak}):
        unchanged = await client.put(url, json={"expected_version": version, "price": legacy})
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json() == scheduled.json()

    repriced = await client.put(url, json={
        "expected_version": version, "price": {
            **base, "off_peak": {**off_peak, "cached_input_per_million": "2"},
        },
    })
    assert repriced.status_code == 200, repriced.text
    assert repriced.json()["price"]["off_peak"]["cached_input_per_million"] == "2"
    assert repriced.json()["version"] != version
    stale = await client.put(url, json={"expected_version": version, "price": base})
    assert stale.status_code == 409 and stale.json()["code"] == "model_price_changed"
    version = repriced.json()["version"]

    changed_base = await client.put(url, json={
        "expected_version": version, "price": {**base, "currency": "USD", "off_peak": None},
    })
    assert changed_base.status_code == 400
    changed_nested = await client.put(url, json={
        "expected_version": version, "price": {
            **base, "currency": "USD", "cached_input_per_million": "0", "off_peak": off_peak,
        },
    })
    assert changed_nested.status_code == 400
    assert (await client.get(url)).json() == repriced.json()

    cleared = await client.put(url, json={
        "expected_version": version, "price": {
            **base, "currency": "USD", "cached_input_per_million": None,
            "off_peak": {**off_peak, "cached_input_per_million": None},
        },
    })
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["price"]["cached_input_per_million"] is None
    assert cleared.json()["price"]["off_peak"]["cached_input_per_million"] is None
    assert cleared.json()["version"] != version
    history = (await client.get(f"{_API}/ai-usage")).json()["items"][0]
    assert history["currency"] == "CNY" and history["estimated_cost"] == "0.00004"
    assert history["price_period"] == "off_peak"
    assert history["cached_input_tokens"] == 10 and history["cached_input_per_million"] == "1"


async def test_video_price_api_accepts_only_per_second_and_preserves_schedule(client):
    await register(client)
    result = await client.post(f"{_API}/ai-configs", json={
        "name": "视频", "model_type": "video", "provider": "minimax", "model": "MiniMax-Hailuo-2.3",
    })
    assert result.status_code == 201, result.text
    url = f"{_API}/ai-configs/{result.json()['id']}/price"
    initial = (await client.get(url)).json()
    price = {"currency": "CNY", "per_video_second": "0.125",
             "off_peak": {"start": "22:00", "end": "06:00", "per_video_second": "0"}}
    for invalid in ({"per_image": "1"}, {"input_per_million": "1"}, {"per_video_second": None}):
        rejected = await client.put(url, json={"expected_version": initial["version"], "price": price | invalid})
        assert rejected.status_code == 400
    saved = await client.put(url, json={"expected_version": initial["version"], "price": price})
    assert saved.status_code == 200, saved.text
    assert saved.json()["price"]["per_video_second"] == "0.125"
    assert saved.json()["price"]["off_peak"]["per_video_second"] == "0"
    assert saved.json() == (await client.get(url)).json()
