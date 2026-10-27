"""检索基建：分词、BM25 归一化、混合打分、阈值、embedding_key 不一致、缓存失效、查询向量失败降级。"""

import numpy as np
import pytest
from sqlalchemy import select

from app.ai.providers import AIProviderError
from app.models import Project, RetrievalChunk, User
from app.services import retrieval_service as rs
from app.services.retrieval_service import ChunkInput, Embedder, SearchFilter
from tests.helpers import FakeLLM

DOCS = {
    1: "天剑宗位于北境雪山之巅，宗主林远山以剑道闻名。",
    2: "天剑宗的弟子每年冬至在雪山比剑，胜者可入藏经阁。",
    3: "东海龙宫掌管四海水族，龙王性情暴躁。",
    4: "藏经阁收藏天剑宗历代剑谱与心法。",
    5: "城南集市以售卖灵草闻名，每逢初一十五最热闹。",
}


@pytest.fixture
async def db(app):
    rs.invalidate()
    async with app.state.sessionmaker() as session:
        user = User(username="r", password_hash="x")
        session.add(user)
        await session.flush()
        project = Project(user_id=user.id, title="检索")
        session.add(project)
        await session.flush()
        session.info["project_id"] = project.id
        await rs.upsert_many(session, project.id, "kb", [ChunkInput(ref_id=k, text=v) for k, v in DOCS.items()])
        await session.commit()
        yield session
    rs.invalidate()


def _pid(db) -> int:
    return db.info["project_id"]


def _embedder(fake: FakeLLM, model: str = "emb-a") -> Embedder:
    return Embedder(provider=fake.factory("openai_compat", "k", None, 30), provider_name="openai_compat", model=model)


async def _embed_all(db, embedder: Embedder, ref_ids=None) -> None:
    rows = await rs.stale_chunks(db, _pid(db), embedder.model_key, ref_ids=ref_ids, limit=100)
    vectors = await embedder.embed([r.text for r in rows])
    await rs.store_embeddings(db, rows, vectors, embedder.key)
    await db.commit()


def test_tokenize_rules():
    assert rs.tokenize("林远山，在GPT-4的天剑宗！ 林 Hello") == [
        "林远",
        "远山",
        "gpt",
        "4",
        "的天",
        "天剑",
        "剑宗",
        "hello",
    ]
    # 单个中文字、纯标点不产生词；全角英文数字按半角处理
    assert rs.tokenize("林。！？") == []
    assert rs.tokenize("ＡＢＣ１２") == ["abc12"]


def test_vector_roundtrip_and_key():
    vec = rs.normalize_vector([3.0, 4.0])
    assert vec is not None and abs(float(np.linalg.norm(vec)) - 1) < 1e-6
    assert np.allclose(rs.decode_vector(rs.encode_vector(vec)), vec)
    assert rs.normalize_vector([0.0, 0.0]) is None
    assert rs.key_matches_model("openai_compat:nomic:latest:768", "openai_compat:nomic:latest")
    assert not rs.key_matches_model("openai_compat:a:768", "openai_compat:b")
    assert not rs.key_matches_model(None, "openai_compat:a")


async def test_keyword_scores_bounded_and_threshold(db):
    res = await rs.search(db, _pid(db), "kb", "天剑宗 雪山 剑道", top_k=10, min_score=0)
    assert res.mode == "keyword"
    scores = [h.score for h in res.hits]
    assert scores and all(0 <= s <= 1 for s in scores)
    assert scores[0] == 1.0 and scores == sorted(scores, reverse=True)
    assert all(h.vector_score is None for h in res.hits)
    # 完全不相关的文档（关键词分 0）不算命中
    assert 3 not in {h.ref_id for h in res.hits} and 5 not in {h.ref_id for h in res.hits}

    lowest = scores[-1]
    threshold = (lowest + scores[0]) / 2
    filtered = await rs.search(db, _pid(db), "kb", "天剑宗 雪山 剑道", top_k=10, min_score=threshold)
    assert filtered.hits and all(h.score >= threshold for h in filtered.hits)
    assert len(filtered.hits) < len(res.hits)
    # 阈值以上的结果与不设阈值时一致
    assert [h.ref_id for h in filtered.hits] == [h.ref_id for h in res.hits if h.score >= threshold]

    # 阈值高于所有分数：不返回任何结果
    none = await rs.search(db, _pid(db), "kb", "龙宫", top_k=10, min_score=1.01)
    assert none.hits == []


async def test_hybrid_scores_bounded_and_threshold(db):
    fake = FakeLLM()
    embedder = _embedder(fake)
    await _embed_all(db, embedder)
    res = await rs.search(db, _pid(db), "kb", "天剑宗弟子比剑", top_k=10, min_score=0, embedder=embedder)
    assert res.mode == "hybrid" and res.warnings == []
    assert len(res.hits) == len(DOCS)  # 混合时全部候选都有向量分
    for h in res.hits:
        assert 0 <= h.score <= 1 and 0 <= h.vector_score <= 1 and 0 <= h.keyword_score <= 1
        assert h.score == pytest.approx(0.7 * h.vector_score + 0.3 * h.keyword_score, abs=1e-3)
    threshold = res.hits[2].score
    cut = await rs.search(db, _pid(db), "kb", "天剑宗弟子比剑", top_k=10, min_score=threshold, embedder=embedder)
    assert [h.ref_id for h in cut.hits] == [h.ref_id for h in res.hits if h.score >= threshold]
    assert all(h.score >= threshold for h in cut.hits)
    # 查询向量命中缓存：同一 Embedder 重复查询不再调用接口
    calls = len(fake.embed_calls)
    await rs.search(db, _pid(db), "kb", "天剑宗弟子比剑", top_k=3, min_score=0, embedder=embedder)
    assert len(fake.embed_calls) == calls


async def test_no_embedder_keyword_only(db):
    fake = FakeLLM()
    await _embed_all(db, _embedder(fake))
    res = await rs.search(db, _pid(db), "kb", "藏经阁", top_k=5, min_score=0)
    assert res.mode == "keyword" and {h.ref_id for h in res.hits} == {2, 4}


async def test_embedding_key_mismatch_not_used(db):
    fake = FakeLLM()
    old = _embedder(fake, "emb-old")
    await _embed_all(db, old)
    new = _embedder(fake, "emb-new")
    calls = len(fake.embed_calls)
    res = await rs.search(db, _pid(db), "kb", "天剑宗", top_k=5, min_score=0, embedder=new)
    # 旧模型的向量不参与打分，也不为查询求向量
    assert res.mode == "keyword" and rs.KEYWORD_ONLY_STALE in res.warnings
    assert all(h.vector_score is None for h in res.hits)
    assert len(fake.embed_calls) == calls
    counts = await rs.stale_counts(db, _pid(db), new.model_key)
    assert counts["kb"] == len(DOCS)
    assert (await rs.stale_counts(db, _pid(db), old.model_key))["kb"] == 0

    # 部分重建：仍有候选缺少新向量 → 整次只用关键词，也不为查询求向量
    await _embed_all(db, new, ref_ids=[1, 2])
    calls = len(fake.embed_calls)
    res = await rs.search(db, _pid(db), "kb", "天剑宗", top_k=5, min_score=0, embedder=new)
    assert res.mode == "keyword" and rs.KEYWORD_ONLY_STALE in res.warnings
    assert len(fake.embed_calls) == calls
    # 重建完成 → 混合
    await _embed_all(db, new)
    res = await rs.search(db, _pid(db), "kb", "天剑宗", top_k=5, min_score=0, embedder=new)
    assert res.mode == "hybrid"

    # 同名模型但维度不同（换了接口）也视为不一致
    fake.embed_dim = 32
    other = _embedder(fake, "emb-new")
    res = await rs.search(db, _pid(db), "kb", "天剑宗", top_k=5, min_score=0, embedder=other)
    assert res.mode == "keyword" and rs.KEYWORD_ONLY_STALE in res.warnings
    # 同名模型、部分块已换成新维度：候选的向量维度混杂 → 仍只用关键词
    rows = list(await db.scalars(select(RetrievalChunk).where(RetrievalChunk.ref_id.in_([1, 2]))))
    await rs.store_embeddings(db, rows, await other.embed([r.text for r in rows]), other.key)
    await db.commit()
    res = await rs.search(db, _pid(db), "kb", "天剑宗", top_k=5, min_score=0, embedder=other)
    assert res.mode == "keyword" and rs.KEYWORD_ONLY_STALE in res.warnings
    assert all(h.vector_score is None for h in res.hits)


async def test_query_embedding_failure_falls_back(db):
    fake = FakeLLM()
    embedder = _embedder(fake)
    await _embed_all(db, embedder)
    fake.embed_error = AIProviderError("模型调用失败：额度不足")
    res = await rs.search(db, _pid(db), "kb", "龙宫龙王", top_k=5, min_score=0, embedder=_embedder(fake))
    assert res.mode == "keyword" and "额度不足" in res.warnings[0]
    assert [h.ref_id for h in res.hits] == [3]


async def test_cache_invalidation(db):
    pid = _pid(db)
    res = await rs.search(db, pid, "kb", "灵草", top_k=5, min_score=0)
    assert [h.ref_id for h in res.hits] == [5]
    cached = rs._CACHE[(pid, "kb")]
    await rs.search(db, pid, "kb", "灵草", top_k=5, min_score=0)
    assert rs._CACHE[(pid, "kb")] is cached  # 数据未变：复用

    # 改正文
    await rs.upsert_many(db, pid, "kb", [ChunkInput(ref_id=5, text="城南集市售卖兵器")])
    await db.commit()
    assert (await rs.search(db, pid, "kb", "灵草", top_k=5, min_score=0)).hits == []
    assert rs._CACHE[(pid, "kb")] is not cached

    # 关闭（屏蔽）
    await rs.set_flags(db, "kb", [3], enabled=False)
    await db.commit()
    assert (await rs.search(db, pid, "kb", "龙宫", top_k=5, min_score=0)).hits == []
    flt = SearchFilter(enabled_only=False)
    assert [h.ref_id for h in (await rs.search(db, pid, "kb", "龙宫", top_k=5, min_score=0, filters=flt)).hits] == [3]

    # 删除最大 id 后再插入（id 不复用，仍能感知新行）
    await rs.delete_refs(db, "kb", [5])
    await db.commit()
    await rs.search(db, pid, "kb", "兵器", top_k=5, min_score=0)
    await rs.upsert_many(db, pid, "kb", [ChunkInput(ref_id=6, text="城南集市售卖兵器")])
    await db.commit()
    assert [h.ref_id for h in (await rs.search(db, pid, "kb", "兵器", top_k=5, min_score=0)).hits] == [6]

    # 章节号平移（不经 retrieval_service）也会失效
    await rs.upsert_many(db, pid, "memory", [ChunkInput(ref_id=1, text="林远击败对手", chapter_no=3)])
    await db.commit()
    assert (await rs.search(db, pid, "memory", "林远", top_k=5, min_score=0, filters=SearchFilter(chapter_lt=4))).hits
    from app.services.chapter_numbering import shift_chapter_numbers

    await shift_chapter_numbers(db, pid, from_no=1, delta=2)
    await db.commit()
    flt = SearchFilter(chapter_lt=4)
    assert (await rs.search(db, pid, "memory", "林远", top_k=5, min_score=0, filters=flt)).hits == []


async def test_filters(db):
    pid = _pid(db)
    await rs.upsert_many(
        db,
        pid,
        "memory",
        [
            ChunkInput(ref_id=11, text="林远拜师", chapter_no=1),
            ChunkInput(ref_id=12, text="林远下山", chapter_no=2),
            ChunkInput(ref_id=13, text="林远归来", chapter_no=5, pinned=True),
        ],
    )
    await db.commit()

    async def refs(**kw) -> list[int]:
        res = await rs.search(db, pid, "memory", "林远", top_k=10, min_score=0, filters=SearchFilter(**kw))
        return sorted(h.ref_id for h in res.hits)

    assert await refs() == [11, 12, 13]
    assert await refs(chapter_lt=5) == [11, 12]
    assert await refs(exclude_chapter_nos={2}) == [11, 13]
    assert await refs(exclude_ref_ids={11}) == [12, 13]
    assert await refs(ref_ids={13}) == [13]
    # kb 与 memory 互不影响
    assert (await rs.search(db, pid, "kb", "拜师下山", top_k=10, min_score=0)).hits == []
    hit = (await rs.search(db, pid, "memory", "归来", top_k=1, min_score=0)).hits[0]
    assert hit.pinned is True and hit.chapter_no == 5


async def test_upsert_clears_embedding_on_text_change(db):
    fake = FakeLLM()
    embedder = _embedder(fake)
    await _embed_all(db, embedder)
    await rs.upsert_many(db, _pid(db), "kb", [ChunkInput(ref_id=1, text="改过的正文")])
    await rs.upsert_many(db, _pid(db), "kb", [ChunkInput(ref_id=2, text=DOCS[2], enabled=False)])
    await db.commit()
    rows = {r.ref_id: r for r in await db.scalars(select(RetrievalChunk))}
    assert rows[1].embedding is None and rows[1].embedding_key is None
    assert rows[2].embedding is not None and rows[2].enabled is False
    assert (await rs.stale_counts(db, _pid(db), embedder.model_key))["kb"] == 1


def test_normalize_vector_rejects_degenerate():
    assert rs.normalize_vector([]) is None
    assert rs.normalize_vector([float("nan"), 1.0]) is None
    assert rs.normalize_vector([float("inf"), 1.0]) is None
    assert rs.normalize_vector([[1.0, 2.0]]) is None


async def test_bm25_edge_cases(db):
    pid = _pid(db)
    # 查询没有任何可用词 / 全部候选关键词分为 0：不返回、不报错
    assert (await rs.search(db, pid, "kb", "。！", top_k=5, min_score=0)).hits == []
    assert (await rs.search(db, pid, "kb", "火星基地", top_k=5, min_score=0)).hits == []
    # 候选只有一条：分数仍有界且为 1
    res = await rs.search(db, pid, "kb", "灵草", top_k=5, min_score=0, filters=SearchFilter(ref_ids={5}))
    assert [(h.ref_id, h.score) for h in res.hits] == [(5, 1.0)]
    # top_k <= 0 不返回
    assert (await rs.search(db, pid, "kb", "天剑宗", top_k=0, min_score=0)).hits == []


async def test_corrupt_vector_not_used(db):
    """库里的向量字节数与 embedding_key 声明的维度不符：视为没有向量，整次查询只用关键词。"""
    fake = FakeLLM()
    embedder = _embedder(fake)
    await _embed_all(db, embedder)
    row = await db.scalar(select(RetrievalChunk).where(RetrievalChunk.ref_id == 1))
    row.embedding = row.embedding[:-4]
    row.version += 1
    await db.commit()
    res = await rs.search(db, _pid(db), "kb", "天剑宗", top_k=5, min_score=0, embedder=embedder)
    assert res.mode == "keyword" and rs.KEYWORD_ONLY_STALE in res.warnings
    assert res.hits and all(0 <= h.score <= 1 for h in res.hits)
