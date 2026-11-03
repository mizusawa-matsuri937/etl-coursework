"""任务 8 运行编排（spec R33–R38）：输入检查 → 抽取锁 → 复用判断 → 四个阶段 → 物化版本 → 核对 → 成功。

- execute_dedup 一次串起全部步骤（测试与 CLI 的 run）；DAG 的六个执行步骤（review_clean、review_family、review_dedup、
  review_attribution、finalize_version、verify）用 run_stage，共用一条 task8.run，靠 airflow_run_id 找回（与任务 7 同义）。
- 同一输入键已有通过核对的运行时只记一条 reused 审计，不新增也不修改目标行（R35）。
- 阶段结果按阶段键保存：已完成的阶段直接跳过，未完成的从第一个未提交的批次单元继续（R34、R36）；只改 cv 版本或
  归属口径时只有 review_attribution 的阶段键变化，前三个阶段原样复用。
- 运行状态在 verify 通过之后才是 succeeded；核对失败的运行是 failed，其版本保持 unverified，不会被 reused 复用；
  同输入重跑时阶段结果保留，只在单事务里重写那个未核实版本（R38）。
- run_stages（kind = 'stage'）只按依赖链执行前若干阶段，不物化版本，用于测试与业务库副本演练。

命令（项目根目录；演练库见 design“业务库副本演练”）：

    docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t8_rehearsal crawler \
        python -m etl_task8.runner stages --through review_attribution
    docker compose --profile crawl run --rm -T -e ETL_DB_NAME=etl_t8_rehearsal crawler python -m etl_task8.runner run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg

from . import check, versions
from .rules import RuleSet, ruleset
from .run import (DEFAULT_BATCH, DEFAULT_PARTITIONS, STAGES, BeforeCommit, RunContext, RunInputs, Task8InputError,
                  add_counts, apply_task8_schema, check_attribution_inputs, check_extraction, check_inputs,
                  close_abandoned, extraction_lock, fail_run, finish_run, start_run)

STAGE_ORDER = STAGES
STEPS = (*STAGES, "finalize_version", "verify")


class VerificationFailed(RuntimeError):
    """核对不通过（确定性错误）：运行记为 failed，版本保持 unverified。"""


def exec_stage(conn: psycopg.Connection, ctx: RunContext, stage: str, *, batch_size: int = DEFAULT_BATCH,
               partitions: int = DEFAULT_PARTITIONS, before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """执行一个阶段（调用方已持锁并建好运行）；返回该阶段的计数。"""
    if stage == "review_clean":
        from . import clean
        return clean.clean_reviews(conn, ctx, batch_size=batch_size, before_commit=before_commit)
    if stage == "review_family":
        from . import family
        return family.build_families(conn, ctx, partitions=partitions, before_commit=before_commit)
    if stage == "review_dedup":
        from . import dedup
        return dedup.dedup_reviews(conn, ctx, partitions=partitions, before_commit=before_commit)
    if stage == "review_attribution":
        from . import attribution
        return attribution.attribute_reviews(conn, ctx, before_commit=before_commit)
    raise ValueError(f"未知的阶段 {stage!r}")


def _rules_for(inputs: RunInputs, rules: RuleSet | None) -> RuleSet:
    rules = rules or ruleset(inputs.rule_version)
    if rules.version != inputs.rule_version:
        raise ValueError(f"规则集版本 {rules.version} ≠ 输入里的规则版本 {inputs.rule_version}")
    return rules


def run_stages(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet | None = None, *,
               through: str = "review_dedup", batch_size: int = DEFAULT_BATCH, partitions: int = DEFAULT_PARTITIONS,
               before_commit: BeforeCommit | None = None) -> dict[str, Any]:
    """按依赖链执行到 through（含）为止；记一条 kind = 'stage' 的运行。失败时运行记为 failed，已提交的批次保留。
    through 为 review_attribution 时另检查任务 3 与任务 7 输入（check_attribution_inputs）。"""
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    if through not in STAGE_ORDER:
        raise ValueError(f"through 只能是 {STAGE_ORDER}")
    rules = _rules_for(inputs, rules)
    source = check_extraction(conn, inputs)
    if through == "review_attribution":
        inputs = check_attribution_inputs(conn, inputs)
    apply_task8_schema(conn)
    with extraction_lock(conn, inputs.extraction_id):
        close_abandoned(conn, inputs.extraction_id)
        ctx = start_run(conn, inputs, rules, "stage",
                        params={"through": through, "batch_size": batch_size, "partitions": partitions})
        counts: dict[str, Any] = {"run_id": ctx.run_id, "source": source}
        try:
            for stage in STAGE_ORDER[:STAGE_ORDER.index(through) + 1]:
                started = time.monotonic()
                counts[stage] = exec_stage(conn, ctx, stage, batch_size=batch_size, partitions=partitions,
                                           before_commit=before_commit)
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


# ---------------------------------------------------------------- 完整运行（R33–R38）

@dataclass
class RunResult:
    run_id: str
    status: str                          # succeeded | reused
    review_dedup_version_id: str | None
    inputs: RunInputs
    counts: dict[str, Any] = field(default_factory=dict)


def reusable_run(conn: psycopg.Connection, inputs: RunInputs) -> tuple[str, str] | None:
    """R35：同一输入键已有成功且版本通过核对（verified）的运行时返回 (run_id, 去重版本号)。"""
    row = conn.execute(
        "SELECT r.run_id, r.review_dedup_version_id FROM task8.run r JOIN task8.dedup_version v "
        "ON v.review_dedup_version_id = r.review_dedup_version_id WHERE r.extraction_id = %s AND r.input_key = %s "
        "AND r.kind = 'dedup' AND r.status = 'succeeded' AND v.status = 'verified' "
        "ORDER BY r.started_at DESC LIMIT 1", (inputs.extraction_id, inputs.key())).fetchone()
    return (row[0], row[1]) if row else None


def _verify(conn: psycopg.Connection, ctx: RunContext, rd: str, counts: dict[str, Any], *,
            inject_failure: bool = False) -> dict[str, Any]:
    """核对版本；通过则标 verified 并把运行记为 succeeded，否则运行记为 failed（版本保持 unverified）。"""
    info = check.verify(conn, rd, ctx.rules)
    found = check.problems(info, expect_verified=False)
    if inject_failure:
        found = found + ["注入的核对失败（仅测试）"]
    if found:
        reason = "；".join(found)[:2000]
        fail_run(conn, ctx.run_id, f"verify: {reason}")
        raise VerificationFailed(reason)
    versions.mark_verified(conn, rd, ctx.run_id, {**counts, "verify": {"table_counts": info["table_counts"]}})
    return info


def execute_dedup(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet | None = None, *,
                  batch_size: int = DEFAULT_BATCH, partitions: int = DEFAULT_PARTITIONS, dag_id: str | None = None,
                  airflow_run_id: str | None = None, before_commit: BeforeCommit | None = None,
                  inject_verify_failure: bool = False) -> RunResult:
    """串行执行全部步骤；同一输入键已有通过核对的运行直接复用。"""
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    rules = _rules_for(inputs, rules)
    inputs, source = check_inputs(conn, inputs)
    apply_task8_schema(conn)
    with extraction_lock(conn, inputs.extraction_id):
        close_abandoned(conn, inputs.extraction_id)
        prior = reusable_run(conn, inputs)
        ctx = start_run(conn, inputs, rules, "dedup", params={"batch_size": batch_size, "partitions": partitions},
                        dag_id=dag_id, airflow_run_id=airflow_run_id)
        if prior:
            finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, review_dedup_version_id=prior[1])
            return RunResult(ctx.run_id, "reused", prior[1], inputs, {"reused_run": prior[0]})
        counts: dict[str, Any] = {"source": source}
        try:
            for stage in STAGES:
                started = time.monotonic()
                counts[stage] = exec_stage(conn, ctx, stage, batch_size=batch_size, partitions=partitions,
                                           before_commit=before_commit)
                counts[stage]["elapsed_seconds"] = round(time.monotonic() - started, 2)
                add_counts(conn, ctx.run_id, stage, counts[stage])
            rd, n = versions.finalize_version(conn, ctx)
            counts["finalize_version"] = n
            _verify(conn, ctx, rd, counts, inject_failure=inject_verify_failure)
        except VerificationFailed:
            raise
        except BaseException as exc:
            try:
                fail_run(conn, ctx.run_id, f"{type(exc).__name__}: {exc}")
            except psycopg.Error:
                pass
            raise
        return RunResult(ctx.run_id, "succeeded", rd, inputs, counts)


# ---------------------------------------------------------------- 分步骤运行（Airflow DAG 步骤）

@dataclass
class StageResult:
    stage: str
    run_id: str
    status: str                          # done | reused
    review_dedup_version_id: str | None
    counts: dict[str, Any]
    inputs: RunInputs
    elapsed_seconds: float = 0.0


_RUN_COLUMNS = ("run_id, status, extraction_id, t3_rule_version, conflict_version_id, rule_version, "
                "review_dedup_version_id, counts, params")


def _find_run(conn: psycopg.Connection, extraction_id: str, airflow_run_id: str):
    """同一个 DAG run 的去重运行：步骤之间、以及步骤重试时靠 airflow_run_id 找回同一条运行。"""
    return conn.execute(f"SELECT {_RUN_COLUMNS} FROM task8.run WHERE extraction_id = %s AND kind = 'dedup' "
                        "AND airflow_run_id = %s ORDER BY started_at DESC LIMIT 1",
                        (extraction_id, airflow_run_id)).fetchone()


def _inputs_of_row(row) -> RunInputs:
    return RunInputs(extraction_id=row[2], t3_rule_version=row[3], conflict_version_id=row[4], rule_version=row[5])


def run_stage(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet | None, stage: str, *, airflow_run_id: str,
              batch_size: int = DEFAULT_BATCH, partitions: int = DEFAULT_PARTITIONS, dag_id: str | None = None,
              before_commit: BeforeCommit | None = None, inject_verify_failure: bool = False) -> StageResult:
    """DAG 的一个步骤：review_clean → review_family → review_dedup → review_attribution → finalize_version → verify。
    每个步骤各持一次抽取锁；六个步骤共用一条 task8.run，靠 airflow_run_id 找回。步骤失败把运行记为 failed，
    同一 DAG run 里重试时接着用这条运行，已提交的批次不重做。输入键已有通过核对的运行时 review_clean 记 reused，其余步骤跳过。
    只有 verify 通过才把运行记为 succeeded。"""
    if stage not in STEPS:
        raise ValueError(f"未知的步骤 {stage!r}")
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    rules = _rules_for(inputs, rules)
    started = time.monotonic()
    with extraction_lock(conn, inputs.extraction_id):
        row = _find_run(conn, inputs.extraction_id, airflow_run_id)
        if row is not None and row[1] == "reused":
            return StageResult(stage, row[0], "reused", row[6], dict(row[7]), _inputs_of_row(row))
        if stage == "review_clean":
            inputs, _ = check_inputs(conn, inputs)
            if row is None:
                close_abandoned(conn, inputs.extraction_id, keep_airflow_run_id=airflow_run_id)
                prior = reusable_run(conn, inputs)
                ctx = start_run(conn, inputs, rules, "dedup", params={"batch_size": batch_size, "partitions": partitions},
                                dag_id=dag_id, airflow_run_id=airflow_run_id)
                if prior:
                    finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, review_dedup_version_id=prior[1])
                    return StageResult(stage, ctx.run_id, "reused", prior[1], {"reused_run": prior[0]}, inputs,
                                       round(time.monotonic() - started, 2))
            else:
                ctx = RunContext(row[0], inputs, rules)
        else:
            if row is None:
                raise Task8InputError(f"没有找到 DAG run {airflow_run_id} 的去重运行（review_clean 步骤尚未执行）")
            ctx = RunContext(row[0], check_attribution_inputs(conn, _inputs_of_row(row)), rules)
        if row is not None:      # 重试或后续步骤：把上次失败的运行接着用
            conn.execute("UPDATE task8.run SET status = 'running', finished_at = NULL, failure_reason = NULL "
                         "WHERE run_id = %s AND status IN ('failed', 'interrupted')", (ctx.run_id,))
        rd: str | None = row[6] if row is not None else None
        try:
            if stage in STAGES:
                counts = exec_stage(conn, ctx, stage, batch_size=batch_size, partitions=partitions,
                                    before_commit=before_commit)
            elif stage == "finalize_version":
                rd, counts = versions.finalize_version(conn, ctx)
            else:
                rd = rd or conn.execute("SELECT review_dedup_version_id FROM task8.run WHERE run_id = %s",
                                        (ctx.run_id,)).fetchone()[0]
                if not rd:
                    raise Task8InputError("finalize_version 步骤尚未物化版本，不能核对")
                merged = dict(conn.execute("SELECT counts FROM task8.run WHERE run_id = %s", (ctx.run_id,)).fetchone()[0])
                info = _verify(conn, ctx, rd, merged, inject_failure=inject_verify_failure)
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
        return StageResult(stage, ctx.run_id, "done", rd, counts, ctx.inputs, elapsed)


# ---------------------------------------------------------------- 命令行

def _connect() -> psycopg.Connection:
    from etl_task1.config import load_settings
    from etl_task1.db import connect
    return connect(load_settings(os.environ))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m etl_task8.runner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stages", help="按依赖链执行前若干阶段（演练与调试，不物化版本）")
    st.add_argument("--through", default="review_dedup", choices=STAGE_ORDER)
    rn = sub.add_parser("run", help="完整运行：四个阶段、物化版本、核对（同输入已核对时 reused）")
    for p in (st, rn):
        p.add_argument("--extraction-id", default=RunInputs().extraction_id)
        p.add_argument("--rule-version", default=RunInputs().rule_version)
        p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH)
        p.add_argument("--partitions", type=int, default=DEFAULT_PARTITIONS)
    rn.add_argument("--t3-rule-version", default=RunInputs().t3_rule_version)
    rn.add_argument("--conflict-version-id", default=RunInputs().conflict_version_id)
    args = ap.parse_args(argv)
    with _connect() as conn:
        if args.cmd == "stages":
            inputs = RunInputs(extraction_id=args.extraction_id, rule_version=args.rule_version)
            out: Any = run_stages(conn, inputs, through=args.through, batch_size=args.batch_size,
                                  partitions=args.partitions)
        else:
            inputs = RunInputs(extraction_id=args.extraction_id, rule_version=args.rule_version,
                               t3_rule_version=args.t3_rule_version, conflict_version_id=args.conflict_version_id)
            r = execute_dedup(conn, inputs, batch_size=args.batch_size, partitions=args.partitions)
            out = {"run_id": r.run_id, "status": r.status, "review_dedup_version_id": r.review_dedup_version_id,
                   "counts": r.counts}
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
