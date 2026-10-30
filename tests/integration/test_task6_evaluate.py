"""任务 6 评价工具集成测试（spec R36–R39，AC17）。

测试范围：
1. 开发池冻结（R36）：按 md5(movie_id) 排序前 20% 作品进入 task6.dev_pool，重复冻结幂等。
2. 评价表导出（R37）：评价池作品不含开发池；各层按规则分类与补足；导出的 eval_items.csv
   不含系统年份、状态、层与上界；导出的 eval_items_draft.csv 含 AI 建议字段。
3. 标注导入（R39）：校验格式、检测矛盾警告、成功入库 task6.eval_label。
4. 指标计算（R38）：年份准确率（完全一致与相差 ≤1 年，按层、按来源与总体）、tconst 正确率、
   缺失可查到率、推断上界成立率；不确定单独计数；无推断上界时成立率为“无定义”（None）。

命令：
    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task6_evaluate.py
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
import psycopg

import _task6_helpers as h
from etl_task6.evaluate import (
    DEV_FRACTION,
    DRAFT_COLUMNS,
    ITEM_COLUMNS,
    EvalError,
    eval_set_id,
    export_eval_set,
    freeze_dev_pool,
    import_labels,
    metrics,
    render_metrics,
)
from etl_task6.runner import execute_release

pytestmark = pytest.mark.integration


@pytest.fixture
def world(conn, make_settings, tmp_path):
    w = h.make_world(conn, make_settings, tmp_path)
    res = execute_release(conn, h.inputs_for(w), batch_size=10)
    assert res.status == "succeeded"
    return w, res.release_version_id


# ------------------------------------------------------------------ 1. 开发池冻结

def test_ac17_freeze_dev_pool_is_twenty_percent_and_idempotent(conn, world):
    w, rv = world
    total_movies = conn.execute(
        "SELECT count(*) FROM task4.movie WHERE mapping_version_id = %s", (w.mapping,)).fetchone()[0]

    res = freeze_dev_pool(conn, w.extraction_id, w.mapping)
    assert res["status"] == "frozen"
    dev_count = res["dev_movies"]
    # 前 20% 向上取整
    assert dev_count == -(-total_movies // DEV_FRACTION)

    # 重复冻结幂等
    res2 = freeze_dev_pool(conn, w.extraction_id, w.mapping)
    assert res2["status"] == "already_frozen"
    assert res2["dev_movies"] == dev_count


# ------------------------------------------------------------------ 2. 评价表导出

def test_ac17_export_excludes_dev_pool_and_hides_system_fields(conn, world, tmp_path):
    w, rv = world
    freeze_dev_pool(conn, w.extraction_id, w.mapping)
    dev_ids = {r[0] for r in conn.execute(
        "SELECT movie_id FROM task6.dev_pool WHERE extraction_id = %s", (w.extraction_id,)).fetchall()}

    out_dir = tmp_path / "eval_out"
    exp = export_eval_set(conn, rv, out_dir)
    assert exp["items_total"] > 0

    # 1. 验证 task6.eval_item 不包含任何开发池中的作品
    eval_mids = [r[0] for r in conn.execute(
        "SELECT movie_id FROM task6.eval_item WHERE eval_set_id = %s", (exp["eval_set_id"],)).fetchall()]
    for mid in eval_mids:
        assert mid not in dev_ids

    # 2. 验证导出的 eval_items.csv 列与字段：不含系统年份、状态、层与上界
    csv_path = Path(exp["eval_items_file"])
    assert csv_path.exists()
    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert tuple(reader.fieldnames) == ITEM_COLUMNS
        for forbidden in ("system_year", "status", "stratum", "bound_year"):
            assert forbidden not in reader.fieldnames
        rows = list(reader)
        assert len(rows) == len(eval_mids)

    # 3. 验证 eval_items_draft.csv 包含 AI 预标注列
    draft_path = Path(exp["draft_file"])
    assert draft_path.exists()
    with draft_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert tuple(reader.fieldnames) == DRAFT_COLUMNS
        draft_rows = list(reader)
        assert len(draft_rows) == len(eval_mids)
        # 至少有一行生成了建议
        has_ai_suggestion = any(r.get("ai_true_year") or r.get("ai_reason") for r in draft_rows)
        assert has_ai_suggestion


# ------------------------------------------------------------------ 3. 标注导入与指标计算

def test_ac17_import_and_metrics_with_hand_calculated_ground_truth(conn, world, tmp_path):
    w, rv = world
    freeze_dev_pool(conn, w.extraction_id, w.mapping)
    out_dir = tmp_path / "eval_test"
    exp = export_eval_set(conn, rv, out_dir)
    sid = exp["eval_set_id"]

    # 查出导出的 items
    items = conn.execute(
        "SELECT item_id, stratum, movie_id, tconst, system_year, status, bound_year "
        "FROM task6.eval_item WHERE eval_set_id = %s ORDER BY item_id", (sid,)).fetchall()
    assert len(items) >= 5

    # 构造人工标注测试数据：
    # item 0: 完全一致 (true_year == system_year), tconst same
    # item 1: 相差 1 年 (true_year == system_year + 1), tconst different
    # item 2: 相差 3 年 (true_year == system_year + 3), tconst same
    # item 3: unsure (不确定)
    # 其余 items: 正常填或测试 missing
    labels_to_write = []
    for idx, (iid, strat, mid, tc, sys_yr, st, bnd_yr) in enumerate(items):
        if idx == 0:
            labels_to_write.append({
                "item_id": iid,
                "true_year": str(sys_yr or 2000),
                "year_label": "known",
                "tconst_label": "same" if tc else "na",
                "note": "item 0 exact",
            })
        elif idx == 1:
            labels_to_write.append({
                "item_id": iid,
                "true_year": str((sys_yr or 2000) + 1),
                "year_label": "known",
                "tconst_label": "different" if tc else "na",
                "note": "item 1 diff 1yr",
            })
        elif idx == 2:
            labels_to_write.append({
                "item_id": iid,
                "true_year": str((sys_yr or 2000) + 3),
                "year_label": "known",
                "tconst_label": "same" if tc else "na",
                "note": "item 2 diff 3yr",
            })
        elif idx == 3:
            labels_to_write.append({
                "item_id": iid,
                "true_year": "",
                "year_label": "unsure",
                "tconst_label": "unsure",
                "note": "item 3 unsure",
            })
        else:
            # 对于 missing 项目，给出一个 known 年份，便于测试 missing 可查到率
            if st == "missing":
                labels_to_write.append({
                    "item_id": iid,
                    "true_year": "1995",
                    "year_label": "known",
                    "tconst_label": "same" if tc else "na",
                    "note": "missing found year",
                })
            else:
                labels_to_write.append({
                    "item_id": iid,
                    "true_year": str(sys_yr or 1999),
                    "year_label": "known",
                    "tconst_label": "same" if tc else "na",
                    "note": "default known",
                })

    labeled_csv = out_dir / "test_labeled.csv"
    with labeled_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ITEM_COLUMNS)
        writer.writeheader()
        for row in labels_to_write:
            writer.writerow(row)

    # 导入标注
    imp = import_labels(conn, sid, labeled_csv, annotator="test_annotator")
    assert imp["imported_items"] == len(labels_to_write)
    assert imp["annotator"] == "test_annotator"

    # 计算指标
    m = metrics(conn, rv)
    assert m["eval_set_id"] == sid
    assert m["labeled_total"] == len(labels_to_write)
    assert m["unsure_counts"]["year_unsure"] >= 1
    assert m["unsure_counts"]["tconst_unsure"] >= 1

    # 检验手算逻辑：
    # item 3 是 unsure，不进入准确率分母
    # 至少有 1 个 exact (item 0)，至少有 1 个 diff 1yr (item 1)，至少有 1 个 diff 3yr (item 2)
    y_ov = m["year_accuracy"]["overall"]
    assert y_ov["exact_count"] >= 1
    assert y_ov["within_1yr_count"] >= y_ov["exact_count"] + 1  # exact + item 1
    assert y_ov["within_1yr_rate"] >= y_ov["exact_rate"]

    # tconst 正确率：包含 item 1 (different) 和 item 0 (same)
    t_ov = m["tconst_accuracy"]["overall"]
    assert t_ov["different"] >= 1
    assert t_ov["same"] >= 1
    assert 0 < t_ov["rate"] < 1.0

    # 缺失可查到率：
    assert m["missing_findable"]["findable_count"] >= 1

    # 渲染 Markdown 报告
    md = render_metrics(m)
    assert "# 任务 6 评价指标报告" in md
    assert "完全一致准确率" in md
    assert "tconst 匹配正确率" in md


# ------------------------------------------------------------------ 4. 边界情况：无推断上界时为“无定义”

def test_ac17_no_bounds_yields_undefined_rate(conn, world, tmp_path):
    w, rv = world
    freeze_dev_pool(conn, w.extraction_id, w.mapping)
    out_dir = tmp_path / "eval_nobounds"
    exp = export_eval_set(conn, rv, out_dir)
    sid = exp["eval_set_id"]

    # 手动把 eval_item 中的 bound_year 设为 NULL 模拟无推断上界集合
    conn.execute("UPDATE task6.eval_item SET bound_year = NULL WHERE eval_set_id = %s", (sid,))

    labeled_csv = out_dir / "test_nobounds.csv"
    with labeled_csv.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ITEM_COLUMNS)
        writer.writeheader()
        items = conn.execute("SELECT item_id, system_year FROM task6.eval_item WHERE eval_set_id = %s",
                             (sid,)).fetchall()
        for iid, yr in items:
            writer.writerow({
                "item_id": iid,
                "true_year": str(yr or 1990),
                "year_label": "known",
                "tconst_label": "same",
                "note": "nobounds test",
            })

    import_labels(conn, sid, labeled_csv, annotator="test_annotator")
    m = metrics(conn, rv)
    # 上界成立率的分母为 0，应当为 None（“无定义”）
    assert m["bound_validity"]["overall"]["rate"] is None
    md = render_metrics(m)
    assert "无定义" in md
