"""任务 4 流程步骤：供 Airflow DAG 调用的纯 Python 函数（spec R40）。

两个 DAG：
- imdb_reference_task4_load：check_files → load_snapshot → verify_snapshot → run_gate；
- amazon_movies_task4_match：check_inputs → normalize → candidates_decide → cluster_map → verify → finalize_report → run_gate。
失败语义与任务 1–3 相同：确定性错误（哈希不符、输入缺失、核对不符、抽取锁被占用）不重试，暂时错误（数据库断连、IMDb 快照装载锁被占用）
交给 Airflow 有上限地重试；收尾与门禁无论上游成败都运行，门禁只在上游全部成功时通过。两个 DAG 共用失败记录与 run_step。
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

from . import DEFAULT_T3_RULE_VERSION, NORM_VERSION, RULE_VERSION, check, imdb, report
from .match import ruleset
from .run import DEFAULT_BATCH, RunInputs, Task4InputError, apply_task4_schema, check_inputs
from .runner import StageResult, run_stage

log = logging.getLogger(__name__)

IMDB_DAG_ID = "imdb_reference_task4_load"
IMDB_STEP_NAMES = ("check_files", "load_snapshot", "verify_snapshot")
SUMMARY_VERSION = "task4-imdb-summary-1"


@dataclass(frozen=True)
class ImdbParams:
    run_key: str                    # 一次 DAG run 的稳定标识，重试期间不变
    snapshot_id: str | None = None
    root: str | None = None         # 快照所在父目录；默认取 IMDB_DATA_DIR 或 /data/imdb
    fault_injection: str | None = None   # before_commit | transient_once:<标记文件>；仅 ETL_ENV=test
    try_number: int = 1
    dag_id: str = IMDB_DAG_ID

    @staticmethod
    def sanitize(run_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


class InjectedFault(RuntimeError):
    """测试用确定性故障。"""


class SnapshotVerificationError(RuntimeError):
    """IMDb 快照核对不通过（确定性错误）。"""


def is_transient(exc: BaseException) -> bool:
    return isinstance(exc, imdb.ImdbBusy) or _task1_transient(exc)


def settings_for(params: Any, env: dict[str, str] | None = None) -> Settings:
    s = load_settings(dict(os.environ if env is None else env))
    if getattr(params, "fault_injection", None) and s.env != "test":
        raise ConfigError("故障注入仅允许在 ETL_ENV=test 下启用")
    batch, limit = getattr(params, "batch_size", None), getattr(params, "limit", None)
    if (batch is not None and batch < 1) or (limit is not None and limit < 1):
        raise ConfigError("batch_size 与 limit 必须为正整数")
    return s


def output_root(output_dir: Path) -> Path:
    return output_dir / "task4"


def failures_dir(output_dir: Path, run_key: str) -> Path:
    return output_root(output_dir) / "_failures" / run_key


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


def run_step(step: str, params: Any, fn: Callable[[], dict[str, Any]],
             output_dir: Path | None = None) -> dict[str, Any]:
    """执行步骤；失败时记录并转成 StepError，由 DAG 决定是否重试。"""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        transient = is_transient(exc)
        record_failure(output_dir or Path(os.environ.get("ETL_OUTPUT_DIR", "outputs")), params, step, exc, transient)
        raise StepError(step, exc, transient) from exc


def read_failures(output_dir: Path, run_key: str) -> list[dict[str, Any]]:
    out = []
    for p in sorted(failures_dir(output_dir, run_key).glob("*.json")):
        if p.name == "summary.json":
            continue
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            out.append({"task": p.stem, "error_type": "unreadable_failure_file", "message": str(p)})
    return out


def _fault_hook(params: Any) -> Callable[[], None] | None:
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
    if fault == "before_commit":
        def boom() -> None:
            raise InjectedFault("injected failure before commit")
        return boom
    raise ConfigError(f"未知的故障注入：{fault!r}")


# ---------------------------------------------------------------- DAG imdb_reference_task4_load

def _snapshot_directory(params: ImdbParams) -> tuple[str, Path]:
    if not params.snapshot_id:
        raise ConfigError("必须提供 snapshot_id 参数（形如 imdb_2026-10-30）")
    return params.snapshot_id, imdb.snapshot_dir(params.snapshot_id, params.root)


def check_files_step(params: ImdbParams) -> dict[str, Any]:
    """R04：应用迁移，逐个文件核对大小与 SHA-256；不一致即确定性失败，库里什么都不写。"""
    sid, directory = _snapshot_directory(params)
    s = settings_for(params)
    started = time.monotonic()
    record = imdb.check_files(directory)
    if record.snapshot_id != sid:
        raise imdb.ImdbInputError(f"下载记录里的快照编号 {record.snapshot_id} ≠ 参数 {sid}")
    with connect(s) as conn:
        imdb.apply_imdb_schema(conn)
    return {"snapshot_id": sid, "directory": str(directory), "base_url": record.base_url,
            "downloaded_at": record.downloaded_at, "files": {n: {"size_bytes": f.size_bytes, "sha256": f.sha256}
                                                           for n, f in record.files.items()},
            "elapsed_seconds": round(time.monotonic() - started, 2)}


def load_step(params: ImdbParams, checked: dict[str, Any]) -> dict[str, Any]:
    """R05、R38：流式过滤装载；已装载的同一快照返回 reused。"""
    s = settings_for(params)
    hook = _fault_hook(params)
    with connect(s) as conn:
        result = imdb.load_snapshot(conn, Path(checked["directory"]), snapshot_id=checked["snapshot_id"],
                                    before_commit=hook)
    return dataclasses.asdict(result)


def verify_step(params: ImdbParams, snapshot_id: str) -> dict[str, Any]:
    """装载结果与文件记录核对。"""
    s = settings_for(params)
    started = time.monotonic()
    with connect(s) as conn:
        out = imdb.verify_snapshot(conn, snapshot_id)
    found = imdb.problems(out)
    if found:
        raise SnapshotVerificationError("；".join(found))
    return {**out, "warnings": imdb.warnings(out), "elapsed_seconds": round(time.monotonic() - started, 2)}


def gate_step(params: ImdbParams, upstream: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """无论上游成败都运行：写 summary.json，整体成功才算交付完整。"""
    s = load_settings(dict(os.environ))
    checked = upstream.get("check_files")
    sid = (checked or {}).get("snapshot_id") or params.snapshot_id
    failures = read_failures(s.output_dir, params.run_key)
    failed_tasks = {f.get("task") for f in failures}
    tasks = {n: ("success" if upstream.get(n) is not None else ("failed" if n in failed_tasks else "not_run"))
             for n in IMDB_STEP_NAMES}
    final = SUCCEEDED if all(v == "success" for v in tasks.values()) else FAILED
    directory = (output_root(s.output_dir) / "imdb" / sid / params.run_key) if (checked and sid) \
        else failures_dir(s.output_dir, params.run_key)
    summary = {"summary_version": SUMMARY_VERSION, "generated_at": reporting.utc_now(), "dag_id": params.dag_id,
               "run_key": params.run_key, "snapshot_id": sid, "tasks": tasks, "failures": failures,
               "load": upstream.get("load_snapshot"), "verify": upstream.get("verify_snapshot"),
               "final_status": final, "delivery_complete": final == SUCCEEDED}
    reporting.write_atomic(directory / "summary.json",
                           (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode())
    return {"final_status": final, "delivery_complete": summary["delivery_complete"],
            "report_dir": str(directory), "snapshot_id": sid}


# ---------------------------------------------------------------- DAG amazon_movies_task4_match

MATCH_DAG_ID = "amazon_movies_task4_match"
MATCH_STEP_NAMES = ("check_inputs", "normalize", "candidates_decide", "cluster_map", "verify")
MATCH_SUMMARY_VERSION = "task4-match-summary-1"
STAGE_OF_STEP = {"normalize": "normalize", "candidates_decide": "decide", "cluster_map": "cluster"}


@dataclass(frozen=True)
class MatchParams:
    run_key: str                    # 一次 DAG run 的稳定标识，重试期间不变；也是 task4.run.airflow_run_id
    extraction_id: str | None = None
    t3_rule_version: str = DEFAULT_T3_RULE_VERSION
    imdb_snapshot_id: str | None = None
    norm_version: str = NORM_VERSION
    rule_version: str = RULE_VERSION
    batch_size: int = DEFAULT_BATCH
    limit: int | None = None        # 只限制标准化的商品数，用于试跑；之后的步骤因仍有未处理商品而失败，不生成映射
    fault_injection: str | None = None   # before_commit:<阶段>:<批号> | transient_once:<标记文件>；仅 ETL_ENV=test
    try_number: int = 1
    dag_id: str = MATCH_DAG_ID

    @staticmethod
    def sanitize(run_id: str) -> str:
        return ImdbParams.sanitize(run_id)


class MatchVerificationError(RuntimeError):
    """映射结果核对不通过（确定性错误）。"""


def _match_fault_hook(params: MatchParams) -> Callable[[str, int], None] | None:
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
        _, stage, batch = fault.split(":")
        target = (stage, int(batch))

        def boom(st: str, batch_no: int) -> None:
            if (st, batch_no) == target:
                raise InjectedFault(f"injected failure before commit of {st} batch {batch_no}")
        return boom
    raise ConfigError(f"未知的故障注入：{fault!r}")


def _inputs(params: MatchParams, checked: dict[str, Any]) -> RunInputs:
    return RunInputs(extraction_id=checked["extraction_id"], imdb_snapshot_id=checked["imdb_snapshot_id"],
                     t3_rule_version=params.t3_rule_version, norm_version=params.norm_version,
                     rule_version=params.rule_version)


def check_inputs_step(params: MatchParams) -> dict[str, Any]:
    """R01、R40：应用迁移；确认抽取、任务 3 分类（覆盖整张清单）与 IMDb 快照存在。输入不合格是确定性失败。"""
    s = settings_for(params)
    if params.norm_version != NORM_VERSION:
        raise ConfigError(f"标准化版本 {params.norm_version} 未实现（当前 {NORM_VERSION}）")
    ruleset(params.rule_version)         # 未登记的规则版本是确定性错误
    with connect(s) as conn:
        imdb.apply_imdb_schema(conn)
        apply_task4_schema(conn)
        eid = params.extraction_id or latest_full_extraction(conn)
        sid = params.imdb_snapshot_id
        if sid is None:
            row = conn.execute("SELECT snapshot_id FROM imdb.snapshot WHERE status = 'loaded' "
                               "ORDER BY loaded_at DESC LIMIT 1").fetchone()
            if row is None:
                raise Task4InputError("没有已装载的 IMDb 快照；请先运行 DAG imdb_reference_task4_load")
            sid = row[0]
        counts = check_inputs(conn, RunInputs(extraction_id=eid, imdb_snapshot_id=sid,
                                              t3_rule_version=params.t3_rule_version, norm_version=params.norm_version,
                                              rule_version=params.rule_version))
    return {"extraction_id": eid, "imdb_snapshot_id": sid, "t3_rule_version": params.t3_rule_version,
            "norm_version": params.norm_version, "rule_version": params.rule_version, **counts}


def _stage_step(params: MatchParams, checked: dict[str, Any], stage: str) -> dict[str, Any]:
    s = settings_for(params)
    hook = _match_fault_hook(params) if stage == "normalize" or (params.fault_injection or "").startswith(
        "before_commit:") else None
    with connect(s) as conn:
        r: StageResult = run_stage(conn, _inputs(params, checked), ruleset(params.rule_version), stage,
                                   airflow_run_id=params.run_key, batch_size=params.batch_size,
                                   limit=params.limit if stage == "normalize" else None, dag_id=params.dag_id,
                                   before_commit=hook)
    return {"stage": stage, "run_id": r.run_id, "status": r.status, "mapping_version_id": r.mapping_version_id,
            "extraction_id": r.inputs.extraction_id, "elapsed_seconds": r.elapsed_seconds, "counts": r.counts}


def normalize_step(params: MatchParams, checked: dict[str, Any]) -> dict[str, Any]:
    return _stage_step(params, checked, "normalize")


def candidates_decide_step(params: MatchParams, checked: dict[str, Any]) -> dict[str, Any]:
    return _stage_step(params, checked, "decide")


def cluster_map_step(params: MatchParams, checked: dict[str, Any]) -> dict[str, Any]:
    return _stage_step(params, checked, "cluster")


def match_verify_step(params: MatchParams, clustered: dict[str, Any]) -> dict[str, Any]:
    """AC14 的核对：只读，与命令 python -m etl_task4.check 同一份代码。"""
    s = settings_for(params)
    started = time.monotonic()
    version = clustered.get("mapping_version_id")
    if not version:
        raise MatchVerificationError("上一步没有给出映射版本")
    with connect(s) as conn:
        out = check.verify(conn, version)
    found = check.problems(out)
    if found:
        raise MatchVerificationError("；".join(found))
    return {**out, "elapsed_seconds": round(time.monotonic() - started, 2)}


def finalize_step(params: MatchParams, upstream: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """无论上游成败都运行：写 summary.json、report.md；核对通过时再写统计、CSV 与实例。写出报告本身不代表成功。"""
    s = load_settings(dict(os.environ))
    checked = upstream.get("check_inputs")
    clustered = upstream.get("cluster_map")
    verified = upstream.get("verify")
    eid = (checked or {}).get("extraction_id") or params.extraction_id
    version = (clustered or {}).get("mapping_version_id")
    failures = read_failures(s.output_dir, params.run_key)
    failed_tasks = {f.get("task") for f in failures}
    tasks = {n: ("success" if upstream.get(n) is not None else ("failed" if n in failed_tasks else "not_run"))
             for n in MATCH_STEP_NAMES}
    summary: dict[str, Any] = {
        "summary_version": MATCH_SUMMARY_VERSION, "generated_at": reporting.utc_now(), "dag_id": params.dag_id,
        "run_key": params.run_key, "extraction_id": eid, "rule_version": params.rule_version,
        "norm_version": params.norm_version,
        "params": {k: v for k, v in dataclasses.asdict(params).items() if k not in ("run_key", "try_number")},
        "tasks": tasks, "failures": failures,
        "stage_runs": {n: upstream.get(n) for n in ("normalize", "candidates_decide", "cluster_map")},
        "verify": verified,
    }
    directory = report.run_dir(s.output_dir, eid, version, params.run_key) if (eid and version) \
        else failures_dir(s.output_dir, params.run_key)
    if verified:
        with connect(s) as conn:
            summary["stats"] = report.build_summary(conn, version)
            summary["examples"] = ex = report.examples(conn, version)
            summary["exports"] = report.export_files(conn, version, directory, ex)
            summary["examples_file"] = "examples.md"
    summary["database_available"] = _database_available(s)
    final = SUCCEEDED if all(v == "success" for v in tasks.values()) else FAILED
    summary["final_status"] = final
    summary["delivery_complete"] = final == SUCCEEDED
    reporting.write_atomic(directory / "summary.json",
                           (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode())
    reporting.write_atomic(directory / "report.md", report.render_markdown(summary).encode())
    return {"final_status": final, "delivery_complete": summary["delivery_complete"], "report_dir": str(directory),
            "extraction_id": eid, "mapping_version_id": version}
