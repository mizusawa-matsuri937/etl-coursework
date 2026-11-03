"""任务 8 流程步骤：供 Airflow DAG 调用的纯 Python 函数（spec R37、R38）。

DAG amazon_reviews_task8_dedup：check_inputs → review_clean → review_family → review_dedup → review_attribution →
finalize_version → verify → finalize_report → gate。
失败语义与任务 1–7 相同：确定性错误（输入缺失、核对不符、抽取锁被占用）不重试，暂时错误（数据库断连）交给 Airflow 有上限地重试；
收尾与门禁无论上游成败都运行，门禁只在上游全部成功时通过。运行状态在 verify 通过之后才是 succeeded，核对失败的版本保持
unverified、不会被 reused（R38）。输出根目录是 outputs/task8。
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

import psycopg

from etl_task1 import reporting
from etl_task1.config import ConfigError, load_settings
from etl_task1.db import connect
from etl_task1.pipeline import FAILED, SUCCEEDED, StepError, gate_decision  # noqa: F401  门禁与任务 1 同义
from etl_task1.pipeline import is_transient

from etl_task2.crawl.crawler import latest_full_extraction
from etl_task2.pipeline import _database_available
from etl_task4.pipeline import InjectedFault, settings_for

from . import CURRENT_RULE_VERSION, DEFAULT_CONFLICT_VERSION, DEFAULT_T3_RULE, check, report
from .rules import ruleset
from .run import DEFAULT_BATCH, DEFAULT_PARTITIONS, RunInputs, apply_task8_schema, check_inputs
from .runner import StageResult, VerificationFailed, run_stage

log = logging.getLogger(__name__)

DEDUP_DAG_ID = "amazon_reviews_task8_dedup"
# DAG 任务名 → 运行器步骤名
STEP_STAGE = {"review_clean": "review_clean", "review_family": "review_family", "review_dedup": "review_dedup",
              "review_attribution": "review_attribution", "finalize_version": "finalize_version", "verify": "verify"}
DEDUP_STEP_NAMES = ("check_inputs", *STEP_STAGE)
DEDUP_SUMMARY_VERSION = "task8-dedup-run-1"


@dataclass(frozen=True)
class DedupParams:
    run_key: str                    # 一次 DAG run 的稳定标识，重试期间不变；也是 task8.run.airflow_run_id
    extraction_id: str | None = None
    t3_rule_version: str = DEFAULT_T3_RULE
    conflict_version_id: str = DEFAULT_CONFLICT_VERSION
    rule_version: str = CURRENT_RULE_VERSION
    batch_size: int = DEFAULT_BATCH
    partitions: int = DEFAULT_PARTITIONS
    fault_injection: str | None = None   # before_commit:<阶段>:<批号> | transient_once:<标记文件> | verify_fail；仅 ETL_ENV=test
    try_number: int = 1
    dag_id: str = DEDUP_DAG_ID

    @staticmethod
    def sanitize(run_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


def _settings(params: DedupParams):
    s = settings_for(params)
    if params.partitions < 1:
        raise ConfigError("partitions 必须为正整数")
    return s


def failures_dir(output_dir: Path, run_key: str) -> Path:
    return report.failures_dir(output_dir, run_key)


def record_failure(output_dir: Path, params: Any, step: str, exc: BaseException, transient: bool) -> None:
    """失败写成文件，数据库不可用时也能留下原因。写不进去只记日志，不掩盖原异常。"""
    try:
        entry = {"task": step, "try_number": params.try_number, "error_type": type(exc).__name__,
                 "message": str(exc)[:4000], "transient": transient, "at": reporting.utc_now(),
                 "traceback": "".join(traceback.format_exception(exc))[-6000:]}
        path = failures_dir(output_dir, params.run_key) / f"{step}-try{params.try_number}.json"
        reporting.write_atomic(path, (json.dumps(entry, ensure_ascii=False, indent=2) + "\n").encode())
    except Exception:  # noqa: BLE001
        log.exception("无法写入失败记录")


def run_step(step: str, params: Any, fn: Callable[[], dict[str, Any]], output_dir: Path | None = None) -> dict[str, Any]:
    """执行步骤；失败时记录并转成 StepError，由 DAG 决定是否重试。核对失败（VerificationFailed）是确定性错误。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        transient = is_transient(exc) and not isinstance(exc, VerificationFailed)
        record_failure(output_dir or Path(os.environ.get("ETL_OUTPUT_DIR", "outputs")), params, step, exc, transient)
        raise StepError(step, exc, transient) from exc


def read_failures(output_dir: Path, run_key: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(failures_dir(output_dir, run_key).glob("*.json")):
        if p.name == "summary.json":
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
            pass
    return out


def _fault_hook(params: DedupParams):
    """故障注入：仅 ETL_ENV=test。返回 before_commit 回调（没有则 None）。verify_fail 不是 before_commit，由 verify 步骤读取。"""
    fault = params.fault_injection
    if not fault or fault == "verify_fail" or os.environ.get("ETL_ENV") != "test":
        return None
    if fault.startswith("transient_once:"):
        marker = Path(fault.split(":", 1)[1])
        if not marker.exists():  # 模拟数据库暂时断连：第一次失败，之后成功
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("injected\n")
            raise psycopg.OperationalError("injected transient connection failure")
        return None
    if fault.startswith("before_commit:"):
        _, stage, batch = fault.split(":")
        target = (stage, int(batch))

        def boom(st: str, batch_no: int) -> None:
            if (st, batch_no) == target:
                raise InjectedFault(f"injected failure before commit of {st} batch {batch_no}")
        return boom
    raise ConfigError(f"未知的故障注入：{fault!r}")


def _inputs(params: DedupParams, checked: dict[str, Any]) -> RunInputs:
    return RunInputs(extraction_id=checked["extraction_id"], t3_rule_version=checked["t3_rule_version"],
                     conflict_version_id=checked["conflict_version_id"], rule_version=params.rule_version)


def check_inputs_step(params: DedupParams) -> dict[str, Any]:
    """R01、R37：应用迁移 701；确认抽取完成、t3r2 分类存在、cv 版本属于该抽取且 verified。输入不合格是确定性失败。"""
    s = _settings(params)
    ruleset(params.rule_version)         # 未登记的规则版本是确定性错误
    with connect(s) as conn:
        eid = params.extraction_id or latest_full_extraction(conn)
        inputs, counts = check_inputs(conn, RunInputs(extraction_id=eid, t3_rule_version=params.t3_rule_version,
                                                      conflict_version_id=params.conflict_version_id,
                                                      rule_version=params.rule_version))
        apply_task8_schema(conn)
    return {"extraction_id": eid, "t3_rule_version": inputs.t3_rule_version,
            "conflict_version_id": inputs.conflict_version_id, "rule_version": params.rule_version, **counts}


def stage_step(params: DedupParams, checked: dict[str, Any], step: str) -> dict[str, Any]:
    """DAG 的一个执行步骤（review_clean … verify）。verify 步骤通过才把运行记为 succeeded（R38）。"""
    stage = STEP_STAGE[step]
    s = _settings(params)
    hook = _fault_hook(params) if (stage == "review_clean" or (params.fault_injection or "").startswith("before_commit:")) else None
    inject = params.fault_injection == "verify_fail" and s.env == "test"
    with connect(s) as conn:
        r: StageResult = run_stage(conn, _inputs(params, checked), ruleset(params.rule_version), stage,
                                   airflow_run_id=params.run_key, batch_size=params.batch_size,
                                   partitions=params.partitions, dag_id=params.dag_id, before_commit=hook,
                                   inject_verify_failure=inject and stage == "verify")
        verified = None
        if stage == "verify" and r.status == "reused" and r.review_dedup_version_id:
            info = check.verify(conn, r.review_dedup_version_id)    # 复用的版本早已通过核对，这里只读确认
            found = check.problems(info)
            if found:
                raise VerificationFailed("；".join(found))
            verified = {"table_counts": info["table_counts"]}
    return {"stage": stage, "run_id": r.run_id, "status": r.status, "review_dedup_version_id": r.review_dedup_version_id,
            "extraction_id": r.inputs.extraction_id, "elapsed_seconds": r.elapsed_seconds, "counts": r.counts,
            **({"verified": verified} if verified else {})}


def finalize_step(params: DedupParams, upstream: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """无论上游成败都运行：写 run_summary.json；核对通过时再写统计、实例、CSV 与校验和。写出报告本身不代表成功。"""
    s = load_settings(dict(os.environ))
    checked = upstream.get("check_inputs")
    verified = upstream.get("verify")
    eid = (checked or {}).get("extraction_id") or params.extraction_id
    rd = next((u["review_dedup_version_id"] for u in (verified, upstream.get("finalize_version"))
               if u and u.get("review_dedup_version_id")), None)
    failures = read_failures(s.output_dir, params.run_key)
    failed_tasks = {f.get("task") for f in failures}
    tasks = {n: ("success" if upstream.get(n) is not None else ("failed" if n in failed_tasks else "not_run"))
             for n in DEDUP_STEP_NAMES}
    summary: dict[str, Any] = {
        "summary_version": DEDUP_SUMMARY_VERSION, "generated_at": reporting.utc_now(), "dag_id": params.dag_id,
        "run_key": params.run_key, "extraction_id": eid, "rule_version": params.rule_version,
        "params": {k: v for k, v in dataclasses.asdict(params).items() if k not in ("run_key", "try_number")},
        "tasks": tasks, "failures": failures,
        "stage_runs": {n: upstream.get(n) for n in STEP_STAGE}, "review_dedup_version_id": rd,
    }
    ok = verified is not None and rd is not None
    directory = report.run_dir(s.output_dir, eid, rd, params.run_key) if (eid and rd and ok) \
        else failures_dir(s.output_dir, params.run_key)
    if ok:
        with connect(s) as conn:
            summary["stats"] = stats = report.build_summary(conn, rd, run_id=verified["run_id"])
            ex = report.examples(conn, rd)
            summary["exports"] = report.export_files(conn, rd, directory, ex, run_id=verified["run_id"], summary=stats)
            summary["examples_file"] = "examples.md"
    summary["database_available"] = _database_available(s)
    final = SUCCEEDED if all(v == "success" for v in tasks.values()) else FAILED
    summary["final_status"] = final
    summary["delivery_complete"] = final == SUCCEEDED
    reporting.write_atomic(directory / "run_summary.json",
                           (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode())
    if not ok:
        reporting.write_atomic(directory / "report.md", f"# 评论去重运行未成功\n\nRun Key: {params.run_key}\n".encode())
    return {"final_status": final, "delivery_complete": summary["delivery_complete"], "report_dir": str(directory),
            "extraction_id": eid, "review_dedup_version_id": rd}
