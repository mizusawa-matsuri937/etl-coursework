"""任务 6 统计、导出、实例与版本差异（spec R33、R35，AC13、AC16）。

口径见 design“统计与报告”“实例”。全部查询限定一个上映信息版本；导出不含评论正文与整页 HTML。
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

import psycopg

from etl_task1 import reporting
from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction

from .check import latest_rv, rv_row
from .run import RunInputs

SUMMARY_VERSION = "task6-release-summary-1"
ALL_PRODUCTS_TOTAL = 253059


def output_root(output_dir: Path) -> Path:
    return output_dir / "task6"


def run_dir(output_dir: Path, eid: str, rv: str, run_key: str) -> Path:
    return output_root(output_dir) / eid / rv / run_key


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 6) if d else None


def _inputs_for_row(row: dict[str, Any]) -> RunInputs:
    return RunInputs(
        extraction_id=row["extraction_id"],
        t4_mapping_version_id=row["t4_mapping_version_id"],
        t5_person_mapping_version_id=row["t5_person_mapping_version_id"],
        imdb_snapshot_id=row["imdb_snapshot_id"],
        parser_version=row["parser_version"],
        rule_version=row["rule_version"],
    )


def _resolve_run(conn: psycopg.Connection, rv: str, run_id: str | None = None) -> tuple[str, dict[str, Any]]:
    """解析并校验上映信息版本对应的真实成功运行（run_id, run_counts）。
    显式传入不存在的 run_id 时报错拒绝；未传时按版本查找最新的 status = 'succeeded' 运行（跳过 reused）。"""
    if run_id is not None:
        r = conn.execute(
            "SELECT run_id, status, release_version_id, counts FROM task6.run WHERE run_id = %s",
            (run_id,),
        ).fetchone()
        if r is None:
            raise ValueError(f"运行 {run_id!r} 在 task6.run 中不存在")
        if r[2] != rv:
            raise ValueError(f"运行 {run_id!r} 属于上映信息版本 {r[2]!r}，而非 {rv!r}")
        if r[1] == "reused":
            reused_from = (r[3] or {}).get("reused_run")
            if reused_from:
                orig = conn.execute(
                    "SELECT run_id, counts FROM task6.run WHERE run_id = %s AND status = 'succeeded'",
                    (reused_from,),
                ).fetchone()
                if orig:
                    return r[0], dict(orig[1] or {})
        return r[0], dict(r[3] or {})
    r = conn.execute(
        "SELECT run_id, counts FROM task6.run WHERE release_version_id = %s AND kind = 'release' "
        "AND status = 'succeeded' ORDER BY started_at DESC LIMIT 1",
        (rv,),
    ).fetchone()
    if r is not None:
        return r[0], dict(r[1] or {})
    v = rv_row(conn, rv)
    return v["run_id"], {}


def build_summary(conn: psycopg.Connection, rv: str, *, run_id: str | None = None) -> dict[str, Any]:
    """汇总上映信息版本的全貌统计（R33）。"""
    row = rv_row(conn, rv)
    actual_run_id, run_counts = _resolve_run(conn, rv, run_id)
    inputs = _inputs_for_row(row)
    eid = row["extraction_id"]
    t4 = row["t4_mapping_version_id"]
    pv = row["parser_version"]
    cand_key = inputs.stage_key("candidates")
    cooc_key = inputs.stage_key("cooccur")
    rvy_key = inputs.stage_key("review_years")

    def one(sql: str, *a: Any) -> Any:
        params: tuple | list
        if len(a) == 1 and isinstance(a[0], (tuple, list)):
            params = a[0]
        else:
            params = a
        return conn.execute(sql, params).fetchone()[0]

    # 作品与状态
    edition_movies = one(
        "SELECT count(DISTINCT movie_id) FROM task4.product_movie WHERE mapping_version_id = %s "
        "AND relation_type = 'edition'", t4)
    contains_only_movies = one(
        "SELECT count(*) FROM task4.movie m WHERE m.mapping_version_id = %s AND NOT EXISTS ("
        "SELECT 1 FROM task4.product_movie pm WHERE pm.mapping_version_id = m.mapping_version_id "
        "AND pm.movie_id = m.movie_id AND pm.relation_type = 'edition')", t4)
    total_movies = edition_movies + contains_only_movies

    status_counts = dict(conn.execute(
        "SELECT status, count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "GROUP BY 1 ORDER BY 1", (rv,)).fetchall())
    missing_reasons = dict(conn.execute(
        "SELECT missing_reason, count(*) FROM task6.release_selection WHERE release_version_id = %s "
        "AND status = 'missing' GROUP BY 1 ORDER BY 1", (rv,)).fetchall())

    # 覆盖率
    works_with_year = sum(v for k, v in status_counts.items() if k != "missing")
    matched_products_total = one(
        "SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s "
        "AND match_scope = 'matched'", eid)
    matched_products_with_year = one(
        "SELECT count(DISTINCT pm.asin) FROM task4.product_movie pm JOIN task6.release_selection s "
        "ON s.release_version_id = %s AND s.movie_id = pm.movie_id WHERE pm.mapping_version_id = %s "
        "AND pm.relation_type = 'edition' AND s.status != 'missing'", rv, t4)

    # 候选（按当前版本的 candidates 与 cooccur 阶段键过滤）
    candidate_counts = dict(conn.execute(
        "SELECT source, count(*) FROM task6.release_candidate WHERE extraction_id = %s "
        "AND stage_key IN (%s, %s) GROUP BY 1 ORDER BY 1", (eid, cand_key, cooc_key)).fetchall())
    title_types = dict(conn.execute(
        "SELECT COALESCE(source_ref->>'title_type', source_ref->>'titleType', 'null'), count(*) "
        "FROM task6.release_candidate WHERE extraction_id = %s AND stage_key = %s "
        "AND source = 'imdb_t4_link' GROUP BY 1 ORDER BY 1", (eid, cand_key)).fetchall())

    # 线索处理（按当前版本的 candidates 阶段键过滤；candidate_shared_count 用独立 COUNT(*) 查询）
    hint_actions = dict(conn.execute(
        "SELECT action, count(*) FROM task6.hint_use WHERE extraction_id = %s AND stage_key = %s "
        "GROUP BY 1 ORDER BY 1", (eid, cand_key)).fetchall())
    candidate_shared_count = one(
        "SELECT count(*) FROM task6.hint_use WHERE extraction_id = %s AND stage_key = %s "
        "AND action = 'candidate_shared'", eid, cand_key)
    shared_hints = [
        {"asin": r[0], "tconst": r[1], "owner_movie_id": r[2]}
        for r in conn.execute(
            "SELECT asin, tconst, owner_movie_id FROM task6.hint_use WHERE extraction_id = %s "
            "AND stage_key = %s AND action = 'candidate_shared' ORDER BY asin LIMIT 20",
            (eid, cand_key)).fetchall()
    ]

    # 共现反查（按当前版本的 cooccur 阶段键过滤）
    cooccur_outcomes = dict(conn.execute(
        "SELECT outcome, count(*) FROM task6.cooccur_result WHERE extraction_id = %s "
        "AND stage_key = %s GROUP BY 1 ORDER BY 1", (eid, cooc_key)).fetchall())
    shared_cooccur_count = one(
        "SELECT count(*) FROM task6.cooccur_result WHERE extraction_id = %s AND stage_key = %s "
        "AND owner_movie_id IS NOT NULL", eid, cooc_key)
    title_guard_excluded = one(
        "SELECT count(DISTINCT movie_id) FROM task6.cooccur_score WHERE extraction_id = %s "
        "AND stage_key = %s AND title_ok IS FALSE", eid, cooc_key)
    number_guard_excluded = one(
        "SELECT count(DISTINCT movie_id) FROM task6.cooccur_score WHERE extraction_id = %s "
        "AND stage_key = %s AND number_guard IS FALSE", eid, cooc_key)

    # 评论提及（按当前版本的 review_years 阶段键过滤）
    review_mentions_movies = one(
        "SELECT count(DISTINCT movie_id) FROM task6.review_year_mention WHERE extraction_id = %s "
        "AND stage_key = %s", eid, rvy_key)

    # 商品发售日期
    dates_by_kind = dict(conn.execute(
        "SELECT kind, count(*) FROM task6.product_release_date WHERE extraction_id = %s "
        "AND parser_version = %s GROUP BY 1 ORDER BY 1", (eid, pv)).fetchall())
    jan_first_suspect = one(
        "SELECT count(*) FROM task6.product_release_date WHERE extraction_id = %s "
        "AND parser_version = %s AND 'jan_first_suspect' = ANY(flags)", eid, pv)
    after_review_period = one(
        "SELECT count(*) FROM task6.product_release_date WHERE extraction_id = %s "
        "AND parser_version = %s AND 'after_review_period' = ANY(flags)", eid, pv)
    products_with_dates = one(
        "SELECT count(DISTINCT asin) FROM task6.product_release_date WHERE extraction_id = %s "
        "AND parser_version = %s", eid, pv)

    # 评论时间与上界
    movies_with_review = one(
        "SELECT count(*) FROM task6.movie_review_time WHERE extraction_id = %s "
        "AND t4_mapping_version_id = %s", eid, t4)
    assessments_count = one(
        "SELECT count(*) FROM task6.bound_assessment WHERE release_version_id = %s", rv)
    bounds_by_evidence = dict(conn.execute(
        "SELECT chosen_evidence, count(*) FROM task6.inferred_release_bound WHERE release_version_id = %s "
        "GROUP BY 1 ORDER BY 1", (rv,)).fetchall())
    bounds_by_confidence = dict(conn.execute(
        "SELECT round(confidence::numeric, 2)::text, count(*) FROM task6.inferred_release_bound "
        "WHERE release_version_id = %s GROUP BY 1 ORDER BY 1", (rv,)).fetchall())
    bounds_for_missing = one(
        "SELECT count(*) FROM task6.inferred_release_bound b JOIN task6.release_selection s "
        "ON s.release_version_id = b.release_version_id AND s.movie_id = b.movie_id "
        "WHERE b.release_version_id = %s AND s.status = 'missing'", rv)
    bounds_before_year = dict(conn.execute(
        "SELECT detail->>'evidence', count(*) FROM task6.quality_issue "
        "WHERE issue_code = 'bound_before_release_year' AND extraction_id = %s AND run_id = %s "
        "GROUP BY 1 ORDER BY 1", (eid, row["run_id"])).fetchall())

    # 证据未通过条件
    failed_checks = dict(conn.execute(
        "SELECT unnest(failed_checks), count(*) FROM task6.bound_evidence WHERE release_version_id = %s "
        "GROUP BY 1 ORDER BY 1", (rv,)).fetchall())

    # R26 预售、合集、跨版本
    collection_reviews = one(
        "SELECT coalesce(sum(review_count), 0) FROM task6.product_review_time WHERE extraction_id = %s "
        "AND product_kind = 'collection'", eid)
    multi_edition_movies = one(
        "SELECT count(*) FROM (SELECT movie_id FROM task4.product_movie WHERE mapping_version_id = %s "
        "AND relation_type = 'edition' GROUP BY movie_id HAVING count(*) > 1) s", t4)
    earliest_review_before_all_media = one(
        "SELECT count(*) FROM task6.movie_review_time t WHERE t.extraction_id = %s AND t.t4_mapping_version_id = %s "
        "AND t.earliest_review_time IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM task6.product_release_date d JOIN task4.product_movie pm ON pm.mapping_version_id = t.t4_mapping_version_id "
        "AND pm.asin = d.asin AND pm.member_ordinal = 0 AND pm.relation_type = 'edition' "
        "WHERE d.extraction_id = t.extraction_id AND d.parser_version = %s AND pm.movie_id = t.movie_id "
        "AND d.kind = 'media_release' AND d.value_date <= t.earliest_review_time::date)", eid, t4, pv)

    return {
        "summary_version": SUMMARY_VERSION,
        "release_version_id": rv,
        "run_id": actual_run_id,
        "stage_counts": run_counts,
        "extraction_id": eid,
        "t4_mapping_version_id": t4,
        "t5_person_mapping_version_id": row["t5_person_mapping_version_id"],
        "parser_version": pv,
        "rule_version": row["rule_version"],
        "imdb_snapshot_id": row["imdb_snapshot_id"],
        "movies": {
            "total": total_movies,
            "edition_movies": edition_movies,
            "contains_only_movies": contains_only_movies,
            "by_status": status_counts,
            "missing_reasons": missing_reasons,
        },
        "coverage": {
            "works_with_year": works_with_year,
            "works_total": total_movies,
            "works_coverage": _ratio(works_with_year, total_movies),
            "matched_products_with_year": matched_products_with_year,
            "matched_products_total": matched_products_total,
            "matched_products_coverage": _ratio(matched_products_with_year, matched_products_total),
            "all_products_with_year": matched_products_with_year,
            "all_products_total": ALL_PRODUCTS_TOTAL,
            "all_products_coverage": _ratio(matched_products_with_year, ALL_PRODUCTS_TOTAL),
        },
        "candidates": {
            "by_source": candidate_counts,
            "title_types": title_types,
            "year_disagreement": one(
                "SELECT count(*) FROM task6.quality_issue WHERE issue_code = 'year_disagreement' "
                "AND extraction_id = %s AND run_id = %s", eid, row["run_id"]),
            "tconst_disagreement": one(
                "SELECT count(*) FROM task6.quality_issue WHERE issue_code = 'tconst_disagreement' "
                "AND extraction_id = %s AND run_id = %s", eid, row["run_id"]),
        },
        "hint_use": {
            "actions": hint_actions,
            "candidate_shared_count": candidate_shared_count,
            "candidate_shared_top": shared_hints,
        },
        "shared_tconst_summary": {
            "candidate_shared_count": candidate_shared_count,
            "cooccur_shared_count": shared_cooccur_count,
            "sample_shared_hints": shared_hints,
        },
        "cooccur": {
            "outcomes": cooccur_outcomes,
            "shared_tconst_count": shared_cooccur_count,
            "title_guard_excluded": title_guard_excluded,
            "number_guard_excluded": number_guard_excluded,
        },
        "review_years": {
            "movies_with_mentions": review_mentions_movies,
        },
        "regional_dates": {
            "coverage": 0,
            "missing_reason": "no_permitted_dated_source",
        },
        "product_dates": {
            "by_kind": dates_by_kind,
            "jan_first_suspect": jan_first_suspect,
            "after_review_period": after_review_period,
            "products_with_dates": products_with_dates,
        },
        "bounds": {
            "movies_with_review": movies_with_review,
            "review_coverage": _ratio(movies_with_review, total_movies),
            "assessments_total": assessments_count,
            "by_evidence": bounds_by_evidence,
            "by_confidence": bounds_by_confidence,
            "bounds_for_missing": bounds_for_missing,
            "failed_checks": failed_checks,
            "bounds_before_year": bounds_before_year,
        },
        "presale_collection_cross_edition": {
            "collection_reviews": collection_reviews,
            "multi_edition_movies": multi_edition_movies,
            "earliest_review_before_all_media": earliest_review_before_all_media,
            "note": "数据中没有预售标记，不宣称已排除预售",
        },
    }


def examples(conn: psycopg.Connection, rv: str) -> dict[str, list[dict[str, Any]]]:
    """生成 R35 要求的各类实例。"""
    row = rv_row(conn, rv)
    eid = row["extraction_id"]
    t4 = row["t4_mapping_version_id"]
    pv = row["parser_version"]

    def _sample_status(st: str, limit: int = 3):
        res = []
        for mid, title, year, reason, cid in conn.execute(
                "SELECT s.movie_id, m.display_title, s.release_year, s.reason, s.selected_candidate_id "
                "FROM task6.release_selection s JOIN task4.movie m ON m.mapping_version_id = %s "
                "AND m.movie_id = s.movie_id WHERE s.release_version_id = %s AND s.status = %s "
                "ORDER BY md5(s.movie_id) LIMIT %s", (t4, rv, st, limit)).fetchall():
            c = conn.execute(
                "SELECT source, source_key, raw_value, source_ref, match_evidence "
                "FROM task6.release_candidate WHERE candidate_id = %s", (cid,)).fetchone() if cid else None
            res.append({
                "movie_id": mid, "title": title, "year": year, "reason": reason,
                "candidate": {
                    "source": c[0], "key": c[1], "raw": c[2], "source_ref": dict(c[3]) if c and c[3] else {},
                    "evidence": dict(c[4]) if c and c[4] else {},
                } if c else None,
            })
        return res

    def _sample_bounds(evidence: str, limit: int = 3):
        res = []
        for mid, title, when, prec, conf, conds, basis in conn.execute(
                "SELECT b.movie_id, m.display_title, b.bound_time, b.bound_precision, b.confidence, "
                "b.conditions, b.basis FROM task6.inferred_release_bound b JOIN task4.movie m "
                "ON m.mapping_version_id = %s AND m.movie_id = b.movie_id WHERE b.release_version_id = %s "
                "AND b.chosen_evidence = %s ORDER BY md5(b.movie_id) LIMIT %s",
                (t4, rv, evidence, limit)).fetchall():
            res.append({
                "movie_id": mid, "title": title, "bound_time": when.isoformat(), "precision": prec,
                "confidence": float(conf), "conditions": dict(conds), "basis": dict(basis),
            })
        return res

    def _sample_failed_evidence(evidence: str, limit: int = 3):
        res = []
        for mid, title, failed, checks in conn.execute(
                "SELECT e.movie_id, m.display_title, e.failed_checks, e.checks "
                "FROM task6.bound_evidence e JOIN task4.movie m ON m.mapping_version_id = %s "
                "AND m.movie_id = e.movie_id WHERE e.release_version_id = %s AND e.evidence = %s "
                "AND e.passed IS FALSE ORDER BY md5(e.movie_id) LIMIT %s",
                (t4, rv, evidence, limit)).fetchall():
            res.append({
                "movie_id": mid, "title": title, "failed_checks": list(failed), "checks": dict(checks),
            })
        return res

    def _sample_coexisting(limit: int = 3):
        res = []
        for mid, title, year, d_asin, d_date, d_kind in conn.execute(
                "SELECT s.movie_id, m.display_title, s.release_year, d.asin, d.value_date, d.kind "
                "FROM task6.release_selection s JOIN task4.movie m ON m.mapping_version_id = %s "
                "AND m.movie_id = s.movie_id JOIN task4.product_movie pm ON pm.mapping_version_id = m.mapping_version_id "
                "AND pm.movie_id = s.movie_id AND pm.member_ordinal = 0 AND pm.relation_type = 'edition' "
                "JOIN task6.product_release_date d ON d.extraction_id = %s AND d.parser_version = %s AND d.asin = pm.asin "
                "WHERE s.release_version_id = %s AND s.status != 'missing' "
                "ORDER BY md5(s.movie_id) LIMIT %s", (t4, eid, pv, rv, limit)).fetchall():
            res.append({
                "movie_id": mid, "title": title, "release_year": year, "asin": d_asin,
                "product_date": d_date.isoformat(), "kind": d_kind,
            })
        return res

    return {
        "verified": _sample_status("verified"),
        "hint_matched": _sample_status("hint_matched"),
        "cooccur_matched": _sample_status("cooccur_matched"),
        "page_reported": _sample_status("page_reported"),
        "missing": _sample_status("missing"),
        "inferred_review": _sample_bounds("review"),
        "inferred_media": _sample_bounds("media_release"),
        "failed_review": _sample_failed_evidence("review"),
        "failed_media": _sample_failed_evidence("media_release"),
        "coexisting": _sample_coexisting(),
    }


def export_files(conn: psycopg.Connection, rv: str, directory: Path,
                 examples_data: dict[str, Any] | None = None, *,
                 run_id: str | None = None) -> dict[str, str]:
    """导出所有报告文件与数据 CSV，返回 文件名 -> SHA256 哈希。"""
    directory.mkdir(parents=True, exist_ok=True)
    row = rv_row(conn, rv)
    inputs = _inputs_for_row(row)
    cand_key = inputs.stage_key("candidates")
    cooc_key = inputs.stage_key("cooccur")
    pv = row["parser_version"]
    summary_data = build_summary(conn, rv, run_id=run_id)
    if examples_data is None:
        examples_data = examples(conn, rv)

    files: dict[str, bytes] = {}

    # JSON 报告
    summary_json = json.dumps(summary_data, ensure_ascii=False, indent=2, default=str) + "\n"
    files["summary.json"] = summary_json.encode("utf-8")

    # Markdown 报告
    report_md = render_markdown(summary_data)
    files["report.md"] = report_md.encode("utf-8")

    # Markdown 实例
    examples_md = render_examples_markdown(examples_data)
    files["examples.md"] = examples_md.encode("utf-8")

    # CSV 数据表导出
    def _export_query_csv(sql: str, params: tuple, columns: list[str]) -> bytes:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        for r in conn.execute(sql, params).fetchall():
            writer.writerow([json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else x for x in r])
        return buf.getvalue().encode("utf-8")

    files["release_selection.csv"] = _export_query_csv(
        "SELECT movie_id, status, missing_reason, release_year, precision, region, release_type, "
        "regional_release_missing_reason, selected_candidate_id, selection_rule, reason, confidence, disagreement "
        "FROM task6.release_selection WHERE release_version_id = %s ORDER BY movie_id",
        (rv,),
        ["movie_id", "status", "missing_reason", "release_year", "precision", "region", "release_type",
         "regional_release_missing_reason", "selected_candidate_id", "selection_rule", "reason", "confidence", "disagreement"])

    files["release_candidates.csv"] = _export_query_csv(
        "SELECT candidate_id, movie_id, source, source_key, raw_value, value_year, precision, semantic, "
        "flags, shared_with_movie_id, confidence, rule_version FROM task6.release_candidate "
        "WHERE extraction_id = %s AND stage_key IN (%s, %s) ORDER BY candidate_id",
        (summary_data["extraction_id"], cand_key, cooc_key),
        ["candidate_id", "movie_id", "source", "source_key", "raw_value", "value_year", "precision", "semantic",
         "flags", "shared_with_movie_id", "confidence", "rule_version"])

    files["cooccur.csv"] = _export_query_csv(
        "SELECT movie_id, outcome, chosen_tconst, passed_tconsts, owner_movie_id "
        "FROM task6.cooccur_result WHERE extraction_id = %s AND stage_key = %s ORDER BY movie_id",
        (summary_data["extraction_id"], cooc_key),
        ["movie_id", "outcome", "chosen_tconst", "passed_tconsts", "owner_movie_id"])

    files["product_release_dates.csv"] = _export_query_csv(
        "SELECT asin, kind, label_raw, raw_value, value_date, precision, flags, section, page_ordinal "
        "FROM task6.product_release_date WHERE extraction_id = %s AND parser_version = %s ORDER BY asin, assertion_id",
        (summary_data["extraction_id"], pv),
        ["asin", "kind", "label_raw", "raw_value", "value_date", "precision", "flags", "section", "page_ordinal"])

    files["bounds.csv"] = _export_query_csv(
        "SELECT movie_id, bound_time, bound_precision, bound_kind, chosen_evidence, confidence, status, rule_version "
        "FROM task6.inferred_release_bound WHERE release_version_id = %s ORDER BY movie_id",
        (rv,),
        ["movie_id", "bound_time", "bound_precision", "bound_kind", "chosen_evidence", "confidence", "status", "rule_version"])

    files["hint_use.csv"] = _export_query_csv(
        "SELECT asin, member_ordinal, tconst, movie_id, action, owner_movie_id "
        "FROM task6.hint_use WHERE extraction_id = %s AND stage_key = %s ORDER BY asin, member_ordinal",
        (summary_data["extraction_id"], cand_key),
        ["asin", "member_ordinal", "tconst", "movie_id", "action", "owner_movie_id"])

    # 写入文件并记录哈希
    digests: dict[str, str] = {}
    for name, content in files.items():
        reporting.write_atomic(directory / name, content)
        digests[name] = reporting.file_digest(directory / name)

    return digests


build_report = build_summary


def build_and_write_report(conn: psycopg.Connection, rv: str | None = None,
                           output_dir: Path = Path("outputs"), *,
                           extraction_id: str | None = None,
                           run_id: str | None = None,
                           run_key: str | None = None) -> tuple[dict[str, Any], Path, dict[str, str]]:
    """生成统计并写出报告目录；默认按当前上映信息版本查找最新成功运行的真实 run_id，不再硬编码 t6_full_1。"""
    eid = extraction_id or (rv_row(conn, rv)["extraction_id"] if rv else latest_full_extraction(conn))
    resolved_rv = rv or latest_rv(conn, eid)
    actual_run_id, _ = _resolve_run(conn, resolved_rv, run_id)
    effective_key = run_key or actual_run_id
    directory = run_dir(output_dir, eid, resolved_rv, effective_key)
    summary_data = build_summary(conn, resolved_rv, run_id=run_id)
    ex = examples(conn, resolved_rv)
    digests = export_files(conn, resolved_rv, directory, ex, run_id=run_id)
    return summary_data, directory, digests


def render_markdown(s: dict[str, Any]) -> str:
    m = s["movies"]
    cov = s["coverage"]
    c = s["candidates"]
    b = s["bounds"]
    p = s["presale_collection_cross_edition"]
    return f"""# 任务 6 上映信息报告（{s['release_version_id']}）

- **抽取 ID**：`{s['extraction_id']}`
- **任务 4 映射版本**：`{s['t4_mapping_version_id']}`
- **作品总数**：{m['total']}（经 edition：{m['edition_movies']}，只经 contains：{m['contains_only_movies']}）

## 1. 覆盖率与状态分布

| 状态 | 作品数 | 占比 |
|---|---|---|
| verified | {m['by_status'].get('verified', 0)} | {m['by_status'].get('verified', 0) / m['total']:.2%} |
| hint_matched | {m['by_status'].get('hint_matched', 0)} | {m['by_status'].get('hint_matched', 0) / m['total']:.2%} |
| cooccur_matched | {m['by_status'].get('cooccur_matched', 0)} | {m['by_status'].get('cooccur_matched', 0) / m['total']:.2%} |
| page_reported | {m['by_status'].get('page_reported', 0)} | {m['by_status'].get('page_reported', 0) / m['total']:.2%} |
| missing | {m['by_status'].get('missing', 0)} | {m['by_status'].get('missing', 0) / m['total']:.2%} |

### 缺失原因
{json.dumps(m['missing_reasons'], ensure_ascii=False, indent=2)}

### 覆盖率口径
- 作品覆盖率：{cov['works_with_year']} / {cov['works_total']} ({cov['works_coverage']:.2%})
- 参与匹配商品覆盖率：{cov['matched_products_with_year']} / {cov['matched_products_total']} ({cov['matched_products_coverage']:.2%})
- 全部商品覆盖率：{cov['all_products_with_year']} / {cov['all_products_total']} ({cov['all_products_coverage']:.2%})

## 2. 候选与质量问题
- 候选来源分布：{json.dumps(c['by_source'], ensure_ascii=False)}
- 年份分歧（year_disagreement）：{c['year_disagreement']}
- tconst 分歧（tconst_disagreement）：{c['tconst_disagreement']}

## 3. 推断上界与评论
- 评估作品数：{b['assessments_total']}
- 推断上界总数：{sum(b['by_evidence'].values())}（按证据：{json.dumps(b['by_evidence'], ensure_ascii=False)}）
- 无年份作品中获得推断上界数：{b['bounds_for_missing']}
- 上界早于上映年份（bound_before_release_year）：{json.dumps(b['bounds_before_year'], ensure_ascii=False)}

## 4. 预售、合集与跨版本分析（R26）
- 合集商品评论数（保持在商品层）：{p['collection_reviews']}
- 多版本作品数：{p['multi_edition_movies']}
- 最早评论早于全部 media_release 日期的作品数：{p['earliest_review_before_all_media']}
- 说明：{p['note']}
"""


def render_examples_markdown(ex: dict[str, list[dict[str, Any]]]) -> str:
    lines = ["# 任务 6 实例报告", ""]
    for cat, items in ex.items():
        lines.append(f"## {cat} ({len(items)} 例)")
        for item in items:
            lines.append(f"- **{item.get('title', 'N/A')}** (`{item.get('movie_id', 'N/A')}`):")
            lines.append(f"  ```json\n  {json.dumps(item, ensure_ascii=False, indent=2)}\n  ```")
        lines.append("")
    return "\n".join(lines)


def diff_versions(conn: psycopg.Connection, old_rv: str, new_rv: str) -> list[tuple[str, str, str, str]]:
    """列出两个版本之间选择记录或推断上界变化的作品与原因（R27）。
    覆盖版本 A 与版本 B 的作品并集（FULL OUTER JOIN），统计新增/移除作品、状态变化、年份变化、tconst 变化以及推断上界变化。"""
    diffs: list[tuple[str, str, str, str]] = []
    imdb_sources = ("imdb_t4_link", "imdb_t5_hint", "imdb_t6_cooccur")
    for mid, o_st, o_yr, o_tc, o_r, n_st, n_yr, n_tc, n_r in conn.execute(
            """
            WITH o AS (
                SELECT s.movie_id, s.status, s.release_year, s.reason,
                       CASE WHEN c.source = ANY(%s) THEN c.source_key END AS tconst
                FROM task6.release_selection s
                LEFT JOIN task6.release_candidate c ON c.candidate_id = s.selected_candidate_id
                WHERE s.release_version_id = %s
            ),
            n AS (
                SELECT s.movie_id, s.status, s.release_year, s.reason,
                       CASE WHEN c.source = ANY(%s) THEN c.source_key END AS tconst
                FROM task6.release_selection s
                LEFT JOIN task6.release_candidate c ON c.candidate_id = s.selected_candidate_id
                WHERE s.release_version_id = %s
            )
            SELECT COALESCE(o.movie_id, n.movie_id) AS movie_id,
                   o.status, o.release_year, o.tconst, o.reason,
                   n.status, n.release_year, n.tconst, n.reason
            FROM o FULL OUTER JOIN n ON n.movie_id = o.movie_id
            WHERE o.movie_id IS NULL OR n.movie_id IS NULL
               OR o.status IS DISTINCT FROM n.status
               OR o.release_year IS DISTINCT FROM n.release_year
               OR o.tconst IS DISTINCT FROM n.tconst
            ORDER BY 1
            """,
            (list(imdb_sources), old_rv, list(imdb_sources), new_rv)).fetchall():
        if o_st is None:
            n_desc = f"{n_st}({n_yr}" + (f",{n_tc})" if n_tc else ")")
            diffs.append((mid, "ABSENT", n_desc, f"新增作品：{n_r}"))
        elif n_st is None:
            o_desc = f"{o_st}({o_yr}" + (f",{o_tc})" if o_tc else ")")
            diffs.append((mid, o_desc, "ABSENT", f"移除作品：{o_r}"))
        else:
            changes = []
            if o_st != n_st:
                changes.append("状态变化")
            if o_yr != n_yr:
                changes.append("年份变化")
            if o_tc != n_tc:
                changes.append(f"tconst变化({o_tc}->{n_tc})")
            label = "、".join(changes) or "选择变化"
            o_desc = f"{o_st}({o_yr}" + (f",{o_tc})" if o_tc != n_tc else ")")
            n_desc = f"{n_st}({n_yr}" + (f",{n_tc})" if o_tc != n_tc else ")")
            diffs.append((mid, o_desc, n_desc, f"选择变化（{label}）：{o_r} -> {n_r}"))

    for mid, o_t, o_p, o_e, n_t, n_p, n_e in conn.execute(
            """
            SELECT COALESCE(o.movie_id, n.movie_id) AS movie_id,
                   o.bound_time, o.bound_precision, o.chosen_evidence,
                   n.bound_time, n.bound_precision, n.chosen_evidence
            FROM (SELECT * FROM task6.inferred_release_bound WHERE release_version_id = %s) o
            FULL OUTER JOIN (SELECT * FROM task6.inferred_release_bound WHERE release_version_id = %s) n
              ON n.movie_id = o.movie_id
            WHERE o.movie_id IS NULL OR n.movie_id IS NULL
               OR o.bound_time IS DISTINCT FROM n.bound_time
               OR o.bound_precision IS DISTINCT FROM n.bound_precision
               OR o.chosen_evidence IS DISTINCT FROM n.chosen_evidence
            ORDER BY 1
            """,
            (old_rv, new_rv)).fetchall():
        if o_t is None:
            diffs.append((mid, "NONE", f"{n_e}({n_t},{n_p})", "推断上界变化（新增上界）"))
        elif n_t is None:
            diffs.append((mid, f"{o_e}({o_t},{o_p})", "NONE", "推断上界变化（移除上界）"))
        else:
            diffs.append((mid, f"{o_e}({o_t},{o_p})", f"{n_e}({n_t},{n_p})", "推断上界变化"))
    return diffs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 6 报告与导出")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sum = sub.add_parser("summary", help="打印统计 JSON")
    p_sum.add_argument("--rv", help="上映信息版本号")
    p_sum.add_argument("--extraction-id", help="抽取 ID")
    p_sum.add_argument("--run-id", help="运行 ID（默认取该版本最新 succeeded 运行）")

    p_ex = sub.add_parser("examples", help="打印实例")
    p_ex.add_argument("--rv", help="上映信息版本号")

    p_exp = sub.add_parser("export", help="导出文件")
    p_exp.add_argument("--rv", help="上映信息版本号")
    p_exp.add_argument("--run-id", help="运行 ID（默认取该版本最新 succeeded 运行）")
    p_exp.add_argument("--run-key", default=None, help="输出目录 run_key（默认取真实 run_id）")
    p_exp.add_argument("--output-dir", default="outputs", help="输出根目录")

    p_diff = sub.add_parser("diff", help="版本差异")
    p_diff.add_argument("old_rv", help="旧上映信息版本号")
    p_diff.add_argument("new_rv", help="新上映信息版本号")

    args = parser.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        if args.cmd == "diff":
            d = diff_versions(conn, args.old_rv, args.new_rv)
            print(f"版本差异 {args.old_rv} -> {args.new_rv}：共 {len(d)} 处变化")
            for mid, before, after, reason in d:
                print(f"  {mid}: {before} -> {after} ({reason})")
            return 0
        eid = getattr(args, "extraction_id", None) or latest_full_extraction(conn)
        rv = getattr(args, "rv", None) or latest_rv(conn, eid)
        if args.cmd == "summary":
            print(json.dumps(build_summary(conn, rv, run_id=args.run_id), ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "examples":
            print(json.dumps(examples(conn, rv), ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "export":
            _, directory, hashes = build_and_write_report(
                conn, rv, Path(args.output_dir), extraction_id=eid, run_id=args.run_id, run_key=args.run_key
            )
            print(f"已导出到 {directory}")
            for k, v in hashes.items():
                print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
