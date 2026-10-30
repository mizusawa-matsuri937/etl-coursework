"""任务 6 的人物共现反查与评论年份提及（spec AC05、AC06）。

预期值按夹具手算，见 tests/fixtures/task6/README.md。测试先运行任务 4 的匹配得到映射版本，再逐阶段运行
任务 6（_task6_helpers.run_stages 与 T6-05 的 runner 执行同一组函数）。命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_cooccur.py
"""

from __future__ import annotations

import pytest
import _task6_helpers as h
from etl_task6.run import RunInputs

pytestmark = pytest.mark.integration


@pytest.fixture
def world(conn, make_settings, tmp_path):
    return h.make_world(conn, make_settings, tmp_path)


@pytest.fixture
def full(conn, world):
    return h.run_stages(conn, world, batch_size=7)


def _movie_of(conn, world, asin, ordinal=0):
    return conn.execute("SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND asin = %s "
                        "AND member_ordinal = %s", (world.mapping, asin, ordinal)).fetchone()[0]


def _sel(conn, rv, movie):
    return conn.execute("SELECT status, missing_reason, release_year FROM task6.release_selection "
                        "WHERE release_version_id = %s AND movie_id = %s", (rv, movie)).fetchone()


def _score(conn, world, movie, tconst):
    key = RunInputs(extraction_id=world.extraction_id, t4_mapping_version_id=world.mapping,
                    t5_person_mapping_version_id=world.pmap_with_hints,
                    imdb_snapshot_id=world.sid).stage_key("cooccur")
    return conn.execute("SELECT score, director_hits, actor_hits, title_ok, matched_title_key, matched_asin, "
                        "key_source, title_key_source_line, number_guard, number_guard_detail, passed "
                        "FROM task6.cooccur_score WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s "
                        "AND tconst = %s", (world.extraction_id, key, movie, tconst)).fetchone()


def _result(conn, world, movie):
    key = RunInputs(extraction_id=world.extraction_id, t4_mapping_version_id=world.mapping,
                    t5_person_mapping_version_id=world.pmap_with_hints,
                    imdb_snapshot_id=world.sid).stage_key("cooccur")
    return conn.execute("SELECT outcome, chosen_tconst, passed_tconsts, owner_movie_id FROM task6.cooccur_result "
                        "WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s",
                        (world.extraction_id, key, movie)).fetchone()


def _mentions(conn, world, movie):
    key = RunInputs(extraction_id=world.extraction_id, t4_mapping_version_id=world.mapping,
                    t5_person_mapping_version_id=world.pmap_with_hints,
                    imdb_snapshot_id=world.sid).stage_key("review_years")
    return {r[0]: r[1:] for r in conn.execute(
        "SELECT year, votes, source_review_ids, patterns FROM task6.review_year_mention WHERE extraction_id = %s "
        "AND stage_key = %s AND movie_id = %s", (world.extraction_id, key, movie))}


# ------------------------------------------------------------------ AC05 人物共现反查

def test_ac05_director_hit_unique_candidate(conn, world, full):
    """P1：页面导演命中 IMDb 作品导演（2 分）+ 标题实词子集 + 唯一 → imdb_t6_cooccur 候选。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6TRS001")
    row = _score(conn, world, movie, "tt0006020")
    assert row[0] == 2
    hits = row[1]
    assert hits == [{"page_key": "fletcher frame", "assertion_id": hits[0]["assertion_id"],
                     "asin": "B0T6TRS001", "nconst": "nm0006020", "crew_line_no": hits[0]["crew_line_no"]}]
    assert row[3] is True and row[4] == "treasure hunt island" and row[5] == "B0T6TRS001"
    assert row[6] in ("primary", "original") and row[7] == 9       # 官方标题键（primary 与 original 同值时取排序在前者）
    assert row[8] is True and row[10] is True
    result = _result(conn, world, movie)
    assert result == ("unique", "tt0006020", ["tt0006020"], None)
    sel = conn.execute("SELECT status, release_year, confidence FROM task6.release_selection "
                       "WHERE release_version_id = %s AND movie_id = %s", (rv, movie)).fetchone()
    assert sel[0] == "cooccur_matched" and sel[1] == 1950 and float(sel[2]) == pytest.approx(0.75)
    cand = conn.execute("SELECT source_key, value_year, flags, match_evidence FROM task6.release_candidate "
                        "WHERE extraction_id = %s AND movie_id = %s AND source = 'imdb_t6_cooccur'",
                        (world.extraction_id, movie)).fetchone()
    assert cand[0] == "tt0006020" and cand[1] == 1950 and cand[2] == []
    assert cand[3]["outcome"] == "unique" and cand[3]["review_year_used"] is False


def test_ac05_single_actor_score_too_low(conn, world, full):
    """P2：只有一位页面演员同名（1 分）→ 评分记录写明，无候选。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6SOL001")
    rows = conn.execute("SELECT tconst, score, passed FROM task6.cooccur_score WHERE extraction_id = %s "
                        "AND movie_id = %s", (world.extraction_id, movie)).fetchall()
    assert rows == [("tt0006024", 1, False)]                       # 只有演员命中：分数不足 2
    assert _result(conn, world, movie) == ("none", None, [], None)
    assert _sel(conn, rv, movie)[:2] == ("missing", "no_candidate")


def test_ac05_title_words_not_subset(conn, world, full):
    """P3：两位页面演员同名但 IMDb 标题实词不全在商品标题里 → title_ok 为假。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6DIS001")
    row = _score(conn, world, movie, "tt0006024")
    assert row[0] == 2 and row[3] is False and row[8] is None      # 标题未通过：编号未评估
    assert _sel(conn, rv, movie)[:2] == ("missing", "no_candidate")


def test_ac05_number_guard_excludes_sequel(conn, world, full):
    """P4：商品标题带编号而 IMDb 标题键没有同一编号 → number_guard 为假并写明编号。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6LND001")
    row = _score(conn, world, movie, "tt0006024")
    assert row[3] is True and row[8] is False
    assert "8" in row[9] and "商品编号" in row[9]
    assert _result(conn, world, movie) == ("none", None, [], None)
    assert _sel(conn, rv, movie)[:2] == ("missing", "no_candidate")


def test_ac05_ambiguous_two_titles(conn, world, full):
    """P5：导演命中两部标题都吻合的作品（Ambi One 1950 / 1972），评论无年份提及 → ambiguous。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6AM1001")
    result = _result(conn, world, movie)
    assert result[0] == "ambiguous" and result[1] is None
    assert sorted(result[2]) == ["tt0006030", "tt0006031"]
    assert conn.execute("SELECT count(*) FROM task6.release_candidate WHERE extraction_id = %s AND movie_id = %s "
                        "AND source = 'imdb_t6_cooccur'", (world.extraction_id, movie)).fetchone()[0] == 0
    assert _sel(conn, rv, movie)[:2] == ("missing", "cooccur_ambiguous")


def test_ac05_shared_tconst_with_owner_movie(conn, world, full):
    """P6：命中的作品已属于另一部作品（Beta Tale 属 B0T6B10001）→ 候选标 shared_tconst 并写质量问题。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6SHR001")
    owner = _movie_of(conn, world, "B0T6B10001")
    result = _result(conn, world, movie)
    assert result == ("unique", "tt0006002", ["tt0006002"], owner)
    sel = conn.execute("SELECT status, release_year FROM task6.release_selection WHERE release_version_id = %s "
                       "AND movie_id = %s", (rv, movie)).fetchone()
    assert sel == ("cooccur_matched", 2004)
    flags = conn.execute("SELECT flags, shared_with_movie_id FROM task6.release_candidate WHERE extraction_id = %s "
                         "AND movie_id = %s AND source = 'imdb_t6_cooccur'",
                         (world.extraction_id, movie)).fetchone()
    assert flags[0] == ["shared_tconst"] and flags[1] == owner
    issue = conn.execute("SELECT detail FROM task6.quality_issue WHERE issue_code = 'shared_tconst_with_movie' "
                         "AND entity_id = %s", (movie,)).fetchone()
    assert issue[0]["origin"] == "cooccur" and issue[0]["owner_movie_id"] == owner


def test_ac05_only_page_directors_and_actors_are_used(conn, world, full):
    """评分只用署名行的导演与演员断言；没有署名的作品（Cross Movie 的详情无）不参与评分。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6FLJ001")                    # 已关联 tconst：不评分
    assert conn.execute("SELECT count(*) FROM task6.cooccur_score WHERE extraction_id = %s AND movie_id = %s",
                        (world.extraction_id, movie)).fetchone()[0] == 0
    no_names = _movie_of(conn, world, "B0T6SPR001")                 # 无署名：没有评分记录
    assert conn.execute("SELECT count(*) FROM task6.cooccur_score WHERE extraction_id = %s AND movie_id = %s",
                        (world.extraction_id, no_names)).fetchone()[0] == 0


# ------------------------------------------------------------------ AC06 评论年份提及与消歧

def test_ac06_votes_and_future_year_excluded(conn, world, full):
    """每条评论每个年份一票；晚于评论年份的 (2003) 不计（A 作品 2001 年的评论）。"""
    ctx, rv = full
    a = _movie_of(conn, world, "B0T6A10001")                        # A10002 的评论归到 A 的作品
    mentions = _mentions(conn, world, a)
    assert sorted(mentions) == [2001]
    votes, review_ids, patterns = mentions[2001]
    assert votes == 2 and len(review_ids) == 2
    assert patterns == {"paren": 2}


def test_ac06_three_patterns_three_votes(conn, world, full):
    """三条评论分别命中 (1950)、year_word、made_in → 1950 三票与源评论 ID。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6RVT001")
    mentions = _mentions(conn, world, movie)
    assert sorted(mentions) == [1950]
    votes, review_ids, patterns = mentions[1950]
    assert votes == 3 and len(review_ids) == 3
    assert patterns == {"paren": 1, "year_word": 1, "made_in": 1}
    assert _sel(conn, rv, movie)[:2] == ("missing", "no_candidate")   # 只有提及，仍为 missing


def test_ac06_tiebreak_resolves_ambiguity(conn, world, full):
    """AM2001 两部 Ambi Two（1950/1972）都通过，评论两票 1950 唯一选出 1950 那部：review_year_tiebreak。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6AM2001")
    result = _result(conn, world, movie)
    assert result[0] == "resolved_by_review_year" and result[1] == "tt0006032"
    sel = conn.execute("SELECT status, release_year, confidence FROM task6.release_selection "
                       "WHERE release_version_id = %s AND movie_id = %s", (rv, movie)).fetchone()
    assert sel[0] == "cooccur_matched" and sel[1] == 1950
    assert float(sel[2]) == pytest.approx(0.70)                     # 经评论年份消歧的置信度
    cand = conn.execute("SELECT flags, match_evidence FROM task6.release_candidate WHERE extraction_id = %s "
                        "AND movie_id = %s AND source = 'imdb_t6_cooccur'",
                        (world.extraction_id, movie)).fetchone()
    assert cand[0] == ["review_year_tiebreak"]
    assert cand[1]["review_year_used"] is True


def test_ac06_tiebreak_needs_unique_top_year(conn, world, full):
    """并列最高或票数不足时不能消歧：AM1001 的评论没有年份提及，保持 ambiguous（P5 的另一半）。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6AM1001")
    assert _mentions(conn, world, movie) == {}
    assert _result(conn, world, movie)[0] == "ambiguous"
