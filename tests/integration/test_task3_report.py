"""任务 3 统计报告与导出（spec AC10），以及人工抽查表的导出与导入（AC12 的程序部分）。"""

from __future__ import annotations

import csv
import io
import json

import pytest

from _task3_helpers import setup, standard_products
from etl_task3 import RULE_VERSION, pipeline, report
from etl_task3.pipeline import ClassifyParams

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def out_dir(monkeypatch, tmp_path):
    d = tmp_path / "outputs"
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(d))
    return d


@pytest.fixture
def fx(conn, make_settings, tmp_path):
    return setup(conn, make_settings, tmp_path, standard_products())


def _run(eid, run_key="t3_report"):
    p = ClassifyParams(run_key=run_key, extraction_id=eid)
    checked = pipeline.check_input_step(p)
    up = {"check_input": checked, "classify_products": pipeline.classify_step(p, checked)}
    up["verify_classification"] = pipeline.verify_step(p, eid)
    return pipeline.finalize_step(p, up)


def test_summary_matches_database_and_exports(conn, fx, out_dir):
    final = _run(fx.eid)
    assert final["final_status"] == "succeeded"
    d = out_dir / "task3" / fx.eid / RULE_VERSION / "t3_report"
    s = json.loads((d / "summary.json").read_text())["stats"]
    cs = s["collection_status"]
    assert cs["total_products"] == 10 and cs["distribution"]["success"] == 5 and cs["success_rate"] == 0.5
    assert s["source_kind"]["live_crawl"] == 5 and s["source_kind"]["offline_snapshot"] == 0 \
        and s["source_kind"]["third_party"] == 0
    assert sum(s["input_groups"].values()) == 10 and s["input_groups"]["dp_page"] == 3
    c = s["classification"]
    assert c["categories"] == {"movie": 1, "non_movie": 0, "collection": 0, "tv_series": 1, "concert_other": 0,
                               "pending": 8}
    assert c["by_basis_level"]["movie"]["structured"] == 1 and c["movie_inferred"] == 0
    assert c["recognition_coverage_all_products"] == 0.2
    assert c["recognition_coverage_dp_page"] == round(2 / 3, 6)
    db = dict(conn.execute("SELECT category, count(*) FROM task3.product_classification WHERE extraction_id = %s "
                           "GROUP BY 1", (fx.eid,)).fetchall())
    assert {k: v for k, v in c["categories"].items() if v} == db
    cov = {f["field_key"]: f for f in s["field_coverage"]}
    assert cov["director"]["movie_with_value"] == 1 and cov["director"]["movie_ratio"] == 1.0
    assert cov["title"]["dp_parsed_with_value"] == 3 and cov["title"]["all_products_ratio"] == 0.3
    assert cov["genre"]["dp_parsed_with_value"] == 1 and cov["genre"]["dp_parsed_missing"] == 2
    assert "上映" in cov["product_release_date"]["note"] and all(f["inferred"] == 0 for f in s["field_coverage"])
    r = s["reviews_by_category"]
    assert r["consistent"] and r["records_read"] == 12 and r["records_without_asin"] == 1

    files = {f["path"]: f for f in json.loads((d / "summary.json").read_text())["exports"]}
    assert files["classification.csv"]["rows"] == 10 and files["field_coverage.csv"]["rows"] == 20
    text = (d / "classification.csv").read_text()
    assert "Plain review text" not in text and "<html" not in text
    rows = list(csv.DictReader(io.StringIO(text)))
    assert {r["category"] for r in rows} == {"movie", "tv_series", "pending"}
    assert "## 字段覆盖率" in (d / "report.md").read_text()
    from etl_task1.reporting import file_digest
    assert file_digest(d / "classification.csv")["sha256"] == files["classification.csv"]["sha256"]


def test_review_export_and_import(conn, fx, tmp_path, monkeypatch):
    _run(fx.eid)
    monkeypatch.setattr(report, "DEV_SAMPLE_SIZE", 1)  # 夹具很小：排除 md5 序第 1 个 dp_page 作开发样本
    exported = report.review_export(conn, fx.eid, RULE_VERSION, tmp_path / "review")
    rows = list(csv.DictReader(io.StringIO((tmp_path / "review" / "review_sample.csv").read_text())))
    assert exported["rows"] == len(rows) == 2
    dev = conn.execute("SELECT asin FROM task2.product_manifest m WHERE extraction_id = %s AND status = 'success' "
                       "AND final_url LIKE 'https://www.amazon.com/%%' AND asin IN (SELECT asin FROM "
                       "task3.page_parse) ORDER BY md5(asin) LIMIT 1", (fx.eid,)).fetchone()[0]
    assert dev not in {r["asin"] for r in rows}
    rows[0]["human_category"] = rows[0]["auto_category"]
    rows[1]["human_category"] = "concert_other"
    rows[1]["note"] = "test"
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=report.REVIEW_COLUMNS)
    w.writeheader()
    w.writerows(rows)
    labeled = tmp_path / "labeled.csv"
    labeled.write_text(buf.getvalue())
    got = report.review_import(conn, fx.eid, RULE_VERSION, labeled, "tester")
    assert got["labeled"] == 2 and got["unlabeled"] == 0
    summary = report.review_summary(conn, fx.eid, RULE_VERSION)
    assert summary["reviewed"] == 2 and summary["agree"] == 1 and summary["reviewers"] == ["tester"]
    assert len(summary["mismatches"]) == 1 and summary["mismatches"][0]["human_category"] == "concert_other"
    rows[0]["human_category"] = "bad"
    w = csv.DictWriter(buf := io.StringIO(), fieldnames=report.REVIEW_COLUMNS)
    w.writeheader()
    w.writerows(rows)
    labeled.write_text(buf.getvalue())
    with pytest.raises(report.ReviewImportError):
        report.review_import(conn, fx.eid, RULE_VERSION, labeled, "tester")
    with pytest.raises(report.ReviewImportError):
        report.review_import(conn, fx.eid, RULE_VERSION, labeled, " ")
