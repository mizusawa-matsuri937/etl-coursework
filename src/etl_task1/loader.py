"""分批装载：每批数据与检查点在同一事务提交，崩溃后以数据库中的已提交批次为准恢复。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import errors
from psycopg.types.json import Jsonb

from .checkpoint import (
    Checkpoint,
    CheckpointMismatch,
    ExtractionBusy,
    LoadConflict,
    RunContext,
    finish_run,
    load_checkpoint,
    plan_extraction_id,
    register_extraction,
    register_source,
    start_run,
    try_lock,
    unlock,
)
from .config import Settings
from .identity import RULES_VERSION
from .db import run_integrity
from .evidence import persist_spill
from .parser import ScanStatus, scan_records
from .records import QUARANTINED, VALID, ParsedRecord, RawBlock
from .source import SourceVerification, open_gzip

log = logging.getLogger(__name__)

RECORD_COLS = ("extraction_id", "source_review_id", "record_no", "start_line", "end_line",
               "start_offset", "end_offset", "byte_size", "raw_sha256", "classification",
               "product_id_observed", "product_id_status", "reviewer_id_observed",
               "reviewer_id_status", "batch_no")
STAGING_COLS = ("extraction_id", "source_review_id", "product_id_raw", "product_id",
                "reviewer_id_raw", "reviewer_id", "profile_name_raw", "helpfulness_raw",
                "helpful_votes", "total_votes", "helpfulness_ratio", "score_raw", "score",
                "review_time_raw", "review_time_unix", "review_time_utc", "summary_raw",
                "review_text_raw", "field_present", "field_start_lines", "rules_version")
QUARANTINE_COLS = ("extraction_id", "source_review_id", "reasons", "raw_bytes", "spill_path",
                   "spill_sha256", "spill_size", "rules_version")
ISSUE_COLS = ("extraction_id", "issue_key", "source_review_id", "field", "code", "severity",
              "evidence", "details", "rule_version")


class InjectedFault(Exception):
    """测试用故障注入，仅 ETL_ENV=test 时可由配置触发。"""


@dataclass
class BatchRows:
    batch_no: int
    first_record_no: int
    last_record_no: int
    resume_offset: int
    resume_line: int
    valid_count: int
    quarantined_count: int
    byte_count: int
    batch_sha256: str
    records: list[tuple]
    staging: list[tuple]
    quarantine: list[tuple]
    issues: list[tuple]


@dataclass(frozen=True)
class LoadResult:
    extraction_id: str
    run_id: str
    outcome: str  # loaded | reused
    batches_committed: int
    records_valid: int
    records_quarantined: int
    scope_complete: bool
    source_eof_reached: bool
    gzip_verified: bool


def batch_digest(records: list[ParsedRecord]) -> str:
    h = hashlib.sha256()
    for r in records:
        h.update(f"{r.block.record_no}:{r.source_review_id}:{r.block.sha256}:{r.status}\n".encode())
    return h.hexdigest()


def build_batch(extraction_id: str, batch_no: int, records: list[ParsedRecord],
                spill_refs: dict[int, tuple[str, str, int]]) -> BatchRows:
    """把一批解析结果转成各表的 COPY 行。spill_refs: record_no -> (相对路径, sha256, size)。"""
    rec_rows, stg_rows, q_rows, issue_rows = [], [], [], []
    for r in records:
        b = r.block
        rec_rows.append((extraction_id, r.source_review_id, b.record_no, b.start_line, b.end_line,
                         b.start_offset, b.end_offset, b.size, b.sha256, r.status,
                         r.observed_product_id, r.product_id_status,
                         r.observed_reviewer_id, r.reviewer_id_status, batch_no))
        if r.status == VALID:
            n = r.normalized
            stg_rows.append((
                extraction_id, r.source_review_id, r.raw["product_id"], n.product_id,
                r.raw["reviewer_id"], n.reviewer_id, r.raw["profile_name"], r.raw["helpfulness"],
                n.helpful_votes, n.total_votes, n.helpfulness_ratio, r.raw["score"], n.score,
                r.raw["review_time"], n.review_time_unix, n.review_time_utc, r.raw["summary"],
                r.raw["review_text"], Jsonb(r.present), Jsonb(r.start_lines),
                RULES_VERSION))
        else:
            ref = spill_refs.get(b.record_no)
            q_rows.append((extraction_id, r.source_review_id, list(r.quarantine_reasons),
                           None if ref else b.data, ref[0] if ref else None,
                           ref[1] if ref else None, ref[2] if ref else None, RULES_VERSION))
        seen: dict[tuple, int] = {}
        for issue in r.issues:
            slot = (issue.field, issue.code)
            ordinal = seen.get(slot, 0)
            seen[slot] = ordinal + 1
            issue_rows.append((extraction_id, issue.key(r.source_review_id, ordinal),
                               r.source_review_id, issue.field, issue.code, issue.severity,
                               issue.evidence, Jsonb(issue.details), issue.rule_version))
    last = records[-1].block
    return BatchRows(
        batch_no=batch_no, first_record_no=records[0].block.record_no, last_record_no=last.record_no,
        resume_offset=last.end_offset, resume_line=last.end_line + 1,
        valid_count=sum(r.status == VALID for r in records),
        quarantined_count=sum(r.status == QUARANTINED for r in records),
        byte_count=sum(r.block.size for r in records), batch_sha256=batch_digest(records),
        records=rec_rows, staging=stg_rows, quarantine=q_rows, issues=issue_rows)


def _copy(conn: psycopg.Connection, table: str, cols: tuple[str, ...], rows: list[tuple]) -> None:
    if not rows:
        return
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({', '.join(cols)}) FROM STDIN") as copy:
        for row in rows:
            copy.write_row(row)


def _parse_fault(spec: str | None) -> tuple[str, int] | None:
    if not spec:
        return None
    kind, _, n = spec.partition(":")
    if kind not in ("before_commit", "after_commit") or not n.isdigit():
        raise ValueError(f"无法识别的故障注入：{spec!r}")
    return kind, int(n)


def commit_batch(conn: psycopg.Connection, run_id: str, extraction_id: str, rows: BatchRows,
                 fault: tuple[str, int] | None) -> bool:
    """在一个事务内写批次检查点与全部明细。返回 False 表示该批已存在且内容一致（跳过）。"""
    try:
        with conn.transaction():
            existing = conn.execute(
                "SELECT batch_sha256, first_record_no, last_record_no FROM etl_batch "
                "WHERE extraction_id = %s AND batch_no = %s", (extraction_id, rows.batch_no)).fetchone()
            if existing is not None:
                if existing == (rows.batch_sha256, rows.first_record_no, rows.last_record_no):
                    return False
                raise LoadConflict(f"批次 {rows.batch_no} 已提交但内容与待写入内容不同")
            conn.execute(
                """INSERT INTO etl_batch (extraction_id, batch_no, first_record_no, last_record_no,
                       resume_offset, resume_line, valid_count, quarantined_count, byte_count,
                       batch_sha256, run_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (extraction_id, rows.batch_no, rows.first_record_no, rows.last_record_no,
                 rows.resume_offset, rows.resume_line, rows.valid_count, rows.quarantined_count,
                 rows.byte_count, rows.batch_sha256, run_id))
            _copy(conn, "review_record", RECORD_COLS, rows.records)
            _copy(conn, "review_staging", STAGING_COLS, rows.staging)
            _copy(conn, "quarantine_record", QUARANTINE_COLS, rows.quarantine)
            _copy(conn, "quality_issue", ISSUE_COLS, rows.issues)
            if fault == ("before_commit", rows.batch_no):
                raise InjectedFault(f"批次 {rows.batch_no} 提交前注入故障")
    except errors.UniqueViolation as exc:
        raise LoadConflict(f"批次 {rows.batch_no} 与已有记录冲突：{exc.diag.message_detail}") from exc
    if fault == ("after_commit", rows.batch_no):
        raise InjectedFault(f"批次 {rows.batch_no} 提交后注入故障（客户端未收到确认）")
    return True


def _total_counts(conn: psycopg.Connection, extraction_id: str) -> tuple[int, int]:
    row = conn.execute(
        "SELECT coalesce(sum(valid_count), 0), coalesce(sum(quarantined_count), 0) "
        "FROM etl_batch WHERE extraction_id = %s", (extraction_id,)).fetchone()
    return int(row[0]), int(row[1])


def _set_extraction(conn: psycopg.Connection, extraction_id: str, **cols) -> None:
    sets = ", ".join(f"{k} = %s" for k in cols)
    done = ", completed_at = now()" if cols.get("status") == "complete" else ""
    with conn.transaction():
        conn.execute(f"UPDATE extraction SET {sets}{done} WHERE extraction_id = %s",
                     (*cols.values(), extraction_id))


def _clean_stale_spills(evidence_dir: Path) -> None:
    for p in evidence_dir.glob("oversize-*.bin"):  # 崩溃遗留的未引用临时文件
        p.unlink(missing_ok=True)


def load_extraction(settings: Settings, verification: SourceVerification, conn: psycopg.Connection,
                    ctx: RunContext = RunContext(), *, expected_extraction_id: str | None = None,
                    ) -> LoadResult:
    """装载（或恢复、复用）一次抽取。表结构须已应用。"""
    if not conn.autocommit:
        raise ValueError("装载连接必须是自动提交模式（使用 etl_task1.db.connect）")
    eid = plan_extraction_id(settings, verification)
    if expected_extraction_id is not None and expected_extraction_id != eid:
        raise CheckpointMismatch("输入或配置已变化，extraction_id 与待恢复的不同；不得沿用旧检查点")
    fault = _parse_fault(settings.fault_injection)
    register_source(conn, verification)
    state = register_extraction(conn, settings, verification)
    if not try_lock(conn, eid):
        raise ExtractionBusy(f"抽取 {eid} 正由另一个装载器处理")
    run_id = start_run(conn, eid, ctx)
    try:
        cp = load_checkpoint(conn, eid)
        if state.status == "complete":
            bad = {k: v for k, v in run_integrity(conn, eid).items() if v}
            if bad:
                raise LoadConflict(f"已完成的抽取未通过完整性核对：{bad}")
            finish_run(conn, run_id, "reused")
            valid, quarantined = _total_counts(conn, eid)
            return LoadResult(eid, run_id, "reused", 0, valid, quarantined,
                              state.scope_complete, state.source_eof_reached, state.gzip_verified)
        _set_extraction(conn, eid, status="loading", failure_reason=None)
        status = _scan_and_load(settings, verification, conn, run_id, eid, cp, fault)
        _set_extraction(conn, eid, scope_complete=status.scope_complete,
                        source_eof_reached=status.source_eof_reached,
                        gzip_verified=status.gzip_verified, status="complete")
        finish_run(conn, run_id, "succeeded")
    except BaseException as exc:
        _record_failure(conn, eid, run_id, exc)
        raise
    finally:
        try:
            unlock(conn, eid)
        except psycopg.Error:
            pass
    valid, quarantined = _total_counts(conn, eid)
    batches = conn.execute("SELECT count(*) FROM etl_batch WHERE run_id = %s", (run_id,)).fetchone()[0]
    return LoadResult(eid, run_id, "loaded", int(batches), valid, quarantined,
                      status.scope_complete, status.source_eof_reached, status.gzip_verified)


def _record_failure(conn: psycopg.Connection, eid: str, run_id: str, exc: BaseException) -> None:
    reason = f"{type(exc).__name__}: {exc}"[:2000]
    try:
        _set_extraction(conn, eid, status="failed", failure_reason=reason)
        finish_run(conn, run_id, "failed", reason)
    except psycopg.Error:
        log.exception("无法登记失败状态")  # 数据库不可用时由调用方的日志/报告兜底


def _scan_and_load(settings: Settings, v: SourceVerification, conn: psycopg.Connection,
                   run_id: str, eid: str, cp: Checkpoint, fault: tuple[str, int] | None) -> ScanStatus:
    status = ScanStatus(records_read=cp.last_record_no)
    sample_max = settings.sample_max_records
    if sample_max is not None and cp.last_record_no >= sample_max:
        status.scope_complete = True  # 样本范围已全部提交，无需再读文件
        return status

    evidence_dir = settings.output_dir / "evidence" / eid
    evidence_dir.mkdir(parents=True, exist_ok=True)
    _clean_stale_spills(evidence_dir)

    def verify_checkpoint_block(block: RawBlock) -> None:
        row = conn.execute("SELECT raw_sha256, end_offset FROM review_record "
                           "WHERE extraction_id = %s AND record_no = %s", (eid, block.record_no)).fetchone()
        if row is None or row != (block.sha256, block.end_offset):
            raise CheckpointMismatch(f"检查点记录 {block.record_no} 与源文件不一致，不能恢复")

    pending: list[ParsedRecord] = []
    pending_bytes = 0
    batch_no = cp.last_batch_no
    first_resumed = cp.last_record_no > 0

    def flush() -> None:
        nonlocal pending, pending_bytes, batch_no
        if not pending:
            return
        batch_no += 1
        refs: dict[int, tuple[str, str, int]] = {}
        for r in pending:
            if r.block.oversize:
                name = f"{r.source_review_id}.bin"
                persist_spill(r.block, evidence_dir, name)  # 先完整落盘，再由事务引用
                refs[r.block.record_no] = (f"evidence/{eid}/{name}", r.block.sha256, r.block.size)
        rows = build_batch(eid, batch_no, pending, refs)
        commit_batch(conn, run_id, eid, rows, fault)
        log.info("批次 %s 已提交：记录 %s–%s，有效 %s，隔离 %s", rows.batch_no, rows.first_record_no,
                 rows.last_record_no, rows.valid_count, rows.quarantined_count)
        pending, pending_bytes = [], 0

    with open_gzip(v.path) as fh:
        for rec in scan_records(fh, v.source_file_id, status, max_records=sample_max,
                                start_after=cp.last_record_no, on_skip=verify_checkpoint_block,
                                max_record_bytes=settings.max_record_bytes, spool_dir=evidence_dir):
            if first_resumed:
                first_resumed = False
                if rec.block.start_offset < cp.resume_offset or rec.block.start_line < cp.resume_line:
                    raise CheckpointMismatch("恢复位置早于检查点记录的位置，不能恢复")
            pending.append(rec)
            pending_bytes += rec.block.size
            if len(pending) >= settings.batch_rows or pending_bytes >= settings.batch_bytes:
                flush()
        flush()
    if status.records_read < cp.last_record_no:
        raise CheckpointMismatch("源文件的记录数少于检查点，不能恢复")
    return status
