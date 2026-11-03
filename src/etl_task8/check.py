"""任务 8 只读核对（spec R03、R19、R27–R30、R38，AC15、AC16；与 DAG 的 verify 步骤共用 verify 与 problems）。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task8.check                         # 默认抽取的最新去重版本
    python -m etl_task8.check --version rd0001
输出一段 JSON；全部一致时退出码 0，否则 1。整个核对在只读事务里进行，不写任何表。
命令行默认要求版本已 verified；DAG 的 verify 步骤在标记 verified 之前调用，所以用 expect_verified=False。
核对覆盖：阶段键登记、依赖链与完成标记；每条原始记录恰好一行映射且状态与任务 1 的有效、隔离划分一致；代表记录数与
逻辑评论数、副本数之和与映射一致；作品归属只来自 cv 的电影单品（不含合集）；统计组覆盖全部作品且一条逻辑评论不跨组；
三张统计表的总数与归属一致；候选引用的逻辑评论存在；运行与版本状态一致；任务 1–7 主表行数快照。
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
from .rules import RULESETS, RuleSet
from .run import STAGES, RunInputs, short_key

UPSTREAM_TABLES = (
    ("public", "review_record"), ("public", "review_staging"), ("public", "quarantine_record"),
    ("task2", "product_manifest"), ("task3", "product_classification"), ("task4", "movie"), ("task5", "person"),
    ("task6", "release_selection"), ("task7", "conflict_version"), ("task7", "edition_resolved"),
    ("task7", "shared_tconst_result"),
)
VERSION_COLUMNS = ("review_dedup_version_id", "extraction_id", "parent_version_id", "run_id", "input_key",
                   "t3_rule_version", "conflict_version_id", "rule_version", "clean_stage_key", "family_stage_key",
                   "dedup_stage_key", "attribution_stage_key", "status", "counts", "created_at", "verified_at")


def latest_version(conn: psycopg.Connection, extraction_id: str) -> str:
    row = conn.execute("SELECT review_dedup_version_id FROM task8.dedup_version WHERE extraction_id = %s "
                       "ORDER BY created_at DESC, review_dedup_version_id DESC LIMIT 1", (extraction_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"抽取 {extraction_id} 还没有去重版本")
    return row[0]


def version_row(conn: psycopg.Connection, rd: str) -> dict[str, Any]:
    r = conn.execute(f"SELECT {', '.join(VERSION_COLUMNS)} FROM task8.dedup_version WHERE review_dedup_version_id = %s",
                     (rd,)).fetchone()
    if r is None:
        raise ValueError(f"去重版本 {rd} 不存在")
    d = dict(zip(VERSION_COLUMNS, r))
    d["counts"] = dict(d["counts"])
    for k in ("created_at", "verified_at"):
        d[k] = d[k].isoformat() if d[k] else None
    return d


def inputs_of(conn: psycopg.Connection, row: dict[str, Any]) -> RunInputs:
    link = conn.execute("SELECT link_stage_key FROM task7.conflict_version WHERE conflict_version_id = %s",
                        (row["conflict_version_id"],)).fetchone()
    return RunInputs(extraction_id=row["extraction_id"], t3_rule_version=row["t3_rule_version"],
                     conflict_version_id=row["conflict_version_id"], rule_version=row["rule_version"],
                     cv_link_stage_key=link[0] if link else None)


def verify(conn: psycopg.Connection, rd: str, rules: RuleSet | None = None) -> dict[str, Any]:
    """只读核对一个去重版本。rules：未登记的规则版本（测试）由调用方给出，用于重算阶段键。"""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        return _verify(conn, rd, rules)


def _verify(conn: psycopg.Connection, rd: str, rules: RuleSet | None) -> dict[str, Any]:
    row = version_row(conn, rd)
    eid, cv = row["extraction_id"], row["conflict_version_id"]
    keys = {s: row[f"{s.removeprefix('review_')}_stage_key"] for s in STAGES}
    ck, fk, dk, ak = (keys[s] for s in STAGES)
    out: dict[str, Any] = {"review_dedup_version_id": rd, "extraction_id": eid, "version_status": row["status"],
                           "conflict_version_id": cv, "t3_rule_version": row["t3_rule_version"],
                           "rule_version": row["rule_version"], "stage_keys": keys}
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731

    # 1. 阶段键：登记、依赖链、完成标记；规则版本已登记（或调用方给出规则）时按输入重算
    chain_bad = 0
    for k, s in enumerate(STAGES):
        reg = conn.execute("SELECT stage, parent_stage_key, extraction_id FROM task8.stage_key WHERE stage_key = %s",
                           (keys[s],)).fetchone()
        if reg is None or reg[0] != s or reg[2] != eid or reg[1] != (keys[STAGES[k - 1]] if k else None):
            chain_bad += 1
    out["stage_chain_bad"] = chain_bad
    out["stages_incomplete"] = sum(1 for s in STAGES if not one(
        "SELECT EXISTS (SELECT 1 FROM task8.entity_stage WHERE stage_key = %s AND entity_id = '__complete__')", keys[s]))
    r = rules or RULESETS.get(row["rule_version"])
    if r is not None and r.version == row["rule_version"]:
        inputs = inputs_of(conn, row)
        out["stage_key_mismatch"] = sum(short_key(s, inputs.full_stage_key(s, r)) != keys[s] for s in STAGES)
    else:
        out["stage_key_mismatch"] = 0
        out["stage_key_recompute"] = "skipped（规则版本未登记）"
    out["run_version_mismatch"] = one(
        "SELECT count(*) FROM task8.run r JOIN task8.dedup_version v ON v.review_dedup_version_id = r.review_dedup_version_id "
        "WHERE v.review_dedup_version_id = %s AND r.status IN ('succeeded', 'reused') AND v.status <> 'verified' "
        "AND r.run_id <> %s", rd, row["run_id"])

    # 2. 映射覆盖（R03、R05、R08、R19）
    out["records_expected"] = one("SELECT count(*) FROM public.review_record WHERE extraction_id = %s", eid)
    out["mapping_total"] = one("SELECT count(*) FROM task8.review_mapping WHERE dedup_stage_key = %s", dk)
    out["records_unmapped"] = one(
        "SELECT count(*) FROM public.review_record r WHERE r.extraction_id = %s AND NOT EXISTS ("
        "SELECT 1 FROM task8.review_mapping m WHERE m.dedup_stage_key = %s AND m.record_no = r.record_no)", eid, dk)
    out["mapping_extra"] = out["mapping_total"] - (out["records_expected"] - out["records_unmapped"])
    out["quarantine_status_mismatch"] = one(
        "SELECT count(*) FROM task8.review_mapping m JOIN public.review_record r ON r.extraction_id = %s "
        "AND r.record_no = m.record_no WHERE m.dedup_stage_key = %s "
        "AND ((r.classification = 'quarantined') <> (m.mapping_status = 'quarantined') "
        "OR r.source_review_id <> m.source_review_id)", eid, dk)
    out["incomplete_status_mismatch"] = one(
        "SELECT count(*) FROM task8.review_clean c JOIN task8.review_mapping m ON m.dedup_stage_key = %s "
        "AND m.record_no = c.record_no WHERE c.clean_stage_key = %s "
        "AND (NOT c.key_complete) <> (m.mapping_status = 'incomplete')", dk, ck)
    out["mapping_status"] = {s: n for s, n in conn.execute(
        "SELECT mapping_status, count(*) FROM task8.review_mapping WHERE dedup_stage_key = %s GROUP BY 1 ORDER BY 1", (dk,))}
    out["match_level"] = {s: n for s, n in conn.execute(
        "SELECT match_level, count(*) FROM task8.review_mapping WHERE dedup_stage_key = %s AND match_level IS NOT NULL "
        "GROUP BY 1 ORDER BY 1", (dk,))}

    # 3. 逻辑评论与映射一致（R18、R19、R23）
    out["logical_reviews"] = one("SELECT count(*) FROM task8.logical_review WHERE dedup_stage_key = %s", dk)
    out["representative_mismatch"] = abs(out["match_level"].get("representative", 0) - out["logical_reviews"])
    out["copy_count_mismatch"] = abs(
        one("SELECT coalesce(sum(copy_count), 0) FROM task8.logical_review WHERE dedup_stage_key = %s", dk)
        - out["mapping_status"].get("mapped", 0))
    out["mapping_orphan_logical"] = one(
        "SELECT count(*) FROM task8.review_mapping m WHERE m.dedup_stage_key = %s AND m.logical_review_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM task8.logical_review l WHERE l.dedup_stage_key = %s "
        "AND l.logical_review_id = m.logical_review_id)", dk, dk)
    out["representative_wrong"] = one(
        "SELECT count(*) FROM task8.logical_review l LEFT JOIN task8.review_mapping m ON m.dedup_stage_key = %s "
        "AND m.record_no = l.representative_record_no WHERE l.dedup_stage_key = %s "
        "AND (m.match_level IS DISTINCT FROM 'representative' OR m.logical_review_id IS DISTINCT FROM l.logical_review_id)",
        dk, dk)
    key_complete = one("SELECT count(*) FROM task8.review_clean WHERE clean_stage_key = %s AND key_complete", ck)
    out["key_complete_records"] = key_complete
    out["duplicates"] = sum(v for k, v in out["match_level"].items() if k != "representative")
    out["duplicates_mismatch"] = abs(out["duplicates"] - (key_complete - out["logical_reviews"]))
    out["candidate_orphan"] = one(
        "SELECT count(*) FROM task8.dup_candidate c WHERE c.stage_key IN (%s, %s) AND EXISTS ("
        "SELECT 1 FROM unnest(c.logical_review_ids) x WHERE NOT EXISTS (SELECT 1 FROM task8.logical_review l "
        "WHERE l.dedup_stage_key = %s AND l.logical_review_id = x))", dk, ak, dk)

    # 4. 作品归属只来自 cv 的电影单品（R27、R28）
    out["work_rows"] = one("SELECT count(*) FROM task8.logical_review_work WHERE attribution_stage_key = %s", ak)
    out["work_invalid_source"] = one(
        "SELECT count(*) FROM task8.logical_review_work w WHERE w.attribution_stage_key = %s AND EXISTS ("
        "SELECT 1 FROM unnest(w.via_asins) a WHERE NOT EXISTS (SELECT 1 FROM task7.edition_resolved e "
        "WHERE e.conflict_version_id = %s AND e.asin = a AND e.product_kind = 'movie' AND e.movie_ids[1] = w.movie_id))",
        ak, cv)
    out["work_orphan_logical"] = one(
        "SELECT count(*) FROM task8.logical_review_work w WHERE w.attribution_stage_key = %s AND NOT EXISTS ("
        "SELECT 1 FROM task8.logical_review l WHERE l.dedup_stage_key = %s AND l.logical_review_id = w.logical_review_id)",
        ak, dk)
    out["work_missing"] = one(
        "SELECT count(*) FROM (SELECT DISTINCT m.logical_review_id, e.movie_ids[1] AS movie_id "
        "FROM task7.edition_resolved e JOIN task8.review_mapping m ON m.dedup_stage_key = %s AND m.asin = e.asin "
        "AND m.mapping_status = 'mapped' WHERE e.conflict_version_id = %s AND e.product_kind = 'movie' "
        "AND cardinality(e.movie_ids) >= 1) x WHERE NOT EXISTS (SELECT 1 FROM task8.logical_review_work w "
        "WHERE w.attribution_stage_key = %s AND w.logical_review_id = x.logical_review_id AND w.movie_id = x.movie_id)",
        dk, cv, ak)

    # 5. 统计组（R29）
    movies = len(sources.cv_movies(conn, cv))
    out["movies_expected"] = movies
    out["group_members"] = one("SELECT count(*) FROM task8.stat_group_member WHERE attribution_stage_key = %s", ak)
    out["work_movie_without_group"] = one(
        "SELECT count(DISTINCT w.movie_id) FROM task8.logical_review_work w WHERE w.attribution_stage_key = %s "
        "AND NOT EXISTS (SELECT 1 FROM task8.stat_group_member g WHERE g.attribution_stage_key = %s "
        "AND g.movie_id = w.movie_id)", ak, ak)
    out["logical_split_across_groups"] = one(
        "SELECT count(*) FROM (SELECT w.logical_review_id FROM task8.logical_review_work w JOIN task8.stat_group_member g "
        "ON g.attribution_stage_key = w.attribution_stage_key AND g.movie_id = w.movie_id WHERE w.attribution_stage_key = %s "
        "GROUP BY 1 HAVING count(DISTINCT g.stat_group_id) > 1) x", ak)
    out["group_size_mismatch"] = one(
        "SELECT count(*) FROM task8.stat_group s LEFT JOIN (SELECT stat_group_id, count(*) n FROM task8.stat_group_member "
        "WHERE attribution_stage_key = %s GROUP BY 1) m USING (stat_group_id) WHERE s.attribution_stage_key = %s "
        "AND s.movie_count IS DISTINCT FROM m.n", ak, ak)

    # 6. 统计表与归属一致（R26、R30）
    attributed = one("SELECT count(DISTINCT logical_review_id) FROM task8.logical_review_work "
                     "WHERE attribution_stage_key = %s", ak)
    out["attributed_logical_reviews"] = attributed
    out["movie_stats_rows"] = one("SELECT count(*) FROM task8.movie_review_stats WHERE review_dedup_version_id = %s", rd)
    out["movie_stats_sum_mismatch"] = abs(one(
        "SELECT coalesce(sum(logical_reviews), 0) FROM task8.movie_review_stats WHERE review_dedup_version_id = %s", rd)
        - out["work_rows"])
    out["group_stats_sum_mismatch"] = abs(one(
        "SELECT coalesce(sum(logical_reviews), 0) FROM task8.stat_group_review_stats WHERE review_dedup_version_id = %s",
        rd) - attributed)
    out["product_stats_rows"] = one("SELECT count(*) FROM task8.product_review_stats WHERE review_dedup_version_id = %s",
                                    rd)
    out["product_stats_rows_expected"] = one(
        "SELECT count(DISTINCT asin) FROM task8.review_mapping WHERE dedup_stage_key = %s AND asin IS NOT NULL", dk)
    valid = out["mapping_status"].get("mapped", 0) + out["mapping_status"].get("incomplete", 0)
    out["product_valid_mismatch"] = abs(one(
        "SELECT coalesce(sum(raw_valid), 0) FROM task8.product_review_stats WHERE review_dedup_version_id = %s", rd) - valid)
    out["product_category_counts"] = {c: n for c, n in conn.execute(
        "SELECT product_category, count(*) FROM task8.product_review_stats WHERE review_dedup_version_id = %s "
        "GROUP BY 1 ORDER BY 1", (rd,))}

    # 7. 上游表行数快照（任务 1–7 全部只读）
    out["table_counts"] = {f"{sch}.{tbl}": one(f"SELECT count(*) FROM {sch}.{tbl}") for sch, tbl in UPSTREAM_TABLES}
    return out


PROBLEM_LABELS = {
    "stage_chain_bad": "版本登记的阶段键未登记或依赖链不符", "stages_incomplete": "版本的阶段没有完成标记",
    "stage_key_mismatch": "版本登记的阶段键与输入重算不一致", "run_version_mismatch": "运行已成功或复用但版本未核实",
    "records_unmapped": "原始记录没有映射行", "mapping_extra": "多余的映射行",
    "quarantine_status_mismatch": "映射的隔离状态或 source_review_id 与任务 1 不一致",
    "incomplete_status_mismatch": "映射的不完整状态与清洗键不一致",
    "representative_mismatch": "代表记录数 ≠ 逻辑评论数", "copy_count_mismatch": "逻辑评论副本数之和 ≠ mapped 映射数",
    "mapping_orphan_logical": "映射引用不存在的逻辑评论", "representative_wrong": "逻辑评论的代表记录映射不符",
    "duplicates_mismatch": "重复评论数 ≠ 键完整记录数 − 逻辑评论数", "candidate_orphan": "候选引用不存在的逻辑评论",
    "work_invalid_source": "作品归属来自非电影单品或与 movie_ids[1] 不符", "work_orphan_logical": "作品归属引用不存在的逻辑评论",
    "work_missing": "电影单品上的逻辑评论缺少作品归属", "work_movie_without_group": "有归属的作品不在任何统计组",
    "logical_split_across_groups": "一条逻辑评论跨多个统计组", "group_size_mismatch": "统计组作品数与成员不符",
    "movie_stats_sum_mismatch": "作品统计的逻辑评论数之和 ≠ 归属行数",
    "group_stats_sum_mismatch": "统计组的逻辑评论数之和 ≠ 有归属的逻辑评论数",
    "product_valid_mismatch": "商品统计的原始有效数之和 ≠ 有效记录数",
}


def problems(info: dict[str, Any], *, expect_verified: bool = True) -> list[str]:
    """根据 verify 的结果列出所有违规项，空列表即通过。"""
    out = [f"{label}：{info[key]}" for key, label in PROBLEM_LABELS.items() if info.get(key, 0) > 0]
    if info.get("mapping_total") != info.get("records_expected"):
        out.append(f"映射行数 {info.get('mapping_total')} ≠ 原始记录数 {info.get('records_expected')}")
    if info.get("group_members") != info.get("movies_expected"):
        out.append(f"统计组成员 {info.get('group_members')} ≠ 作品数 {info.get('movies_expected')}")
    if info.get("movie_stats_rows") != info.get("movies_expected"):
        out.append(f"作品统计行 {info.get('movie_stats_rows')} ≠ 作品数 {info.get('movies_expected')}")
    if info.get("product_stats_rows") != info.get("product_stats_rows_expected"):
        out.append(f"商品统计行 {info.get('product_stats_rows')} ≠ 映射中的 ASIN 数 {info.get('product_stats_rows_expected')}")
    if expect_verified and info.get("version_status") != "verified":
        out.append(f"版本状态为 {info.get('version_status')}，不是 verified")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 8 只读核对")
    parser.add_argument("--version", help="去重版本号（如 rd0001，默认取最近一次）")
    parser.add_argument("--extraction-id", help="抽取 ID")
    args = parser.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        rd = args.version or latest_version(conn, eid)
        info = verify(conn, rd)
        p = problems(info)
        info["problems"] = p
        info["passed"] = len(p) == 0
        print(json.dumps(info, ensure_ascii=False, indent=2, default=str))
        return 0 if info["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
