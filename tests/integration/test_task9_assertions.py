"""任务 9 暂存层与候选级断言 build_assertions（spec AC05；AC01 的断言部分）。夹具见 tests/fixtures/task9/README.md；
预期值按 spec 手算或逐列对照上游行，不由实现生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_assertions.py
"""

from __future__ import annotations

import pytest

import _task9_helpers as h
from etl_task9.rules import assertion_id
from etl_task9.run import Task9InputError, check_inputs, start_run
from etl_task9.assertions import build_assertions

COLS = ("assertion_id", "level", "entity_type", "entity_id", "field_name", "member_key", "raw_value", "normalized_value",
        "source_record_id", "field_locator", "extract_rule_version", "rule_id", "upstream_status", "upstream_reason",
        "upstream_table", "upstream_key", "upstream_run_id", "confidence")


@pytest.fixture
def t9(conn, make_settings, tmp_path):
    t = h.seed(conn, make_settings, tmp_path)
    h.run(conn, t, through="assertions")
    return t


def _rows(conn, sk, table):
    return {r[COLS.index("upstream_key")]: dict(zip(COLS, r)) for r in conn.execute(
        f"SELECT {', '.join(COLS)} FROM task9.assertion WHERE stage_key = %s AND upstream_table = %s", (sk, table))}


def _sr(conn, ssk):
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT source_record_id, source_kind, locator FROM task9.source_record WHERE stage_key = %s", (ssk,))}


# ---------------------------------------------------------------- AC05：抽取级来源齐全，各列与上游一致

def test_ac05_extract_sources_complete(conn, t9):
    sk = h.stage_key(conn, "assertions")
    by = dict(((t, lvl), n) for t, lvl, n in conn.execute(
        "SELECT upstream_table, level, count(*) FROM task9.assertion WHERE stage_key = %s GROUP BY 1, 2", (sk,)))
    for table in ("task3.page_field", "task4.field_assertion", "task5.person_mention", "imdb.title", "imdb.title_aka",
                  "imdb.title_director", "imdb.title_principal", "imdb.name", "public.review_staging"):
        assert by.get((table, "extract"), 0) > 0, table
    assert by[("task4.field_assertion", "derived")] > 0            # 有父断言（标题拆出的说明段、年份）
    assert by[("task6.release_candidate", "candidate")] > 0 and by[("task7.field_candidate", "candidate")] > 0
    # 每条抽取级断言都指向同一血缘输入下的源记录
    assert conn.execute("SELECT count(*) FROM task9.assertion a WHERE a.stage_key = %s AND a.level = 'extract' "
                        "AND NOT EXISTS (SELECT 1 FROM task9.source_record s WHERE s.stage_key = %s "
                        "AND s.source_record_id = a.source_record_id)",
                        (sk, h.stage_key(conn, "sources"))).fetchone()[0] == 0


def test_ac05_task4_assertions_match_upstream(conn, t9):
    sk, ssk = h.stage_key(conn, "assertions"), h.stage_key(conn, "sources")
    rows = _rows(conn, sk, "task4.field_assertion")
    up = conn.execute("SELECT a.assertion_id, a.entity_type, a.entity_id, a.field_name, a.ordinal, a.raw_value, "
                      "a.normalized_value, a.rule_id, a.parent_assertion_id, a.qualifier, s.locator->>'asin' "
                      "FROM task4.field_assertion a JOIN task4.source_record s USING (source_record_id) "
                      "WHERE a.extraction_id = %s AND a.parser_version = 't3p1' AND a.norm_version = 't4n1'",
                      (t9.eid,)).fetchall()
    assert len(up) == len(rows)
    srs = _sr(conn, ssk)
    for aid, etype, eid, field, ordinal, raw, norm, rule, parent, qualifier, asin in up:
        r = rows[str(aid)]
        assert r["assertion_id"] == assertion_id("task4.field_assertion", str(aid))
        assert (r["entity_type"], r["entity_id"], r["field_name"], r["member_key"], r["raw_value"],
                r["normalized_value"], r["rule_id"], r["extract_rule_version"]) == \
            ({"product_edition": "edition", "collection_member": "collection_member"}[etype], eid, field, str(ordinal),
             raw, norm, rule, "t4n1")
        assert r["level"] == ("extract" if parent is None else "derived")
        assert r["field_locator"]["task4_assertion_id"] == aid
        assert r["field_locator"]["parent_assertion_id"] == parent and r["field_locator"]["qualifier"] == qualifier
        kind, loc = srs[r["source_record_id"]]
        assert kind == "page_snapshot" and loc["asin"] == asin
    lg = [r for r in rows.values() if r["entity_id"] == "B0Lg" and r["field_name"] == "title_year"]
    assert len(lg) == 1 and lg[0]["level"] == "derived" and lg[0]["raw_value"] == "1986"


def test_ac05_page_field_and_mentions_match_upstream(conn, t9):
    sk = h.stage_key(conn, "assertions")
    fields = _rows(conn, sk, "task3.page_field")
    up = conn.execute("SELECT asin, section, ordinal, label_raw, value_raw, field_key FROM task3.page_field "
                      "WHERE extraction_id = %s AND parser_version = 't3p1'", (t9.eid,)).fetchall()
    assert len(up) == len(fields) > 0
    for asin, section, ordinal, label, value, key in up:
        r = fields[f"{t9.eid}|{asin}|t3p1|{section}|{ordinal}"]
        assert (r["entity_id"], r["field_name"], r["member_key"], r["raw_value"]) == \
            (asin, key or section, f"{section}:{ordinal}", value)
        assert (r["field_locator"]["section"], r["field_locator"]["ordinal"], r["field_locator"]["label_raw"]) == \
            (section, ordinal, label)
    mentions = _rows(conn, sk, "task5.person_mention")
    up = conn.execute("SELECT mention_key, role_class, raw_value, display_name, asin, section, page_ordinal, status, "
                      "run_id FROM task5.person_mention WHERE extraction_id = %s", (t9.eid,)).fetchall()
    assert len(up) == len(mentions) > 0
    for key, role, raw, display, asin, section, page_ordinal, status, run in up:
        r = mentions[f"{t9.eid}|t3p1|t5n1|{key}"]
        assert (r["entity_type"], r["entity_id"], r["field_name"], r["raw_value"], r["normalized_value"],
                r["upstream_status"], r["upstream_run_id"]) == ("mention", key, role, raw, display, status, run)
        assert (r["field_locator"]["asin"], r["field_locator"]["section"], r["field_locator"]["page_ordinal"]) == \
            (asin, section, page_ordinal)


def test_ac05_imdb_and_review_rows(conn, t9):
    sk, ssk = h.stage_key(conn, "assertions"), h.stage_key(conn, "sources")
    tconst, line, year, genres = conn.execute(
        "SELECT tconst, line_no, start_year, genres FROM imdb.title WHERE snapshot_id = %s "
        "AND primary_title = 'Legend Picture'", (h.SNAPSHOT,)).fetchone()
    titles = _rows(conn, sk, "imdb.title")
    r = titles[f"{h.SNAPSHOT}|{tconst}|start_year"]
    assert (r["entity_type"], r["entity_id"], r["field_name"], r["raw_value"]) == ("imdb_title", tconst, "start_year",
                                                                                  str(year))
    assert r["field_locator"] == {"file": "title.basics.tsv.gz", "line_no": line, "column": "start_year"}
    assert r["extract_rule_version"] == h.SNAPSHOT
    kind, loc = _sr(conn, ssk)[r["source_record_id"]]
    assert kind == "imdb_row" and loc["line_no"] == line and loc["tconst"] == tconst
    aka = [x for x in _rows(conn, sk, "imdb.title_aka").values() if x["entity_id"] == tconst]
    assert [(x["field_name"], x["raw_value"]) for x in aka] == [("aka_title", "Legend Of Darkness")]
    reviews = _rows(conn, sk, "public.review_staging")
    up = conn.execute("SELECT source_review_id, review_time_raw, review_time_utc FROM public.review_staging "
                      "WHERE extraction_id = %s AND product_id IN ('B0Lg', 'B0LgB', 'B0LgD')", (t9.eid,)).fetchall()
    assert len(up) == len(reviews) == 6
    for rid, raw, utc in up:
        x = reviews[f"{t9.eid}|{rid}"]
        assert (x["entity_type"], x["entity_id"], x["field_name"], x["raw_value"]) == ("review", rid, "review_time", raw)
        assert x["normalized_value"] == utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _sr(conn, ssk)[x["source_record_id"]][0] == "review_record"


# ---------------------------------------------------------------- AC05：ID 稳定

def test_ac05_assertion_ids_stable_across_builds(conn, t9):
    sk1 = h.stage_key(conn, "assertions")
    first = dict(conn.execute("SELECT upstream_table || '|' || upstream_key, assertion_id FROM task9.assertion "
                              "WHERE stage_key = %s", (sk1,)).fetchall())
    for key, aid in first.items():
        table, _, upstream = key.partition("|")
        assert aid == assertion_id(table, upstream)
    h.run(conn, t9, through="assertions", over={"conflict_version_id": t9.other_cv,
                                                "review_dedup_version_id": "rd0002"})
    sk2 = h.stage_key(conn, "assertions")
    assert sk2 != sk1
    second = dict(conn.execute("SELECT upstream_table || '|' || upstream_key, assertion_id FROM task9.assertion "
                               "WHERE stage_key = %s", (sk2,)).fetchall())
    page_side = {k for k in first if k.split("|", 1)[0] in ("task3.page_field", "task4.field_assertion",
                                                            "task5.person_mention", "public.review_staging")}
    assert page_side and page_side <= set(second)
    assert all(first[k] == second[k] for k in set(first) & set(second))


# ---------------------------------------------------------------- AC05：未采用候选与上游状态（R15）

def test_ac05_unadopted_candidates_kept_with_status(conn, t9):
    sk = h.stage_key(conn, "assertions")
    lg = t9.mid("Lg")
    cands = [r for r in _rows(conn, sk, "task7.field_candidate").values()
             if r["entity_id"] == lg and r["field_name"] == "title"]
    up = conn.execute("SELECT candidate_id, source, raw_value, normalized_value, status, status_reason, run_id, "
                      "match_reliability FROM task7.field_candidate WHERE extraction_id = %s AND stage_key = %s "
                      "AND entity_id = %s AND field_name = 'title'",
                      (t9.eid, check_inputs(conn, t9.inputs()).cv_stage_keys[2], lg)).fetchall()
    assert len(cands) == len(up) == 5
    assert {r["upstream_status"] for r in cands} == {"adopted", "rejected", "superseded_by_specific"}
    by_key = {r["upstream_key"]: r for r in cands}
    for cid, source, raw, norm, status, reason, run, rel in up:
        r = by_key[str(cid)]
        assert (r["level"], r["entity_type"], r["raw_value"], r["normalized_value"], r["upstream_status"],
                r["upstream_reason"], r["upstream_run_id"], r["rule_id"], r["confidence"]) == \
            ("candidate", "movie", raw, norm, status, reason, run, source, rel)
        assert r["field_locator"]["source"] == source
    rejected = [r for r in cands if r["upstream_status"] == "rejected"]
    assert [(r["raw_value"], r["upstream_reason"]) for r in rejected] == [("Unicorn Chase Adventure [DVD]",
                                                                           "unrelated_to_imdb_title")]
    superseded = [r for r in cands if r["upstream_status"] == "superseded_by_specific"]
    assert [r["raw_value"] for r in superseded] == ["Legend Of Darkness [VHS]"]
    # 任务 6：IMDb 1985 被选中，页面标题年份 1986 未被选中，二者都登记（R15）
    t6 = sorted((r["normalized_value"], r["upstream_status"], r["rule_id"])
                for r in _rows(conn, sk, "task6.release_candidate").values() if r["entity_id"] == lg)
    assert t6 == [("1985", "selected", "imdb_t4_link"), ("1986", "not_selected", "amazon_title_year")]


# ---------------------------------------------------------------- AC01：断言只读指定版本

def test_ac01_assertions_only_from_requested_versions(conn, t9):
    sk = h.stage_key(conn, "assertions")
    i = check_inputs(conn, t9.inputs())
    # 干扰：规范化版本 t4n0、解析版本 t3p0 的断言，上映版本 rv0002 的候选，cv0002（t7r1）阶段键下的候选
    decoy_t4 = {str(r[0]) for r in conn.execute(
        "SELECT assertion_id FROM task4.field_assertion WHERE norm_version <> 't4n1' OR parser_version <> 't3p1'")}
    assert decoy_t4
    keys = {r["upstream_key"] for r in _rows(conn, sk, "task4.field_assertion").values()}
    assert not keys & decoy_t4
    rv2 = {str(r[0]) for r in conn.execute(
        "SELECT unnest(considered_candidate_ids) FROM task6.release_selection WHERE release_version_id = 'rv0002'")}
    assert rv2 and not rv2 & set(_rows(conn, sk, "task6.release_candidate"))
    other_keys = check_inputs(conn, t9.inputs(conflict_version_id=t9.other_cv, review_dedup_version_id="rd0002"))
    other = {str(r[0]) for r in conn.execute(
        "SELECT candidate_id FROM task7.field_candidate WHERE stage_key IN (%s, %s) AND stage_key NOT IN (%s, %s)",
        (other_keys.cv_stage_keys[2], other_keys.cv_stage_keys[0], i.cv_stage_keys[2], i.cv_stage_keys[0]))}
    assert other and not other & set(_rows(conn, sk, "task7.field_candidate"))
    stages = {r[0] for r in conn.execute("SELECT DISTINCT field_locator->>'stage_key' FROM task9.assertion "
                                         "WHERE stage_key = %s AND upstream_table = 'task7.field_candidate'", (sk,))}
    assert stages == {i.cv_stage_keys[2], i.cv_stage_keys[0]}
    # IMDb 断言只来自指定快照
    assert {r["extract_rule_version"] for r in _rows(conn, sk, "imdb.title").values()} == {h.SNAPSHOT}


def test_ac01_assertions_require_sources_stage(conn, make_settings, tmp_path):
    t = h.seed(conn, make_settings, tmp_path)
    ctx = start_run(conn, check_inputs(conn, t.inputs()), "stage")
    with pytest.raises(Task9InputError, match="sources"):
        build_assertions(conn, ctx)


def test_ac05_rerun_is_idempotent(conn, t9):
    sk = h.stage_key(conn, "assertions")
    snap = sorted(conn.execute("SELECT assertion_id, raw_value, upstream_status FROM task9.assertion "
                               "WHERE stage_key = %s", (sk,)).fetchall())
    out = h.run(conn, t9, through="assertions")
    assert out["sources"]["skipped"] and out["assertions"]["skipped"]
    assert sorted(conn.execute("SELECT assertion_id, raw_value, upstream_status FROM task9.assertion "
                               "WHERE stage_key = %s", (sk,)).fetchall()) == snap
