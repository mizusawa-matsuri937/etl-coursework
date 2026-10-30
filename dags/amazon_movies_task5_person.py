"""任务 5 DAG（人物姓名归一与消歧）：检查输入 → 人物提及 → 外部 ID → 归并与人物 → MoviePerson → 核对 → 收尾报告 → 最终门禁。

手动触发。默认处理最近一次全量清单的抽取、任务 4 映射版本 map0002（不属于该抽取时取该抽取最新的映射版本）、该映射版本所用的 IMDb 快照、规则 t5r1；
输入键相同时记 reused，不新增也不修改目标行。业务逻辑都在 etl_task5.pipeline 与 etl_task5.runner，DAG 只负责依赖、重试和状态：
- 确定性错误（输入缺失、核对不符、抽取锁被占用、参数不合法等）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连）按 retries 重试，次数有上限；已提交的批次保留，重试只处理剩余批次；
- finalize_report 无论上游成败都运行，run_gate 再判定整体结果。
人工决策集不是参数：它按抽取累积在 task5.human_decision 里（python -m etl_task5.review import），哈希进入输入键。
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
    "imdb_snapshot_id": Param(None, type=["null", "string"], description="IMDb 快照编号；默认取映射版本所用的快照"),
    "norm_version": Param("t5n1", type="string", description="姓名规范化版本"),
    "rule_version": Param("t5r1", type="string", description="人物规则版本（等级、得分、阈值与特征开关，见 etl_task5.rules.RULESETS）"),
    "batch_size": Param(500, type="integer", minimum=1, description="每批提交的商品数（分块判定按块批量另计）"),
    "limit": Param(None, type=["null", "integer"], minimum=1,
                   description="本次最多抽取提及的商品数；只用于试跑，之后的步骤会失败，不生成人物映射版本"),
    "fault_injection": Param(None, type=["null", "string"],
                             description="before_commit:<阶段>:<批号> 或 transient_once:<标记文件>；仅 ETL_ENV=test"),
}


def _params():
    from etl_task5.pipeline import PersonParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, PersonParams(
        run_key=PersonParams.sanitize(ctx["run_id"]), extraction_id=p.get("extraction_id"),
        t4_mapping_version_id=p.get("t4_mapping_version_id"), imdb_snapshot_id=p.get("imdb_snapshot_id"),
        norm_version=p["norm_version"], rule_version=p["rule_version"], batch_size=int(p["batch_size"]),
        limit=p.get("limit"), fault_injection=p.get("fault_injection"), try_number=ctx["ti"].try_number,
        dag_id=ctx["dag"].dag_id)


def _run(step, fn):
    from etl_task5.pipeline import StepError, run_step

    ctx, params = _params()
    try:
        return run_step(step, params, lambda: fn(ctx, params))
    except StepError as err:
        if err.transient:
            raise  # 交给 Airflow 重试，次数受 retries 限制
        raise AirflowFailException(str(err)) from err


with DAG(
    dag_id="amazon_movies_task5_person",
    description="任务 5：人物提及、外部人物 ID、归并与消歧、MoviePerson，生成稳定的 person_id",
    schedule=None,
    start_date=datetime(2026, 10, 1),
    catchup=False,
    max_active_runs=1,
    max_active_tasks=1,
    is_paused_upon_creation=False,
    params=PARAMS,
    default_args={"retries": RETRIES, "retry_delay": RETRY_DELAY},
    tags=["etl", "task5"],
) as dag:

    @task(task_id="check_inputs")
    def check_inputs():
        from etl_task5 import pipeline
        return _run("check_inputs", lambda ctx, p: pipeline.check_inputs_step(p))

    @task(task_id="mentions")
    def mentions():
        from etl_task5 import pipeline
        return _run("mentions", lambda ctx, p: pipeline.mentions_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="link_external")
    def link_external():
        from etl_task5 import pipeline
        return _run("link_external", lambda ctx, p: pipeline.link_external_step(
            p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="resolve")
    def resolve():
        from etl_task5 import pipeline
        return _run("resolve", lambda ctx, p: pipeline.resolve_step(p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="movie_person")
    def movie_person():
        from etl_task5 import pipeline
        return _run("movie_person", lambda ctx, p: pipeline.movie_person_step(
            p, ctx["ti"].xcom_pull(task_ids="check_inputs")))

    @task(task_id="verify")
    def verify():
        from etl_task5 import pipeline
        return _run("verify", lambda ctx, p: pipeline.person_verify_step(
            p, ctx["ti"].xcom_pull(task_ids="movie_person")))

    @task(task_id="finalize_report", trigger_rule="all_done")
    def finalize_report():
        from etl_task5 import pipeline
        ctx, params = _params()
        upstream = {n: ctx["ti"].xcom_pull(task_ids=n) for n in pipeline.PERSON_STEP_NAMES}
        try:
            return pipeline.run_step("finalize_report", params, lambda: pipeline.finalize_step(params, upstream))
        except pipeline.StepError as err:
            raise AirflowFailException(str(err)) from err  # 报告写不出来不重试，门禁随后失败

    @task(task_id="run_gate", trigger_rule="all_done")
    def run_gate():
        from etl_task5.pipeline import gate_decision
        ctx = get_current_context()
        ok, reason = gate_decision(ctx["ti"].xcom_pull(task_ids="finalize_report"))
        if not ok:
            raise AirflowFailException(reason)
        return {"gate": reason}

    (check_inputs() >> mentions() >> link_external() >> resolve() >> movie_person() >> verify()
     >> finalize_report() >> run_gate())
