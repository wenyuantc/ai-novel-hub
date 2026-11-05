"""章节分析的时间线回写：乱序、替代、人工保护与旧记录校准。"""

from sqlalchemy import select

from app.models import AnalysisChange, ChapterAnalysis, Character, SettingProgression, SettingTimeline
from tests.helpers import analysis_json, create_ai_config, create_project, register, run_task


async def _setup(app, client):
    await register(client)
    await create_ai_config(client)
    project = await create_project(client, title="时间线", genre="仙侠")
    base = f"/api/v1/projects/{project['id']}"
    response = await client.post(f"{base}/characters", json={"name": "林远", "current_state": "练气"})
    assert response.status_code == 201, response.text
    character = response.json()
    chapters = {}
    for number in (3, 5):
        response = await client.post(
            f"{base}/chapters",
            json={"title": f"第{number}章", "chapter_no": number, "content": f"林远在第{number}章修行。"},
        )
        assert response.status_code == 201, response.text
        chapters[number] = response.json()

    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project["id"],
                SettingTimeline.subject_kind == "character",
                SettingTimeline.character_id == character["id"],
            )
        )
        if timeline is None:
            record = await db.get(Character, character["id"])
            fields = (
                "profile", "appearance", "personality", "background", "current_state", "age", "gender", "life_status"
            )
            timeline = SettingTimeline(
                project_id=project["id"],
                subject_kind="character",
                character_id=character["id"],
                baseline={field: getattr(record, field) for field in fields},
                baseline_status="confirmed",
                legacy_compat_allowed=False,
            )
            db.add(timeline)
            await db.flush()
        timeline_id = timeline.id
        await db.commit()
    return base, project, character, chapters, timeline_id


async def _analyze(client, fake_llm, chapter, state):
    fake_llm.push(analysis_json(character_changes=[{"name": "林远", "current_state": state}], foreshadows=[]))
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapter['id']}/analyze"))
    assert task["status"] == "succeeded", task
    analyses = (await client.get(f"/api/v1/chapters/{chapter['id']}/analyses")).json()
    changes = (await client.get(f"/api/v1/analyses/{analyses[0]['id']}")).json()["changes"]
    return [change for change in changes if change["kind"] == "character_state"]


async def _events(app, timeline_id):
    async with app.state.sessionmaker() as db:
        events = list(
            await db.scalars(
                select(SettingProgression)
                .where(SettingProgression.timeline_id == timeline_id)
                .order_by(SettingProgression.id)
            )
        )
        return [
            {
                "id": event.id,
                "chapter_id": event.chapter_id,
                "boundary": event.boundary,
                "value": event.value,
                "active": event.active,
                "source_change_id": event.source_change_id,
                "protection": event.protection,
            }
            for event in events
        ]


async def test_early_analysis_writes_end_event_without_overwriting_later_state(app, client, fake_llm):
    base, _, character, chapters, timeline_id = await _setup(app, client)
    late = await _analyze(client, fake_llm, chapters[5], "金丹")
    early = await _analyze(client, fake_llm, chapters[3], "筑基")
    assert [change["status"] for change in late] == ["applied"]
    assert [change["status"] for change in early] == ["applied"]
    assert early[0]["before"] == "练气"
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["current_state"] == "金丹"

    events = await _events(app, timeline_id)
    assert {(event["chapter_id"], event["boundary"], event["value"]) for event in events if event["active"]} == {
        (chapters[3]["id"], "end", "筑基"),
        (chapters[5]["id"], "end", "金丹"),
    }
    assert {event["source_change_id"] for event in events} == {early[0]["id"], late[0]["id"]}

    reverted = await client.post(f"/api/v1/analysis-changes/{early[0]['id']}/revert")
    assert reverted.status_code == 200, reverted.text
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["current_state"] == "金丹"
    events = await _events(app, timeline_id)
    assert next(event for event in events if event["source_change_id"] == early[0]["id"])["active"] is False
    assert next(event for event in events if event["source_change_id"] == late[0]["id"])["active"] is True


async def test_early_analysis_matching_current_future_value_still_records_event(app, client, fake_llm):
    base, _, character, chapters, timeline_id = await _setup(app, client)
    late = await _analyze(client, fake_llm, chapters[5], "金丹")
    early = await _analyze(client, fake_llm, chapters[3], "金丹")
    assert [change["status"] for change in late] == ["applied"]
    assert [change["status"] for change in early] == ["applied"]
    assert early[0]["before"] == "练气"
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["current_state"] == "金丹"
    active = [event for event in await _events(app, timeline_id) if event["active"]]
    assert {event["chapter_id"] for event in active} == {chapters[3]["id"], chapters[5]["id"]}


async def test_reanalysis_to_baseline_supersedes_old_automatic_event(app, client, fake_llm):
    base, _, character, chapters, timeline_id = await _setup(app, client)
    first = await _analyze(client, fake_llm, chapters[3], "筑基")
    second = await _analyze(client, fake_llm, chapters[3], "练气")
    assert [change["status"] for change in first] == ["applied"]
    assert [change["status"] for change in second] == ["applied"]
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["current_state"] == "练气"

    events = await _events(app, timeline_id)
    old = next(event for event in events if event["source_change_id"] == first[0]["id"])
    new = next(event for event in events if event["source_change_id"] == second[0]["id"])
    assert old["active"] is False
    assert new["active"] is True and new["value"] == "练气"
    response = await client.post(f"/api/v1/analysis-changes/{first[0]['id']}/revert")
    assert response.status_code == 409
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["current_state"] == "练气"


async def test_reanalysis_same_value_transfers_source_without_old_revert(app, client, fake_llm):
    _, _, _, chapters, timeline_id = await _setup(app, client)
    first = await _analyze(client, fake_llm, chapters[3], "筑基")
    second = await _analyze(client, fake_llm, chapters[3], "筑基")
    assert [change["status"] for change in first] == ["applied"]
    assert [change["status"] for change in second] == ["applied"]
    events = await _events(app, timeline_id)
    assert next(event for event in events if event["source_change_id"] == first[0]["id"])["active"] is False
    assert next(event for event in events if event["source_change_id"] == second[0]["id"])["active"] is True
    assert (await client.post(f"/api/v1/analysis-changes/{first[0]['id']}/revert")).status_code == 409


async def test_reanalysis_preserves_manually_edited_event(app, client, fake_llm):
    base, _, character, chapters, timeline_id = await _setup(app, client)
    first = await _analyze(client, fake_llm, chapters[3], "筑基")
    event = (await _events(app, timeline_id))[0]
    url = f"{base}/setting-timelines/{timeline_id}"
    revision = (await client.get(f"{url}/baseline")).json()["timeline_revision"]
    edited = await client.patch(
        f"{url}/progressions/{event['id']}", json={"expected_revision": revision, "value": "作者亲定"}
    )
    assert edited.status_code == 200, edited.text
    second = await _analyze(client, fake_llm, chapters[3], "金丹")
    assert second[0]["status"] == "applied"
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["current_state"] == "作者亲定"
    events = await _events(app, timeline_id)
    assert next(item for item in events if item["id"] == event["id"])["protection"] == "manual"
    assert (await client.post(f"/api/v1/analysis-changes/{first[0]['id']}/revert")).status_code == 409


async def test_reanalysis_failure_keeps_previous_event_and_projection(app, client, fake_llm, monkeypatch):
    from app.core.errors import BadRequestError
    from app.services import setting_progression_service

    base, _, character, chapters, timeline_id = await _setup(app, client)
    first = await _analyze(client, fake_llm, chapters[3], "筑基")
    original = setting_progression_service.materialize_timeline

    async def fail_after_write(db, timeline):
        await original(db, timeline)
        if timeline.id == timeline_id:
            raise BadRequestError("投影写入失败")

    monkeypatch.setattr(setting_progression_service, "materialize_timeline", fail_after_write)
    failed = await _analyze(client, fake_llm, chapters[3], "金丹")
    assert (failed[0]["status"], failed[0]["reason"]) == ("failed", "投影写入失败")
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["current_state"] == "筑基"
    events = await _events(app, timeline_id)
    assert len(events) == 1 and events[0]["source_change_id"] == first[0]["id"] and events[0]["active"]


async def test_relationship_savepoint_failure_is_isolated(app, client, fake_llm, monkeypatch):
    from app.core.errors import BadRequestError
    from app.services import setting_progression_service
    from tests.test_writeback import _analyze as analyze_world
    from tests.test_writeback import _world

    base, _, relation, _, _, _, chapters = await _world(client)
    original = setting_progression_service.materialize_timeline
    attempts = 0

    async def fail_first_relation(db, timeline):
        nonlocal attempts
        if timeline.subject_kind == "relationship":
            attempts += 1
            if attempts == 1:
                raise BadRequestError("首个字段写入失败")
        await original(db, timeline)

    monkeypatch.setattr(setting_progression_service, "materialize_timeline", fail_first_relation)
    changes = await analyze_world(client, fake_llm, chapters[3], relationship_changes=[{
        "a": "林远", "b": "张三", "intimacy": 10, "status": "broken",
    }])
    by_field = {item["target"]["field"]: item for item in changes}
    assert (by_field["intimacy"]["status"], by_field["status"]["status"]) == ("failed", "applied")
    current = next(item for item in (await client.get(f"{base}/relationships")).json() if item["id"] == relation["id"])
    assert (current["intimacy"], current["status"]) == (60, "broken")


async def test_retry_same_source_does_not_duplicate_or_advance_revision(app, client, fake_llm):
    from app.services import analysis_progression_service

    _, _, _, chapters, timeline_id = await _setup(app, client)
    first = await _analyze(client, fake_llm, chapters[3], "筑基")
    async with app.state.sessionmaker() as db:
        change = await db.get(AnalysisChange, first[0]["id"])
        analysis = await db.get(ChapterAnalysis, change.analysis_id)
        timeline = await db.get(SettingTimeline, timeline_id)
        revision = timeline.revision
        prepared = await analysis_progression_service.prepare_field(
            db, analysis, timeline, source_field_key=change.field_key, field_key="current_state"
        )
        first_event = await analysis_progression_service.apply_field(db, analysis, change, prepared, "筑基")
        second_event = await analysis_progression_service.apply_field(db, analysis, change, prepared, "筑基")
        assert first_event.id == second_event.id
        assert timeline.revision == revision
        assert len(list(await db.scalars(
            select(SettingProgression).where(SettingProgression.source_change_id == change.id)
        ))) == 1


async def test_unreported_previous_event_is_kept_and_marked_for_review(app, client, fake_llm):
    base, _, character, chapters, timeline_id = await _setup(app, client)
    first = await _analyze(client, fake_llm, chapters[3], "筑基")
    fake_llm.push(analysis_json(character_changes=[], foreshadows=[]))
    task, _ = await run_task(client, await client.post(f"/api/v1/chapters/{chapters[3]['id']}/analyze"))
    assert task["status"] == "succeeded", task
    old = (await client.get(f"/api/v1/analyses/{first[0]['analysis_id']}")).json()["changes"][0]
    assert old["reason"] == "本次重新分析未报告此变化，请复核"
    assert (await _events(app, timeline_id))[0]["active"] is True
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["current_state"] == "筑基"


async def test_calibrated_legacy_baseline_cannot_be_overwritten_by_old_revert(app, client, fake_llm):
    base, project, character, chapters, timeline_id = await _setup(app, client)
    async with app.state.sessionmaker() as db:
        analysis = ChapterAnalysis(
            project_id=project["id"], chapter_id=chapters[3]["id"], chapter_no=3, content_hash="0" * 64
        )
        db.add(analysis)
        await db.flush()
        change = AnalysisChange(
            analysis_id=analysis.id,
            project_id=project["id"],
            chapter_no=3,
            kind="character_state",
            target={"character_id": character["id"], "name": "林远"},
            field_key=f"char:{character['id']}:current_state",
            before="被人工校准淘汰的旧值",
            after="练气",
            status="applied",
        )
        db.add(change)
        timeline = await db.get(SettingTimeline, timeline_id)
        timeline.baseline_status = "legacy_unverified"
        timeline.legacy_compat_allowed = True
        await db.flush()
        change_id, revision = change.id, timeline.revision
        await db.commit()

    # 基线经过真实的 A→C→A 修改，末值虽然相同，旧记录的资格不会恢复。
    url = f"{base}/setting-timelines/{timeline_id}/baseline"
    first = await client.patch(url, json={"expected_revision": revision, "changes": {"current_state": "人工校准"}})
    assert first.status_code == 200 and first.json()["legacy_compat_allowed"] is False
    second = await client.patch(url, json={
        "expected_revision": first.json()["timeline_revision"], "changes": {"current_state": "练气"},
    })
    assert second.status_code == 200 and second.json()["legacy_compat_allowed"] is False
    response = await client.post(f"/api/v1/analysis-changes/{change_id}/revert")
    assert response.status_code == 409
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["current_state"] == "练气"
    async with app.state.sessionmaker() as db:
        assert (await db.get(SettingTimeline, timeline_id)).baseline["current_state"] == "练气"


async def test_untouched_legacy_character_revert_uses_unverified_baseline(app, client, fake_llm):
    base, project, character, chapters, timeline_id = await _setup(app, client)
    async with app.state.sessionmaker() as db:
        timeline = await db.get(SettingTimeline, timeline_id)
        timeline.baseline_status = "legacy_unverified"
        timeline.legacy_compat_allowed = True
        analysis = ChapterAnalysis(
            project_id=project["id"], chapter_id=chapters[3]["id"], chapter_no=3, content_hash="0" * 64
        )
        db.add(analysis)
        await db.flush()
        change = AnalysisChange(
            analysis_id=analysis.id, project_id=project["id"], chapter_no=3, kind="character_state",
            target={"character_id": character["id"], "name": "林远"},
            field_key=f"char:{character['id']}:current_state", before="凡人", after="练气", status="applied",
        )
        db.add(change)
        await db.flush()
        change_id = change.id
        await db.commit()
    response = await client.post(f"/api/v1/analysis-changes/{change_id}/revert")
    assert response.status_code == 200, response.text
    assert (await client.get(f"{base}/characters/{character['id']}")).json()["current_state"] == "凡人"
    baseline = (await client.get(f"{base}/setting-timelines/{timeline_id}/baseline")).json()
    assert baseline["baseline_status"] == "legacy_unverified" and baseline["legacy_compat_allowed"] is True


async def test_legacy_relationship_creation_cannot_delete_subject_with_new_field_event(app, client):
    from tests.test_writeback import _world

    base, characters, relation, _, _, _, chapters = await _world(client)
    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(select(SettingTimeline).where(
            SettingTimeline.subject_kind == "relationship", SettingTimeline.project_id == relation["project_id"],
            SettingTimeline.character_id == min(relation["a_id"], relation["b_id"]),
            SettingTimeline.related_character_id == max(relation["a_id"], relation["b_id"]),
        ))
        timeline.baseline_status = "legacy_unverified"
        timeline.legacy_compat_allowed = True
        analysis = ChapterAnalysis(
            project_id=timeline.project_id, chapter_id=chapters[3]["id"], chapter_no=3, content_hash="0" * 64
        )
        db.add(analysis)
        await db.flush()
        change = AnalysisChange(
            analysis_id=analysis.id, project_id=timeline.project_id, chapter_no=3, kind="relationship_create",
            target={"relationship_id": relation["id"], "a_id": relation["a_id"], "b_id": relation["b_id"]},
            field_key=f"relpair:{timeline.character_id}:{timeline.related_character_id}", before=None,
            after={key: relation[key] for key in ("a_id", "b_id", "type_id", "intimacy", "status", "description")},
            status="applied",
        )
        db.add(change)
        await db.flush()
        change_id, timeline_id, revision = change.id, timeline.id, timeline.revision
        await db.commit()
    event = await client.post(f"{base}/setting-timelines/{timeline_id}/progressions", json={
        "expected_revision": revision, "field_key": "status", "operation": "replace", "value": relation["status"],
        "anchor_kind": "chapter", "chapter_id": chapters[5]["id"], "boundary": "end",
    })
    assert event.status_code == 201, event.text
    assert (await client.post(f"/api/v1/analysis-changes/{change_id}/revert")).status_code == 409
    current = (await client.get(f"{base}/relationships", params={"character_id": characters["林远"]["id"]})).json()
    assert any(item["id"] == relation["id"] for item in current)


async def test_untouched_legacy_pending_relation_can_be_confirmed_and_reverted(app, client):
    from app.services.relationship_progression_service import get_or_create_relationship_timeline
    from tests.test_writeback import _world

    base, characters, relation, _, _, _, chapters = await _world(client)
    a_id, b_id = characters["林远"]["id"], characters["王五"]["id"]
    async with app.state.sessionmaker() as db:
        timeline = await get_or_create_relationship_timeline(
            db, relation["project_id"], a_id, b_id, baseline_status="legacy_unverified"
        )
        analysis = ChapterAnalysis(
            project_id=relation["project_id"], chapter_id=chapters[3]["id"], chapter_no=3, content_hash="0" * 64
        )
        db.add(analysis)
        await db.flush()
        change = AnalysisChange(
            analysis_id=analysis.id, project_id=relation["project_id"], chapter_no=3, kind="relationship_create",
            target={"a_id": a_id, "b_id": b_id}, field_key=f"relpair:{min(a_id, b_id)}:{max(a_id, b_id)}",
            before=None, after={"role_a": "宿敌", "role_b": "宿敌", "category": "hostile",
                                "intimacy": 0, "status": "normal", "description": ""}, status="pending",
        )
        db.add(change)
        await db.flush()
        change_id, timeline_id = change.id, timeline.id
        await db.commit()
    confirmed = await client.post(f"/api/v1/analysis-changes/{change_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["target"]["relationship_id"]
    assert (await client.post(f"/api/v1/analysis-changes/{change_id}/revert")).status_code == 200
    baseline = (await client.get(f"{base}/setting-timelines/{timeline_id}/baseline")).json()
    assert baseline["baseline"] == {"exists": False}
    assert baseline["legacy_compat_allowed"] is True and baseline["baseline_status"] == "legacy_unverified"


async def test_legacy_membership_revert_preserves_note_unknown_to_old_audit(app, client):
    from tests.test_writeback import _world

    base, characters, relation, _, _, org, chapters = await _world(client)
    character_id = characters["李四"]["id"]
    created = await client.post(f"{base}/organizations/{org['id']}/members", json={
        "character_id": character_id, "position": "长老", "note": "旧审计未记录的作者手记",
    })
    assert created.status_code == 201, created.text
    async with app.state.sessionmaker() as db:
        timeline = await db.scalar(select(SettingTimeline).where(
            SettingTimeline.project_id == relation["project_id"], SettingTimeline.subject_kind == "membership",
            SettingTimeline.character_id == character_id, SettingTimeline.organization_id == org["id"],
        ))
        timeline.baseline_status = "legacy_unverified"
        timeline.legacy_compat_allowed = True
        analysis = ChapterAnalysis(
            project_id=relation["project_id"], chapter_id=chapters[3]["id"], chapter_no=3, content_hash="0" * 64
        )
        db.add(analysis)
        await db.flush()
        change = AnalysisChange(
            analysis_id=analysis.id, project_id=relation["project_id"], chapter_no=3, kind="org_membership",
            target={"organization_id": org["id"], "character_id": character_id},
            field_key=f"org:{org['id']}:{character_id}",
            before={"position": "弟子", "status": "active"}, after={"position": "长老", "status": "active"},
            status="applied",
        )
        db.add(change)
        await db.flush()
        change_id = change.id
        await db.commit()
    response = await client.post(f"/api/v1/analysis-changes/{change_id}/revert")
    assert response.status_code == 200, response.text
    member = (await client.get(f"{base}/characters/{character_id}/memberships")).json()[0]
    assert (member["position"], member["note"]) == ("弟子", "旧审计未记录的作者手记")


async def test_structured_analysis_records_earlier_events_without_clobbering_later(app, client, fake_llm):
    from tests.test_writeback import _analyze as analyze_world
    from tests.test_writeback import _world

    base, characters, relation, career, stages, org, chapters = await _world(client)

    async def analyze(number, intimacy, stage, position):
        return await analyze_world(
            client,
            fake_llm,
            chapters[number],
            relationship_changes=[{"a": "林远", "b": "张三", "intimacy": intimacy}],
            career_changes=[{"name": "林远", "career": "剑修", "stage": stage}],
            organization_changes=[{"name": "李四", "organization": "青云门", "action": "join", "position": position}],
        )

    later = await analyze(5, 90, "金丹", "内门")
    earlier = await analyze(3, 20, "练气", "外门")
    assert {item["kind"] for item in earlier} == {"relationship_update", "career_stage", "org_membership"}
    assert {item["status"] for item in earlier} == {"applied"}
    assert next(item for item in earlier if item["kind"] == "relationship_update")["before"] == 60
    assert next(item for item in earlier if item["kind"] == "career_stage")["before"]["stage_id"] == stages["筑基"]
    assert next(item for item in earlier if item["kind"] == "org_membership")["before"] is None
    current_relation = next(r for r in (await client.get(f"{base}/relationships")).json() if r["id"] == relation["id"])
    assert current_relation["intimacy"] == 90
    assignment = (await client.get(f"{base}/characters/{characters['林远']['id']}/careers")).json()[0]
    assert assignment["stage_id"] == stages["金丹"]
    membership = (await client.get(f"{base}/characters/{characters['李四']['id']}/memberships")).json()[0]
    assert membership["organization_id"] == org["id"] and membership["position"] == "内门"
    async with app.state.sessionmaker() as db:
        events = list(await db.scalars(select(SettingProgression).where(SettingProgression.source_change_id.in_(
            [item["id"] for item in earlier + later]
        ))))
        assert len(events) == 6
        assert {event.boundary for event in events} == {"end"}
        assert {event.chapter_id for event in events} == {chapters[3]["id"], chapters[5]["id"]}
    for item in earlier:
        response = await client.post(f"/api/v1/analysis-changes/{item['id']}/revert")
        assert response.status_code == 200, (item["kind"], response.text)
    current_relation = next(r for r in (await client.get(f"{base}/relationships")).json() if r["id"] == relation["id"])
    assert current_relation["intimacy"] == 90
    assignment = (await client.get(f"{base}/characters/{characters['林远']['id']}/careers")).json()[0]
    assert assignment["stage_id"] == stages["金丹"]
    membership = (await client.get(f"{base}/characters/{characters['李四']['id']}/memberships")).json()[0]
    assert membership["position"] == "内门"


async def test_analysis_uses_historical_relation_and_career_even_after_later_removal(app, client, fake_llm):
    from tests.test_writeback import _analyze as analyze_world
    from tests.test_writeback import _world

    base, characters, relation, career, _, _, chapters = await _world(client)
    async with app.state.sessionmaker() as db:
        timelines = list(await db.scalars(select(SettingTimeline).where(
            SettingTimeline.project_id == relation["project_id"],
            SettingTimeline.subject_kind.in_(("relationship", "career")),
            SettingTimeline.character_id == characters["林远"]["id"],
        )))
        timelines = [(timeline.id, timeline.subject_kind, timeline.revision) for timeline in timelines]
    assert {kind for _, kind, _ in timelines} == {"relationship", "career"}
    for timeline_id, _, revision in timelines:
        removed = await client.post(f"{base}/setting-timelines/{timeline_id}/progressions", json={
            "expected_revision": revision, "field_key": "presence", "operation": "replace", "value": {"exists": False},
            "anchor_kind": "chapter", "chapter_id": chapters[5]["id"], "boundary": "end",
        })
        assert removed.status_code == 201, removed.text
    changes = await analyze_world(
        client, fake_llm, chapters[3],
        relationship_changes=[{"a": "林远", "b": "张三", "intimacy": 20}],
        career_changes=[{"name": "林远", "career": career["name"], "stage": "金丹"}],
    )
    assert {item["kind"] for item in changes} == {"relationship_update", "career_stage"}
    assert {item["status"] for item in changes} == {"applied"}
    assert (await client.get(f"{base}/relationships")).json() == []
    assert (await client.get(f"{base}/characters/{characters['林远']['id']}/careers")).json() == []
    for change in changes:
        assert (await client.post(f"/api/v1/analysis-changes/{change['id']}/revert")).status_code == 200


async def test_membership_field_events_preserve_independent_later_fields_and_note(app, client, fake_llm):
    from tests.test_writeback import _analyze as analyze_world
    from tests.test_writeback import _world

    base, characters, _, _, _, org, chapters = await _world(client)
    created = await client.post(f"{base}/organizations/{org['id']}/members", json={
        "character_id": characters["李四"]["id"], "position": "弟子", "note": "作者手记",
    })
    assert created.status_code == 201, created.text
    later = await analyze_world(client, fake_llm, chapters[5], organization_changes=[{
        "name": "李四", "organization": "青云门", "action": "update", "status": "left",
    }])
    earlier = await analyze_world(client, fake_llm, chapters[3], organization_changes=[{
        "name": "李四", "organization": "青云门", "action": "update", "position": "长老", "status": "expelled",
    }])
    assert later[0]["status"] == earlier[0]["status"] == "applied"
    assert earlier[0]["before"] == {"position": "弟子", "status": "active"}
    url = f"{base}/characters/{characters['李四']['id']}/memberships"
    member = (await client.get(url)).json()[0]
    assert (member["position"], member["status"], member["note"]) == ("长老", "left", "作者手记")
    async with app.state.sessionmaker() as db:
        fields = list(await db.scalars(select(SettingProgression.field_key).where(
            SettingProgression.source_change_id == earlier[0]["id"]
        )))
        assert set(fields) == {"position", "status"}
    assert (await client.post(f"/api/v1/analysis-changes/{earlier[0]['id']}/revert")).status_code == 200
    member = (await client.get(url)).json()[0]
    assert (member["position"], member["status"], member["note"]) == ("弟子", "left", "作者手记")


async def test_membership_reanalysis_atomically_supersedes_both_fields_at_baseline(app, client, fake_llm, monkeypatch):
    from app.core.errors import BadRequestError
    from app.services import setting_progression_service
    from tests.test_writeback import _analyze as analyze_world
    from tests.test_writeback import _world

    base, characters, _, _, _, org, chapters = await _world(client)
    response = await client.post(f"{base}/organizations/{org['id']}/members", json={
        "character_id": characters["李四"]["id"], "position": "弟子", "note": "不可丢失",
    })
    assert response.status_code == 201, response.text

    async def analyze(position, status):
        return (await analyze_world(client, fake_llm, chapters[3], organization_changes=[{
            "name": "李四", "organization": "青云门", "action": "update", "position": position, "status": status,
        }]))[0]

    first = await analyze("长老", "left")
    original = setting_progression_service.apply_progression

    async def fail_second_field(db, timeline, data, **kwargs):
        if timeline.subject_kind == "membership" and data.field_key == "status":
            raise BadRequestError("第二个字段写入失败")
        return await original(db, timeline, data, **kwargs)

    monkeypatch.setattr(setting_progression_service, "apply_progression", fail_second_field)
    failed = await analyze("弟子", "active")
    assert (failed["status"], failed["reason"]) == ("failed", "第二个字段写入失败")
    async with app.state.sessionmaker() as db:
        old = list(await db.scalars(
            select(SettingProgression).where(SettingProgression.source_change_id == first["id"])
        ))
        assert len(old) == 2 and all(event.active for event in old)
    member = (await client.get(f"{base}/characters/{characters['李四']['id']}/memberships")).json()[0]
    assert (member["position"], member["status"], member["note"]) == ("长老", "left", "不可丢失")
    monkeypatch.setattr(setting_progression_service, "apply_progression", original)
    second = await analyze("弟子", "active")
    assert first["status"] == second["status"] == "applied"
    async with app.state.sessionmaker() as db:
        events = list(await db.scalars(select(SettingProgression).where(
            SettingProgression.source_change_id.in_((first["id"], second["id"]))
        )))
        assert len(events) == 4
        assert all(not event.active for event in events if event.source_change_id == first["id"])
        assert all(event.active for event in events if event.source_change_id == second["id"])
    assert (await client.post(f"/api/v1/analysis-changes/{first['id']}/revert")).status_code == 409
    member = (await client.get(f"{base}/characters/{characters['李四']['id']}/memberships")).json()[0]
    assert (member["position"], member["status"], member["note"]) == ("弟子", "active", "不可丢失")


async def test_pending_relationship_confirmation_rechecks_chapter_content(app, client, fake_llm):
    from tests.test_writeback import _analyze as analyze_world
    from tests.test_writeback import _world

    base, characters, _, _, _, _, chapters = await _world(client)
    changes = await analyze_world(client, fake_llm, chapters[3], relationship_changes=[{
        "a": "林远", "b": "王五", "new_relation": {"role_a": "宿敌", "role_b": "宿敌", "category": "hostile"},
    }])
    pending = changes[0]
    assert pending["kind"] == "relationship_create" and pending["status"] == "pending"
    edited = await client.patch(f"{base}/chapters/{chapters[3]['id']}", json={"content": "改写后的正文"})
    assert edited.status_code == 200, edited.text
    confirmed = await client.post(f"/api/v1/analysis-changes/{pending['id']}/confirm")
    assert confirmed.status_code == 409
    current = (await client.get(f"{base}/relationships", params={"character_id": characters["王五"]["id"]})).json()
    assert current == []
