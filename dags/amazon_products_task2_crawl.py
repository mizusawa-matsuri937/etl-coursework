"""任务 2 第二部分 DAG：检查清单 → 爬取产品页面 → 核对 → 收尾报告 → 最终门禁。

手动触发，每次最多处理 limit 个 ASIN、最多运行 deadline_minutes 分钟；中断或重跑都从清单里未处理的行续接。
业务逻辑都在 etl_task2.crawl.pipeline，DAG 只负责依赖、重试和状态：
- 确定性错误（参数不合法、没有清单、核对不符等）抛 AirflowFailException，不重试；
- 暂时错误（数据库断连、同一抽取的爬虫锁被占用）按 retries 重试，次数有上限；
- finalize_report 无论上游成败都运行，run_gate 再判定整体结果。
fetch_mode=fixture 用离线假请求，只允许在 ETL_ENV=test（compose.e2e.yaml）下使用。
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
    "limit": Param(1000, type=["null", "integer"], minimum=1, description="本次最多处理的 ASIN 数；null 表示不限"),
    "deadline_minutes": Param(60, type=["null", "number"], minimum=0.01, description="本次最多运行的分钟数"),
    "concurrency": Param(40, type="integer", minimum=1, description="并发 worker 数，约为出口数 × 3"),
    "rate": Param(0.4, type="number", exclusiveMinimum=0, description="每个出口每秒请求数上限"),
    "timeout": Param(15, type="number", exclusiveMinimum=0, description="单次请求超时秒数"),
    "retry_statuses": Param([], type="array",
                            items={"type": "string", "enum": ["access_restricted", "timeout", "parse_failed"]},
                            description="除 not_collected 外，重新采集这些状态的行"),
    "fetch_mode": Param("live", type="string", enum=["live", "fixture"],
                        description="live 真实请求；fixture 离线假请求，仅 ETL_ENV=test"),
}


def _params():
    from etl_task2.crawl.pipeline import CrawlParams

    ctx = get_current_context()
    p = ctx["params"]
    return ctx, CrawlParams(
        run_key=CrawlParams.sanitize(ctx["run_id"]), extraction_id=p.get("extraction_id"), limit=p.get("limit"),
        deadline_minutes=p.get("deadline_minutes"), concurrency=int(p["concurrency"]), rate=float(p["rate"]),
        timeout=float(p["timeout"]), retry_statuses=tuple(p.get("retry_statuses") or ()),
        fetch_mode=p["fetch_mode"], try_number=ctx["ti"].try_number, dag_id=ctx["dag"].dag_id)


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
    dag_id="amazon_products_task2_crawl",
    description="任务 2 第二部分：按采集清单爬取产品页面，写回状态与证据",
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

    @task(task_id="check_crawl_input")
    def check_crawl_input():
        from etl_task2.crawl import pipeline
        return _run("check_crawl_input", lambda ctx, p: pipeline.check_crawl_input_step(p))

    @task(task_id="crawl_pages")
    def crawl_pages():
        from etl_task2.crawl import pipeline
        return _run("crawl_pages", lambda ctx, p: pipeline.crawl_pages_step(
            p, ctx["ti"].xcom_pull(task_ids="check_crawl_input")))

    @task(task_id="verify_crawl")
    def verify_crawl():
        from etl_task2.crawl import pipeline
        return _run("verify_crawl", lambda ctx, p: pipeline.verify_crawl_step(
            p, ctx["ti"].xcom_pull(task_ids="check_crawl_input")["extraction_id"],
            ctx["ti"].xcom_pull(task_ids="crawl_pages")))

    @task(task_id="finalize_report", trigger_rule="all_done")
    def finalize_report():
        from etl_task2.crawl import pipeline
        from etl_task2.pipeline import StepError, run_step
        ctx, params = _params()
        upstream = {n: ctx["ti"].xcom_pull(task_ids=n) for n in pipeline.STEP_NAMES}
        try:
            return run_step("finalize_report", params, lambda: pipeline.finalize_step(params, upstream))
        except StepError as err:
            raise AirflowFailException(str(err)) from err  # 报告写不出来不重试，门禁随后失败

    @task(task_id="run_gate", trigger_rule="all_done")
    def run_gate():
        from etl_task2.pipeline import gate_decision
        ctx = get_current_context()
        ok, reason = gate_decision(ctx["ti"].xcom_pull(task_ids="finalize_report"))
        if not ok:
            raise AirflowFailException(reason)
        return {"gate": reason}

    check_crawl_input() >> crawl_pages() >> verify_crawl() >> finalize_report() >> run_gate()
