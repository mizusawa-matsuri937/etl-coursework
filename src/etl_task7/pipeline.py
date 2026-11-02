"""任务 7 流程步骤：供 Airflow DAG 调用的纯 Python 函数（spec R54、R55）。

DAG amazon_movies_task7_conflict：check_inputs → edition_resolve → link_assess → movie_scalar_resolve →
movie_multivalue_resolve → finalize_version → verify → finalize_report → gate。
失败语义与任务 1–6 相同：确定性错误（输入缺失、核对不符、抽取锁被占用）不重试，暂时错误（数据库断连）交给 Airflow 有上限地重试；
收尾与门禁无论上游成败都运行，门禁只在上游全部成功时通过。与任务 6 的差别（R55）：运行状态在 verify 通过之后才是 succeeded，
核对失败的版本保持 unverified、不会被 reused。输出根目录是 outputs/task7。
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

from . import DEFAULT_PMAP, DEFAULT_T4_MAPPING_VERSION, DEFAULT_T6_RELEASE_VERSION, check, report
from .rules import CURRENT_RULE_VERSION, ruleset
from .run import DEFAULT_BATCH, RunInputs, Task7InputError, apply_task7_schema, check_inputs
from .runner import StageResult, VerificationFailed, run_stage

log = logging.getLogger(__name__)

CONFLICT_DAG_ID = "amazon_movies_task7_conflict"
# DAG 任务名 → 运行器阶段名
STEP_STAGE = {"edition_resolve": "edition_resolve", "link_assess": "link_assess", "movie_scalar_resolve": "movie_scalar",
              "movie_multivalue_resolve": "movie_multivalue", "finalize_version": "finalize_version", "verify": "verify"}
CONFLICT_STEP_NAMES = ("check_inputs", *STEP_STAGE)
CONFLICT_SUMMARY_VERSION = "task7-conflict-run-1"


@dataclass(frozen=True)
class ConflictParams:
    run_key: str                    # 一次 DAG run 的稳定标识，重试期间不变；也是 task7.run.airflow_run_id
    extraction_id: str | None = None
    t4_mapping_version_id: str | None = None   # 默认 map0002（属于该抽取时），否则该抽取最新的任务 4 映射版本
    t5_person_mapping_version_id: str | None = None  # 默认 pmap0001（基于所选映射版本时），否则最新的
    t6_release_version_id: str | None = None   # 默认 rv0001（基于所选映射与人物版本时），否则最新的
    imdb_snapshot_id: str | None = None        # 默认取映射版本所用的快照
    rule_version: str = CURRENT_RULE_VERSION
    batch_size: int = DEFAULT_BATCH
    fault_injection: str | None = None   # before_commit:<阶段>:<批号> | transient_once:<标记文件> | verify_fail；仅 ETL_ENV=test
    try_number: int = 1
    dag_id: str = CONFLICT_DAG_ID

    @staticmethod
    def sanitize(run_id: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", run_id)


def output_root(output_dir: Path) -> Path:
    return output_dir / "task7"


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


def _fault_hook(params: ConflictParams):
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


def default_t4_mapping(conn: psycopg.Connection, extraction_id: str) -> str:
    if conn.execute("SELECT 1 FROM task4.mapping_version WHERE mapping_version_id = %s AND extraction_id = %s",
                    (DEFAULT_T4_MAPPING_VERSION, extraction_id)).fetchone():
        return DEFAULT_T4_MAPPING_VERSION
    row = conn.execute("SELECT mapping_version_id FROM task4.mapping_version WHERE extraction_id = %s "
                       "ORDER BY created_at DESC, mapping_version_id DESC LIMIT 1", (extraction_id,)).fetchone()
    if row is None:
        raise Task7InputError(f"抽取 {extraction_id} 还没有任务 4 映射版本；请先运行 DAG amazon_movies_task4_match")
    return row[0]


def default_t5_pmap(conn: psycopg.Connection, extraction_id: str, t4: str) -> str:
    if conn.execute("SELECT 1 FROM task5.person_mapping_version WHERE person_mapping_version_id = %s "
                    "AND extraction_id = %s AND t4_mapping_version_id = %s", (DEFAULT_PMAP, extraction_id, t4)).fetchone():
        return DEFAULT_PMAP
    row = conn.execute("SELECT person_mapping_version_id FROM task5.person_mapping_version WHERE extraction_id = %s "
                       "AND t4_mapping_version_id = %s ORDER BY created_at DESC, person_mapping_version_id DESC LIMIT 1",
                       (extraction_id, t4)).fetchone()
    if row is None:
        raise Task7InputError(f"基于（{extraction_id}, {t4}）还没有任务 5 人物映射版本；请先运行人物 DAG")
    return row[0]


def default_t6_release(conn: psycopg.Connection, extraction_id: str, t4: str, pmap: str) -> str:
    if conn.execute("SELECT 1 FROM task6.release_version WHERE release_version_id = %s AND extraction_id = %s "
                    "AND t4_mapping_version_id = %s AND t5_person_mapping_version_id = %s",
                    (DEFAULT_T6_RELEASE_VERSION, extraction_id, t4, pmap)).fetchone():
        return DEFAULT_T6_RELEASE_VERSION
    row = conn.execute("SELECT release_version_id FROM task6.release_version WHERE extraction_id = %s "
                       "AND t4_mapping_version_id = %s AND t5_person_mapping_version_id = %s "
                       "ORDER BY created_at DESC, release_version_id DESC LIMIT 1", (extraction_id, t4, pmap)).fetchone()
    if row is None:
        raise Task7InputError(f"基于（{t4}, {pmap}）还没有任务 6 上映版本；请先运行 DAG amazon_movies_task6_release")
    return row[0]


def _inputs(params: ConflictParams, checked: dict[str, Any]) -> RunInputs:
    return RunInputs(extraction_id=checked["extraction_id"], t4_mapping_version_id=checked["t4_mapping_version_id"],
                     t5_person_mapping_version_id=checked["t5_person_mapping_version_id"],
                     t6_release_version_id=checked["t6_release_version_id"],
                     imdb_snapshot_id=checked["imdb_snapshot_id"], rule_version=params.rule_version)


def check_inputs_step(params: ConflictParams) -> dict[str, Any]:
    """R01、R54：应用迁移 601；确认抽取、任务 4 映射、任务 5 人物映射、任务 6 上映版本与 IMDb 快照存在且相容。输入不合格是确定性失败。"""
    s = settings_for(params)
    ruleset(params.rule_version)         # 未登记的规则版本是确定性错误
    with connect(s) as conn:
        apply_task7_schema(conn)
        eid = params.extraction_id or latest_full_extraction(conn)
        t4 = params.t4_mapping_version_id or default_t4_mapping(conn, eid)
        pmap = params.t5_person_mapping_version_id or default_t5_pmap(conn, eid, t4)
        rv = params.t6_release_version_id or default_t6_release(conn, eid, t4, pmap)
        sid = params.imdb_snapshot_id
        if sid is None:
            row = conn.execute("SELECT imdb_snapshot_id FROM task4.mapping_version WHERE mapping_version_id = %s", (t4,)).fetchone()
            sid = row[0] if row else ""
        counts = check_inputs(conn, RunInputs(extraction_id=eid, t4_mapping_version_id=t4,
                                              t5_person_mapping_version_id=pmap, t6_release_version_id=rv,
                                              imdb_snapshot_id=sid, rule_version=params.rule_version))
    return {"extraction_id": eid, "t4_mapping_version_id": t4, "t5_person_mapping_version_id": pmap,
            "t6_release_version_id": rv, "imdb_snapshot_id": sid, "rule_version": params.rule_version, **counts}


def stage_step(params: ConflictParams, checked: dict[str, Any], step: str) -> dict[str, Any]:
    """DAG 的一个执行步骤（edition_resolve … verify）。verify 步骤通过才把运行记为 succeeded（R55）。"""
    stage = STEP_STAGE[step]
    s = settings_for(params)
    hook = _fault_hook(params) if (stage == "edition_resolve" or (params.fault_injection or "").startswith("before_commit:")) else None
    inject = params.fault_injection == "verify_fail" and s.env == "test"
    with connect(s) as conn:
        r: StageResult = run_stage(conn, _inputs(params, checked), ruleset(params.rule_version), stage,
                                   airflow_run_id=params.run_key, batch_size=params.batch_size, dag_id=params.dag_id,
                                   before_commit=hook, inject_verify_failure=inject and stage == "verify")
        verified = None
        if stage == "verify" and r.status == "reused" and r.conflict_version_id:
            info = check.verify(conn, r.conflict_version_id)       # 复用的版本早已通过核对，这里只读确认
            found = check.problems(info)
            if found:
                raise VerificationFailed("；".join(found))
            verified = {"table_counts": info["table_counts"]}
    return {"stage": stage, "run_id": r.run_id, "status": r.status, "conflict_version_id": r.conflict_version_id,
            "extraction_id": r.inputs.extraction_id, "elapsed_seconds": r.elapsed_seconds, "counts": r.counts,
            **({"verified": verified} if verified else {})}


def finalize_step(params: ConflictParams, upstream: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
    """无论上游成败都运行：写 summary.json、report.md；核对通过时再写统计、CSV 与实例。写出报告本身不代表成功。"""
    s = load_settings(dict(os.environ))
    checked = upstream.get("check_inputs")
    verified = upstream.get("verify")
    eid = (checked or {}).get("extraction_id") or params.extraction_id
    cv = next((u["conflict_version_id"] for u in (verified, upstream.get("finalize_version")) if u and u.get("conflict_version_id")), None)
    failures = read_failures(s.output_dir, params.run_key)
    failed_tasks = {f.get("task") for f in failures}
    tasks = {n: ("success" if upstream.get(n) is not None else ("failed" if n in failed_tasks else "not_run"))
             for n in CONFLICT_STEP_NAMES}
    summary: dict[str, Any] = {
        "summary_version": CONFLICT_SUMMARY_VERSION, "generated_at": reporting.utc_now(), "dag_id": params.dag_id,
        "run_key": params.run_key, "extraction_id": eid, "rule_version": params.rule_version,
        "params": {k: v for k, v in dataclasses.asdict(params).items() if k not in ("run_key", "try_number")},
        "tasks": tasks, "failures": failures,
        "stage_runs": {n: upstream.get(n) for n in STEP_STAGE}, "conflict_version_id": cv,
    }
    ok = verified is not None and cv is not None
    directory = report.run_dir(s.output_dir, eid, cv, params.run_key) if (eid and cv and ok) else failures_dir(s.output_dir, params.run_key)
    if ok:
        with connect(s) as conn:
            summary["stats"] = stats = report.build_summary(conn, cv, run_id=verified["run_id"])
            summary["examples"] = ex = report.examples(conn, cv)
            summary["exports"] = report.export_files(conn, cv, directory, ex, run_id=verified["run_id"], summary=stats)
            summary["examples_file"] = "examples.md"
    summary["database_available"] = _database_available(s)
    final = SUCCEEDED if all(v == "success" for v in tasks.values()) else FAILED
    summary["final_status"] = final
    summary["delivery_complete"] = final == SUCCEEDED
    reporting.write_atomic(directory / "run_summary.json",
                           (json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n").encode())
    if not ok:
        reporting.write_atomic(directory / "report.md", f"# 冲突消解运行未成功\n\nRun Key: {params.run_key}\n".encode())
    return {"final_status": final, "delivery_complete": summary["delivery_complete"], "report_dir": str(directory),
            "extraction_id": eid, "conflict_version_id": cv}
