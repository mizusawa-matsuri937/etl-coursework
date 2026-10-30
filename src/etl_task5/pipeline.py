"""任务 5 流程步骤：供 Airflow DAG 调用的纯 Python 函数（spec R45）。

DAG amazon_movies_task5_person：check_inputs → mentions → link_external → resolve → movie_person → verify → finalize_report → run_gate。
失败语义与任务 1–4 相同：确定性错误（输入缺失、核对不符、抽取锁被占用）不重试，暂时错误（数据库断连）交给 Airflow 有上限地重试；
收尾与门禁无论上游成败都运行，门禁只在上游全部成功时通过。失败记录、run_step 的写法与任务 4 对应，输出根目录是 outputs/task5。
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
from etl_task1.config import ConfigError, load_settings
from etl_task1.db import connect
from etl_task1.pipeline import FAILED, SUCCEEDED, StepError, gate_decision  # noqa: F401  门禁与任务 1 同义
from etl_task1.pipeline import is_transient

from etl_task2.crawl.crawler import latest_full_extraction
from etl_task2.pipeline import _database_available
from etl_task4.pipeline import InjectedFault, settings_for

from . import DEFAULT_T4_MAPPING_VERSION, NORM_VERSION, RULE_VERSION, check, report
from .rules import ruleset
from .run import DEFAULT_BATCH, RunInputs, Task5InputError, apply_task5_schema, check_inputs
from .runner import StageResult, run_stage

log = logging.getLogger(__name__)

PERSON_DAG_ID = "amazon_movies_task5_person"
PERSON_STEP_NAMES = ("check_inputs", "mentions", "link_external", "resolve", "movie_person", "verify")
PERSON_SUMMARY_VERSION = "task5-person-summary-1"


@dataclass(frozen=True)
class PersonParams:
    run_key: str                    # 一次 DAG run 的稳定标识，重试期间不变；也是 task5.run.airflow_run_id
    extraction_id: str | None = None
    t4_mapping_version_id: str | None = None   # 默认 map0002（属于该抽取时），否则该抽取最新的任务 4 映射版本
    imdb_snapshot_id: str | None = None        # 默认取映射版本所用的快照
    norm_version: str = NORM_VERSION
    rule_version: str = RULE_VERSION
    batch_size: int = DEFAULT_BATCH
    limit: int | None = None        # 只限制提及阶段的商品数，用于试跑；之后的步骤因仍有未处理商品而失败，不生成人物映射版本
    fault_injection: str | None = None   # before_commit:<阶段>:<批号> | transient_once:<标记文件>；仅 ETL_ENV=test
    try_number: int = 1
    dag_id: str = PERSON_DAG_ID

    @staticmethod
    def sanitize(run_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


def output_root(output_dir: Path) -> Path:
    return output_dir / "task5"


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


def run_step(step: str, params: Any, fn: Callable[[], dict[str, Any]], output_dir: Path | None = None) -> dict[str, Any]:
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


def _fault_hook(params: PersonParams) -> Callable[[str, int], None] | None:
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


def default_t4_mapping(conn: psycopg.Connection, extraction_id: str) -> str:
    """默认的任务 4 映射版本：map0002（spec R01）属于该抽取时取它，否则取该抽取最新的映射版本。"""
    if conn.execute("SELECT 1 FROM task4.mapping_version WHERE mapping_version_id = %s AND extraction_id = %s",
                    (DEFAULT_T4_MAPPING_VERSION, extraction_id)).fetchone():
        return DEFAULT_T4_MAPPING_VERSION
    row = conn.execute("SELECT mapping_version_id FROM task4.mapping_version WHERE extraction_id = %s "
                       "ORDER BY created_at DESC, mapping_version_id DESC LIMIT 1", (extraction_id,)).fetchone()
    if row is None:
        raise Task5InputError(f"抽取 {extraction_id} 还没有任务 4 映射版本；请先运行 DAG amazon_movies_task4_match")
    return row[0]


def _inputs(params: PersonParams, checked: dict[str, Any]) -> RunInputs:
    return RunInputs(extraction_id=checked["extraction_id"], t4_mapping_version_id=checked["t4_mapping_version_id"],
                     imdb_snapshot_id=checked["imdb_snapshot_id"], norm_version=params.norm_version,
                     rule_version=params.rule_version)


def check_inputs_step(params: PersonParams) -> dict[str, Any]:
    """R01、R45：应用迁移 401；确认抽取、任务 4 映射版本（属于该抽取）与 IMDb 快照（含标题键）存在。输入不合格是确定性失败。"""
    s = settings_for(params)
    if params.norm_version != NORM_VERSION:
        raise ConfigError(f"规范化版本 {params.norm_version} 未实现（当前 {NORM_VERSION}）")
    ruleset(params.rule_version)         # 未登记的规则版本是确定性错误
    with connect(s) as conn:
        apply_task5_schema(conn)
        eid = params.extraction_id or latest_full_extraction(conn)
        t4 = params.t4_mapping_version_id or default_t4_mapping(conn, eid)
        sid = params.imdb_snapshot_id
        if sid is None:
            row = conn.execute("SELECT imdb_snapshot_id FROM task4.mapping_version WHERE mapping_version_id = %s",
                               (t4,)).fetchone()
            sid = row[0] if row else ""
        counts = check_inputs(conn, RunInputs(extraction_id=eid, t4_mapping_version_id=t4, imdb_snapshot_id=sid,
                                              norm_version=params.norm_version, rule_version=params.rule_version))
    return {"extraction_id": eid, "t4_mapping_version_id": t4, "imdb_snapshot_id": sid,
            "norm_version": params.norm_version, "rule_version": params.rule_version, **counts}


def _stage_step(params: PersonParams, checked: dict[str, Any], stage: str) -> dict[str, Any]:
    s = settings_for(params)
    hook = _fault_hook(params) if stage == "mentions" or (params.fault_injection or "").startswith(
        "before_commit:") else None
    with connect(s) as conn:
        r: StageResult = run_stage(conn, _inputs(params, checked), ruleset(params.rule_version), stage,
                                   airflow_run_id=params.run_key, batch_size=params.batch_size,
                                   limit=params.limit if stage == "mentions" else None, dag_id=params.dag_id,
                                   before_commit=hook)
    return {"stage": stage, "run_id": r.run_id, "status": r.status, "person_mapping_version_id":
            r.person_mapping_version_id, "extraction_id": r.inputs.extraction_id, "elapsed_seconds": r.elapsed_seconds,
            "counts": r.counts}


def mentions_step(params: PersonParams, checked: dict[str, Any]) -> dict[str, Any]:
    return _stage_step(params, checked, "mentions")


def link_external_step(params: PersonParams, checked: dict[str, Any]) -> dict[str, Any]:
    return _stage_step(params, checked, "link_external")


def resolve_step(params: PersonParams, checked: dict[str, Any]) -> dict[str, Any]:
    return _stage_step(params, checked, "resolve")


def movie_person_step(params: PersonParams, checked: dict[str, Any]) -> dict[str, Any]:
    return _stage_step(params, checked, "movie_person")


class PersonVerificationError(RuntimeError):
    """人物结果核对不通过（确定性错误）。"""


def person_verify_step(params: PersonParams, built: dict[str, Any]) -> dict[str, Any]:
    """AC14 的核对：只读，与命令 python -m etl_task5.check 同一份代码。"""
    s = settings_for(params)
    started = time.monotonic()
    pmap = built.get("person_mapping_version_id")
    if not pmap:
        raise PersonVerificationError("上一步没有给出人物映射版本")
    with connect(s) as conn:
        out = check.verify(conn, pmap)
    found = check.problems(out)
    if found:
        raise PersonVerificationError("；".join(found))
    return {**out, "elapsed_seconds": round(time.monotonic() - started, 2)}


def finalize_step(params: PersonParams, upstream: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """无论上游成败都运行：写 summary.json、report.md；核对通过时再写统计、CSV 与实例。写出报告本身不代表成功。"""
    s = load_settings(dict(os.environ))
    checked = upstream.get("check_inputs")
    built = upstream.get("movie_person")
    verified = upstream.get("verify")
    eid = (checked or {}).get("extraction_id") or params.extraction_id
    pmap = (built or {}).get("person_mapping_version_id")
    failures = read_failures(s.output_dir, params.run_key)
    failed_tasks = {f.get("task") for f in failures}
    tasks = {n: ("success" if upstream.get(n) is not None else ("failed" if n in failed_tasks else "not_run"))
             for n in PERSON_STEP_NAMES}
    summary: dict[str, Any] = {
        "summary_version": PERSON_SUMMARY_VERSION, "generated_at": reporting.utc_now(), "dag_id": params.dag_id,
        "run_key": params.run_key, "extraction_id": eid, "rule_version": params.rule_version,
        "norm_version": params.norm_version,
        "params": {k: v for k, v in dataclasses.asdict(params).items() if k not in ("run_key", "try_number")},
        "tasks": tasks, "failures": failures,
        "stage_runs": {n: upstream.get(n) for n in ("mentions", "link_external", "resolve", "movie_person")},
        "verify": verified,
    }
    directory = report.run_dir(s.output_dir, eid, pmap, params.run_key) if (eid and pmap) \
        else failures_dir(s.output_dir, params.run_key)
    if verified:
        with connect(s) as conn:
            summary["stats"] = report.build_summary(conn, pmap)
            summary["examples"] = ex = report.examples(conn, pmap)
            summary["exports"] = report.export_files(conn, pmap, directory, ex)
            summary["examples_file"] = "examples.md"
    summary["database_available"] = _database_available(s)
    final = SUCCEEDED if all(v == "success" for v in tasks.values()) else FAILED
    summary["final_status"] = final
    summary["delivery_complete"] = final == SUCCEEDED
    reporting.write_atomic(directory / "summary.json",
                           (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode())
    reporting.write_atomic(directory / "report.md", report.render_markdown(summary).encode())
    return {"final_status": final, "delivery_complete": summary["delivery_complete"], "report_dir": str(directory),
            "extraction_id": eid, "person_mapping_version_id": pmap}
