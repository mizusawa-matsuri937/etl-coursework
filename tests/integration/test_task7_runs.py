"""任务 7 运行语义（spec AC11：复用、按依赖链增量重算与版本差异；AC12：分批事务恢复与并发咨询锁；AC17：多运行共存）。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_runs.py
"""

from __future__ import annotations

import psycopg
import pytest

import _task7_helpers as h
from _task7_worlds import small_world
from etl_task1.db import connect
from etl_task7 import check, query
from etl_task7.rules import DEFAULT_RULES, RULESETS
from etl_task7.run import RunBusy, apply_task7_schema, extraction_lock
from etl_task7.runner import VerificationFailed, execute_conflict
from etl_task7.versions import diff_versions
from task7 import world as w

pytestmark = pytest.mark.integration

STAGES = ("edition_resolve", "link_assess", "movie_scalar", "movie_multivalue")
TABLES = ("field_candidate", "edition_stage_result", "link_assessment", "shared_tconst_result",
          "movie_scalar_stage_result", "runtime_diff", "movie_title_alias", "movie_multivalue_stage_result",
          "movie_genre", "movie_genre_term", "credit_alignment", "movie_person_resolved", "imdb_person_ext",
          "movie_resolved", "edition_resolved", "movie_field_selection", "edition_field_selection",
          "shared_tconst_conflict", "entity_stage", "quality_issue", "conflict_version")


def q1(conn, sql, *a):
    return conn.execute(sql, a).fetchone()[0]


def row_counts(conn: psycopg.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM task7.{t}").fetchone()[0] for t in TABLES}


def digest(conn: psycopg.Connection, cv: str) -> dict[str, str]:
    """版本结果的内容摘要，不含候选 ID 与运行 ID（分批回滚后序列有间隙，内容必须相同）。"""
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0] or ""      # noqa: E731
    out = {}
    for t, cols in (("movie_field_selection", "movie_id, field_name, status, winning_value, conflict_type, selection_rule, "
                                                "reason, confidence, disagreement_detail"),
                    ("edition_field_selection", "asin, field_name, status, winning_value, conflict_type, selection_rule, "
                                                 "reason, confidence, disagreement_detail"),
                    ("movie_resolved", "movie_id, title, original_title, release_year, runtime_minutes, mpaa_rating, "
                                       "effective_tconst, cross_source_mismatch_halt, cast_coverage, genre_count, "
                                       "director_count, starring_count, cast_count"),
                    ("edition_resolved", "asin, product_kind, movie_ids, edition_title, runtime_minutes, mpaa_rating, "
                                         "publisher, product_release_date, discs, media_format, language"),
                    ("shared_tconst_conflict", "tconst, movie_id, other_movie_id, link_class")):
        out[t] = q(f"SELECT md5(string_agg(x::text, '|' ORDER BY x::text)) FROM (SELECT {cols} FROM task7.{t} "
                   "WHERE conflict_version_id = %s) x", cv)
    keys = conn.execute("SELECT edition_stage_key, link_stage_key, scalar_stage_key, multivalue_stage_key "
                        "FROM task7.conflict_version WHERE conflict_version_id = %s", (cv,)).fetchone()
    out["candidates"] = q("SELECT md5(string_agg(x::text, '|' ORDER BY x::text)) FROM (SELECT entity_type, entity_id, "
                          "field_name, source, source_key, raw_value, normalized_value, status, status_reason "
                          "FROM task7.field_candidate WHERE stage_key = ANY(%s)) x", list(keys))
    for t, cols in (("runtime_diff", "movie_id, asin, diff_class, delta"), ("movie_title_alias", "movie_id, alias_kind, alias"),
                    ("movie_genre", "movie_id, genre, source_coverage, raw_terms"),
                    ("credit_alignment", "movie_id, role, page_person_id, segment, resolved_person_id, level, outcome"),
                    ("movie_person_resolved", "movie_id, person_id, role, source_coverage, status, credit_order, "
                                              "imdb_ordering, nconst, raw_names, resolution_note")):
        out[t] = q(f"SELECT md5(string_agg(x::text, '|' ORDER BY x::text)) FROM (SELECT {cols} FROM task7.{t} "
                   "WHERE stage_key = ANY(%s)) x", list(keys))
    return out


def reset_task7(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA task7 CASCADE")
    conn.execute("DELETE FROM public.schema_migration WHERE version = '601'")
    apply_task7_schema(conn)


def batches(conn: psycopg.Connection, run_id: str) -> dict[str, int]:
    return {s: n for s, n in conn.execute("SELECT stage, count(*) FROM task7.run_batch WHERE run_id = %s GROUP BY 1", (run_id,))}


def with_d3(world: w.World) -> w.World:
    """再加一部页面片长比 IMDb 多 3 分钟的作品：默认规则是 variance_3_5，片长容差放宽到 3 分钟后是 tolerance_1_2。"""
    i, m, e = h.case("D3", "imdb_t4_link", imdb_runtime=100, page_runtime=103)
    return w.World(world.imdb + [i], world.movies + [m], world.editions + [e])


@pytest.fixture
def alt_rules():
    RULESETS["t7r1tol"] = DEFAULT_RULES.with_version("t7r1tol", runtime_tol_small=3)
    RULESETS["t7r1pub"] = DEFAULT_RULES.with_version("t7r1pub", publisher_suffixes=(*DEFAULT_RULES.publisher_suffixes, "records"))
    yield
    RULESETS.pop("t7r1tol", None)
    RULESETS.pop("t7r1pub", None)


def clone_mapping(conn: psycopg.Connection, eid: str) -> dict[str, str]:
    """把 map0002、pmap0001、rv0001 整体复制为 map0003、pmap0002、rv0002（内容相同，版本号不同）。"""
    conn.execute("INSERT INTO task4.mapping_version (mapping_version_id, extraction_id, run_id, input_key, t3_rule_version, "
                 "parser_version, norm_version, rule_version, imdb_snapshot_id, human_set_hash, counts) "
                 "SELECT 'map0003', extraction_id, run_id, 'fix|map0003', t3_rule_version, parser_version, norm_version, "
                 "rule_version, imdb_snapshot_id, human_set_hash, counts FROM task4.mapping_version "
                 "WHERE mapping_version_id = 'map0002'")
    conn.execute("INSERT INTO task4.movie (mapping_version_id, movie_id, status, anchor_key, tconst, display_title) "
                 "SELECT 'map0003', movie_id, status, anchor_key, tconst, display_title FROM task4.movie "
                 "WHERE mapping_version_id = 'map0002'")
    conn.execute("INSERT INTO task4.product_movie (mapping_version_id, asin, member_ordinal, movie_id, relation_type, basis) "
                 "SELECT 'map0003', asin, member_ordinal, movie_id, relation_type, basis FROM task4.product_movie "
                 "WHERE mapping_version_id = 'map0002'")
    conn.execute("INSERT INTO task5.person_mapping_version (person_mapping_version_id, extraction_id, run_id, input_key, "
                 "t4_mapping_version_id, parser_version, norm_version, rule_version, imdb_snapshot_id, human_set_hash, counts) "
                 "SELECT 'pmap0002', extraction_id, run_id, 'fix2', 'map0003', parser_version, norm_version, rule_version, "
                 "imdb_snapshot_id, human_set_hash, counts FROM task5.person_mapping_version WHERE person_mapping_version_id = 'pmap0001'")
    conn.execute("INSERT INTO task5.person (person_mapping_version_id, person_id, status, has_external_id, external_route, "
                 "anchor_key, canonical_name, canonical_source, mention_count, work_count) SELECT 'pmap0002', person_id, status, "
                 "has_external_id, external_route, anchor_key, canonical_name, canonical_source, mention_count, work_count "
                 "FROM task5.person WHERE person_mapping_version_id = 'pmap0001'")
    conn.execute("INSERT INTO task5.person_external_id (person_mapping_version_id, person_id, system, external_id, snapshot_id) "
                 "SELECT 'pmap0002', person_id, system, external_id, snapshot_id FROM task5.person_external_id "
                 "WHERE person_mapping_version_id = 'pmap0001'")
    conn.execute("INSERT INTO task5.person_alias (person_mapping_version_id, person_id, alias, alias_types, source, occurrences) "
                 "SELECT 'pmap0002', person_id, alias, alias_types, source, occurrences FROM task5.person_alias "
                 "WHERE person_mapping_version_id = 'pmap0001'")
    conn.execute("INSERT INTO task5.mention_person (person_mapping_version_id, extraction_id, parser_version, norm_version, "
                 "mention_key, person_id, group_key, attribution, basis) SELECT 'pmap0002', extraction_id, parser_version, "
                 "norm_version, mention_key, person_id, group_key, attribution, basis FROM task5.mention_person "
                 "WHERE person_mapping_version_id = 'pmap0001'")
    conn.execute("INSERT INTO task5.movie_person (person_mapping_version_id, movie_id, person_id, role, credit_order, "
                 "order_conflict, source_labels, mention_count) SELECT 'pmap0002', movie_id, person_id, role, credit_order, "
                 "order_conflict, source_labels, mention_count FROM task5.movie_person WHERE person_mapping_version_id = 'pmap0001'")
    conn.execute("INSERT INTO task5.movie_person_source (person_mapping_version_id, movie_id, person_id, role, mention_key, "
                 "asin, byline_rank, list_position) SELECT 'pmap0002', movie_id, person_id, role, mention_key, asin, "
                 "byline_rank, list_position FROM task5.movie_person_source WHERE person_mapping_version_id = 'pmap0001'")
    conn.execute("INSERT INTO task6.run (run_id, kind, extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, "
                 "parser_version, rule_version, imdb_snapshot_id, input_key, status, finished_at) SELECT 't6fix2', kind, "
                 "extraction_id, 'map0003', 'pmap0002', parser_version, rule_version, imdb_snapshot_id, 'fix2', 'succeeded', now() "
                 "FROM task6.run WHERE run_id = 't6fix'")
    conn.execute("INSERT INTO task6.release_version (release_version_id, extraction_id, run_id, input_key, "
                 "t4_mapping_version_id, t5_person_mapping_version_id, parser_version, rule_version, imdb_snapshot_id, counts) "
                 "SELECT 'rv0002', extraction_id, 't6fix2', 'fix|rv0002', 'map0003', 'pmap0002', parser_version, rule_version, "
                 "imdb_snapshot_id, counts FROM task6.release_version WHERE release_version_id = 'rv0001'")
    conn.execute("INSERT INTO task6.release_selection (release_version_id, movie_id, status, missing_reason, release_year, "
                 "precision, release_type, regional_release_missing_reason, selected_candidate_id, considered_candidate_ids, "
                 "selection_rule, reason, confidence) SELECT 'rv0002', movie_id, status, missing_reason, release_year, precision, "
                 "release_type, regional_release_missing_reason, selected_candidate_id, considered_candidate_ids, selection_rule, "
                 "reason, confidence FROM task6.release_selection WHERE release_version_id = 'rv0001'")
    return dict(t4_mapping_version_id="map0003", t5_person_mapping_version_id="pmap0002", t6_release_version_id="rv0002")


# ------------------------------------------------------------------ AC01 补完：movie_resolved、edition_resolved 物化

def test_ac01_every_movie_and_matched_edition_is_materialized_exactly_once(conn):
    from test_task7_movies_scalar import _scope_world
    world = _scope_world(decoys=True)
    world.imdb[0].directors = (("nm8000001", "Someone Director"),)                  # 输入检查要求快照里有人名
    s = h.seed(conn, world)
    r = execute_conflict(conn, s.inputs(), batch_size=2)
    cv = r.conflict_version_id
    movies = conn.execute("SELECT movie_id, effective_tconst, tconst_source FROM task7.movie_resolved WHERE conflict_version_id = %s "
                          "ORDER BY movie_id", (cv,)).fetchall()
    names = ("Verified", "Hinted", "Cooccur", "PageOnly", "Unknown", "MemberOnly")
    assert sorted(m[0] for m in movies) == sorted(s.mid(n) for n in names)           # 含只经 contains 出现的成员
    src = {m[0]: m[2] for m in movies}
    assert (src[s.mid("Verified")], src[s.mid("Hinted")], src[s.mid("Cooccur")], src[s.mid("PageOnly")]) == \
        ("imdb_t4_link", "imdb_t5_hint", "imdb_t6_cooccur", None)
    eds = {a: ids for a, ids in conn.execute("SELECT asin, movie_ids FROM task7.edition_resolved WHERE conflict_version_id = %s", (cv,))}
    assert set(eds) == {"B0V", "B0H", "B0C", "B0P", "B0U", "B0COL", "B0EMPTY"}       # tv_series 的 B0TV 不进入
    assert eds["B0EMPTY"] == [] and eds["B0COL"] == [s.mid("MemberOnly")]            # 无关联作品的合集 movie_ids = '{}'
    assert q1(conn, "SELECT count(*) FROM task7.movie_field_selection WHERE conflict_version_id = %s", cv) == 9 * len(names)
    assert q1(conn, "SELECT count(*) FROM task7.edition_field_selection WHERE conflict_version_id = %s", cv) == 6 * len(eds)
    assert check.problems(check.verify(conn, cv)) == []


# ------------------------------------------------------------------ AC11 版本、阶段依赖、复用与差异

def test_ac11_second_run_is_reused_and_changes_no_business_row(conn):
    s = h.seed(conn, small_world())
    first = execute_conflict(conn, s.inputs(), batch_size=2)
    assert first.status == "succeeded" and first.conflict_version_id == "cv0001"
    assert conn.execute("SELECT status FROM task7.conflict_version").fetchone()[0] == "verified"
    before, runs = row_counts(conn), conn.execute("SELECT count(*) FROM task7.run").fetchone()[0]
    second = execute_conflict(conn, s.inputs(), batch_size=2)
    assert second.status == "reused" and second.conflict_version_id == "cv0001"
    assert row_counts(conn) == before                                           # 业务行不变
    assert conn.execute("SELECT count(*) FROM task7.run").fetchone()[0] == runs + 1
    status, counts = conn.execute("SELECT status, counts FROM task7.run WHERE run_id = %s", (second.run_id,)).fetchone()
    assert status == "reused" and counts["reused_run"] == first.run_id
    assert conn.execute("SELECT count(*) FROM task7.run_batch WHERE run_id = %s", (second.run_id,)).fetchone()[0] == 0


def test_ac11_runtime_tolerance_change_recomputes_only_movie_scalar(conn, alt_rules):
    s = h.seed(conn, with_d3(small_world()))
    old = execute_conflict(conn, s.inputs(), batch_size=2)
    new = execute_conflict(conn, s.inputs(rule_version="t7r1tol"), batch_size=2)
    assert new.status == "succeeded" and new.conflict_version_id == "cv0002"
    assert set(batches(conn, new.run_id)) == {"movie_scalar"}                     # 其余阶段键相同，已完成的批次全部跳过
    k1 = conn.execute("SELECT edition_stage_key, link_stage_key, scalar_stage_key, multivalue_stage_key "
                      "FROM task7.conflict_version WHERE conflict_version_id = 'cv0001'").fetchone()
    k2 = conn.execute("SELECT edition_stage_key, link_stage_key, scalar_stage_key, multivalue_stage_key "
                      "FROM task7.conflict_version WHERE conflict_version_id = 'cv0002'").fetchone()
    assert k1[0] == k2[0] and k1[1] == k2[1] and k1[3] == k2[3] and k1[2] != k2[2]
    assert conn.execute("SELECT parent_version_id FROM task7.conflict_version WHERE conflict_version_id = 'cv0002'").fetchone()[0] == "cv0001"
    assert old.conflict_version_id == "cv0001"
    # 差异：D3 的片长冲突类型从 framerate_or_edition 变为 minor_tolerance，旧版本保留
    d3 = s.mid("D3")
    diff = [r for r in diff_versions(conn, "cv0001", "cv0002") if r[1] == d3]
    assert [(r[0], r[2]) for r in diff] == [("movie", "runtime_minutes")]
    assert "冲突类型" in diff[0][5]
    for cv, ctype in (("cv0001", "framerate_or_edition"), ("cv0002", "minor_tolerance")):
        got = conn.execute("SELECT conflict_type FROM task7.movie_field_selection WHERE conflict_version_id = %s "
                           "AND movie_id = %s AND field_name = 'runtime_minutes'", (cv, d3)).fetchone()[0]
        assert got == ctype
    assert query.query_movie(conn, "cv0001", d3)["fields"]["runtime_minutes"]["conflict_type"] == "framerate_or_edition"


def test_ac11_publisher_rule_change_recomputes_every_stage(conn, alt_rules):
    s = h.seed(conn, small_world())
    execute_conflict(conn, s.inputs(), batch_size=2)
    new = execute_conflict(conn, s.inputs(rule_version="t7r1pub"), batch_size=2)
    assert new.status == "succeeded" and new.conflict_version_id == "cv0002"
    assert set(batches(conn, new.run_id)) == set(STAGES)                          # 版本层规则变了，整条链重算
    keys = conn.execute("SELECT edition_stage_key, link_stage_key, scalar_stage_key, multivalue_stage_key "
                        "FROM task7.conflict_version ORDER BY conflict_version_id").fetchall()
    assert all(a != b for a, b in zip(*keys))


def test_ac11_switching_task4_mapping_makes_a_new_version(conn):
    s = h.seed(conn, small_world())
    first = execute_conflict(conn, s.inputs(), batch_size=2)
    over = clone_mapping(conn, s.eid)
    second = execute_conflict(conn, s.inputs(**over), batch_size=2)
    assert second.status == "succeeded" and second.conflict_version_id == "cv0002"
    assert set(batches(conn, second.run_id)) == set(STAGES)
    assert conn.execute("SELECT t4_mapping_version_id FROM task7.conflict_version WHERE conflict_version_id = 'cv0002'").fetchone()[0] == "map0003"
    assert diff_versions(conn, "cv0001", "cv0002") == []                          # 内容相同，差异为空
    # 旧版本仍可查询
    mid = s.mid("S1")
    assert query.query_movie(conn, "cv0001", mid)["found"] and query.query_movie(conn, "cv0002", mid)["found"]
    assert check.problems(check.verify(conn, "cv0001")) == [] and check.problems(check.verify(conn, "cv0002")) == []
    assert first.conflict_version_id == "cv0001"


def test_ac11_report_diff_lists_added_and_removed_entities_with_full_outer_join(conn):
    """版本差异用 FULL OUTER JOIN：一边没有的实体显示为 ABSENT。"""
    s = h.seed(conn, small_world())
    execute_conflict(conn, s.inputs(), batch_size=2)
    over = clone_mapping(conn, s.eid)
    execute_conflict(conn, s.inputs(**over), batch_size=2)
    conn.execute("DELETE FROM task7.movie_field_selection WHERE conflict_version_id = 'cv0002' AND movie_id = %s "
                 "AND field_name = 'title'", (s.mid("S2"),))
    rows = diff_versions(conn, "cv0001", "cv0002")
    assert [(r[0], r[1], r[2], r[4]) for r in rows] == [("movie", s.mid("S2"), "title", "ABSENT")]


# ------------------------------------------------------------------ AC12 分批事务恢复与并发咨询锁

TABLE_OF_STAGE = {"edition_resolve": "edition_stage_result", "link_assess": "link_assessment",
                  "movie_scalar": "movie_scalar_stage_result", "movie_multivalue": "movie_multivalue_stage_result"}


def boom_at(stage: str, batch: int):
    def hook(st: str, no: int) -> None:
        if (st, no) == (stage, batch):
            raise RuntimeError(f"injected failure before commit of {st} batch {no}")
    return hook


@pytest.mark.parametrize("stage", STAGES)
def test_ac12_failure_in_second_batch_keeps_only_first_batch_and_rerun_matches_a_clean_run(conn, stage):
    world = small_world()
    s = h.seed(conn, world)
    clean = execute_conflict(conn, s.inputs(), batch_size=2)
    reference = digest(conn, clean.conflict_version_id)
    reset_task7(conn)                                                             # 只清任务 7（上游断言 ID 不变），同一输入再来一遍
    with pytest.raises(RuntimeError, match="injected failure"):
        execute_conflict(conn, s.inputs(), batch_size=2, before_commit=boom_at(stage, 2))
    i = s.inputs()
    done = {st: conn.execute("SELECT count(*) FROM task7.entity_stage WHERE stage = %s AND stage_key = %s",
                             (st, i.stage_key(st))).fetchone()[0] for st in STAGES}
    assert done[stage] == 2                                                       # 只有第 1 批（2 个实体）落库
    for st in STAGES[:STAGES.index(stage)]:
        assert done[st] > 0                                                       # 上游阶段已完成
    for st in STAGES[STAGES.index(stage) + 1:]:
        assert done[st] == 0                                                      # 下游阶段没有开始
    assert conn.execute(f"SELECT count(*) FROM task7.{TABLE_OF_STAGE[stage]} WHERE stage_key = %s", (i.stage_key(stage),)).fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM task7.conflict_version").fetchone()[0] == 0
    run = conn.execute("SELECT status, failure_reason FROM task7.run").fetchone()
    assert run[0] == "failed" and "injected failure" in run[1]
    again = execute_conflict(conn, s.inputs(), batch_size=2)
    assert again.status == "succeeded" and again.conflict_version_id == "cv0001"
    assert digest(conn, "cv0001") == reference                                    # 与一次跑完一致
    assert check.problems(check.verify(conn, "cv0001")) == []
    resumed = batches(conn, again.run_id)
    # 5 个实体共 3 批：第 1 批已完成，重跑 2 批（link_assess 另有一批共享 tconst 分类）
    assert resumed.get(stage, 0) == 2 + (1 if stage == "link_assess" else 0)


def test_ac12_concurrent_run_fails_deterministically(conn, make_settings):
    s = h.seed(conn, small_world())
    conn2 = connect(make_settings())
    with extraction_lock(conn2, s.eid):
        with pytest.raises(RunBusy):
            execute_conflict(conn, s.inputs(), batch_size=2)
    conn2.close()
    assert execute_conflict(conn, s.inputs(), batch_size=2).status == "succeeded"   # 锁释放后可以运行


# ------------------------------------------------------------------ AC17 多运行、核对失败与未核实版本

def test_ac17_failed_verify_leaves_an_unverified_version_that_is_not_reused_and_is_rewritten(conn):
    s = h.seed(conn, small_world())
    with pytest.raises(VerificationFailed):
        execute_conflict(conn, s.inputs(), batch_size=2, inject_verify_failure=True)
    assert conn.execute("SELECT status FROM task7.conflict_version").fetchone()[0] == "unverified"
    assert conn.execute("SELECT status FROM task7.run").fetchone()[0] == "failed"
    cand_before = conn.execute("SELECT count(*) FROM task7.field_candidate").fetchone()[0]
    again = execute_conflict(conn, s.inputs(), batch_size=2)
    assert again.status == "succeeded" and again.conflict_version_id == "cv0001"   # 不是 reused，原版本上重写
    assert conn.execute("SELECT count(*) FROM task7.conflict_version").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM task7.conflict_version").fetchone()[0] == "verified"
    assert conn.execute("SELECT count(*) FROM task7.field_candidate").fetchone()[0] == cand_before   # 阶段结果保留，没有重算
    assert set(batches(conn, again.run_id)) == set()
    assert check.problems(check.verify(conn, "cv0001")) == []


def test_ac17_two_versions_coexist_without_cross_reading(conn, alt_rules):
    from etl_task7.report import build_summary
    s = h.seed(conn, with_d3(small_world()))
    execute_conflict(conn, s.inputs(), batch_size=2)
    execute_conflict(conn, s.inputs(rule_version="t7r1tol"), batch_size=2)
    cand = {cv: conn.execute("SELECT count(*) FROM task7.field_candidate c JOIN task7.conflict_version v ON "
                             "c.stage_key = v.scalar_stage_key AND c.entity_type = 'movie' WHERE v.conflict_version_id = %s",
                             (cv,)).fetchone()[0] for cv in ("cv0001", "cv0002")}
    for cv in ("cv0001", "cv0002"):
        total = sum(build_summary(conn, cv)["candidate_status"]["movie"].values())
        assert total == cand[cv] and total > 0                                    # 各版本只数自己阶段键下的候选
    assert build_summary(conn, "cv0001")["runtime_diff"]["by_class"] != build_summary(conn, "cv0002")["runtime_diff"]["by_class"]
