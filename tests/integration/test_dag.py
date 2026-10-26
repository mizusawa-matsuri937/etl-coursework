"""DAG 定义检查。真实 scheduler 触发的成功/失败运行见 checklist 记录的证据（scripts/run_dag_e2e.py）。"""

from pathlib import Path

import pytest

DAGS = Path("/opt/etl/dags")


@pytest.fixture(scope="module")
def dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["amazon_reviews_task1"]


def test_six_tasks_in_dependency_order(dag):
    order = ["verify_source", "extract_load", "validate_counts", "export_results", "finalize_report", "run_gate"]
    assert set(dag.task_dict) == set(order)
    for a, b in zip(order, order[1:]):
        assert dag.task_dict[a].downstream_task_ids == {b}


def _rule(dag, task_id):
    rule = dag.task_dict[task_id].trigger_rule
    return getattr(rule, "value", rule)


def test_finalize_and_gate_run_regardless_of_upstream(dag):
    assert _rule(dag, "finalize_report") == "all_done" and _rule(dag, "run_gate") == "all_done"
    assert _rule(dag, "extract_load") == "all_success"


def test_manual_trigger_concurrency_and_retry_limits(dag):
    assert dag.schedule is None and dag.catchup is False
    assert dag.max_active_runs == 1 and dag.max_active_tasks == 1
    assert all(t.retries == 2 for t in dag.tasks)
    assert {"scope", "sample_max_records", "source_gzip_path", "baseline_explanation"} <= set(dag.params)


def test_dag_file_does_not_import_etl_code_at_parse_time():
    text = (DAGS / "amazon_reviews_task1.py").read_text()
    top = text.split("with DAG(")[0]
    assert "from etl_task1" not in top.replace("    from etl_task1", "")
