"""任务 1 DAG：校验输入 → 流式解析与装载 → 数量核对 → 导出 → 收尾报告 → 最终门禁。

手动触发，不做历史补跑。业务逻辑都在 etl_task1.pipeline，DAG 只负责依赖、重试和状态：
- 确定性错误（输入不符、守恒破坏、证据不完整等）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连等）按 retries 重试，次数有上限；
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
    "scope": Param("sample", enum=["sample", "full"], description="sample 需同时给出 sample_max_records；full 读到 EOF"),
    "sample_max_records": Param(1000, type=["null", "integer"], minimum=1,
                                description="scope=sample 时必填；scope=full 时忽略"),
    "batch_rows": Param(None, type=["null", "integer"], minimum=1),
    "source_gzip_path": Param(None, type=["null", "string"]),
    "source_manifest_path": Param(None, type=["null", "string"]),
    "baseline_explanation": Param(None, type=["null", "string"], description="全量与官方基准有差异时的有依据说明"),
    "fault_injection": Param(None, type=["null", "string"], description="仅 ETL_ENV=test 时有效"),
}


def _params():
    from etl_task1.pipeline import RunParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, RunParams(
        run_key=RunParams.sanitize(ctx["run_id"]), try_number=ctx["ti"].try_number, dag_id=ctx["dag"].dag_id,
        scope=p.get("scope"), sample_max_records=p.get("sample_max_records"),
        batch_rows=p.get("batch_rows"), source_gzip_path=p.get("source_gzip_path"),
        source_manifest_path=p.get("source_manifest_path"),
        baseline_explanation=p.get("baseline_explanation"), fault_injection=p.get("fault_injection"))


def _run(step, fn):
    from etl_task1.pipeline import StepError, run_step

    ctx, params = _params()
    try:
        return run_step(step, params, lambda: fn(ctx, params))
    except StepError as err:
        if err.transient:
            raise  # 交给 Airflow 重试，次数受 retries 限制
        raise AirflowFailException(str(err)) from err


with DAG(
    dag_id="amazon_reviews_task1",
    description="Amazon 电影评论任务 1：全量流式抽取与装载",
    schedule=None,
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,  # 重型装载与核对不并发
    is_paused_upon_creation=False,
    params=PARAMS,
    default_args={"retries": RETRIES, "retry_delay": RETRY_DELAY},
    tags=["etl", "task1"],
) as dag:

    @task(task_id="verify_source")
    def verify_source():
        from etl_task1 import pipeline
        return _run("verify_source", lambda ctx, p: pipeline.verify_source_step(p))

    @task(task_id="extract_load")
    def extract_load():
        from etl_task1 import pipeline
        return _run("extract_load", lambda ctx, p: pipeline.extract_load_step(
            p, ctx["ti"].xcom_pull(task_ids="verify_source")))

    @task(task_id="validate_counts")
    def validate_counts():
        from etl_task1 import pipeline
        return _run("validate_counts", lambda ctx, p: pipeline.validate_counts_step(
            p, ctx["ti"].xcom_pull(task_ids="extract_load")["extraction_id"]))

    @task(task_id="export_results")
    def export_results():
        from etl_task1 import pipeline
        return _run("export_results", lambda ctx, p: pipeline.export_results_step(
            p, ctx["ti"].xcom_pull(task_ids="extract_load")["extraction_id"],
            ctx["ti"].xcom_pull(task_ids="validate_counts")))

    @task(task_id="finalize_report", trigger_rule="all_done")
    def finalize_report():
        from etl_task1 import pipeline
        ctx, params = _params()
        upstream = {n: ctx["ti"].xcom_pull(task_ids=n) for n in pipeline.STEP_NAMES}
        try:
            return pipeline.run_step("finalize_report", params, lambda: pipeline.finalize_report_step(params, upstream))
        except pipeline.StepError as err:
            raise AirflowFailException(str(err)) from err  # 报告写不出来不重试，门禁随后失败

    @task(task_id="run_gate", trigger_rule="all_done")
    def run_gate():
        from etl_task1 import pipeline
        ctx = get_current_context()
        ok, reason = pipeline.gate_decision(ctx["ti"].xcom_pull(task_ids="finalize_report"))
        if not ok:
            raise AirflowFailException(reason)
        return {"gate": reason}

    (verify_source() >> extract_load() >> validate_counts() >> export_results()
     >> finalize_report() >> run_gate())
