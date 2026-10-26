"""集成测试辅助。"""

from __future__ import annotations

import psycopg


def dump_state(conn: psycopg.Connection, eid: str) -> dict:
    """可比较的有序结果快照（不含运行时间与批次归属）。"""
    q = lambda sql: conn.execute(sql, (eid,)).fetchall()
    return {
        "records": q("SELECT record_no, source_review_id, start_offset, end_offset, raw_sha256, classification, "
                     "product_id_observed, product_id_status, reviewer_id_observed, reviewer_id_status "
                     "FROM review_record WHERE extraction_id = %s ORDER BY record_no"),
        "staging": q("SELECT s.* FROM review_staging s WHERE extraction_id = %s ORDER BY source_review_id"),
        "quarantine": q("SELECT source_review_id, reasons, raw_bytes, spill_path FROM quarantine_record "
                        "WHERE extraction_id = %s ORDER BY source_review_id"),
        "issues": q("SELECT issue_key, source_review_id, field, code, severity, evidence, details "
                    "FROM quality_issue WHERE extraction_id = %s ORDER BY issue_key"),
    }
