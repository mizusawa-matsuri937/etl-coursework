"""review_attribution 阶段（spec R21、R26–R29、R34、R36）：作品归属、评分统计组、统计组内的近似评论候选。

只读 review_dedup 的结果与任务 7 版本（cv0003）的 edition_resolved、shared_tconst_result（限定其 link_stage_key）。
批次单元依次为：
- work：作品归属（R27、R28）。逻辑评论任一副本在 cv 中 product_kind = 'movie' 的单品上，归属该单品的 movie_ids[1]；
  一条逻辑评论在同一作品只一行（via_asins 记全部单品）；副本也在合集上时标 also_on_collection。合集本身不产生作品归属，
  剧集、演唱会、非电影、待确认与未采集商品不在 edition_resolved 里，自然只在商品层统计。整个单元是一条 INSERT … SELECT。
- groups：评分统计组（R29）与统计组内的近似评论（R21）。作品为该 cv 版本全部作品（单品与合集的 movie_ids 并集）；
  两部作品为 likely_duplicate_movie，或至少一条逻辑评论同时归属两者时相连，连通分量为统计组，单部作品自成一组；
  组 ID 为 sg_ + sha256(组内最小 movie_id) 前 16 位。同一用户在同一统计组内、分属不同家族、正文不同的逻辑评论
  两两算词集 Jaccard，≥ 阈值记 near_duplicate（同一家族内的对已由 review_dedup 记录，这里不重复）。
  统计组、成员、候选与完成标记同一事务写入。
统计表按去重版本物化（versions.finalize_version），不在本阶段写。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import sources
from .rules import candidate_id, components, near_duplicate_pairs, stat_group_id
from .run import (BeforeCommit, RunContext, Task8InputError, copy_rows, done_units, mark_complete, mark_unit,
                  next_batch_no, register_stage_key, require_stage_complete, stage_complete)

STAGE = "review_attribution"
WORK = "work"
GROUPS = "groups"

_WORK = """
WITH coll AS MATERIALIZED (
    SELECT DISTINCT m.logical_review_id
    FROM task7.edition_resolved e
    JOIN task8.review_mapping m ON m.dedup_stage_key = %(dk)s AND m.asin = e.asin AND m.mapping_status = 'mapped'
    WHERE e.conflict_version_id = %(cv)s AND e.product_kind = 'collection')
INSERT INTO task8.logical_review_work (attribution_stage_key, logical_review_id, movie_id, via_asins, also_on_collection)
SELECT %(ak)s, m.logical_review_id, e.movie_ids[1], array_agg(DISTINCT m.asin ORDER BY m.asin),
       bool_or(c.logical_review_id IS NOT NULL)
FROM task7.edition_resolved e
JOIN task8.review_mapping m ON m.dedup_stage_key = %(dk)s AND m.asin = e.asin AND m.mapping_status = 'mapped'
LEFT JOIN coll c ON c.logical_review_id = m.logical_review_id
WHERE e.conflict_version_id = %(cv)s AND e.product_kind = 'movie' AND cardinality(e.movie_ids) >= 1
GROUP BY m.logical_review_id, e.movie_ids[1]
"""

_SHARED = """
SELECT a.movie_id, b.movie_id, count(*)
FROM task8.logical_review_work a
JOIN task8.logical_review_work b ON b.attribution_stage_key = a.attribution_stage_key
     AND b.logical_review_id = a.logical_review_id AND a.movie_id COLLATE "C" < b.movie_id
WHERE a.attribution_stage_key = %s
GROUP BY 1, 2 ORDER BY 1, 2
"""

# 同一用户在同一统计组里、跨 ≥ 2 个家族且正文不止一种的逻辑评论（只有它们可能产生跨家族的近似对）
_GROUP_SCOPE = """
WITH x AS MATERIALIZED (
    SELECT DISTINCT w.logical_review_id, g.stat_group_id, l.reviewer_id, l.family_id, l.text_md5,
           l.representative_record_no
    FROM task8.logical_review_work w
    JOIN task8.stat_group_member g ON g.attribution_stage_key = w.attribution_stage_key AND g.movie_id = w.movie_id
    JOIN task8.logical_review l ON l.dedup_stage_key = %(dk)s AND l.logical_review_id = w.logical_review_id
    WHERE w.attribution_stage_key = %(ak)s),
k AS (SELECT reviewer_id, stat_group_id FROM x GROUP BY 1, 2
      HAVING count(DISTINCT family_id) >= 2 AND count(DISTINCT text_md5) >= 2)
SELECT x.reviewer_id, x.stat_group_id, x.logical_review_id, x.family_id, x.text_md5, r.source_review_id
FROM x JOIN k USING (reviewer_id, stat_group_id)
JOIN task8.review_mapping r ON r.dedup_stage_key = %(dk)s AND r.record_no = x.representative_record_no
ORDER BY 1, 2, 3
"""


def attribute_reviews(conn: psycopg.Connection, ctx: RunContext, *, before_commit: BeforeCommit | None = None
                      ) -> dict[str, Any]:
    """R26–R29：作品归属与统计组两个单元，各一个事务；返回本次写入的计数。"""
    i = ctx.inputs
    if i.cv_link_stage_key is None:
        raise Task8InputError("review_attribution 需要任务 7 版本的 link_stage_key（先运行 check_attribution_inputs）")
    dk = require_stage_complete(conn, ctx, "review_dedup")
    ak = register_stage_key(conn, ctx, STAGE)
    counts: dict[str, Any] = {"stage_key": ak, "units": 0}
    if stage_complete(conn, ak):
        counts["skipped"] = "complete"
        return counts
    done = done_units(conn, ak)
    batch_no = next_batch_no(conn, ctx, STAGE)
    for unit in (WORK, GROUPS):
        if unit in done:
            continue
        with conn.transaction():
            part = _work(conn, ctx, dk, ak) if unit == WORK else _groups(conn, ctx, dk, ak)
            mark_unit(conn, ctx, STAGE, ak, batch_no, unit, part.pop("rows"), complete=unit == GROUPS)
            if before_commit:
                before_commit(STAGE, batch_no)
        counts.update(part)
        counts["units"] += 1
        batch_no += 1
    if not stage_complete(conn, ak):
        mark_complete(conn, ctx, STAGE, ak, batch_no)
    return counts


def _work(conn: psycopg.Connection, ctx: RunContext, dk: str, ak: str) -> dict[str, Any]:
    n = conn.execute(_WORK, {"dk": dk, "ak": ak, "cv": ctx.inputs.conflict_version_id}).rowcount
    attributed, multi, coll = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE n >= 2), count(*) FILTER (WHERE c) FROM ("
        "SELECT logical_review_id, count(*) n, bool_or(also_on_collection) c FROM task8.logical_review_work "
        "WHERE attribution_stage_key = %s GROUP BY 1) x", (ak,)).fetchone()
    return {"rows": n, "work_rows": n, "attributed_logical_reviews": attributed, "multi_movie_logical_reviews": multi,
            "also_on_collection": coll}


def _groups(conn: psycopg.Connection, ctx: RunContext, dk: str, ak: str) -> dict[str, Any]:
    i, rules = ctx.inputs, ctx.rules
    movies = sources.cv_movies(conn, i.conflict_version_id)
    edges: list[tuple[str, str, dict[str, Any]]] = []
    if "likely_duplicate_movie" in rules.stat_group_edges:
        for a, b, tconst in sources.likely_duplicate_pairs(conn, i.extraction_id, i.cv_link_stage_key):
            edges.append((a, b, {"a": a, "b": b, "type": "likely_duplicate_movie", "tconst": tconst}))
    if "shared_logical_review" in rules.stat_group_edges:
        for a, b, n in conn.execute(_SHARED, (ak,)):
            edges.append((a, b, {"a": a, "b": b, "type": "shared_logical_review", "shared": n}))
    root = components(movies, [(a, b) for a, b, _ in edges])
    members: dict[str, list[str]] = defaultdict(list)
    for m, r in root.items():
        members[r].append(m)
    gid = {r: stat_group_id(min(ms)) for r, ms in members.items()}
    basis: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a, _, e in edges:
        basis[gid[root[a]]].append(e)
    copy_rows(conn, "task8.stat_group", ("attribution_stage_key", "stat_group_id", "movie_count", "basis"),
              [(ak, gid[r], len(ms), Jsonb(sorted(basis[gid[r]], key=lambda e: (e["a"], e["b"], e["type"]))))
               for r, ms in sorted(members.items())])
    copy_rows(conn, "task8.stat_group_member", ("attribution_stage_key", "movie_id", "stat_group_id"),
              [(ak, m, gid[r]) for m, r in sorted(root.items())])
    pairs = _group_near_duplicates(conn, ctx, dk, ak)
    multi = [ms for ms in members.values() if len(ms) > 1]
    return {"rows": len(members) + len(root) + pairs, "movies": len(root), "stat_groups": len(members),
            "multi_movie_groups": len(multi), "movies_in_multi_groups": sum(len(ms) for ms in multi),
            "likely_duplicate_edges": sum(1 for *_, e in edges if e["type"] == "likely_duplicate_movie"),
            "shared_review_edges": sum(1 for *_, e in edges if e["type"] == "shared_logical_review"),
            "group_near_duplicate_pairs": pairs}


def _group_near_duplicates(conn: psycopg.Connection, ctx: RunContext, dk: str, ak: str) -> int:
    """R21：统计组内（跨家族）的近似评论；同一家族内的对由 review_dedup 记录，这里只保留两边家族不同的对。"""
    scopes: dict[tuple[str, str], list[tuple[str, str, bytes, str]]] = defaultdict(list)
    for reviewer, group, lid, fam, md5, sid in conn.execute(_GROUP_SCOPE, {"dk": dk, "ak": ak}):
        scopes[(reviewer, group)].append((lid, fam, bytes(md5), sid))
    if not scopes:
        return 0
    texts = sources.review_texts(conn, ctx.inputs.extraction_id, [s for items in scopes.values() for *_, s in items])
    rows = []
    for (reviewer, group), items in sorted(scopes.items()):
        fam_of = {lid: fam for lid, fam, _, _ in items}
        for a, b, sim in near_duplicate_pairs([(lid, md5, texts.get(sid)) for lid, _, md5, sid in items],
                                              ctx.rules.near_dup_jaccard, ctx.rules.token_pattern):
            if fam_of[a] == fam_of[b]:
                continue
            rows.append((ak, candidate_id("near_duplicate", [a, b]), STAGE, "near_duplicate", reviewer, [a, b], sim,
                         Jsonb({"scope": "stat_group", "stat_group_id": group, "family_ids": [fam_of[a], fam_of[b]]})))
    copy_rows(conn, "task8.dup_candidate", ("stage_key", "candidate_id", "stage", "candidate_type", "reviewer_id",
                                            "logical_review_ids", "similarity", "basis"), rows)
    return len(rows)
