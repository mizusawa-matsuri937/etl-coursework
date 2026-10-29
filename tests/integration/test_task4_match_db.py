"""任务 4 候选与判定落库（spec AC05 的十个案例与决策行、AC06 的合集成员关联）。预期按 spec 与 design 手算。"""

from __future__ import annotations

import pytest

from _task4_helpers import match_products, run_decisions
from etl_task4 import candidates
from etl_task4.match import DEFAULT_RULES

pytestmark = pytest.mark.integration

A = "B0T4M00"


@pytest.fixture
def world(conn, make_settings, tmp_path):
    return run_decisions(conn, make_settings, tmp_path)


def decision(conn, left, right, kind="imdb"):
    row = conn.execute(
        "SELECT d.tier, d.reason, d.veto, d.score, d.confidence, d.rank, d.margin, c.generator, d.features "
        "FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id) "
        "WHERE c.left_entity = %s AND c.right_kind = %s AND c.right_id = %s AND d.rule_version = 't4r1'",
        (left, kind, right)).fetchone()
    assert row is not None, (left, right)
    return row


# (案例, 左实体, tconst, 预期档, 预期原因, 预期硬冲突)
IMDB_CASES = [
    ("three_editions_dvd", f"{A}A01", "tt0000101", "auto", None, []),
    ("three_editions_bluray", f"{A}A02", "tt0000101", "auto", None, []),
    ("three_editions_extended_runtime_plus_40pct", f"{A}A03", "tt0000101", "auto", None, []),
    ("remake_1960_matches_1960", f"{A}B01", "tt0000102", "auto", None, []),
    ("remake_1960_vs_1983_vetoed", f"{A}B01", "tt0000103", "reject", "director_disjoint",
     ["director_disjoint", "year_conflict"]),
    ("remake_1983_matches_1983", f"{A}B02", "tt0000103", "auto", None, []),
    ("remake_1983_vs_1960_vetoed", f"{A}B02", "tt0000102", "reject", "director_disjoint",
     ["director_disjoint", "year_conflict"]),
    ("rocky_matches_rocky", f"{A}D01", "tt0000104", "auto", None, []),
    ("rocky_ii_matches_rocky_ii", f"{A}D02", "tt0000105", "auto", None, []),
    ("tie_first", f"{A}H01", "tt0000106", "review", "tie", []),
    ("tie_second", f"{A}H01", "tt0000107", "review", "tie", []),
    ("title_only", f"{A}I01", "tt0000109", "reject", None, []),
    ("spanish_version_same_work", f"{A}J01", "tt0000101", "auto", None, []),
    ("alias_only_match", f"{A}S01", "tt0000102", "auto", None, []),
]


@pytest.mark.parametrize("case, left, tconst, tier, reason, veto", IMDB_CASES, ids=[c[0] for c in IMDB_CASES])
def test_decision_imdb_cases(conn, world, case, left, tconst, tier, reason, veto):
    row = decision(conn, left, tconst)
    assert row[0] == tier and row[1] == reason and sorted(row[2]) == sorted(veto)


def test_decision_alias_match_is_generator_g2_and_scores_below_primary(conn, world):
    row = decision(conn, f"{A}S01", "tt0000102")
    assert row[7] == "G2" and row[8]["title"]["score"] == 1
    assert decision(conn, f"{A}B01", "tt0000102")[7] == "G1"


# (左, 右, 档, 原因)
PAIR_CASES = [
    ("same_work_editions_are_one_imdb_work", f"{A}A01", f"{A}A02", "auto", "same_imdb_work"),
    ("extended_vs_dvd_still_same_work", f"{A}A01", f"{A}A03", "auto", "same_imdb_work"),
    ("spanish_vs_english", f"{A}A01", f"{A}J01", "auto", "same_imdb_work"),
    ("remakes_hard_conflict", f"{A}B01", f"{A}B02", "reject", "different_external_id"),
    ("animated_vs_live_action", f"{A}C01", f"{A}C02", "reject", None),
    ("no_year_same_director", f"{A}E01", f"{A}E02", "review", "no_title_year"),
    ("same_title_year_same_director", f"{A}F01", f"{A}F02", "auto", "page_cluster"),
]


@pytest.mark.parametrize("case, left, right, tier, reason", PAIR_CASES, ids=[c[0] for c in PAIR_CASES])
def test_decision_pair_cases(conn, world, case, left, right, tier, reason):
    row = decision(conn, left, right, "product")
    assert (row[0], row[1], row[7]) == (tier, reason, "G3")


def test_decision_remake_pair_records_hard_conflicts(conn, world):
    veto = decision(conn, f"{A}B01", f"{A}B02", "product")[2]
    assert {"director_disjoint", "year_conflict"} <= set(veto)


def test_decision_sequel_titles_are_not_candidates_of_each_other(conn, world):
    assert conn.execute("SELECT count(*) FROM task4.candidate WHERE left_entity = %s AND right_id = 'tt0000105'",
                        (f"{A}D01",)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task4.candidate WHERE left_entity = %s AND right_id = 'tt0000104'",
                        (f"{A}D02",)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task4.candidate WHERE right_kind = 'product' AND "
                        "(left_entity = %s OR right_id = %s)", (f"{A}D01", f"{A}D01")).fetchone()[0] == 0


def test_decision_tv_series_only_match_is_not_linked_and_flagged(conn, world):
    rows = conn.execute("SELECT c.generator, c.right_id, d.tier, d.reason FROM task4.candidate c "
                        "JOIN task4.merge_decision d USING (candidate_id) WHERE c.left_entity = %s",
                        (f"{A}G01",)).fetchall()
    assert rows == [("G4", "tt0000108", "review", "non_movie_title_type")]
    assert candidates.auto_links(conn, world[1], [f"{A}G01"]) == {}


def test_decision_title_only_pair_and_year_alone_never_auto(conn, world):
    tiers = {r[0] for r in conn.execute(
        "SELECT d.tier FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id) "
        "WHERE c.left_entity = ANY(%s)", ([f"{A}I01", f"{A}C01"],)).fetchall()}
    assert "auto" not in tiers


def test_decision_every_candidate_has_one_decision_with_score_confidence_thresholds_and_rule_version(conn, world):
    total = conn.execute("SELECT count(*) FROM task4.candidate").fetchone()[0]
    assert conn.execute("SELECT count(*) FROM task4.merge_decision").fetchone()[0] == total > 20
    assert conn.execute("SELECT count(*) FROM task4.merge_decision WHERE rule_version <> 't4r1' OR run_id IS NULL "
                        "OR decided_by <> 'system' OR features IS NULL OR thresholds->>'auto_threshold' IS NULL "
                        "OR confidence IS NULL").fetchone()[0] == 0
    # 未采用（reject）的候选同样有决策，并且带所用特征
    assert conn.execute("SELECT count(*) FROM task4.merge_decision WHERE tier = 'reject' AND "
                        "features ? 'director'").fetchone()[0] > 0
    row = decision(conn, f"{A}A01", "tt0000101")
    f = row[8]
    assert set(f) >= {"title", "director", "actor", "runtime", "year", "genre", "evidence"}
    assert f["director"]["value"] == "strict" and f["director"]["overlap"] == ["michael curtiz"]
    ev = f["evidence"]
    assert ev["imdb"]["title"] == {"file": "title.basics.tsv.gz", "line_no": 2} and ev["imdb_source_record_id"]
    assert ev["page"]["director"] and ev["page"]["title"]            # 页面证据是字段断言编号
    assert float(row[4]) == round(float(row[3]) / 11, 2)


def test_decision_auto_always_satisfies_the_no_auto_without_people_or_year_rule(conn, world):
    rows = conn.execute(
        "SELECT d.features, d.veto, d.score, c.right_kind, d.reason FROM task4.merge_decision d "
        "JOIN task4.candidate c USING (candidate_id) WHERE d.tier = 'auto'").fetchall()
    assert len(rows) > 10
    for features, veto, score, kind, reason in rows:
        assert veto == []
        if kind == "imdb":
            assert features["director"]["value"] == "strict" or features["actor"]["value"].startswith("overlap")
            assert features["evidence"]["start_year"] is not None
            assert score >= 6
        elif reason == "page_cluster":
            assert features["year"]["value"] == "match" and features["director"]["value"] == "strict"


def test_decision_each_product_has_at_most_one_auto_imdb_link(conn, world):
    rows = conn.execute("SELECT c.left_entity, count(*) FROM task4.merge_decision d JOIN task4.candidate c "
                        "USING (candidate_id) WHERE d.tier = 'auto' AND c.right_kind = 'imdb' "
                        "GROUP BY 1 HAVING count(*) > 1").fetchall()
    assert rows == []


def test_decision_candidate_overflow_is_recorded_and_limits_to_review(conn, make_settings, tmp_path):
    rules = DEFAULT_RULES.with_version("t4r1", imdb_candidate_cap=1)
    fx, ctx = run_decisions(conn, make_settings, tmp_path, rules=rules)
    o = conn.execute("SELECT entity_id, generator, candidate_total, kept FROM task4.candidate_overflow "
                     "WHERE generator = 'G1/G2'").fetchall()
    assert {r[0] for r in o} >= {f"{A}H01"} and all(r[2] > r[3] == 1 for r in o)
    assert conn.execute("SELECT count(*) FROM task4.quality_issue WHERE issue_code = 'candidate_overflow'"
                        ).fetchone()[0] >= 1
    rows = conn.execute("SELECT d.tier, d.reason FROM task4.merge_decision d JOIN task4.candidate c USING "
                        "(candidate_id) WHERE c.left_entity = %s", (f"{A}H01",)).fetchall()
    assert len(rows) == 1 and rows[0] == ("review", "candidate_overflow")


def test_decision_block_above_cap_is_recorded_and_review_only(conn, make_settings, tmp_path):
    rules = DEFAULT_RULES.with_version("t4r1", block_cap=3)         # casablanca 块有 4 个商品
    run_decisions(conn, make_settings, tmp_path, rules=rules)
    assert conn.execute("SELECT generator FROM task4.candidate_overflow WHERE entity_id = 'block:casablanca'"
                        ).fetchall() == [("G3",)]


# ---------------------------------------------------------------- IMDb 标题键与别名（R09）

def test_decision_imdb_title_keys_cover_primary_original_and_aliases_with_region_language_and_line(conn, world):
    rows = conn.execute("SELECT key_source, aka_ordering, title_key, region, language, source_line, title_class "
                        "FROM task4.imdb_title_key WHERE tconst = 'tt0000102' ORDER BY key_source, aka_ordering"
                        ).fetchall()
    assert ("original", 0, "bout de souffle", None, None, 3, "movie") in rows        # basics 第 3 行（表头为第 1 行）
    assert ("aka", 1, "bout de souffle", "FR", "fr", 2, "movie") in rows
    assert ("aka", 3, "sin aliento", "ES", "es", 4, "movie") in rows
    assert ("primary", 0, "breathless", None, None, 3, "movie") in rows
    other = conn.execute("SELECT title_class, title_key FROM task4.imdb_title_key WHERE tconst = 'tt0000108'"
                         ).fetchall()
    assert {r[0] for r in other} == {"other"}


def test_decision_imdb_keys_are_idempotent(conn, world):
    fx, ctx = world
    n = conn.execute("SELECT count(*) FROM task4.imdb_title_key").fetchone()[0]
    assert candidates.build_imdb_keys(conn, ctx).keys == 0
    assert conn.execute("SELECT count(*) FROM task4.imdb_title_key").fetchone()[0] == n


def test_decision_stage_reruns_add_nothing(conn, world):
    fx, ctx = world
    before = [conn.execute(f"SELECT count(*) FROM task4.{t}").fetchone()[0]
              for t in ("candidate", "merge_decision", "entity_stage")]
    assert candidates.decide_entities(conn, ctx, batch_size=3).entities == 0
    assert candidates.decide_pairs(conn, ctx, batch_size=3).blocks == 0
    assert [conn.execute(f"SELECT count(*) FROM task4.{t}").fetchone()[0]
            for t in ("candidate", "merge_decision", "entity_stage")] == before


def test_decision_stage_is_resumable_after_a_failed_batch(conn, make_settings, tmp_path):
    from etl_task4 import editions
    from etl_task4.run import start_run
    from _task4_helpers import build, inputs_for, load_imdb, reset_task4
    from task4 import imdb_files as ifx

    fx = build(conn, make_settings, tmp_path, match_products())
    reset_task4(conn)
    ctx = start_run(conn, inputs_for(fx, load_imdb(conn, tmp_path, ifx.MATCH)))
    editions.build_editions(conn, ctx)
    editions.normalize_extraction(conn, ctx, batch_size=100)
    candidates.build_imdb_keys(conn, ctx)

    def boom(stage, batch_no):
        if batch_no == 2:
            raise RuntimeError("injected")

    with pytest.raises(RuntimeError):
        candidates.decide_entities(conn, ctx, batch_size=4, before_commit=boom)
    done = conn.execute("SELECT count(*) FROM task4.entity_stage WHERE stage = 'decide'").fetchone()[0]
    assert done == 4
    lefts = {r[0] for r in conn.execute("SELECT DISTINCT c.left_entity FROM task4.candidate c").fetchall()}
    marked = {r[0] for r in conn.execute("SELECT entity_id FROM task4.entity_stage WHERE stage = 'decide'").fetchall()}
    assert lefts <= marked                                    # 第 2 批没有部分行
    stats = candidates.decide_entities(conn, ctx, batch_size=4)
    assert stats.remaining == 0 and conn.execute("SELECT count(*) FROM task4.entity_stage WHERE stage = 'decide'"
                                                 ).fetchone()[0] == done + stats.entities
    candidates.decide_pairs(conn, ctx, batch_size=2)
    assert conn.execute("SELECT count(*) FROM task4.merge_decision").fetchone()[0] == conn.execute(
        "SELECT count(*) FROM task4.candidate").fetchone()[0]


# ---------------------------------------------------------------- AC06：合集成员关联

def test_collection_members_are_matched_individually_to_imdb_works(conn, world):
    assert decision(conn, f"{A}K01#1", "tt0000110")[0] == "auto"
    assert decision(conn, f"{A}K01#2", "tt0000111")[0] == "auto"
    assert decision(conn, f"{A}K03#1", "tt0000110")[0] == "auto"
    assert decision(conn, f"{A}K03#2", "tt0000111")[0] == "auto"
    # 合集成员的片长不计分，合集页面的人物用于比较
    assert decision(conn, f"{A}K01#1", "tt0000110")[8]["runtime"]["value"] in ("missing", "not_scored")
    assert candidates.auto_links(conn, world[1], [f"{A}K01#1", f"{A}K01#2"]) == {
        f"{A}K01#1": "tt0000110", f"{A}K01#2": "tt0000111"}


def test_collection_members_without_imdb_match_and_unsplit_collection_have_no_candidates(conn, world):
    members = conn.execute("SELECT DISTINCT entity_id FROM task4.field_assertion WHERE entity_id LIKE %s ORDER BY 1",
                           (f"{A}K03#%",)).fetchall()
    assert [m[0] for m in members] == [f"{A}K03#{k}" for k in (1, 2, 3, 4)]
    for k in (3, 4):
        assert conn.execute("SELECT count(*) FROM task4.candidate WHERE left_entity = %s",
                            (f"{A}K03#{k}",)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM task4.candidate WHERE left_entity LIKE %s",
                        (f"{A}K02%",)).fetchone()[0] == 0
    # 合集商品本身不是候选的左实体，只有成员是
    assert conn.execute("SELECT count(*) FROM task4.candidate WHERE left_entity IN (%s, %s, %s)",
                        (f"{A}K01", f"{A}K02", f"{A}K03")).fetchone()[0] == 0
