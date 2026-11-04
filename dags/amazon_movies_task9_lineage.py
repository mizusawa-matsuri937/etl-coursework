"""任务 9 DAG（字段级数据血缘）：检查输入 → 源记录 → 字段断言 → 派生边 → 字段选择 → 影响闭包 → 物化血缘版本 → 核对 →
收尾报告 → 最终门禁。

手动触发。默认处理最近一次全量清单的抽取与 spec R01 的上游版本（map0002、pmap0001、rv0001、cv0003、rd0001、
imdb_2026-10-29）、规则 t9r1；输入键相同且已通过核对时记 reused，不新增也不修改目标行。业务逻辑都在 etl_task9.pipeline
与 etl_task9.runner，DAG 只负责依赖、重试和状态：
- 确定性错误（输入缺失、版本未核实、核对不符、抽取锁被占用、参数不合法等）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连）按 retries 重试，最多 2 次；已提交的批次保留，重试只处理剩余实体；
- finalize_report 与 gate 无论上游成败都运行（all_done），gate 再判定整体结果，不掩盖失败；
- 运行在 verify 通过之后才是 succeeded；核对失败的版本保持 unverified，同输入重跑在原版本上重写。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.sdk import DAG, Param, get_current_context, task
from airflow.sdk.exceptions import AirflowFailException

RETRIES = min(2, int(os.environ.get("ETL_TASK_RETRIES", "2")))
RETRY_DELAY = timedelta(seconds=int(os.environ.get("ETL_RETRY_DELAY_SECONDS", "60")))

PARAMS = {
    "extraction_id": Param(None, type=["null", "string"], description="任务 1 抽取 ID；默认取最近一次全量清单构建"),
    "t4_mapping_version_id": Param("map0002", type="string", description="任务 4 映射版本"),
    "t5_person_mapping_version_id": Param("pmap0001", type="string", description="任务 5 人物映射版本"),
    "t6_release_version_id": Param("rv0001", type="string", description="任务 6 上映版本"),
    "conflict_version_id": Param("cv0003", type="string", description="任务 7 冲突消解版本（须已 verified）"),
    "review_dedup_version_id": Param("rd0001", type="string", description="任务 8 去重版本（须已 verified）"),
    "imdb_snapshot_id": Param("imdb_2026-10-29", type="string", description="IMDb 快照"),
    "rule_version": Param("t9r1", type="string", description="血缘规则版本"),
    "batch_size": Param(1000, type="integer", minimum=1, description="每批实体数"),
    "snapshot_root": Param(None, type=["null", "string"], description="快照相对路径的根目录；默认项目根目录 /opt/etl"),
    "fault_injection": Param(None, type=["null", "string"],
                             description="before_commit:<阶段>:<批号>、transient_once:<标记文件> 或 verify_fail；仅 ETL_ENV=test"),
}


def _params():
    from etl_task9.pipeline import LineageParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, LineageParams(
        run_key=LineageParams.sanitize(ctx["run_id"]), extraction_id=p.get("extraction_id"),
        t4_mapping_version_id=p["t4_mapping_version_id"], t5_person_mapping_version_id=p["t5_person_mapping_version_id"],
        t6_release_version_id=p["t6_release_version_id"], conflict_version_id=p["conflict_version_id"],
        review_dedup_version_id=p["review_dedup_version_id"], imdb_snapshot_id=p["imdb_snapshot_id"],
        rule_version=p["rule_version"], batch_size=int(p["batch_size"]), snapshot_root=p.get("snapshot_root"),
        fault_injection=p.get("fault_injection"), try_number=ctx["ti"].try_number, dag_id=ctx["dag"].dag_id)


def _run(step, fn):
    from etl_task9.pipeline import StepError, run_step

    ctx, params = _params()
    try:
        return run_step(step, params, lambda: fn(ctx, params))
    except StepError as err:
        if err.transient:
            raise  # 交给 Airflow 重试，次数受 retries 限制
        raise AirflowFailException(str(err)) from err


with DAG(
    dag_id="amazon_movies_task9_lineage",
    description="任务 9：字段级数据血缘（源记录、字段断言、派生边、字段选择、影响闭包）并物化血缘版本",
    schedule=None,
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=False,
    params=PARAMS,
    default_args={"retries": RETRIES, "retry_delay": RETRY_DELAY},
    tags=["etl", "task9"],
) as dag:

    @task(task_id="check_inputs")
    def check_inputs():
        from etl_task9 import pipeline
        return _run("check_inputs", lambda ctx, p: pipeline.check_inputs_step(p))

    def _step(step: str):
        def body():
            from etl_task9 import pipeline
            return _run(step, lambda ctx, p: pipeline.stage_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs"), step))
        return body

    build_sources = task(task_id="build_sources")(_step("build_sources"))
    build_assertions = task(task_id="build_assertions")(_step("build_assertions"))
    build_derivations = task(task_id="build_derivations")(_step("build_derivations"))
    build_selections = task(task_id="build_selections")(_step("build_selections"))
    build_impact = task(task_id="build_impact")(_step("build_impact"))
    finalize_version = task(task_id="finalize_version")(_step("finalize_version"))
    verify = task(task_id="verify")(_step("verify"))

    @task(task_id="finalize_report", trigger_rule="all_done")
    def finalize_report():
        from etl_task9 import pipeline
        ti = get_current_context()["ti"]
        upstream = {s: ti.xcom_pull(task_ids=s) for s in pipeline.STEP_NAMES}
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
        >> build_sources()
        >> build_assertions()
        >> build_derivations()
        >> build_selections()
        >> build_impact()
        >> finalize_version()
        >> verify()
        >> finalize_report()
        >> gate()
    )
