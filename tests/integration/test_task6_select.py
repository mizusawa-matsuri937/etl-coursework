"""任务 6 的择优、状态与日期语义分离（spec AC02、AC08）。

预期值按夹具手算，见 tests/fixtures/task6/README.md。命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_select.py
"""

from __future__ import annotations

import pytest
import _task6_helpers as h

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
    return conn.execute("SELECT status, release_year, release_date, precision, region, release_type, "
                        "regional_release_missing_reason FROM task6.release_selection WHERE release_version_id = %s "
                        "AND movie_id = %s", (rv, movie)).fetchone()


# ------------------------------------------------------------------ AC02 日期语义分离与精度

def test_ac02_semantics_and_precision(conn, world, full):
    """作品 A：IMDb 年份 1999 胜出，商品发售日期、评论时间与评论年份提及都不参与择优。"""
    ctx, rv = full
    a = _movie_of(conn, world, "B0T6A10001")
    row = _sel(conn, rv, a)
    assert row == ("verified", 1999, None, "year", None, "first_release", "no_permitted_dated_source")
    # A 有两个商品发售日期（2003-05-20、2001-01-01）、最早评论 1999-11-02、评论年份提及 2001（两票），
    # 都没有出现在年份候选或选择记录里（标题年份 "(1999)" 是合法的 amazon_title_year 候选）
    sources = {r[0] for r in conn.execute("SELECT source FROM task6.release_candidate WHERE extraction_id = %s "
                                          "AND movie_id = %s", (world.extraction_id, a))}
    assert sources == {"imdb_t4_link", "amazon_title_year"}
    asin_dates = conn.execute("SELECT value_date, flags FROM task6.product_release_date WHERE extraction_id = %s "
                              "AND asin = ANY(%s) ORDER BY value_date",
                              (world.extraction_id, ["B0T6A10001", "B0T6A10002"])).fetchall()
    assert [(str(d), f) for d, f in asin_dates] == [("2001-01-01", ["jan_first_suspect"]),
                                                    ("2003-05-20", [])]
    review_time = conn.execute("SELECT earliest_review_time FROM task6.movie_review_time WHERE extraction_id = %s "
                               "AND t4_mapping_version_id = %s AND movie_id = %s",
                               (world.extraction_id, world.mapping, a)).fetchone()
    assert review_time[0].date().isoformat() == "1999-11-02"


def test_ac02_missing_stays_missing(conn, world, full):
    """作品 B（Sparse Show）：没有年份候选，有评论与提及，release_year 保持 NULL。"""
    ctx, rv = full
    b = _movie_of(conn, world, "B0T6SPR001")
    row = _sel(conn, rv, b)
    assert row[0] == "missing" and row[1] is None
    # 有评论与评论年份提及的证据行，但不是年份候选
    assert conn.execute("SELECT count(*) FROM task6.movie_review_time WHERE movie_id = %s", (b,)).fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM task6.release_candidate WHERE extraction_id = %s AND movie_id = %s",
                        (world.extraction_id, b)).fetchone()[0] == 0


def test_ac02_no_year_becomes_date(conn, world, full):
    """全库：release_date 全为 NULL、precision 恒为 year；每个非 missing 的年份都等于采用候选的值。"""
    ctx, rv = full
    n = conn.execute("SELECT count(*) FROM task6.release_selection WHERE release_date IS NOT NULL "
                     "OR precision <> 'year' OR region IS NOT NULL").fetchone()[0]
    assert n == 0
    bad = conn.execute("SELECT count(*) FROM task6.release_selection s LEFT JOIN task6.release_candidate c "
                       "ON c.candidate_id = s.selected_candidate_id WHERE s.release_version_id = %s AND "
                       "((s.status = 'missing') <> (s.selected_candidate_id IS NULL) OR "
                       "(s.status <> 'missing' AND c.value_year IS DISTINCT FROM s.release_year))",
                       (rv,)).fetchone()[0]
    assert bad == 0


# ------------------------------------------------------------------ AC08 择优顺序、状态与分歧

def _choice(conn, rv, movie):
    return conn.execute("SELECT status, release_year, selected_candidate_id, considered_candidate_ids, "
                        "selection_rule, reason, confidence, disagreement FROM task6.release_selection "
                        "WHERE release_version_id = %s AND movie_id = %s", (rv, movie)).fetchone()


def test_ac08_verified_with_consistent_title_year(conn, world, full):
    """作品 F：IMDb 1990 + 标题年份 1990 → verified，选择记录列出全部候选与理由。"""
    ctx, rv = full
    f = _movie_of(conn, world, "B0T6FLF001")
    row = _choice(conn, rv, f)
    assert row[0] == "verified" and row[1] == 1990
    srcs = {r[0] for r in conn.execute("SELECT source FROM task6.release_candidate WHERE extraction_id = %s "
                                       "AND movie_id = %s", (world.extraction_id, f))}
    assert srcs == {"imdb_t4_link", "amazon_title_year"}
    assert len(row[3]) == 2                                  # 参与候选（含未采用的标题年份）
    assert row[4] == "t6r1:order:imdb_t4_link>imdb_t5_hint>imdb_t6_cooccur>amazon_title_year"
    assert row[5].startswith("采用 imdb_t4_link 候选")
    assert float(row[6]) == pytest.approx(0.95)
    assert row[7] == {"year_diff_1": [], "year_disagreement": [], "tconst_disagreement": []}


def test_ac08_one_year_difference_only_counted(conn, world, full):
    """作品 G：IMDb 1990 + 标题 1991 → 仍 verified 1990，相差 1 年只计数。"""
    ctx, rv = full
    g = _movie_of(conn, world, "B0T6FLG001")
    row = _choice(conn, rv, g)
    assert row[0] == "verified" and row[1] == 1990
    other = [c for c in row[3] if c != row[2]][0]
    assert row[7]["year_diff_1"] == [other] and not row[7]["year_disagreement"]
    assert conn.execute("SELECT count(*) FROM task6.quality_issue WHERE issue_code = 'year_disagreement' "
                        "AND entity_id = %s", (g,)).fetchone()[0] == 0


def test_ac08_year_disagreement_quality_issue(conn, world, full):
    """作品 H：IMDb 1990 + 标题 1994 → verified，year_disagreement（相差 4 年）。"""
    ctx, rv = full
    h_movie = _movie_of(conn, world, "B0T6FLH001")
    row = _choice(conn, rv, h_movie)
    assert row[0] == "verified" and row[1] == 1990
    bad = row[7]["year_disagreement"]
    assert len(bad) == 1 and bad[0]["year"] == 1994 and bad[0]["diff"] == 4
    issue = conn.execute("SELECT count(*) FROM task6.quality_issue WHERE issue_code = 'year_disagreement' "
                         "AND entity_id = %s", (h_movie,)).fetchone()[0]
    assert issue == 1


def test_ac08_hint_matched_with_same_title_year(conn, world, full):
    """作品 I：线索 2001 与标题年份 2001 → hint_matched（择优先于标题年份）。"""
    ctx, rv = full
    i_movie = _movie_of(conn, world, "B0T6IDX001")
    row = _choice(conn, rv, i_movie)
    assert row[0] == "hint_matched" and row[1] == 2001
    assert float(row[6]) == pytest.approx(0.80)


def test_ac08_tconst_disagreement_between_hint_and_cooccur(conn, world, full):
    """作品 L：线索 tt0006044 与共现反查 tt0006045 → 采用线索，tconst_disagreement。"""
    ctx, rv = full
    l_movie = _movie_of(conn, world, "B0T6LUK001")
    row = _choice(conn, rv, l_movie)
    assert row[0] == "hint_matched" and row[1] == 1970
    assert row[7]["tconst_disagreement"] == ["tt0006045"]
    issue = conn.execute("SELECT count(*) FROM task6.quality_issue WHERE issue_code = 'tconst_disagreement' "
                         "AND entity_id = %s", (l_movie,)).fetchone()[0]
    assert issue == 1


def test_ac08_cooccur_matched_with_title_year(conn, world, full):
    """作品 N：只有共现反查（1966）与标题年份（1966）→ cooccur_matched。"""
    ctx, rv = full
    n_movie = _movie_of(conn, world, "B0T6NEU001")
    row = _choice(conn, rv, n_movie)
    assert row[0] == "cooccur_matched" and row[1] == 1966
    assert float(row[6]) == pytest.approx(0.75)
    assert len(row[3]) == 2                                  # 共现候选与标题年份候选都算参与


def test_ac08_selection_status_is_exclusive(conn, world, full):
    """每部作品恰好一条选择记录；missing 当且仅当年份与采用候选为 NULL。"""
    ctx, rv = full
    n_movies = conn.execute("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s",
                            (world.mapping,)).fetchone()[0]
    assert conn.execute("SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s",
                        (rv,)).fetchone()[0] == n_movies
    bad = conn.execute("SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s AND "
                       "((status = 'missing') <> (release_year IS NULL AND selected_candidate_id IS NULL) "
                       "OR (status = 'missing') <> (missing_reason IS NOT NULL))", (rv,)).fetchone()[0]
    assert bad == 0
