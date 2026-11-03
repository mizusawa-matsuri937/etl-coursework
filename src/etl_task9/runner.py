"""任务 9 运行编排（spec R36–R40）：输入检查 → 抽取锁 → 复用判断 → 五个阶段 → 物化版本 → 核对 → 成功。写法同任务 8 runner。

- execute_lineage 一次串起全部步骤（测试与 CLI 的 run）；DAG 的七个执行步骤（build_sources … build_impact、
  finalize_version、verify）用 run_stage，共用一条 task9.run（kind = 'lineage'），靠 airflow_run_id 找回。
- 同一输入键已有通过核对的运行时只记一条 reused 审计，不新增也不修改目标行（R37）。
- 阶段结果按阶段键保存：已完成的阶段直接跳过，未完成的从第一个未提交的实体继续（R38）。
- 运行在 verify 通过之后才是 succeeded；核对失败的运行是 failed、版本保持 unverified，不会被复用；同输入重跑时阶段
  结果保留，只在原版本上重写（R40）。
- run_stages（kind = 'stage'）只按依赖链执行前若干阶段，不物化血缘版本，用于测试与业务库副本演练。

命令（项目根目录；演练库见 design“业务库副本演练”）：

    docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler \
        python -m etl_task9.runner stages --through impact
    docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t9_rehearsal crawler python -m etl_task9.runner run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import psycopg

from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, RunInputs, Task9InputError, add_counts, apply_task9_schema,
                  check_inputs, close_abandoned, extraction_lock, fail_run, finish_run, stage_complete, start_run)
from .rules import STAGES

IMPLEMENTED = ("sources", "assertions", "derivations", "selections", "impact")   # T9-01–T9-04


def exec_stage(conn: psycopg.Connection, ctx: RunContext, stage: str, *, batch_size: int = DEFAULT_BATCH,
               snapshot_root: str | Path | None = None, snapshot_workers: int = 8,
               before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """执行一个阶段（调用方已持锁并建好运行）；该阶段键已完成时直接返回 skipped。"""
    if stage_complete(conn, ctx.key(stage)):
        return {"stage_key": ctx.key(stage), "skipped": True}
    if stage == "sources":
        from . import source_records
        return source_records.build_sources(conn, ctx, batch_size=batch_size, snapshot_root=snapshot_root,
                                            snapshot_workers=snapshot_workers, before_commit=before_commit)
    if stage == "assertions":
        from . import assertions
        return assertions.build_assertions(conn, ctx, batch_size=batch_size, before_commit=before_commit)
    if stage == "derivations":
        from . import derivations
        return derivations.build_derivations(conn, ctx, batch_size=batch_size, before_commit=before_commit)
    if stage == "selections":
        from . import selections
        return selections.build_selections(conn, ctx, batch_size=batch_size, before_commit=before_commit)
    if stage == "impact":
        from . import impact
        return impact.build_impact(conn, ctx, batch_size=batch_size, before_commit=before_commit)
    raise NotImplementedError(f"阶段 {stage} 尚未实现（当前只实现 {IMPLEMENTED}）")


def run_stages(conn: psycopg.Connection, inputs: RunInputs, *, through: str = "assertions",
               batch_size: int = DEFAULT_BATCH, snapshot_root: str | Path | None = None, snapshot_workers: int = 8,
               before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """按依赖链执行到 through（含）为止；记一条 kind = 'stage' 的运行。失败时运行记为 failed，已提交的批次保留。"""
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    if through not in STAGES:
        raise ValueError(f"through 只能是 {STAGES}")
    inputs = check_inputs(conn, inputs)
    apply_task9_schema(conn)
    with extraction_lock(conn, inputs.extraction_id):
        close_abandoned(conn, inputs.extraction_id)
        ctx = start_run(conn, inputs, "stage", params={"through": through, "batch_size": batch_size,
                                                       "snapshot_root": str(snapshot_root) if snapshot_root else None,
                                                       "snapshot_workers": snapshot_workers})
        counts: dict[str, Any] = {"run_id": ctx.run_id}
        try:
            for stage in STAGES[:STAGES.index(through) + 1]:
                started = time.monotonic()
                counts[stage] = exec_stage(conn, ctx, stage, batch_size=batch_size, snapshot_root=snapshot_root,
                                           snapshot_workers=snapshot_workers, before_commit=before_commit)
                counts[stage]["elapsed_seconds"] = round(time.monotonic() - started, 2)
                add_counts(conn, ctx.run_id, stage, counts[stage])
        except BaseException as exc:
            try:
                fail_run(conn, ctx.run_id, f"{type(exc).__name__}: {exc}")
            except psycopg.Error:
                pass
            raise
        finish_run(conn, ctx.run_id, "succeeded", {k: v for k, v in counts.items() if k != "run_id"})
        return counts


# ---------------------------------------------------------------- 完整运行（R36–R40）

STEPS = (*STAGES, "finalize_version", "verify")


class VerificationFailed(RuntimeError):
    """核对不通过（确定性错误）：运行记为 failed，版本保持 unverified。"""


@dataclass
class RunResult:
    run_id: str
    status: str                          # succeeded | reused
    lineage_version_id: str | None
    inputs: RunInputs
    counts: dict[str, Any] = field(default_factory=dict)


def reusable_run(conn: psycopg.Connection, inputs: RunInputs) -> tuple[str, str] | None:
    """R37：同一输入键已有成功且版本通过核对的运行时返回 (run_id, 血缘版本号)。"""
    row = conn.execute(
        "SELECT r.run_id, r.lineage_version_id FROM task9.run r JOIN task9.lineage_version v "
        "ON v.lineage_version_id = r.lineage_version_id WHERE r.extraction_id = %s AND r.input_key = %s "
        "AND r.kind = 'lineage' AND r.status = 'succeeded' AND v.status = 'verified' "
        "ORDER BY r.started_at DESC LIMIT 1", (inputs.extraction_id, inputs.key())).fetchone()
    return (row[0], row[1]) if row else None


def _verify(conn: psycopg.Connection, ctx: RunContext, lv: str, counts: dict[str, Any], *,
            inject_failure: bool = False) -> dict[str, Any]:
    """核对版本；通过则标 verified 并把运行记为 succeeded，否则运行记为 failed（版本保持 unverified）。"""
    from . import check, versions
    info = check.verify(conn, lv)
    found = check.problems(info, expect_verified=False)
    if inject_failure:
        found = found + ["注入的核对失败（仅测试）"]
    if found:
        reason = "；".join(found)[:2000]
        fail_run(conn, ctx.run_id, f"verify: {reason}")
        raise VerificationFailed(reason)
    versions.mark_verified(conn, lv, ctx.run_id, {"verify": {"table_counts": info["table_counts"]}})
    return info


def _params(batch_size: int, snapshot_root: Any, snapshot_workers: int) -> dict[str, Any]:
    return {"batch_size": batch_size, "snapshot_root": str(snapshot_root) if snapshot_root else None,
            "snapshot_workers": snapshot_workers}


def execute_lineage(conn: psycopg.Connection, inputs: RunInputs, *, batch_size: int = DEFAULT_BATCH,
                    snapshot_root: str | Path | None = None, snapshot_workers: int = 8, dag_id: str | None = None,
                    airflow_run_id: str | None = None, before_commit: BeforeCommit | None = None,
                    inject_verify_failure: bool = False) -> RunResult:
    """串行执行全部步骤；同一输入键已有通过核对的运行直接复用（reused）。"""
    from . import versions
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    inputs = check_inputs(conn, inputs)
    apply_task9_schema(conn)
    with extraction_lock(conn, inputs.extraction_id):
        close_abandoned(conn, inputs.extraction_id)
        prior = reusable_run(conn, inputs)
        ctx = start_run(conn, inputs, "lineage", params=_params(batch_size, snapshot_root, snapshot_workers),
                        dag_id=dag_id, airflow_run_id=airflow_run_id)
        if prior:
            finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, lineage_version_id=prior[1])
            return RunResult(ctx.run_id, "reused", prior[1], inputs, {"reused_run": prior[0]})
        counts: dict[str, Any] = {}
        try:
            for stage in STAGES:
                started = time.monotonic()
                counts[stage] = exec_stage(conn, ctx, stage, batch_size=batch_size, snapshot_root=snapshot_root,
                                           snapshot_workers=snapshot_workers, before_commit=before_commit)
                counts[stage]["elapsed_seconds"] = round(time.monotonic() - started, 2)
                add_counts(conn, ctx.run_id, stage, counts[stage])
            lv, n = versions.finalize_version(conn, ctx)
            counts["finalize_version"] = n
            add_counts(conn, ctx.run_id, "finalize_version", n)
            _verify(conn, ctx, lv, counts, inject_failure=inject_verify_failure)
        except VerificationFailed:
            raise
        except BaseException as exc:
            try:
                fail_run(conn, ctx.run_id, f"{type(exc).__name__}: {exc}")
            except psycopg.Error:
                pass
            raise
        return RunResult(ctx.run_id, "succeeded", lv, inputs, counts)


# ---------------------------------------------------------------- 分步骤运行（Airflow DAG 步骤）

@dataclass
class StageResult:
    stage: str
    run_id: str
    status: str                          # done | reused
    lineage_version_id: str | None
    counts: dict[str, Any]
    inputs: RunInputs
    elapsed_seconds: float = 0.0


def _find_run(conn: psycopg.Connection, extraction_id: str, airflow_run_id: str):
    return conn.execute("SELECT run_id, status, lineage_version_id, counts, inputs FROM task9.run "
                        "WHERE extraction_id = %s AND kind = 'lineage' AND airflow_run_id = %s "
                        "ORDER BY started_at DESC LIMIT 1", (extraction_id, airflow_run_id)).fetchone()


def run_stage(conn: psycopg.Connection, inputs: RunInputs, stage: str, *, airflow_run_id: str,
              batch_size: int = DEFAULT_BATCH, snapshot_root: str | Path | None = None, snapshot_workers: int = 8,
              dag_id: str | None = None, before_commit: BeforeCommit | None = None,
              inject_verify_failure: bool = False) -> StageResult:
    """DAG 的一个步骤：build_sources → … → build_impact → finalize_version → verify。每个步骤各持一次抽取锁；七个步骤
    共用一条 task9.run，靠 airflow_run_id 找回。步骤失败把运行记为 failed，同一 DAG run 里重试时接着用这条运行，已提交
    的批次不重做。输入键已有通过核对的运行时第一个步骤记 reused，其余步骤跳过。只有 verify 通过才 succeeded。"""
    from . import versions
    if stage not in STEPS:
        raise ValueError(f"未知的步骤 {stage!r}")
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    started = time.monotonic()
    inputs = check_inputs(conn, inputs)
    with extraction_lock(conn, inputs.extraction_id):
        row = _find_run(conn, inputs.extraction_id, airflow_run_id)
        if row is not None and row[1] == "reused":
            return StageResult(stage, row[0], "reused", row[2], dict(row[3]), inputs)
        if row is None:
            if stage != STEPS[0]:
                raise Task9InputError(f"没有找到 DAG run {airflow_run_id} 的血缘运行（{STEPS[0]} 步骤尚未执行）")
            close_abandoned(conn, inputs.extraction_id, keep_airflow_run_id=airflow_run_id)
            prior = reusable_run(conn, inputs)
            ctx = start_run(conn, inputs, "lineage", params=_params(batch_size, snapshot_root, snapshot_workers),
                            dag_id=dag_id, airflow_run_id=airflow_run_id)
            if prior:
                finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, lineage_version_id=prior[1])
                return StageResult(stage, ctx.run_id, "reused", prior[1], {"reused_run": prior[0]}, inputs,
                                   round(time.monotonic() - started, 2))
        else:
            if row[4] != inputs.as_json():
                raise Task9InputError(f"DAG run {airflow_run_id} 的运行输入与本步骤不一致")
            ctx = RunContext(row[0], inputs)
            conn.execute("UPDATE task9.run SET status = 'running', finished_at = NULL, failure_reason = NULL "
                         "WHERE run_id = %s AND status IN ('failed', 'interrupted')", (ctx.run_id,))
        lv: str | None = row[2] if row is not None else None
        try:
            if stage in STAGES:
                counts = exec_stage(conn, ctx, stage, batch_size=batch_size, snapshot_root=snapshot_root,
                                    snapshot_workers=snapshot_workers, before_commit=before_commit)
            elif stage == "finalize_version":
                lv, counts = versions.finalize_version(conn, ctx)
            else:
                lv = lv or conn.execute("SELECT lineage_version_id FROM task9.run WHERE run_id = %s",
                                        (ctx.run_id,)).fetchone()[0]
                if not lv:
                    raise Task9InputError("finalize_version 步骤尚未物化版本，不能核对")
                info = _verify(conn, ctx, lv, {}, inject_failure=inject_verify_failure)
                counts = {"table_counts": info["table_counts"]}
        except VerificationFailed:
            raise
        except BaseException as exc:
            try:
                fail_run(conn, ctx.run_id, f"{stage}: {type(exc).__name__}: {exc}")
            except psycopg.Error:
                pass
            raise
        elapsed = round(time.monotonic() - started, 2)
        counts["elapsed_seconds"] = elapsed
        if stage != "verify":
            add_counts(conn, ctx.run_id, stage, counts)
        return StageResult(stage, ctx.run_id, "done", lv, counts, ctx.inputs, elapsed)


# ---------------------------------------------------------------- 命令行

def _connect() -> psycopg.Connection:
    from etl_task1.config import load_settings
    from etl_task1.db import connect
    return connect(load_settings(os.environ))


def main(argv: list[str] | None = None) -> int:
    d = RunInputs()
    ap = argparse.ArgumentParser(prog="python -m etl_task9.runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stages", help="按依赖链执行前若干阶段（演练与调试，不物化血缘版本）")
    st.add_argument("--through", default="assertions", choices=IMPLEMENTED)
    st.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    st.add_argument("--snapshot-root", default=None, help="快照相对路径的根目录（默认项目根目录 /opt/etl）")
    st.add_argument("--snapshot-workers", type=int, default=8)
    rn = sub.add_parser("run", help="完整运行：五个阶段、物化血缘版本、核对（同输入已核对时 reused）")
    rn.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
    rn.add_argument("--snapshot-root", default=None)
    rn.add_argument("--snapshot-workers", type=int, default=8)
    for name in ("extraction_id", "t3_rule_version", "parser_version", "t4_mapping_version_id", "norm_version",
                 "t5_person_mapping_version_id", "t6_release_version_id", "conflict_version_id",
                 "review_dedup_version_id", "imdb_snapshot_id", "rule_version"):
        st.add_argument("--" + name.replace("_", "-"), default=getattr(d, name))
        rn.add_argument("--" + name.replace("_", "-"), default=getattr(d, name))
    args = ap.parse_args(argv)
    inputs = RunInputs(**{k: getattr(args, k) for k in (
        "extraction_id", "t3_rule_version", "parser_version", "t4_mapping_version_id", "norm_version",
        "t5_person_mapping_version_id", "t6_release_version_id", "conflict_version_id", "review_dedup_version_id",
        "imdb_snapshot_id", "rule_version")})
    with _connect() as conn:
        if args.cmd == "stages":
            out: Any = run_stages(conn, inputs, through=args.through, batch_size=args.batch_size,
                                  snapshot_root=args.snapshot_root, snapshot_workers=args.snapshot_workers)
        else:
            r = execute_lineage(conn, inputs, batch_size=args.batch_size, snapshot_root=args.snapshot_root,
                                snapshot_workers=args.snapshot_workers)
            out = {"run_id": r.run_id, "status": r.status, "lineage_version_id": r.lineage_version_id,
                   "counts": r.counts}
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
