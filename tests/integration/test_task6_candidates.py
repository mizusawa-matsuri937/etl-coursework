"""任务 6 的年份候选与输入范围（spec AC01、AC03、AC04、AC07）。

预期值按夹具手算，见 tests/fixtures/task6/README.md。测试先运行任务 4 的匹配得到映射版本，再逐阶段运行任务 6
（_task6_helpers.run_stages 与 T6-05 的 runner 执行同一组函数）。命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_candidates.py
"""

from __future__ import annotations

import pytest
import _task5_helpers as t5h
import _task6_helpers as h
from etl_task6.run import RunInputs

pytestmark = pytest.mark.integration

MOVIES_EXPECTED_VERIFIED = {  # ASIN → tconst（任务 4 应自动关联的作品）
    "B0T6A10001": "tt0006001", "B0T6A10002": "tt0006001",
    "B0T6B10001": "tt0006002", "B0T6V10001": "tt0006003",
    "B0T6FLF001": "tt0006040", "B0T6FLG001": "tt0006041", "B0T6FLH001": "tt0006042",
    "B0T6ERS001": "tt0006050", "B0T6CLS001": "tt0006051", "B0T6BOT001": "tt0006049",
    "B0T6FLJ001": "tt0006060", "B0T6FLJ002": "tt0006060",
}


@pytest.fixture
def world(conn, make_settings, tmp_path):
    return h.make_world(conn, make_settings, tmp_path)


@pytest.fixture
def full(conn, world):
    """全流程运行一次（分批大小取小，顺带检验分批不重复）。"""
    return h.run_stages(conn, world, batch_size=7)


def _movie_of(conn, world, asin, ordinal=0):
    return conn.execute("SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND asin = %s "
                        "AND member_ordinal = %s", (world.mapping, asin, ordinal)).fetchone()[0]


def _sel(conn, rv, movie):
    return conn.execute("SELECT status, missing_reason, release_year, selected_candidate_id FROM "
                        "task6.release_selection WHERE release_version_id = %s AND movie_id = %s",
                        (rv, movie)).fetchone()


def _cands(conn, world, movie, source, stage_pmap=None):
    key = RunInputs(extraction_id=world.extraction_id, t4_mapping_version_id=world.mapping,
                    t5_person_mapping_version_id=stage_pmap or world.pmap_with_hints,
                    imdb_snapshot_id=world.sid).stage_key("candidates")
    return conn.execute("SELECT candidate_id, source_key, value_year, flags, shared_with_movie_id, source_ref, "
                        "match_evidence, confidence, extracted_at, source_acquired_at, raw_value "
                        "FROM task6.release_candidate WHERE extraction_id = %s AND stage_key = %s AND movie_id = %s "
                        "AND source = %s ORDER BY candidate_id",
                        (world.extraction_id, key, movie, source)).fetchall()


# ------------------------------------------------------------------ 世界健全性：任务 4 的映射符合夹具设计

def test_world_task4_mapping(conn, world):
    st = h.movie_status(conn, world)
    for asin, tconst in MOVIES_EXPECTED_VERIFIED.items():
        assert st[asin] == ("imdb_linked", tconst), asin
    assert st["B0T6D10001"] == ("imdb_linked", "tt0006010")            # FORCE_LINK 改造
    # 改造后的合并：Film D 的两个商品在同一作品、Film H 的两个商品在同一作品
    assert _movie_of(conn, world, "B0T6FLD001") == _movie_of(conn, world, "B0T6FLD002")
    assert _movie_of(conn, world, "B0T6FLH001") == _movie_of(conn, world, "B0T6FLH002")
    # 合集：Film J 两个版本合并，K2 只经 contains 出现，合集商品的成员 1 归到 J 的作品
    j_main = _movie_of(conn, world, "B0T6FLJ001")
    assert _movie_of(conn, world, "B0T6FLJ002") == j_main
    assert _movie_of(conn, world, "B0T6FLJ003", 1) == j_main
    k2 = _movie_of(conn, world, "B0T6FLJ003", 2)
    assert k2 != j_main
    # B1 的反例：两个商品没有合并（product_match_status 都是 review_pending，作品为 page_only）
    assert conn.execute("SELECT count(*) FROM task4.product_match_status WHERE mapping_version_id = %s "
                        "AND asin IN ('B0T6ONB001', 'B0T6ONB002') AND status = 'review_pending'",
                        (world.mapping,)).fetchone()[0] == 2
    assert st["B0T6ONB001"][0] == "page_only" and st["B0T6ONB002"][0] == "page_only"
    # 剧集不参与匹配：product_movie 没有它的行
    assert "B0T6TV0001" not in st


# ------------------------------------------------------------------ AC01 输入版本与处理对象

def test_ac01_inputs_scope_versions(conn, world, full):
    ctx, rv = full
    n_movies = conn.execute("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s",
                            (world.mapping,)).fetchone()[0]
    n_sel = conn.execute("SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s",
                         (rv,)).fetchone()[0]
    assert n_sel == n_movies
    # 覆盖率分母等于作品数（counts 记录）
    counts = conn.execute("SELECT counts FROM task6.release_version WHERE release_version_id = %s",
                          (rv,)).fetchone()[0]
    assert counts["movies"] == n_movies
    # 只经 contains 出现的成员作品（K2）也有一条记录：imdb:tt0006061 锚点、有 tconst 与年份 → verified
    k2 = _movie_of(conn, world, "B0T6FLJ003", 2)
    assert _sel(conn, rv, k2)[:2] == ("verified", None)
    tconst_k2 = conn.execute("SELECT tconst FROM task4.movie WHERE mapping_version_id = %s AND movie_id = %s",
                             (world.mapping, k2)).fetchone()[0]
    assert tconst_k2 == "tt0006061"
    # 旧映射版本没有的作品：clone 出的 map9002 多一部，rv 里没有它的记录
    # （用多版本作品的第二个商品拆出来；singleton 商品的 asin: 锚点已存在会冲突）
    new_map = t5h.clone_mapping(conn, world, "B0T6A10002")
    ctx2, rv2 = h.run_stages(conn, world, batch_size=7, t4_mapping_version_id=new_map)
    extra = conn.execute("SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s AND movie_id = "
                         "'mv_00000000000000aa'", (rv,)).fetchone()[0]
    assert extra == 0
    # 新版本（多一部作品）记录数 = 旧版本 + 1
    n2 = conn.execute("SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s",
                      (rv2,)).fetchone()[0]
    assert n2 == n_movies + 1


def test_ac01_inputs_scope_person_mapping(conn, world, full):
    """换用没有线索的人物映射版本（t5r9）：imdb_t5_hint 候选不存在；共现反查不读任务 5，
    仍唯一命中同一作品，状态变为 cooccur_matched。"""
    ctx2, rv2 = h.run_stages(conn, world, batch_size=7, t5_person_mapping_version_id=world.pmap_alt)
    hinted = _movie_of(conn, world, "B0T6HNT001")
    assert _cands(conn, world, hinted, "imdb_t5_hint", stage_pmap=world.pmap_alt) == []
    assert _sel(conn, rv2, hinted)[:2] == ("cooccur_matched", None)


def test_ac01_non_participating_product(conn, world, full):
    ctx, rv = full
    assert conn.execute("SELECT count(*) FROM task6.product_release_date WHERE extraction_id = %s AND asin = %s",
                        (world.extraction_id, "B0T6TV0001")).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task6.product_review_time WHERE extraction_id = %s AND asin = %s",
                        (world.extraction_id, "B0T6TV0001")).fetchone()[0] == 0
    # 合集商品的评论在商品层、不在成员作品上
    assert conn.execute("SELECT count(*) FROM task6.product_review_time WHERE extraction_id = %s AND asin = %s "
                        "AND product_kind = 'collection'", (world.extraction_id, "B0T6FLJ003")).fetchone()[0] == 1
    k2 = _movie_of(conn, world, "B0T6FLJ003", 2)
    assert conn.execute("SELECT count(*) FROM task6.movie_review_time WHERE extraction_id = %s AND movie_id = %s",
                        (world.extraction_id, k2)).fetchone()[0] == 0


# ------------------------------------------------------------------ AC03 IMDb 关联作品的年份候选

def test_ac03_imdb_link_evidence(conn, world, full):
    ctx, rv = full
    a = _movie_of(conn, world, "B0T6A10001")
    rows = _cands(conn, world, a, "imdb_t4_link")
    assert len(rows) == 1
    row = rows[0]
    assert row[2] == 1999 and row[1] == "tt0006001" and row[10] == "1999"
    ref, ev = row[5], row[6]
    assert ref["snapshot"] == world.sid and ref["title_type"] == "movie" and ref["line_no"]
    assert ref["file"].startswith("title.basics")
    assert ev["basis_products"] and all(p["decision_id"] for p in ev["basis_products"] if p["basis"] == "auto_decision")
    assert float(row[7]) == pytest.approx(0.95)       # 候选置信度（R13）
    assert row[9] is not None                      # source_acquired_at：IMDb 快照下载时间
    # 置信度在选择记录里（0.95）；候选行记录了抽取时间
    a_sel = conn.execute("SELECT status, release_year, confidence FROM task6.release_selection "
                         "WHERE release_version_id = %s AND movie_id = %s", (rv, a)).fetchone()
    assert a_sel[0] == "verified" and a_sel[1] == 1999 and float(a_sel[2]) == pytest.approx(0.95)
    # tvMovie 与 video 也各有一条
    b, v = _movie_of(conn, world, "B0T6B10001"), _movie_of(conn, world, "B0T6V10001")
    assert _cands(conn, world, b, "imdb_t4_link")[0][2] == 2004
    assert _cands(conn, world, b, "imdb_t4_link")[0][5]["title_type"] == "tvMovie"
    assert _cands(conn, world, v, "imdb_t4_link")[0][5]["title_type"] == "video"


def test_ac03_null_start_year(conn, world, full):
    ctx, rv = full
    d = _movie_of(conn, world, "B0T6D10001")
    assert _cands(conn, world, d, "imdb_t4_link") == []
    assert _sel(conn, rv, d) == ("missing", "no_candidate", None, None)


# ------------------------------------------------------------------ AC04 任务 5 升级线索

def test_ac04_hint_candidate(conn, world, full):
    ctx, rv = full
    hinted = _movie_of(conn, world, "B0T6HNT001")
    rows = _cands(conn, world, hinted, "imdb_t5_hint")
    assert len(rows) == 1
    row = rows[0]
    assert row[2] == 1975 and row[1] == "tt0006011"
    ev = row[6]
    assert ev["mention_keys"] == ["B0T6HNT001:byline:1:1", "B0T6HNT001:byline:1:2"]
    assert ev["link_decision_ids"] == [101, 102] and ev["mention_count"] == 2
    assert ev["person_mapping_version"] == world.pmap_with_hints
    assert _sel(conn, rv, hinted) == ("hint_matched", None, 1975, row[0])
    sel_conf = conn.execute("SELECT confidence FROM task6.release_selection WHERE release_version_id = %s AND "
                            "movie_id = %s", (rv, hinted)).fetchone()[0]
    assert float(sel_conf) == pytest.approx(0.80)


def test_ac04_hint_shared_tconst(conn, world, full):
    ctx, rv = full
    owned = _movie_of(conn, world, "B0T6OWN001")
    owner = _movie_of(conn, world, "B0T6A10001")
    rows = _cands(conn, world, owned, "imdb_t5_hint")
    assert len(rows) == 1
    row = rows[0]
    assert "shared_tconst" in row[3] and row[4] == owner and row[2] == 1999
    assert _sel(conn, rv, owned)[:3] == ("hint_matched", None, 1999)
    issue = conn.execute("SELECT detail FROM task6.quality_issue WHERE issue_code = 'shared_tconst_with_movie' "
                         "AND entity_id = %s", (owned,)).fetchall()
    assert issue and issue[0][0]["owner_movie_id"] == owner


def test_ac04_hint_collection_member_and_multiple(conn, world, full):
    ctx, rv = full
    actions = dict(conn.execute("SELECT asin || '#' || member_ordinal || '=' || tconst, action FROM task6.hint_use "
                                "WHERE extraction_id = %s", (world.extraction_id,)).fetchall())
    assert actions["B0T6FLJ003#1=tt0006050"] == "collection_member"
    assert actions["B0T6MHA001#0=tt0006012"] == "multiple_tconst"
    assert actions["B0T6MHA002#0=tt0006013"] == "multiple_tconst"
    multi = _movie_of(conn, world, "B0T6MHA001")
    assert conn.execute("SELECT count(*) FROM task6.release_candidate WHERE extraction_id = %s AND movie_id = %s "
                        "AND source = 'imdb_t5_hint'", (world.extraction_id, multi)).fetchone()[0] == 0
    assert _sel(conn, rv, multi)[:2] == ("missing", "hint_ambiguous")
    assert actions["B0T6HNT001#0=tt0006011"] == "candidate"


def test_ac04_task4_and_task5_not_written(conn, world):
    """任务 4、任务 5 的表行数在任务 6 运行前后不变。"""
    snapshot = ("SELECT 'task4.movie', count(*) FROM task4.movie UNION ALL "
                "SELECT 'task4.product_movie', count(*) FROM task4.product_movie UNION ALL "
                "SELECT 'task4.field_assertion', count(*) FROM task4.field_assertion UNION ALL "
                "SELECT 'task5.person_mapping_version', count(*) FROM task5.person_mapping_version UNION ALL "
                "SELECT 'task5.t4_upgrade_hint', count(*) FROM task5.t4_upgrade_hint")
    before = conn.execute(snapshot).fetchall()
    h.run_stages(conn, world, batch_size=7)
    after = conn.execute(snapshot).fetchall()
    assert before == after


# ------------------------------------------------------------------ AC07 商品标题年份

def test_ac07_title_year_consistent(conn, world, full):
    ctx, rv = full
    c = _movie_of(conn, world, "B0T6FLC001")
    rows = _cands(conn, world, c, "amazon_title_year")
    assert sorted(r[2] for r in rows) == [1985, 1985]
    assert rows[0][5]["asin"] in ("B0T6FLC001", "B0T6FLC002") and rows[0][5]["assertion_id"]
    sel = conn.execute("SELECT status, release_year, selected_candidate_id, confidence FROM task6.release_selection "
                       "WHERE release_version_id = %s AND movie_id = %s", (rv, c)).fetchone()
    assert sel[0] == "page_reported" and sel[1] == 1985 and float(sel[3]) == pytest.approx(0.60)
    assert sel[2] == rows[0][0]                     # 代表取断言 ID 最小（candidate_id 最小）的一条


def test_ac07_title_year_disagree(conn, world, full):
    ctx, rv = full
    d = _movie_of(conn, world, "B0T6FLD001")
    assert sorted(r[2] for r in _cands(conn, world, d, "amazon_title_year")) == [1985, 1987]
    assert _sel(conn, rv, d)[:2] == ("missing", "title_years_disagree")


def test_ac07_title_year_single(conn, world, full):
    ctx, rv = full
    e = _movie_of(conn, world, "B0T6FLE001")
    assert [r[2] for r in _cands(conn, world, e, "amazon_title_year")] == [1986]
    assert _sel(conn, rv, e)[:3] == ("page_reported", None, 1986)
