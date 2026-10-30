"""任务 6 的两种上界证据与推断上界（spec AC11、AC12 的数据面）。

预期值按夹具手算，见 tests/fixtures/task6/README.md。AC12 的报告计数在 T6-05 的 report 测试里核对，
本文件核对库里的数据面（最早评论早于全部 media_release 的作品、合集评论不进作品层）。
命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_bounds.py
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


def _evidence(conn, rv, movie, kind):
    return conn.execute("SELECT evidence_time, evidence_precision, basis, checks, passed, failed_checks, confidence "
                        "FROM task6.bound_evidence WHERE release_version_id = %s AND movie_id = %s AND evidence = %s",
                        (rv, movie, kind)).fetchone()


def _bound(conn, rv, movie):
    return conn.execute("SELECT bound_time, bound_precision, bound_kind, chosen_evidence, conditions, confidence, "
                        "status, rule_version FROM task6.inferred_release_bound WHERE release_version_id = %s "
                        "AND movie_id = %s", (rv, movie)).fetchone()


def _failed(conn, rv, movie, kind):
    return _evidence(conn, rv, movie, kind)[5]


# ------------------------------------------------------------------ AC11 条件反例

def test_ac11_b1_review_pending_blocks_review_evidence(conn, world, full):
    """B1：作品的两个商品都是 review_pending → 评论证据不成立（media 证据可成立）。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6ONB001")
    ev = _evidence(conn, rv, movie, "review")
    assert ev[4] is False and ev[5] == ["B1"]
    assert ev[3]["B1"] is False and ev[3]["B3"] is True
    assert _bound(conn, rv, movie) is None or _bound(conn, rv, movie)[3] == "media_release"


def test_ac11_b3_single_review_is_isolated(conn, world, full):
    """B3：只有一条评论（没有第二早）→ 不孤立不成立。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6SPR001")
    ev = _evidence(conn, rv, movie, "review")
    assert ev[4] is False and ev[5] == ["B3"]
    assert _bound(conn, rv, movie) is None


def test_ac11_b5_ancient_review_time(conn, world, full):
    """B5：最早评论 1970-01-01 早于该抽取评论时间下界（1997）→ 不成立。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6ANC001")
    ev = _evidence(conn, rv, movie, "review")
    assert ev[4] is False and ev[5] == ["B5"]
    assert _bound(conn, rv, movie) is None


def test_ac11_b4_review_before_release_year(conn, world, full):
    """B4：最早评论 1999-12-01 早于已采用 IMDb 年份 2004 → bound_before_release_year。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6ERS001")
    ev = _evidence(conn, rv, movie, "review")
    assert ev[4] is False and ev[5] == ["B4"]
    issue = conn.execute("SELECT detail FROM task6.quality_issue WHERE issue_code = 'bound_before_release_year' "
                         "AND entity_id = %s", (movie,)).fetchone()
    assert issue[0]["evidence"] == "review" and issue[0]["adopted_year"] == 2004
    sel = conn.execute("SELECT status, release_year FROM task6.release_selection WHERE release_version_id = %s "
                       "AND movie_id = %s", (rv, movie)).fetchone()
    assert sel == ("verified", 2004)                    # 年份与状态不受影响


def test_ac11_m4_media_before_release_year(conn, world, full):
    """M4：media_release 1998 早于 IMDb 年份 2004 → bound_before_release_year（证据 media_release）。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6CLS001")
    ev = _evidence(conn, rv, movie, "media_release")
    assert ev[4] is False and ev[5] == ["M4"]
    issue = conn.execute("SELECT detail FROM task6.quality_issue WHERE issue_code = 'bound_before_release_year' "
                         "AND entity_id = %s", (movie,)).fetchone()
    assert issue[0]["evidence"] == "media_release"


# ------------------------------------------------------------------ AC11 正例与精度

def test_ac11_both_evidence_take_earlier_media(conn, world, full):
    """两种证据都成立（评论 2005-02-01、media 2004-06-06）→ 推断上界取较早的发售日期。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6BOT001")
    bound = _bound(conn, rv, movie)
    assert bound[0].isoformat() == "2004-06-06" and bound[1] == "day" and bound[3] == "media_release"
    assert bound[2] == "not_later_than" and bound[6] == "inferred" and bound[7] == "t6r1"
    assert bound[4]["M4"] is True
    assert float(bound[5]) == pytest.approx(0.7)
    assert _evidence(conn, rv, movie, "review")[4] is True
    assert _evidence(conn, rv, movie, "media_release")[4] is True


def test_ac11_jan_first_suspect_downgrades_to_year(conn, world, full):
    """只有 1 月 1 日的 media_release（jan_first_suspect）→ 上界取 2001、精度 year。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6JAN001")
    ev = _evidence(conn, rv, movie, "media_release")
    assert ev[4] is True and ev[1] == "year"
    bound = _bound(conn, rv, movie)
    assert bound[0].isoformat() == "2001-01-01" and bound[1] == "year"
    assert float(bound[5]) == pytest.approx(0.6)        # jan_first_suspect 的置信度分档


def test_ac11_listing_first_available_not_media_bound(conn, world, full):
    """只有 Date First Available（上架日期）→ 没有 media 证据，也没有推断上界。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6LST001")
    assert _evidence(conn, rv, movie, "media_release") is None
    assert _bound(conn, rv, movie) is None
    assessment = conn.execute("SELECT has_review_evidence, has_media_evidence, passed_evidence, chosen_evidence "
                              "FROM task6.bound_assessment WHERE release_version_id = %s AND movie_id = %s",
                              (rv, movie)).fetchone()
    assert assessment == (False, False, [], None)


def test_ac11_bound_does_not_override_year(conn, world, full):
    """推断上界不写进选择记录、不改变 release_year 与状态（R24）。"""
    ctx, rv = full
    for asin in ("B0T6BOT001", "B0T6JAN001"):
        movie = _movie_of(conn, world, asin)
        assert _bound(conn, rv, movie) is not None
        sel = conn.execute("SELECT selected_candidate_id, disagreement FROM task6.release_selection "
                           "WHERE release_version_id = %s AND movie_id = %s", (rv, movie)).fetchone()
        assert "bound" not in (sel[1] or {})
    # 有推断上界的作品数与选择记录无交集变化：状态仍是各自原来的
    movie = _movie_of(conn, world, "B0T6BOT001")
    sel = conn.execute("SELECT status, release_year FROM task6.release_selection WHERE release_version_id = %s "
                       "AND movie_id = %s", (rv, movie)).fetchone()
    assert sel == ("verified", 2004)


# ------------------------------------------------------------------ AC12 的数据面（预售 / 合集 / 跨版本）

def test_ac12_cross_edition_earliest_review_before_media(conn, world, full):
    """多版本作品（Cross Movie）的最早评论（2000-01-01）早于全部 media_release（2005）。"""
    ctx, rv = full
    movie = _movie_of(conn, world, "B0T6CRM001")
    row = conn.execute("SELECT earliest_review_time, earliest_asin FROM task6.movie_review_time WHERE "
                       "extraction_id = %s AND t4_mapping_version_id = %s AND movie_id = %s",
                       (world.extraction_id, world.mapping, movie)).fetchone()
    assert row[0].date().isoformat() == "2000-01-01" and row[1] == "B0T6CRM001"
    media = [str(r[0]) for r in conn.execute(
        "SELECT d.value_date FROM task6.product_release_date d JOIN task4.product_movie pm "
        "ON pm.mapping_version_id = %s AND pm.asin = d.asin AND pm.member_ordinal = 0 "
        "AND pm.relation_type = 'edition' AND pm.movie_id = %s WHERE d.kind = 'media_release' "
        "ORDER BY 1", (world.mapping, movie))]
    assert media == ["2005-06-01", "2005-09-09"]
    assert row[0].date().isoformat() < media[0]         # 最早评论早于全部 media_release
    # 该作品的推断上界来自评论（评论 2000-01-01 早于 media）
    bound = _bound(conn, rv, movie)
    assert bound[3] == "review" and bound[0].isoformat() == "2000-01-01"


def test_ac12_collection_comment_counts(conn, world, full):
    """合集商品的评论（1 条）只在商品层；成员作品的评论数不含它。"""
    ctx, rv = full
    n = conn.execute("SELECT count(*) FROM task6.product_review_time WHERE extraction_id = %s AND "
                     "product_kind = 'collection'", (world.extraction_id,)).fetchone()[0]
    assert n == 1                                       # 只有 FLJ003 一个有评论的合集
    j = _movie_of(conn, world, "B0T6FLJ001")
    assert _movie_reviews(conn, world, j)[0] == 3


def _movie_reviews(conn, world, movie):
    return conn.execute("SELECT review_count FROM task6.movie_review_time WHERE extraction_id = %s AND "
                        "t4_mapping_version_id = %s AND movie_id = %s",
                        (world.extraction_id, world.mapping, movie)).fetchone()
