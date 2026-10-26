"""流程步骤：与 Airflow 解耦的纯 Python 函数，DAG 只负责调用与编排。

步骤之间只传小型 JSON（抽取 ID、运行 ID、统计、文件位置），不传评论正文或整批数据。
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import resource
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psycopg

from . import metrics, reporting, validation
from .checkpoint import (CheckpointMismatch, ExtractionBusy, LoadConflict, RunContext,
                         plan_extraction_id)
from .config import ConfigError, Settings, load_settings
from .db import apply_schema, connect
from .evidence import EvidenceError
from .identity import program_version
from .loader import InjectedFault, load_extraction
from .source import SourceError, SourceVerification, verify_source

DAG_ID = "amazon_reviews_task1"
STEP_NAMES = ("verify_source", "extract_load", "validate_counts", "export_results")
FINAL_STEPS = ("finalize_report", "run_gate")

SUCCEEDED = "succeeded"
FAILED = "failed"
REVIEW_REQUIRED = "review_required"


@dataclass(frozen=True)
class RunParams:
    run_key: str                       # 一次 DAG run 的稳定标识，重试期间不变
    try_number: int = 1
    dag_id: str = DAG_ID
    scope: str | None = None
    sample_max_records: int | None = None
    batch_rows: int | None = None
    source_gzip_path: str | None = None
    source_manifest_path: str | None = None
    baseline_explanation: str | None = None
    fault_injection: str | None = None

    @staticmethod
    def sanitize(run_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


class StepError(Exception):
    """步骤失败的统一包装。transient 为 True 时才值得重试。"""

    def __init__(self, step: str, cause: BaseException, transient: bool):
        super().__init__(f"{step} 失败（{type(cause).__name__}）：{cause}")
        self.step, self.cause, self.transient = step, cause, transient


def is_transient(exc: BaseException) -> bool:
    """暂时错误：连接中断、数据库重启或资源紧张、同抽取正被另一装载器占用。其余视为确定性错误。"""
    if isinstance(exc, (ExtractionBusy, psycopg.InterfaceError, ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, psycopg.OperationalError):
        state = getattr(exc, "sqlstate", None)
        return state is None or state.startswith(("08", "53", "57"))
    return False


def settings_for(params: RunParams, env: dict[str, str] | None = None) -> Settings:
    env = dict(os.environ if env is None else env)
    over = {
        "ETL_SCOPE": params.scope, "ETL_SAMPLE_MAX_RECORDS": params.sample_max_records,
        "ETL_BATCH_ROWS": params.batch_rows, "SOURCE_GZIP_PATH": params.source_gzip_path,
        "SOURCE_MANIFEST_PATH": params.source_manifest_path,
        "ETL_FAULT_INJECTION": params.fault_injection,
    }
    for key, value in over.items():
        if value not in (None, ""):
            env[key] = str(value)
    if params.scope == "full":
        env.pop("ETL_SAMPLE_MAX_RECORDS", None)
    return load_settings(env)


def record_failure(output_dir: Path, params: RunParams, step: str, exc: BaseException,
                   transient: bool) -> None:
    """失败写成文件，数据库不可用时也能留下原因。写不进去只记日志，不掩盖原异常。"""
    try:
        entry = {"task": step, "try_number": params.try_number, "error_type": type(exc).__name__,
                 "message": str(exc)[:4000], "transient": transient, "at": reporting.utc_now(),
                 "traceback": "".join(traceback.format_exception(exc))[-6000:]}
        path = reporting.failures_dir(output_dir, params.run_key) / f"{step}-try{params.try_number}.json"
        reporting.write_atomic(path, (json.dumps(entry, ensure_ascii=False, indent=2) + "\n").encode())
    except Exception:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("无法写入失败记录")


def run_step(step: str, params: RunParams, fn: Callable[[], dict[str, Any]],
             output_dir: Path | None = None) -> dict[str, Any]:
    """执行步骤；失败时记录并转成 StepError，由 DAG 决定是否重试。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        transient = is_transient(exc)
        if output_dir is None:
            try:
                output_dir = Path(os.environ.get("ETL_OUTPUT_DIR", "outputs"))
            except Exception:  # noqa: BLE001
                output_dir = Path("outputs")
        record_failure(output_dir, params, step, exc, transient)
        raise StepError(step, exc, transient) from exc


def _source_dict(v: SourceVerification) -> dict[str, Any]:
    d = dataclasses.asdict(v)
    d["path"], d["manifest_path"] = str(v.path), str(v.manifest_path)
    return d


def _source_from(d: dict[str, Any]) -> SourceVerification:
    return SourceVerification(**{**d, "path": Path(d["path"]), "manifest_path": Path(d["manifest_path"])})


def verify_source_step(params: RunParams) -> dict[str, Any]:
    s = settings_for(params)
    v = verify_source(s.source_gzip_path, s.source_manifest_path)
    return {"extraction_id": plan_extraction_id(s, v), "scope": s.scope, "source": _source_dict(v)}


def extract_load_step(params: RunParams, verified: dict[str, Any]) -> dict[str, Any]:
    s = settings_for(params)
    v = _source_from(verified["source"])
    started = time.monotonic()
    if s.fault_injection and s.fault_injection.startswith("transient_once:"):
        marker = Path(s.fault_injection.split(":", 1)[1])
        if not marker.exists():  # 模拟数据库暂时断连：第一次失败，之后成功
            marker.write_text("injected\n")
            raise psycopg.OperationalError("injected transient connection failure")
        s = dataclasses.replace(s, fault_injection=None)
    with connect(s) as conn:
        apply_schema(conn)
        ctx = RunContext(run_id=f"{params.run_key}-t{params.try_number}", dag_id=params.dag_id,
                         airflow_run_id=params.run_key)
        directory = reporting.run_dir(s.output_dir, verified["extraction_id"], params.run_key)
        monitor = metrics.ResourceMonitor(directory / "resources.jsonl", s.disk_paths(), s.min_free_bytes)
        try:
            r = load_extraction(s, v, conn, ctx, expected_extraction_id=verified["extraction_id"],
                                monitor=monitor)
        finally:
            monitor.finish(conn)
            resources = monitor.summary()
            try:  # 失败时也保留已测得的资源汇总
                reporting.write_atomic(directory / "resources.json",
                                       (json.dumps(resources, ensure_ascii=False, indent=2) + "\n").encode())
            except OSError:
                pass
    return {**dataclasses.asdict(r), "elapsed_seconds": round(time.monotonic() - started, 2),
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, "resources": resources}


def validate_counts_step(params: RunParams, extraction_id: str) -> dict[str, Any]:
    s = settings_for(params)
    with connect(s) as conn:
        ex = conn.execute("SELECT status, scope, scope_complete, source_eof_reached, gzip_verified "
                          "FROM extraction WHERE extraction_id = %s", (extraction_id,)).fetchone()
        if ex is None:
            raise validation.InvariantViolation(["抽取记录不存在"])
        counts, integrity, invariants = validation.collect_and_check(conn, extraction_id)
        quality = validation.quality_distribution(conn, extraction_id)
    failures = [f"{i.name}: {i.detail}" for i in invariants if not i.ok]
    status, scope, scope_complete, eof, gz = ex
    if status != "complete" or not scope_complete:
        failures.append(f"抽取未完成：status={status} scope_complete={scope_complete}")
    if scope == "full" and not (eof and gz):
        failures.append(f"全量必须读到 EOF 并通过 gzip 校验：eof={eof} gzip_verified={gz}")
    baseline = validation.compare_baseline(counts, scope, explanation=params.baseline_explanation)
    result = {"counts": counts, "integrity": integrity, "invariants": [dataclasses.asdict(i) for i in invariants],
              "quality": quality,
              "baseline": {"status": baseline.status, "rows": baseline.rows, "note": baseline.note,
                           "explanation": baseline.explanation, "source": validation.BASELINE_SOURCE}}
    if failures:
        raise validation.InvariantViolation(failures)
    return result


def export_results_step(params: RunParams, extraction_id: str, validated: dict[str, Any]) -> dict[str, Any]:
    s = settings_for(params)
    directory = reporting.run_dir(s.output_dir, extraction_id, params.run_key)
    files = []
    with connect(s) as conn:
        if s.scope == "full":  # 不同产品汇总来自完整 observed 数据，供任务 2 接续
            files.append(reporting.export_csv(conn, "task1_distinct_products.sql", extraction_id,
                                              directory / "distinct_products.csv"))
    q = ["code,severity,issue_rows,records"] + [
        f"{r['code']},{r['severity']},{r['issue_rows']},{r['records']}" for r in validated["quality"]]
    reporting.write_atomic(directory / "quality_issues_by_code.csv", ("\n".join(q) + "\n").encode())
    files.append(reporting.file_digest(directory / "quality_issues_by_code.csv"))
    return {"directory": str(directory), "files": files}


def _read_failures(output_dir: Path, run_key: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(reporting.failures_dir(output_dir, run_key).glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            out.append({"task": p.stem, "error_type": "unreadable_failure_file", "message": str(p)})
    return out


def finalize_report_step(params: RunParams, upstream: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """无论上游成败都运行：汇总现状并写报告。报告生成成功本身不代表数据成功。"""
    s = settings_for(params)
    verify, extract, validate, export = (upstream.get(n) for n in STEP_NAMES)
    eid = (verify or {}).get("extraction_id") or (extract or {}).get("extraction_id")
    failures = _read_failures(s.output_dir, params.run_key)
    failed_tasks = {f.get("task") for f in failures}
    # 成功优先（重试后成功的任务也会留下首次失败记录）；有失败记录为 failed；其余是上游失败而未运行
    tasks = {n: ("success" if upstream.get(n) is not None else ("failed" if n in failed_tasks else "not_run"))
             for n in STEP_NAMES}

    summary: dict[str, Any] = {
        "report_version": reporting.REPORT_VERSION, "generated_at": reporting.utc_now(),
        "dag_id": params.dag_id, "run_key": params.run_key, "extraction_id": eid,
        "scope": s.scope, "tasks": tasks, "failures": failures, "ai_assistance": reporting.AI_ASSISTANCE,
        "program": program_version(), "source": (verify or {}).get("source"),
        "extract_run": {k: extract[k] for k in ("run_id", "outcome", "elapsed_seconds", "peak_rss_kib")
                        if k in extract} if extract else None,
    }
    conn = None
    try:
        conn = connect(s, connect_timeout=5)
        summary["database_available"] = True
        if eid:
            row = conn.execute(
                "SELECT status, scope_complete, source_eof_reached, gzip_verified, failure_reason "
                "FROM extraction WHERE extraction_id = %s", (eid,)).fetchone()
            prog = conn.execute("SELECT count(*), coalesce(max(last_record_no), 0) FROM etl_batch "
                                "WHERE extraction_id = %s", (eid,)).fetchone()
            if row:
                summary["extraction"] = {"status": row[0], "scope_complete": row[1],
                                         "source_eof_reached": row[2], "gzip_verified": row[3],
                                         "failure_reason": row[4], "batches_committed": prog[0],
                                         "last_record_no": prog[1]}
        summary["resources"] = {"report_time": reporting.resources_snapshot(conn, s.output_dir)}
    except psycopg.Error as exc:
        summary["database_available"] = False
        summary["database_error"] = f"{type(exc).__name__}: {exc}"[:500]
        summary["resources"] = {"report_time": reporting.resources_snapshot(None, s.output_dir)}
    finally:
        if conn is not None:
            conn.close()

    if extract and extract.get("resources"):
        summary["resources"]["extract_load"] = extract["resources"]
    if validate:
        summary.update(counts=validate["counts"], invariants=validate["invariants"],
                       baseline=validate["baseline"], quality=validate["quality"])
    summary["exports"] = (export or {}).get("files", [])

    all_ok = all(v == "success" for v in tasks.values())
    if not all_ok:
        final = FAILED
    elif summary.get("baseline", {}).get("status") == validation.REVIEW_REQUIRED:
        final = REVIEW_REQUIRED
    else:
        final = SUCCEEDED
    summary["final_status"] = final
    summary["delivery_complete"] = final == SUCCEEDED  # 写出文件成功后才生效，见下
    directory = reporting.run_dir(s.output_dir, eid, params.run_key)
    reporting.write_atomic(directory / "manifest.json", _manifest_bytes(s))
    try:
        reporting.write_report(directory, summary)
    except OSError:
        summary["delivery_complete"] = False
        raise
    return {"final_status": final, "delivery_complete": summary["delivery_complete"],
            "report_dir": str(directory), "extraction_id": eid}


def _manifest_bytes(s: Settings) -> bytes:
    try:
        return s.source_manifest_path.read_bytes()
    except OSError:
        return b"{}\n"


def gate_decision(finalized: dict[str, Any] | None) -> tuple[bool, str]:
    """最终状态门禁：只有收尾确认整体成功且交付完整才通过。"""
    if finalized is None:
        return False, "收尾任务没有产出结果，无法确认整体状态"
    if finalized["final_status"] != SUCCEEDED:
        return False, f"整体状态为 {finalized['final_status']}（报告位置 {finalized['report_dir']}）"
    if not finalized["delivery_complete"]:
        return False, "交付不完整"
    return True, "整体成功"
