"""任务 7 DAG（处理不同来源的信息冲突）：检查输入 → 版本层消解 → 关联评估 → 作品层标量 → 作品层多值 → 物化版本 → 核对 → 收尾报告 → 最终门禁。

手动触发。默认处理最近一次全量清单的抽取、任务 4 映射版本 map0002、任务 5 人物映射版本 pmap0001、任务 6 上映版本 rv0001、
IMDb 快照 imdb_2026-10-29、规则 t7r1；输入键相同且已通过核对时记 reused，不新增也不修改目标行。业务逻辑都在
etl_task7.pipeline 与 etl_task7.runner，DAG 只负责依赖、重试和状态：
- 确定性错误（输入缺失、核对不符、抽取锁被占用、参数不合法等）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连）按 retries 重试，次数有上限；已提交的批次保留，重试只处理剩余批次；
- finalize_report 与 gate 无论上游成败都运行（all_done），gate 再判定整体结果，不掩盖失败；
- 运行在 verify 通过之后才是 succeeded；核对失败的版本保持 unverified，同输入重跑不会被 reused。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.sdk import DAG, Param, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException

RETRIES = int(os.environ.get("ETL_TASK_RETRIES", "2"))
RETRY_DELAY = timedelta(seconds=int(os.environ.get("ETL_RETRY_DELAY_SECONDS", "60")))

PARAMS = {
    "extraction_id": Param(None, type=["null", "string"], description="任务 1 抽取 ID；默认取最近一次全量清单构建"),
    "t4_mapping_version_id": Param(None, type=["null", "string"],
                                   description="任务 4 映射版本；默认 map0002（属于该抽取时），否则该抽取最新的映射版本"),
    "t5_person_mapping_version_id": Param(None, type=["null", "string"], description="任务 5 人物映射版本；默认 pmap0001"),
    "t6_release_version_id": Param(None, type=["null", "string"], description="任务 6 上映版本；默认 rv0001"),
    "imdb_snapshot_id": Param(None, type=["null", "string"], description="IMDb 快照编号；默认取映射版本所用的快照"),
    "rule_version": Param("t7r1", type="string", description="冲突消解规则版本（见 etl_task7.rules.RULESETS）"),
    "batch_size": Param(1000, type="integer", minimum=1, description="每批提交的作品/商品数"),
    "fault_injection": Param(None, type=["null", "string"],
                             description="before_commit:<阶段>:<批号>、transient_once:<标记文件> 或 verify_fail；仅 ETL_ENV=test"),
}


def _params():
    from etl_task7.pipeline import ConflictParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, ConflictParams(
        run_key=ConflictParams.sanitize(ctx["run_id"]), extraction_id=p.get("extraction_id"),
        t4_mapping_version_id=p.get("t4_mapping_version_id"),
        t5_person_mapping_version_id=p.get("t5_person_mapping_version_id"),
        t6_release_version_id=p.get("t6_release_version_id"), imdb_snapshot_id=p.get("imdb_snapshot_id"),
        rule_version=p["rule_version"], batch_size=int(p["batch_size"]), fault_injection=p.get("fault_injection"),
        try_number=ctx["ti"].try_number, dag_id=ctx["dag"].dag_id)


def _run(step, fn):
    from etl_task7.pipeline import StepError, run_step

    ctx, params = _params()
    try:
        return run_step(step, params, lambda: fn(ctx, params))
    except StepError as err:
        if err.transient:
            raise  # 交给 Airflow 重试，次数受 retries 限制
        raise AirflowFailException(str(err)) from err


with DAG(
    dag_id="amazon_movies_task7_conflict",
    description="任务 7：处理不同来源的信息冲突，消解作品层与商品版本层字段并生成冲突消解版本",
    schedule=None,
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=False,
    params=PARAMS,
    default_args={"retries": RETRIES, "retry_delay": RETRY_DELAY},
    tags=["etl", "task7"],
) as dag:

    @task(task_id="check_inputs")
    def check_inputs():
        from etl_task7 import pipeline
        return _run("check_inputs", lambda ctx, p: pipeline.check_inputs_step(p))

    def _step(step: str):
        def body():
            from etl_task7 import pipeline
            return _run(step, lambda ctx, p: pipeline.stage_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs"), step))
        return body

    edition_resolve = task(task_id="edition_resolve")(_step("edition_resolve"))
    link_assess = task(task_id="link_assess")(_step("link_assess"))
    movie_scalar_resolve = task(task_id="movie_scalar_resolve")(_step("movie_scalar_resolve"))
    movie_multivalue_resolve = task(task_id="movie_multivalue_resolve")(_step("movie_multivalue_resolve"))
    finalize_version = task(task_id="finalize_version")(_step("finalize_version"))
    verify = task(task_id="verify")(_step("verify"))

    @task(task_id="finalize_report", trigger_rule="all_done")
    def finalize_report():
        from etl_task7 import pipeline
        ti = get_current_context()["ti"]
        upstream = {s: ti.xcom_pull(task_ids=s) for s in pipeline.CONFLICT_STEP_NAMES}
        return _run("finalize_report", lambda ctx, p: pipeline.finalize_step(p, upstream))

    @task(task_id="gate", trigger_rule="all_done")
    def gate():
        from etl_task1.pipeline import gate_decision

        ti = get_current_context()["ti"]
        ok, reason = gate_decision(ti.xcom_pull(task_ids="finalize_report"))
        if not ok:
            raise AirflowFailException(f"门禁判定失败：{reason}")
        return {"status": "passed"}

    (
        check_inputs()
        >> edition_resolve()
        >> link_assess()
        >> movie_scalar_resolve()
        >> movie_multivalue_resolve()
        >> finalize_version()
        >> verify()
        >> finalize_report()
        >> gate()
    )
