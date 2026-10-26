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
    User,
)
from app.services.chapter_numbering import CHAPTER_NO_REFS, shift_chapter_numbers


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
    assert len(registered) == 11


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
    ids: dict[str, list[int]] = {k: [] for k in ("chapter", "outline", "analysis", "change", "rh", "ch", "fh", "fs")}
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
        db.add(fh)
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
