"""任务 3 流程步骤：供 DAG amazon_products_task3_classify 调用的纯 Python 函数（spec R27）。

DAG：check_input → classify_products → verify_classification → finalize_report → run_gate。
失败语义与任务 1、任务 2 相同：确定性错误不重试，暂时错误（数据库断连、同一版本的锁被占用）交给 Airflow
有上限地重试；分类可续跑，重试只处理剩余 ASIN。收尾无论成败都运行，门禁判定整体结果。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import psycopg

from etl_task1 import reporting
from etl_task1.config import ConfigError, Settings, load_settings
from etl_task1.db import connect
from etl_task1.pipeline import FAILED, SUCCEEDED, StepError, gate_decision  # noqa: F401  门禁与任务 1 同义
from etl_task1.pipeline import is_transient as _task1_transient
from etl_task2.crawl.crawler import latest_full_extraction
from etl_task2.pipeline import _database_available

from . import LABEL_MAP_VERSION, PARSER_VERSION, RULE_VERSION
from .classify import ClassifyBusy, apply_task3_schema, classify_extraction, input_groups, problems, verify
from .report import build_summary, export_files, output_root, render_markdown, run_dir

log = logging.getLogger(__name__)

DAG_ID = "amazon_products_task3_classify"
STEP_NAMES = ("check_input", "classify_products", "verify_classification")
SUMMARY_VERSION = "task3-classify-summary-1"


@dataclass(frozen=True)
class ClassifyParams:
    run_key: str                    # 一次 DAG run 的稳定标识，重试期间不变
    extraction_id: str | None = None
    limit: int | None = None        # 只用于试跑；限定运行的核对必然失败，不冒充全量（spec H12）
    batch_size: int = 500
    fault_injection: str | None = None   # before_commit:<批号> | transient_once:<标记文件>；仅 ETL_ENV=test
    try_number: int = 1
    dag_id: str = DAG_ID

    @staticmethod
    def sanitize(run_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


class InjectedFault(RuntimeError):
    """测试用确定性故障。"""


class ClassifyVerificationError(RuntimeError):
    """分类结果核对不通过（确定性错误）。"""


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, ClassifyBusy) or _task1_transient(exc)


def settings_for(params: ClassifyParams, env: dict[str, str] | None = None) -> Settings:
    s = load_settings(dict(os.environ if env is None else env))
    if params.fault_injection and s.env != "test":
        raise ConfigError("故障注入仅允许在 ETL_ENV=test 下启用")
    if params.batch_size < 1 or (params.limit is not None and params.limit < 1):
        raise ConfigError("batch_size 与 limit 必须为正整数")
    return s


def failures_dir(output_dir: Path, run_key: str) -> Path:
    return output_root(output_dir) / "_failures" / run_key


def record_failure(output_dir: Path, params: ClassifyParams, step: str, exc: BaseException, transient: bool) -> None:
    """失败写成文件，数据库不可用时也能留下原因。写不进去只记日志，不掩盖原异常。"""
    try:
        entry = {"task": step, "try_number": params.try_number, "error_type": type(exc).__name__,
                 "message": str(exc)[:4000], "transient": transient, "at": reporting.utc_now(),
                 "traceback": "".join(traceback.format_exception(exc))[-6000:]}
        path = failures_dir(output_dir, params.run_key) / f"{step}-try{params.try_number}.json"
        reporting.write_atomic(path, (json.dumps(entry, ensure_ascii=False, indent=2) + "\n").encode())
    except Exception:  # noqa: BLE001
        log.exception("无法写入失败记录")


def run_step(step: str, params: ClassifyParams, fn: Callable[[], dict[str, Any]],
             output_dir: Path | None = None) -> dict[str, Any]:
    """执行步骤；失败时记录并转成 StepError，由 DAG 决定是否重试。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        transient = is_transient(exc)
        record_failure(output_dir or Path(os.environ.get("ETL_OUTPUT_DIR", "outputs")), params, step, exc, transient)
        raise StepError(step, exc, transient) from exc


def _read_failures(output_dir: Path, run_key: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(failures_dir(output_dir, run_key).glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            out.append({"task": p.stem, "error_type": "unreadable_failure_file", "message": str(p)})
    return out


def _fault_hook(params: ClassifyParams) -> Callable[[int], None] | None:
    fault = params.fault_injection
    if not fault:
        return None
    if fault.startswith("transient_once:"):
        marker = Path(fault.split(":", 1)[1])
        if not marker.exists():  # 模拟数据库暂时断连：第一次失败，之后成功
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("injected\n")
            raise psycopg.OperationalError("injected transient connection failure")
        return None
    if fault.startswith("before_commit:"):
        target = int(fault.split(":", 1)[1])

        def boom(batch_no: int) -> None:
            if batch_no == target:
                raise InjectedFault(f"injected failure before commit of batch {batch_no}")
        return boom
    raise ConfigError(f"未知的故障注入：{fault!r}")


def check_input_step(params: ClassifyParams) -> dict[str, Any]:
    """R01、R03、R29：应用迁移，确认清单存在且分组之和等于清单行数，记录运行前的分组。"""
    s = settings_for(params)
    with connect(s) as conn:
        apply_task3_schema(conn)
        eid = params.extraction_id or latest_full_extraction(conn)
        if not conn.execute("SELECT 1 FROM public.extraction WHERE extraction_id = %s", (eid,)).fetchone():
            raise ConfigError(f"抽取 {eid} 不存在")
        groups = input_groups(conn, eid)
    return {"extraction_id": eid, "input_groups": groups, "manifest_rows": sum(groups.values()),
            "parser_version": PARSER_VERSION, "label_map_version": LABEL_MAP_VERSION, "rule_version": RULE_VERSION}


def classify_step(params: ClassifyParams, checked: dict[str, Any]) -> dict[str, Any]:
    s = settings_for(params)
    hook = _fault_hook(params)
    started = time.monotonic()
    with connect(s) as conn:
        r = classify_extraction(conn, checked["extraction_id"], batch_size=params.batch_size, limit=params.limit,
                                dag_id=params.dag_id, airflow_run_id=params.run_key, before_commit=hook)
    return {**dataclasses.asdict(r), "elapsed_seconds": round(time.monotonic() - started, 2)}


def verify_step(params: ClassifyParams, extraction_id: str) -> dict[str, Any]:
    """R02、R17、R22：每个 ASIN 恰好一行分类、证据齐全、评论数一致。"""
    s = settings_for(params)
    started = time.monotonic()
    with connect(s) as conn:
        out = verify(conn, extraction_id)
    found = problems(out)
    if found:
        raise ClassifyVerificationError("；".join(found))
    return {**out, "elapsed_seconds": round(time.monotonic() - started, 2)}


def finalize_step(params: ClassifyParams, upstream: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """无论上游成败都运行：写 summary.json、report.md；核对通过时再写统计与 CSV。写出报告本身不代表成功。
    参数不合法时也要留下报告，所以这里只读配置、不校验参数。"""
    s = load_settings(dict(os.environ))
    checked, classified, verified = (upstream.get(n) for n in STEP_NAMES)
    eid = (checked or {}).get("extraction_id") or params.extraction_id
    failures = _read_failures(s.output_dir, params.run_key)
    failed_tasks = {f.get("task") for f in failures}
    tasks = {n: ("success" if upstream.get(n) is not None else ("failed" if n in failed_tasks else "not_run"))
             for n in STEP_NAMES}
    summary: dict[str, Any] = {
        "summary_version": SUMMARY_VERSION, "generated_at": reporting.utc_now(), "dag_id": params.dag_id,
        "run_key": params.run_key, "extraction_id": eid, "rule_version": RULE_VERSION,
        "parser_version": PARSER_VERSION, "label_map_version": LABEL_MAP_VERSION,
        "params": {k: v for k, v in dataclasses.asdict(params).items() if k not in ("run_key", "try_number")},
        "tasks": tasks, "failures": failures, "classify_run": classified, "verify": verified,
    }
    directory = run_dir(s.output_dir, checked["extraction_id"], RULE_VERSION, params.run_key) if checked \
        else failures_dir(s.output_dir, params.run_key)
    if verified:
        with connect(s) as conn:
            summary["stats"] = build_summary(conn, eid)
            summary["exports"] = export_files(conn, eid, RULE_VERSION, directory, summary["stats"])
    summary["database_available"] = _database_available(s)
    final = SUCCEEDED if all(v == "success" for v in tasks.values()) else FAILED
    summary["final_status"] = final
    summary["delivery_complete"] = final == SUCCEEDED
    reporting.write_atomic(directory / "summary.json",
                           (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode())
    reporting.write_atomic(directory / "report.md", render_markdown(summary).encode())
    return {"final_status": final, "delivery_complete": summary["delivery_complete"],
            "report_dir": str(directory), "extraction_id": eid}
