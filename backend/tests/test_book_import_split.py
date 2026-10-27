"""拆书导入的纯函数：切章规则（强规则 / 分卷 / 序章 / 目录区 / 兜底 / docx / epub / 自定义正则）、校正操作、提示，
以及反推任务的合并（merge）、装批与跳过。"""

import multiprocessing

import pytest

from app.core.errors import BadRequestError
from app.services import kb_parser
from app.services.book_import import merge, reverse, splitter
from app.services.book_import.splitter import apply_op, chapter_views, classify, cn_to_int, split_auto
from tests.kb_fixtures import make_docx, make_epub

BODY = "　　他走进雨夜，四下无人，只有远处的灯火在风中摇晃。\n" * 14  # 336 字


def _text(raw: str) -> str:
    return kb_parser.clean_text(raw)


def _brief(text: str, chapters: list[dict]) -> list[tuple]:
    return [(c["title"], c["volume_no"], c["volume_title"]) for c in chapters]


def _body(text: str, chapter: dict) -> str:
    return splitter.body_text(text, chapter["segments"])


# ---------------- 标题识别 ----------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("第一章 雨夜", ("第一章 雨夜", None, 1)),
        ("第12章：重逢", ("第12章：重逢", None, 12)),
        ("第１２回 夜奔", ("第１２回 夜奔", None, 12)),
        ("第一百零三节", ("第一百零三节", None, 103)),
        ("第三章雨夜", ("第三章雨夜", None, 3)),
        ("Chapter 7 The Storm", ("Chapter 7 The Storm", None, 7)),
        ("chap. 8", ("chap. 8", None, 8)),
        ("序章", ("序章", None, None)),
        ("楔子：旧梦", ("楔子：旧梦", None, None)),
        ("尾声", ("尾声", None, None)),
        ("番外一 春日", ("番外一 春日", None, None)),
        ("第一卷 风起", (None, "第一卷 风起", None)),
        ("第二部", (None, "第二部", None)),
        ("第一卷 第一章 雨夜", ("第一章 雨夜", "第一卷", 1)),
    ],
)
def test_classify_strong_rules(line, expected):
    assert classify(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "“第一章写完了吗？”",
        "第一节课下课后，他走出教室。",
        "他说：第三回合开始。",
        "第一卷风起",  # 卷号后没有分隔符
        "第一部分内容",
        "第一章 " + "很" * 60,  # 超过 50 字
        "后记得带伞",
        "序章写完了，他松了口气。",
        "第三回合，他终于出手。",
    ],
)
def test_classify_rejects_dialogue_and_sentences(line):
    assert classify(line) is None


def test_cn_to_int():
    assert [cn_to_int(x) for x in ("一千二百三十四", "十二", "十", "一〇三", "两万零五", "１２", "百")] == [
        1234,
        12,
        10,
        103,
        20005,
        12,
        100,
    ]
    assert cn_to_int("十卷") is None


# ---------------- 自动切章 ----------------


def test_volumes_prologue_and_dialogue_lines():
    text = _text(
        "引言" * 120  # 240 字的序章，位于第一个卷之前
        + "\n\n第一卷 风起\n\n第1章 雨夜\n"
        + BODY
        + "“第一节课下课了吗？”\n第一节课下课后，他走出教室。\n\n第2章 晨光\n"
        + BODY
        + "\n第二卷 云涌\n第1章 远行\n"
        + BODY
        + "\n第三卷 第一章 归来\n"
        + BODY
    )
    chapters, notices = split_auto("txt", text)
    assert notices == []
    assert _brief(text, chapters) == [
        ("序章", 0, ""),
        ("第1章 雨夜", 1, "第一卷 风起"),
        ("第2章 晨光", 1, "第一卷 风起"),
        ("第1章 远行", 2, "第二卷 云涌"),
        ("第一章 归来", 3, "第三卷"),
    ]
    # 对话体短行留在正文里
    assert "第一节课下课后，他走出教室。" in _body(text, chapters[1])
    assert _body(text, chapters[1]).startswith("　　他走进雨夜")
    assert [c["source_no"] for c in chapters] == [None, 1, 2, 1, 1]


def test_prologue_threshold():
    short = _text("前言" * 99 + "\n\n第一章 开始\n" + BODY)  # 198 字
    assert [c["title"] for c in split_auto("txt", short)[0]] == ["第一章 开始"]
    # 边界：199 字丢弃、200 字保留
    edge = _text("前言" * 99 + "序\n\n第一章 开始\n" + BODY)  # 199 字
    assert [c["title"] for c in split_auto("txt", edge)[0]] == ["第一章 开始"]
    long = _text("前言" * 100 + "\n\n第一章 开始\n" + BODY)  # 200 字
    chapters = split_auto("txt", long)[0]
    assert [c["title"] for c in chapters] == ["序章", "第一章 开始"]
    # 没有任何分卷：全部为第 1 卷、卷名为空
    assert {(c["volume_no"], c["volume_title"]) for c in chapters} == {(1, "")}


def test_toc_region_removed():
    toc = "目录\n" + "\n".join(f"第{i}章 标题{i}" for i in range(1, 7))
    text = _text("某书\n作者：某人\n\n" + toc + "\n\n第1章 标题1\n" + BODY + "\n第2章 标题2\n" + BODY)
    chapters, notices = split_auto("txt", text)
    assert [c["title"] for c in chapters] == ["第1章 标题1", "第2章 标题2"]
    assert notices == ["已去除目录区 7 行"]
    # 恰好 5 个目录项：当作目录区
    toc5 = "\n".join(f"第{i}章 标题{i}" for i in range(1, 6))
    text5 = _text(toc5 + "\n\n第1章 标题1\n" + BODY)
    chapters5, notices5 = split_auto("txt", text5)
    assert [c["title"] for c in chapters5] == ["第1章 标题1"] and notices5 == ["已去除目录区 6 行"]
    # 目录项之间夹着不到 50 字的文字（如页码、短简介）仍是目录区；正文达到 50 字的标题是正文的开始
    toc_notes = "\n".join(f"第{i}章 标题{i}\n" + "简" * 45 for i in range(1, 6))
    text_notes = _text(toc_notes + "\n\n第1章 标题1\n" + "正" * 50 + "\n第2章 标题2\n" + BODY)
    chapters_n, notices_n = split_auto("txt", text_notes)
    assert [c["title"] for c in chapters_n] == ["第1章 标题1", "第2章 标题2"] and len(notices_n) == 1
    # 只有 4 个目录项：不当作目录区
    toc4 = "\n".join(f"第{i}章 标题{i}" for i in range(1, 5))
    text4 = _text(toc4 + "\n\n第1章 标题1\n" + BODY)
    chapters4, notices4 = split_auto("txt", text4)
    assert len(chapters4) == 5 and notices4 == []


def test_toc_followed_by_intro_becomes_prologue():
    # 目录最后一项后面紧跟引言：该项标题在正文里再次出现，仍属目录；引言 ≥ 200 字保留为序章
    toc = "目录\n" + "\n".join(f"第{i}章 标题{i}" for i in range(1, 7))
    intro = "引" * 210
    text = _text("某书\n\n" + toc + "\n\n" + intro + "\n\n第1章 标题1\n" + BODY + "\n第6章 标题6\n" + BODY)
    chapters, notices = split_auto("txt", text)
    assert [c["title"] for c in chapters] == ["序章", "第1章 标题1", "第6章 标题6"]
    assert _body(text, chapters[0]) == intro
    assert notices == ["已去除目录区 6 行"]
    # 引言不足 200 字：丢弃，目录照样去掉
    short = _text("某书\n\n" + toc + "\n\n" + "引" * 120 + "\n\n第1章 标题1\n" + BODY + "\n第6章 标题6\n" + BODY)
    chapters_s, _ = split_auto("txt", short)
    assert [c["title"] for c in chapters_s] == ["第1章 标题1", "第6章 标题6"]
    # 标题不在后文重复出现：不扩展，按正文章节处理
    other = _text(toc + "\n\n" + intro + "\n\n第7章 标题7\n" + BODY)
    chapters_o, _ = split_auto("txt", other)
    assert [c["title"] for c in chapters_o] == ["第6章 标题6", "第7章 标题7"]


def test_toc_keeps_volume_before_first_chapter():
    toc = "\n".join(["第一卷 风起", *(f"第{i}章 标题{i}" for i in range(1, 7))])
    text = _text(toc + "\n\n第一卷 风起\n第1章 标题1\n" + BODY)
    chapters, notices = split_auto("txt", text)
    assert _brief(text, chapters) == [("第1章 标题1", 1, "第一卷 风起")]
    assert notices and notices[0].startswith("已去除目录区")


def test_fallback_chunks_at_sentence_end():
    text = "这是一句很长的话。" * 2000  # 18000 字
    chapters, notices = split_auto("txt", text)
    assert notices == ["没有识别到章节标题，已按 3000–5000 字分段"]
    assert [c["title"] for c in chapters[:2]] == ["第 1 段", "第 2 段"]
    for c in chapters[:-1]:
        body = _body(text, c)
        assert 3000 <= len(body) <= 5000 and body.endswith("。")
    assert "".join(_body(text, c) for c in chapters) == text
    # 找不到句末：5000 字处硬切
    hard = "字" * 12000
    assert [len(_body(hard, c)) for c in split_auto("txt", hard)[0]] == [5000, 5000, 2000]


def test_markdown_headings():
    text = _text("# 第一章 雨夜\n\n" + BODY + "\n# 第二章 晨光\n\n" + BODY)
    assert [c["title"] for c in split_auto("md", text)[0]] == ["第一章 雨夜", "第二章 晨光"]


def test_docx_heading_levels_and_strong_rules():
    styles = {"H1": "heading 1", "H2": "heading 2", "T": "Title"}
    level = make_docx(
        [
            ("T", "书名"),
            ("H1", "第一卷 风起"),
            ("H2", "雨夜"),
            (None, BODY),
            ("H2", "晨光"),
            (None, BODY),
            ("H1", "第二卷 云涌"),
            ("H2", "远行"),
            (None, BODY),
        ],
        styles=styles,
    )
    parsed = splitter.parse_book("书.docx", level)
    assert parsed.kind == "docx" and parsed.book_title == "书"
    assert _brief(parsed.text, parsed.chapters) == [
        ("雨夜", 1, "第一卷 风起"),
        ("晨光", 1, "第一卷 风起"),
        ("远行", 2, "第二卷 云涌"),
    ]
    strong = make_docx([("H1", "第一章 雨夜"), (None, BODY), ("H1", "第二章 晨光"), (None, BODY)], styles=styles)
    assert [c["title"] for c in splitter.parse_book("b.docx", strong).chapters] == ["第一章 雨夜", "第二章 晨光"]


def _epub_with_title(chapters: list[tuple[str, str]], title: str) -> bytes:
    import io
    import zipfile

    src = zipfile.ZipFile(io.BytesIO(make_epub(chapters)))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith("content.opf"):
                data = data.replace(
                    b"<manifest>",
                    f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title></metadata>'
                    "<manifest>".encode(),
                )
            out.writestr(info, data)
    return buf.getvalue()


def test_epub_documents():
    body = "<p>" + BODY.replace("\n", "</p><p>") + "</p>"
    data = _epub_with_title(
        [
            ("cover.xhtml", "<p>封面</p>"),
            ("vol1.xhtml", "<h1>第一卷 风起</h1>"),
            ("c1.xhtml", f"<h1>雨夜</h1>{body}"),
            ("c2.xhtml", f"<h2>第二章 晨光</h2>{body}<h2>第三章 远行</h2>{body}"),
            ("toc.xhtml", "".join(f"<p>第{i}章 标题{i}标题{i}标题{i}</p>" for i in range(1, 9))),
            ("c4.xhtml", f"<p>没有标题的一章</p>{body}"),
        ],
        "星海纪元",
    )
    parsed = splitter.parse_book("x.epub", data)
    assert parsed.book_title == "星海纪元"
    assert _brief(parsed.text, parsed.chapters) == [
        ("雨夜", 1, "第一卷 风起"),
        ("第二章 晨光", 1, "第一卷 风起"),
        ("第三章 远行", 1, "第一卷 风起"),
        ("没有标题的一章", 1, "第一卷 风起"),
    ]
    assert parsed.notices == ["跳过 1 个过短的文档（封面、版权页等）", "跳过 1 个目录文档"]
    assert not _body(parsed.text, parsed.chapters[0]).startswith("雨夜")
    assert len(parsed.doc_spans) == 6
    # 自动重切按文档切，结果相同
    assert split_auto("epub", parsed.text, parsed.doc_spans)[0] == parsed.chapters


def test_parse_book_encodings_and_rejections():
    text = "第一章 天下大勢\n" + "話說天下大勢，分久必合，合久必分。" * 20
    big5 = splitter.parse_book("三國.txt", text.encode("big5"))
    assert big5.chapters[0]["title"] == "第一章 天下大勢" and "分久必合" in big5.text
    gbk = splitter.parse_book("书.txt", text.replace("勢", "势").replace("話說", "话说").encode("gbk"))
    assert gbk.chapters[0]["title"] == "第一章 天下大势"
    for name in ("a.pdf", "a.json", "a.doc"):
        with pytest.raises(BadRequestError) as exc:
            splitter.parse_book(name, b"x")
        assert exc.value.detail == splitter.UNSUPPORTED_TYPE


def test_text_over_limit_rejected(monkeypatch):
    monkeypatch.setattr(splitter, "MAX_TEXT_CHARS", 100)
    with pytest.raises(BadRequestError) as exc:
        splitter.parse_book("a.txt", ("第一章 x\n" + "字" * 200).encode())
    assert "无法导入" in exc.value.detail


# ---------------- 自定义正则 ----------------


def test_regex_validation():
    for pattern, message in (
        ("", "请填写"),
        ("a" * 101, "不能超过 100"),
        ("(a+)+$", "嵌套量词"),
        ("((ab)*c){2}", "嵌套量词"),
        ("([第]", "不合法"),
    ):
        with pytest.raises(BadRequestError) as exc:
            splitter.validate_pattern(pattern)
        assert message in exc.value.detail
    assert splitter.validate_pattern(r"^卷\d+·(.+)$") == r"^卷\d+·(.+)$"
    assert not splitter.has_nested_quantifier(r"[(+)]+\d+")


def test_regex_split_and_errors():
    text = _text(
        "引子" * 110 + "\n\n【一】雨夜\n" + BODY + "\n【二】晨光\n" + BODY + "\n" + "【三】" + "长" * 70 + "\n" + BODY
    )
    chapters, _ = splitter.split_regex("txt", text, r"^【.】")
    # 超过 60 字的行不参与匹配；序章规则照常适用
    assert [c["title"] for c in chapters] == ["序章", "【一】雨夜", "【二】晨光"]
    with pytest.raises(BadRequestError) as exc:
        splitter.split_regex("txt", text, r"^不存在的标题$")
    assert "没有匹配到任何标题" in exc.value.detail


def test_regex_timeout_kills_worker():
    text = "a" * 59 + "b\n\n正文"
    with pytest.raises(BadRequestError) as exc:
        splitter.split_regex("txt", text, r"^(a|aa)+$", timeout_seconds=0.5)
    assert "超时" in exc.value.detail
    # 超时的子进程已被终止并回收：没有遗留的子进程（也就没有僵尸进程）
    assert multiprocessing.active_children() == []


def test_regex_reap_kills_process_ignoring_sigterm(monkeypatch):
    """SIGTERM 后 1 秒内没退出的子进程再 SIGKILL，回收后释放进程对象。"""
    calls: list[str] = []

    class Stubborn:
        alive = True

        def is_alive(self):
            return self.alive

        def terminate(self):
            calls.append("terminate")

        def kill(self):
            calls.append("kill")
            self.alive = False

        def join(self, _timeout=None):
            calls.append("join")

        def close(self):
            calls.append("close")

    splitter._reap(Stubborn())
    assert calls == ["terminate", "join", "kill", "join", "close"]


# ---------------- 校正操作 ----------------


def _sample() -> tuple[str, list[dict]]:
    text = _text(
        "第一卷 风起\n第1章 雨夜\n第一行\n第二行\n\n第三行\n第2章 晨光\n"
        + BODY
        + "第二卷 云涌\n第3章 远行\n"
        + BODY
        + "第4章 归来\n"
        + BODY
    )
    return text, split_auto("txt", text)[0]


def test_ops_rename_merge_split_delete():
    text, chapters = _sample()
    renamed = apply_op(text, chapters, "rename", 1, title="  第1章 夜雨  ")
    assert renamed[0]["title"] == "第1章 夜雨" and renamed[0]["source_no"] == 1
    assert chapters[0]["title"] == "第1章 雨夜"  # 不修改原列表

    merged = apply_op(text, chapters, "merge_next", 1)
    assert len(merged) == 3 and merged[0]["title"] == "第1章 雨夜"
    assert _body(text, merged[0]) == _body(text, chapters[0]) + "\n\n" + _body(text, chapters[1])
    lines = [ln for ln, _ in splitter.chapter_lines(text, merged[0]["segments"])]
    assert "" in lines  # 拼接处的空行

    # 在合并后的章里按行拆分：拆出的新章标题取该行，该行留在正文里
    at = lines.index("第三行") + 1
    split = apply_op(text, merged, "split", 1, line=at)
    assert [c["title"] for c in split[:2]] == ["第1章 雨夜", "第三行"]
    assert _body(text, split[0]) == "第一行\n第二行"
    assert _body(text, split[1]).startswith("第三行\n\n")
    # 在拼接处的第一行拆分：正好恢复为合并前
    seam = lines.index("第三行") + 3  # 第三行之后是拼接处的空行，再下一行是原第 2 章的第一行
    assert [(c["title"], c["segments"]) for c in apply_op(text, merged, "split", 1, line=seam)][:2] == [
        (chapters[0]["title"], chapters[0]["segments"]),
        (lines[seam - 1].strip(), chapters[1]["segments"]),
    ]
    for line, message in ((1, "不能是第 1 行"), (3, "不能在空行处拆分"), (999, "超出范围")):
        with pytest.raises(BadRequestError) as exc:
            apply_op(text, chapters, "split", 1, line=line)
        assert message in exc.value.detail

    deleted = apply_op(text, chapters, "delete", 2)
    assert [c["title"] for c in deleted] == ["第1章 雨夜", "第3章 远行", "第4章 归来"]
    with pytest.raises(BadRequestError):
        apply_op(text, chapters[:1], "delete", 1)
    with pytest.raises(BadRequestError):
        apply_op(text, chapters, "merge_next", 4)
    with pytest.raises(BadRequestError):
        apply_op(text, chapters, "rename", 5, title="x")
    with pytest.raises(BadRequestError):
        apply_op(text, chapters, "rename", 1, title="  ")


def test_ops_volumes():
    text, chapters = _sample()
    assert [(c["volume_no"], c["volume_title"]) for c in chapters] == [
        (1, "第一卷 风起"),
        (1, "第一卷 风起"),
        (2, "第二卷 云涌"),
        (2, "第二卷 云涌"),
    ]
    # 本章起新开一卷：到下一个卷起点为止，卷号重排
    started = apply_op(text, chapters, "start_volume", 2, title="插入卷")
    assert [(c["volume_no"], c["volume_title"]) for c in started] == [
        (1, "第一卷 风起"),
        (2, "插入卷"),
        (3, "第二卷 云涌"),
        (3, "第二卷 云涌"),
    ]
    renamed = apply_op(text, chapters, "rename_volume", 4, title="云涌卷")
    assert [c["volume_title"] for c in renamed] == ["第一卷 风起", "第一卷 风起", "云涌卷", "云涌卷"]
    # 取消分卷：并入上一卷
    cleared = apply_op(text, chapters, "clear_volume", 3)
    assert {(c["volume_no"], c["volume_title"]) for c in cleared} == {(1, "第一卷 风起")}
    # 第一卷取消分卷 → 无分卷（0）；后面的卷重新从 1 编号
    first = apply_op(text, chapters, "clear_volume", 1)
    assert [(c["volume_no"], c["volume_title"]) for c in first] == [
        (0, ""),
        (0, ""),
        (1, "第二卷 云涌"),
        (1, "第二卷 云涌"),
    ]
    with pytest.raises(BadRequestError) as exc:
        apply_op(text, first, "rename_volume", 1, title="x")
    assert exc.value.detail == "本章不在任何分卷中"
    # 全部取消 → 没有任何分卷：全部为 1、卷名为空
    none = apply_op(text, first, "clear_volume", 3)
    assert {(c["volume_no"], c["volume_title"]) for c in none} == {(1, "")}
    # 无分卷时新开一卷
    again = apply_op(text, none, "start_volume", 3, title="新卷")
    assert [(c["volume_no"], c["volume_title"]) for c in again] == [(0, ""), (0, ""), (1, "新卷"), (1, "新卷")]


def test_warnings():
    text = _text(
        "第1章 开始\n短\n第2章 雨夜\n"
        + "长" * 20001
        + "\n第4章 雨夜\n"
        + BODY
        + "第一卷 新卷\n第1章 重来\n"
        + BODY
        + "第2章 继续\n"
        + BODY
    )
    chapters, _ = split_auto("txt", text)
    views, counts = chapter_views(text, chapters)
    assert [v["warnings"] for v in views] == [["short"], ["long", "dup_title"], ["dup_title", "gap"], [], []]
    assert counts == {"short": 1, "long": 1, "dup_title": 2, "gap": 1}
    assert views[0]["first_line"] == "短" and views[0]["chars"] == 1 and views[0]["index"] == 1
    # 第 5 章是新卷的第 1 章（序号从 1 重新开始）：不算断档
    assert views[3]["volume_no"] == 1 and views[2]["volume_no"] == 0


# ---------------- 合并 ----------------


def test_merge_characters_by_alias():
    findings = merge.accumulate(
        merge.empty_findings(),
        {"characters": [{"name": "林远", "aliases": ["小林"], "role_type": "主角", "profile": "少年"}]},
    )
    findings = merge.accumulate(
        findings,
        {
            "characters": [
                {"name": "小林", "aliases": ["林师兄"], "personality": "沉稳坚毅"},
                {"name": "林 远", "aliases": [], "profile": "天剑宗外门弟子，后成为掌门"},
                {"name": "苏晴", "aliases": []},
            ]
        },
    )
    chars = {c["name"]: c for c in findings["characters"]}
    assert set(chars) == {"林远", "苏晴"}
    lin = chars["林远"]
    assert lin["aliases"] == ["小林", "林师兄"] and lin["count"] == 3
    assert lin["profile"] == "天剑宗外门弟子，后成为掌门" and lin["personality"] == "沉稳坚毅"
    assert lin["role_type"] == "主角"
    # 显示名取出现次数最多的姓名
    more = merge.merge_characters(
        [{"name": "小林", "aliases": ["林远"], "count": 3}, {"name": "林远", "aliases": [], "count": 1}]
    )
    assert more[0]["name"] == "小林" and more[0]["aliases"] == ["林远"]
    assert merge.known_characters_text(findings["characters"]) == "林远（小林、林师兄）\n苏晴"


def test_fill_final_restores_aliases():
    findings = merge.accumulate(
        merge.empty_findings(),
        {
            "characters": [
                {"name": "林远", "aliases": ["远哥", "林师兄"]},
                {"name": "苏晴", "aliases": ["晴儿", "小七"]},  # 各批把「小七」误并进苏晴
                {"name": "白鹤", "aliases": ["鹤老"]},
            ]
        },
    )
    final = reverse.normalize_final(
        {
            "project": {},
            "characters": [
                {"name": "林远", "role_type": "主角"},  # 模型漏了别名
                {"name": "苏晴", "aliases": ["晴儿"], "role_type": "重要配角"},
                {"name": "小七", "role_type": "配角"},  # 汇总把两人分开了
                {"name": "鹤老", "role_type": "配角"},  # 模型用了别名做姓名
            ],
        }
    )
    chars = {c["name"]: c["aliases"] for c in reverse.fill_final(final, findings)["characters"]}
    assert chars["林远"] == ["远哥", "林师兄"]
    # 合并组含另一个最终角色的姓名：有歧义，两人都保持模型输出
    assert chars["苏晴"] == ["晴儿"] and chars["小七"] == []
    assert chars["鹤老"] == ["白鹤"]


def test_merge_entities_and_cap(monkeypatch):
    findings = merge.accumulate(
        merge.empty_findings(),
        {
            "characters": [{"name": "林远", "aliases": ["小林"]}, {"name": "苏晴", "aliases": []}],
            "organizations": [{"name": "天剑宗", "org_type": "宗门", "description": "短", "members": []}],
            "world": [{"title": "北境", "category": "place", "content": "雪"}],
            "careers": [{"name": "剑修", "kind": "main", "description": "", "stages": [{"name": "练气"}]}],
            "relations": [{"a": "小林", "b": "苏晴", "role_a": "师兄", "role_b": "师妹", "category": "social"}],
        },
    )
    findings = merge.accumulate(
        findings,
        {
            "organizations": [{"name": "天剑宗", "description": "北境第一大宗", "members": [{"name": "林远"}]}],
            "world": [
                {"title": "北境", "category": "place", "content": "终年积雪的雪原"},
                {"title": "北境", "category": "history", "content": "北境之战"},
            ],
            "careers": [{"name": "剑修", "stages": [{"name": "筑基"}, {"name": "练气"}]}],
            "relations": [{"a": "苏晴", "b": "林远", "role_a": "朋友", "role_b": "朋友", "category": "social"}],
        },
    )
    org = findings["organizations"][0]
    assert org["description"] == "北境第一大宗" and org["org_type"] == "宗门" and org["count"] == 2
    assert org["members"] == [{"name": "林远", "position": ""}]
    assert [(w["category"], w["content"]) for w in findings["world"]] == [
        ("place", "终年积雪的雪原"),
        ("history", "北境之战"),
    ]
    assert [s["name"] for s in findings["careers"][0]["stages"]] == ["练气", "筑基"]
    # 关系两端映射到显示名，无序去重，保留首条
    assert [(r["a"], r["b"], r["role_a"], r["count"]) for r in findings["relations"]] == [("林远", "苏晴", "师兄", 2)]
    # 体积上限：丢弃出现次数最少的
    monkeypatch.setattr(merge, "MAX_ITEMS", 2)
    capped = merge.accumulate(
        {**merge.empty_findings(), "world": [{"title": "甲", "category": "place", "content": "", "count": 5}]},
        {"world": [{"title": "乙", "category": "place"}, {"title": "丙", "category": "place"}]},
    )
    assert [w["title"] for w in capped["world"]] == ["甲", "乙"]
    long = merge.accumulate(
        merge.empty_findings(), {"world": [{"title": "丁", "category": "rule", "content": "长" * 999}]}
    )
    assert len(long["world"][0]["content"]) == merge.TEXT_LIMIT


# ---------------- 装批与跳过 ----------------


def test_split_parts_and_plan_batches():
    content = "一句话。" * 2600  # 10400 字
    parts = reverse.split_parts(content, 4000)
    assert "".join(parts) == content and len(parts) == 3
    assert all(len(p) <= 4000 and p.endswith("。") for p in parts)
    assert reverse.split_parts("短", 4000) == ["短"]
    batches = reverse.plan_batches([(1, 1, [1500]), (2, 2, [1500]), (3, 3, [3900, 3900]), (4, 4, [100])], 4000)
    assert batches == [[[1, 0, 1, 1], [2, 0, 1, 2]], [[3, 0, 2, 3]], [[3, 1, 2, 3], [4, 0, 1, 4]]]


def test_apply_skip():
    cp = reverse.new_checkpoint([[[1, 0, 1, 1], [2, 0, 2, 2]], [[2, 1, 2, 2]]])
    cp["pause_reason"] = {"reason": "failed", "phase": "digest", "batch_index": 0, "error": "额度不足"}
    assert reverse.apply_skip(cp, 0) == 1
    assert cp["batches"][0]["state"] == "skipped" and cp["batches"][0]["error"] == "额度不足"
    assert cp["skipped"] == [{"stage": "digest", "batch_index": 0, "chapter_nos": [1, 2]}]
    assert cp["part_summaries"] == {"2": {"0": ""}} and cp["pause_reason"] is None
    cp["phase"] = "synthesize"
    cp["findings"]["characters"] = [{"name": "林远", "aliases": [], "count": 2}]
    cp["pause_reason"] = {"reason": "failed", "phase": "synthesize"}
    assert reverse.apply_skip(cp, 2) == 2
    assert cp["phase"] == "apply" and [c["name"] for c in cp["final"]["characters"]] == ["林远"]
    assert cp["final"]["project"]["synopsis"] == ""
    summary = reverse.summarize(cp)
    assert summary["batches_skipped"] == 1 and summary["batches_total"] == 2 and "findings" not in summary


def test_sample_summaries_and_style_name():
    lines = [f"第{i}章：" + "摘" * 98 for i in range(1, 301)]
    text = reverse.sample_summaries(lines, 5000)
    kept = text.split("\n")
    assert len(text) <= 5000 and kept[:10] == lines[:10] and kept[-10:] == lines[-10:]
    assert reverse.style_name("星海", set()) == "《星海》原文风格"
    assert reverse.style_name("星海", {"《星海》原文风格", "《星海》原文风格（2）"}) == "《星海》原文风格（3）"
    assert len(reverse.style_name("长" * 300, set())) == reverse.STYLE_NAME_LIMIT
