"""任务 4 DAG（同一电影识别）：检查输入 → 标准化 → 候选与判定 → 聚类与映射 → 核对 → 收尾报告 → 最终门禁。

手动触发。默认处理最近一次全量清单的抽取、最新已装载的 IMDb 快照、规则 t4r2；输入键相同时记 reused，不新增也不修改目标行。
业务逻辑都在 etl_task4.pipeline 与 etl_task4.runner，DAG 只负责依赖、重试和状态：
- 确定性错误（输入缺失、核对不符、抽取锁被占用、参数不合法等）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连）按 retries 重试，次数有上限；已提交的批次保留，重试只处理剩余批次；
- finalize_report 无论上游成败都运行，run_gate 再判定整体结果。
人工决策集不是参数：它按抽取累积在 task4.human_decision 里（python -m etl_task4.review import），哈希进入输入键。
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
    "t3_rule_version": Param("t3r2", type="string", description="任务 3 分类规则版本"),
    "imdb_snapshot_id": Param(None, type=["null", "string"], description="IMDb 快照编号；默认取最近装载的快照"),
    "norm_version": Param("t4n1", type="string", description="标准化版本"),
    "rule_version": Param("t4r2", type="string", description="匹配规则版本（权重与阈值，见 etl_task4.match.RULESETS；t4r1 是开发池调整前的版本）"),
    "batch_size": Param(500, type="integer", minimum=1, description="每批提交的商品数"),
    "limit": Param(None, type=["null", "integer"], minimum=1,
                   description="本次最多标准化的商品数；只用于试跑，之后的步骤会失败，不生成映射版本"),
    "fault_injection": Param(None, type=["null", "string"],
                             description="before_commit:<阶段>:<批号> 或 transient_once:<标记文件>；仅 ETL_ENV=test"),
}


def _params():
    from etl_task4.pipeline import MatchParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, MatchParams(
        run_key=MatchParams.sanitize(ctx["run_id"]), extraction_id=p.get("extraction_id"),
        t3_rule_version=p["t3_rule_version"], imdb_snapshot_id=p.get("imdb_snapshot_id"),
        norm_version=p["norm_version"], rule_version=p["rule_version"], batch_size=int(p["batch_size"]),
        limit=p.get("limit"), fault_injection=p.get("fault_injection"), try_number=ctx["ti"].try_number,
        dag_id=ctx["dag"].dag_id)


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
    dag_id="amazon_movies_task4_match",
    description="任务 4：标准化、候选与三档判定、聚类与映射，识别同一部电影并生成 movie_id",
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

    @task(task_id="check_inputs")
    def check_inputs():
        from etl_task4 import pipeline
        return _run("check_inputs", lambda ctx, p: pipeline.check_inputs_step(p))

    @task(task_id="normalize")
    def normalize():
        from etl_task4 import pipeline
        return _run("normalize", lambda ctx, p: pipeline.normalize_step(
            p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="candidates_decide")
    def candidates_decide():
        from etl_task4 import pipeline
        return _run("candidates_decide", lambda ctx, p: pipeline.candidates_decide_step(
            p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="cluster_map")
    def cluster_map():
        from etl_task4 import pipeline
        return _run("cluster_map", lambda ctx, p: pipeline.cluster_map_step(
            p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="verify")
    def verify():
        from etl_task4 import pipeline
        return _run("verify", lambda ctx, p: pipeline.match_verify_step(
            p, ctx["ti"].xcom_pull(task_ids="cluster_map")))

    @task(task_id="finalize_report", trigger_rule="all_done")
    def finalize_report():
        from etl_task4 import pipeline
        ctx, params = _params()
        upstream = {n: ctx["ti"].xcom_pull(task_ids=n) for n in pipeline.MATCH_STEP_NAMES}
        try:
            return pipeline.run_step("finalize_report", params, lambda: pipeline.finalize_step(params, upstream))
        except pipeline.StepError as err:
            raise AirflowFailException(str(err)) from err  # 报告写不出来不重试，门禁随后失败

    @task(task_id="run_gate", trigger_rule="all_done")
    def run_gate():
        from etl_task4.pipeline import gate_decision
        ctx = get_current_context()
        ok, reason = gate_decision(ctx["ti"].xcom_pull(task_ids="finalize_report"))
        if not ok:
            raise AirflowFailException(reason)
        return {"gate": reason}

    (check_inputs() >> normalize() >> candidates_decide() >> cluster_map() >> verify()
     >> finalize_report() >> run_gate())
