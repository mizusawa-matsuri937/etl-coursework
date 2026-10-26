"""数量核对：从明细汇总、检查守恒不变量，并与官方基准比较（spec R06、R22–R24）。"""

from __future__ import annotations

from dataclasses import dataclass, field

import psycopg

from .db import analyze_tables, run_integrity, sql_dir

# SNAP web-Movies 页面公布的规模；仅作全量对照基准，样本不得与之判等。
OFFICIAL_BASELINE = {
    "records_read": 7_911_684,
    "distinct_products_observed": 253_059,
    "distinct_users_observed": 889_176,
}
BASELINE_SOURCE = "https://snap.stanford.edu/data/web-Movies.html"

MATCHED = "matched"
REVIEW_REQUIRED = "review_required"
EXPLAINED = "explained"
NOT_APPLICABLE = "not_applicable"


class InvariantViolation(Exception):
    """数量守恒或完整性不变量被破坏；确定性错误，不应重试。"""

    def __init__(self, failures: list[str]):
        super().__init__("；".join(failures))
        self.failures = failures


def collect_counts(conn: psycopg.Connection, extraction_id: str) -> dict[str, int]:
    analyze_tables(conn)
    sql = (sql_dir() / "task1_counts.sql").read_text(encoding="utf-8")
    cur = conn.execute(sql, {"eid": extraction_id})
    row = cur.fetchone()
    return {d.name: int(v) for d, v in zip(cur.description, row)}


def quality_distribution(conn: psycopg.Connection, extraction_id: str) -> list[dict[str, object]]:
    sql = (sql_dir() / "task1_quality_distribution.sql").read_text(encoding="utf-8")
    cur = conn.execute(sql, {"eid": extraction_id})
    return [dict(zip((d.name for d in cur.description), row)) for row in cur.fetchall()]


@dataclass
class Invariant:
    name: str
    ok: bool
    detail: str = ""


def check_invariants(counts: dict[str, int], integrity: dict[str, int]) -> list[Invariant]:
    c = counts
    checks = [
        Invariant("read_equals_valid_plus_quarantined",
                  c["records_read"] == c["records_valid"] + c["records_quarantined"],
                  f"{c['records_read']} = {c['records_valid']} + {c['records_quarantined']}"),
        Invariant("staging_rows_equal_valid", c["staging_rows"] == c["records_valid"],
                  f"{c['staging_rows']} vs {c['records_valid']}"),
        Invariant("quarantine_rows_equal_quarantined", c["quarantine_rows"] == c["records_quarantined"],
                  f"{c['quarantine_rows']} vs {c['records_quarantined']}"),
        Invariant("batch_sums_equal_records",
                  c["batch_valid_sum"] == c["records_valid"] and c["batch_quarantined_sum"] == c["records_quarantined"],
                  f"批次 {c['batch_valid_sum']}/{c['batch_quarantined_sum']}，明细 {c['records_valid']}/{c['records_quarantined']}"),
        Invariant("no_records_removed", c["records_removed_by_dedup"] == 0, "任务 1 不去重，删除原始记录为 0"),
    ]
    bad = {k: v for k, v in integrity.items() if v}
    checks.append(Invariant("integrity_sql_clean", not bad, "全部为 0" if not bad else str(bad)))
    return checks


def collect_and_check(conn: psycopg.Connection, extraction_id: str
                      ) -> tuple[dict[str, int], dict[str, int], list[Invariant]]:
    counts = collect_counts(conn, extraction_id)
    integrity = run_integrity(conn, extraction_id)
    return counts, integrity, check_invariants(counts, integrity)


@dataclass
class BaselineComparison:
    status: str
    rows: list[dict[str, object]] = field(default_factory=list)
    explanation: str | None = None
    note: str = ""


def compare_baseline(counts: dict[str, int], scope: str, *, explanation: str | None = None
                     ) -> BaselineComparison:
    """全量对照官方基准。评论比较读取总数；产品、用户先比 observed，valid 差异另列。

    差异未解释保持 review_required；只有提供了有依据的说明才记为 explained，且不修改实测数。
    """
    if scope != "full":
        return BaselineComparison(NOT_APPLICABLE, note="样本范围不与全量官方基准比较")
    rows = []
    for metric, official in OFFICIAL_BASELINE.items():
        measured = counts[metric]
        rows.append({"metric": metric, "official": official, "measured": measured,
                     "difference": measured - official})
    rows.append({"metric": "distinct_products_valid", "official": None,
                 "measured": counts["distinct_products_valid"],
                 "difference": counts["distinct_products_valid"] - OFFICIAL_BASELINE["distinct_products_observed"]})
    rows.append({"metric": "distinct_users_valid", "official": None,
                 "measured": counts["distinct_users_valid"],
                 "difference": counts["distinct_users_valid"] - OFFICIAL_BASELINE["distinct_users_observed"]})
    if all(r["difference"] == 0 for r in rows[:3]):
        return BaselineComparison(MATCHED, rows)
    text = (explanation or "").strip()
    if text:
        return BaselineComparison(EXPLAINED, rows, explanation=text,
                                  note="差异已附说明；实测数未被修改")
    return BaselineComparison(REVIEW_REQUIRED, rows, note="与官方基准存在差异且尚无依据，需审阅")
