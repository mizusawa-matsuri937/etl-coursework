"""任务 8 统计、导出、实例与版本差异（spec R32、R33、R42、R44，AC12）。

全部查询限定一个去重版本及其登记的四个阶段键，多个版本、多次运行共存时不会串读。导出不含评论正文全文：
实例只显示正文前 80 个字符，CSV 只有 ID、计数与评分。每次导出写 checksums.json（各文件的 SHA-256）。

    python -m etl_task8.report summary [--version rd0001]
    python -m etl_task8.report examples [--version rd0001]
    python -m etl_task8.report export [--version rd0001] [--run-key K] [--output-dir outputs]
    python -m etl_task8.report diff rd0001 rd0002 [--limit 50]
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

from . import sources
from .check import latest_version, version_row
from .versions import diff_versions

SUMMARY_VERSION = "task8-dedup-summary-1"
TEXT_PREFIX = 80
NOTES = (
    "去重后：一条用户评论（逻辑评论）在同一商品、同一作品或同一统计组内只计一次；去重前为原始有效记录。",
    "作品评分不含合集：合集商品上的评论只在按商品统计中出现；同一逻辑评论同时在合集与单品上时按单品归属作品。",
    "分母为有评分的逻辑评论数（逻辑评论都有评分）；隔离与不完整记录不进评分。",
    "按 movie_id 统计时 shared_with_group 给出与同组其他作品共享的逻辑评论数，各作品求和不等于逻辑评论总数；按统计组时每条只计一次。",
    "按商品统计时同一逻辑评论在它每个副本所在的 ASIN 上各算一次，各 ASIN 求和不等于逻辑评论总数。",
    "近似评论候选（near_duplicate）各计一次；统计组的 sensitivity_* 为每组近似评论只计一次时的分母与平均分。",
)


def output_root(output_dir: Path) -> Path:
    return output_dir / "task8"


def run_dir(output_dir: Path, eid: str, rd: str, run_key: str) -> Path:
    return output_root(output_dir) / eid / rd / run_key


def failures_dir(output_dir: Path, run_key: str) -> Path:
    return output_root(output_dir) / "_failures" / run_key


def _f(x: Any, nd: int = 4) -> float | None:
    return None if x is None else round(float(x), nd)


def resolve_run(conn: psycopg.Connection, rd: str, run_id: str | None = None) -> tuple[str, dict[str, Any]]:
    """版本对应的真实运行（run_id, 阶段计数）：显式 run_id 必须属于该版本；reused 运行读它复用的那次运行的计数；
    未给出时取该版本最新的 succeeded 运行。"""
    if run_id is not None:
        r = conn.execute("SELECT run_id, status, review_dedup_version_id, counts FROM task8.run WHERE run_id = %s",
                         (run_id,)).fetchone()
        if r is None:
            raise ValueError(f"运行 {run_id!r} 在 task8.run 中不存在")
        if r[2] != rd:
            raise ValueError(f"运行 {run_id!r} 属于去重版本 {r[2]!r}，而非 {rd!r}")
        if r[1] == "reused":
            orig = conn.execute("SELECT counts FROM task8.run WHERE run_id = %s AND status = 'succeeded'",
                                ((r[3] or {}).get("reused_run"),)).fetchone()
            return r[0], dict(orig[0]) if orig else dict(r[3] or {})
        return r[0], dict(r[3] or {})
    r = conn.execute("SELECT run_id, counts FROM task8.run WHERE review_dedup_version_id = %s AND kind = 'dedup' "
                     "AND status = 'succeeded' ORDER BY started_at DESC LIMIT 1", (rd,)).fetchone()
    if r is not None:
        return r[0], dict(r[1] or {})
    return version_row(conn, rd)["run_id"], {}


def build_summary(conn: psycopg.Connection, rd: str, *, run_id: str | None = None) -> dict[str, Any]:
    """版本全貌统计（R42）：读取、有效、隔离；helpfulness；层次；逻辑评论；候选；类别；家族；作品与统计组评分。"""
    row = version_row(conn, rd)
    actual_run, run_counts = resolve_run(conn, rd, run_id)
    eid = row["extraction_id"]
    ck, fk, dk, ak = (row[f"{s}_stage_key"] for s in ("clean", "family", "dedup", "attribution"))

    def one(sql: str, *a: Any) -> Any:
        return conn.execute(sql, a).fetchone()[0]

    def tally(sql: str, *a: Any) -> dict[str, int]:
        return {str(k): int(n) for k, n in conn.execute(sql, a)}

    status = tally("SELECT mapping_status, count(*) FROM task8.review_mapping WHERE dedup_stage_key = %s GROUP BY 1", dk)
    levels = tally("SELECT match_level, count(*) FROM task8.review_mapping WHERE dedup_stage_key = %s "
                   "AND match_level IS NOT NULL GROUP BY 1 ORDER BY 1", dk)
    quarantine = tally("SELECT array_to_string(reasons, '+'), count(*) FROM public.quarantine_record "
                       "WHERE extraction_id = %s GROUP BY 1 ORDER BY 1", eid)
    logical = one("SELECT count(*) FROM task8.logical_review WHERE dedup_stage_key = %s", dk)
    valid = status.get("mapped", 0) + status.get("incomplete", 0)
    duplicates = sum(v for k, v in levels.items() if k != "representative")
    diff = tally("SELECT match_level || ':' || coalesce(array_to_string(diff_fields, '+'), ''), count(*) "
                 "FROM task8.review_mapping WHERE dedup_stage_key = %s AND match_level IN "
                 "('D2_same_asin_variant', 'D3_family_copy') GROUP BY 1 ORDER BY 1", dk)
    fam = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE asin_count > 1), coalesce(max(asin_count), 0), "
        "count(*) FILTER (WHERE weak_support), count(*) FILTER (WHERE weak_support AND asin_count > 1), "
        "coalesce(sum(asin_count) FILTER (WHERE weak_support AND asin_count > 1), 0) "
        "FROM task8.review_family WHERE family_stage_key = %s", (fk,)).fetchone()
    weak_d3 = one(
        "SELECT count(*) FROM task8.review_mapping m JOIN task8.family_member f ON f.family_stage_key = %s "
        "AND f.asin = m.asin JOIN task8.review_family r ON r.family_stage_key = f.family_stage_key "
        "AND r.family_id = f.family_id WHERE m.dedup_stage_key = %s AND m.match_level = 'D3_family_copy' "
        "AND r.weak_support AND r.asin_count > 1", fk, dk)
    cands = tally("SELECT candidate_type || ':' || coalesce(basis->>'scope', 'cross_family'), count(*) "
                  "FROM task8.dup_candidate WHERE stage_key IN (%s, %s) GROUP BY 1 ORDER BY 1", dk, ak)
    c1_reviews = one("SELECT coalesce(sum(cardinality(logical_review_ids)), 0) FROM task8.dup_candidate "
                     "WHERE stage_key = %s AND candidate_type = 'cross_family_same_content'", dk)
    categories = {c: {"asins": n, "raw_valid": v, "raw_quarantined": q, "logical_per_asin_sum": lg}
                  for c, n, v, q, lg in conn.execute(
                      "SELECT product_category, count(*), sum(raw_valid), sum(raw_quarantined), sum(logical_reviews) "
                      "FROM task8.product_review_stats WHERE review_dedup_version_id = %s GROUP BY 1 ORDER BY 1", (rd,))}
    for c, n in conn.execute(
            "SELECT p.product_category, count(DISTINCT m.logical_review_id) FROM task8.review_mapping m "
            "JOIN task8.product_review_stats p ON p.review_dedup_version_id = %s AND p.asin = m.asin "
            "WHERE m.dedup_stage_key = %s AND m.mapping_status = 'mapped' GROUP BY 1", (rd, dk)):
        categories[c]["logical_distinct"] = n
    # 作品与统计组（R30–R32）
    mv = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE logical_reviews > 0), coalesce(sum(raw_records), 0), "
        "coalesce(sum(raw_rated), 0), sum(raw_avg_score * raw_rated) / nullif(sum(raw_rated), 0), "
        "coalesce(sum(logical_reviews), 0), count(*) FILTER (WHERE raw_records <> logical_reviews), "
        "count(*) FILTER (WHERE abs(raw_avg_score - logical_avg_score) >= 0.1), max(abs(raw_avg_score - logical_avg_score)), "
        "coalesce(sum(shared_with_group), 0) FROM task8.movie_review_stats WHERE review_dedup_version_id = %s",
        (rd,)).fetchone()
    gr = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE movie_count > 1), coalesce(sum(movie_count) FILTER (WHERE movie_count > 1), 0), "
        "coalesce(max(movie_count), 0), coalesce(sum(logical_reviews), 0), "
        "sum(logical_avg_score * logical_rated) / nullif(sum(logical_rated), 0), coalesce(sum(near_dup_pairs), 0), "
        "coalesce(sum(sensitivity_rated), 0), sum(sensitivity_avg_score * sensitivity_rated) / nullif(sum(sensitivity_rated), 0) "
        "FROM task8.stat_group_review_stats WHERE review_dedup_version_id = %s", (rd,)).fetchone()
    work = conn.execute(
        "SELECT count(*) FILTER (WHERE n >= 2), count(*) FILTER (WHERE c) FROM ("
        "SELECT logical_review_id, count(*) n, bool_or(also_on_collection) c FROM task8.logical_review_work "
        "WHERE attribution_stage_key = %s GROUP BY 1) x", (ak,)).fetchone()
    only_collection = one(
        "SELECT count(DISTINCT m.logical_review_id) FROM task8.review_mapping m JOIN task7.edition_resolved e "
        "ON e.conflict_version_id = %s AND e.asin = m.asin AND e.product_kind = 'collection' "
        "WHERE m.dedup_stage_key = %s AND m.mapping_status = 'mapped' AND NOT EXISTS ("
        "SELECT 1 FROM task8.logical_review_work w WHERE w.attribution_stage_key = %s "
        "AND w.logical_review_id = m.logical_review_id)", row["conflict_version_id"], dk, ak)
    # 单品上的 mapped 记录数、不同 (逻辑评论, ASIN) 数：拆出同 ASIN 重复与跨单品的平台复制
    singles_mapped, lid_asin = conn.execute(
        "SELECT count(*), count(DISTINCT (m.logical_review_id, m.asin)) FROM task8.review_mapping m "
        "JOIN task7.edition_resolved e ON e.conflict_version_id = %s AND e.asin = m.asin AND e.product_kind = 'movie' "
        "AND cardinality(e.movie_ids) >= 1 WHERE m.dedup_stage_key = %s AND m.mapping_status = 'mapped'",
        (row["conflict_version_id"], dk)).fetchone()
    attributed = gr[4]
    movies = {
        "movies": mv[0], "movies_with_reviews": mv[1], "raw_records_on_singles": mv[2], "raw_rated": mv[3],
        "raw_avg_score": _f(mv[4]), "logical_per_movie_sum": mv[5], "logical_distinct": attributed,
        "logical_avg_score": _f(gr[5]), "movies_count_changed": mv[6], "movies_avg_changed_ge_0_1": mv[7],
        "max_avg_change": _f(mv[8]), "shared_with_group_sum": mv[9],
        "logical_in_multiple_movies": work[0], "also_on_collection": work[1],
        "logical_only_on_collections": only_collection,
        # R32：去重前（单品原始记录）→ 去重后（逻辑评论）的差异来源，四项之和 = 原始记录 − 逻辑评论
        "difference_sources": {
            "incomplete_records": mv[2] - singles_mapped,
            "same_asin_duplicates": singles_mapped - lid_asin,
            "platform_copies_across_singles": lid_asin - mv[5],
            "cross_movie_sharing": mv[5] - attributed,
        },
    }
    groups = {"stat_groups": gr[0], "multi_movie_groups": gr[1], "movies_in_multi_groups": gr[2],
              "max_group_movies": gr[3], "logical_reviews": gr[4], "logical_avg_score": _f(gr[5]),
              "near_dup_pairs": gr[6], "sensitivity_rated": gr[7], "sensitivity_avg_score": _f(gr[8])}
    return {
        "summary_version": SUMMARY_VERSION, "review_dedup_version_id": rd, "parent_version_id": row["parent_version_id"],
        "version_status": row["status"], "run_id": actual_run, "stage_counts": run_counts, "extraction_id": eid,
        "t3_rule_version": row["t3_rule_version"], "conflict_version_id": row["conflict_version_id"],
        "rule_version": row["rule_version"], "stage_keys": {"clean": ck, "family": fk, "dedup": dk, "attribution": ak},
        "records": {"read": sum(status.values()), "valid": valid, "quarantined": status.get("quarantined", 0),
                    "quarantine_reasons": quarantine, "incomplete": status.get("incomplete", 0),
                    "key_complete": status.get("mapped", 0)},
        "helpfulness": tally("SELECT helpfulness_status, count(*) FROM task8.review_clean WHERE clean_stage_key = %s "
                             "GROUP BY 1 ORDER BY 1", ck),
        "issues": tally("SELECT code, count(*) FROM task8.review_issue WHERE stage_key IN (%s, %s) GROUP BY 1 ORDER BY 1",
                        ck, dk),
        "duplicates": {"by_level": levels, "total": duplicates, "diff_fields": diff,
                       "check": f"{duplicates} = 键完整记录 {status.get('mapped', 0)} − 逻辑评论 {logical}"},
        "logical_reviews": logical,
        "logical_without_helpfulness": one("SELECT count(*) FROM task8.logical_review WHERE dedup_stage_key = %s "
                                           "AND total_votes IS NULL", dk),
        "reviewers": {"total": one("SELECT count(*) FROM task8.reviewer WHERE dedup_stage_key = %s", dk),
                      "with_profile_variants": one("SELECT count(*) FROM task8.reviewer WHERE dedup_stage_key = %s "
                                                   "AND profile_variant_count > 1", dk)},
        "candidates": {"by_type_scope": cands, "cross_family_logical_reviews": c1_reviews},
        "families": {"families": fam[0], "multi_asin": fam[1], "max_asins": fam[2], "weak_support_all": fam[3],
                     "weak_support_multi_asin": fam[4], "weak_support_multi_asin_asins": fam[5],
                     "weak_support_multi_asin_D3_records": weak_d3},
        "categories": categories, "movies": movies, "stat_groups": groups, "notes": list(NOTES),
    }


# ---------------------------------------------------------------- 实例（R44）

def _texts(conn: psycopg.Connection, eid: str, srids: list[str]) -> dict[str, str | None]:
    return {k: (v[:TEXT_PREFIX] if v is not None else None) for k, v in sources.review_texts(conn, eid, srids).items()}


def examples(conn: psycopg.Connection, rd: str, per_category: int = 3) -> dict[str, list[dict[str, Any]]]:
    """R44 的各类实例，每类最多 per_category 例，附 source_review_id 定位；正文只显示前 80 个字符。"""
    row = version_row(conn, rd)
    eid, cv = row["extraction_id"], row["conflict_version_id"]
    ck, fk, dk, ak = (row[f"{s}_stage_key"] for s in ("clean", "family", "dedup", "attribution"))
    n = per_category

    def logical_detail(lid: str) -> dict[str, Any]:
        recs = conn.execute(
            "SELECT m.record_no, m.source_review_id, m.asin, m.match_level, m.diff_fields, c.helpfulness_raw, "
            "c.profile_name_raw FROM task8.review_mapping m JOIN task8.review_clean c ON c.clean_stage_key = %s "
            "AND c.record_no = m.record_no WHERE m.dedup_stage_key = %s AND m.logical_review_id = %s "
            "ORDER BY m.record_no LIMIT 12", (ck, dk, lid)).fetchall()
        lr = conn.execute("SELECT reviewer_id, review_day, score, family_id, copy_count, asin_count FROM task8.logical_review "
                          "WHERE dedup_stage_key = %s AND logical_review_id = %s", (dk, lid)).fetchone()
        text = _texts(conn, eid, [recs[0][1]]).get(recs[0][1]) if recs else None
        return {"logical_review_id": lid, "reviewer_id": lr[0], "review_day": str(lr[1]), "score": _f(lr[2]),
                "family_id": lr[3], "copy_count": lr[4], "asin_count": lr[5], "text_prefix": text,
                "records": [{"record_no": r[0], "source_review_id": r[1], "asin": r[2], "match_level": r[3],
                             "diff_fields": list(r[4]), "helpfulness_raw": r[5], "profile_name_raw": r[6]}
                            for r in recs]}

    def by_level(level: str) -> list[dict[str, Any]]:
        ids = [r[0] for r in conn.execute(
            "SELECT DISTINCT logical_review_id FROM (SELECT logical_review_id FROM task8.review_mapping "
            "WHERE dedup_stage_key = %s AND match_level = %s ORDER BY record_no LIMIT 50) x LIMIT %s", (dk, level, n))]
        return [logical_detail(lid) for lid in sorted(ids)]

    weak = [r[0] for r in conn.execute(
        "SELECT family_id FROM task8.review_family WHERE family_stage_key = %s AND weak_support AND asin_count > 1 "
        "ORDER BY min_asin LIMIT %s", (fk, n))]
    weak_ex = []
    for fam in weak:
        asins = [r[0] for r in conn.execute("SELECT asin FROM task8.family_member WHERE family_stage_key = %s "
                                            "AND family_id = %s ORDER BY asin", (fk, fam))]
        lids = [r[0] for r in conn.execute("SELECT logical_review_id FROM task8.logical_review WHERE dedup_stage_key = %s "
                                           "AND family_id = %s ORDER BY logical_review_id LIMIT 2", (dk, fam))]
        weak_ex.append({"family_id": fam, "asins": asins, "logical_reviews": [logical_detail(x) for x in lids]})

    def cands(where: str, *a: Any) -> list[dict[str, Any]]:
        out = []
        for cid, typ, reviewer, ids, sim, basis in conn.execute(
                "SELECT candidate_id, candidate_type, reviewer_id, logical_review_ids, similarity, basis "
                f"FROM task8.dup_candidate WHERE {where} ORDER BY candidate_id LIMIT %s", (*a, n)):
            out.append({"candidate_id": cid, "type": typ, "reviewer_id": reviewer, "similarity": _f(sim),
                        "basis": dict(basis), "logical_reviews": [logical_detail(x) for x in list(ids)[:3]]})
        return out

    def issues(codes: tuple[str, ...]) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT record_no, source_review_id, code, evidence FROM task8.review_issue "
                            "WHERE stage_key = %s AND code = ANY(%s) ORDER BY record_no LIMIT %s",
                            (ck, list(codes), n)).fetchall()
        texts = _texts(conn, eid, [r[1] for r in rows])
        return [{"record_no": r[0], "source_review_id": r[1], "code": r[2], "evidence": r[3], "text_prefix": texts.get(r[1])}
                for r in rows]

    def work_examples(where: str) -> list[dict[str, Any]]:
        out = []
        for lid, movies, via, coll in conn.execute(
                "SELECT logical_review_id, array_agg(movie_id ORDER BY movie_id), array_agg(DISTINCT a ORDER BY a), "
                "bool_or(also_on_collection) FROM task8.logical_review_work, unnest(via_asins) a "
                f"WHERE attribution_stage_key = %s GROUP BY 1 HAVING {where} ORDER BY 1 LIMIT %s", (ak, n)):
            out.append({"movie_ids": sorted(set(movies)), "via_asins": list(via), "also_on_collection": coll,
                        **logical_detail(lid)})
        return out

    only_coll = [r[0] for r in conn.execute(
        "SELECT DISTINCT m.logical_review_id FROM task7.edition_resolved e JOIN task8.review_mapping m "
        "ON m.dedup_stage_key = %s AND m.asin = e.asin AND m.mapping_status = 'mapped' WHERE e.conflict_version_id = %s "
        "AND e.product_kind = 'collection' AND NOT EXISTS (SELECT 1 FROM task8.logical_review_work w "
        "WHERE w.attribution_stage_key = %s AND w.logical_review_id = m.logical_review_id) ORDER BY 1 LIMIT %s",
        (dk, cv, ak, n))]
    groups = []
    for gid, cnt, basis in conn.execute("SELECT stat_group_id, movie_count, basis FROM task8.stat_group "
                                        "WHERE attribution_stage_key = %s AND movie_count > 1 ORDER BY stat_group_id "
                                        "LIMIT %s", (ak, n)):
        stats = conn.execute("SELECT logical_reviews, logical_avg_score, raw_records, raw_avg_score, near_dup_pairs, "
                             "sensitivity_rated, sensitivity_avg_score FROM task8.stat_group_review_stats "
                             "WHERE review_dedup_version_id = %s AND stat_group_id = %s", (rd, gid)).fetchone()
        members = [{"movie_id": m, "logical_reviews": lr, "shared_with_group": sh} for m, lr, sh in conn.execute(
            "SELECT movie_id, logical_reviews, shared_with_group FROM task8.movie_review_stats "
            "WHERE review_dedup_version_id = %s AND stat_group_id = %s ORDER BY movie_id", (rd, gid))]
        groups.append({"stat_group_id": gid, "movie_count": cnt, "basis": list(basis)[:6], "members": members,
                       "logical_reviews": stats[0] if stats else None, "logical_avg_score": _f(stats[1]) if stats else None,
                       "raw_records": stats[2] if stats else None, "raw_avg_score": _f(stats[3]) if stats else None,
                       "near_dup_pairs": stats[4] if stats else None,
                       "sensitivity": {"rated": stats[5], "avg_score": _f(stats[6])} if stats else None})
    return {
        "D1_exact_same_asin": by_level("D1_exact_same_asin"),
        "D2_same_asin_variant": by_level("D2_same_asin_variant"),
        "D3_family_copy": by_level("D3_family_copy"),
        "weak_support_family": weak_ex,
        "C1_cross_family_same_content": cands("stage_key = %s AND candidate_type = 'cross_family_same_content'", dk),
        "C2_near_duplicate_family": cands("stage_key = %s AND candidate_type = 'near_duplicate'", dk),
        "C2_near_duplicate_stat_group": cands("stage_key = %s AND candidate_type = 'near_duplicate'", ak),
        "helpfulness_anomaly": issues(("numerator_exceeds_denominator", "negative_votes", "unparseable_helpfulness",
                                       "missing_helpfulness")),
        "incomplete_record": issues(("incomplete_review",)),
        "collection_review": ([logical_detail(x) for x in only_coll]
                              + work_examples("bool_or(also_on_collection)")[:max(0, n - len(only_coll))]),
        "cross_movie_shared": work_examples("count(DISTINCT movie_id) >= 2"),
        "stat_group": groups,
    }


def render_examples_markdown(ex: dict[str, list[dict[str, Any]]]) -> str:
    lines = ["# 任务 8 实例报告", "",
             "每类最多 3 例，附 `source_review_id` 定位；正文只显示前 80 个字符（`text_prefix`），没有实例的类别写明“无”。", ""]
    for cat, items in ex.items():
        lines.append(f"## {cat}（{len(items)} 例）")
        if not items:
            lines += ["无。", ""]
            continue
        for item in items:
            label = item.get("logical_review_id") or item.get("family_id") or item.get("candidate_id") \
                or item.get("stat_group_id") or item.get("source_review_id") or ""
            lines.append(f"- `{label}`")
            lines.append("  ```json\n  " + json.dumps(item, ensure_ascii=False, indent=2, default=str).replace("\n", "\n  ")
                         + "\n  ```")
        lines.append("")
    return "\n".join(lines)


def render_markdown(s: dict[str, Any]) -> str:
    r, d, m, g, f = s["records"], s["duplicates"], s["movies"], s["stat_groups"], s["families"]
    cat_rows = "\n".join(
        f"| {c} | {v['asins']} | {v['raw_valid']} | {v['raw_quarantined']} | {v.get('logical_distinct', 0)} | "
        f"{v['logical_per_asin_sum']} |" for c, v in s["categories"].items())
    notes = "\n".join(f"- {n}" for n in s["notes"])
    return f"""# 任务 8 评论清洗与重复识别报告（{s['review_dedup_version_id']}）

- **抽取 ID**：`{s['extraction_id']}`；任务 3 规则 `{s['t3_rule_version']}`、任务 7 版本 `{s['conflict_version_id']}`、规则 `{s['rule_version']}`
- **版本状态**：{s['version_status']}；运行 `{s['run_id']}`；上一版本 `{s['parent_version_id']}`

## 1. 记录（R03、R05、R08）

- 读取 {r['read']} 条：有效 {r['valid']}、隔离 {r['quarantined']}（{json.dumps(r['quarantine_reasons'], ensure_ascii=False)}）；有效中不完整 {r['incomplete']}、键完整 {r['key_complete']}
- helpfulness：{json.dumps(s['helpfulness'], ensure_ascii=False)}（`zero_zero` 不是异常）
- 质量问题：{json.dumps(s['issues'], ensure_ascii=False)}

## 2. 重复与逻辑评论（R14–R19）

- 逻辑评论 {s['logical_reviews']} 条；重复评论数 {d['total']}（{d['check']}）
- 按层次：{json.dumps(d['by_level'], ensure_ascii=False)}
- 差异字段：{json.dumps(d['diff_fields'], ensure_ascii=False)}
- 没有合格 helpfulness 的逻辑评论 {s['logical_without_helpfulness']} 条；用户 {s['reviewers']['total']} 个，其中 {s['reviewers']['with_profile_variants']} 个有多个显示名

## 3. 评论家族（R12、R13）

- 家族 {f['families']} 个，多 ASIN 家族 {f['multi_asin']} 个，最大 {f['max_asins']} 个 ASIN
- `weak_support`：全部 {f['weak_support_all']} 个；多 ASIN 的 {f['weak_support_multi_asin']} 个（{f['weak_support_multi_asin_asins']} 个 ASIN、`D3` 复制记录 {f['weak_support_multi_asin_D3_records']} 条）

## 4. 不合并的候选（R20、R21）

- {json.dumps(s['candidates']['by_type_scope'], ensure_ascii=False)}；跨家族同内容涉及逻辑评论 {s['candidates']['cross_family_logical_reviews']} 条

## 5. 按商品类别（R26、R28）

| 类别 | ASIN | 原始有效 | 隔离 | 逻辑评论（去重） | 各 ASIN 逻辑评论之和 |
|---|---|---|---|---|---|
{cat_rows}

## 6. 作品与统计组评分（R29–R32）

- 作品 {m['movies']} 部（有归属评论 {m['movies_with_reviews']} 部）；去重前单品原始记录 {m['raw_records_on_singles']} 条、平均 {m['raw_avg_score']}；去重后归属作品的逻辑评论 {m['logical_distinct']} 条、平均 {m['logical_avg_score']}
- 差异来源：{json.dumps(m['difference_sources'], ensure_ascii=False)}
- 评论数因去重变化的作品 {m['movies_count_changed']} 部；平均分变化 ≥ 0.1 的 {m['movies_avg_changed_ge_0_1']} 部，最大 {m['max_avg_change']}
- 归属 ≥ 2 部作品的逻辑评论 {m['logical_in_multiple_movies']} 条；按 `movie_id` 求和 {m['logical_per_movie_sum']}；`shared_with_group` 之和 {m['shared_with_group_sum']}
- 合集：只在合集上的逻辑评论 {m['logical_only_on_collections']} 条（不进作品评分）；同时在合集与单品上的 {m['also_on_collection']} 条（按单品归属）
- 统计组 {g['stat_groups']} 个，多作品组 {g['multi_movie_groups']} 个（{g['movies_in_multi_groups']} 部作品，最大 {g['max_group_movies']} 部）；组内近似评论对 {g['near_dup_pairs']}
- 敏感性（每组近似评论只计一次）：分母 {g['logical_reviews']} → {g['sensitivity_rated']}，平均分 {g['logical_avg_score']} → {g['sensitivity_avg_score']}

## 7. 口径说明

{notes}
"""


def export_files(conn: psycopg.Connection, rd: str, directory: Path, examples_data: dict[str, Any] | None = None, *,
                 run_id: str | None = None, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    """导出报告与统计 CSV，写 checksums.json，返回 文件名 -> SHA-256。不含评论正文全文。"""
    directory.mkdir(parents=True, exist_ok=True)
    summary = summary or build_summary(conn, rd, run_id=run_id)
    examples_data = examples_data if examples_data is not None else examples(conn, rd)
    files: dict[str, bytes] = {
        "summary.json": (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode(),
        "report.md": render_markdown(summary).encode(),
        "examples.md": render_examples_markdown(examples_data).encode(),
    }

    def csv_of(sql: str, params: tuple, columns: list[str]) -> bytes:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(columns)
        for r in conn.execute(sql, params):
            w.writerow([json.dumps(x, ensure_ascii=False) if isinstance(x, (dict, list)) else x for x in r])
        return buf.getvalue().encode()

    mcols = ["movie_id", "stat_group_id", "raw_records", "raw_rated", "raw_avg_score", "logical_reviews", "logical_rated",
             "logical_avg_score", "shared_with_group", "near_dup_candidates"]
    files["movie_review_stats.csv"] = csv_of(f"SELECT {', '.join(mcols)} FROM task8.movie_review_stats "
                                             "WHERE review_dedup_version_id = %s ORDER BY movie_id", (rd,), mcols)
    gcols = ["stat_group_id", "movie_count", "raw_records", "raw_rated", "raw_avg_score", "logical_reviews",
             "logical_rated", "logical_avg_score", "near_dup_pairs", "sensitivity_rated", "sensitivity_avg_score"]
    files["stat_group_review_stats.csv"] = csv_of(f"SELECT {', '.join(gcols)} FROM task8.stat_group_review_stats "
                                                  "WHERE review_dedup_version_id = %s ORDER BY stat_group_id", (rd,), gcols)
    pcols = ["asin", "product_category", "has_movie", "raw_valid", "raw_quarantined", "raw_rated", "raw_avg_score",
             "logical_reviews", "logical_rated", "logical_avg_score"]
    files["product_review_stats.csv"] = csv_of(f"SELECT {', '.join(pcols)} FROM task8.product_review_stats "
                                               "WHERE review_dedup_version_id = %s ORDER BY asin", (rd,), pcols)
    row = version_row(conn, rd)
    files["dup_candidate.csv"] = csv_of(
        "SELECT candidate_id, stage, candidate_type, reviewer_id, logical_review_ids, similarity, basis "
        "FROM task8.dup_candidate WHERE stage_key IN (%s, %s) ORDER BY candidate_id",
        (row["dedup_stage_key"], row["attribution_stage_key"]),
        ["candidate_id", "stage", "candidate_type", "reviewer_id", "logical_review_ids", "similarity", "basis"])
    digests: dict[str, Any] = {}
    for name, content in files.items():
        reporting.write_atomic(directory / name, content)
        digests[name] = reporting.file_digest(directory / name)
    reporting.write_atomic(directory / "checksums.json",
                           (json.dumps(digests, ensure_ascii=False, indent=2) + "\n").encode())
    return digests


def build_and_write_report(conn: psycopg.Connection, rd: str | None = None, output_dir: Path = Path("outputs"), *,
                           extraction_id: str | None = None, run_id: str | None = None, run_key: str | None = None
                           ) -> tuple[dict[str, Any], Path, dict[str, Any]]:
    eid = extraction_id or (version_row(conn, rd)["extraction_id"] if rd else latest_full_extraction(conn))
    resolved = rd or latest_version(conn, eid)
    summary = build_summary(conn, resolved, run_id=run_id)
    directory = run_dir(output_dir, eid, resolved, run_key or summary["run_id"])
    digests = export_files(conn, resolved, directory, run_id=run_id, summary=summary)
    return summary, directory, digests


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 8 报告与导出")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, text in (("summary", "打印统计 JSON"), ("examples", "打印实例"), ("export", "导出文件")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--version", help="去重版本号（默认最近一次）")
        p.add_argument("--extraction-id", help="抽取 ID")
        if name != "examples":
            p.add_argument("--run-id", help="运行 ID（默认取该版本最新 succeeded 运行；不存在的会被拒绝）")
        if name == "export":
            p.add_argument("--run-key", default=None, help="输出目录 run_key（默认取真实 run_id）")
            p.add_argument("--output-dir", default="outputs", help="输出根目录")
    p_diff = sub.add_parser("diff", help="两个版本的差异")
    p_diff.add_argument("old_version")
    p_diff.add_argument("new_version")
    p_diff.add_argument("--limit", type=int, default=50, help="每层最多列出的变化数")
    args = parser.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        if args.cmd == "diff":
            d = diff_versions(conn, args.old_version, args.new_version)
            by_layer: dict[str, int] = {}
            for x in d:
                by_layer[x[0]] = by_layer.get(x[0], 0) + 1
            print(f"版本差异 {args.old_version} -> {args.new_version}：共 {len(d)} 处变化 "
                  f"{json.dumps(by_layer, ensure_ascii=False)}")
            shown: dict[str, int] = {}
            for layer, ent, fld, before, after, why in d:
                shown[layer] = shown.get(layer, 0) + 1
                if shown[layer] <= args.limit:
                    print(f"  [{layer}] {ent} {fld}: {before} -> {after}（{why}）")
            return 0
        eid = args.extraction_id or latest_full_extraction(conn)
        rd = args.version or latest_version(conn, eid)
        if args.cmd == "summary":
            print(json.dumps(build_summary(conn, rd, run_id=args.run_id), ensure_ascii=False, indent=2, default=str))
        elif args.cmd == "examples":
            print(json.dumps(examples(conn, rd), ensure_ascii=False, indent=2, default=str))
        else:
            _, directory, hashes = build_and_write_report(conn, rd, Path(args.output_dir), extraction_id=eid,
                                                          run_id=args.run_id, run_key=args.run_key)
            print(f"已导出到 {directory}")
            for k, v in hashes.items():
                print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
