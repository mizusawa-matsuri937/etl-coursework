"""review_family 阶段（spec R12、R13、R24、R36）：签名、分区重叠计数、Jaccard 连通分量。

1. 签名：按 ASIN 分区桶分批，每个 ASIN 取其键完整记录的去重内容键哈希，排序后求 SHA-256，写 asin_signature。
2. 重叠计数：按用户分区桶分批（内容键包含用户，同一内容键只在一个分区），对出现在 ≥ 2 个签名组的内容键枚举签名组对，
   每个分区一个事务写 signature_overlap（分区内部分计数）。
3. 连通分量：汇总各分区计数，J = shared / (n_a + n_b − shared)，J ≥ 阈值时并查集合并（rules.family_components）；
   家族 ID 由家族内最小 ASIN 生成（R24）；review_family、family_member 与完成标记在同一个事务里写入，没有半写入状态。
没有键完整记录的 ASIN 不进家族：它们的记录都是隔离或不完整，不形成逻辑评论。
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .rules import assemble_families, family_components, partition_buckets
from .run import (DEFAULT_PARTITIONS, BeforeCommit, RunContext, copy_rows, done_units, mark_unit, next_batch_no,
                  register_stage_key, require_partitions, require_stage_complete, stage_complete)

STAGE = "review_family"

_SIGNATURES = """
INSERT INTO task8.asin_signature (family_stage_key, asin, signature, review_count, partition_no)
SELECT %(fk)s, d.asin, sha256(string_agg(d.content_hash, ''::bytea ORDER BY d.content_hash)), count(*), %(p)s
FROM (SELECT DISTINCT asin, content_hash FROM task8.review_clean
      WHERE clean_stage_key = %(ck)s AND key_complete AND asin_bucket = ANY(%(b)s)) d
GROUP BY d.asin
"""

_OVERLAPS = """
INSERT INTO task8.signature_overlap (family_stage_key, partition_no, sig_a, sig_b, shared)
WITH cs AS MATERIALIZED (
    SELECT DISTINCT c.content_hash, s.signature
    FROM task8.review_clean c
    JOIN task8.asin_signature s ON s.family_stage_key = %(fk)s AND s.asin = c.asin
    WHERE c.clean_stage_key = %(ck)s AND c.key_complete AND c.reviewer_bucket = ANY(%(b)s))
SELECT %(fk)s, %(p)s, a.signature, b.signature, count(*)
FROM cs a JOIN cs b ON b.content_hash = a.content_hash AND a.signature < b.signature
GROUP BY a.signature, b.signature
"""


def build_families(conn: psycopg.Connection, ctx: RunContext, *, partitions: int = DEFAULT_PARTITIONS,
                   before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """R12、R13、R36：签名与重叠计数分区提交，连通分量单事务写入；返回本次写入的计数。"""
    ck = require_stage_complete(conn, ctx, "review_clean")
    fk = register_stage_key(conn, ctx, STAGE)
    counts: dict[str, Any] = {"stage_key": fk, "partitions": partitions, "signature_rows": 0, "overlap_rows": 0}
    if stage_complete(conn, fk):
        counts["skipped"] = "complete"
        return counts
    done = done_units(conn, fk)
    require_partitions(done, "sig", partitions)
    require_partitions(done, "ovl", partitions)
    batch_no = next_batch_no(conn, ctx, STAGE)
    for step, sql, counter in (("sig", _SIGNATURES, "signature_rows"), ("ovl", _OVERLAPS, "overlap_rows")):
        for p in range(partitions):
            unit = f"{step}{p}/{partitions}"
            if unit in done:
                continue
            with conn.transaction():
                n = conn.execute(sql, {"fk": fk, "ck": ck, "p": p, "b": partition_buckets(p, partitions)}).rowcount
                mark_unit(conn, ctx, STAGE, fk, batch_no, unit, n)
                if before_commit:
                    before_commit(STAGE, batch_no)
            counts[counter] += n
            batch_no += 1
    with conn.transaction():
        fams = _components(conn, fk, ctx)
        rows = len(fams["families"]) + fams["members"]
        mark_unit(conn, ctx, STAGE, fk, batch_no, "components", rows, complete=True)
        if before_commit:
            before_commit(STAGE, batch_no)
    counts.update({k: v for k, v in fams.items() if k != "families"})
    return counts


def _components(conn: psycopg.Connection, fk: str, ctx: RunContext) -> dict[str, Any]:
    """汇总签名与各分区重叠计数，求连通分量并写 review_family、family_member（调用方的事务内）。"""
    asin_sig: dict[str, bytes] = {}
    sizes: dict[bytes, int] = {}
    for asin, sig, n in conn.execute("SELECT asin, signature, review_count FROM task8.asin_signature "
                                     "WHERE family_stage_key = %s", (fk,)):
        sig = bytes(sig)
        asin_sig[asin] = sig
        sizes[sig] = n
    overlaps = [(bytes(a), bytes(b), int(n)) for a, b, n in conn.execute(
        "SELECT sig_a, sig_b, sum(shared) FROM task8.signature_overlap WHERE family_stage_key = %s "
        "GROUP BY sig_a, sig_b", (fk,))]
    root_of, edges = family_components(sizes, overlaps, ctx.rules.family_jaccard)
    fams = assemble_families(asin_sig, sizes, root_of, edges, ctx.rules.weak_support_max)
    copy_rows(conn, "task8.review_family",
              ("family_stage_key", "family_id", "min_asin", "asin_count", "signature_count", "max_review_count",
               "weak_support", "basis"),
              [(fk, f.family_id, f.asins[0], len(f.asins), len(f.signatures), f.max_review_count, f.weak_support,
                Jsonb([{"sig_a": e.sig_a.hex()[:12], "sig_b": e.sig_b.hex()[:12], "shared": e.shared,
                        "jaccard": round(e.jaccard, 4)} for e in f.edges])) for f in fams])
    members = [(fk, a, f.family_id, asin_sig[a], sizes[asin_sig[a]]) for f in fams for a in f.asins]
    copy_rows(conn, "task8.family_member", ("family_stage_key", "asin", "family_id", "signature", "review_count"),
              members)
    weak = [f for f in fams if f.weak_support]
    return {"families": fams, "family_count": len(fams), "members": len(members),
            "multi_asin_families": sum(1 for f in fams if len(f.asins) > 1),
            "signature_groups": len(sizes), "signature_pairs": len(overlaps), "merged_edges": len(edges),
            "weak_support_families": len(weak), "weak_support_asins": sum(len(f.asins) for f in weak),
            "max_family_asins": max((len(f.asins) for f in fams), default=0)}
