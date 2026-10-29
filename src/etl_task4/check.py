"""任务 4 只读核对（spec AC14；与 DAG 的 verify 步骤共用 verify 与 problems）。

运行（容器内，工作目录 /opt/etl）：
    python -m etl_task4.check                                  # 默认抽取的最新映射版本
    python -m etl_task4.check --mapping-version map0001
输出一段 JSON；全部一致时退出码 0，否则 1。整个核对在只读事务里进行，不写任何表。

核对内容：商品版本行数与清单一致、匹配状态之和相等；电影类商品在映射版本里各恰好一条 edition 关系；剧集与其他类别没有任何关系；
contains 只来自合集；每个自动合并的判定都满足 R22（有年份，导演或演员姓名键严格重叠，没有硬冲突）；
多商品作品内没有未处理的硬冲突；映射版本记录的数量与重新数出来的一致；评论口径之和等于清单记录总数；每个候选恰好一条本规则版本的决策。
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

from . import candidates
from .cluster import _conflict, record_scopes
from .match import ruleset
from .run import RunContext, RunInputs


def latest_mapping_version(conn: psycopg.Connection, extraction_id: str) -> str:
    row = conn.execute("SELECT mapping_version_id FROM task4.mapping_version WHERE extraction_id = %s "
                       "ORDER BY created_at DESC, mapping_version_id DESC LIMIT 1", (extraction_id,)).fetchone()
    if row is None:
        raise RuntimeError(f"抽取 {extraction_id} 还没有映射版本")
    return row[0]


def verify(conn: psycopg.Connection, mapping_version_id: str) -> dict[str, Any]:
    """只读核对一个映射版本。"""
    with conn.transaction():
        conn.execute("SET TRANSACTION READ ONLY")
        return _verify(conn, mapping_version_id)


def _verify(conn: psycopg.Connection, v: str) -> dict[str, Any]:
    m = conn.execute("SELECT extraction_id, t3_rule_version, parser_version, norm_version, rule_version, "
                     "imdb_snapshot_id, human_set_hash, counts, run_id FROM task4.mapping_version "
                     "WHERE mapping_version_id = %s", (v,)).fetchone()
    if m is None:
        raise RuntimeError(f"映射版本 {v} 不存在")
    eid, t3, pv, nv, rv, sid, human, counts, run_id = m
    out: dict[str, Any] = {"mapping_version_id": v, "extraction_id": eid, "t3_rule_version": t3, "parser_version": pv,
                           "norm_version": nv, "rule_version": rv, "imdb_snapshot_id": sid, "human_set_hash": human}
    one = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731

    # 商品版本
    out["manifest_rows"] = one("SELECT count(*) FROM task2.product_manifest WHERE extraction_id = %s", eid)
    out["product_edition_rows"] = one("SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s AND "
                                      "t3_rule_version = %s", eid, t3)
    out["match_scope"] = dict(conn.execute(
        "SELECT match_scope, count(*) FROM task4.product_edition WHERE extraction_id = %s AND t3_rule_version = %s "
        "GROUP BY 1 ORDER BY 1", (eid, t3)).fetchall())
    out["product_kind"] = dict(conn.execute(
        "SELECT product_kind, count(*) FROM task4.product_edition WHERE extraction_id = %s AND t3_rule_version = %s "
        "GROUP BY 1 ORDER BY 1", (eid, t3)).fetchall())
    out["product_edition_missing_from_manifest"] = one(
        "SELECT count(*) FROM task2.product_manifest m WHERE m.extraction_id = %s AND NOT EXISTS "
        "(SELECT 1 FROM task4.product_edition e WHERE e.extraction_id = m.extraction_id AND e.asin = m.asin "
        "AND e.t3_rule_version = %s)", eid, t3)

    # 关系
    movie_products = one("SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s AND t3_rule_version = %s "
                         "AND product_kind = 'movie' AND match_scope = 'matched'", eid, t3)
    out["movie_products"] = movie_products
    out["movie_edition_relations"] = one(
        "SELECT count(DISTINCT pm.asin) FROM task4.product_movie pm JOIN task4.product_edition e "
        "ON e.extraction_id = %s AND e.t3_rule_version = %s AND e.asin = pm.asin AND e.product_kind = 'movie' "
        "AND e.match_scope = 'matched' WHERE pm.mapping_version_id = %s AND pm.relation_type = 'edition'", eid, t3, v)
    out["movies_without_exactly_one_edition"] = one(
        "SELECT count(*) FROM task4.product_edition e WHERE e.extraction_id = %s AND e.t3_rule_version = %s "
        "AND e.product_kind = 'movie' AND e.match_scope = 'matched' AND (SELECT count(*) FROM task4.product_movie pm "
        "WHERE pm.mapping_version_id = %s AND pm.asin = e.asin AND pm.relation_type = 'edition') <> 1", eid, t3, v)
    out["edition_relations_on_other_kinds"] = one(
        "SELECT count(*) FROM task4.product_movie pm WHERE pm.mapping_version_id = %s AND pm.relation_type = 'edition' "
        "AND NOT EXISTS (SELECT 1 FROM task4.product_edition e WHERE e.extraction_id = %s AND e.t3_rule_version = %s "
        "AND e.asin = pm.asin AND e.product_kind = 'movie' AND e.match_scope = 'matched')", v, eid, t3)
    out["contains_relations_on_non_collections"] = one(
        "SELECT count(*) FROM task4.product_movie pm WHERE pm.mapping_version_id = %s AND pm.relation_type = 'contains' "
        "AND NOT EXISTS (SELECT 1 FROM task4.product_edition e WHERE e.extraction_id = %s AND e.t3_rule_version = %s "
        "AND e.asin = pm.asin AND e.product_kind = 'collection' AND e.match_scope = 'matched')", v, eid, t3)
    out["tv_series_relations"] = one(
        "SELECT count(*) FROM task4.product_movie pm JOIN task4.product_edition e ON e.extraction_id = %s AND "
        "e.t3_rule_version = %s AND e.asin = pm.asin WHERE pm.mapping_version_id = %s AND e.product_kind = 'tv_series'",
        eid, t3, v)
    out["relations_on_unmatched_products"] = one(
        "SELECT count(*) FROM task4.product_movie pm JOIN task4.product_edition e ON e.extraction_id = %s AND "
        "e.t3_rule_version = %s AND e.asin = pm.asin WHERE pm.mapping_version_id = %s AND e.match_scope <> 'matched'",
        eid, t3, v)
    out["match_status_rows"] = one("SELECT count(*) FROM task4.product_match_status WHERE mapping_version_id = %s", v)
    out["matched_products"] = one("SELECT count(*) FROM task4.product_edition WHERE extraction_id = %s AND "
                                  "t3_rule_version = %s AND match_scope = 'matched'", eid, t3)

    # 自动合并都满足 R22
    out["decisions_by_tier"] = dict(conn.execute(
        "SELECT d.tier, count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id) "
        "WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND d.rule_version = %s "
        "GROUP BY 1 ORDER BY 1", (eid, nv, sid, rv)).fetchall())
    out["auto_imdb_decisions"] = one(
        "SELECT count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id) WHERE "
        "c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND d.rule_version = %s AND "
        "c.right_kind = 'imdb' AND d.tier = 'auto'", eid, nv, sid, rv)
    out["auto_imdb_violating_r22"] = one(
        """SELECT count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id)
           WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND d.rule_version = %s
             AND c.right_kind = 'imdb' AND d.tier = 'auto'
             AND NOT (d.features->'evidence'->>'start_year' IS NOT NULL
                      AND (d.features->'director'->>'value' = 'strict'
                           OR d.features->'actor'->>'value' IN ('overlap_1', 'overlap_2plus'))
                      AND cardinality(d.veto) = 0)""", eid, nv, sid, rv)
    out["auto_pair_decisions"] = one(
        "SELECT count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id) WHERE "
        "c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND d.rule_version = %s AND "
        "c.right_kind = 'product' AND d.tier = 'auto'", eid, nv, sid, rv)
    out["auto_pair_violating_r25"] = one(
        """SELECT count(*) FROM task4.merge_decision d JOIN task4.candidate c USING (candidate_id)
           WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND d.rule_version = %s
             AND c.right_kind = 'product' AND d.tier = 'auto' AND cardinality(d.veto) = 0
             AND NOT ((d.reason = 'same_imdb_work' AND d.features->'links'->>'left' IS NOT NULL
                       AND d.features->'links'->>'left' = d.features->'links'->>'right')
                      OR (d.reason = 'page_cluster' AND d.features->'year'->>'value' = 'match'
                          AND d.features->'year'->>'left' = d.features->'year'->>'right'
                          AND d.features->'director'->>'value' = 'strict'))""", eid, nv, sid, rv)
    out["candidates_without_decision"] = one(
        "SELECT count(*) FROM task4.candidate c WHERE c.extraction_id = %s AND c.norm_version = %s AND "
        "c.imdb_snapshot_id = %s AND NOT EXISTS (SELECT 1 FROM task4.merge_decision d WHERE d.candidate_id = "
        "c.candidate_id AND d.rule_version = %s)", eid, nv, sid, rv)
    out["candidate_overflow_rows"] = one(
        "SELECT count(*) FROM task4.candidate_overflow WHERE extraction_id = %s AND norm_version = %s AND "
        "imdb_snapshot_id = %s", eid, nv, sid)

    # 簇内没有未处理的硬冲突
    ctx = RunContext(run_id, RunInputs(extraction_id=eid, imdb_snapshot_id=sid, t3_rule_version=t3, parser_version=pv,
                                       norm_version=nv, rule_version=rv, human_set_hash=human))
    rules = ruleset(rv)
    groups: dict[str, list[str]] = {}
    for mid, asin in conn.execute(
            "SELECT pm.movie_id, pm.asin FROM task4.product_movie pm JOIN task4.movie mv ON mv.mapping_version_id = "
            "pm.mapping_version_id AND mv.movie_id = pm.movie_id WHERE pm.mapping_version_id = %s AND "
            "pm.relation_type = 'edition' AND mv.edition_count > 1 ORDER BY pm.movie_id, pm.asin COLLATE \"C\"",
            (v,)).fetchall():
        groups.setdefault(mid, []).append(asin)
    kept = {frozenset((a, b)) for a, b in conn.execute(
        "SELECT asin_a, asin_b FROM task4.cluster_check WHERE mapping_version_id = %s AND action = 'kept_by_human'",
        (v,)).fetchall()}
    unresolved = 0
    asins = sorted({a for g in groups.values() for a in g})
    facts = {}
    for k in range(0, len(asins), 1000):
        facts.update(candidates.load_page_facts(conn, ctx, asins[k:k + 1000]))
    for members in groups.values():
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if _conflict(facts[a], facts[b], rules) and frozenset((a, b)) not in kept:
                    unresolved += 1
    out["multi_product_movies"] = len(groups)
    out["unresolved_cluster_conflicts"] = unresolved
    out["cluster_checks"] = dict(conn.execute(
        "SELECT action, count(*) FROM task4.cluster_check WHERE mapping_version_id = %s GROUP BY 1 ORDER BY 1",
        (v,)).fetchall())

    # 映射版本记录的数量与重新数出来的一致
    recount = {
        "movies_with_editions": one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND "
                                    "edition_count > 0", v),
        "multi_product_movies": one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND "
                                    "edition_count > 1", v),
        "review_pending_products": one("SELECT count(*) FROM task4.product_match_status WHERE mapping_version_id = %s "
                                       "AND status = 'review_pending'", v),
        "imdb_linked_movies": one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND "
                                  "status = 'imdb_linked'", v),
        "page_only_movies": one("SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s AND "
                                "status = 'page_only'", v),
        "contains_relations": one("SELECT count(*) FROM task4.product_movie WHERE mapping_version_id = %s AND "
                                  "relation_type = 'contains'", v),
    }
    out["counts_recorded"] = {k: counts.get(k) for k in recount}
    out["counts_recounted"] = recount
    out["edition_count_sum_mismatch"] = one(
        "SELECT count(*) FROM task4.movie mv WHERE mv.mapping_version_id = %s AND mv.edition_count <> "
        "(SELECT count(*) FROM task4.product_movie pm WHERE pm.mapping_version_id = mv.mapping_version_id AND "
        "pm.movie_id = mv.movie_id AND pm.relation_type = 'edition')", v)

    # 评论口径
    scopes = record_scopes(conn, v)
    out["record_scopes"] = scopes
    out["manifest_records_total"] = int(one("SELECT coalesce(sum(records_total), 0) FROM task2.product_manifest WHERE "
                                            "extraction_id = %s", eid))
    out["human_decisions"] = one("SELECT count(*) FROM task4.human_decision WHERE extraction_id = %s", eid)
    out["migrations"] = [r[0] for r in conn.execute("SELECT version FROM public.schema_migration ORDER BY version")]
    return out


def problems(out: dict[str, Any]) -> list[str]:
    found: list[str] = []
    if out["product_edition_rows"] != out["manifest_rows"]:
        found.append(f"商品版本 {out['product_edition_rows']} 行 ≠ 清单 {out['manifest_rows']} 行")
    if out["product_edition_missing_from_manifest"]:
        found.append(f"{out['product_edition_missing_from_manifest']} 个清单商品没有商品版本")
    if sum(out["match_scope"].values()) != out["product_edition_rows"]:
        found.append("匹配状态之和 ≠ 商品版本行数")
    if out["movie_edition_relations"] != out["movie_products"] or out["movies_without_exactly_one_edition"]:
        found.append(f"电影类商品 {out['movie_products']} 个，有 edition 关系 {out['movie_edition_relations']} 个，"
                     f"不是恰好一条的 {out['movies_without_exactly_one_edition']} 个")
    for k, label in (("edition_relations_on_other_kinds", "电影类以外的 edition 关系"),
                     ("contains_relations_on_non_collections", "非合集商品的 contains 关系"),
                     ("tv_series_relations", "剧集商品的关系"), ("relations_on_unmatched_products", "未参与匹配商品的关系"),
                     ("auto_imdb_violating_r22", "不满足 R22 的自动 IMDb 关联"),
                     ("auto_pair_violating_r25", "不满足 R25 的自动商品对"),
                     ("candidates_without_decision", "没有判定的候选"),
                     ("unresolved_cluster_conflicts", "未处理的簇内硬冲突"),
                     ("edition_count_sum_mismatch", "edition_count 与关系数不符的作品")):
        if out[k]:
            found.append(f"{label}：{out[k]}")
    if out["match_status_rows"] != out["matched_products"]:
        found.append(f"匹配状态 {out['match_status_rows']} 行 ≠ 参与匹配商品 {out['matched_products']} 个")
    if out["counts_recorded"] != out["counts_recounted"]:
        found.append(f"映射版本记录的数量与重数不符：{out['counts_recorded']} ≠ {out['counts_recounted']}")
    s = out["record_scopes"]
    if s["manifest_total"] != out["manifest_records_total"]:
        found.append(f"评论口径之和 {s['manifest_total']} ≠ 清单记录合计 {out['manifest_records_total']}")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 4 只读核对")
    ap.add_argument("--extraction-id")
    ap.add_argument("--mapping-version")
    args = ap.parse_args(argv)
    with connect(load_settings()) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        version = args.mapping_version or latest_mapping_version(conn, eid)
        out = verify(conn, version)
    out["problems"] = problems(out)
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 1 if out["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
