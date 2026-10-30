"""一次上映信息运行的编排（spec R27–R30）：输入检查 → 抽取锁 → 复用判断 → 候选 → 共现反查 → 商品日期 → 评论时间与提及 → 择优 → 上界 → 审计。

execute_release 一次串起全部阶段；DAG 的六个执行步骤（candidates、cooccur、product_dates、review_times、select、bounds）用 run_stage，共用一条 task6.run，靠 airflow_run_id 找回（与任务 4、5 的写法同义）。
同一输入键已有成功运行时只记一条 reused 审计，不新增也不修改目标行；中断后重跑只补没有标记的批次。
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import bounds, candidates, cooccur, product_dates, review_years, reviews, select
from .rules import RuleSet, ruleset
from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, RunInputs, Task6InputError, check_inputs, close_abandoned,
                  extraction_lock, finish_run, reusable_run, start_run)


@dataclass
class RunResult:
    run_id: str
    status: str                          # succeeded | reused
    release_version_id: str | None
    inputs: RunInputs
    counts: dict[str, Any] = field(default_factory=dict)


def execute_release(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet | None = None, *,
                    batch_size: int = DEFAULT_BATCH, limit: int | None = None, dag_id: str | None = None,
                    airflow_run_id: str | None = None, before_commit: BeforeCommit | None = None) -> RunResult:
    """串行执行全部步骤（测试与 CLI 使用）；同一输入键已有成功运行直接复用。"""
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    rules = rules or ruleset(inputs.rule_version)
    if rules.version != inputs.rule_version:
        raise ValueError(f"规则集版本 {rules.version} ≠ 输入里的规则版本 {inputs.rule_version}")
    check_inputs(conn, inputs)
    with extraction_lock(conn, inputs.extraction_id):
        close_abandoned(conn, inputs.extraction_id)
        prior = reusable_run(conn, inputs)
        ctx = start_run(conn, inputs, "release", params={"batch_size": batch_size, "limit": limit}, dag_id=dag_id,
                        airflow_run_id=airflow_run_id)
        if prior:
            finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, release_version_id=prior[1])
            return RunResult(ctx.run_id, "reused", prior[1], inputs, {"reused_run": prior[0]})
        try:
            counts: dict[str, Any] = {}
            counts["candidates"] = candidates.extract_candidates(
                conn, ctx, rules, batch_size=batch_size, limit=limit, before_commit=before_commit)
            counts["cooccur"] = cooccur.score_movies(
                conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
            counts["product_dates"] = product_dates.extract_product_dates(
                conn, ctx, batch_size=batch_size, before_commit=before_commit)
            p_rev = reviews.product_review_times(conn, ctx, batch_size=batch_size, before_commit=before_commit)
            m_rev = reviews.movie_review_times(conn, ctx)
            r_mentions = review_years.extract_mentions(
                conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
            counts["review_times"] = {"product_reviews": p_rev, "movie_reviews": m_rev, "mentions": r_mentions}
            rv = select.build_selection(conn, ctx, rules)
            counts["select"] = {"release_version_id": rv}
            counts["bounds"] = bounds.build_bounds(conn, ctx, rules, before_commit=before_commit)
        except BaseException as exc:
            try:
                finish_run(conn, ctx.run_id, "failed", failure_reason=f"{type(exc).__name__}: {exc}"[:2000])
            except psycopg.Error:
                pass
            raise
        finish_run(conn, ctx.run_id, "succeeded", counts, release_version_id=rv)
        return RunResult(ctx.run_id, "succeeded", rv, inputs, counts)


# ---------------------------------------------------------------- 分步骤运行（Airflow DAG 步骤）

STAGES = ("candidates", "cooccur", "product_dates", "review_times", "select", "bounds")


@dataclass
class StageResult:
    stage: str
    run_id: str
    status: str                          # done | reused
    release_version_id: str | None
    counts: dict[str, Any]
    inputs: RunInputs
    elapsed_seconds: float = 0.0


_RUN_COLUMNS = ("run_id, status, extraction_id, t4_mapping_version_id, t5_person_mapping_version_id, "
                "parser_version, rule_version, imdb_snapshot_id, release_version_id, counts")


def _find_run(conn: psycopg.Connection, extraction_id: str, airflow_run_id: str):
    """同一个 DAG run 的上映信息运行：步骤之间、以及步骤重试时靠 airflow_run_id 找回同一条运行。"""
    return conn.execute(f"SELECT {_RUN_COLUMNS} FROM task6.run WHERE extraction_id = %s AND kind = 'release' "
                        "AND airflow_run_id = %s ORDER BY started_at DESC LIMIT 1",
                        (extraction_id, airflow_run_id)).fetchone()


def _inputs_of_row(row) -> RunInputs:
    return RunInputs(extraction_id=row[2], t4_mapping_version_id=row[3], t5_person_mapping_version_id=row[4],
                     parser_version=row[5], rule_version=row[6], imdb_snapshot_id=row[7])


def _add_counts(conn: psycopg.Connection, run_id: str, key: str, value: Any) -> None:
    conn.execute("UPDATE task6.run SET counts = counts || jsonb_build_object(%s::text, %s::jsonb) WHERE run_id = %s",
                 (key, Jsonb(value), run_id))


def run_stage(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet | None, stage: str, *, airflow_run_id: str,
              batch_size: int = DEFAULT_BATCH, limit: int | None = None, dag_id: str | None = None,
              before_commit: BeforeCommit | None = None) -> StageResult:
    """DAG 的一个步骤：candidates → cooccur → product_dates → review_times → select → bounds。
    每个步骤各持一次抽取锁；六个步骤共用一条 task6.run，靠 airflow_run_id 找回。
    步骤失败会把运行记为 failed，同一 DAG run 里重试时接着用这条运行，已提交的批次不重做。
    输入键已有成功运行时，candidates 记 reused，其余步骤跳过。
    limit 只限制候选阶段的作品数，之后的步骤因仍有未处理作品而确定性失败。"""
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
            return StageResult(stage, row[0], "reused", row[8], dict(row[9]), _inputs_of_row(row))
        if stage == "candidates":
            check_inputs(conn, inputs)
            if row is None:
                close_abandoned(conn, inputs.extraction_id, keep_airflow_run_id=airflow_run_id)
                prior = reusable_run(conn, inputs)
                ctx = start_run(conn, inputs, "release", params={"batch_size": batch_size, "limit": limit},
                                dag_id=dag_id, airflow_run_id=airflow_run_id)
                if prior:
                    finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, release_version_id=prior[1])
                    return StageResult(stage, ctx.run_id, "reused", prior[1], {"reused_run": prior[0]}, inputs,
                                       round(time.monotonic() - started, 2))
            else:
                ctx = RunContext(row[0], _inputs_of_row(row))
        else:
            if row is None:
                raise Task6InputError(f"没有找到 DAG run {airflow_run_id} 的上映信息运行（candidates 步骤尚未执行）")
            ctx = RunContext(row[0], _inputs_of_row(row))
        inputs = ctx.inputs
        if row is not None:      # 重试：把上次失败的运行接着用
            conn.execute("UPDATE task6.run SET status = 'running', finished_at = NULL, failure_reason = NULL "
                         "WHERE run_id = %s AND status IN ('failed', 'interrupted')", (ctx.run_id,))
        try:
            counts, rv = _do_stage(conn, ctx, rules, stage, batch_size, limit, before_commit,
                                   row[8] if row is not None else None)
        except BaseException as exc:
            try:
                finish_run(conn, ctx.run_id, "failed", failure_reason=f"{stage}: {type(exc).__name__}: {exc}"[:2000])
            except psycopg.Error:
                pass
            raise
        elapsed = round(time.monotonic() - started, 2)
        counts["elapsed_seconds"] = elapsed
        if stage == "bounds":
            merged = dict(conn.execute("SELECT counts FROM task6.run WHERE run_id = %s", (ctx.run_id,)).fetchone()[0])
            merged[stage] = counts
            finish_run(conn, ctx.run_id, "succeeded", merged, release_version_id=rv)
        else:
            _add_counts(conn, ctx.run_id, stage, counts)
            if rv:
                conn.execute("UPDATE task6.run SET release_version_id = %s WHERE run_id = %s", (rv, ctx.run_id))
        return StageResult(stage, ctx.run_id, "done", rv, counts, inputs, elapsed)


def _do_stage(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, stage: str, batch_size: int,
              limit: int | None, before_commit: BeforeCommit | None,
              run_rv: str | None) -> tuple[dict[str, Any], str | None]:
    if stage == "candidates":
        c = candidates.extract_candidates(conn, ctx, rules, batch_size=batch_size, limit=limit,
                                          before_commit=before_commit)
        return c, None
    if stage == "cooccur":
        c = cooccur.score_movies(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
        return c, None
    if stage == "product_dates":
        c = product_dates.extract_product_dates(conn, ctx, batch_size=batch_size, before_commit=before_commit)
        return c, None
    if stage == "review_times":
        p_rev = reviews.product_review_times(conn, ctx, batch_size=batch_size, before_commit=before_commit)
        m_rev = reviews.movie_review_times(conn, ctx)
        r_mentions = review_years.extract_mentions(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
        return {"product_reviews": p_rev, "movie_reviews": m_rev, "mentions": r_mentions}, None
    if stage == "select":
        rv = select.build_selection(conn, ctx, rules)
        return {"release_version_id": rv}, rv
    if not run_rv:
        raise Task6InputError("择优步骤尚未生成上映信息版本，不能评估推断上界")
    b_counts = bounds.build_bounds(conn, ctx, rules, before_commit=before_commit)
    return b_counts, run_rv
