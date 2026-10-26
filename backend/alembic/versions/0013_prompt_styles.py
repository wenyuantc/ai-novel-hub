"""prompt styles

提示词模板与写作风格：新建 prompt_overrides（用户覆盖）/ prompt_override_versions（覆盖历史）/ writing_styles
（写作风格，user_id 为空 = 预设），projects 加 default_style_id、chapters 加 style_id（均 SET NULL）。
数据迁移：插入 6 个预设风格；把非空的 projects.style 迁移为项目所有者的一个自定义风格「《书名》原风格」
（description = 原文前 100 字，extra = 原文全文，上限 5000），并设为项目默认。projects.style 列保留、不再读写。
downgrade：把项目默认风格的 extra（为空时取 description）写回 projects.style（没有默认风格 → 空串），再删除新表新列。

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-27 10:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# 冻结的预设副本（与 app/models/style_presets.py 一致，由 test_0013_prompt_styles 断言）
PRESET_STYLES = ({'preset_key': 'lean_cold',
  'name': '冷峻简洁',
  'description': '克制冷静的白描，信息密度高，不渲染情绪',
  'pov': '第三人称有限视角，紧跟主角，不进入其他人物内心',
  'rhythm': '短句为主，一句一个动作；关键处用单句成段制造停顿',
  'dialogue': '对白偏少，每句都推进情节或暴露立场，不寒暄',
  'description_density': '低，只写对情节有用的细节，环境一两笔带过',
  'tone': '冷静、疏离，情绪藏在动作里',
  'diction': '动词准确具体，少用形容词和副词，不用成语堆砌',
  'taboos': '不写内心独白式的感叹，不用“仿佛”“不禁”“嘴角上扬”之类的套话',
  'sample': '',
  'extra': ''},
 {'preset_key': 'delicate_lyrical',
  'name': '细腻抒情',
  'description': '感官描写丰富，节奏舒缓，重人物情绪的层层铺陈',
  'pov': '第三人称有限视角，可以较多呈现主角的感受与回忆',
  'rhythm': '长短句交错，抒情段落允许长句铺陈，转折处收短',
  'dialogue': '对白适中，常有欲言又止的留白',
  'description_density': '高，调动视觉、听觉、气味、触感，以景写情',
  'tone': '温柔、怅惘，有余味',
  'diction': '用词讲究、有画面感，比喻贴近人物生活经验',
  'taboos': '不空洞抒情，不连用三个以上的排比，不用华丽却无指向的辞藻',
  'sample': '',
  'extra': ''},
 {'preset_key': 'fast_web',
  'name': '爽文快节奏',
  'description': '冲突密集、反馈及时的网文节奏，每章都有爽点',
  'pov': '第三人称有限视角，主角视角为主，必要时切到旁观者反应',
  'rhythm': '节奏快，段落短，三五百字内出现一次冲突或转折',
  'dialogue': '对白多，口语化，配角反应鲜明，用来烘托主角',
  'description_density': '低到中，战斗与关键场面写细，过场一笔带过',
  'tone': '昂扬、痛快',
  'diction': '直白有力，少用生僻字，节奏感强',
  'taboos': '不大段说明设定，不让主角长时间憋屈，不写与主线无关的支线',
  'sample': '',
  'extra': ''},
 {'preset_key': 'tense_suspense',
  'name': '悬疑紧张',
  'description': '信息有控制地释放，制造不安与悬念',
  'pov': '第三人称有限视角，读者只知道主角知道的信息',
  'rhythm': '平静处用中长句，紧张处骤然收短；章末停在悬念上',
  'dialogue': '对白克制，话里有话，关键信息常被打断或回避',
  'description_density': '中，突出异常的细节与声音，留出暗处',
  'tone': '压抑、不安',
  'diction': '精确、冷静，避免直接点破情绪',
  'taboos': '不提前揭示谜底，不用“他不知道的是”之类的全知旁白',
  'sample': '',
  'extra': ''},
 {'preset_key': 'light_humor',
  'name': '轻松幽默',
  'description': '语气俏皮，笑点来自人物性格与反差',
  'pov': '第三人称有限视角，叙述者语气可以带一点调侃',
  'rhythm': '轻快，善用短句抖包袱，铺垫与反转之间留一拍',
  'dialogue': '对白多，斗嘴、吐槽、答非所问',
  'description_density': '低到中，挑有喜感的细节写',
  'tone': '明快、温暖',
  'diction': '口语化，偶尔一本正经地说荒唐话',
  'taboos': '不堆砌网络流行梗，不靠贬低他人制造笑点',
  'sample': '',
  'extra': ''},
 {'preset_key': 'epic_classic',
  'name': '古典厚重',
  'description': '略带古意的叙述，格局宏大，适合仙侠、历史与史诗题材',
  'pov': '第三人称，可在宏观场面与人物视角之间切换',
  'rhythm': '句式整饬，偶用对仗，大场面长句铺排，人物对峙时收紧',
  'dialogue': '对白较少而有分量，人物说话符合身份与时代',
  'description_density': '中到高，山川、天象、器物写出气象',
  'tone': '庄重、苍茫',
  'diction': '半文半白，用词典雅，避免现代口语和外来词',
  'taboos': '不用现代网络用语，不让古人说出现代概念',
  'sample': '',
  'extra': ''})

STYLE_FIELDS = ("pov", "rhythm", "dialogue", "description_density", "tone", "diction", "taboos", "sample", "extra")


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "writing_styles",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("preset_key", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=200), server_default="", nullable=False),
        sa.Column("pov", sa.String(length=500), server_default="", nullable=False),
        sa.Column("rhythm", sa.String(length=500), server_default="", nullable=False),
        sa.Column("dialogue", sa.String(length=500), server_default="", nullable=False),
        sa.Column("description_density", sa.String(length=500), server_default="", nullable=False),
        sa.Column("tone", sa.String(length=500), server_default="", nullable=False),
        sa.Column("diction", sa.String(length=500), server_default="", nullable=False),
        sa.Column("taboos", sa.String(length=1000), server_default="", nullable=False),
        sa.Column("sample", sa.Text(), server_default="", nullable=False),
        sa.Column("extra", sa.Text(), server_default="", nullable=False),
        sa.Column("sort", sa.Integer(), server_default="0", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_writing_styles_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_writing_styles")),
        sa.UniqueConstraint("preset_key", name=op.f("uq_writing_styles_preset_key")),
    )
    with op.batch_alter_table("writing_styles", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_writing_styles_user_id"), ["user_id"], unique=False)

    op.create_table(
        "prompt_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("system", sa.Text(), server_default="", nullable=False),
        sa.Column("user", sa.Text(), server_default="", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("base_fingerprint", sa.String(length=16), server_default="", nullable=False),
        sa.Column("base_system", sa.Text(), server_default="", nullable=False),
        sa.Column("base_user", sa.Text(), server_default="", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_prompt_overrides_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_overrides")),
        sa.UniqueConstraint("user_id", "key", name=op.f("uq_prompt_overrides_user_id")),
    )
    with op.batch_alter_table("prompt_overrides", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_prompt_overrides_user_id"), ["user_id"], unique=False)

    op.create_table(
        "prompt_override_versions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("override_id", sa.Integer(), nullable=False),
        sa.Column("system", sa.Text(), server_default="", nullable=False),
        sa.Column("user", sa.Text(), server_default="", nullable=False),
        sa.Column("note", sa.String(length=200), server_default="", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["override_id"],
            ["prompt_overrides.id"],
            name=op.f("fk_prompt_override_versions_override_id_prompt_overrides"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompt_override_versions")),
    )
    with op.batch_alter_table("prompt_override_versions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_prompt_override_versions_override_id"), ["override_id"], unique=False)

    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(sa.Column("default_style_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_projects_default_style_id"), ["default_style_id"], unique=False)
        batch_op.create_foreign_key(
            batch_op.f("fk_projects_default_style_id_writing_styles"),
            "writing_styles",
            ["default_style_id"],
            ["id"],
            ondelete="SET NULL",
        )
    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.add_column(sa.Column("style_id", sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f("ix_chapters_style_id"), ["style_id"], unique=False)
        batch_op.create_foreign_key(
            batch_op.f("fk_chapters_style_id_writing_styles"), "writing_styles", ["style_id"], ["id"], ondelete="SET NULL"
        )

    styles = sa.table(
        "writing_styles",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("preset_key", sa.String),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
        *(sa.column(f, sa.String) for f in STYLE_FIELDS),
        sa.column("sort", sa.Integer),
    )
    bind = op.get_bind()
    bind.execute(styles.insert(), [{**p, "user_id": None, "sort": i} for i, p in enumerate(PRESET_STYLES, start=1)])

    rows = bind.execute(sa.text("SELECT id, user_id, title, style FROM projects ORDER BY id")).all()
    for project_id, user_id, title, style in rows:
        text = (style or "").strip()
        if not text:
            continue
        new_id = bind.execute(
            styles.insert().returning(styles.c.id),
            {
                "user_id": user_id,
                "preset_key": None,
                "name": f"《{title}》原风格"[:100],
                "description": text[:100],
                **{f: "" for f in STYLE_FIELDS},
                "extra": text[:5000],
                "sort": 0,
            },
        ).scalar_one()
        bind.execute(
            sa.text("UPDATE projects SET default_style_id = :sid WHERE id = :pid"), {"sid": new_id, "pid": project_id}
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT p.id, s.extra, s.description FROM projects p "
            "LEFT JOIN writing_styles s ON s.id = p.default_style_id"
        )
    ).all()
    for project_id, extra, description in rows:
        bind.execute(
            sa.text("UPDATE projects SET style = :style WHERE id = :pid"),
            {"style": (extra or description or "")[:5000], "pid": project_id},
        )

    with op.batch_alter_table("chapters", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_chapters_style_id_writing_styles"), type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_chapters_style_id"))
        batch_op.drop_column("style_id")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("fk_projects_default_style_id_writing_styles"), type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_projects_default_style_id"))
        batch_op.drop_column("default_style_id")

    with op.batch_alter_table("prompt_override_versions", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_prompt_override_versions_override_id"))
    op.drop_table("prompt_override_versions")
    with op.batch_alter_table("prompt_overrides", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_prompt_overrides_user_id"))
    op.drop_table("prompt_overrides")
    with op.batch_alter_table("writing_styles", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_writing_styles_user_id"))
    op.drop_table("writing_styles")
