"""任务 9 流程步骤：供 Airflow DAG 调用的纯 Python 函数（spec R38–R40、R44）。写法同任务 8 pipeline。

DAG amazon_movies_task9_lineage：check_inputs → build_sources → build_assertions → build_derivations → build_selections →
build_impact → finalize_version → verify → finalize_report → gate。
确定性错误（输入缺失、版本未核实、核对不符、抽取锁被占用）不重试；暂时错误（数据库断连）交给 Airflow 有上限地重试；
收尾与门禁无论上游成败都运行，门禁只在上游全部成功时通过。运行在 verify 通过之后才是 succeeded（R40）。输出根目录
outputs/task9。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from etl_task1 import reporting
from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task1.pipeline import FAILED, SUCCEEDED, StepError, is_transient  # noqa: F401  门禁与任务 1 同义
from etl_task2.crawl.crawler import latest_full_extraction
from etl_task2.pipeline import _database_available
from etl_task4.pipeline import settings_for
from etl_task8.pipeline import _fault_hook     # 故障注入的解析与任务 8 相同（只读 params.fault_injection）

from . import (CURRENT_RULE_VERSION, DEFAULT_CONFLICT_VERSION, DEFAULT_DEDUP_VERSION, DEFAULT_IMDB_SNAPSHOT,
               DEFAULT_PMAP, DEFAULT_T4_MAPPING_VERSION, DEFAULT_T6_RELEASE_VERSION, check, report)
from .run import DEFAULT_BATCH, RunInputs, apply_task9_schema, check_inputs
from .runner import StageResult, VerificationFailed, run_stage

log = logging.getLogger(__name__)

LINEAGE_DAG_ID = "amazon_movies_task9_lineage"
STEP_STAGE = {"build_sources": "sources", "build_assertions": "assertions", "build_derivations": "derivations",
              "build_selections": "selections", "build_impact": "impact", "finalize_version": "finalize_version",
              "verify": "verify"}
STEP_NAMES = ("check_inputs", *STEP_STAGE)
SUMMARY_VERSION = "task9-lineage-run-1"


@dataclass(frozen=True)
class LineageParams:
    run_key: str                    # 一次 DAG run 的稳定标识，重试期间不变；也是 task9.run.airflow_run_id
    extraction_id: str | None = None
    t4_mapping_version_id: str = DEFAULT_T4_MAPPING_VERSION
    t5_person_mapping_version_id: str = DEFAULT_PMAP
    t6_release_version_id: str = DEFAULT_T6_RELEASE_VERSION
    conflict_version_id: str = DEFAULT_CONFLICT_VERSION
    review_dedup_version_id: str = DEFAULT_DEDUP_VERSION
    imdb_snapshot_id: str = DEFAULT_IMDB_SNAPSHOT
    rule_version: str = CURRENT_RULE_VERSION
    batch_size: int = DEFAULT_BATCH
    snapshot_root: str | None = None
    fault_injection: str | None = None   # before_commit:<阶段>:<批号> | transient_once:<标记文件> | verify_fail；仅 ETL_ENV=test
    try_number: int = 1
    dag_id: str = LINEAGE_DAG_ID

    @staticmethod
    def sanitize(run_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


def record_failure(output_dir: Path, params: Any, step: str, exc: BaseException, transient: bool) -> None:
    try:
        entry = {"task": step, "try_number": params.try_number, "error_type": type(exc).__name__,
                 "message": str(exc)[:4000], "transient": transient, "at": reporting.utc_now(),
                 "traceback": "".join(traceback.format_exception(exc))[-6000:]}
        path = report.failures_dir(output_dir, params.run_key) / f"{step}-try{params.try_number}.json"
        reporting.write_atomic(path, (json.dumps(entry, ensure_ascii=False, indent=2) + "\n").encode())
    except Exception:  # noqa: BLE001
        log.exception("无法写入失败记录")


def run_step(step: str, params: Any, fn: Callable[[], dict[str, Any]], output_dir: Path | None = None) -> dict[str, Any]:
    """执行步骤；失败时记录并转成 StepError。核对失败（VerificationFailed）是确定性错误。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        transient = is_transient(exc) and not isinstance(exc, VerificationFailed)
        record_failure(output_dir or Path(os.environ.get("ETL_OUTPUT_DIR", "outputs")), params, step, exc, transient)
        raise StepError(step, exc, transient) from exc


def read_failures(output_dir: Path, run_key: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(report.failures_dir(output_dir, run_key).glob("*.json")):
        if p.name == "run_summary.json":
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return out


def _inputs(params: LineageParams, eid: str) -> RunInputs:
    return RunInputs(extraction_id=eid, t4_mapping_version_id=params.t4_mapping_version_id,
                     t5_person_mapping_version_id=params.t5_person_mapping_version_id,
                     t6_release_version_id=params.t6_release_version_id,
                     conflict_version_id=params.conflict_version_id,
                     review_dedup_version_id=params.review_dedup_version_id,
                     imdb_snapshot_id=params.imdb_snapshot_id, rule_version=params.rule_version)


def check_inputs_step(params: LineageParams) -> dict[str, Any]:
    """R01：应用迁移 801；确认抽取完成、各上游版本存在、相互一致且 cv、rd 已 verified。不合格是确定性失败。"""
    s = settings_for(params)
    with connect(s) as conn:
        eid = params.extraction_id or latest_full_extraction(conn)
        inputs = check_inputs(conn, _inputs(params, eid))
        apply_task9_schema(conn)
    return {"extraction_id": eid, **{k: v for k, v in inputs.as_json().items() if k != "extraction_id"}}


def stage_step(params: LineageParams, checked: dict[str, Any], step: str) -> dict[str, Any]:
    stage = STEP_STAGE[step]
    s = settings_for(params)
    hook = _fault_hook(params) if (stage == "sources" or (params.fault_injection or "").startswith("before_commit:")) \
        else None
    inject = params.fault_injection == "verify_fail" and s.env == "test"
    with connect(s) as conn:
        r: StageResult = run_stage(conn, _inputs(params, checked["extraction_id"]), stage,
                                   airflow_run_id=params.run_key, batch_size=params.batch_size,
                                   snapshot_root=params.snapshot_root, dag_id=params.dag_id, before_commit=hook,
                                   inject_verify_failure=inject and stage == "verify")
        verified = None
        if stage == "verify" and r.status == "reused" and r.lineage_version_id:
            found = check.problems(check.verify(conn, r.lineage_version_id))   # 复用的版本只读再确认
            if found:
                raise VerificationFailed("；".join(found))
            verified = True
    return {"stage": stage, "run_id": r.run_id, "status": r.status, "lineage_version_id": r.lineage_version_id,
            "extraction_id": r.inputs.extraction_id, "elapsed_seconds": r.elapsed_seconds,
            "counts": {k: v for k, v in r.counts.items() if k != "table_counts"}, **({"verified": verified} if verified else {})}


def finalize_step(params: LineageParams, upstream: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """无论上游成败都运行：写 run_summary.json；核对通过时再导出覆盖率、实例与校验和（R44、R45）。"""
    s = load_settings(dict(os.environ))
    checked, verified = upstream.get("check_inputs"), upstream.get("verify")
    eid = (checked or {}).get("extraction_id") or params.extraction_id
    lv = next((u["lineage_version_id"] for u in (verified, upstream.get("finalize_version"))
               if u and u.get("lineage_version_id")), None)
    failed_tasks = {f.get("task") for f in read_failures(s.output_dir, params.run_key)}
    tasks = {n: ("success" if upstream.get(n) is not None else ("failed" if n in failed_tasks else "not_run"))
             for n in STEP_NAMES}
    summary: dict[str, Any] = {
        "summary_version": SUMMARY_VERSION, "generated_at": reporting.utc_now(), "dag_id": params.dag_id,
        "run_key": params.run_key, "extraction_id": eid, "rule_version": params.rule_version,
        "params": {k: v for k, v in dataclasses.asdict(params).items() if k not in ("run_key", "try_number")},
        "tasks": tasks, "failures": read_failures(s.output_dir, params.run_key),
        "stage_runs": {n: upstream.get(n) for n in STEP_STAGE}, "lineage_version_id": lv}
    ok = verified is not None and lv is not None
    directory = report.run_dir(s.output_dir, eid, lv, params.run_key) if ok else report.failures_dir(s.output_dir,
                                                                                                   params.run_key)
    if ok:
        with connect(s) as conn:
            summary["exports"] = report.export_files(conn, lv, directory)
    summary["database_available"] = _database_available(s)
    final = SUCCEEDED if all(v == "success" for v in tasks.values()) else FAILED
    summary["final_status"] = final
    summary["delivery_complete"] = final == SUCCEEDED
    reporting.write_atomic(directory / "run_summary.json",
                           (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode())
    if not ok:
        reporting.write_atomic(directory / "report.md", f"# 血缘运行未成功\n\nRun Key: {params.run_key}\n".encode())
    return {"final_status": final, "delivery_complete": summary["delivery_complete"], "report_dir": str(directory),
            "extraction_id": eid, "lineage_version_id": lv}
