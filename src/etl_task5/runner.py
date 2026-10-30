"""一次人物运行的编排（spec R42、R43、R45）：输入检查 → 抽取锁 → 复用判断 → 提及 → 外部 ID → 作品内组 → 跨作品判定 → 聚类与人物 → MoviePerson → 审计。

execute_resolve 一次串起全部阶段；DAG 的四个步骤（mentions、link_external、resolve、movie_person）用 run_stage，共用一条 task5.run，
靠 airflow_run_id 找回（与任务 4 的 run_stage 同义，见 pipeline.py）。
同一输入键已有成功运行时只记一条 reused 审计，不新增也不修改目标行；中断后重跑只补没有标记的批次。
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from . import cluster, external, mentions, movie_person, resolve
from .rules import RuleSet, ruleset
from .run import (DEFAULT_BATCH, BeforeCommit, RunContext, RunInputs, Task5InputError, check_inputs, close_abandoned,
                  extraction_lock, finish_run, human_set_hash, reusable_run, start_run)


@dataclass
class RunResult:
    run_id: str
    status: str                          # succeeded | reused
    person_mapping_version_id: str | None
    inputs: RunInputs
    counts: dict[str, Any] = field(default_factory=dict)


def execute_resolve(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet | None = None, *,
                    batch_size: int = DEFAULT_BATCH, dag_id: str | None = None, airflow_run_id: str | None = None,
                    before_commit: BeforeCommit | None = None) -> RunResult:
    """人工决策集的哈希以数据库里当前的为准，所以导入决策后再运行就是新的输入键。"""
    if not conn.autocommit:
        raise ValueError("运行连接必须是自动提交模式（使用 etl_task1.db.connect）")
    rules = rules or ruleset(inputs.rule_version)
    if rules.version != inputs.rule_version:
        raise ValueError(f"规则集版本 {rules.version} ≠ 输入里的规则版本 {inputs.rule_version}")
    check_inputs(conn, inputs)
    with extraction_lock(conn, inputs.extraction_id):
        inputs = dataclasses.replace(inputs, human_set_hash=human_set_hash(conn, inputs.extraction_id))
        close_abandoned(conn, inputs.extraction_id)
        prior = reusable_run(conn, inputs)
        ctx = start_run(conn, inputs, "person", params={"batch_size": batch_size}, dag_id=dag_id,
                        airflow_run_id=airflow_run_id)
        if prior:
            finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, person_mapping_version_id=prior[1])
            return RunResult(ctx.run_id, "reused", prior[1], inputs, {"reused_run": prior[0]})
        try:
            counts: dict[str, Any] = {}
            counts["mentions"] = dataclasses.asdict(
                mentions.extract_mentions(conn, ctx, batch_size=batch_size, before_commit=before_commit))
            counts["credits"] = dataclasses.asdict(
                external.link_credits(conn, ctx, batch_size=batch_size, before_commit=before_commit))
            counts["filmography"] = dataclasses.asdict(
                external.link_filmography(conn, ctx, batch_size=batch_size, before_commit=before_commit))
            counts["groups"] = resolve.build_groups(conn, ctx)
            counts["decide_pair"] = dataclasses.asdict(
                resolve.decide_blocks(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit))
            people = cluster.build_people(conn, ctx, rules, before_commit=before_commit)
            counts["people"] = {"person_mapping_version_id": people.person_mapping_version_id, **people.counts}
            counts["movie_person"] = movie_person.build_movie_person(conn, people.person_mapping_version_id,
                                                                     before_commit=before_commit)
        except BaseException as exc:
            try:
                finish_run(conn, ctx.run_id, "failed", failure_reason=f"{type(exc).__name__}: {exc}"[:2000])
            except psycopg.Error:
                pass
            raise
        finish_run(conn, ctx.run_id, "succeeded", counts, person_mapping_version_id=people.person_mapping_version_id)
        return RunResult(ctx.run_id, "succeeded", people.person_mapping_version_id, inputs, counts)


# ---------------------------------------------------------------- 分步骤运行（Airflow DAG 的四个步骤）

STAGES = ("mentions", "link_external", "resolve", "movie_person")


@dataclass
class StageResult:
    stage: str
    run_id: str
    status: str                          # done | reused（输入键已有成功运行，本阶段什么都不做）
    person_mapping_version_id: str | None
    counts: dict[str, Any]
    inputs: RunInputs
    elapsed_seconds: float = 0.0


_RUN_COLUMNS = ("run_id, status, extraction_id, t4_mapping_version_id, parser_version, norm_version, rule_version, "
                "imdb_snapshot_id, human_set_hash, person_mapping_version_id, counts")


def _find_run(conn: psycopg.Connection, extraction_id: str, airflow_run_id: str):
    """同一个 DAG run 的人物运行：步骤之间、以及步骤重试时靠 airflow_run_id 找回同一条运行。"""
    return conn.execute(f"SELECT {_RUN_COLUMNS} FROM task5.run WHERE extraction_id = %s AND kind = 'person' "
                        "AND airflow_run_id = %s ORDER BY started_at DESC LIMIT 1",
                        (extraction_id, airflow_run_id)).fetchone()


def _inputs_of_row(row) -> RunInputs:
    return RunInputs(extraction_id=row[2], t4_mapping_version_id=row[3], parser_version=row[4], norm_version=row[5],
                     rule_version=row[6], imdb_snapshot_id=row[7], human_set_hash=row[8])


def _add_counts(conn: psycopg.Connection, run_id: str, key: str, value: Any) -> None:
    conn.execute("UPDATE task5.run SET counts = counts || jsonb_build_object(%s::text, %s::jsonb) WHERE run_id = %s",
                 (key, Jsonb(value), run_id))


def run_stage(conn: psycopg.Connection, inputs: RunInputs, rules: RuleSet | None, stage: str, *, airflow_run_id: str,
              batch_size: int = DEFAULT_BATCH, limit: int | None = None, dag_id: str | None = None,
              before_commit: BeforeCommit | None = None) -> StageResult:
    """DAG 的一个步骤：mentions（提及）→ link_external（外部 ID 途径一、二）→ resolve（作品内组、跨作品判定、聚类与人物）
    → movie_person（MoviePerson，收尾运行）。每个步骤各持一次抽取锁；四个步骤共用一条 task5.run，靠 airflow_run_id 找回。
    步骤失败会把运行记为 failed，同一 DAG run 里重试时接着用这条运行，已提交的批次不重做。输入键已有成功运行时，
    mentions 记 reused，其余步骤跳过。limit 只限制提及阶段，之后的步骤因仍有未处理商品而确定性失败。"""
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
            return StageResult(stage, row[0], "reused", row[9], dict(row[10]), _inputs_of_row(row))
        if stage == "mentions":
            check_inputs(conn, inputs)
            if row is None:
                inputs = dataclasses.replace(inputs, human_set_hash=human_set_hash(conn, inputs.extraction_id))
                close_abandoned(conn, inputs.extraction_id, keep_airflow_run_id=airflow_run_id)
                prior = reusable_run(conn, inputs)
                ctx = start_run(conn, inputs, "person", params={"batch_size": batch_size, "limit": limit},
                                dag_id=dag_id, airflow_run_id=airflow_run_id)
                if prior:
                    finish_run(conn, ctx.run_id, "reused", {"reused_run": prior[0]}, person_mapping_version_id=prior[1])
                    return StageResult(stage, ctx.run_id, "reused", prior[1], {"reused_run": prior[0]}, inputs,
                                       round(time.monotonic() - started, 2))
            else:
                ctx = RunContext(row[0], _inputs_of_row(row))
        else:
            if row is None:
                raise Task5InputError(f"没有找到 DAG run {airflow_run_id} 的人物运行（mentions 步骤尚未执行）")
            ctx = RunContext(row[0], _inputs_of_row(row))
        inputs = ctx.inputs
        if row is not None:      # 重试：把上次失败的运行接着用
            conn.execute("UPDATE task5.run SET status = 'running', finished_at = NULL, failure_reason = NULL "
                         "WHERE run_id = %s AND status IN ('failed', 'interrupted')", (ctx.run_id,))
        try:
            counts, pmap = _do_stage(conn, ctx, rules, stage, batch_size, limit, before_commit,
                                     row[9] if row is not None else None)
        except BaseException as exc:
            try:
                finish_run(conn, ctx.run_id, "failed", failure_reason=f"{stage}: {type(exc).__name__}: {exc}"[:2000])
            except psycopg.Error:
                pass
            raise
        elapsed = round(time.monotonic() - started, 2)
        counts["elapsed_seconds"] = elapsed
        if stage == "movie_person":
            merged = dict(conn.execute("SELECT counts FROM task5.run WHERE run_id = %s", (ctx.run_id,)).fetchone()[0])
            merged[stage] = counts
            finish_run(conn, ctx.run_id, "succeeded", merged, person_mapping_version_id=pmap)
        else:
            _add_counts(conn, ctx.run_id, stage, counts)
            if pmap:      # 人物映射版本已生成；运行仍是 running，由 movie_person 收尾
                conn.execute("UPDATE task5.run SET person_mapping_version_id = %s WHERE run_id = %s", (pmap, ctx.run_id))
        return StageResult(stage, ctx.run_id, "done", pmap, counts, inputs, elapsed)


def _do_stage(conn: psycopg.Connection, ctx: RunContext, rules: RuleSet, stage: str, batch_size: int,
              limit: int | None, before_commit: BeforeCommit | None,
              run_pmap: str | None) -> tuple[dict[str, Any], str | None]:
    if stage == "mentions":
        return dataclasses.asdict(mentions.extract_mentions(conn, ctx, batch_size=batch_size, limit=limit,
                                                            before_commit=before_commit)), None
    if stage == "link_external":
        c = external.link_credits(conn, ctx, batch_size=batch_size, before_commit=before_commit)
        f = external.link_filmography(conn, ctx, batch_size=batch_size, before_commit=before_commit)
        return {"credits": dataclasses.asdict(c), "filmography": dataclasses.asdict(f)}, None
    if stage == "resolve":
        for name in (external.CREDITS, external.FILMOGRAPHY):
            left = external.count_pending(conn, ctx, name)
            if left:
                raise Task5InputError(f"外部 ID 阶段 {name} 还有 {left} 个商品未处理，不进入归并")
        groups = resolve.build_groups(conn, ctx)
        decided = resolve.decide_blocks(conn, ctx, rules, batch_size=batch_size, before_commit=before_commit)
        if decided.remaining:
            raise Task5InputError(f"跨作品判定还有 {decided.remaining} 个分块未处理，不生成人物")
        people = cluster.build_people(conn, ctx, rules, before_commit=before_commit)
        return {"groups": groups, "decide_pair": dataclasses.asdict(decided),
                "person_mapping_version_id": people.person_mapping_version_id, **people.counts}, \
            people.person_mapping_version_id
    if not run_pmap:
        raise Task5InputError("归并步骤尚未生成人物映射版本，不能生成 MoviePerson")
    out = movie_person.build_movie_person(conn, run_pmap, before_commit=before_commit)
    return {"person_mapping_version_id": run_pmap, **out}, run_pmap
