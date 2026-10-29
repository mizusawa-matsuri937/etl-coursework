"""任务 2 第一部分 DAG：检查抽取 → 构建采集清单 → 核对 → 导出 → 收尾报告 → 最终门禁。

手动触发，不发任何网络请求。业务逻辑都在 etl_task2.pipeline，DAG 只负责依赖、重试和状态：
- 确定性错误（抽取不合格、来源冲突、数量不符等）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连、同一构建正被占用）按 retries 重试，次数有上限；
- finalize_report 无论上游成败都运行，run_gate 再判定整体结果，避免收尾成功掩盖上游失败。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.exceptions import AirflowFailException
from airflow.sdk import DAG, Param, get_current_context, task

RETRIES = int(os.environ.get("ETL_TASK_RETRIES", "2"))
RETRY_DELAY = timedelta(seconds=int(os.environ.get("ETL_RETRY_DELAY_SECONDS", "60")))

PARAMS = {
    "extraction_id": Param(None, type=["null", "string"],
                           description="任务 1 抽取 ID（64 位十六进制）；正式清单须为完成的全量抽取"),
    "allow_sample": Param(False, type="boolean", description="仅测试库：允许以样本抽取构建清单"),
    "fault_injection": Param(None, type=["null", "string"],
                             description="仅 ETL_ENV=test：before_commit 或 transient_once:<标记文件>"),
}


def _params():
    from etl_task2.pipeline import RunParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, RunParams(
        run_key=RunParams.sanitize(ctx["run_id"]), extraction_id=p.get("extraction_id"),
        allow_sample=bool(p.get("allow_sample")), try_number=ctx["ti"].try_number,
        dag_id=ctx["dag"].dag_id, fault_injection=p.get("fault_injection"))


def _run(step, fn):
    from etl_task2.pipeline import StepError, run_step

    ctx, params = _params()
    try:
        return run_step(step, params, lambda: fn(ctx, params))
    except StepError as err:
        if err.transient:
            raise  # 交给 Airflow 重试，次数受 retries 限制
        raise AirflowFailException(str(err)) from err


with DAG(
    dag_id="amazon_products_task2_manifest",
    description="任务 2 第一部分：从任务 1 抽取生成产品采集清单与候选链接",
    schedule=None,
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=False,
    params=PARAMS,
    default_args={"retries": RETRIES, "retry_delay": RETRY_DELAY},
    tags=["etl", "task2"],
) as dag:

    @task(task_id="check_extraction")
    def check_extraction():
        from etl_task2 import pipeline
        return _run("check_extraction", lambda ctx, p: pipeline.check_extraction_step(p))

    @task(task_id="build_manifest")
    def build_manifest():
        from etl_task2 import pipeline
        return _run("build_manifest", lambda ctx, p: pipeline.build_manifest_step(
            p, ctx["ti"].xcom_pull(task_ids="check_extraction")))

    @task(task_id="verify_manifest")
    def verify_manifest():
        from etl_task2 import pipeline
        return _run("verify_manifest", lambda ctx, p: pipeline.verify_manifest_step(
            p, ctx["ti"].xcom_pull(task_ids="check_extraction")["extraction_id"]))

    @task(task_id="export_manifest")
    def export_manifest():
        from etl_task2 import pipeline

        def step(ctx, p):
            checked = ctx["ti"].xcom_pull(task_ids="check_extraction")
            return pipeline.export_manifest_step(p, checked["extraction_id"], checked["build_id"],
                                                 ctx["ti"].xcom_pull(task_ids="verify_manifest"))
        return _run("export_manifest", step)

    @task(task_id="finalize_report", trigger_rule="all_done")
    def finalize_report():
        from etl_task2 import pipeline
        ctx, params = _params()
        upstream = {n: ctx["ti"].xcom_pull(task_ids=n) for n in pipeline.STEP_NAMES}
        try:
            return pipeline.run_step("finalize_report", params, lambda: pipeline.finalize_step(params, upstream))
        except pipeline.StepError as err:
            raise AirflowFailException(str(err)) from err  # 报告写不出来不重试，门禁随后失败

    @task(task_id="run_gate", trigger_rule="all_done")
    def run_gate():
        from etl_task2 import pipeline
        ctx = get_current_context()
        ok, reason = pipeline.gate_decision(ctx["ti"].xcom_pull(task_ids="finalize_report"))
        if not ok:
            raise AirflowFailException(reason)
        return {"gate": reason}

    (check_extraction() >> build_manifest() >> verify_manifest() >> export_manifest()
     >> finalize_report() >> run_gate())
