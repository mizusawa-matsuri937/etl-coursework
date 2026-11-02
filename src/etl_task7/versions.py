"""冲突消解版本的物化与差异（spec R50、R55）。

finalize_version 在一个事务里把四个阶段的结果物化为版本表：movie_resolved、edition_resolved、movie_field_selection、
edition_field_selection、shared_tconst_conflict，版本状态为 unverified；verify 通过才由 mark_verified 把版本标为 verified、
运行标为 succeeded（R55）。同一输入键已有未核实的版本（上次 verify 失败）时，在同一个事务里清掉它的物化行再重写，版本号不变。
候选仍保存在阶段键下的 task7.field_candidate，选择记录引用它们的 ID，所以物化不复制候选。
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import sources
from .run import RunContext, Task7InputError, SHARED_MARK, require_stage_complete

MATERIALIZED = ("movie_field_selection", "edition_field_selection", "movie_resolved", "edition_resolved",
                "shared_tconst_conflict")
SELECTION_SQL = """
INSERT INTO task7.{table} (conflict_version_id, {entity}, field_name, status, winning_value, selected_candidate_ids,
    considered_candidate_ids, conflict_type, selection_rule, reason, confidence, disagreement_detail)
SELECT %(cv)s, r.{entity}, f.key, f.value->>'status', f.value->>'winning_value',
       ARRAY(SELECT jsonb_array_elements_text(f.value->'selected_candidate_ids')::bigint),
       ARRAY(SELECT jsonb_array_elements_text(f.value->'considered_candidate_ids')::bigint),
       f.value->>'conflict_type', f.value->>'selection_rule', f.value->>'reason', (f.value->>'confidence')::numeric,
       coalesce(f.value->'disagreement_detail', '{{}}'::jsonb)
FROM task7.{stage_table} r, jsonb_each(r.field_selections) f
WHERE r.extraction_id = %(eid)s AND r.stage_key = %(key)s
"""


def next_version_id(conn: psycopg.Connection) -> str:
    n = conn.execute("SELECT count(*) FROM task7.conflict_version").fetchone()[0]
    return f"cv{n + 1:04d}"


def _require_all_stages(conn: psycopg.Connection, ctx: RunContext) -> None:
    i = ctx.inputs
    asins = [a for a, _ in sources.matched_editions(conn, i)]
    movies = sources.movie_ids(conn, i.t4_mapping_version_id)
    require_stage_complete(conn, ctx, "edition_resolve", asins)
    require_stage_complete(conn, ctx, "link_assess", movies)
    require_stage_complete(conn, ctx, "movie_scalar", movies)
    require_stage_complete(conn, ctx, "movie_multivalue", movies)
    require_stage_complete(conn, ctx, "link_assess", [SHARED_MARK])


def finalize_version(conn: psycopg.Connection, ctx: RunContext) -> tuple[str, dict[str, Any]]:
    """R50、R55：单事务物化版本（状态 unverified）。返回 (冲突消解版本号, 物化行数)。
    上游阶段有未处理实体时确定性失败，不物化残缺版本。"""
    i = ctx.inputs
    _require_all_stages(conn, ctx)
    keys = {s: i.stage_key(s) for s in ("edition_resolve", "link_assess", "movie_scalar", "movie_multivalue")}
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task7_versions'))")
        existing = conn.execute("SELECT conflict_version_id, status FROM task7.conflict_version "
                                "WHERE extraction_id = %s AND input_key = %s", (i.extraction_id, i.key())).fetchone()
        if existing is not None and existing[1] == "verified":
            raise Task7InputError(f"版本 {existing[0]} 已通过核对，应当复用而不是重写")
        if existing is not None:      # 上次 verify 失败留下的未核实版本：同事务清掉物化行后重写
            cv = existing[0]
            for t in MATERIALIZED:
                conn.execute(f"DELETE FROM task7.{t} WHERE conflict_version_id = %s", (cv,))
            conn.execute("UPDATE task7.conflict_version SET run_id = %s, created_at = clock_timestamp() "
                         "WHERE conflict_version_id = %s", (ctx.run_id, cv))
        else:
            cv = next_version_id(conn)
            parent = conn.execute("SELECT conflict_version_id FROM task7.conflict_version WHERE extraction_id = %s "
                                  "ORDER BY created_at DESC, conflict_version_id DESC LIMIT 1",
                                  (i.extraction_id,)).fetchone()
            conn.execute(
                "INSERT INTO task7.conflict_version (conflict_version_id, extraction_id, parent_version_id, run_id, "
                "input_key, t4_mapping_version_id, t5_person_mapping_version_id, t6_release_version_id, parser_version, "
                "norm_version, imdb_snapshot_id, rule_version, edition_stage_key, link_stage_key, scalar_stage_key, "
                "multivalue_stage_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (cv, i.extraction_id, parent[0] if parent else None, ctx.run_id, i.key(), i.t4_mapping_version_id,
                 i.t5_person_mapping_version_id, i.t6_release_version_id, i.parser_version, i.norm_version,
                 i.imdb_snapshot_id, i.rule_version, keys["edition_resolve"], keys["link_assess"],
                 keys["movie_scalar"], keys["movie_multivalue"]))
        p = {"cv": cv, "eid": i.extraction_id}
        n: dict[str, int] = {}
        n["movie_resolved"] = conn.execute(
            "INSERT INTO task7.movie_resolved (conflict_version_id, movie_id, title, original_title, release_year, "
            "release_precision, runtime_minutes, mpaa_rating, effective_tconst, tconst_source, "
            "cross_source_mismatch_halt, cast_coverage, inferred_upper_bound, inferred_bound_precision, "
            "inferred_bound_evidence, genre_count, director_count, starring_count, cast_count) "
            "SELECT %(cv)s, s.movie_id, s.title, s.original_title, s.release_year, s.release_precision, "
            "s.runtime_minutes, s.mpaa_rating, s.effective_tconst, s.tconst_source, s.cross_source_mismatch_halt, "
            "m.cast_coverage, s.inferred_upper_bound, s.inferred_bound_precision, s.inferred_bound_evidence, "
            "m.genre_count, m.director_count, m.starring_count, m.cast_count "
            "FROM task7.movie_scalar_stage_result s JOIN task7.movie_multivalue_stage_result m "
            "ON m.extraction_id = s.extraction_id AND m.movie_id = s.movie_id AND m.stage_key = %(mkey)s "
            "WHERE s.extraction_id = %(eid)s AND s.stage_key = %(skey)s",
            {**p, "skey": keys["movie_scalar"], "mkey": keys["movie_multivalue"]}).rowcount
        n["edition_resolved"] = conn.execute(
            "INSERT INTO task7.edition_resolved (conflict_version_id, asin, product_kind, movie_ids, edition_title, "
            "edition_clean_title, runtime_minutes, mpaa_rating, publisher, product_release_date, media_release_date, "
            "listing_first_available_date, date_flags, discs, media_format, language, subtitles, aspect_ratio, region, "
            "cut_type, video_system, attributes_detail) "
            "SELECT %(cv)s, asin, product_kind, movie_ids, edition_title, edition_clean_title, runtime_minutes, "
            "mpaa_rating, publisher, product_release_date, media_release_date, listing_first_available_date, "
            "date_flags, discs, media_format, language, subtitles, aspect_ratio, region, cut_type, video_system, "
            "attributes_detail FROM task7.edition_stage_result WHERE extraction_id = %(eid)s AND stage_key = %(key)s",
            {**p, "key": keys["edition_resolve"]}).rowcount
        n["movie_field_selection"] = conn.execute(
            SELECTION_SQL.format(table="movie_field_selection", entity="movie_id",
                                 stage_table="movie_scalar_stage_result"),
            {**p, "key": keys["movie_scalar"]}).rowcount
        n["movie_field_selection"] += conn.execute(
            SELECTION_SQL.format(table="movie_field_selection", entity="movie_id",
                                 stage_table="movie_multivalue_stage_result"),
            {**p, "key": keys["movie_multivalue"]}).rowcount
        n["edition_field_selection"] = conn.execute(
            SELECTION_SQL.format(table="edition_field_selection", entity="asin", stage_table="edition_stage_result"),
            {**p, "key": keys["edition_resolve"]}).rowcount
        n["shared_tconst_conflict"] = conn.execute(
            "INSERT INTO task7.shared_tconst_conflict (conflict_version_id, tconst, movie_id, other_movie_id, "
            "link_class, basis, source_issue_ids) SELECT %(cv)s, tconst, movie_id, other_movie_id, link_class, basis, "
            "source_issue_ids FROM task7.shared_tconst_result WHERE extraction_id = %(eid)s AND stage_key = %(key)s",
            {**p, "key": keys["link_assess"]}).rowcount
        conn.execute("UPDATE task7.conflict_version SET counts = %s WHERE conflict_version_id = %s", (Jsonb(n), cv))
        conn.execute("UPDATE task7.run SET conflict_version_id = %s WHERE run_id = %s", (cv, ctx.run_id))
    return cv, n


def mark_verified(conn: psycopg.Connection, cv: str, run_id: str, counts: dict[str, Any]) -> None:
    """R55：verify 通过后单事务把版本标为 verified、运行标为 succeeded。"""
    with conn.transaction():
        conn.execute("UPDATE task7.conflict_version SET status = 'verified', verified_at = clock_timestamp() "
                     "WHERE conflict_version_id = %s", (cv,))
        conn.execute("UPDATE task7.run SET status = 'succeeded', counts = %s, finished_at = clock_timestamp(), "
                     "failure_reason = NULL, conflict_version_id = %s WHERE run_id = %s",
                     (Jsonb(counts), cv, run_id))


# ---------------------------------------------------------------- 版本差异（R50）

def diff_versions(conn: psycopg.Connection, old_cv: str, new_cv: str) -> list[tuple[str, str, str, str, str, str]]:
    """两个冲突消解版本的差异：(层, 实体, 字段, 旧, 新, 说明)，用 FULL OUTER JOIN 列出新增、移除与字段状态、值、规则的变化。
    作品层与版本层各一次；多值字段的成员明细不在这里比较，只比较它们的摘要值。"""
    out: list[tuple[str, str, str, str, str, str]] = []
    for layer, table, entity in (("movie", "movie_field_selection", "movie_id"),
                                 ("edition", "edition_field_selection", "asin")):
        for ent, field, o_st, o_v, o_rule, o_ct, n_st, n_v, n_rule, n_ct in conn.execute(
                f"SELECT coalesce(o.{entity}, n.{entity}), coalesce(o.field_name, n.field_name), o.status, "
                "o.winning_value, o.selection_rule, o.conflict_type, n.status, n.winning_value, n.selection_rule, "
                f"n.conflict_type FROM (SELECT * FROM task7.{table} WHERE conflict_version_id = %s) o "
                f"FULL OUTER JOIN (SELECT * FROM task7.{table} WHERE conflict_version_id = %s) n "
                f"ON n.{entity} = o.{entity} AND n.field_name = o.field_name "
                "WHERE o.field_name IS NULL OR n.field_name IS NULL OR o.status IS DISTINCT FROM n.status "
                "OR o.winning_value IS DISTINCT FROM n.winning_value OR o.conflict_type IS DISTINCT FROM n.conflict_type "
                "OR o.selection_rule IS DISTINCT FROM n.selection_rule ORDER BY 1, 2", (old_cv, new_cv)).fetchall():
            if o_st is None:
                out.append((layer, ent, field, "ABSENT", f"{n_st}({n_v})", f"新增（{n_ct}）"))
            elif n_st is None:
                out.append((layer, ent, field, f"{o_st}({o_v})", "ABSENT", f"移除（{o_ct}）"))
            else:
                what = [w for w, a, b in (("状态", o_st, n_st), ("值", o_v, n_v), ("冲突类型", o_ct, n_ct),
                                           ("规则", o_rule, n_rule)) if a != b]
                out.append((layer, ent, field, f"{o_st}({o_v})", f"{n_st}({n_v})", "、".join(what) + "变化"))
    return out
