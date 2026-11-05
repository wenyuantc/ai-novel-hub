"""项目 JSON 备份与导入：注册表反射、往返一致、可选项、风格、全局关系类型、校验与回滚、跨用户、导入后检索。"""

import json

from sqlalchemy import JSON, func, select

from app.models import (
    AnalysisChange,
    Base,
    Career,
    CareerHistory,
    CareerStage,
    Chapter,
    ChapterAnalysis,
    ChapterCharacter,
    ChapterPlan,
    ChapterVersion,
    Character,
    CharacterCareer,
    Foreshadow,
    ForeshadowCharacter,
    ForeshadowHistory,
    GenerationLog,
    InspirationSession,
    KbChunk,
    KbDocument,
    KbSettings,
    Organization,
    OrganizationMember,
    Outline,
    OutlineSnapshot,
    Project,
    Relationship,
    RelationshipHistory,
    RelationshipType,
    SettingProgression,
    SettingTextField,
    SettingTimeline,
    StoryMemory,
    WorldSetting,
    WritingStyle,
)
from app.services.project_transfer import importer
from app.services.project_transfer.registry import NOT_EXPORTED, PROJECT_RULE, RULES
from tests.helpers import create_ai_config, create_project, register, wait_task


def _project_tables() -> set[str]:
    """直接或经外键间接引用 projects 的表（只沿「引用方 → 被引用方」方向扩散）。"""
    found = {"projects"}
    changed = True
    while changed:
        changed = False
        for table in Base.metadata.tables.values():
            if table.name in found:
                continue
            if any(fk.column.table.name in found for fk in table.foreign_keys):
                found.add(table.name)
                changed = True
    return found


def test_registry_covers_all_project_tables():
    registered = {r.table for r in RULES} | {"projects"}
    assert len(registered) == len(RULES) + 1, "同一张表登记了两次"
    related = _project_tables()
    assert related - registered - set(NOT_EXPORTED) == set(), "有与项目相关的表既没登记也没写进 NOT_EXPORTED"
    assert set(NOT_EXPORTED) <= related
    assert not (set(NOT_EXPORTED) & registered)
    assert all(reason.strip() for reason in NOT_EXPORTED.values())


def test_registry_handles_every_fk_and_json_column():
    for rule in (PROJECT_RULE, *RULES):
        table = rule.model.__table__
        handled = set(rule.fk) | set(rule.fill) | set(rule.custom) | set(rule.exclude)
        for col in table.columns:
            if col.foreign_keys:
                assert col.name in handled, f"{rule.table}.{col.name} 是外键，但没有声明处理方式"
            if isinstance(col.type, JSON) and col.name not in set(rule.exclude) | set(rule.custom):
                assert (col.name in rule.json_refs) != (col.name in rule.json_plain), (
                    f"{rule.table}.{col.name} 是 JSON 列，必须且只能出现在 json_refs 或 json_plain 中"
                )
        # 声明的列都真实存在，fk 指向的表都已登记（或是 projects）
        names = {c.name for c in table.columns}
        declared = handled | set(rule.json_refs) | set(rule.json_plain) | set(rule.str_refs) | set(rule.deferred)
        assert declared <= names, f"{rule.table} 声明了不存在的列：{declared - names}"
        assert rule.deferred <= set(rule.fk)
        for col, target in rule.fk.items():
            assert target == "projects" or target in {r.table for r in RULES}, f"{rule.table}.{col} → {target}"
            fk_targets = {fk.column.table.name for fk in table.columns[col].foreign_keys}
            assert fk_targets == {target}, f"{rule.table}.{col} 的被引用表应为 {fk_targets}"


def test_registry_import_order():
    """非 deferred 的外键只能引用排在前面的表。"""
    position = {r.table: i for i, r in enumerate(RULES)}
    for rule in RULES:
        for col, target in rule.fk.items():
            if col in rule.deferred or target == "projects":
                continue
            assert position[target] < position[rule.table], f"{rule.table}.{col} 引用的 {target} 排在后面"


# ================================================================ 往返与导入

API = "/api/v1/projects"
KB_TEXT = "# 门派\n\n## 天剑宗\n\n天剑宗坐落在云梦山巅，以剑修闻名天下。\n\n## 东海龙宫\n\n龙宫位于东海之底。"


async def _seed(app, project_id: int) -> dict:
    """往项目里写入每一种实体（含同名角色、同名章节），返回各对象 id。"""
    async with app.state.sessionmaker() as db:
        lean = await db.scalar(select(WritingStyle.id).where(WritingStyle.preset_key == "lean_cold"))
        w1 = WorldSetting(project_id=project_id, title="云梦山", content="终年云雾", category="place", sort=1)
        w2 = WorldSetting(project_id=project_id, title="灵气", content="万物之源", category="rule", sort=2)
        c1 = Character(project_id=project_id, name="林远", role_type="主角", aliases=["小远"], always_in_context=True)
        c2 = Character(project_id=project_id, name="林远", role_type="配角", profile="同名的另一个人")
        c3 = Character(project_id=project_id, name="苏晴", life_status="dead", current_state="已故")
        db.add_all([w1, w2, c1, c2, c3])
        await db.flush()
        org = Organization(project_id=project_id, name="天剑宗", power_level=90)
        db.add(org)
        await db.flush()
        sub = Organization(project_id=project_id, name="外门", parent_id=org.id)
        db.add(sub)
        await db.flush()
        db.add(OrganizationMember(organization_id=sub.id, character_id=c1.id, position="弟子"))
        custom_type = RelationshipType(project_id=project_id, category="social", role_a="道侣", role_b="道侣")
        db.add(custom_type)
        await db.flush()
        master = await db.scalar(
            select(RelationshipType).where(RelationshipType.project_id.is_(None), RelationshipType.role_a == "师父")
        )
        r1 = Relationship(
            project_id=project_id,
            a_id=c1.id,
            b_id=c3.id,
            pair_low=min(c1.id, c3.id),
            pair_high=max(c1.id, c3.id),
            type_id=custom_type.id,
            intimacy=80,
        )
        r2 = Relationship(
            project_id=project_id,
            a_id=c3.id,
            b_id=c2.id,
            pair_low=min(c2.id, c3.id),
            pair_high=max(c2.id, c3.id),
            type_id=master.id,
            status="past",
        )
        db.add_all([r1, r2])
        await db.flush()
        db.add(
            RelationshipHistory(
                relationship_id=r1.id, chapter_no=1, source="manual", changes={"intimacy": [50, 80]}, note="n"
            )
        )
        career = Career(project_id=project_id, name="剑修", kind="main")
        db.add(career)
        await db.flush()
        s1 = CareerStage(career_id=career.id, position=1, name="练气")
        s2 = CareerStage(career_id=career.id, position=2, name="筑基")
        db.add_all([s1, s2])
        await db.flush()
        db.add(CharacterCareer(character_id=c1.id, career_id=career.id, role="main", stage_id=s2.id, note="瓶颈"))
        db.add(
            CareerHistory(
                character_id=c1.id,
                career_id=career.id,
                career_name="剑修",
                source="analysis",
                chapter_no=2,
                changes={"stage": ["第1阶·练气", "第2阶·筑基"]},
            )
        )
        o1 = Outline(project_id=project_id, volume_no=1, volume_title="入门", chapter_no=1, title="拜师", sort=1)
        o2 = Outline(project_id=project_id, volume_no=2, volume_title="下山", chapter_no=3, title="历练", sort=1)
        db.add_all([o1, o2])
        await db.flush()
        ch1 = Chapter(
            project_id=project_id,
            outline_id=o1.id,
            chapter_no=1,
            title="重逢",
            content="第一章正文，天剑宗。",
            word_count=9,
            style_id=lean,
        )
        ch2 = Chapter(
            project_id=project_id, outline_id=o1.id, chapter_no=2, title="重逢", content="第二章", word_count=3
        )
        ch3 = Chapter(project_id=project_id, outline_id=o2.id, chapter_no=3, title="下山", content="", status="draft")
        db.add_all([ch1, ch2, ch3])
        await db.flush()
        plan = ChapterPlan(
            project_id=project_id,
            outline_id=o1.id,
            seq=1,
            title="重逢",
            key_events=["相遇"],
            character_ids=[c1.id, c3.id],
            chapter_id=ch2.id,
        )
        db.add(plan)
        await db.flush()
        ch2.plan_id = plan.id
        db.add_all(
            [
                ChapterCharacter(chapter_id=ch1.id, character_id=c1.id),
                ChapterCharacter(chapter_id=ch1.id, character_id=c2.id),
            ]
        )
        fs = Foreshadow(project_id=project_id, title="神秘玉佩", status="planted", actual_plant_chapter=1)
        db.add(fs)
        await db.flush()
        db.add(ForeshadowCharacter(foreshadow_id=fs.id, character_id=c3.id))
        db.add(
            ForeshadowHistory(
                foreshadow_id=fs.id, source="manual", chapter_no=1, changes={"characters": [None, ["苏晴"]]}
            )
        )
        log = GenerationLog(
            project_id=project_id, chapter_id=ch1.id, kind="generate", status="success", params={"a": 1}
        )
        db.add(log)
        await db.flush()
        db.add(ChapterVersion(chapter_id=ch1.id, seq=1, source="generate", content="v1", generation_log_id=log.id))
        db.add(ChapterVersion(chapter_id=ch1.id, seq=2, source="manual", content="v2", starred=True))
        an = ChapterAnalysis(
            project_id=project_id,
            chapter_id=ch1.id,
            content_hash="h",
            chapter_no=1,
            summary="林远拜入天剑宗",
            foreshadow_candidates=[{"action": "resolve", "ref_id": fs.id, "text": "玉佩"}, {"action": "plant"}],
            changes_raw={"character_changes": [{"name": "林远"}]},
            scores={"overall": 7},
        )
        db.add(an)
        await db.flush()
        stage = {"stage_id": s2.id, "stage": "第2阶·筑基"}
        changes = [
            ("character_state", {"character_id": c1.id, "name": "林远"}, f"char:{c1.id}:current_state", "旧", "新"),
            ("life_status", {"character_id": c3.id, "name": "苏晴"}, f"char:{c3.id}:life_status", "alive", "dead"),
            (
                "relationship_update",
                {"relationship_id": r1.id, "a_id": c1.id, "b_id": c3.id, "a_name": "林远", "b_name": "苏晴"},
                f"rel:{r1.id}:intimacy",
                50,
                80,
            ),
            (
                "relationship_create",
                {"a_id": c3.id, "b_id": c2.id, "a_name": "苏晴", "b_name": "林远", "relationship_id": r2.id},
                f"relpair:{min(c2.id, c3.id)}:{max(c2.id, c3.id)}",
                None,
                {"role_a": "师父", "role_b": "徒弟", "a_id": c3.id, "b_id": c2.id, "type_id": master.id},
            ),
            (
                "career_stage",
                {"character_id": c1.id, "name": "林远", "career_id": career.id, "career_name": "剑修"},
                f"career:{c1.id}:{career.id}",
                {"stage_id": s1.id, "stage": "第1阶·练气"},
                stage,
            ),
            (
                "org_membership",
                {"organization_id": sub.id, "organization_name": "外门", "character_id": c1.id, "name": "林远"},
                f"org:{sub.id}:{c1.id}",
                None,
                {"position": "弟子", "status": "active"},
            ),
            (
                "foreshadow_resolve",
                {"foreshadow_id": fs.id, "title": "神秘玉佩"},
                f"fs:{fs.id}:resolve",
                {"status": "planted"},
                {"status": "resolved", "actual_resolve_chapter": 1},
            ),
            (
                "foreshadow_plant_planned",
                {"text": "x", "keyword": "", "foreshadow_id": 99999},
                "",
                None,
                {"status": "planted", "actual_plant_chapter": 1},
            ),
            (
                "foreshadow_new",
                {"title": "神秘玉佩", "foreshadow_id": fs.id},
                "",
                None,
                {"title": "神秘玉佩", "character_ids": [c3.id, c1.id]},
            ),
        ]
        for kind, target, key, before, after in changes:
            db.add(
                AnalysisChange(
                    analysis_id=an.id,
                    project_id=project_id,
                    chapter_no=1,
                    kind=kind,
                    target=target,
                    field_key=key,
                    before=before,
                    after=after,
                    status="applied",
                )
            )
        m1 = StoryMemory(
            project_id=project_id,
            chapter_id=ch1.id,
            chapter_no=1,
            kind="summary",
            content="林远拜入天剑宗修行",
            characters=["林远"],
            origin="auto",
            analysis_id=an.id,
            pinned=True,
            content_key="abc",
        )
        m2 = StoryMemory(
            project_id=project_id, chapter_no=2, kind="manual", content="苏晴留下神秘玉佩", origin="manual", muted=True
        )
        db.add_all([m1, m2])
        db.add(KbSettings(project_id=project_id, top_k=9, auto_inject=False))
        doc = KbDocument(
            project_id=project_id,
            filename="设定集.md",
            mime="text/markdown",
            size=len(KB_TEXT.encode()),
            sha256="0" * 64,
            status="ready",
            text=KB_TEXT,
            char_count=len(KB_TEXT),
            chunk_count=3,
            warnings=["w"],
            inject=True,
        )
        db.add(doc)
        snap = OutlineSnapshot(
            project_id=project_id,
            reason="manual",
            data={
                "outlines": [
                    {"id": o1.id, "volume_no": 1, "title": "拜师", "chapter_no": 1},
                    {"id": 88888, "volume_no": 1, "title": "已删大纲", "chapter_no": 2},
                ],
                "plans": [
                    {"id": plan.id, "outline_id": o1.id, "seq": 1, "title": "重逢", "character_ids": [c1.id, 77777]},
                    {"id": 66666, "outline_id": 88888, "seq": 1, "title": "已删细纲", "character_ids": []},
                ],
                "links": [
                    {"chapter_id": ch2.id, "outline_id": o1.id, "plan_id": plan.id, "chapter_no": 2},
                    {"chapter_id": 55555, "outline_id": 88888, "plan_id": None, "chapter_no": 9},
                ],
            },
        )
        db.add(snap)
        session = InspirationSession(user_id=(await db.get(Project, project_id)).user_id, project_id=project_id)
        session.title = "灵感"
        session.state = {"idea": "修仙"}
        db.add(session)
        # 阅读进度（JSON 内含章节 id）
        project = await db.get(Project, project_id)
        project.reading_progress = {
            "chapter_id": ch2.id,
            "chapter_no": 2,
            "ratio": 0.5,
            "updated_at": "2026-10-27T00:00:00Z",
        }
        await db.commit()
        return {
            "characters": [c1.id, c2.id, c3.id],
            "chapters": [ch1.id, ch2.id, ch3.id],
            "custom_type": custom_type.id,
            "master": master.id,
            "lean": lean,
        }


async def _setup(client, app) -> tuple[dict, dict]:
    await register(client)
    # 旧的 style 字段会建一个自定义风格「《书名》原风格」并设为默认
    p = await create_project(client, tags=["仙侠", "成长"], synopsis="少年求仙")
    ids = await _seed(app, p["id"])
    # _seed 直接插入 ORM 以覆盖完整备份；模拟 0031 对旧作品建立的 M1 基础快照。
    await _seed_legacy_timelines(app, p["id"])
    return p, ids


async def _seed_legacy_timelines(app, project_id: int) -> None:
    """测试夹具绕过业务创建入口，显式建立与迁移后数据库等价的时间线。"""
    async with app.state.sessionmaker() as db:
        common = {"project_id": project_id, "baseline_status": "legacy_unverified", "legacy_compat_allowed": True}
        worlds = list(await db.scalars(select(WorldSetting).where(WorldSetting.project_id == project_id)))
        characters = list(await db.scalars(select(Character).where(Character.project_id == project_id)))
        relationships = list(await db.scalars(select(Relationship).where(Relationship.project_id == project_id)))
        assignments = list(
            await db.scalars(select(CharacterCareer).join(Character).where(Character.project_id == project_id))
        )
        members = list(
            await db.scalars(select(OrganizationMember).join(Character).where(Character.project_id == project_id))
        )
        char_fields = (
            "profile", "appearance", "personality", "background", "current_state", "life_status", "age", "gender"
        )
        db.add_all(
            [
                *(
                    SettingTimeline(
                        **common, subject_kind="world", world_setting_id=w.id, baseline={"content": w.content}
                    )
                    for w in worlds
                ),
                *(SettingTimeline(**common, subject_kind="character", character_id=c.id,
                                  baseline={f: getattr(c, f) for f in char_fields}) for c in characters),
                *(SettingTimeline(
                    **common, subject_kind="relationship", character_id=r.pair_low, related_character_id=r.pair_high,
                    baseline={"exists": True, "identity": {"a_id": r.a_id, "b_id": r.b_id, "type_id": r.type_id},
                              "intimacy": r.intimacy, "status": r.status, "description": r.description},
                ) for r in relationships),
                *(SettingTimeline(
                    **common, subject_kind="career", character_id=a.character_id, career_id=a.career_id,
                    baseline={"exists": True, "role": a.role, "stage_id": a.stage_id, "note": a.note},
                ) for a in assignments),
                *(SettingTimeline(
                    **common, subject_kind="membership", character_id=m.character_id, organization_id=m.organization_id,
                    baseline={"exists": True, "position": m.position, "status": m.status, "note": m.note},
                ) for m in members),
            ]
        )
        await db.commit()


async def _backup(client, pid: int, **params) -> dict:
    resp = await client.get(f"{API}/{pid}/backup", params=params)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    assert "filename*=UTF-8''" in resp.headers["content-disposition"]
    return json.loads(resp.content)


async def _import(client, data: dict, title: str | None = None) -> dict:
    raw = json.dumps(data, ensure_ascii=False).encode()
    resp = await client.post(f"{API}/import/preview", files={"file": ("b.ainovel.json", raw, "application/json")})
    assert resp.status_code == 200, resp.text
    preview = resp.json()
    body = {"token": preview["token"]}
    if title is not None:
        body["title"] = title
    resp = await client.post(f"{API}/import", json=body)
    assert resp.status_code == 201, resp.text
    return {"preview": preview, **resp.json()}


ALL = {"versions": "1", "snapshots": "1", "logs": "1", "inspiration": "1"}


def _canonical(data: dict) -> dict:
    """把每张表的 id / 外键换成行序号（导入保持文件顺序，新旧两边的同一行序号相同），JSON 内的 id 另行断言。"""
    tables = data["tables"]
    index = {t: {row["id"]: i for i, row in enumerate(rows)} for t, rows in tables.items() if rows and "id" in rows[0]}
    out = {}
    for rule in RULES:
        rows = tables.get(rule.table)
        if rows is None:
            continue
        canon = []
        for row in rows:
            r = dict(row)
            if "id" in r:
                r["id"] = index[rule.table][r["id"]]
            for col, target in rule.fk.items():
                if r.get(col) is not None and target != "projects":
                    r[col] = index[target][r[col]]
                elif target == "projects":
                    r[col] = r[col] is not None
            for col in (*rule.json_refs, *rule.str_refs):
                r.pop(col, None)
            canon.append(r)
        out[rule.table] = canon
    return out


async def _seed_progression_changes(app, project_id: int, ids: dict) -> dict:
    """制造文本、关系、职业、来源及已撤销事件，供备份语义测试。"""
    async with app.state.sessionmaker() as db:
        c1, _, c3 = ids["characters"]
        rel = await db.scalar(
            select(Relationship).where(Relationship.project_id == project_id, Relationship.a_id == c1)
        )
        relation = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == "relationship",
                SettingTimeline.character_id == min(c1, c3),
                SettingTimeline.related_character_id == max(c1, c3),
            )
        )
        career = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == project_id,
                SettingTimeline.subject_kind == "career",
                SettingTimeline.character_id == c1,
            )
        )
        world = await db.scalar(
            select(WorldSetting).where(WorldSetting.project_id == project_id, WorldSetting.title == "云梦山")
        )
        world_timeline = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.subject_kind == "world", SettingTimeline.world_setting_id == world.id
            )
        )
        friend = await db.scalar(select(RelationshipType).where(RelationshipType.role_a == "朋友"))
        enemy = await db.scalar(select(RelationshipType).where(RelationshipType.role_a == "仇人"))
        confidant = await db.scalar(select(RelationshipType).where(RelationshipType.role_a == "知己"))
        kin = await db.scalar(select(RelationshipType).where(RelationshipType.role_a == "亲属"))
        stages = list(await db.scalars(
            select(CareerStage).where(CareerStage.career_id == career.career_id).order_by(CareerStage.position)
        ))
        source = await db.scalar(
            select(AnalysisChange).where(AnalysisChange.project_id == project_id, AnalysisChange.kind == "career_stage")
        )
        assert rel and relation and career and world and world_timeline
        assert friend and enemy and confidant and kin and source
        relation.baseline = {**relation.baseline, "identity": {"a_id": c1, "b_id": c3, "type_id": friend.id}}
        rel.type_id = enemy.id
        career.baseline = {**career.baseline, "stage_id": stages[0].id}
        text_field = SettingTextField(timeline_id=world_timeline.id, label="持有者", base_value="林远")
        db.add(text_field)
        await db.flush()
        active = SettingProgression(
            timeline_id=relation.id, field_key="identity", operation="replace",
            value={"a_id": c1, "b_id": c3, "type_id": enemy.id}, anchor_kind="chapter",
            chapter_id=ids["chapters"][2], boundary="start", source="manual", protection="manual",
        )
        stage = SettingProgression(
            timeline_id=career.id, field_key="stage_id", operation="replace", value=stages[1].id,
            anchor_kind="chapter", chapter_id=ids["chapters"][1], boundary="end",
            source="analysis", protection="automatic", source_change_id=source.id,
        )
        custom = SettingProgression(
            timeline_id=world_timeline.id, custom_field_id=text_field.id, operation="append", value="苏晴",
            anchor_kind="planned", chapter_no=20, boundary="start", source="manual", protection="manual",
        )
        db.add_all([active, stage, custom])
        await db.flush()
        orphan = SettingProgression(
            timeline_id=relation.id, field_key="identity", operation="replace",
            value={"a_id": c1, "b_id": c3, "type_id": confidant.id}, anchor_kind="chapter",
            chapter_id=None, boundary="end", source="manual", protection="manual", active=False,
            superseded_by_id=active.id, anchor_label="已删除章节",
        )
        historical_presence = SettingProgression(
            timeline_id=relation.id, field_key="presence", operation="replace",
            value={"exists": True, "state": {"identity": {"a_id": c1, "b_id": c3, "type_id": kin.id},
                                             "intimacy": 37, "status": "normal", "description": "曾为亲属"}},
            anchor_kind="chapter", chapter_id=None, boundary="end", source="manual", protection="manual",
            active=False, anchor_label="已删除章节的关系建立",
        )
        db.add_all([orphan, historical_presence])
        await db.commit()
        return {"friend": friend.id, "enemy": enemy.id, "confidant": confidant.id, "kin": kin.id,
                "world": world.id, "world_timeline": world_timeline.id, "text_field": text_field.id,
                "relationship_timeline": relation.id, "career_timeline": career.id, "source": source.id,
                "active": active.id, "orphan": orphan.id, "historical_presence": historical_presence.id}


async def test_progressions_round_trip_preserves_historical_references(client, app):
    p, ids = await _setup(client, app)
    temporal = await _seed_progression_changes(app, p["id"], ids)
    data = await _backup(client, p["id"])
    assert data["version"] == 3 and data["app_schema"] == "0034"
    types = data["tables"]["relationship_types"]
    assert {temporal["friend"], temporal["enemy"], temporal["confidant"], temporal["kin"]} <= {t["id"] for t in types}
    # 强制全局关系类型映射：朋友只被历史基线引用，模拟不同实例中的原 ID。
    friend_row = next(t for t in types if t["id"] == temporal["friend"])
    friend_row["id"] = 900001
    old_relation = next(t for t in data["tables"]["setting_timelines"] if t["id"] == temporal["relationship_timeline"])
    old_relation["baseline"]["identity"]["type_id"] = 900001
    kin_row = next(t for t in types if t["id"] == temporal["kin"])
    kin_row["id"] = 900002
    presence = next(e for e in data["tables"]["setting_progressions"] if e["id"] == temporal["historical_presence"])
    presence["value"]["state"]["identity"]["type_id"] = 900002
    # 有效备份的行顺序不一定按 ID 排列，导入后的角色大小顺序和替代事件插入顺序都可能反转。
    data["tables"]["characters"].reverse()
    data["tables"]["career_stages"].reverse()
    data["tables"]["setting_progressions"].reverse()
    imported = await _import(client, data)
    pid = imported["project"]["id"]
    async with app.state.sessionmaker() as db:
        chars = list(await db.scalars(select(Character).where(Character.project_id == pid).order_by(Character.id)))
        c1 = next(c.id for c in chars if c.role_type == "主角")
        c3 = next(c.id for c in chars if c.name == "苏晴")
        relation = await db.scalar(
            select(SettingTimeline).where(
                SettingTimeline.project_id == pid, SettingTimeline.subject_kind == "relationship",
                SettingTimeline.character_id == min(c1, c3), SettingTimeline.related_character_id == max(c1, c3),
            )
        )
        assert relation.baseline["identity"] == {"a_id": c1, "b_id": c3, "type_id": temporal["friend"]}
        events = list(await db.scalars(select(SettingProgression).where(SettingProgression.timeline_id == relation.id)))
        active = next(e for e in events if e.active)
        orphan = next(e for e in events if not e.active and e.field_key == "identity")
        presence = next(e for e in events if not e.active and e.field_key == "presence")
        assert active.value == {"a_id": c1, "b_id": c3, "type_id": temporal["enemy"]}
        assert active.chapter_id not in ids["chapters"]
        assert orphan.chapter_id is None and orphan.anchor_kind == "chapter" and orphan.anchor_label == "已删除章节"
        assert orphan.value["type_id"] == temporal["confidant"] and orphan.superseded_by_id == active.id
        assert presence.chapter_id is None and presence.value["state"]["identity"] == {
            "a_id": c1, "b_id": c3, "type_id": temporal["kin"]
        }
        career = await db.scalar(
            select(SettingTimeline).where(SettingTimeline.project_id == pid, SettingTimeline.subject_kind == "career")
        )
        event = await db.scalar(select(SettingProgression).where(SettingProgression.timeline_id == career.id))
        new_stages = list(await db.scalars(
            select(CareerStage).where(CareerStage.career_id == career.career_id).order_by(CareerStage.position)
        ))
        assert career.baseline["stage_id"] == new_stages[0].id and event.value == new_stages[1].id
        assert event.source_change_id != temporal["source"]
        assert (await db.get(AnalysisChange, event.source_change_id)).project_id == pid
        world = await db.scalar(
            select(WorldSetting).where(WorldSetting.project_id == pid, WorldSetting.title == "云梦山")
        )
        wt = await db.scalar(select(SettingTimeline).where(SettingTimeline.world_setting_id == world.id))
        field = await db.scalar(select(SettingTextField).where(SettingTextField.timeline_id == wt.id))
        change = await db.scalar(select(SettingProgression).where(SettingProgression.timeline_id == wt.id))
        assert field.id != temporal["text_field"] and change.custom_field_id == field.id
        assert (change.anchor_kind, change.chapter_no, change.value) == ("planned", 20, "苏晴")


async def test_v1_import_creates_unverified_baselines_without_replaying_history(client, app):
    p, _ = await _setup(client, app)
    data = await _backup(client, p["id"])
    data["version"] = 1
    for table in ("setting_timelines", "setting_text_fields", "setting_progressions"):
        data["tables"].pop(table, None)
    for row in data["tables"]["relationships"]:
        row.pop("is_present", None)
    long_note = "历史成员备注" * 60
    data["tables"]["organization_members"][0]["note"] = long_note
    result = await _import(client, data)
    async with app.state.sessionmaker() as db:
        pid = result["project"]["id"]
        timelines = list(await db.scalars(select(SettingTimeline).where(SettingTimeline.project_id == pid)))
        assert {t.subject_kind for t in timelines} == {"world", "character", "relationship", "career", "membership"}
        assert all(t.baseline_status == "legacy_unverified" and t.legacy_compat_allowed for t in timelines)
        assert await db.scalar(
            select(func.count())
            .select_from(SettingProgression)
            .join(SettingTimeline)
            .where(SettingTimeline.project_id == pid)
        ) == 0
        dead = await db.scalar(select(Character).where(Character.project_id == pid, Character.name == "苏晴"))
        timeline = next(t for t in timelines if t.character_id == dead.id and t.subject_kind == "character")
        assert (dead.current_state, dead.life_status) == ("已故", "dead")
        assert (timeline.baseline["current_state"], timeline.baseline["life_status"]) == ("已故", "dead")
        membership = next(t for t in timelines if t.subject_kind == "membership")
        member = await db.scalar(
            select(OrganizationMember).where(OrganizationMember.character_id == membership.character_id)
        )
        assert member.note == membership.baseline["note"] == long_note


async def test_v2_import_preserves_long_membership_notes(client, app):
    p, _ = await _setup(client, app)
    data = await _backup(client, p["id"])
    long_note = "完整成员备注" * 60
    data["tables"]["organization_members"][0]["note"] = long_note
    timeline = next(row for row in data["tables"]["setting_timelines"] if row["subject_kind"] == "membership")
    timeline["baseline"]["note"] = long_note
    result = await _import(client, data)
    async with app.state.sessionmaker() as db:
        pid = result["project"]["id"]
        membership = await db.scalar(select(SettingTimeline).where(
            SettingTimeline.project_id == pid, SettingTimeline.subject_kind == "membership"
        ))
        member = await db.scalar(
            select(OrganizationMember).where(OrganizationMember.character_id == membership.character_id)
        )
        assert member.note == membership.baseline["note"] == long_note


async def test_v2_rejects_incomplete_or_inconsistent_timeline_data(client, app):
    p, ids = await _setup(client, app)
    temporal = await _seed_progression_changes(app, p["id"], ids)
    data = await _backup(client, p["id"])
    cases = []
    missing_table = json.loads(json.dumps(data))
    missing_table["tables"].pop("setting_text_fields")
    cases.append(missing_table)
    missing_relation_type = json.loads(json.dumps(data))
    timeline = next(
        t for t in missing_relation_type["tables"]["setting_timelines"] if t["id"] == temporal["relationship_timeline"]
    )
    timeline["baseline"]["identity"]["type_id"] = 999999
    cases.append(missing_relation_type)
    missing_presence_type = json.loads(json.dumps(data))
    presence = next(e for e in missing_presence_type["tables"]["setting_progressions"]
                    if e["id"] == temporal["historical_presence"])
    presence["value"]["state"]["identity"]["type_id"] = 999999
    cases.append(missing_presence_type)
    missing_stage = json.loads(json.dumps(data))
    event = next(e for e in missing_stage["tables"]["setting_progressions"] if e.get("field_key") == "stage_id")
    event["value"] = 999999
    cases.append(missing_stage)
    wrong_stage_owner = json.loads(json.dumps(data))
    wrong_stage_owner["tables"]["careers"].append(
        {**wrong_stage_owner["tables"]["careers"][0], "id": 999991, "name": "另一个职业"}
    )
    wrong_stage_owner["tables"]["career_stages"].append(
        {**wrong_stage_owner["tables"]["career_stages"][0], "id": 999992, "career_id": 999991}
    )
    next(e for e in wrong_stage_owner["tables"]["setting_progressions"] if e.get("field_key") == "stage_id")[
        "value"
    ] = 999992
    cases.append(wrong_stage_owner)
    wrong_field_owner = json.loads(json.dumps(data))
    custom = next(e for e in wrong_field_owner["tables"]["setting_progressions"] if e.get("custom_field_id"))
    custom["timeline_id"] = temporal["career_timeline"]
    cases.append(wrong_field_owner)
    missing_subject = json.loads(json.dumps(data))
    subjects = missing_subject["tables"]["setting_timelines"]
    subjects.remove(next(t for t in subjects if t["subject_kind"] == "character"))
    cases.append(missing_subject)
    bad_identity = json.loads(json.dumps(data))
    relation = next(
        t for t in bad_identity["tables"]["setting_timelines"] if t["id"] == temporal["relationship_timeline"]
    )
    relation["baseline"]["identity"]["b_id"] = ids["characters"][1]
    cases.append(bad_identity)
    self_replacement = json.loads(json.dumps(data))
    orphan = next(e for e in self_replacement["tables"]["setting_progressions"] if e["id"] == temporal["orphan"])
    orphan["superseded_by_id"] = orphan["id"]
    cases.append(self_replacement)
    for case in cases:
        resp = await _preview_raw(client, json.dumps(case, ensure_ascii=False).encode())
        assert resp.status_code == 400, resp.text


async def test_v2_analysis_sources_must_belong_to_the_event_subject(client, app):
    p, ids = await _setup(client, app)
    await _seed_progression_changes(app, p["id"], ids)
    data = await _backup(client, p["id"])
    tables = data["tables"]
    sources = {row["kind"]: row for row in tables["analysis_changes"]}
    timelines = tables["setting_timelines"]
    template = next(row for row in tables["setting_progressions"] if row["field_key"] == "stage_id")

    def subject(source):
        target, kind = source["target"], source["kind"]
        for row in timelines:
            if kind in ("character_state", "life_status"):
                match = row["subject_kind"] == "character" and row["character_id"] == target["character_id"]
            elif kind in ("relationship_update", "relationship_create"):
                match = row["subject_kind"] == "relationship" and {
                    row["character_id"], row["related_character_id"]
                } == {target["a_id"], target["b_id"]}
            elif kind == "career_stage":
                match = (row["subject_kind"] == "career" and row["character_id"] == target["character_id"]
                         and row["career_id"] == target["career_id"])
            else:
                match = (row["subject_kind"] == "membership" and row["character_id"] == target["character_id"]
                         and row["organization_id"] == target["organization_id"])
            if match:
                return row
        raise AssertionError(f"未找到 {kind} 对应的主体")

    kinds = ("character_state", "life_status", "relationship_update", "relationship_create",
             "career_stage", "org_membership")
    fields = {"character": "profile", "relationship": "description", "career": "note", "membership": "position"}
    for index, kind in enumerate(kinds):
        source = sources[kind]
        timeline = subject(source)
        # 手动调整后的字段和章节位置允许不同于原分析来源。
        event = {**template, "id": 900000 + index, "timeline_id": timeline["id"],
                 "field_key": fields[timeline["subject_kind"]], "value": "手动修改",
                 "source_change_id": source["id"], "protection": "manual",
                 "anchor_kind": "planned", "chapter_id": None, "chapter_no": 20}
        tables["setting_progressions"].append(event)
    # 关系顺序无关，创建来源可以没有载体 ID。
    target = sources["relationship_update"]["target"]
    target["a_id"], target["b_id"] = target["b_id"], target["a_id"]
    sources["relationship_create"]["target"].pop("relationship_id")
    assert (await _preview_raw(client, json.dumps(data, ensure_ascii=False).encode())).status_code == 200

    def cloned():
        return json.loads(json.dumps(data))

    cases = []
    for kind, key in (("character_state", "character_id"), ("life_status", "character_id"),
                      ("relationship_update", "a_id"), ("relationship_create", "b_id"),
                      ("career_stage", "character_id"), ("org_membership", "character_id")):
        case = cloned()
        source = next(row for row in case["tables"]["analysis_changes"] if row["kind"] == kind)
        source["target"][key] = next(i for i in ids["characters"] if i != source["target"][key])
        cases.append(case)
    missing_endpoint = cloned()
    next(row for row in missing_endpoint["tables"]["analysis_changes"]
         if row["kind"] == "relationship_create")["target"].pop("a_id")
    cases.append(missing_endpoint)
    wrong_carrier = cloned()
    source = next(row for row in wrong_carrier["tables"]["analysis_changes"] if row["kind"] == "relationship_update")
    source["target"]["relationship_id"] = next(
        row["id"] for row in wrong_carrier["tables"]["relationships"]
        if row["id"] != source["target"]["relationship_id"]
    )
    cases.append(wrong_carrier)
    missing_carrier = cloned()
    next(row for row in missing_carrier["tables"]["analysis_changes"]
         if row["kind"] == "relationship_update")["target"]["relationship_id"] = 999999
    cases.append(missing_carrier)
    wrong_career = cloned()
    wrong_career["tables"]["careers"].append({**wrong_career["tables"]["careers"][0], "id": 999991})
    next(row for row in wrong_career["tables"]["analysis_changes"]
         if row["kind"] == "career_stage")["target"]["career_id"] = 999991
    cases.append(wrong_career)
    wrong_organization = cloned()
    source = next(row for row in wrong_organization["tables"]["analysis_changes"] if row["kind"] == "org_membership")
    source["target"]["organization_id"] = next(
        row["id"] for row in wrong_organization["tables"]["organizations"]
        if row["id"] != source["target"]["organization_id"]
    )
    cases.append(wrong_organization)
    unrelated_kind = cloned()
    event = next(row for row in unrelated_kind["tables"]["setting_progressions"] if row["id"] == 900000)
    event["source_change_id"] = sources["foreshadow_resolve"]["id"]
    cases.append(unrelated_kind)
    for case in cases:
        resp = await _preview_raw(client, json.dumps(case, ensure_ascii=False).encode())
        assert resp.status_code == 400, resp.text

    imported = await _import(client, data)
    async with app.state.sessionmaker() as db:
        rows = list(await db.scalars(
            select(SettingProgression).join(SettingTimeline).where(
                SettingTimeline.project_id == imported["project"]["id"], SettingProgression.protection == "manual",
                SettingProgression.source_change_id.is_not(None),
            )
        ))
        assert len(rows) == len(kinds)
        for event in rows:
            timeline = await db.get(SettingTimeline, event.timeline_id)
            source = await db.get(AnalysisChange, event.source_change_id)
            target = source.target
            if timeline.subject_kind == "relationship":
                assert {target["a_id"], target["b_id"]} == {timeline.character_id, timeline.related_character_id}
                if "relationship_id" in target:
                    relation = await db.get(Relationship, target["relationship_id"])
                    assert {relation.a_id, relation.b_id} == {timeline.character_id, timeline.related_character_id}
            else:
                assert target["character_id"] == timeline.character_id
                if timeline.subject_kind == "career":
                    assert target["career_id"] == timeline.career_id
                elif timeline.subject_kind == "membership":
                    assert target["organization_id"] == timeline.organization_id


async def test_v2_import_rebuilds_current_projection_from_all_subject_timelines(client, app):
    p, ids = await _setup(client, app)
    temporal = await _seed_progression_changes(app, p["id"], ids)
    data = await _backup(client, p["id"])
    tables = data["tables"]
    timelines = tables["setting_timelines"]
    character = next(row for row in timelines
                     if row["subject_kind"] == "character" and row["character_id"] == ids["characters"][0])
    world = next(row for row in timelines if row["id"] == temporal["world_timeline"])
    membership = next(row for row in timelines if row["subject_kind"] == "membership")
    career = next(row for row in timelines if row["id"] == temporal["career_timeline"])
    template = next(row for row in tables["setting_progressions"] if row["field_key"] == "stage_id")
    for index, (timeline, field_key, value) in enumerate((
        (character, "profile", "新的角色档案"),
        (world, "content", "新的世界设定"),
        (membership, "position", "护法"),
    )):
        tables["setting_progressions"].append({
            **template, "id": 910000 + index, "timeline_id": timeline["id"], "field_key": field_key,
            "value": value, "anchor_kind": "chapter", "chapter_id": ids["chapters"][-1], "chapter_no": None,
            "source": "manual", "protection": "manual", "source_change_id": None,
        })
    next(row for row in tables["characters"] if row["id"] == character["character_id"])["profile"] = "过时缓存"
    next(row for row in tables["world_settings"] if row["id"] == temporal["world"])["content"] = "过时缓存"
    next(row for row in tables["relationships"] if {row["a_id"], row["b_id"]} ==
         {ids["characters"][0], ids["characters"][2]})["type_id"] = temporal["friend"]
    tables["character_careers"][0]["stage_id"] = career["baseline"]["stage_id"]
    tables["organization_members"][0]["position"] = "过时缓存"

    imported = await _import(client, data)
    async with app.state.sessionmaker() as db:
        pid = imported["project"]["id"]
        main = await db.scalar(select(Character).where(Character.project_id == pid, Character.role_type == "主角"))
        place = await db.scalar(
            select(WorldSetting).where(WorldSetting.project_id == pid, WorldSetting.title == "云梦山")
        )
        member = await db.scalar(select(OrganizationMember).where(OrganizationMember.character_id == main.id))
        assignment = await db.scalar(select(CharacterCareer).where(CharacterCareer.character_id == main.id))
        new_career = await db.scalar(select(SettingTimeline).where(
            SettingTimeline.project_id == pid, SettingTimeline.subject_kind == "career"
        ))
        stage_change = await db.scalar(select(SettingProgression).where(
            SettingProgression.timeline_id == new_career.id, SettingProgression.field_key == "stage_id"
        ))
        relation = await db.scalar(
            select(Relationship).where(Relationship.project_id == pid, Relationship.a_id == main.id)
        )
        assert main.profile == "新的角色档案"
        assert place.content == "新的世界设定"
        assert member.position == "护法"
        assert assignment.stage_id == stage_change.value
        assert relation.type_id == temporal["enemy"]


async def test_v2_import_rolls_back_when_materialized_history_is_invalid(client, app):
    p, ids = await _setup(client, app)
    await _seed_progression_changes(app, p["id"], ids)
    data = await _backup(client, p["id"])
    tables = data["tables"]
    timeline = next(row for row in tables["setting_timelines"] if row["subject_kind"] == "membership")
    template = next(row for row in tables["setting_progressions"] if row["field_key"] == "stage_id")
    tables["setting_progressions"].append({
        **template, "id": 920000, "timeline_id": timeline["id"], "field_key": "presence", "operation": "replace",
        "value": {"exists": False}, "anchor_kind": "chapter", "chapter_id": ids["chapters"][0], "chapter_no": None,
        "source": "manual", "protection": "manual", "source_change_id": None,
    })
    tables["setting_progressions"].append({
        **template, "id": 920001, "timeline_id": timeline["id"], "field_key": "position", "operation": "replace",
        "value": "护法", "anchor_kind": "chapter", "chapter_id": ids["chapters"][1], "chapter_no": None,
        "source": "manual", "protection": "manual", "source_change_id": None,
    })
    preview = await _preview_raw(client, json.dumps(data, ensure_ascii=False).encode())
    assert preview.status_code == 200, preview.text
    async with app.state.sessionmaker() as db:
        before = await db.scalar(select(func.count()).select_from(Project))
    result = await client.post(f"{API}/import", json={"token": preview.json()["token"]})
    assert result.status_code in (400, 409), result.text
    async with app.state.sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(Project)) == before


async def test_round_trip_all_entities(client, app):
    p, ids = await _setup(client, app)
    src = await _backup(client, p["id"], **ALL)
    assert src["format"] == "ainovel.project" and src["version"] == 3
    assert src["options"] == {"versions": True, "snapshots": True, "generation_logs": True, "inspiration": True}
    # 不导出任务 / 分块 / 检索单元
    assert not {"tasks", "kb_chunks", "retrieval_chunks"} & set(src["tables"])
    assert "project_id" not in src["tables"]["characters"][0] and "user_id" not in src["project"]

    result = await _import(client, src)
    new = result["project"]
    assert new["title"] == "星海纪元（导入）" and new["id"] != p["id"]
    assert result["counts"]["characters"] == 3 and result["counts"]["chapter_versions"] == 2
    dst = await _backup(client, new["id"], **ALL)

    # 逐表数量与内容（id / 外键按行序号比对）一致
    assert set(dst["tables"]) == set(src["tables"])
    for table in src["tables"]:
        assert len(dst["tables"][table]) == len(src["tables"][table]), table
    assert _canonical(dst) == _canonical(src)
    assert dst["styles"] == src["styles"]
    strip = {"id", "title", "updated_at", "reading_progress"}
    assert src["project"]["reading_progress"]["chapter_id"] == ids["chapters"][1]
    assert {k: v for k, v in dst["project"].items() if k not in strip} == {
        k: v for k, v in src["project"].items() if k not in strip
    }

    async with app.state.sessionmaker() as db:
        pid = new["id"]
        chars = list(await db.scalars(select(Character).where(Character.project_id == pid).order_by(Character.id)))
        assert [c.name for c in chars] == ["林远", "林远", "苏晴"] and chars[1].profile == "同名的另一个人"
        c1, c2, c3 = (c.id for c in chars)
        assert not {c1, c2, c3} & set(ids["characters"])
        chapters = list(await db.scalars(select(Chapter).where(Chapter.project_id == pid).order_by(Chapter.id)))
        assert [c.title for c in chapters] == ["重逢", "重逢", "下山"]
        ch1, ch2, _ = chapters
        # 阅读进度的章节 id 改写到新章节，其余字段原样
        assert (await db.get(Project, pid)).reading_progress == {
            "chapter_id": ch2.id,
            "chapter_no": 2,
            "ratio": 0.5,
            "updated_at": "2026-10-27T00:00:00Z",
        }
        # 风格：预设对应到同一个预设，自定义风格同名同内容复用
        assert ch1.style_id == ids["lean"]
        assert new["default_style_id"] == p["default_style_id"]
        # 互相引用：细纲 ↔ 章节
        plan = await db.scalar(select(ChapterPlan).where(ChapterPlan.project_id == pid))
        assert plan.chapter_id == ch2.id and ch2.plan_id == plan.id and ch1.plan_id is None
        # v3 的已建章细纲只导出章 cast 的兼容镜像；旧库中分歧的细纲建议不被导出动作改写。
        assert plan.character_ids == []
        original_plan = await db.scalar(select(ChapterPlan).where(ChapterPlan.project_id == p["id"]))
        assert original_plan.character_ids == [ids["characters"][0], ids["characters"][2]]
        # 自引用：组织上级
        orgs = list(
            await db.scalars(select(Organization).where(Organization.project_id == pid).order_by(Organization.id))
        )
        assert orgs[1].parent_id == orgs[0].id
        member = await db.scalar(select(OrganizationMember).where(OrganizationMember.organization_id == orgs[1].id))
        assert member.character_id == c1
        # 关系类型：自定义类型复制到新项目，全局预置类型仍指向全局
        rels = list(
            await db.scalars(select(Relationship).where(Relationship.project_id == pid).order_by(Relationship.id))
        )
        custom = await db.get(RelationshipType, rels[0].type_id)
        assert custom.project_id == pid and custom.role_a == "道侣" and custom.id != ids["custom_type"]
        assert rels[1].type_id == ids["master"]
        assert (rels[0].pair_low, rels[0].pair_high) == (min(c1, c3), max(c1, c3))
        stages = list(
            await db.scalars(
                select(CareerStage).join(Career, Career.id == CareerStage.career_id).where(Career.project_id == pid)
            )
        )
        cc = await db.scalar(select(CharacterCareer).where(CharacterCareer.character_id == c1))
        assert cc.stage_id == stages[1].id and stages[1].name == "筑基"
        fs = await db.scalar(select(Foreshadow).where(Foreshadow.project_id == pid))
        an = await db.scalar(select(ChapterAnalysis).where(ChapterAnalysis.project_id == pid))
        assert an.chapter_id == ch1.id and an.foreshadow_candidates[0]["ref_id"] == fs.id
        assert an.foreshadow_candidates[1] == {"action": "plant"}
        # 分析回写条目：每种类型的 target / before / after / field_key 都改写到新 id
        changes = {c.kind: c for c in await db.scalars(select(AnalysisChange).where(AnalysisChange.project_id == pid))}
        assert set(changes) == set(
            [
                "character_state",
                "life_status",
                "relationship_update",
                "relationship_create",
                "career_stage",
                "org_membership",
                "foreshadow_resolve",
                "foreshadow_plant_planned",
                "foreshadow_new",
            ]
        )
        r1, r2 = rels
        assert changes["character_state"].target["character_id"] == c1
        assert changes["character_state"].field_key == f"char:{c1}:current_state"
        assert changes["life_status"].field_key == f"char:{c3}:life_status"
        upd = changes["relationship_update"]
        assert (upd.target["relationship_id"], upd.target["a_id"], upd.target["b_id"]) == (r1.id, c1, c3)
        assert upd.field_key == f"rel:{r1.id}:intimacy" and (upd.before, upd.after) == (50, 80)
        create = changes["relationship_create"]
        assert (create.target["a_id"], create.target["b_id"], create.target["relationship_id"]) == (c3, c2, r2.id)
        assert create.field_key == f"relpair:{min(c2, c3)}:{max(c2, c3)}"
        assert create.after == {"role_a": "师父", "role_b": "徒弟", "a_id": c3, "b_id": c2, "type_id": ids["master"]}
        career_change = changes["career_stage"]
        career_id = await db.scalar(select(Career.id).where(Career.project_id == pid))
        assert career_change.target["career_id"] == career_id and career_change.field_key == f"career:{c1}:{career_id}"
        assert career_change.before["stage_id"] == stages[0].id and career_change.after["stage_id"] == stages[1].id
        org_change = changes["org_membership"]
        assert org_change.target["organization_id"] == orgs[1].id and org_change.field_key == f"org:{orgs[1].id}:{c1}"
        assert changes["foreshadow_resolve"].target["foreshadow_id"] == fs.id
        assert changes["foreshadow_resolve"].field_key == f"fs:{fs.id}:resolve"
        # 原本就指向不存在的伏笔（跳过的条目）：置空
        assert changes["foreshadow_plant_planned"].target["foreshadow_id"] is None
        assert changes["foreshadow_new"].after["character_ids"] == sorted([c3, c1])
        # 历史（按名称记录，原样保留）
        rh = await db.scalar(select(RelationshipHistory).where(RelationshipHistory.relationship_id == r1.id))
        assert rh.changes == {"intimacy": [50, 80]}
        ch_hist = await db.scalar(select(CareerHistory).where(CareerHistory.character_id == c1))
        assert ch_hist.career_id == career_id and ch_hist.changes == {"stage": ["第1阶·练气", "第2阶·筑基"]}
        fh = await db.scalar(select(ForeshadowHistory).where(ForeshadowHistory.foreshadow_id == fs.id))
        assert fh.changes == {"characters": [None, ["苏晴"]]}
        assert (
            await db.scalar(select(ForeshadowCharacter.character_id).where(ForeshadowCharacter.foreshadow_id == fs.id))
            == c3
        )
        assert set(
            await db.scalars(select(ChapterCharacter.character_id).where(ChapterCharacter.chapter_id == ch1.id))
        ) == {c1, c2}
        # 版本 → 生成记录
        log = await db.scalar(select(GenerationLog).where(GenerationLog.project_id == pid))
        versions = list(
            await db.scalars(
                select(ChapterVersion).where(ChapterVersion.chapter_id == ch1.id).order_by(ChapterVersion.seq)
            )
        )
        assert [v.generation_log_id for v in versions] == [log.id, None] and versions[1].starred
        assert log.chapter_id == ch1.id and log.task_id is None and log.ai_config_id is None
        # 记忆：钉住 / 屏蔽 / content_key 保留
        mems = list(await db.scalars(select(StoryMemory).where(StoryMemory.project_id == pid).order_by(StoryMemory.id)))
        assert (mems[0].pinned, mems[0].muted, mems[0].content_key, mems[0].chapter_id, mems[0].analysis_id) == (
            True,
            False,
            "abc",
            ch1.id,
            an.id,
        )
        assert (mems[1].pinned, mems[1].muted, mems[1].chapter_id) == (False, True, None)
        settings = await db.get(KbSettings, pid)
        assert settings.top_k == 9 and settings.auto_inject is False
        doc = await db.scalar(select(KbDocument).where(KbDocument.project_id == pid))
        chunks = await db.scalar(select(func.count(KbChunk.id)).where(KbChunk.document_id == doc.id))
        assert doc.text == KB_TEXT and doc.chunk_count == chunks > 0 and doc.status == "ready_keyword_only"
        # 快照：现有行改写为新 id，已不存在的大纲 / 细纲分配新 id 保持自洽，指向不存在章节的 link 去掉
        snap = await db.scalar(select(OutlineSnapshot).where(OutlineSnapshot.project_id == pid))
        outline_ids = [
            o.id for o in await db.scalars(select(Outline).where(Outline.project_id == pid).order_by(Outline.id))
        ]
        data = snap.data
        assert data["outlines"][0]["id"] == outline_ids[0]
        ghost = data["outlines"][1]["id"]
        assert ghost not in (88888, *outline_ids) and ghost > max(outline_ids)
        assert data["plans"][0] == {
            "id": plan.id,
            "outline_id": outline_ids[0],
            "seq": 1,
            "title": "重逢",
            "character_ids": [c1],
        }
        assert data["plans"][1]["outline_id"] == ghost and data["plans"][1]["id"] != 66666
        assert data["links"] == [
            {"chapter_id": ch2.id, "outline_id": outline_ids[0], "plan_id": plan.id, "chapter_no": 2}
        ]
        sess = await db.scalar(select(InspirationSession).where(InspirationSession.project_id == pid))
        assert sess.state == {"idea": "修仙"} and sess.user_id == (await db.get(Project, pid)).user_id
    # 快照导入后可以恢复
    snaps = (await client.get(f"{API}/{new['id']}/outline-snapshots")).json()
    resp = await client.post(f"{API}/{new['id']}/outline-snapshots/{snaps[0]['id']}/restore")
    assert resp.status_code == 200, resp.text
    # 警告：JSON 内对应不上的引用
    assert any("引用在备份中找不到" in w for w in result["warnings"])
    assert result["preview"]["counts"] == result["counts"]


async def test_options_include_or_exclude(client, app):
    p, _ = await _setup(client, app)
    default = await _backup(client, p["id"])
    assert default["options"] == {"versions": True, "snapshots": False, "generation_logs": False, "inspiration": False}
    assert "chapter_versions" in default["tables"]
    assert not {"outline_snapshots", "generation_logs", "inspiration_sessions"} & set(default["tables"])
    # 不导出生成记录时，版本里指向生成记录的外键导出为 null
    assert [v["generation_log_id"] for v in default["tables"]["chapter_versions"]] == [None, None]
    none = await _backup(client, p["id"], versions="0")
    assert "chapter_versions" not in none["tables"]
    result = await _import(client, none)
    pid = result["project"]["id"]
    assert result["preview"]["options"]["versions"] is False
    async with app.state.sessionmaker() as db:
        chapter_ids = list(await db.scalars(select(Chapter.id).where(Chapter.project_id == pid)))
        assert (
            await db.scalar(select(func.count(ChapterVersion.id)).where(ChapterVersion.chapter_id.in_(chapter_ids)))
            == 0
        )
        assert await db.scalar(select(func.count(OutlineSnapshot.id)).where(OutlineSnapshot.project_id == pid)) == 0
        assert await db.scalar(select(func.count(GenerationLog.id)).where(GenerationLog.project_id == pid)) == 0
        assert (
            await db.scalar(select(func.count(InspirationSession.id)).where(InspirationSession.project_id == pid)) == 0
        )
    full = await _import(client, await _backup(client, p["id"], **ALL))
    assert full["preview"]["options"] == {
        "versions": True,
        "snapshots": True,
        "generation_logs": True,
        "inspiration": True,
    }
    for table in ("chapter_versions", "outline_snapshots", "generation_logs", "inspiration_sessions"):
        assert full["counts"][table] >= 1


async def test_styles_reuse_copy_and_suffix(client, app, make_client):
    p, ids = await _setup(client, app)
    data = await _backup(client, p["id"])
    custom = next(s for s in data["styles"] if s["preset_key"] is None)
    preset = next(s for s in data["styles"] if s["preset_key"] == "lean_cold")
    # 同一用户：同名同内容复用
    same = await _import(client, data)
    assert {s["action"] for s in same["preview"]["styles"]} == {"preset", "reuse"}
    assert same["project"]["default_style_id"] == p["default_style_id"]
    # 同名不同内容：加「（导入）」新建
    changed = json.loads(json.dumps(data))
    next(s for s in changed["styles"] if s["preset_key"] is None)["extra"] = "完全不同的风格"
    diff = await _import(client, changed)
    brief = next(s for s in diff["preview"]["styles"] if s["preset_key"] is None)
    assert brief == {"name": f"{custom['name']}（导入）", "preset_key": None, "action": "create"}
    style = (await client.get(f"/api/v1/styles/{diff['project']['default_style_id']}")).json()
    assert style["name"] == f"{custom['name']}（导入）" and style["extra"] == "完全不同的风格"
    # 预设已不存在：复制为自定义风格并警告
    missing = json.loads(json.dumps(data))
    next(s for s in missing["styles"] if s["preset_key"])["preset_key"] = "no_such_preset"
    copied = await _import(client, missing)
    assert any("no_such_preset" in w for w in copied["warnings"])
    async with app.state.sessionmaker() as db:
        ch = await db.scalar(
            select(Chapter).where(Chapter.project_id == copied["project"]["id"], Chapter.style_id.is_not(None))
        )
        style_row = await db.get(WritingStyle, ch.style_id)
        assert style_row.user_id is not None and style_row.preset_key is None and style_row.pov == preset["pov"]
    # 其他用户导入：自定义风格复制到他的库，预设仍对应同一个预设
    bob = make_client()
    await register(bob, "bob")
    other = await _import(bob, data)
    styles = (await bob.get("/api/v1/styles")).json()
    mine = [s for s in styles if not s.get("is_preset") and s["name"] == custom["name"]]
    assert len(mine) == 1 and other["project"]["default_style_id"] == mine[0]["id"]
    async with app.state.sessionmaker() as db:
        ch = await db.scalar(
            select(Chapter).where(Chapter.project_id == other["project"]["id"], Chapter.style_id.is_not(None))
        )
        assert ch.style_id == ids["lean"]


async def test_missing_global_relationship_type_falls_back(client, app):
    p, _ = await _setup(client, app)
    data = await _backup(client, p["id"])
    row = next(r for r in data["tables"]["relationship_types"] if r["global"])
    row["role_a"], row["role_b"] = "宗主", "门人"
    result = await _import(client, data)
    warning = "预置关系类型「宗主/门人」在当前版本中不存在，已改为项目自定义类型"
    assert warning in result["preview"]["warnings"] and warning in result["warnings"]
    async with app.state.sessionmaker() as db:
        pid = result["project"]["id"]
        types = list(
            await db.scalars(
                select(RelationshipType)
                .join(Relationship, Relationship.type_id == RelationshipType.id)
                .where(Relationship.project_id == pid)
            )
        )
        assert {(t.role_a, t.project_id) for t in types} == {("道侣", pid), ("宗主", pid)}


async def _preview_raw(client, raw: bytes):
    return await client.post(f"{API}/import/preview", files={"file": ("b.json", raw, "application/json")})


async def test_validation_errors(client, app, monkeypatch):
    p, _ = await _setup(client, app)
    data = await _backup(client, p["id"])

    def dump(d) -> bytes:
        return json.dumps(d, ensure_ascii=False).encode()

    cases = [
        (b"not json", "不是合法的 JSON"),
        (b"[1]", "应为 JSON 对象"),
        (dump({**data, "format": "ainovel.cards"}), "format 应为 ainovel.project"),
        (dump({**data, "version": 4}), "备份来自更新的版本"),
        (dump({**data, "version": 0}), "不支持的备份版本"),
        ("﻿".encode() + b"{" * 1 + b'"format": "x"}', "format 应为"),
    ]
    for raw, msg in cases:
        resp = await _preview_raw(client, raw)
        assert resp.status_code == 400 and msg in resp.json()["detail"], (raw[:40], resp.text)
    # 嵌套过深
    deep = {**data, "x": json.loads("[" * 100 + "]" * 100)}
    resp = await _preview_raw(client, dump(deep))
    assert resp.status_code == 400 and "嵌套层级过深" in resp.json()["detail"]
    # 文件内引用断裂：返回 400 并列出原因
    broken = json.loads(json.dumps(data))
    broken["tables"]["chapters"][0]["outline_id"] = 99999
    broken["tables"]["characters"][0].pop("name")
    resp = await _preview_raw(client, dump(broken))
    assert resp.status_code == 400
    errors = resp.json()["errors"]
    assert "角色第 1 行缺少字段 name" in errors
    broken["tables"]["characters"][0]["name"] = "林远"
    resp = await _preview_raw(client, dump(broken))
    assert resp.status_code == 400 and resp.json()["errors"] == ["章节第 1 行的 outline_id 引用了不存在的大纲 #99999"]
    # 类型 / 长度
    bad = json.loads(json.dumps(data))
    bad["tables"]["characters"][0]["name"] = "长" * 101
    bad["tables"]["characters"][1]["always_in_context"] = "yes"
    errors = (await _preview_raw(client, dump(bad))).json()["errors"]
    assert "角色第 1 行字段 name 超过长度上限 100" in errors
    assert "角色第 2 行字段 always_in_context 应为布尔值" in errors
    bad = json.loads(json.dumps(data))
    bad["tables"]["chapters"][0]["style_ref"] = "s99"
    errors = (await _preview_raw(client, dump(bad))).json()["errors"]
    assert errors == ["章节第 1 行引用的风格 s99 不在 styles 中"]
    # 大小上限（路由兜底；请求体总量由中间件在解析前拦截，见 test_upload_limit_rule）
    monkeypatch.setattr(importer, "MAX_BYTES", 100)
    resp = await _preview_raw(client, dump(data))
    assert resp.status_code == 400 and resp.json()["detail"] == "备份文件过大，上限为 100MB"


async def test_validation_rejects_values_that_break_reads(client, app):
    """能写进库、但会让读取接口 500 的值在预览时就拦下：JSON 列结构、枚举、整数范围、NaN、向导进度结构。"""
    p, _ = await _setup(client, app)
    data = await _backup(client, p["id"])

    def case(mutate) -> dict:
        d = json.loads(json.dumps(data))
        mutate(d)
        return d

    def set_project(col, value):
        return lambda d: d["project"].__setitem__(col, value)

    def set_row(table, col, value):
        return lambda d: d["tables"][table][0].__setitem__(col, value)

    cases = [
        (set_project("tags", "仙侠"), "项目第 1 行字段 tags 应为数组"),
        (set_project("wizard_progress", {"done": "x"}), "项目字段 wizard_progress 结构不正确"),
        (set_row("characters", "aliases", ["a", 1]), "角色第 1 行字段 aliases 应为字符串数组"),
        (set_row("chapter_plans", "key_events", {}), "细纲第 1 行字段 key_events 应为数组"),
        (set_row("chapters", "status", "weird"), "章节第 1 行字段 status 取值 weird 不合法"),
        (set_row("story_memories", "kind", "x"), "剧情记忆第 1 行字段 kind 取值 x 不合法"),
        (set_row("world_settings", "sort", 10**20), "世界观第 1 行字段 sort 超出整数范围"),
    ]
    for mutate, msg in cases:
        raw = json.dumps(case(mutate), ensure_ascii=False).encode()
        resp = await _preview_raw(client, raw)
        assert resp.status_code == 400 and msg in resp.json()["errors"], (msg, resp.text)
    # NaN / Infinity 不是合法 JSON（json.loads 默认会接受）
    raw = json.dumps(data, ensure_ascii=False).replace('"top_k": 9', '"top_k": 9, "min_score": NaN').encode()
    resp = await _preview_raw(client, raw)
    assert resp.status_code == 400 and "NaN" in resp.json()["detail"]
    # 生成中的章节导入为草稿（没有对应的任务）
    gen = case(lambda d: d["tables"]["chapters"][0].__setitem__("status", "generating"))
    result = await _import(client, gen)
    chapters = (await client.get(f"{API}/{result['project']['id']}/chapters")).json()
    assert "generating" not in {c["status"] for c in chapters}


def test_upload_limit_rule(app):
    middleware = next(m for m in app.user_middleware if m.cls.__name__ == "UploadLimitMiddleware")
    rules = middleware.kwargs["rules"]
    for path in ("/api/v1/projects/import/preview", "/api/v1/projects/import"):
        rule = next(r for r in rules if r[0] == "POST" and r[1].fullmatch(path))
        assert rule[2] >= 100 * 1024 * 1024 and rule[3] == "备份文件过大，上限为 100MB"


async def test_upload_body_limit_before_parsing(client, app, monkeypatch):
    await register(client)
    # 分块传输（没有 Content-Length）：中间件边收边计数，超限即 400
    from app.core import upload_limit

    async def chunks():
        yield b'--x\r\nContent-Disposition: form-data; name="file"; filename="b.json"\r\n\r\n'
        for _ in range(5):
            yield b"x" * 1024

    middleware = next(m for m in app.user_middleware if m.cls.__name__ == "UploadLimitMiddleware")
    rules = middleware.kwargs["rules"]
    monkeypatch.setattr(
        upload_limit.UploadLimitMiddleware,
        "_match",
        lambda self, scope: (2048, rules[-1][3]) if scope.get("path", "").endswith("/import/preview") else None,
    )
    resp = await client.post(
        f"{API}/import/preview",
        content=chunks(),
        headers={"content-type": "multipart/form-data; boundary=x"},
    )
    assert resp.status_code == 400 and resp.json()["detail"] == "备份文件过大，上限为 100MB"


async def test_import_error_rolls_back(client, app):
    p, _ = await _setup(client, app)
    data = await _backup(client, p["id"])
    # 文件内校验能通过、插入时违反唯一约束（同项目组织重名）
    data["tables"]["organizations"][1]["name"] = "天剑宗"
    next(s for s in data["styles"] if s["preset_key"] is None)["extra"] = "会新建的风格"
    async with app.state.sessionmaker() as db:
        before = (
            await db.scalar(select(func.count(Project.id))),
            await db.scalar(select(func.count(WritingStyle.id))),
            await db.scalar(select(func.count(Character.id))),
        )
    raw = json.dumps(data, ensure_ascii=False).encode()
    token = (await _preview_raw(client, raw)).json()["token"]
    resp = await client.post(f"{API}/import", json={"token": token})
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["table"] == "organizations" and "组织（organizations）" in body["detail"] and "行" in body["detail"]
    async with app.state.sessionmaker() as db:
        after = (
            await db.scalar(select(func.count(Project.id))),
            await db.scalar(select(func.count(WritingStyle.id))),
            await db.scalar(select(func.count(Character.id))),
        )
    assert after == before
    # 失败后令牌仍可用（修好文件前不会重复导入）；成功导入后令牌失效
    resp = await client.post(f"{API}/import", json={"token": token})
    assert resp.status_code == 400 and resp.json()["table"] == "organizations"


async def test_token_expiry_and_isolation(client, app, make_client, monkeypatch):
    p, _ = await _setup(client, app)
    raw = json.dumps(await _backup(client, p["id"]), ensure_ascii=False).encode()
    token = (await _preview_raw(client, raw)).json()["token"]
    bob = make_client()
    await register(bob, "bob")
    resp = await bob.post(f"{API}/import", json={"token": token})
    assert resp.status_code == 400 and resp.json()["detail"] == importer.TOKEN_EXPIRED
    resp = await client.post(f"{API}/import", json={"token": token, "title": "新书名"})
    assert resp.status_code == 201 and resp.json()["project"]["title"] == "新书名"
    # 用过即失效
    resp = await client.post(f"{API}/import", json={"token": token})
    assert resp.status_code == 400 and resp.json()["detail"] == importer.TOKEN_EXPIRED
    # 过期
    token = (await _preview_raw(client, raw)).json()["token"]
    now = importer._now()
    monkeypatch.setattr(importer, "_now", lambda: now + importer.TOKEN_TTL + 1)
    resp = await client.post(f"{API}/import", json={"token": token})
    assert resp.status_code == 400 and resp.json()["detail"] == "导入凭据已过期，请重新选择备份文件"
    resp = await client.post(f"{API}/import", json={"title": "x"})
    assert resp.status_code == 400


async def test_direct_multipart_import_and_title_suffix(client, app):
    p, _ = await _setup(client, app)
    raw = json.dumps(await _backup(client, p["id"]), ensure_ascii=False).encode()
    first = await client.post(f"{API}/import", files={"file": ("b.json", raw, "application/json")})
    assert first.status_code == 201 and first.json()["project"]["title"] == "星海纪元（导入）"
    second = await client.post(
        f"{API}/import", files={"file": ("b.json", raw, "application/json")}, data={"title": "星海纪元"}
    )
    assert second.status_code == 201 and second.json()["project"]["title"] == "星海纪元（导入2）"
    resp = await client.post(f"{API}/import", files={"other": ("b.json", raw, "application/json")})
    assert resp.status_code == 400 and resp.json()["detail"] == "请选择备份文件"


async def test_cross_user(client, app, make_client):
    p, _ = await _setup(client, app)
    data = await _backup(client, p["id"])
    bob = make_client()
    await register(bob, "bob")
    assert (await bob.get(f"{API}/{p['id']}/backup")).status_code == 404
    assert (await bob.get(f"{API}/{p['id']}/backup/estimate")).status_code == 404
    result = await _import(bob, data)
    pid = result["project"]["id"]
    assert result["project"]["title"] == "星海纪元"
    assert pid in [x["id"] for x in (await bob.get(API)).json()]
    assert pid not in [x["id"] for x in (await client.get(API)).json()]
    assert (await client.get(f"{API}/{pid}")).status_code == 404
    async with app.state.sessionmaker() as db:
        bob_id = (await db.get(Project, pid)).user_id
        sess = await db.scalar(select(InspirationSession).where(InspirationSession.project_id == pid))
        assert sess is None or sess.user_id == bob_id


async def test_estimate(client, app):
    p, _ = await _setup(client, app)
    est = (await client.get(f"{API}/{p['id']}/backup/estimate")).json()
    assert set(est) == {"core", "versions", "snapshots", "generation_logs", "inspiration"}
    assert est["versions"]["rows"] == 2 and est["generation_logs"]["rows"] == 1
    assert est["snapshots"]["rows"] == 1 and est["inspiration"]["rows"] == 1
    assert est["core"]["rows"] > 20 and est["core"]["chars"] > len(KB_TEXT) and est["core"]["bytes"] > 0


async def test_retrieval_after_import_keyword_only(client, app):
    p, _ = await _setup(client, app)
    result = await _import(client, await _backup(client, p["id"]))
    assert result["task_id"] is None
    pid = result["project"]["id"]
    kb = (await client.post(f"{API}/{pid}/kb/search", json={"query": "天剑宗在哪里", "min_score": 0})).json()
    assert kb["mode"] == "keyword" and kb["hits"] and kb["hits"][0]["filename"] == "设定集.md"
    mem = (
        await client.post(
            f"{API}/{pid}/memories/search", json={"query": "神秘玉佩", "min_score": 0, "include_muted": True}
        )
    ).json()
    assert [h["memory"]["content"] for h in mem["hits"]][:1] == ["苏晴留下神秘玉佩"]
    # 屏蔽的记忆默认不参与检索，钉住标记保留
    mem = (await client.post(f"{API}/{pid}/memories/search", json={"query": "神秘玉佩", "min_score": 0})).json()
    assert all(h["memory"]["content"] != "苏晴留下神秘玉佩" for h in mem["hits"])
    page = (await client.get(f"{API}/{pid}/memories")).json()
    flags = {m["content"]: (m["pinned"], m["muted"]) for m in page["items"]}
    assert flags == {"林远拜入天剑宗修行": (True, False), "苏晴留下神秘玉佩": (False, True)}


async def test_import_queues_rebuild_with_embedding_model(client, app):
    p, _ = await _setup(client, app)
    await create_ai_config(client, name="向量", model_type="embedding", model="emb")
    result = await _import(client, await _backup(client, p["id"]))
    pid = result["project"]["id"]
    assert result["task_id"] is not None
    task = await wait_task(client, result["task_id"])
    assert task["status"] == "succeeded" and task["kind"] == "kb.index" and task["project_id"] == pid
    status = (await client.get(f"{API}/{pid}/kb/index-status")).json()
    assert status["needs_rebuild"] is False and status["stale_count"] == 0
    docs = (await client.get(f"{API}/{pid}/kb/documents")).json()
    assert docs[0]["status"] == "ready"
    kb = (await client.post(f"{API}/{pid}/kb/search", json={"query": "天剑宗", "min_score": 0})).json()
    assert kb["mode"] == "hybrid"


def test_token_cache_global_cap(monkeypatch):
    """全实例缓存份数有上限：超出时淘汰最早到期的预览，被淘汰的凭据视为过期。"""
    importer.clear_tokens()
    monkeypatch.setattr(importer, "MAX_TOKENS_TOTAL", 3)
    tokens = [importer.put_token(uid, {"n": uid}) for uid in (1, 2, 3, 4)]
    assert len(importer._CACHE) == 3
    assert tokens[0] not in importer._CACHE
    assert all(t in importer._CACHE for t in tokens[1:])
    importer.clear_tokens()
