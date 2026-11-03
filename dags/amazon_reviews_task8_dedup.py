"""任务 8 DAG（评论清洗与重复识别）：检查输入 → 清洗 → 评论家族 → 去重 → 归属与统计组 → 物化版本 → 核对 → 收尾报告 → 最终门禁。

手动触发。默认处理最近一次全量清单的抽取、任务 3 规则 t3r2、任务 7 冲突消解版本 cv0003、规则 t8r1；输入键相同且已通过核对时
记 reused，不新增也不修改目标行。业务逻辑都在 etl_task8.pipeline 与 etl_task8.runner，DAG 只负责依赖、重试和状态：
- 确定性错误（输入缺失、核对不符、抽取锁被占用、参数不合法等）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连）按 retries 重试，次数有上限；已提交的批次单元保留，重试只处理剩余单元；
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
    "t3_rule_version": Param("t3r2", type="string", description="任务 3 分类规则版本"),
    "conflict_version_id": Param("cv0003", type="string", description="任务 7 冲突消解版本（须已 verified）"),
    "rule_version": Param("t8r1", type="string", description="去重规则版本（见 etl_task8.rules.RULESETS）"),
    "batch_size": Param(500000, type="integer", minimum=1, description="review_clean 每批的记录号区间长度"),
    "partitions": Param(32, type="integer", minimum=1, maximum=1024,
                        description="review_family 与 review_dedup 的用户或 ASIN 哈希分区数"),
    "fault_injection": Param(None, type=["null", "string"],
                             description="before_commit:<阶段>:<批号>、transient_once:<标记文件> 或 verify_fail；仅 ETL_ENV=test"),
}


def _params():
    from etl_task8.pipeline import DedupParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, DedupParams(
        run_key=DedupParams.sanitize(ctx["run_id"]), extraction_id=p.get("extraction_id"),
        t3_rule_version=p["t3_rule_version"], conflict_version_id=p["conflict_version_id"],
        rule_version=p["rule_version"], batch_size=int(p["batch_size"]), partitions=int(p["partitions"]),
        fault_injection=p.get("fault_injection"), try_number=ctx["ti"].try_number, dag_id=ctx["dag"].dag_id)


def _run(step, fn):
    from etl_task8.pipeline import StepError, run_step

    ctx, params = _params()
    try:
        return run_step(step, params, lambda: fn(ctx, params))
    except StepError as err:
        if err.transient:
            raise  # 交给 Airflow 重试，次数受 retries 限制
        raise AirflowFailException(str(err)) from err


with DAG(
    dag_id="amazon_reviews_task8_dedup",
    description="任务 8：评论清洗与重复识别，生成逻辑评论、ReviewMapping、作品归属与评分统计并物化去重版本",
    schedule=None,
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=False,
    params=PARAMS,
    default_args={"retries": RETRIES, "retry_delay": RETRY_DELAY},
    tags=["etl", "task8"],
) as dag:

    @task(task_id="check_inputs")
    def check_inputs():
        from etl_task8 import pipeline
        return _run("check_inputs", lambda ctx, p: pipeline.check_inputs_step(p))

    def _step(step: str):
        def body():
            from etl_task8 import pipeline
            return _run(step, lambda ctx, p: pipeline.stage_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs"), step))
        return body

    review_clean = task(task_id="review_clean")(_step("review_clean"))
    review_family = task(task_id="review_family")(_step("review_family"))
    review_dedup = task(task_id="review_dedup")(_step("review_dedup"))
    review_attribution = task(task_id="review_attribution")(_step("review_attribution"))
    finalize_version = task(task_id="finalize_version")(_step("finalize_version"))
    verify = task(task_id="verify")(_step("verify"))

    @task(task_id="finalize_report", trigger_rule="all_done")
    def finalize_report():
        from etl_task8 import pipeline
        ti = get_current_context()["ti"]
        upstream = {s: ti.xcom_pull(task_ids=s) for s in pipeline.DEDUP_STEP_NAMES}
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
        >> review_clean()
        >> review_family()
        >> review_dedup()
        >> review_attribution()
        >> finalize_version()
        >> verify()
        >> finalize_report()
        >> gate()
    )
