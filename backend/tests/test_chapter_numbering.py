"""章节号平移：登记表完整（反射）、每个登记字段都平移、边界、负向平移、其他项目不受影响。"""

from sqlalchemy import Integer, select

from app.models import (
    AnalysisChange,
    Base,
    CareerHistory,
    Chapter,
    ChapterAnalysis,
    Character,
    Foreshadow,
    ForeshadowHistory,
    Outline,
    Project,
    Relationship,
    RelationshipHistory,
    RelationshipType,
    RetrievalChunk,
    SettingProgression,
    SettingTimeline,
    StoryMemory,
    User,
    WorldSetting,
)
from app.schemas.chapter import ChapterUpdate
from app.services.chapter_numbering import CHAPTER_NO_REFS, shift_chapter_numbers
from app.services.chapter_service import delete_chapter, update_chapter


def test_all_chapter_no_columns_registered():
    """所有名为 chapter_no 或以 _chapter 结尾的 Integer 列都必须登记在 CHAPTER_NO_REFS（新增字段漏登记即失败）。"""
    found = {
        (table.name, col.name)
        for table in Base.metadata.tables.values()
        for col in table.columns
        if isinstance(col.type, Integer) and (col.name == "chapter_no" or col.name.endswith("_chapter"))
    }
    registered = {ref.key for ref in CHAPTER_NO_REFS}
    assert found - registered == set()
    assert registered - found == set()
    assert len(registered) == 14


async def _seed(db, user_id: int, title: str, nos: tuple[int, ...]) -> dict:
    """每张登记表各写几行，章节号取 nos（含 None 的字段写 None）。返回各对象 id。"""
    p = Project(user_id=user_id, title=title)
    db.add(p)
    await db.flush()
    a = Character(project_id=p.id, name="甲")
    b = Character(project_id=p.id, name="乙")
    db.add_all([a, b])
    await db.flush()
    type_id = await db.scalar(select(RelationshipType.id).where(RelationshipType.project_id.is_(None)).limit(1))
    rel = Relationship(
        project_id=p.id, a_id=a.id, b_id=b.id, pair_low=min(a.id, b.id), pair_high=max(a.id, b.id), type_id=type_id
    )
    db.add(rel)
    await db.flush()
    ids: dict[str, list[int]] = {
        k: [] for k in ("chapter", "outline", "analysis", "change", "rh", "ch", "fh", "fs", "mem", "rc")
    }
    fs_null = Foreshadow(project_id=p.id, title="空")
    db.add(fs_null)
    for no in nos:
        ch = Chapter(project_id=p.id, chapter_no=no, title=f"c{no}")
        o = Outline(project_id=p.id, chapter_no=no, title=f"o{no}")
        db.add_all([ch, o])
        await db.flush()
        an = ChapterAnalysis(project_id=p.id, chapter_id=ch.id, content_hash="x", chapter_no=no)
        db.add(an)
        await db.flush()
        change = AnalysisChange(
            analysis_id=an.id, project_id=p.id, chapter_no=no, kind="character_state", status="applied"
        )
        rh = RelationshipHistory(relationship_id=rel.id, chapter_no=no, source="manual", changes={})
        chh = CareerHistory(character_id=a.id, source="manual", chapter_no=no, changes={})
        fs = Foreshadow(
            project_id=p.id,
            title=f"f{no}",
            planned_plant_chapter=no,
            actual_plant_chapter=no,
            planned_resolve_chapter=no,
            actual_resolve_chapter=no,
        )
        db.add_all([change, rh, chh, fs])
        await db.flush()
        fh = ForeshadowHistory(foreshadow_id=fs.id, source="manual", chapter_no=no, changes={})
        mem = StoryMemory(project_id=p.id, chapter_id=ch.id, chapter_no=no, kind="summary", content=f"m{no}")
        db.add_all([fh, mem])
        await db.flush()
        rc = RetrievalChunk(project_id=p.id, source="memory", ref_id=mem.id, text=f"m{no}", tokens={}, chapter_no=no)
        db.add(rc)
        await db.flush()
        for key, obj in (
            ("chapter", ch),
            ("outline", o),
            ("analysis", an),
            ("change", change),
            ("rh", rh),
            ("ch", chh),
            ("fh", fh),
            ("fs", fs),
            ("mem", mem),
            ("rc", rc),
        ):
            ids[key].append(obj.id)
    await db.flush()
    return {"project_id": p.id, "fs_null": fs_null.id, **ids}


async def _values(db, seeded: dict) -> dict[str, list]:
    async def col(model, column, key):
        rows = await db.execute(select(getattr(model, column)).where(model.id.in_(seeded[key])).order_by(model.id))
        return [r[0] for r in rows.all()]

    out = {
        "chapters": await col(Chapter, "chapter_no", "chapter"),
        "outlines": await col(Outline, "chapter_no", "outline"),
        "analyses": await col(ChapterAnalysis, "chapter_no", "analysis"),
        "changes": await col(AnalysisChange, "chapter_no", "change"),
        "relationship_history": await col(RelationshipHistory, "chapter_no", "rh"),
        "career_history": await col(CareerHistory, "chapter_no", "ch"),
        "foreshadow_history": await col(ForeshadowHistory, "chapter_no", "fh"),
        "story_memories": await col(StoryMemory, "chapter_no", "mem"),
        "retrieval_chunks": await col(RetrievalChunk, "chapter_no", "rc"),
    }
    for f in ("planned_plant_chapter", "actual_plant_chapter", "planned_resolve_chapter", "actual_resolve_chapter"):
        out[f] = await col(Foreshadow, f, "fs")
    null_fs = await db.get(Foreshadow, seeded["fs_null"])
    out["null"] = [null_fs.planned_resolve_chapter, null_fs.actual_plant_chapter]
    return out


async def test_shift_every_registered_field_and_boundaries(app):
    async with app.state.sessionmaker() as db:
        user = User(username="u", password_hash="x")
        db.add(user)
        await db.flush()
        mine = await _seed(db, user.id, "甲", (1, 2, 3, 4))
        other = await _seed(db, user.id, "乙", (1, 2, 3, 4))
        await db.commit()

        await shift_chapter_numbers(db, mine["project_id"], from_no=3, delta=2)
        await db.commit()
        values = await _values(db, mine)
        for key, vals in values.items():
            if key == "null":
                assert vals == [None, None]
            else:
                # 边界：>= from_no 才平移
                assert vals == [1, 2, 5, 6], key
        # 其他项目不受影响
        for key, vals in (await _values(db, other)).items():
            if key != "null":
                assert vals == [1, 2, 3, 4], key

        # 负向平移（删除空章后）：>= 4 的减 1
        await shift_chapter_numbers(db, mine["project_id"], from_no=4, delta=-1)
        await db.commit()
        for key, vals in (await _values(db, mine)).items():
            if key != "null":
                assert vals == [1, 2, 4, 5], key

        # delta = 0 不做任何事
        await shift_chapter_numbers(db, mine["project_id"], from_no=1, delta=0)
        assert (await _values(db, mine))["chapters"] == [1, 2, 4, 5]


async def test_planned_progressions_shift_but_chapter_anchors_follow_identity(app):
    """计划章号按作品平移；已绑定章节跟随 ID，删除后不能因同号重建而恢复。"""
    async with app.state.sessionmaker() as db:
        user = User(username="positions", password_hash="x")
        db.add(user)
        await db.flush()
        mine = Project(user_id=user.id, title="甲")
        other = Project(user_id=user.id, title="乙")
        db.add_all([mine, other])
        await db.flush()
        mine_character = Character(project_id=mine.id, name="甲")
        other_character = Character(project_id=other.id, name="乙")
        mine_chapter = Chapter(project_id=mine.id, chapter_no=6, title="六")
        other_chapter = Chapter(project_id=other.id, chapter_no=6, title="六")
        db.add_all([mine_character, other_character, mine_chapter, other_chapter])
        await db.flush()
        mine_timeline = SettingTimeline(
            project_id=mine.id, subject_kind="character", character_id=mine_character.id,
            baseline={"current_state": "练气"},
        )
        other_timeline = SettingTimeline(
            project_id=other.id, subject_kind="character", character_id=other_character.id,
            baseline={"current_state": "练气"},
        )
        db.add_all([mine_timeline, other_timeline])
        await db.flush()
        planned = SettingProgression(
            timeline_id=mine_timeline.id, field_key="current_state", operation="replace", value="筑基",
            anchor_kind="planned", chapter_no=6,
        )
        linked = SettingProgression(
            timeline_id=mine_timeline.id, field_key="current_state", operation="replace", value="金丹",
            anchor_kind="chapter", chapter_id=mine_chapter.id, anchor_label="第6章 六",
        )
        already_orphaned = SettingProgression(
            timeline_id=mine_timeline.id, field_key="current_state", operation="replace", value="元婴",
            anchor_kind="chapter", chapter_id=None, anchor_label="已删除的第6章",
        )
        other_planned = SettingProgression(
            timeline_id=other_timeline.id, field_key="current_state", operation="replace", value="筑基",
            anchor_kind="planned", chapter_no=6,
        )
        db.add_all([planned, linked, already_orphaned, other_planned])
        await db.commit()

        await shift_chapter_numbers(db, mine.id, from_no=5, delta=3)
        await db.flush()
        await db.refresh(planned)
        await db.refresh(linked)
        await db.refresh(already_orphaned)
        await db.refresh(other_planned)
        await db.refresh(mine_chapter)
        await db.refresh(other_chapter)
        assert planned.chapter_no == 9
        assert linked.chapter_id == mine_chapter.id and linked.chapter_no is None
        assert mine_chapter.chapter_no == 9
        assert already_orphaned.chapter_id is None and already_orphaned.chapter_no is None
        assert other_planned.chapter_no == 6 and other_chapter.chapter_no == 6

        await shift_chapter_numbers(db, mine.id, from_no=7, delta=-1)
        await db.refresh(planned)
        await db.refresh(mine_chapter)
        await db.refresh(other_planned)
        assert planned.chapter_no == mine_chapter.chapter_no == 8
        assert other_planned.chapter_no == 6

        # 数据库对已删除章节设置 NULL；随后创建同号章节不能把孤儿自动关联过去。
        await db.delete(mine_chapter)
        await db.flush()
        await db.refresh(linked)
        assert linked.anchor_kind == "chapter" and linked.chapter_id is None
        assert linked.anchor_label == "第6章 六"
        replacement = Chapter(project_id=mine.id, chapter_no=8, title="新六")
        db.add(replacement)
        await db.flush()
        await db.refresh(linked)
        await db.refresh(planned)
        assert linked.chapter_id is None
        assert planned.anchor_kind == "planned" and planned.chapter_no == replacement.chapter_no


async def test_chapter_renumber_and_delete_recompute_latest_in_same_transaction(app):
    """调整既有章号改变事件先后；删章后同会话读到孤儿且最新值回退。"""
    async with app.state.sessionmaker() as db:
        user = User(username="renumber", password_hash="x")
        db.add(user)
        await db.flush()
        project = Project(user_id=user.id, title="世界")
        db.add(project)
        await db.flush()
        world = WorldSetting(project_id=project.id, title="城", content="战后")
        early = Chapter(project_id=project.id, chapter_no=3, title="战前")
        late = Chapter(project_id=project.id, chapter_no=9, title="战后")
        db.add_all([world, early, late])
        await db.flush()
        timeline = SettingTimeline(
            project_id=project.id, subject_kind="world", world_setting_id=world.id, baseline={"content": "初始"}
        )
        db.add(timeline)
        await db.flush()
        early_event = SettingProgression(
            timeline_id=timeline.id, field_key="content", operation="replace", value="战前",
            anchor_kind="chapter", chapter_id=early.id,
        )
        late_event = SettingProgression(
            timeline_id=timeline.id, field_key="content", operation="replace", value="战后",
            anchor_kind="chapter", chapter_id=late.id, anchor_label="第9章 战后",
        )
        db.add_all([early_event, late_event])
        await db.commit()
        user_id, project_id, late_id = user.id, project.id, late.id

        # 直接移动第9章到第1章后，第3章变化成为最新。
        await update_chapter(db, user_id, project_id, late_id, ChapterUpdate(chapter_no=1))
        assert world.content == "战前"
        assert late_event.chapter_id == late_id

        # 调回原位置后删章；事件对象在删除前已加载，不得使用缓存中的旧外键。
        await update_chapter(db, user_id, project_id, late_id, ChapterUpdate(chapter_no=9))
        assert world.content == "战后"
        await delete_chapter(db, user_id, project_id, late_id)
        assert late_event.anchor_kind == "chapter" and late_event.chapter_id is None
        assert world.content == "战前"

        replacement = Chapter(project_id=project_id, chapter_no=9, title="另一章")
        db.add(replacement)
        await db.flush()
        assert late_event.chapter_id is None and world.content == "战前"
