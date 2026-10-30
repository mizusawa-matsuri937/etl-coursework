"""任务 6 DAG（补全上映信息）：检查输入 → 年份候选 → 共现反查 → 商品发售日期 → 评论时间与年份提及 → 择优 → 推断上界 → 核对 → 收尾报告 → 最终门禁。

手动触发。默认处理最近一次全量清单的抽取、任务 4 映射版本 map0002、任务 5 人物映射版本 pmap0001、IMDb 快照 imdb_2026-10-29、规则 t6r1；
输入键相同时记 reused，不新增也不修改目标行。业务逻辑都在 etl_task6.pipeline 与 etl_task6.runner，DAG 只负责依赖、重试和状态：
- 确定性错误（输入缺失、核对不符、抽取锁被占用、参数不合法等）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连）按 retries 重试，次数有上限；已提交的批次保留，重试只处理剩余批次；
- finalize_report 无论上游成败都运行，run_gate 再判定整体结果。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.exceptions import AirflowFailException
from airflow.sdk import DAG, Param, get_current_context, task

RETRIES = int(os.environ.get("ETL_TASK_RETRIES", "2"))
RETRY_DELAY = timedelta(seconds=int(os.environ.get("ETL_RETRY_DELAY_SECONDS", "60")))

PARAMS = {
    "extraction_id": Param(None, type=["null", "string"], description="任务 1 抽取 ID；默认取最近一次全量清单构建"),
    "t4_mapping_version_id": Param(None, type=["null", "string"],
                                   description="任务 4 映射版本；默认 map0002（属于该抽取时），否则该抽取最新的映射版本"),
    "t5_person_mapping_version_id": Param(None, type=["null", "string"],
                                          description="任务 5 人物映射版本；默认 pmap0001"),
    "imdb_snapshot_id": Param(None, type=["null", "string"], description="IMDb 快照编号；默认取映射版本所用的快照"),
    "rule_version": Param("t6r1", type="string", description="上映规则版本（优先级、分歧、上界条件与置信度分档，见 etl_task6.rules.RULESETS）"),
    "batch_size": Param(500, type="integer", minimum=1, description="每批提交的作品/商品数"),
    "limit": Param(None, type=["null", "integer"], minimum=1,
                   description="本次最多处理的作品数；只用于试跑，之后的步骤会失败，不生成上映信息版本"),
    "fault_injection": Param(None, type=["null", "string"],
                             description="before_commit:<阶段>:<批号> 或 transient_once:<标记文件>；仅 ETL_ENV=test"),
}


def _params():
    from etl_task6.pipeline import ReleaseParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, ReleaseParams(
        run_key=ReleaseParams.sanitize(ctx["run_id"]), extraction_id=p.get("extraction_id"),
        t4_mapping_version_id=p.get("t4_mapping_version_id"),
        t5_person_mapping_version_id=p.get("t5_person_mapping_version_id"),
        imdb_snapshot_id=p.get("imdb_snapshot_id"),
        rule_version=p["rule_version"], batch_size=int(p["batch_size"]),
        limit=p.get("limit"), fault_injection=p.get("fault_injection"), try_number=ctx["ti"].try_number,
        dag_id=ctx["dag"].dag_id)


def _run(step, fn):
    from etl_task6.pipeline import StepError, run_step

    ctx, params = _params()
    try:
        return run_step(step, params, lambda: fn(ctx, params))
    except StepError as err:
        if err.transient:
            raise  # 交给 Airflow 重试，次数受 retries 限制
        raise AirflowFailException(str(err)) from err


with DAG(
    dag_id="amazon_movies_task6_release",
    description="任务 6：补全首次公开发行年份、商品发售日期、最早评论时间与推断上界",
    schedule=None,
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=False,
    params=PARAMS,
    default_args={"retries": RETRIES, "retry_delay": RETRY_DELAY},
    tags=["etl", "task6"],
) as dag:

    @task(task_id="check_inputs")
    def check_inputs():
        from etl_task6 import pipeline
        return _run("check_inputs", lambda ctx, p: pipeline.check_inputs_step(p))

    @task(task_id="candidates")
    def candidates():
        from etl_task6 import pipeline
        return _run("candidates", lambda ctx, p: pipeline.candidates_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="cooccur")
    def cooccur():
        from etl_task6 import pipeline
        return _run("cooccur", lambda ctx, p: pipeline.cooccur_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="product_dates")
    def product_dates():
        from etl_task6 import pipeline
        return _run("product_dates", lambda ctx, p: pipeline.product_dates_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="review_times")
    def review_times():
        from etl_task6 import pipeline
        return _run("review_times", lambda ctx, p: pipeline.review_times_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="select")
    def select():
        from etl_task6 import pipeline
        return _run("select", lambda ctx, p: pipeline.select_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="bounds")
    def bounds():
        from etl_task6 import pipeline
        return _run("bounds", lambda ctx, p: pipeline.bounds_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="verify")
    def verify():
        from etl_task6 import pipeline
        return _run("verify", lambda ctx, p: pipeline.release_verify_step(p, ctx["ti"].xcom_pull(task_ids="bounds")))

    @task(task_id="finalize_report", trigger_rule="all_done")
    def finalize_report():
        from etl_task6 import pipeline
        ti = get_current_context()["ti"]
        upstream = {s: ti.xcom_pull(task_ids=s) for s in pipeline.RELEASE_STEP_NAMES}
        return _run("finalize_report", lambda ctx, p: pipeline.finalize_step(p, upstream))

    @task(task_id="run_gate", trigger_rule="all_done")
    def run_gate():
        from etl_task1.pipeline import gate_decision

        ti = get_current_context()["ti"]
        ok, reason = gate_decision(ti.xcom_pull(task_ids="finalize_report"))
        if not ok:
            raise AirflowFailException(f"门禁判定失败：{reason}")
        return {"status": "passed"}

    (
        check_inputs()
        >> candidates()
        >> cooccur()
        >> product_dates()
        >> review_times()
        >> select()
        >> bounds()
        >> verify()
        >> finalize_report()
        >> run_gate()
    )
