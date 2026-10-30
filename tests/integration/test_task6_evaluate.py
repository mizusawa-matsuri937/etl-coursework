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
import hashlib
from pathlib import Path

import pytest
import psycopg

import _task6_helpers as h
from etl_task6.evaluate import (
    DEV_FRACTION,
    DRAFT_COLUMNS,
    ITEM_COLUMNS,
    EvalError,
    _classify_movie_stratum,
    _generate_ai_draft,
    build_eval_candidates,
    compute_eval_metrics,
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

    # 构造人工标注测试数据（由于 item_id 已按 md5 全局打乱，从有 system_year 和 tconst 的项目中取前 4 个分别构造）：
    # role 0: 完全一致 (true_year == system_year), tconst same
    # role 1: 相差 1 年 (true_year == system_year + 1), tconst different
    # role 2: 相差 3 年 (true_year == system_year + 3), tconst same
    # role 3: unsure (不确定)
    # 其余 items: 正常填或测试 missing
    with_tc_ids = [iid for iid, _, _, tc, sys_yr, _, _ in items if sys_yr is not None and tc is not None]
    assert len(with_tc_ids) >= 4
    role_of = {with_tc_ids[0]: 0, with_tc_ids[1]: 1, with_tc_ids[2]: 2, with_tc_ids[3]: 3}

    labels_to_write = []
    for iid, strat, mid, tc, sys_yr, st, bnd_yr in items:
        role = role_of.get(iid)
        if role == 0:
            labels_to_write.append({
                "item_id": iid,
                "true_year": str(sys_yr),
                "year_label": "known",
                "tconst_label": "same",
                "note": "item exact",
            })
        elif role == 1:
            labels_to_write.append({
                "item_id": iid,
                "true_year": str(sys_yr + 1),
                "year_label": "known",
                "tconst_label": "different",
                "note": "item diff 1yr",
            })
        elif role == 2:
            labels_to_write.append({
                "item_id": iid,
                "true_year": str(sys_yr + 3),
                "year_label": "known",
                "tconst_label": "same",
                "note": "item diff 3yr",
            })
        elif role == 3:
            labels_to_write.append({
                "item_id": iid,
                "true_year": "",
                "year_label": "unsure",
                "tconst_label": "unsure",
                "note": "item unsure",
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

    # 细分指标断言：按层（by_stratum）、按来源（by_source）、按上界证据类型（by_evidence / by_evidence_type）
    assert "by_stratum" in m["year_accuracy"] and m["by_stratum"] == m["year_accuracy"]["by_stratum"]
    assert "by_source" in m["year_accuracy"] and m["by_source"] == m["year_accuracy"]["by_source"]
    assert "by_source" in m["tconst_accuracy"]
    assert "by_evidence" in m["bound_validity"]
    assert m["bound_validity"]["by_evidence_type"] == m["bound_validity"]["by_evidence"]
    assert m["by_evidence_type"] == m["bound_validity"]["by_evidence"]
    assert set(m["bound_validity"]["by_evidence"].keys()) == {"review", "media_release"}

    # 逐层核对：各层 year_accuracy 的 total 之和等于 overall.total，exact_count 之和等于 overall.exact_count
    stratum_totals = sum(v["total"] for v in m["year_accuracy"]["by_stratum"].values())
    stratum_exacts = sum(v["exact_count"] for v in m["year_accuracy"]["by_stratum"].values())
    stratum_w1s = sum(v["within_1yr_count"] for v in m["year_accuracy"]["by_stratum"].values())
    assert stratum_totals == y_ov["total"]
    assert stratum_exacts == y_ov["exact_count"]
    assert stratum_w1s == y_ov["within_1yr_count"]

    # 逐来源核对：各来源 year_accuracy 的 total 之和等于 overall.total，tconst_accuracy 的 same/different 之和等于 overall
    source_totals = sum(v["total"] for v in m["year_accuracy"]["by_source"].values())
    assert source_totals == y_ov["total"]
    tc_same_sum = sum(v["same"] for v in m["tconst_accuracy"]["by_source"].values())
    tc_diff_sum = sum(v["different"] for v in m["tconst_accuracy"]["by_source"].values())
    assert tc_same_sum == t_ov["same"]
    assert tc_diff_sum == t_ov["different"]

    # 上界证据类型核对：review 与 media_release 的 total/valid 之和等于 bound_validity.overall
    ev_totals = sum(v["total"] for v in m["bound_validity"]["by_evidence"].values())
    ev_valids = sum(v["valid"] for v in m["bound_validity"]["by_evidence"].values())
    assert ev_totals == m["bound_validity"]["overall"]["total"]
    assert ev_valids == m["bound_validity"]["overall"]["valid"]

    # compute_eval_metrics 别名返回一致结果
    assert compute_eval_metrics(conn, rv) == m

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


# ------------------------------------------------------------------ 5. AC23：分层键名修复（E1/E2/E3–E7）

def test_ac23_stratum_classification_tvmovie_video_and_disagreement_keys(conn, world):
    w, rv = world
    # 1. 单元级直接验证 _classify_movie_stratum 对新旧键名的支持
    # E1：movie 且无任何分歧或上界冲突
    assert _classify_movie_stratum(
        ("verified", {"year_diff_1": [], "year_disagreement": [], "tconst_disagreement": []}),
        ("imdb_t4_link", {"title_type": "movie"}, 1999),
        None,
        False,
    ) == "E1"
    # E2：title_type = tvMovie 或 video（含旧键名 titleType）
    assert _classify_movie_stratum(
        ("verified", {}), ("imdb_t4_link", {"title_type": "tvMovie"}, 2004), None, False
    ) == "E2"
    assert _classify_movie_stratum(
        ("verified", {}), ("imdb_t4_link", {"title_type": "video"}, 2001), None, False
    ) == "E2"
    assert _classify_movie_stratum(
        ("verified", {}), ("imdb_t4_link", {"titleType": "tvMovie"}, 2004), None, False
    ) == "E2"
    # E2：三种分歧键名 year_diff_1 / year_disagreement / tconst_disagreement
    assert _classify_movie_stratum(
        ("verified", {"year_diff_1": [10]}), ("imdb_t4_link", {"title_type": "movie"}, 1990), None, False
    ) == "E2"
    assert _classify_movie_stratum(
        ("verified", {"year_disagreement": [{"candidate_id": 11, "diff": 4}]}),
        ("imdb_t4_link", {"title_type": "movie"}, 1990),
        None,
        False,
    ) == "E2"
    assert _classify_movie_stratum(
        ("verified", {"tconst_disagreement": ["tt9999999"]}),
        ("imdb_t4_link", {"title_type": "movie"}, 1990),
        None,
        False,
    ) == "E2"
    # E3–E7
    assert _classify_movie_stratum(("hint_matched", {"tconst_disagreement": ["tt1"]}), None, None, False) == "E3"
    assert _classify_movie_stratum(("cooccur_matched", {}), None, None, False) == "E4"
    assert _classify_movie_stratum(("page_reported", {}), None, None, False) == "E5"
    assert _classify_movie_stratum(("missing", {}), None, ("2001-01-01", "media_release"), False) == "E6"
    assert _classify_movie_stratum(("missing", {}), None, None, False) == "E7"

    # 2. 集成级验证夹具全池（清空 dev_pool 以观察全部作品的分层）
    conn.execute("DELETE FROM task6.dev_pool WHERE extraction_id = %s", (w.extraction_id,))
    strata = build_eval_candidates(conn, rv)
    by_mid = {item["movie_id"]: item["stratum"] for items in strata.values() for item in items}

    m_alpha = h._movie_of(conn, w.mapping, "B0T6A10001")   # movie, 1999, 无分歧
    m_beta = h._movie_of(conn, w.mapping, "B0T6B10001")    # tvMovie, 2004
    m_video = h._movie_of(conn, w.mapping, "B0T6V10001")   # video, 2001
    m_filmg = h._movie_of(conn, w.mapping, "B0T6FLG001")   # movie, year_diff_1 (1990 vs 1991)
    m_filmh = h._movie_of(conn, w.mapping, "B0T6FLH001")   # movie, year_disagreement (1990 vs 1994)
    m_ers = h._movie_of(conn, w.mapping, "B0T6ERS001")     # bound_before_release_year (B4)
    m_cls = h._movie_of(conn, w.mapping, "B0T6CLS001")     # bound_before_release_year (M4)
    m_luk = h._movie_of(conn, w.mapping, "B0T6LUK001")     # hint_matched + tconst_disagreement

    assert by_mid[m_alpha] == "E1"
    assert by_mid[m_beta] == "E2"
    assert by_mid[m_video] == "E2"
    assert by_mid[m_filmg] == "E2"
    assert by_mid[m_filmh] == "E2"
    assert by_mid[m_ers] == "E2"
    assert by_mid[m_cls] == "E2"
    assert by_mid[m_luk] == "E3"


# ------------------------------------------------------------------ 6. AC23：商品原始标题导出与 item_id 全局 MD5 打乱

def test_ac23_raw_product_titles_and_global_md5_shuffle(conn, world, tmp_path):
    w, rv = world
    conn.execute("DELETE FROM task6.dev_pool WHERE extraction_id = %s", (w.extraction_id,))
    out_dir = tmp_path / "eval_ac23_blind"
    exp = export_eval_set(conn, rv, out_dir)
    sid = exp["eval_set_id"]

    # 1. 验证 item_id 格式为 t6_eval_0001.. 且顺序严格按 md5(movie_id) 全局打乱，而非按 stratum 扎堆
    db_rows = conn.execute(
        "SELECT item_id, movie_id, stratum FROM task6.eval_item WHERE eval_set_id = %s ORDER BY item_id",
        (sid,),
    ).fetchall()
    assert [r[0] for r in db_rows] == [f"t6_eval_{i:04d}" for i in range(1, len(db_rows) + 1)]
    expected_mids = sorted(
        [r[1] for r in db_rows],
        key=lambda m: (hashlib.md5(m.encode("utf-8")).hexdigest(), m),
    )
    assert [r[1] for r in db_rows] == expected_mids
    # 验证 stratum 序列不是单调非降序（即未按 E1..E7 顺序连续排列）
    strata_seq = [r[2] for r in db_rows]
    assert strata_seq != sorted(strata_seq)

    # 2. 验证 eval_items.csv 的 product_titles 保留了 task4.field_assertion 的原始标题（含括号年份与 [VHS]）
    with Path(exp["eval_items_file"]).open("r", encoding="utf-8") as f:
        csv_by_id = {r["item_id"]: r for r in csv.DictReader(f)}

    mid_to_iid = {r[1]: r[0] for r in db_rows}
    m_filmc = h._movie_of(conn, w.mapping, "B0T6FLC001")   # E5 page_reported: Film C (1985) / Film C (1985) [VHS]
    m_filme = h._movie_of(conn, w.mapping, "B0T6FLE001")   # E5 page_reported: Film E (1986)
    assert "(1985)" in csv_by_id[mid_to_iid[m_filmc]]["product_titles"]
    assert "[VHS]" in csv_by_id[mid_to_iid[m_filmc]]["product_titles"]
    assert csv_by_id[mid_to_iid[m_filme]]["product_titles"] == "Film E (1986)"

    # 3. 验证当总池充足但中间层不足时，按 FILL_ORDER (E1, E7) 补足凑满 120 部并分配 t6_eval_0001..t6_eval_0120
    cand_id = conn.execute(
        "SELECT selected_candidate_id FROM task6.release_selection WHERE release_version_id = %s AND status = 'verified' LIMIT 1",
        (rv,),
    ).fetchone()[0]
    t4_run_id = conn.execute(
        "SELECT run_id FROM task4.mapping_version WHERE mapping_version_id = %s",
        (w.mapping,),
    ).fetchone()[0]
    for i in range(1, 45):
        mid = f"mv_e10000000000{i:04x}"
        conn.execute(
            "INSERT INTO task4.movie_identity (anchor_key, movie_id, created_run) VALUES (%s, %s, %s)",
            (f"fill_e1:{i}", mid, t4_run_id),
        )
        conn.execute(
            "INSERT INTO task4.movie (mapping_version_id, movie_id, status, anchor_key, tconst, display_title, edition_count) "
            "VALUES (%s, %s, 'imdb_linked', %s, 'tt0006001', %s, 1)",
            (w.mapping, mid, f"fill_e1:{i}", f"Fill E1 Movie {i}"),
        )
        conn.execute(
            "INSERT INTO task6.release_selection (release_version_id, movie_id, status, release_year, precision, "
            "release_type, regional_release_missing_reason, selected_candidate_id, selection_rule, reason, confidence, disagreement) "
            "VALUES (%s, %s, 'verified', 1999, 'year', 'first_release', 'no_permitted_dated_source', %s, 't6r1', 'fill e1', 0.95, '{}')",
            (rv, mid, cand_id),
        )
    for i in range(1, 80):
        mid = f"mv_e70000000000{i:04x}"
        conn.execute(
            "INSERT INTO task4.movie_identity (anchor_key, movie_id, created_run) VALUES (%s, %s, %s)",
            (f"fill_e7:{i}", mid, t4_run_id),
        )
        conn.execute(
            "INSERT INTO task4.movie (mapping_version_id, movie_id, status, anchor_key, display_title, edition_count) "
            "VALUES (%s, %s, 'page_only', %s, %s, 1)",
            (w.mapping, mid, f"fill_e7:{i}", f"Fill E7 Movie {i}"),
        )
        conn.execute(
            "INSERT INTO task6.release_selection (release_version_id, movie_id, status, missing_reason, precision, "
            "release_type, regional_release_missing_reason, selection_rule, reason, confidence, disagreement) "
            "VALUES (%s, %s, 'missing', 'no_candidate', 'year', 'first_release', 'no_permitted_dated_source', 't6r1', 'fill e7', 0.0, '{}')",
            (rv, mid),
        )
    out_dir_120 = tmp_path / "eval_ac23_120"
    exp120 = export_eval_set(conn, rv, out_dir_120)
    assert exp120["items_total"] == 120
    assert exp120["strata_counts"]["E1"]["filled"] > 0
    assert exp120["strata_counts"]["E7"]["filled"] > 0
    rows120 = conn.execute(
        "SELECT item_id, movie_id FROM task6.eval_item WHERE eval_set_id = %s ORDER BY item_id",
        (exp120["eval_set_id"],),
    ).fetchall()
    assert [r[0] for r in rows120] == [f"t6_eval_{i:04d}" for i in range(1, 121)]
    assert [r[1] for r in rows120] == sorted(
        [r[1] for r in rows120],
        key=lambda m: (hashlib.md5(m.encode("utf-8")).hexdigest(), m),
    )


# ------------------------------------------------------------------ 7. AC23：AI 草稿非循环且不采用禁用来源

def test_ac23_ai_draft_is_non_circular_and_rejects_forbidden_sources(conn, world, tmp_path):
    w, rv = world
    conn.execute("DELETE FROM task6.dev_pool WHERE extraction_id = %s", (w.extraction_id,))

    m_alpha = h._movie_of(conn, w.mapping, "B0T6A10001")   # tt0006001, IMDb start_year=1999
    m_jan = h._movie_of(conn, w.mapping, "B0T6JAN001")     # missing, 有 media_release 2001-01-01 与推断上界 2001
    m_rvt = h._movie_of(conn, w.mapping, "B0T6RVT001")     # missing, 仅有评论年份提及 1950 与评论时间 1999

    # 故意篡改 release_selection.release_year 为虚假的 1911，验证 _generate_ai_draft 绝不读取或复制 system_year
    conn.execute(
        "UPDATE task6.release_selection SET release_year = 1911 WHERE release_version_id = %s AND movie_id = %s",
        (rv, m_alpha),
    )

    out_dir = tmp_path / "eval_ac23_draft"
    exp = export_eval_set(conn, rv, out_dir)
    sid = exp["eval_set_id"]
    mid_to_iid = dict(
        conn.execute("SELECT movie_id, item_id FROM task6.eval_item WHERE eval_set_id = %s", (sid,)).fetchall()
    )

    with Path(exp["draft_file"]).open("r", encoding="utf-8") as f:
        draft_by_id = {r["item_id"]: r for r in csv.DictReader(f)}

    # 1. Alpha Film 的草稿年份来自独立查询的 imdb.title (1999)，绝不等于被篡改的 system_year (1911)
    d_alpha = draft_by_id[mid_to_iid[m_alpha]]
    assert d_alpha["ai_true_year"] == "1999"
    assert d_alpha["ai_tconst_label"] == "same"
    assert "1911" not in d_alpha["ai_reason"]

    # 2. missing 作品（含发售日/推断上界 2001 的 January Movie、含评论提及 1950 的 Review Talk）绝不填入 ai_true_year，且不泄露上界年份
    d_jan = draft_by_id[mid_to_iid[m_jan]]
    assert d_jan["ai_true_year"] == ""
    assert d_jan["ai_tconst_label"] == "na"
    assert d_jan["ai_doubt"] == "true"
    assert "2001" not in d_jan["ai_reason"]

    d_rvt = draft_by_id[mid_to_iid[m_rvt]]
    assert d_rvt["ai_true_year"] == ""
    assert d_rvt["ai_tconst_label"] == "na"
    assert d_rvt["ai_doubt"] == "true"

    # 3. 当候选 tconst 的 IMDb 标题或演职员与商品不吻合（如单实词别名跨作品错配）时，标 ai_tconst_label = different, ai_doubt = true
    mismatch_draft = _generate_ai_draft(
        {
            "movie_title": "New Legend of Shaolin",
            "product_titles": "New Legend of Shaolin [DVD]",
            "directors": "Corey Yuen",
            "actors": "Jet Li",
            "tconst": "tt0106936",
            "system_year": 1993,   # 即使传入 system_year 也不得读取
            "bound_year": 1998,
        },
        {"titles": ["New Legend of Shaolin [DVD]"], "directors": ["Corey Yuen"], "actors": ["Jet Li"]},
        {
            "tt0106936": {
                "tconst": "tt0106936",
                "primary_title": "The Legend",
                "original_title": "Fong Sai-Yuk",
                "start_year": 1993,
                "title_type": "movie",
                "title_keys": {"legend", "fong sai yuk"},
                "directors": {"corey yuen"},
                "actors": {"jet li"},
            }
        },
    )
    assert mismatch_draft["ai_true_year"] == ""
    assert mismatch_draft["ai_tconst_label"] == "different"
    assert mismatch_draft["ai_doubt"] == "true"

