"""任务 6 只读核对（spec AC18；与 DAG 的 verify 步骤共用 verify 与 problems）。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task6.check                              # 默认抽取的最新上映信息版本
    python -m etl_task6.check --rv rv0001
输出一段 JSON；全部一致时退出码 0，否则 1。整个核对在只读事务里进行，不写任何表。
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

T1_TO_T5_TABLES = (
    ("public", "review_record"),
    ("public", "review_staging"),
    ("task2", "product_manifest"),
    ("task3", "page_field"),
    ("task4", "movie"),
    ("task4", "product_movie"),
    ("task4", "merge_decision"),
    ("task5", "person"),
    ("task5", "movie_person"),
)


def latest_rv(conn: psycopg.Connection, extraction_id: str) -> str:
    row = conn.execute(
        "SELECT release_version_id FROM task6.release_version WHERE extraction_id = %s "
        "ORDER BY created_at DESC, release_version_id DESC LIMIT 1", (extraction_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"抽取 {extraction_id} 还没有上映信息版本")
    return row[0]


def rv_row(conn: psycopg.Connection, rv: str) -> dict[str, Any]:
    r = conn.execute(
        "SELECT release_version_id, extraction_id, parent_version_id, run_id, input_key, "
        "t4_mapping_version_id, t5_person_mapping_version_id, parser_version, rule_version, "
        "imdb_snapshot_id, counts, created_at FROM task6.release_version WHERE release_version_id = %s",
        (rv,)).fetchone()
    if r is None:
        raise ValueError(f"上映信息版本 {rv} 不存在")
    return {
        "release_version_id": r[0], "extraction_id": r[1], "parent_version_id": r[2], "run_id": r[3],
        "input_key": r[4], "t4_mapping_version_id": r[5], "t5_person_mapping_version_id": r[6],
        "parser_version": r[7], "rule_version": r[8], "imdb_snapshot_id": r[9], "counts": dict(r[10]),
        "created_at": r[11].isoformat() if r[11] else None,
    }


def verify(conn: psycopg.Connection, rv: str) -> dict[str, Any]:
    """只读核对一个上映信息版本。"""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        return _verify(conn, rv)


def _verify(conn: psycopg.Connection, rv: str) -> dict[str, Any]:
    row = rv_row(conn, rv)
    eid = row["extraction_id"]
    t4 = row["t4_mapping_version_id"]
    out: dict[str, Any] = {
        "release_version_id": rv, "extraction_id": eid, "t4_mapping_version_id": t4,
        "t5_person_mapping_version_id": row["t5_person_mapping_version_id"],
        "parser_version": row["parser_version"], "rule_version": row["rule_version"],
        "imdb_snapshot_id": row["imdb_snapshot_id"],
    }
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731

    # 1. 作品覆盖与选择记录一一对应
    movie_count = one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s", t4)
    sel_count = one("SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s", rv)
    out["movies_expected"] = movie_count
    out["selections_total"] = sel_count
    out["selections_missing_movies"] = one(
        "SELECT count(*) FROM task4.movie m LEFT JOIN task6.release_selection s "
        "ON s.release_version_id = %s AND s.movie_id = m.movie_id WHERE m.mapping_version_id = %s "
        "AND s.movie_id IS NULL", rv, t4)
    out["selections_extra_movies"] = one(
        "SELECT count(*) FROM task6.release_selection s LEFT JOIN task4.movie m "
        "ON m.mapping_version_id = %s AND m.movie_id = s.movie_id WHERE s.release_version_id = %s "
        "AND m.movie_id IS NULL", t4, rv)

    # 2. 状态分布与约束
    status_counts = dict(conn.execute(
        "SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "GROUP BY 1 ORDER BY 1", (rv,)).fetchall())
    out["status_counts"] = status_counts

    # 3. 日期语义与字段约束（release_date 恒 NULL、precision 恒 year、缺失原因恒 no_permitted_dated_source）
    out["release_date_non_null"] = one(
        "SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "AND release_date IS NOT NULL", rv)
    out["precision_not_year"] = one(
        "SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "AND precision != 'year'", rv)
    out["bad_regional_missing_reason"] = one(
        "SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "AND regional_release_missing_reason != 'no_permitted_dated_source'", rv)

    # 4. 非 missing 选择指向候选且年份一致
    out["invalid_non_missing"] = one(
        "SELECT count(*) FROM task6.release_selection s JOIN task6.release_candidate c "
        "ON c.candidate_id = s.selected_candidate_id WHERE s.release_version_id = %s "
        "AND s.status != 'missing' AND s.release_year != c.value_year", rv)
    out["missing_with_year"] = one(
        "SELECT count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "AND status = 'missing' AND (release_year IS NOT NULL OR selected_candidate_id IS NOT NULL)", rv)

    # 5. 共现候选满足 R09–R10
    out["cooccur_matched_invalid"] = one(
        "SELECT count(*) FROM task6.release_selection s JOIN task6.release_candidate c "
        "ON c.candidate_id = s.selected_candidate_id WHERE s.release_version_id = %s "
        "AND s.status = 'cooccur_matched' AND c.source != 'imdb_t6_cooccur'", rv)

    # 6. 推断上界不写入选择记录
    out["bounds_in_selection"] = one(
        "SELECT count(*) FROM task6.release_selection s JOIN task6.inferred_release_bound b "
        "ON b.release_version_id = s.release_version_id AND b.movie_id = s.movie_id "
        "WHERE s.release_version_id = %s AND s.release_year::text = b.bound_time::text", rv)

    # 7. 每个推断上界至少有一种成立证据
    out["bounds_without_passed_evidence"] = one(
        "SELECT count(*) FROM task6.inferred_release_bound b JOIN task6.bound_assessment a "
        "ON a.release_version_id = b.release_version_id AND a.movie_id = b.movie_id "
        "WHERE b.release_version_id = %s AND cardinality(a.passed_evidence) = 0", rv)

    # 8. 商品发售日期不进候选与选择
    out["dates_in_candidates"] = one(
        "SELECT count(*) FROM task6.release_candidate WHERE extraction_id = %s "
        "AND raw_value LIKE '%%-%%-%%'", eid)

    # 9. 任务 1–5 表行数快照
    table_counts = {}
    for sch, tbl in T1_TO_T5_TABLES:
        table_counts[f"{sch}.{tbl}"] = one(f"SELECT count(*) FROM {sch}.{tbl}")
    out["t1_to_t5_table_counts"] = table_counts

    return out


def problems(info: dict[str, Any]) -> list[str]:
    """根据 verify 的结果列出所有违规项，空列表即通过。"""
    prob: list[str] = []
    if info.get("selections_missing_movies", 0) > 0:
        prob.append(f"作品缺失选择记录：{info['selections_missing_movies']} 部")
    if info.get("selections_extra_movies", 0) > 0:
        prob.append(f"多余选择记录：{info['selections_extra_movies']} 部")
    if info.get("release_date_non_null", 0) > 0:
        prob.append(f"release_date 被填充非空值：{info['release_date_non_null']} 行")
    if info.get("precision_not_year", 0) > 0:
        prob.append(f"precision 非 year：{info['precision_not_year']} 行")
    if info.get("bad_regional_missing_reason", 0) > 0:
        prob.append(f"地区上映缺失原因不符：{info['bad_regional_missing_reason']} 行")
    if info.get("invalid_non_missing", 0) > 0:
        prob.append(f"采用值与候选年份不一致：{info['invalid_non_missing']} 行")
    if info.get("missing_with_year", 0) > 0:
        prob.append(f"missing 状态却包含年份或候选：{info['missing_with_year']} 行")
    if info.get("cooccur_matched_invalid", 0) > 0:
        prob.append(f"cooccur_matched 未指向共现候选：{info['cooccur_matched_invalid']} 行")
    if info.get("bounds_in_selection", 0) > 0:
        prob.append(f"推断上界写入了选择年份：{info['bounds_in_selection']} 行")
    if info.get("bounds_without_passed_evidence", 0) > 0:
        prob.append(f"推断上界缺少通过的证据：{info['bounds_without_passed_evidence']} 行")
    if info.get("dates_in_candidates", 0) > 0:
        prob.append(f"商品发售日期混入年份候选：{info['dates_in_candidates']} 行")
    return prob


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 6 只读核对")
    parser.add_argument("--rv", help="上映信息版本号（如 rv0001，默认取最近一次）")
    parser.add_argument("--extraction-id", help="抽取 ID")
    args = parser.parse_args(argv)

    s = load_settings()
    with connect(s) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        rv = args.rv or latest_rv(conn, eid)
        info = verify(conn, rv)
        p = problems(info)
        info["problems"] = p
        info["passed"] = len(p) == 0
        print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return 0 if info["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
