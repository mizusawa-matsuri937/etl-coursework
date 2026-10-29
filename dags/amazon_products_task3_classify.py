"""任务 3 DAG：检查输入 → 解析与分类 → 核对 → 收尾报告 → 最终门禁。

手动触发。默认处理全部 dp_page 页面；limit 只用于试跑，限定运行的核对会失败、门禁失败，不冒充全量。
中断或重跑都只处理本规则版本还没有分类的 ASIN。业务逻辑都在 etl_task3.pipeline，DAG 只负责依赖、重试和状态：
- 确定性错误（参数不合法、没有清单、核对不符等）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连、同一版本的锁被占用）按 retries 重试，次数有上限；
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
    "limit": Param(None, type=["null", "integer"], minimum=1,
                   description="本次最多处理的 dp_page 数；只用于试跑，限定运行门禁会失败"),
    "batch_size": Param(500, type="integer", minimum=1, description="每批提交的 dp_page 数"),
    "fault_injection": Param(None, type=["null", "string"],
                             description="before_commit:<批号> 或 transient_once:<标记文件>；仅 ETL_ENV=test"),
}


def _params():
    from etl_task3.pipeline import ClassifyParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, ClassifyParams(
        run_key=ClassifyParams.sanitize(ctx["run_id"]), extraction_id=p.get("extraction_id"), limit=p.get("limit"),
        batch_size=int(p["batch_size"]), fault_injection=p.get("fault_injection"),
        try_number=ctx["ti"].try_number, dag_id=ctx["dag"].dag_id)


def _run(step, fn):
    from etl_task3.pipeline import StepError, run_step

    ctx, params = _params()
    try:
        return run_step(step, params, lambda: fn(ctx, params))
    except StepError as err:
        if err.transient:
            raise  # 交给 Airflow 重试，次数受 retries 限制
        raise AirflowFailException(str(err)) from err


with DAG(
    dag_id="amazon_products_task3_classify",
    description="任务 3：从产品页快照抽取原始字段，按规则给每个产品分类并统计",
    schedule=None,
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=False,
    params=PARAMS,
    default_args={"retries": RETRIES, "retry_delay": RETRY_DELAY},
    tags=["etl", "task3"],
) as dag:

    @task(task_id="check_input")
    def check_input():
        from etl_task3 import pipeline
        return _run("check_input", lambda ctx, p: pipeline.check_input_step(p))

    @task(task_id="classify_products")
    def classify_products():
        from etl_task3 import pipeline
        return _run("classify_products", lambda ctx, p: pipeline.classify_step(
            p, ctx["ti"].xcom_pull(task_ids="check_input")))

    @task(task_id="verify_classification")
    def verify_classification():
        from etl_task3 import pipeline
        return _run("verify_classification", lambda ctx, p: pipeline.verify_step(
            p, ctx["ti"].xcom_pull(task_ids="check_input")["extraction_id"]))

    @task(task_id="finalize_report", trigger_rule="all_done")
    def finalize_report():
        from etl_task3 import pipeline
        ctx, params = _params()
        upstream = {n: ctx["ti"].xcom_pull(task_ids=n) for n in pipeline.STEP_NAMES}
        try:
            return pipeline.run_step("finalize_report", params, lambda: pipeline.finalize_step(params, upstream))
        except pipeline.StepError as err:
            raise AirflowFailException(str(err)) from err  # 报告写不出来不重试，门禁随后失败

    @task(task_id="run_gate", trigger_rule="all_done")
    def run_gate():
        from etl_task3.pipeline import gate_decision
        ctx = get_current_context()
        ok, reason = gate_decision(ctx["ti"].xcom_pull(task_ids="finalize_report"))
        if not ok:
            raise AirflowFailException(reason)
        return {"gate": reason}

    check_input() >> classify_products() >> verify_classification() >> finalize_report() >> run_gate()
