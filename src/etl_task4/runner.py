"""一次匹配运行的编排（spec R38、R39）：输入检查 → 抽取锁 → 复用判断 → 各阶段 → 聚类与映射 → 运行审计。

阶段与恢复：商品版本（一条语句）→ 标准化（分批）→ IMDb 标题键（分批）→ 候选与判定（分批）→ 商品对判定（分批）→ 聚类与映射（一个事务）。
同一输入键已有成功运行时只记一条 reused 审计，不新增也不修改目标行；中断后重跑只补没有标记的批次。
DAG 的三个步骤（normalize、decide、cluster）用下面的 run_stage，共用一条运行记录，见 pipeline.py。
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import candidates, cluster, editions
from .match import DEFAULT_RULES, RuleSet
from .run import (DEFAULT_BATCH, BeforeCommit, RunBusy, RunContext, RunInputs, Task4InputError, check_inputs,
                  close_abandoned, extraction_lock, finish_run, human_set_hash, reusable_run, start_run)


@dataclass
class RunResult:
    run_id: str
    status: str                          # succeeded | reused
    mapping_version_id: str | None
    inputs: RunInputs
    counts: dict[str, Any] = field(default_factory=dict)


def execute_match(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet = DEFAULT_RULES, *,
                  batch_size: int = DEFAULT_BATCH, dag_id: str | None = None, airflow_run_id: str | None = None,
                  before_commit: BeforeCommit | None = None) -> RunResult:
    """一次完整匹配运行。人工决策集的哈希以数据库里当前的为准，所以导入决策后再运行就是新的输入键。"""
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    if rules.version != inputs.rule_version:
        raise ValueError(f"规则集版本 {rules.version} ≠ 输入里的规则版本 {inputs.rule_version}")
    check_inputs(conn, inputs)
    with extraction_lock(conn, inputs.extraction_id):
        inputs = dataclasses.replace(inputs, human_set_hash=human_set_hash(conn, inputs.extraction_id))
        close_abandoned(conn, inputs.extraction_id)
        params = {"batch_size": batch_size}
        prior = reusable_run(conn, inputs)
        ctx = start_run(conn, inputs, "match", params=params, dag_id=dag_id, airflow_run_id=airflow_run_id)
        if prior:
            finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, mapping_version_id=prior[1])
            return RunResult(ctx.run_id, "reused", prior[1], inputs, {"reused_run": prior[0]})
        try:
            counts: dict[str, Any] = {"product_editions": editions.build_editions(conn, ctx)}
            n = editions.normalize_extraction(conn, ctx, batch_size=batch_size, before_commit=before_commit)
            counts["normalize"] = dataclasses.asdict(n)
            k = candidates.build_imdb_keys(conn, ctx, before_commit=before_commit)
            counts["imdb_keys"] = dataclasses.asdict(k)
            d = candidates.decide_entities(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
            counts["decide"] = dataclasses.asdict(d)
            p = candidates.decide_pairs(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
            counts["decide_pair"] = dataclasses.asdict(p)
            mapping = cluster.build_mapping(conn, ctx, rules)
            counts["mapping"] = {"mapping_version_id": mapping.mapping_version_id, **mapping.counts,
                                 "events": mapping.events}
        except BaseException as exc:
            try:
                finish_run(conn, ctx.run_id, "failed", failure_reason=f"{type(exc).__name__}: {exc}"[:2000])
            except psycopg.Error:
                pass
            raise
        finish_run(conn, ctx.run_id, "succeeded", counts, mapping_version_id=mapping.mapping_version_id)
        return RunResult(ctx.run_id, "succeeded", mapping.mapping_version_id, inputs, counts)


# ---------------------------------------------------------------- 分步骤运行（Airflow DAG 的三个步骤）

STAGES = ("normalize", "decide", "cluster")


@dataclass
class StageResult:
    stage: str
    run_id: str
    status: str                          # done | reused（输入键已有成功运行，本阶段什么都不做）
    mapping_version_id: str | None
    counts: dict[str, Any]
    inputs: RunInputs
    elapsed_seconds: float = 0.0


_RUN_COLUMNS = ("run_id, status, extraction_id, t3_rule_version, parser_version, norm_version, rule_version, "
                "imdb_snapshot_id, human_set_hash, mapping_version_id, counts")


def _find_run(conn: psycopg.Connection, extraction_id: str, airflow_run_id: str):
    """同一个 DAG run 的匹配运行：步骤之间、以及步骤重试时靠 airflow_run_id 找回同一条运行。"""
    return conn.execute(f"SELECT {_RUN_COLUMNS} FROM task4.run WHERE extraction_id = %s AND kind = 'match' "
                        "AND airflow_run_id = %s ORDER BY started_at DESC LIMIT 1",
                        (extraction_id, airflow_run_id)).fetchone()


def _inputs_of_row(row) -> RunInputs:
    return RunInputs(extraction_id=row[2], t3_rule_version=row[3], parser_version=row[4], norm_version=row[5],
                     rule_version=row[6], imdb_snapshot_id=row[7], human_set_hash=row[8])


def _add_counts(conn: psycopg.Connection, run_id: str, key: str, value: Any) -> None:
    conn.execute("UPDATE task4.run SET counts = counts || jsonb_build_object(%s::text, %s::jsonb) WHERE run_id = %s",
                 (key, Jsonb(value), run_id))


def run_stage(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet, stage: str, *, airflow_run_id: str,
              batch_size: int = DEFAULT_BATCH, limit: int | None = None, dag_id: str | None = None,
              before_commit: BeforeCommit | None = None) -> StageResult:
    """DAG 的一个步骤：normalize（商品版本与标准化）→ decide（IMDb 标题键、候选与判定）→ cluster（聚类与映射，收尾运行）。
    每个步骤各持一次抽取锁；三个步骤共用一条 task4.run，靠 airflow_run_id 找回。步骤失败会把运行记为 failed，
    同一 DAG run 里重试时接着用这条运行，已提交的批次不重做。输入键已有成功运行时，normalize 记 reused，其余步骤跳过。"""
    if stage not in STAGES:
        raise ValueError(f"未知的步骤 {stage!r}")
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    if rules.version != inputs.rule_version:
        raise ValueError(f"规则集版本 {rules.version} ≠ 输入里的规则版本 {inputs.rule_version}")
    started = time.monotonic()
    with extraction_lock(conn, inputs.extraction_id):
        row = _find_run(conn, inputs.extraction_id, airflow_run_id)
        if row is not None and row[1] == "reused":
            return StageResult(stage, row[0], "reused", row[9], dict(row[10]), _inputs_of_row(row))
        if stage == "normalize":
            check_inputs(conn, inputs)
            if row is None:
                inputs = dataclasses.replace(inputs, human_set_hash=human_set_hash(conn, inputs.extraction_id))
                close_abandoned(conn, inputs.extraction_id, keep_airflow_run_id=airflow_run_id)
                prior = reusable_run(conn, inputs)
                ctx = start_run(conn, inputs, "match", params={"batch_size": batch_size, "limit": limit},
                                dag_id=dag_id, airflow_run_id=airflow_run_id)
                if prior:
                    finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, mapping_version_id=prior[1])
                    return StageResult(stage, ctx.run_id, "reused", prior[1], {"reused_run": prior[0]}, inputs,
                                       round(time.monotonic() - started, 2))
            else:
                ctx = RunContext(row[0], _inputs_of_row(row))
        else:
            if row is None:
                raise Task4InputError(f"没有找到 DAG run {airflow_run_id} 的匹配运行（normalize 步骤尚未执行）")
            ctx = RunContext(row[0], _inputs_of_row(row))
        inputs = ctx.inputs
        if row is not None:      # 重试：把上次失败的运行接着用
            conn.execute("UPDATE task4.run SET status = 'running', finished_at = NULL, failure_reason = NULL "
                         "WHERE run_id = %s AND status IN ('failed', 'interrupted')", (ctx.run_id,))
        try:
            counts, mapping_id = _do_stage(conn, ctx, rules, stage, batch_size, limit, before_commit)
        except BaseException as exc:
            try:
                finish_run(conn, ctx.run_id, "failed", failure_reason=f"{stage}: {type(exc).__name__}: {exc}"[:2000])
            except psycopg.Error:
                pass
            raise
        elapsed = round(time.monotonic() - started, 2)
        counts["elapsed_seconds"] = elapsed
        if stage == "cluster":
            merged = dict(conn.execute("SELECT counts FROM task4.run WHERE run_id = %s", (ctx.run_id,)).fetchone()[0])
            merged[stage] = counts
            finish_run(conn, ctx.run_id, "succeeded", merged, mapping_version_id=mapping_id)
        else:
            _add_counts(conn, ctx.run_id, stage, counts)
        return StageResult(stage, ctx.run_id, "done", mapping_id, counts, inputs, elapsed)


def _do_stage(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, stage: str, batch_size: int,
              limit: int | None, before_commit: BeforeCommit | None) -> tuple[dict[str, Any], str | None]:
    if stage == "normalize":
        counts: dict[str, Any] = {"product_editions_new": editions.build_editions(conn, ctx)}
        n = editions.normalize_extraction(conn, ctx, batch_size=batch_size, limit=limit, before_commit=before_commit)
        counts["normalize"] = dataclasses.asdict(n)
        return counts, None
    if stage == "decide":
        left = editions.count_pending(conn, ctx)
        if left:
            raise Task4InputError(f"标准化阶段还有 {left} 个商品未处理（limit 试跑或上一步中断），不进入判定")
        k = candidates.build_imdb_keys(conn, ctx, before_commit=before_commit)
        d = candidates.decide_entities(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
        p = candidates.decide_pairs(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
        return {"imdb_keys": dataclasses.asdict(k), "decide": dataclasses.asdict(d),
                "decide_pair": dataclasses.asdict(p)}, None
    left = candidates.count_pending_entities(conn, ctx)
    if left:
        raise Task4InputError(f"判定阶段还有 {left} 个商品未处理，不生成映射")
    mapping = cluster.build_mapping(conn, ctx, rules)
    return {"mapping_version_id": mapping.mapping_version_id, **mapping.counts, "events": mapping.events}, \
        mapping.mapping_version_id
