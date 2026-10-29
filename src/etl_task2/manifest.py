"""采集清单的表结构应用、抽取前置检查与构建（spec R01–R13）。

构建在单个事务内完成：聚合任务 1 的 observed ASIN → 生成候选链接 → 与已有清单比对构建拥有的列 →
只插入缺失行。`ON CONFLICT DO NOTHING` 不更新任何列，从机制上保证不碰爬虫写入的状态。
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import psycopg

from etl_task1.db import advisory_key, sql_dir

from . import RULE_VERSION
from .urls import URL_TEMPLATE, URL_TEMPLATE_VERSION, build_candidate

log = logging.getLogger(__name__)

SCHEMA_VERSION = "101"
FETCH_ROWS = 10_000
EXTRACTION_ID = re.compile(r"[0-9a-f]{64}")

# 构建拥有、重跑时必须一致的列（first_build_id 只记录首次来源，不参与比对）。
OWNED_COLUMNS = ("original_url", "url_issue", "url_template_version", "in_valid_records",
                 "records_total", "records_valid", "records_quarantined",
                 "first_record_no", "last_record_no", "earliest_review_time", "latest_review_time")
SOURCE_COLUMNS = ("asin", "records_total", "records_valid", "records_quarantined",
                  "first_record_no", "last_record_no", "earliest_review_time", "latest_review_time")


class ManifestInputError(ValueError):
    """输入抽取不满足构建条件（确定性错误，不应重试）。"""


class ManifestConflict(RuntimeError):
    """已有清单的构建列与本次来源不一致（确定性错误，不应重试）。"""


class ManifestBusy(RuntimeError):
    """同一构建正由另一个进程执行。"""


@dataclass(frozen=True)
class BuildResult:
    build_id: str
    run_id: str
    status: str          # succeeded | reused
    scope: str
    inserted_rows: int
    total_rows: int


def apply_task2_schema(conn: psycopg.Connection) -> bool:
    """应用 task2 表结构（须先有任务 1 表结构）；已应用时不做任何事。返回是否本次新建。"""
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task2_schema'))")
        if conn.execute("SELECT to_regclass('public.schema_migration')").fetchone()[0] is None:
            raise RuntimeError("任务 1 表结构尚未应用，不能创建 task2 清单表")
        if conn.execute("SELECT 1 FROM public.schema_migration WHERE version = %s",
                        (SCHEMA_VERSION,)).fetchone():
            return False
        conn.execute((sql_dir() / "101_task2_schema.sql").read_text(encoding="utf-8"))
    return True


def build_id_for(extraction_id: str, template_version: str = URL_TEMPLATE_VERSION,
                 rule_version: str = RULE_VERSION) -> str:
    return hashlib.sha256(f"{extraction_id}|{template_version}|{rule_version}".encode()).hexdigest()


def check_extraction(conn: psycopg.Connection, extraction_id: str, *, allow_sample: bool = False) -> str:
    """R01：抽取须存在且完成；全量须读到 EOF 并通过 gzip 验证；样本须显式允许。返回 scope。"""
    if not EXTRACTION_ID.fullmatch(extraction_id or ""):
        raise ManifestInputError(f"extraction_id 须为 64 位小写十六进制：{extraction_id!r}")
    row = conn.execute(
        "SELECT scope, status, source_eof_reached, gzip_verified FROM public.extraction "
        "WHERE extraction_id = %s", (extraction_id,)).fetchone()
    if row is None:
        raise ManifestInputError(f"抽取 {extraction_id} 不存在")
    scope, status, eof, gzip_ok = row
    if status != "complete":
        raise ManifestInputError(f"抽取 {extraction_id} 状态为 {status}，未完成，不能生成清单")
    if scope == "sample" and not allow_sample:
        raise ManifestInputError(f"抽取 {extraction_id} 是样本抽取；正式清单只接受全量，测试须显式允许样本")
    if scope == "full" and not (eof and gzip_ok):
        raise ManifestInputError(f"全量抽取 {extraction_id} 未读到 EOF 或未通过 gzip 验证")
    return scope


def build_manifest(conn: psycopg.Connection, extraction_id: str, *, allow_sample: bool = False,
                   dag_id: str | None = None, airflow_run_id: str | None = None,
                   before_commit: Callable[[], None] | None = None) -> BuildResult:
    """构建（或复用）一次清单。`before_commit` 仅供测试在提交前注入故障。"""
    if not conn.autocommit:
        raise ValueError("构建连接必须是自动提交模式（使用 etl_task1.db.connect）")
    scope = check_extraction(conn, extraction_id, allow_sample=allow_sample)
    build_id = build_id_for(extraction_id)
    run_id = uuid.uuid4().hex
    with conn.transaction():
        conn.execute(
            "INSERT INTO task2.manifest_build (build_id, extraction_id, scope, url_template, "
            "url_template_version, rule_version) VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (build_id) DO NOTHING",
            (build_id, extraction_id, scope, URL_TEMPLATE, URL_TEMPLATE_VERSION, RULE_VERSION))
        conn.execute("INSERT INTO task2.manifest_run (run_id, build_id, dag_id, airflow_run_id) "
                     "VALUES (%s, %s, %s, %s)", (run_id, build_id, dag_id, airflow_run_id))
    try:
        with conn.transaction():
            if not conn.execute("SELECT pg_try_advisory_xact_lock(%s)",
                                (advisory_key(build_id),)).fetchone()[0]:
                raise ManifestBusy(f"构建 {build_id} 正由另一个进程执行")
            _close_abandoned_runs(conn, build_id, run_id)
            _stage_source(conn, extraction_id)
            _check_conflicts(conn, extraction_id)
            inserted = _insert_missing(conn, extraction_id, build_id)
            total = conn.execute("SELECT count(*) FROM task2.product_manifest WHERE extraction_id = %s",
                                 (extraction_id,)).fetchone()[0]
            if before_commit is not None:
                before_commit()
            status = "succeeded" if inserted else "reused"
            conn.execute("UPDATE task2.manifest_build SET asin_count = %s WHERE build_id = %s",
                         (total, build_id))
            conn.execute("UPDATE task2.manifest_run SET status = %s, inserted_rows = %s, finished_at = clock_timestamp() "
                         "WHERE run_id = %s", (status, inserted, run_id))
    except BaseException as exc:
        _record_failure(conn, run_id, exc)
        raise
    log.info("清单构建 %s：extraction=%s build=%s 新增 %d 行，共 %d 行",
             status, extraction_id, build_id, inserted, total)
    return BuildResult(build_id, run_id, status, scope, int(inserted), int(total))


def _close_abandoned_runs(conn: psycopg.Connection, build_id: str, run_id: str) -> None:
    """持有构建锁后，同一构建仍为 running 的其他运行必然已中断；登记为 failed。"""
    conn.execute(
        "UPDATE task2.manifest_run SET status = 'failed', finished_at = clock_timestamp(), "
        "failure_reason = '进程中断，未登记结果；由后续运行在取得构建锁后收尾' "
        "WHERE build_id = %s AND status = 'running' AND run_id <> %s", (build_id, run_id))


def _stage_source(conn: psycopg.Connection, extraction_id: str) -> None:
    """来源行经服务端游标分块读出，生成链接后 COPY 进临时表；内存只保留一块。"""
    conn.execute(
        "CREATE TEMP TABLE task2_manifest_src ("
        " asin text PRIMARY KEY, original_url text, url_issue text,"
        " records_total bigint, records_valid bigint, records_quarantined bigint,"
        " first_record_no bigint, last_record_no bigint,"
        " earliest_review_time timestamptz, latest_review_time timestamptz) ON COMMIT DROP")
    sql = (sql_dir() / "task2_manifest_source.sql").read_text(encoding="utf-8")
    copy_sql = ("COPY task2_manifest_src (asin, original_url, url_issue, records_total, records_valid, "
                "records_quarantined, first_record_no, last_record_no, earliest_review_time, "
                "latest_review_time) FROM STDIN")
    with conn.cursor(name="task2_manifest_source") as src:
        src.execute(sql, {"eid": extraction_id})
        while rows := src.fetchmany(FETCH_ROWS):
            with conn.cursor().copy(copy_sql) as copy:
                for row in rows:
                    rec = dict(zip(SOURCE_COLUMNS, row))
                    cand = build_candidate(rec["asin"])
                    copy.write_row((rec["asin"], cand.original_url, cand.url_issue,
                                    *(rec[c] for c in SOURCE_COLUMNS[1:])))


def _check_conflicts(conn: psycopg.Connection, extraction_id: str) -> None:
    """R12：已有行的构建列须与来源一致，清单也不能有来源之外的行。"""
    src_cols = ", ".join(
        "s.records_valid > 0" if c == "in_valid_records"
        else "%(tv)s::text" if c == "url_template_version"
        else f"s.{c}" for c in OWNED_COLUMNS)
    dst_cols = ", ".join(f"m.{c}" for c in OWNED_COLUMNS)
    rows = conn.execute(
        f"""SELECT m.asin, s.asin IS NULL AS extra
            FROM task2.product_manifest m
            LEFT JOIN task2_manifest_src s ON s.asin = m.asin
            WHERE m.extraction_id = %(eid)s
              AND (s.asin IS NULL OR ({src_cols}) IS DISTINCT FROM ({dst_cols}))
            ORDER BY m.asin COLLATE "C" LIMIT 5""",
        {"eid": extraction_id, "tv": URL_TEMPLATE_VERSION}).fetchall()
    if rows:
        detail = ", ".join(f"{a}（{'来源中不存在' if extra else '构建列不一致'}）" for a, extra in rows)
        raise ManifestConflict(f"已有清单与本次来源不一致，未修改任何行：{detail}")


def _insert_missing(conn: psycopg.Connection, extraction_id: str, build_id: str) -> int:
    cur = conn.execute(
        """INSERT INTO task2.product_manifest (
               extraction_id, asin, original_url, url_issue, url_template_version, first_build_id,
               in_valid_records, records_total, records_valid, records_quarantined,
               first_record_no, last_record_no, earliest_review_time, latest_review_time)
           SELECT %(eid)s, asin, original_url, url_issue, %(tv)s, %(bid)s,
                  records_valid > 0, records_total, records_valid, records_quarantined,
                  first_record_no, last_record_no, earliest_review_time, latest_review_time
           FROM task2_manifest_src
           ORDER BY asin COLLATE "C"
           ON CONFLICT (extraction_id, asin) DO NOTHING""",
        {"eid": extraction_id, "tv": URL_TEMPLATE_VERSION, "bid": build_id})
    return cur.rowcount


def _record_failure(conn: psycopg.Connection, run_id: str, exc: BaseException) -> None:
    reason = f"{type(exc).__name__}: {exc}"[:2000]
    try:
        conn.execute("UPDATE task2.manifest_run SET status = 'failed', finished_at = clock_timestamp(), "
                     "failure_reason = %s WHERE run_id = %s AND status = 'running'", (reason, run_id))
    except psycopg.Error:
        log.exception("无法登记清单构建失败")  # 数据库不可用时由调用方的日志/报告兜底
