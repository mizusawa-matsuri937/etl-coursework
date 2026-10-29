"""任务 2 清单 DAG 定义检查。真实 scheduler 触发的运行见 checklist 记录的证据（scripts/run_dag_e2e.py）。"""

from pathlib import Path

import pytest

DAGS = Path("/opt/etl/dags")
ORDER = ["check_extraction", "build_manifest", "verify_manifest", "export_manifest", "finalize_report", "run_gate"]


@pytest.fixture(scope="module")
def dag():
    pytest.importorskip("airflow")
    from airflow.dag_processing.dagbag import DagBag

    bag = DagBag(dag_folder=str(DAGS))
    assert not bag.import_errors, bag.import_errors
    return bag.dags["amazon_products_task2_manifest"]


def _rule(dag, task_id):
    rule = dag.task_dict[task_id].trigger_rule
    return getattr(rule, "value", rule)


def test_six_tasks_in_dependency_order(dag):
    assert set(dag.task_dict) == set(ORDER)
    for a, b in zip(ORDER, ORDER[1:]):
        assert dag.task_dict[a].downstream_task_ids == {b}


def test_finalize_and_gate_run_regardless_of_upstream(dag):
    assert _rule(dag, "finalize_report") == "all_done" and _rule(dag, "run_gate") == "all_done"
    assert all(_rule(dag, t) == "all_success" for t in ORDER[1:4])


def test_manual_trigger_concurrency_retry_limits_and_params(dag):
    assert dag.schedule is None and dag.catchup is False
    assert dag.max_active_runs == 1 and dag.max_active_tasks == 1
    assert all(t.retries == 2 for t in dag.tasks)
    assert set(dag.params) == {"extraction_id", "allow_sample", "fault_injection"}
    assert dag.params["allow_sample"] is False


def test_dag_file_does_not_import_etl_code_at_parse_time():
    top = (DAGS / "amazon_products_task2_manifest.py").read_text().split("with DAG(")[0]
    assert "from etl_task2" not in top.replace("    from etl_task2", "")
