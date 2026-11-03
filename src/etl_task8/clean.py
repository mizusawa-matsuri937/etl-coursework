"""review_clean 阶段（spec R06–R09、R36）：每条有效记录一行清洗键，异常写 review_issue。

按 record_no 区间分批（默认 500,000 条），每批一条 INSERT … SELECT 在库内完成：读 review_record 连 review_staging，
判定 helpfulness 状态（rules.helpfulness_status_sql）、算内容键哈希（rules.content_hash_sql）与分区桶号；
正文与摘要只存 MD5，不进 Python 内存。批结果、review_issue、entity_stage 与 run_batch 同事务提交，
中断后从最后一个已提交区间之后继续。隔离记录不进入本表（R05），在 review_dedup 直接写 quarantined 映射。
"""

from __future__ import annotations

from typing import Any

import psycopg

from .rules import HELPFULNESS_ANOMALIES, bucket_sql, content_hash_sql, helpfulness_status_sql
from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, done_units, mark_complete, mark_unit, next_batch_no,
                  register_stage_key, stage_complete)

STAGE = "review_clean"

_COMPLETE = ("s.reviewer_id IS NOT NULL AND s.review_time_utc IS NOT NULL AND s.score IS NOT NULL "
             "AND s.review_text_raw IS NOT NULL")

_INSERT = f"""
INSERT INTO task8.review_clean (clean_stage_key, record_no, source_review_id, batch_no, asin, reviewer_id, review_day,
    score, helpfulness_raw, helpfulness_status, helpful_votes, total_votes, profile_name_raw, summary_md5, text_md5,
    content_hash, key_complete, asin_bucket, reviewer_bucket)
SELECT %(key)s, r.record_no, r.source_review_id, %(batch)s, s.product_id, s.reviewer_id,
       (s.review_time_utc AT TIME ZONE 'UTC')::date, s.score, s.helpfulness_raw,
       {helpfulness_status_sql('s.helpfulness_raw')}, s.helpful_votes, s.total_votes, s.profile_name_raw,
       decode(md5(s.summary_raw), 'hex'), decode(md5(s.review_text_raw), 'hex'),
       CASE WHEN {_COMPLETE} THEN {content_hash_sql('s.reviewer_id', 's.review_time_utc', 's.score', 's.summary_raw',
                                                    's.review_text_raw')} END,
       {_COMPLETE}, {bucket_sql('s.product_id')}, {bucket_sql('s.reviewer_id')}
FROM public.review_record r
JOIN public.review_staging s ON s.extraction_id = r.extraction_id AND s.source_review_id = r.source_review_id
WHERE r.extraction_id = %(eid)s AND r.classification = 'valid' AND r.record_no BETWEEN %(lo)s AND %(hi)s
"""

_CODE = "CASE c.helpfulness_status " + " ".join(f"WHEN '{s}' THEN '{c}'" for s, c in HELPFULNESS_ANOMALIES.items()) + " END"

_ISSUES = f"""
INSERT INTO task8.review_issue (stage_key, issue_key, record_no, source_review_id, code, evidence, detail, rule_version)
SELECT %(key)s, x.code || ':' || x.record_no, x.record_no, x.source_review_id, x.code, x.evidence, x.detail, %(rv)s
FROM (
    SELECT c.record_no, c.source_review_id, {_CODE} AS code, c.helpfulness_raw AS evidence, '{{}}'::jsonb AS detail
    FROM task8.review_clean c
    WHERE c.clean_stage_key = %(key)s AND c.record_no BETWEEN %(lo)s AND %(hi)s
      AND c.helpfulness_status NOT IN ('ok', 'zero_zero')
    UNION ALL
    SELECT c.record_no, c.source_review_id, 'incomplete_review', NULL,
           jsonb_build_object('missing', to_jsonb(array_remove(ARRAY[
               CASE WHEN c.reviewer_id IS NULL THEN 'reviewer_id' END,
               CASE WHEN c.review_day IS NULL THEN 'review_time' END,
               CASE WHEN c.score IS NULL THEN 'score' END,
               CASE WHEN c.text_md5 IS NULL THEN 'review_text' END], NULL)))
    FROM task8.review_clean c
    WHERE c.clean_stage_key = %(key)s AND c.record_no BETWEEN %(lo)s AND %(hi)s AND NOT c.key_complete
) x
"""


def _max_record_no(conn: psycopg.Connection, extraction_id: str) -> int:
    return conn.execute("SELECT coalesce(max(last_record_no), 0) FROM public.etl_batch WHERE extraction_id = %s",
                        (extraction_id,)).fetchone()[0]


def _resume_from(done: set[str]) -> int:
    """已提交区间 r<首>-<末> 的最大末号之后继续（批大小改变也能接上）。"""
    ends = [int(u.split("-")[1]) for u in done if u.startswith("r") and "-" in u]
    return max(ends) + 1 if ends else 1


def clean_reviews(conn: psycopg.Connection, ctx: RunContext, *, batch_size: int = DEFAULT_BATCH,
                  before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """R06–R09、R36：按记录号区间分批写 review_clean 与 review_issue；返回本次写入的计数。"""
    if batch_size < 1:
        raise ValueError("batch_size 必须为正")
    key = register_stage_key(conn, ctx, STAGE)
    counts: dict[str, Any] = {"stage_key": key, "batches": 0, "rows": 0, "issues": 0, "incomplete": 0,
                              "helpfulness_status": {}}
    if stage_complete(conn, key):
        counts["skipped"] = "complete"
        return counts
    eid = ctx.inputs.extraction_id
    last = _max_record_no(conn, eid)
    start = _resume_from(done_units(conn, key))
    batch_no = next_batch_no(conn, ctx, STAGE)
    for lo in range(start, last + 1, batch_size):
        hi = min(lo + batch_size - 1, last)
        params = {"key": key, "batch": batch_no, "eid": eid, "lo": lo, "hi": hi, "rv": ctx.rules.version}
        with conn.transaction():
            rows = conn.execute(_INSERT, params).rowcount
            issues = conn.execute(_ISSUES, params).rowcount
            for status, n in conn.execute(
                    "SELECT helpfulness_status, count(*) FROM task8.review_clean WHERE clean_stage_key = %s "
                    "AND record_no BETWEEN %s AND %s GROUP BY 1", (key, lo, hi)):
                counts["helpfulness_status"][status] = counts["helpfulness_status"].get(status, 0) + n
            counts["incomplete"] += conn.execute(
                "SELECT count(*) FROM task8.review_clean WHERE clean_stage_key = %s AND record_no BETWEEN %s AND %s "
                "AND NOT key_complete", (key, lo, hi)).fetchone()[0]
            mark_unit(conn, ctx, STAGE, key, batch_no, f"r{lo}-{hi}", rows, complete=hi == last)
            if before_commit:
                before_commit(STAGE, batch_no)
        counts["batches"] += 1
        counts["rows"] += rows
        counts["issues"] += issues
        batch_no += 1
    if not stage_complete(conn, key):      # 没有新区间（全部已提交，或抽取为空）：补写完成标记
        mark_complete(conn, ctx, STAGE, key, batch_no)
    return counts
