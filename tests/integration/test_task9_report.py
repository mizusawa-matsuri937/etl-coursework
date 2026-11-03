"""任务 9 报告导出与实例（spec AC17；R44、R45）。预期值由测试直接查询数据库重算，不由导出代码生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_report.py
"""

from __future__ import annotations

import hashlib
import json

import pytest

import _task9_helpers as h
from etl_task9.report import TITLES, export_files
from etl_task9.runner import execute_lineage


@pytest.fixture
def exported(conn, make_settings, tmp_path):
    world = h.t9_world()
    mv1 = next(m for m in world.movies if m.name == "Mv1")
    mv1.release.inferred = ("2003-05-20", "day", "media_release")   # 年份缺失、只有发售日期证据的上界（同 AC09）
    t = h.seed(conn, make_settings, tmp_path, world=world)
    h.add_products(conn, t)
    r = execute_lineage(conn, t.inputs(), snapshot_root=t.snapshot_root, snapshot_workers=2, batch_size=500)
    out = tmp_path / "export"
    digests = export_files(conn, r.lineage_version_id, out)
    t.lv, t.dir, t.digests = r.lineage_version_id, out, digests
    t.summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    return t


def test_ac17_summary_counts_match_database(conn, exported):
    """summary.json 的源记录、断言、派生边、选择与闭包计数等于数据库重算；覆盖率按实体、字段与 trace_status 分列，
    且字段分列之和等于总数。"""
    s = exported.summary
    k = s["stage_keys"]
    assert s["counts"]["source_records"] == dict(conn.execute(
        "SELECT source_kind, count(*) FROM task9.source_record WHERE stage_key = %s GROUP BY 1", (k["sources"],)).fetchall())
    assert s["counts"]["assertions"] == dict(conn.execute(
        "SELECT level, count(*) FROM task9.assertion WHERE stage_key = %s GROUP BY 1", (k["assertions"],)).fetchall())
    assert s["counts"]["derivations"] == dict(conn.execute(
        "SELECT operation, count(*) FROM task9.derivation WHERE stage_key = %s GROUP BY 1", (k["derivations"],)).fetchall())
    assert s["counts"]["field_impact"] == conn.execute("SELECT count(*) FROM task9.field_impact WHERE stage_key = %s",
                                                       (k["impact"],)).fetchone()[0]
    total, non_null = conn.execute("SELECT count(*), count(value_text) FROM task9.field_selection WHERE stage_key = %s",
                                   (k["selections"],)).fetchone()
    assert sum(v["selections"] for v in s["coverage_by_field"].values()) == total
    assert sum(v["non_null"] for v in s["coverage_by_entity"].values()) == non_null == s["counts"]["non_null"]
    lg = s["coverage_by_field"]["movie.release_year"]
    assert sum(lg["trace_status"].values()) == lg["selections"]
    assert set(s["coverage_by_entity"]) >= {"movie", "edition", "person", "product_movie"}
    assert s["review_view_mapping_status"] and s["status"] == "verified"


def test_ac17_examples_cover_four_queries_and_assignment_cases(exported):
    """examples.md 覆盖 field、product、review、source、person 五个演示查询（各至少 3 例或说明）与四类作业示例。"""
    text = (exported.dir / "examples.md").read_text(encoding="utf-8")
    for title in TITLES.values():
        assert f"## {title}" in text
    for title in ("查询 field", "查询 product", "查询 source", "查询 person"):
        sec = text.split(f"## {TITLES[[k for k, v in TITLES.items() if v.startswith(title)][0]]}")[1].split("\n## ")[0]
        assert sec.count("### 例") >= 2, title
    for k in ("third_party_adopted", "alias_merged", "year_missing_bound_only"):
        sec = text.split(f"## {TITLES[k]}")[1].split("\n## ")[0]
        assert "### 例 1" in sec, k


def test_ac17_no_review_text_or_html_and_checksums_match(conn, exported):
    """导出不含评论正文与整页 HTML；checksums 与文件实际 SHA-256 一致。"""
    texts = [r[0] for r in conn.execute("SELECT review_text_raw FROM public.review_staging WHERE extraction_id = %s "
                                        "AND length(review_text_raw) > 10", (exported.eid,))]
    assert texts
    for name, d in exported.digests.items():
        data = (exported.dir / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == d["sha256"]
        body = data.decode("utf-8")
        assert "<html" not in body.lower() and not any(t in body for t in texts)
    assert json.loads((exported.dir / "checksums.json").read_text())["summary.json"]["sha256"] == \
        exported.digests["summary.json"]["sha256"]
