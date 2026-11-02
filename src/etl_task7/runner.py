"""一次冲突消解运行的编排（spec R50–R55）：输入检查 → 抽取锁 → 复用判断 → 四个阶段 → 物化版本 → 核对 → 成功。

execute_conflict 一次串起全部步骤（测试与 CLI）；DAG 的六个执行步骤（edition_resolve、link_assess、movie_scalar、
movie_multivalue、finalize_version、verify）用 run_stage，共用一条 task7.run，靠 airflow_run_id 找回（与任务 4–6 同义）。
与任务 6 的差别（R55）：运行状态在 verify 通过之后才是 succeeded，核对失败的运行是 failed，其版本保持 unverified，
不会被 reused 复用；同输入重跑时阶段结果按阶段键保留，只在单事务里重写那个未核实版本。
同一输入键已有通过核对的运行时只记一条 reused 审计，不新增也不修改目标行（R52）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import psycopg

from . import check, editions, link_assess, movies_multivalue, movies_scalar, versions
from .rules import RuleSet, ruleset
from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, RunInputs, Task7InputError, add_counts, check_inputs,
                  close_abandoned, extraction_lock, finish_run, reusable_run, start_run)

STAGES = ("edition_resolve", "link_assess", "movie_scalar", "movie_multivalue", "finalize_version", "verify")


def _fail(conn: psycopg.Connection, run_id: str, reason: str, cv: str | None = None) -> None:
    """把运行记为 failed，保留已累计的阶段计数（finish_run 会整体替换计数）。"""
    conn.execute("UPDATE task7.run SET status = 'failed', failure_reason = %s, finished_at = clock_timestamp(), "
                 "conflict_version_id = coalesce(%s, conflict_version_id) WHERE run_id = %s", (reason[:2000], cv, run_id))


class VerificationFailed(RuntimeError):
    """核对不通过（确定性错误）：运行记为 failed，版本保持 unverified。"""


@dataclass
class RunResult:
    run_id: str
    status: str                          # succeeded | reused
    conflict_version_id: str | None
    inputs: RunInputs
    counts: dict[str, Any] = field(default_factory=dict)


def _stage_fn(stage: str):
    return {"edition_resolve": editions.resolve_editions, "link_assess": link_assess.assess_links,
            "movie_scalar": movies_scalar.resolve_movies_scalar,
            "movie_multivalue": movies_multivalue.resolve_movies_multivalue}[stage]


def _verify(conn: psycopg.Connection, ctx: RunContext, cv: str, counts: dict[str, Any], *,
            inject_failure: bool = False) -> dict[str, Any]:
    """核对版本；通过则标 verified 并把运行记为 succeeded，否则运行记为 failed（版本保持 unverified）。"""
    info = check.verify(conn, cv)
    found = check.problems(info, expect_verified=False)
    if inject_failure:
        found = found + ["注入的核对失败（仅测试）"]
    if found:
        reason = "；".join(found)[:2000]
        _fail(conn, ctx.run_id, f"verify: {reason}", cv)
        raise VerificationFailed(reason)
    versions.mark_verified(conn, cv, ctx.run_id, {**counts, "verify": {"table_counts": info["table_counts"]}})
    return info


def execute_conflict(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet | None = None, *,
                     batch_size: int = DEFAULT_BATCH, dag_id: str | None = None, airflow_run_id: str | None = None,
                     before_commit: BeforeCommit | None = None, inject_verify_failure: bool = False) -> RunResult:
    """串行执行全部步骤；同一输入键已有通过核对的运行直接复用。"""
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    rules = rules or ruleset(inputs.rule_version)
    if rules.version != inputs.rule_version:
        raise ValueError(f"规则集版本 {rules.version} ≠ 输入里的规则版本 {inputs.rule_version}")
    check_inputs(conn, inputs)
    with extraction_lock(conn, inputs.extraction_id):
        close_abandoned(conn, inputs.extraction_id)
        prior = reusable_run(conn, inputs)
        ctx = start_run(conn, inputs, "resolve", params={"batch_size": batch_size}, dag_id=dag_id,
                        airflow_run_id=airflow_run_id)
        if prior:
            finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, conflict_version_id=prior[1])
            return RunResult(ctx.run_id, "reused", prior[1], inputs, {"reused_run": prior[0]})
        counts: dict[str, Any] = {}
        try:
            for stage in ("edition_resolve", "link_assess", "movie_scalar", "movie_multivalue"):
                counts[stage] = _stage_fn(stage)(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
            cv, n = versions.finalize_version(conn, ctx)
            counts["finalize_version"] = n
            _verify(conn, ctx, cv, counts, inject_failure=inject_verify_failure)
        except VerificationFailed:
            raise
        except BaseException as exc:
            try:
                for stage_name, value in counts.items():      # 失败前已完成的阶段计数保留在审计里
                    add_counts(conn, ctx.run_id, stage_name, value)
                _fail(conn, ctx.run_id, f"{type(exc).__name__}: {exc}")
            except psycopg.Error:
                pass
            raise
        return RunResult(ctx.run_id, "succeeded", cv, inputs, counts)


# ---------------------------------------------------------------- 分步骤运行（Airflow DAG 步骤）

@dataclass
class StageResult:
    stage: str
    run_id: str
    status: str                          # done | reused
    conflict_version_id: str | None
    counts: dict[str, Any]
    inputs: RunInputs
    elapsed_seconds: float = 0.0


_RUN_COLUMNS = ("run_id, status, extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, "
                "t6_release_version_id, parser_version, norm_version, rule_version, imdb_snapshot_id, "
                "conflict_version_id, counts")


def _find_run(conn: psycopg.Connection, extraction_id: str, airflow_run_id: str):
    """同一个 DAG run 的冲突消解运行：步骤之间、以及步骤重试时靠 airflow_run_id 找回同一条运行。"""
    return conn.execute(f"SELECT {_RUN_COLUMNS} FROM task7.run WHERE extraction_id = %s AND kind = 'resolve' "
                        "AND airflow_run_id = %s ORDER BY started_at DESC LIMIT 1",
                        (extraction_id, airflow_run_id)).fetchone()


def _inputs_of_row(row) -> RunInputs:
    return RunInputs(extraction_id=row[2], t4_mapping_version_id=row[3], t5_person_mapping_version_id=row[4],
                     t6_release_version_id=row[5], parser_version=row[6], norm_version=row[7], rule_version=row[8],
                     imdb_snapshot_id=row[9])


def run_stage(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet | None, stage: str, *, airflow_run_id: str,
              batch_size: int = DEFAULT_BATCH, dag_id: str | None = None, before_commit: BeforeCommit | None = None,
              inject_verify_failure: bool = False) -> StageResult:
    """DAG 的一个步骤：edition_resolve → link_assess → movie_scalar → movie_multivalue → finalize_version → verify。
    每个步骤各持一次抽取锁；六个步骤共用一条 task7.run，靠 airflow_run_id 找回。步骤失败把运行记为 failed，
    同一 DAG run 里重试时接着用这条运行，已提交的批次不重做。输入键已有通过核对的运行时 edition_resolve 记 reused，其余步骤跳过。
    只有 verify 通过才把运行记为 succeeded。"""
    if stage not in STAGES:
        raise ValueError(f"未知的步骤 {stage!r}")
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    rules = rules or ruleset(inputs.rule_version)
    if rules.version != inputs.rule_version:
        raise ValueError(f"规则集版本 {rules.version} ≠ 输入里的规则版本 {inputs.rule_version}")
    started = time.monotonic()
    with extraction_lock(conn, inputs.extraction_id):
        row = _find_run(conn, inputs.extraction_id, airflow_run_id)
        if row is not None and row[1] == "reused":
            return StageResult(stage, row[0], "reused", row[10], dict(row[11]), _inputs_of_row(row))
        if stage == "edition_resolve":
            check_inputs(conn, inputs)
            if row is None:
                close_abandoned(conn, inputs.extraction_id, keep_airflow_run_id=airflow_run_id)
                prior = reusable_run(conn, inputs)
                ctx = start_run(conn, inputs, "resolve", params={"batch_size": batch_size}, dag_id=dag_id,
                                airflow_run_id=airflow_run_id)
                if prior:
                    finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, conflict_version_id=prior[1])
                    return StageResult(stage, ctx.run_id, "reused", prior[1], {"reused_run": prior[0]}, inputs,
                                       round(time.monotonic() - started, 2))
            else:
                ctx = RunContext(row[0], _inputs_of_row(row))
        else:
            if row is None:
                raise Task7InputError(f"没有找到 DAG run {airflow_run_id} 的冲突消解运行（edition_resolve 步骤尚未执行）")
            ctx = RunContext(row[0], _inputs_of_row(row))
        inputs = ctx.inputs
        if row is not None:      # 重试：把上次失败的运行接着用
            conn.execute("UPDATE task7.run SET status = 'running', finished_at = NULL, failure_reason = NULL "
                         "WHERE run_id = %s AND status IN ('failed', 'interrupted')", (ctx.run_id,))
        cv: str | None = row[10] if row is not None else None
        try:
            if stage in ("edition_resolve", "link_assess", "movie_scalar", "movie_multivalue"):
                counts = _stage_fn(stage)(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
            elif stage == "finalize_version":
                cv, counts = versions.finalize_version(conn, ctx)
            else:
                cv = cv or conn.execute("SELECT conflict_version_id FROM task7.run WHERE run_id = %s",
                                        (ctx.run_id,)).fetchone()[0]
                if not cv:
                    raise Task7InputError("finalize_version 步骤尚未物化版本，不能核对")
                merged = dict(conn.execute("SELECT counts FROM task7.run WHERE run_id = %s", (ctx.run_id,)).fetchone()[0])
                info = _verify(conn, ctx, cv, merged, inject_failure=inject_verify_failure)
                counts = {"table_counts": info["table_counts"]}
        except VerificationFailed:
            raise
        except BaseException as exc:
            try:
                _fail(conn, ctx.run_id, f"{stage}: {type(exc).__name__}: {exc}", cv)
            except psycopg.Error:
                pass
            raise
        elapsed = round(time.monotonic() - started, 2)
        counts["elapsed_seconds"] = elapsed
        if stage != "verify":
            add_counts(conn, ctx.run_id, stage, counts)
        return StageResult(stage, ctx.run_id, "done", cv, counts, inputs, elapsed)
