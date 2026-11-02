"""任务 7 只读核对（spec AC17、AC18；与 DAG 的 verify 步骤共用 verify 与 problems）。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task7.check                              # 默认抽取的最新冲突消解版本
    python -m etl_task7.check --cv cv0001
输出一段 JSON；全部一致时退出码 0，否则 1。整个核对在只读事务里进行，不写任何表。
命令行默认要求版本已 verified；DAG 的 verify 步骤在标记 verified 之前调用，所以用 expect_verified=False。
核对覆盖：作品与商品的记录覆盖、字段选择记录的完整与互斥、采用候选有效且为 adopted、孤儿候选、状态不一致、多值字段的成员与来源、
物化表与选择记录一致、熔断作品不含 IMDb 成员、阶段键与版本登记一致、运行与版本状态一致，以及任务 1–6 表行数快照。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import psycopg

from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction

from . import sources
from .rules import CONTROLLED_GENRES
from .run import RunInputs

MOVIE_SCALAR = ("title", "original_title", "release_year", "runtime_minutes", "mpaa_rating")
MOVIE_MULTI = ("genres", "director", "starring", "cast")
EDITION_FIELDS = ("edition_title", "runtime_minutes", "mpaa_rating", "publisher", "product_release_date", "discs")

UPSTREAM_TABLES = (
    ("public", "review_record"), ("task2", "product_manifest"), ("task3", "page_field"), ("task4", "movie"),
    ("task4", "product_movie"), ("task4", "field_assertion"), ("task5", "person"), ("task5", "movie_person"),
    ("task6", "release_selection"), ("task6", "release_candidate"), ("task6", "product_release_date"),
    ("imdb", "title"), ("imdb", "name"),
)


def latest_cv(conn: psycopg.Connection, extraction_id: str) -> str:
    row = conn.execute("SELECT conflict_version_id FROM task7.conflict_version WHERE extraction_id = %s "
                       "ORDER BY created_at DESC, conflict_version_id DESC LIMIT 1", (extraction_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"抽取 {extraction_id} 还没有冲突消解版本")
    return row[0]


def cv_row(conn: psycopg.Connection, cv: str) -> dict[str, Any]:
    cols = ("conflict_version_id", "extraction_id", "parent_version_id", "run_id", "input_key", "t4_mapping_version_id",
            "t5_person_mapping_version_id", "t6_release_version_id", "parser_version", "norm_version",
            "imdb_snapshot_id", "rule_version", "edition_stage_key", "link_stage_key", "scalar_stage_key",
            "multivalue_stage_key", "status", "counts", "created_at", "verified_at")
    r = conn.execute(f"SELECT {', '.join(cols)} FROM task7.conflict_version WHERE conflict_version_id = %s",
                     (cv,)).fetchone()
    if r is None:
        raise ValueError(f"冲突消解版本 {cv} 不存在")
    d = dict(zip(cols, r))
    d["counts"] = dict(d["counts"])
    for k in ("created_at", "verified_at"):
        d[k] = d[k].isoformat() if d[k] else None
    return d


def inputs_of(row: dict[str, Any]) -> RunInputs:
    return RunInputs(extraction_id=row["extraction_id"], t4_mapping_version_id=row["t4_mapping_version_id"],
                     t5_person_mapping_version_id=row["t5_person_mapping_version_id"],
                     t6_release_version_id=row["t6_release_version_id"], parser_version=row["parser_version"],
                     norm_version=row["norm_version"], imdb_snapshot_id=row["imdb_snapshot_id"],
                     rule_version=row["rule_version"])


def verify(conn: psycopg.Connection, cv: str) -> dict[str, Any]:
    """只读核对一个冲突消解版本。"""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        return _verify(conn, cv)


def _verify(conn: psycopg.Connection, cv: str) -> dict[str, Any]:
    row = cv_row(conn, cv)
    eid, t4 = row["extraction_id"], row["t4_mapping_version_id"]
    inputs = inputs_of(row)
    keys = {"edition": row["edition_stage_key"], "link": row["link_stage_key"], "scalar": row["scalar_stage_key"],
            "multi": row["multivalue_stage_key"]}
    out: dict[str, Any] = {"conflict_version_id": cv, "extraction_id": eid, "version_status": row["status"],
                           "t4_mapping_version_id": t4, "rule_version": row["rule_version"],
                           "imdb_snapshot_id": row["imdb_snapshot_id"]}
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731

    # 1. 版本登记的阶段键与输入重算一致；运行与版本状态一致
    expected = {"edition": inputs.stage_key("edition_resolve"), "link": inputs.stage_key("link_assess"),
                "scalar": inputs.stage_key("movie_scalar"), "multi": inputs.stage_key("movie_multivalue")}
    out["stage_key_mismatch"] = sum(expected[k] != keys[k] for k in keys)
    out["run_version_mismatch"] = one(
        "SELECT count(*) FROM task7.run r JOIN task7.conflict_version v ON v.conflict_version_id = r.conflict_version_id "
        "WHERE v.conflict_version_id = %s AND r.status IN ('succeeded', 'reused') AND v.status <> 'verified' "
        "AND r.run_id <> %s", cv, row["run_id"])

    # 2. 作品与商品覆盖
    movies = one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s", t4)
    editions = len(sources.matched_editions(conn, inputs))
    out["movies_expected"], out["editions_expected"] = movies, editions
    out["movie_resolved_total"] = one("SELECT count(*) FROM task7.movie_resolved WHERE conflict_version_id = %s", cv)
    out["edition_resolved_total"] = one("SELECT count(*) FROM task7.edition_resolved WHERE conflict_version_id = %s", cv)
    out["movies_missing"] = one(
        "SELECT count(*) FROM task4.movie m LEFT JOIN task7.movie_resolved r ON r.conflict_version_id = %s "
        "AND r.movie_id = m.movie_id WHERE m.mapping_version_id = %s AND r.movie_id IS NULL", cv, t4)
    out["movies_extra"] = one(
        "SELECT count(*) FROM task7.movie_resolved r LEFT JOIN task4.movie m ON m.mapping_version_id = %s "
        "AND m.movie_id = r.movie_id WHERE r.conflict_version_id = %s AND m.movie_id IS NULL", t4, cv)
    out["editions_missing"] = one(
        "SELECT count(*) FROM task4.product_edition e LEFT JOIN task7.edition_resolved r ON r.conflict_version_id = %s "
        "AND r.asin = e.asin WHERE e.extraction_id = %s AND e.t3_rule_version = (SELECT t3_rule_version FROM "
        "task4.mapping_version WHERE mapping_version_id = %s) AND e.match_scope = 'matched' AND r.asin IS NULL",
        cv, eid, t4)
    out["editions_extra"] = one(
        "SELECT count(*) FROM task7.edition_resolved r LEFT JOIN task4.product_edition e ON e.extraction_id = %s "
        "AND e.asin = r.asin AND e.match_scope = 'matched' AND e.t3_rule_version = (SELECT t3_rule_version FROM "
        "task4.mapping_version WHERE mapping_version_id = %s) WHERE r.conflict_version_id = %s AND e.asin IS NULL",
        eid, t4, cv)

    # 3. 选择记录完整：作品 × 9 字段，商品 × 6 字段；状态互斥由 CHECK 保证，这里统计分布
    out["movie_selections_missing"] = one(
        "SELECT count(*) FROM task7.movie_resolved r CROSS JOIN unnest(%s::text[]) f(name) "
        "LEFT JOIN task7.movie_field_selection s ON s.conflict_version_id = r.conflict_version_id "
        "AND s.movie_id = r.movie_id AND s.field_name = f.name WHERE r.conflict_version_id = %s "
        "AND s.field_name IS NULL", list(MOVIE_SCALAR + MOVIE_MULTI), cv)
    out["movie_selections_orphan"] = one(
        "SELECT count(*) FROM task7.movie_field_selection s LEFT JOIN task7.movie_resolved r "
        "ON r.conflict_version_id = s.conflict_version_id AND r.movie_id = s.movie_id "
        "WHERE s.conflict_version_id = %s AND r.movie_id IS NULL", cv)
    out["edition_selections_missing"] = one(
        "SELECT count(*) FROM task7.edition_resolved r CROSS JOIN unnest(%s::text[]) f(name) "
        "LEFT JOIN task7.edition_field_selection s ON s.conflict_version_id = r.conflict_version_id "
        "AND s.asin = r.asin AND s.field_name = f.name WHERE r.conflict_version_id = %s "
        "AND s.field_name IS NULL", list(EDITION_FIELDS), cv)
    out["edition_selections_orphan"] = one(
        "SELECT count(*) FROM task7.edition_field_selection s LEFT JOIN task7.edition_resolved r "
        "ON r.conflict_version_id = s.conflict_version_id AND r.asin = s.asin "
        "WHERE s.conflict_version_id = %s AND r.asin IS NULL", cv)
    out["movie_status_counts"] = {f"{f}:{st}": n for f, st, n in conn.execute(
        "SELECT field_name, status, count(*) FROM task7.movie_field_selection WHERE conflict_version_id = %s "
        "GROUP BY 1, 2 ORDER BY 1, 2", (cv,))}
    out["edition_status_counts"] = {f"{f}:{st}": n for f, st, n in conn.execute(
        "SELECT field_name, status, count(*) FROM task7.edition_field_selection WHERE conflict_version_id = %s "
        "GROUP BY 1, 2 ORDER BY 1, 2", (cv,))}

    # 4. 标量字段：采用候选有效（存在、实体与字段相符、状态 adopted），考虑过的候选存在，非 resolved 字段没有 adopted 候选
    for layer, table, entity, entity_type, fields, key in (
            ("movie", "movie_field_selection", "movie_id", "movie", MOVIE_SCALAR, keys["scalar"]),
            ("edition", "edition_field_selection", "asin", "edition", EDITION_FIELDS, keys["edition"])):
        out[f"{layer}_selected_invalid"] = one(
            f"SELECT count(*) FROM task7.{table} s WHERE s.conflict_version_id = %s AND s.field_name = ANY(%s) "
            "AND s.status = 'resolved' AND EXISTS (SELECT 1 FROM unnest(s.selected_candidate_ids) sid WHERE NOT EXISTS ("
            "SELECT 1 FROM task7.field_candidate c WHERE c.candidate_id = sid AND c.extraction_id = %s "
            f"AND c.stage_key = %s AND c.entity_type = %s AND c.entity_id = s.{entity} AND c.field_name = s.field_name "
            "AND c.status = 'adopted'))", cv, list(fields), eid, key, entity_type)
        out[f"{layer}_considered_invalid"] = one(
            f"SELECT count(*) FROM task7.{table} s WHERE s.conflict_version_id = %s AND s.field_name = ANY(%s) "
            "AND EXISTS (SELECT 1 FROM unnest(s.considered_candidate_ids) sid WHERE NOT EXISTS ("
            "SELECT 1 FROM task7.field_candidate c WHERE c.candidate_id = sid AND c.extraction_id = %s "
            f"AND c.stage_key = %s AND c.entity_type = %s AND c.entity_id = s.{entity} AND c.field_name = s.field_name))",
            cv, list(fields), eid, key, entity_type)
        out[f"{layer}_unresolved_with_adopted"] = one(
            f"SELECT count(*) FROM task7.{table} s WHERE s.conflict_version_id = %s AND s.field_name = ANY(%s) "
            "AND s.status <> 'resolved' AND EXISTS (SELECT 1 FROM task7.field_candidate c WHERE c.candidate_id = "
            "ANY(s.considered_candidate_ids) AND c.status = 'adopted')", cv, list(fields))
        out[f"{layer}_orphan_candidates"] = one(
            "SELECT count(*) FROM task7.field_candidate c WHERE c.extraction_id = %s AND c.stage_key = %s "
            f"AND c.entity_type = %s AND NOT EXISTS (SELECT 1 FROM task7.{table} s WHERE s.conflict_version_id = %s "
            f"AND s.{entity} = c.entity_id AND s.field_name = c.field_name AND c.candidate_id = ANY(s.considered_candidate_ids))",
            eid, key, entity_type, cv)
        out[f"{layer}_selected_value_mismatch"] = one(
            f"SELECT count(*) FROM task7.{table} s WHERE s.conflict_version_id = %s AND s.field_name = ANY(%s) "
            "AND s.status = 'resolved' AND NOT EXISTS (SELECT 1 FROM task7.field_candidate c WHERE c.candidate_id = "
            "ANY(s.selected_candidate_ids) AND (c.normalized_value IS NOT DISTINCT FROM s.winning_value "
            "OR c.raw_value IS NOT DISTINCT FROM s.winning_value))",
            cv, list(fields))

    # 5. 物化表与选择记录一致
    mism = 0
    for col in MOVIE_SCALAR:
        cast = "::integer" if col in ("release_year", "runtime_minutes") else ""
        mism += one(
            f"SELECT count(*) FROM task7.movie_resolved r JOIN task7.movie_field_selection s "
            "ON s.conflict_version_id = r.conflict_version_id AND s.movie_id = r.movie_id AND s.field_name = %s "
            f"WHERE r.conflict_version_id = %s AND r.{col} IS DISTINCT FROM s.winning_value{cast}", col, cv)
    for col in ("runtime_minutes", "mpaa_rating", "publisher", "discs"):
        cast = "::integer" if col in ("runtime_minutes", "discs") else ""
        mism += one(
            f"SELECT count(*) FROM task7.edition_resolved r JOIN task7.edition_field_selection s "
            "ON s.conflict_version_id = r.conflict_version_id AND s.asin = r.asin AND s.field_name = %s "
            f"WHERE r.conflict_version_id = %s AND r.{col} IS DISTINCT FROM s.winning_value{cast}", col, cv)
    mism += one(
        "SELECT count(*) FROM task7.edition_resolved r JOIN task7.edition_field_selection s "
        "ON s.conflict_version_id = r.conflict_version_id AND s.asin = r.asin AND s.field_name = 'product_release_date' "
        "WHERE r.conflict_version_id = %s AND r.product_release_date IS DISTINCT FROM s.winning_value::date", cv)
    out["materialized_mismatch"] = mism

    # 6. 多值字段：选择状态与成员一致，成员有来源，starring 只来自页面，IMDb 成员确实在 IMDb 里
    person_bad = 0
    for role in ("director", "starring", "cast"):
        person_bad += one(
            "SELECT count(*) FROM task7.movie_field_selection s LEFT JOIN LATERAL ("
            "SELECT count(*) FILTER (WHERE status = 'adopted') adopted, count(*) FILTER (WHERE status <> 'adopted') pending "
            "FROM task7.movie_person_resolved p WHERE p.extraction_id = %s AND p.stage_key = %s AND p.movie_id = s.movie_id "
            "AND p.role = %s) m ON true WHERE s.conflict_version_id = %s AND s.field_name = %s AND NOT ("
            "(s.status = 'resolved' AND m.adopted > 0) OR (s.status = 'conflict_unresolved' AND m.adopted = 0 AND m.pending > 0) "
            "OR (s.status = 'missing' AND m.adopted + m.pending = 0))", eid, keys["multi"], role, cv, role)
    out["person_status_inconsistent"] = person_bad
    out["genre_status_inconsistent"] = one(
        "SELECT count(*) FROM task7.movie_field_selection s LEFT JOIN LATERAL ("
        "SELECT count(*) n FROM task7.movie_genre g WHERE g.extraction_id = %s AND g.stage_key = %s "
        "AND g.movie_id = s.movie_id) m ON true WHERE s.conflict_version_id = %s AND s.field_name = 'genres' "
        "AND NOT ((s.status = 'resolved' AND m.n > 0) OR (s.status = 'missing' AND m.n = 0))", eid, keys["multi"], cv)
    out["genre_outside_vocabulary"] = one(
        "SELECT count(*) FROM task7.movie_genre WHERE extraction_id = %s AND stage_key = %s AND NOT genre = ANY(%s)",
        eid, keys["multi"], list(CONTROLLED_GENRES))
    out["starring_not_page_only"] = one(
        "SELECT count(*) FROM task7.movie_person_resolved WHERE extraction_id = %s AND stage_key = %s "
        "AND role = 'starring' AND source_coverage <> 'page_only'", eid, keys["multi"])
    out["members_without_source"] = one(
        "SELECT count(*) FROM task7.movie_person_resolved p WHERE p.extraction_id = %s AND p.stage_key = %s AND ("
        "(p.source_coverage IN ('page_only', 'both') AND cardinality(p.raw_names) = 0) OR "
        "(p.source_coverage IN ('imdb_only', 'both') AND p.nconst IS NULL) OR "
        "(p.source_coverage IN ('imdb_only', 'both') AND p.role = 'starring'))", eid, keys["multi"])
    out["imdb_members_not_in_imdb"] = one(
        "SELECT count(*) FROM task7.movie_person_resolved p JOIN task7.movie_resolved r ON r.conflict_version_id = %s "
        "AND r.movie_id = p.movie_id WHERE p.extraction_id = %s AND p.stage_key = %s "
        "AND p.source_coverage IN ('imdb_only', 'both') AND NOT EXISTS ("
        "SELECT 1 FROM imdb.title_director d WHERE p.role = 'director' AND d.snapshot_id = %s "
        "AND d.tconst = r.effective_tconst AND d.nconst = p.nconst UNION ALL "
        "SELECT 1 FROM imdb.title_principal t WHERE p.role = 'cast' AND t.snapshot_id = %s AND t.tconst = r.effective_tconst "
        "AND t.nconst = p.nconst AND t.category IN ('actor', 'actress'))",
        cv, eid, keys["multi"], row["imdb_snapshot_id"], row["imdb_snapshot_id"])
    out["halted_with_imdb_members"] = one(
        "SELECT count(*) FROM task7.movie_resolved r WHERE r.conflict_version_id = %s AND r.cross_source_mismatch_halt "
        "AND (EXISTS (SELECT 1 FROM task7.movie_person_resolved p WHERE p.extraction_id = %s AND p.stage_key = %s "
        "AND p.movie_id = r.movie_id AND p.source_coverage IN ('imdb_only', 'both')) OR EXISTS ("
        "SELECT 1 FROM task7.movie_genre g WHERE g.extraction_id = %s AND g.stage_key = %s AND g.movie_id = r.movie_id "
        "AND g.source_coverage IN ('imdb_only', 'both')))", cv, eid, keys["multi"], eid, keys["multi"])
    out["imdb_person_id_wrong"] = one(
        "SELECT count(*) FROM task7.imdb_person_ext WHERE extraction_id = %s AND imdb_snapshot_id = %s "
        "AND NOT in_task5_identity AND person_id <> 'ps_' || substr(encode(sha256(('imdb:' || nconst)::bytea), 'hex'), 1, 16)",
        eid, row["imdb_snapshot_id"])

    # 7. 熔断作品的 IMDb 值不得出现在作品层：原名、年份、片长不能是 resolved（有候选时为 conflict_unresolved，没有候选时为 missing）
    out["halted_with_imdb_values"] = one(
        "SELECT count(*) FROM task7.movie_resolved r JOIN task7.movie_field_selection s ON s.conflict_version_id = r.conflict_version_id "
        "AND s.movie_id = r.movie_id AND s.field_name IN ('original_title', 'release_year', 'runtime_minutes') "
        "WHERE r.conflict_version_id = %s AND r.cross_source_mismatch_halt AND s.status = 'resolved'", cv)

    # 8. 共享 tconst 物化行数
    out["shared_tconst_rows"] = one("SELECT count(*) FROM task7.shared_tconst_conflict WHERE conflict_version_id = %s", cv)
    out["shared_tconst_mismatch"] = one(
        "SELECT abs((SELECT count(*) FROM task7.shared_tconst_conflict WHERE conflict_version_id = %s) - "
        "(SELECT count(*) FROM task7.shared_tconst_result WHERE extraction_id = %s AND stage_key = %s))",
        cv, eid, keys["link"])

    # 9. 上游表行数快照（任务 1–6 与 IMDb 全部只读）
    out["table_counts"] = {f"{sch}.{tbl}": one(f"SELECT count(*) FROM {sch}.{tbl}") for sch, tbl in UPSTREAM_TABLES}
    return out


PROBLEM_LABELS = {
    "stage_key_mismatch": "版本登记的阶段键与输入重算不一致",
    "run_version_mismatch": "运行已成功或复用但版本未核实",
    "movies_missing": "作品缺失 movie_resolved 记录", "movies_extra": "多余的 movie_resolved 记录",
    "editions_missing": "商品缺失 edition_resolved 记录", "editions_extra": "多余的 edition_resolved 记录",
    "movie_selections_missing": "作品缺失字段选择记录", "movie_selections_orphan": "没有对应作品的字段选择记录",
    "edition_selections_missing": "商品缺失字段选择记录", "edition_selections_orphan": "没有对应商品的字段选择记录",
    "movie_selected_invalid": "作品层采用候选无效（不存在、不相符或不是 adopted）",
    "edition_selected_invalid": "版本层采用候选无效（不存在、不相符或不是 adopted）",
    "movie_considered_invalid": "作品层考虑过的候选不存在或不相符", "edition_considered_invalid": "版本层考虑过的候选不存在或不相符",
    "movie_unresolved_with_adopted": "作品层非 resolved 字段含 adopted 候选（状态不一致）",
    "edition_unresolved_with_adopted": "版本层非 resolved 字段含 adopted 候选（状态不一致）",
    "movie_orphan_candidates": "作品层孤儿候选（不被任何选择记录考虑）", "edition_orphan_candidates": "版本层孤儿候选",
    "movie_selected_value_mismatch": "作品层胜出值既不是采用候选的规范值也不是原始值",
    "edition_selected_value_mismatch": "版本层胜出值既不是采用候选的规范值也不是原始值",
    "materialized_mismatch": "movie_resolved、edition_resolved 与选择记录的值不一致",
    "person_status_inconsistent": "演职员字段状态与成员状态不一致", "genre_status_inconsistent": "类型字段状态与成员不一致",
    "genre_outside_vocabulary": "类型成员不在受控词表内", "starring_not_page_only": "starring 含非页面来源的成员",
    "members_without_source": "成员缺少来源", "imdb_members_not_in_imdb": "标为 IMDb 来源的成员不在 IMDb 里",
    "halted_with_imdb_members": "已熔断作品含 IMDb 成员", "imdb_person_id_wrong": "imdb_person_ext 的 person_id 不符公式",
    "halted_with_imdb_values": "已熔断作品的原名、上映年份或片长被采用（应为 conflict_unresolved 或 missing）",
    "shared_tconst_mismatch": "共享 tconst 物化行数与阶段结果不一致",
}


def problems(info: dict[str, Any], *, expect_verified: bool = True) -> list[str]:
    """根据 verify 的结果列出所有违规项，空列表即通过。expect_verified：命令行要求版本已 verified；
    DAG 的 verify 步骤在标记 verified 之前调用，传 False。"""
    out = [f"{label}：{info[key]}" for key, label in PROBLEM_LABELS.items() if info.get(key, 0) > 0]
    if info.get("movie_resolved_total") != info.get("movies_expected"):
        out.append(f"movie_resolved 行数 {info.get('movie_resolved_total')} ≠ 作品数 {info.get('movies_expected')}")
    if info.get("edition_resolved_total") != info.get("editions_expected"):
        out.append(f"edition_resolved 行数 {info.get('edition_resolved_total')} ≠ 商品数 {info.get('editions_expected')}")
    if expect_verified and info.get("version_status") != "verified":
        out.append(f"版本状态为 {info.get('version_status')}，不是 verified")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 7 只读核对")
    parser.add_argument("--cv", help="冲突消解版本号（如 cv0001，默认取最近一次）")
    parser.add_argument("--extraction-id", help="抽取 ID")
    args = parser.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        cv = args.cv or latest_cv(conn, eid)
        info = verify(conn, cv)
        p = problems(info)
        info["problems"] = p
        info["passed"] = len(p) == 0
        print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return 0 if info["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
