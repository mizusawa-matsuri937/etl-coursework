"""任务 4 DAG（IMDb 参考数据）：校验文件 → 装载 → 核对 → 最终门禁。

手动触发，参数 snapshot_id（形如 imdb_2026-10-30），文件在 /data/imdb/<日期>/（宿主机目录只读挂载，见 compose 的 IMDB_HOST_DIR）。
业务逻辑都在 etl_task4.pipeline 与 etl_task4.imdb，DAG 只负责依赖、重试和状态：
- 确定性错误（哈希或大小不符、文件缺失、核对不符）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连、同一快照的锁被占用）按 retries 重试，次数有上限；
- run_gate 无论上游成败都运行，只在三步全部成功时通过；同一快照再次触发时装载步骤返回 reused。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta

from airflow.exceptions import AirflowFailException
from airflow.sdk import DAG, Param, get_current_context, task

RETRIES = int(os.environ.get("ETL_TASK_RETRIES", "2"))
RETRY_DELAY = timedelta(seconds=int(os.environ.get("ETL_RETRY_DELAY_SECONDS", "60")))

PARAMS = {
    "snapshot_id": Param(None, type=["null", "string"],
                         description="IMDb 快照编号，形如 imdb_2026-10-30；文件位于 /data/imdb/<日期>/"),
    "fault_injection": Param(None, type=["null", "string"],
                             description="before_commit 或 transient_once:<标记文件>；仅 ETL_ENV=test"),
}


def _params():
    from etl_task4.pipeline import ImdbParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, ImdbParams(
        run_key=ImdbParams.sanitize(ctx["run_id"]), snapshot_id=p.get("snapshot_id"),
        fault_injection=p.get("fault_injection"), try_number=ctx["ti"].try_number, dag_id=ctx["dag"].dag_id)


def _run(step, fn):
    from etl_task4.pipeline import StepError, run_step

    ctx, params = _params()
    try:
        return run_step(step, params, lambda: fn(ctx, params))
    except StepError as err:
        if err.transient:
            raise  # 交给 Airflow 重试，次数受 retries 限制
        raise AirflowFailException(str(err)) from err


with DAG(
    dag_id="imdb_reference_task4_load",
    description="任务 4：校验并流式装载 IMDb 固定快照（五个文件）",
    schedule=None,
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=False,
    params=PARAMS,
    default_args={"retries": RETRIES, "retry_delay": RETRY_DELAY},
    tags=["etl", "task4"],
) as dag:

    @task(task_id="check_files")
    def check_files():
        from etl_task4 import pipeline
        return _run("check_files", lambda ctx, p: pipeline.check_files_step(p))

    @task(task_id="load_snapshot")
    def load_snapshot():
        from etl_task4 import pipeline
        return _run("load_snapshot", lambda ctx, p: pipeline.load_step(
            p, ctx["ti"].xcom_pull(task_ids="check_files")))

    @task(task_id="verify_snapshot")
    def verify_snapshot():
        from etl_task4 import pipeline
        return _run("verify_snapshot", lambda ctx, p: pipeline.verify_step(
            p, ctx["ti"].xcom_pull(task_ids="check_files")["snapshot_id"]))

    @task(task_id="run_gate", trigger_rule="all_done")
    def run_gate():
        from etl_task4 import pipeline
        ctx, params = _params()
        upstream = {n: ctx["ti"].xcom_pull(task_ids=n) for n in pipeline.IMDB_STEP_NAMES}
        try:
            final = pipeline.run_step("run_gate", params, lambda: pipeline.gate_step(params, upstream))
        except pipeline.StepError as err:
            raise AirflowFailException(str(err)) from err  # 汇总写不出来不重试，门禁失败
        ok, reason = pipeline.gate_decision(final)
        if not ok:
            raise AirflowFailException(reason)
        return {"gate": reason}

    check_files() >> load_snapshot() >> verify_snapshot() >> run_gate()
