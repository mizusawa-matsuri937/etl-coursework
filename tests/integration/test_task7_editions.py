"""任务 7 版本层消解（edition_resolve）：范围与输入版本、候选与版本属性隔离、发行商与发售日、碟数与多值特征
（spec AC01、AC02、AC09 的版本层部分）。预期值按 spec 的 AC 表手算，夹具见 tests/fixtures/task7/README.md。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_editions.py
"""

from __future__ import annotations

import pytest
import _task7_helpers as h
from etl_task7.rules import CANDIDATE_STATUSES, CONFLICT_TYPES, SELECTION_STATUSES
from task7 import world as w

pytestmark = pytest.mark.integration

M = w.Movie
E = w.Ed
IMDB = w.Imdb


def _ed(conn, ctx, asin: str) -> dict:
    cols = ("asin", "product_kind", "movie_ids", "edition_title", "edition_clean_title", "runtime_minutes",
            "mpaa_rating", "publisher", "product_release_date", "media_release_date", "listing_first_available_date",
            "date_flags", "discs", "media_format", "language", "subtitles", "aspect_ratio", "region", "cut_type",
            "video_system", "attributes_detail")
    row = conn.execute(f"SELECT {', '.join(cols)} FROM task7.edition_stage_result WHERE extraction_id = %s "
                       "AND stage_key = %s AND asin = %s",
                       (ctx.inputs.extraction_id, ctx.inputs.stage_key("edition_resolve"), asin)).fetchone()
    return dict(zip(cols, row))


# ------------------------------------------------------------------ AC01 输入版本、实体边界与处理范围（版本层）

def _scope_world(decoys: bool = True) -> w.World:
    imdb = [IMDB("tt1000001", "Verified Film", year=1990, runtime=100),
            IMDB("tt1000002", "Hinted Film", year=1991, runtime=95),
            IMDB("tt1000003", "Cooccur Film", year=1992, runtime=90)]
    movies = [M("Verified", w.Release("imdb_t4_link", "tt1000001")), M("Hinted", w.Release("imdb_t5_hint", "tt1000002")),
              M("Cooccur", w.Release("imdb_t6_cooccur", "tt1000003")),
              M("PageOnly", w.Release("page_reported", title_years=(1980,))), M("Unknown"),
              M("MemberOnly", w.Release("imdb_t4_link", "tt1000001"))]
    eds = [E("B0V", "Verified Film [DVD]", movie="Verified", runtime=((100, "detail_bullets"),)),
           E("B0H", "Hinted Film [VHS]", movie="Hinted", runtime=((95, "detail_bullets"),)),
           E("B0C", "Cooccur Film", movie="Cooccur", runtime=((90, "product_overview"),)),
           E("B0P", "Page Only (1980)", movie="PageOnly", runtime=((88, "detail_bullets"),)),
           E("B0U", "Unknown Film", movie="Unknown"),
           E("B0COL", "Double Feature [DVD]", kind="collection", runtime=((190, "detail_bullets"),),
             members=((1, "MemberOnly", "Member One"),)),
           E("B0EMPTY", "Mystery Box Set", kind="collection"),            # 没有关联作品的合集
           E("B0TV", "Some Series Season 1", kind="tv_series", movie=None, runtime=((45, "detail_bullets"),))]
    return w.World(imdb, movies, eds, decoys=decoys)


def test_ac01_edition_scope_one_row_per_matched_product_and_no_tv_series(conn):
    """R06：范围取 task4.product_edition 的 matched 商品，每个一行；无关联合集 movie_ids = '{}'；tv_series 不进入 task7。"""
    s = h.seed(conn, _scope_world())
    ctx = h.run_stages(conn, s, upto="edition_resolve")
    asins = [r[0] for r in conn.execute("SELECT asin FROM task7.edition_stage_result WHERE extraction_id = %s AND "
                                        "stage_key = %s ORDER BY asin", (s.eid, ctx.inputs.stage_key("edition_resolve")))]
    assert asins == sorted(["B0V", "B0H", "B0C", "B0P", "B0U", "B0COL", "B0EMPTY"])        # 7 个 matched 商品
    assert _ed(conn, ctx, "B0EMPTY")["movie_ids"] == [] and _ed(conn, ctx, "B0EMPTY")["product_kind"] == "collection"
    assert _ed(conn, ctx, "B0COL")["movie_ids"] == [s.mid("MemberOnly")]                   # 只经 contains 出现的成员
    assert _ed(conn, ctx, "B0V")["movie_ids"] == [s.mid("Verified")]
    # tv_series 商品没有进入任何 task7 表
    for table, col in (("task7.edition_stage_result", "asin"), ("task7.field_candidate", "entity_id"),
                       ("task7.entity_stage", "entity_id"), ("task7.quality_issue", "entity_id")):
        assert conn.execute(f"SELECT count(*) FROM {table} WHERE {col} = 'B0TV'").fetchone()[0] == 0, table
    # 实体标记与批次审计：每个 matched 商品恰好一个标记
    assert conn.execute("SELECT count(*), count(DISTINCT entity_id) FROM task7.entity_stage WHERE stage = 'edition_resolve'"
                        ).fetchone() == (7, 7)


def test_ac01_edition_reads_only_the_input_versions(conn):
    """R01：只读 map0002、t4n1、t3p1、rv0001；其他规范化版本、解析版本与映射版本的干扰行不进入结果。"""
    s = h.seed(conn, _scope_world(decoys=True))
    # 干扰行确实存在：t4n0 与 t3p0 的断言与 map0001 的关系
    assert conn.execute("SELECT count(*) FROM task4.field_assertion WHERE norm_version = 't4n0' OR parser_version = 't3p0'"
                        ).fetchone()[0] > 0
    assert conn.execute("SELECT count(*) FROM task4.product_movie WHERE mapping_version_id = 'map0001'").fetchone()[0] > 0
    ctx = h.run_stages(conn, s, upto="edition_resolve")
    assert _ed(conn, ctx, "B0V")["runtime_minutes"] == 100 and _ed(conn, ctx, "B0V")["edition_title"] == "Verified Film [DVD]"
    assert _ed(conn, ctx, "B0V")["movie_ids"] == [s.mid("Verified")]                         # 不是 map0001 的 Decoy Movie
    runtime_values = {r[0] for r in conn.execute("SELECT normalized_value FROM task7.field_candidate "
                                                 "WHERE field_name = 'runtime_minutes' AND entity_type = 'edition'")}
    assert runtime_values.isdisjoint({"999", "888"})
    assert conn.execute("SELECT count(*) FROM task7.field_candidate WHERE raw_value LIKE '%%Decoy%%'").fetchone()[0] == 0


# ------------------------------------------------------------------ AC02 候选保留、状态语义与版本属性隔离（版本层）

def _attribute_world() -> w.World:
    imdb = [IMDB("tt2000001", "Seven Samurai", year=1954, runtime=207)]
    movies = [M("Samurai", w.Release("imdb_t4_link", "tt2000001"))]
    eds = [E("B0DVD", "Seven Samurai [DVD]", movie="Samurai", runtime=((207, "detail_bullets"),), discs=((2, "detail_bullets"),),
             publisher=(("Criterion Collection", "detail_bullets"),), rating=(("NR (Not Rated)", "detail_bullets"),),
             media_format=(("DVD", "byline"),), aspect_ratio=(("1.85:1", "detail_bullets"),), packaging=("Widescreen Edition",),
             video_system=(("NTSC", "detail_bullets"),), language=(("Japanese", "product_overview"),),
             subtitles=(("English", "detail_bullets"),)),
           E("B0VHS", "Seven Samurai [VHS]", movie="Samurai", runtime=((160, "detail_bullets"),),
             discs=((1, "detail_bullets"),), publisher=(("Voyager", "detail_bullets"),), rating=(("Unrated", "byline"),),
             media_format=(("VHS Tape", "byline"),), packaging=("Full Screen",), video_system=(("PAL", "detail_bullets"),),
             cut_type=(("Unrated", "title"),)),
           E("B0BOX", "Kurosawa Collection [DVD]", kind="collection", runtime=((600, "detail_bullets"),),
             rating=(("R (Restricted)", "detail_bullets"),), members=((1, "Samurai", "Seven Samurai"),),
             media_format=(("DVD", "byline"),), publisher=(("Criterion", "detail_bullets"),))]
    return w.World(imdb, movies, eds)


def test_ac02_candidates_have_full_location_and_exclusive_status(conn):
    """R07、R08：每条候选带完整定位；状态互斥且在闭集内；missing 的 considered_candidate_ids 为空；冲突类型在闭集内。"""
    s = h.seed(conn, w.World(*_missing_world_args()))
    before = h.upstream_digest(conn)
    ctx = h.run_stages(conn, s, upto="edition_resolve")
    assert h.upstream_digest(conn) == before                                              # 上游行不变
    rows = conn.execute("SELECT status, source_ref, source_key, source, raw_value, precision, match_reliability, "
                        "source_acquired_at FROM task7.field_candidate WHERE extraction_id = %s AND stage_key = %s",
                        (s.eid, ctx.inputs.stage_key("edition_resolve"))).fetchall()
    assert rows, "应有候选"
    for status, ref, key, source, raw, precision, reliability, acquired in rows:
        assert status in CANDIDATE_STATUSES
        assert ref["asin"] == "B0M1" and key.startswith("B0M1:")
        assert ref.get("section") and ref.get("content_hash")                              # 区域与快照哈希
        assert source.startswith("page_") and raw is not None and precision and acquired is not None
        assert 0 <= float(reliability) <= 1
    sels = conn.execute("SELECT field_selections FROM task7.edition_stage_result WHERE extraction_id = %s AND stage_key = %s",
                        (s.eid, ctx.inputs.stage_key("edition_resolve"))).fetchall()
    seen_missing = False
    for (fs,) in sels:
        assert set(fs) == {"edition_title", "runtime_minutes", "mpaa_rating", "publisher", "product_release_date", "discs"}
        for field, sel in fs.items():
            assert sel["status"] in SELECTION_STATUSES and sel["conflict_type"] in CONFLICT_TYPES
            assert (sel["status"] == "missing") == (sel["considered_candidate_ids"] == [])
            assert (sel["status"] == "resolved") == (sel["winning_value"] is not None)
            if sel["status"] != "resolved":
                assert sel["selected_candidate_ids"] == []
            else:
                assert sel["selected_candidate_ids"] and set(sel["selected_candidate_ids"]) <= set(sel["considered_candidate_ids"])
            seen_missing |= sel["status"] == "missing"
    assert seen_missing


def _missing_world_args():
    imdb = []
    movies = [M("Mm")]
    eds = [E("B0M1", "Plain Film [DVD]", movie="Mm", runtime=((90, "detail_bullets"), (90, "product_overview")),
             publisher=(("Fox", "detail_bullets"),), rating=(("PG", "byline"),))]
    return imdb, movies, eds


def test_ac02_edition_attributes_stay_on_the_edition(conn):
    """R04、R05：同一作品的 DVD 与 VHS 版本、合集版本各自保留片长、分级、发行商、碟数与多值特征，不互相覆盖或下推。"""
    s = h.seed(conn, _attribute_world())
    ctx = h.run_stages(conn, s, upto="edition_resolve")
    dvd, vhs, box = _ed(conn, ctx, "B0DVD"), _ed(conn, ctx, "B0VHS"), _ed(conn, ctx, "B0BOX")
    assert (dvd["runtime_minutes"], vhs["runtime_minutes"], box["runtime_minutes"]) == (207, 160, 600)
    assert (dvd["mpaa_rating"], vhs["mpaa_rating"], box["mpaa_rating"]) == ("NR/Unrated", "NR/Unrated", "R")
    assert (dvd["discs"], vhs["discs"], box["discs"]) == (2, 1, None)
    assert (dvd["publisher"], vhs["publisher"], box["publisher"]) == ("Criterion Collection", "Voyager", "Criterion")
    assert dvd["media_format"] == ["DVD"] and vhs["media_format"] == ["VHS"]
    assert dvd["aspect_ratio"] == ["1.85:1", "Widescreen"] and vhs["aspect_ratio"] == ["Full Screen"]
    assert dvd["video_system"] == ["NTSC"] and vhs["video_system"] == ["PAL"]
    assert vhs["cut_type"] == ["Unrated"] and dvd["cut_type"] == []
    assert dvd["language"] == ["Japanese"] and dvd["subtitles"] == ["English"]
    # 合集的总片长与分级属于合集版本自身，成员作品所在的版本行没有被改动
    assert box["movie_ids"] == [s.mid("Samurai")] and dvd["movie_ids"] == [s.mid("Samurai")]
    # 版本差异没有产生任何作品层冲突：版本层不写 movie 实体的候选
    assert conn.execute("SELECT count(*) FROM task7.field_candidate WHERE entity_type = 'movie'").fetchone()[0] == 0


# ------------------------------------------------------------------ AC09 版本层标量与多值特征

def _ac09_world() -> w.World:
    movies = [M(f"Mv{k}") for k in range(1, 10)]
    eds = [
        E("B0E1", "Warner Film [DVD]", movie="Mv1", publisher=(("Warner Home Video", "detail_bullets"),
                                                               ("Warner Bros.", "product_overview")),
          dates=(("media_release", "2003-05-20", ()), ("listing_first_available", "2001-09-10", ()))),
        E("B0E2", "Sony Film", movie="Mv2", publisher=(("Universal Studios", "product_overview"),
                                                       ("Sony Pictures", "detail_bullets"))),
        E("B0E3", "Multi Region Film", movie="Mv3",
          media_format=(("DVD", "byline"), ("Color, DVD, NTSC, Widescreen", "detail_bullets")),
          video_system=(("NTSC", "detail_bullets"),), language=(("English", "product_overview"), ("English", "detail_bullets")),
          subtitles=(("French", "detail_bullets"),), swatch=("DVD",), region=(("Region 2", "title"),),
          cut_type=(("directors cut", "title"),)),
        E("B0E4", "Flagged Date Film", movie="Mv4",
          dates=(("media_release", "2004-01-01", ("jan_first_suspect", "after_review_period")),)),
        E("B0E5", "Packaging Discs Film", movie="Mv5", packaging=("2-Disc Set",)),
        E("B0E6", "Discs Agree Film", movie="Mv6", discs=((1, "detail_bullets"),), packaging=("Two-Disc Special Edition",)),
        E("B0E7", "Discs Conflict Film", movie="Mv7", discs=((2, "product_details"), (1, "detail_bullets"))),
        E("B0E8", "Listing Only Film", movie="Mv8", dates=(("listing_first_available", "2005-06-07", ()),)),
        E("B0E9", "Runtime Conflict Film", movie="Mv9", runtime=((97, "product_overview"), (95, "detail_bullets"),
                                                                 (95, "product_overview"))),
    ]
    return w.World([], movies, eds)


@pytest.fixture
def ac09(conn):
    s = h.seed(conn, _ac09_world())
    return s, h.run_stages(conn, s, upto="edition_resolve")


def test_ac09_publisher_alias_and_semantic_date(conn, ac09):
    """E1：发行商取信息更完整的 Warner Home Video（formatting_or_alias），发售日取 media_release 并保留上架日（semantic_distinction）。"""
    s, ctx = ac09
    e1 = _ed(conn, ctx, "B0E1")
    assert e1["publisher"] == "Warner Home Video"
    sel = h.edition_sel(conn, ctx, "B0E1", "publisher")
    assert (sel["status"], sel["conflict_type"], sel["selection_rule"]) == ("resolved", "formatting_or_alias", "EP:PB2")
    c = h.candidates(conn, ctx, "edition_resolve", "B0E1", "publisher")
    assert {x["raw_value"]: x["status"] for x in c} == {"Warner Home Video": "adopted", "Warner Bros.": "superseded_by_specific"}
    assert sel["selected_candidate_ids"] == [next(x["candidate_id"] for x in c if x["raw_value"] == "Warner Home Video")]
    assert str(e1["product_release_date"]) == "2003-05-20"
    assert (str(e1["media_release_date"]), str(e1["listing_first_available_date"])) == ("2003-05-20", "2001-09-10")
    dsel = h.edition_sel(conn, ctx, "B0E1", "product_release_date")
    assert (dsel["status"], dsel["conflict_type"], dsel["winning_value"]) == ("resolved", "semantic_distinction", "2003-05-20")
    assert dsel["disagreement_detail"]["listing_first_available"] == "2001-09-10"
    dc = h.candidates(conn, ctx, "edition_resolve", "B0E1", "product_release_date")
    assert {x["source"]: (x["normalized_value"], x["status"]) for x in dc} == {
        "page_media_release": ("2003-05-20", "adopted"), "page_listing_first_available": ("2001-09-10", "rejected")}
    assert len(dc) == 2 and e1["date_flags"] == []


def test_ac09_publisher_conflict_uses_region_priority(conn, ac09):
    """E2：完全不同的发行商取区域优先级最高的 Sony Pictures（intra_asin_publisher_conflict），另一候选 rejected。"""
    s, ctx = ac09
    assert _ed(conn, ctx, "B0E2")["publisher"] == "Sony Pictures"
    sel = h.edition_sel(conn, ctx, "B0E2", "publisher")
    assert (sel["conflict_type"], sel["confidence"]) == ("intra_asin_publisher_conflict", 0.70)
    by_raw = {c["raw_value"]: c["status"] for c in h.candidates(conn, ctx, "edition_resolve", "B0E2", "publisher")}
    assert by_raw == {"Sony Pictures": "adopted", "Universal Studios": "rejected"}


def test_ac09_multivalue_features_are_unions_with_sections(conn, ac09):
    """E3：多区域 DVD、Widescreen、NTSC、English、French 的并集，每个值保留来源区域，按区域优先级排序。"""
    s, ctx = ac09
    e3 = _ed(conn, ctx, "B0E3")
    assert e3["media_format"] == ["DVD"] and e3["aspect_ratio"] == ["Widescreen"] and e3["video_system"] == ["NTSC"]
    assert e3["language"] == ["English"] and e3["subtitles"] == ["French"]
    assert e3["region"] == ["Region 2"] and e3["cut_type"] == ["Director's Cut"]
    d = e3["attributes_detail"]
    assert d["media_format"]["DVD"] == ["detail_bullets", "byline", "format_swatch"]        # 三个来源：页面详情、署名行、格式选择器
    assert d["language"]["English"] == ["detail_bullets", "product_overview"]
    assert d["video_system"]["NTSC"] == ["detail_bullets"]                                    # media_format 串里的 NTSC 与独立断言去重
    assert d["aspect_ratio"]["Widescreen"] == ["detail_bullets"]
    assert set(d) == {"media_format", "language", "subtitles", "aspect_ratio", "region", "cut_type", "video_system"}


def test_ac09_date_flags_lower_confidence_without_changing_value(conn, ac09):
    """E4：任务 6 的 after_review_period、jan_first_suspect 写入 date_flags，置信度降到 0.60，日期值不改动。"""
    s, ctx = ac09
    e4 = _ed(conn, ctx, "B0E4")
    assert e4["date_flags"] == ["after_review_period", "jan_first_suspect"]
    assert str(e4["product_release_date"]) == "2004-01-01"
    sel = h.edition_sel(conn, ctx, "B0E4", "product_release_date")
    assert (sel["status"], sel["confidence"], sel["conflict_type"]) == ("resolved", 0.60, "none")
    assert sel["disagreement_detail"]["date_flags"] == ["after_review_period", "jan_first_suspect"]


def test_ac09_discs_from_packaging_has_selection_record(conn, ac09):
    """E5：没有 edition.discs 断言时从包装 2-Disc Set 提取 discs = 2，且有选择记录与候选。"""
    s, ctx = ac09
    assert _ed(conn, ctx, "B0E5")["discs"] == 2
    sel = h.edition_sel(conn, ctx, "B0E5", "discs")
    assert (sel["status"], sel["winning_value"], sel["selection_rule"]) == ("resolved", "2", "EC3")
    assert sel["disagreement_detail"] == {"derived_from": "edition.packaging"}
    c = h.candidates(conn, ctx, "edition_resolve", "B0E5", "discs")
    assert [(x["source"], x["raw_value"], x["normalized_value"], x["status"]) for x in c] == [
        ("page_packaging_discs", "2-Disc Set", "2", "adopted")]
    assert sel["selected_candidate_ids"] == [c[0]["candidate_id"]]
    # 有 edition.discs 断言时断言优先，包装只作候选保留
    sel6 = h.edition_sel(conn, ctx, "B0E6", "discs")
    assert (sel6["winning_value"], sel6["selection_rule"]) == ("1", "EC1")
    c6 = {x["source"]: (x["normalized_value"], x["status"]) for x in h.candidates(conn, ctx, "edition_resolve", "B0E6", "discs")}
    assert c6 == {"page_discs": ("1", "adopted"), "page_packaging_discs": ("2", "rejected")}
    # 同 ASIN 多区域碟数不同：区域优先级（R47 引 R44）
    sel7 = h.edition_sel(conn, ctx, "B0E7", "discs")
    assert (sel7["winning_value"], sel7["conflict_type"]) == ("1", "intra_asin_runtime_conflict")


def test_ac09_listing_only_and_missing_fields(conn, ac09):
    """只有上架日时回退并在理由注明；没有断言的字段为 missing，considered_candidate_ids 为空。"""
    s, ctx = ac09
    e8 = _ed(conn, ctx, "B0E8")
    assert str(e8["product_release_date"]) == "2005-06-07" and e8["media_release_date"] is None
    sel = h.edition_sel(conn, ctx, "B0E8", "product_release_date")
    assert sel["disagreement_detail"]["fallback"] == "listing_first_available" and "回退" in sel["reason"]
    pub = h.edition_sel(conn, ctx, "B0E8", "publisher")
    assert (pub["status"], pub["considered_candidate_ids"], pub["winning_value"], pub["conflict_type"]) == (
        "missing", [], None, "none")
    assert _ed(conn, ctx, "B0E8")["publisher"] is None and _ed(conn, ctx, "B0E8")["discs"] is None


def test_ac09_same_asin_runtime_conflict_uses_section_priority(conn, ac09):
    """R44：同 ASIN 多区域片长按 detail_bullets > product_overview 取值并记 intra_asin_runtime_conflict。"""
    s, ctx = ac09
    assert _ed(conn, ctx, "B0E9")["runtime_minutes"] == 95
    sel = h.edition_sel(conn, ctx, "B0E9", "runtime_minutes")
    assert sel["conflict_type"] == "intra_asin_runtime_conflict" and sel["confidence"] == 0.70
    got = {(x["source_ref"]["section"], x["normalized_value"]): x["status"]
           for x in h.candidates(conn, ctx, "edition_resolve", "B0E9", "runtime_minutes")}
    assert got == {("product_overview", "97"): "rejected", ("detail_bullets", "95"): "adopted",
                   ("product_overview", "95"): "adopted"}
    # 原始标题与清洁规范名都保存
    t = _ed(conn, ctx, "B0E1")
    assert (t["edition_title"], t["edition_clean_title"]) == ("Warner Film [DVD]", "Warner Film")
