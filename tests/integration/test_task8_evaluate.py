"""任务 8 评价工具（spec AC13）：开发池排除、六层配额与补足、盲测不泄露、草稿只读盲测表、导入校验、指标手算。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task8_evaluate.py
"""

from __future__ import annotations

import csv
import hashlib

import pytest

import _task8_helpers as h
from etl_task8 import evaluate as ev
from task8.world import Rec, copies, unix

pytestmark = pytest.mark.integration

LEAKS = ("lr_", "rf_", "sg_", "dc_", "D1_", "D2_", "D3_", "representative", "cross_family", "near_duplicate", "merged",
         "candidate", "separate", "grouped", "weak_support")


@pytest.fixture
def world(conn, make_settings, tmp_path):
    recs, products, dups = h.small_world()
    s5 = "B0000E2ES5"
    recs += [Rec(s5, f"EY{i}", text=f"first opinion {i}") for i in range(6)]          # V6：同一用户同 ASIN 两条不同评论
    recs += [Rec(s5, f"EY{i}", text=f"totally other words {i}", time=unix(2)) for i in range(6)]
    for i in range(6):                                                                # V3：更多跨家族同内容
        recs += copies(["B0000E2EQ1", "B0000E2EQ2"], f"EUC{i}", text="Order for a friend")
    eid = h.setup(conn, make_settings, tmp_path, recs)
    h.seed_upstream(conn, eid, products, dups)
    frozen = ev.freeze_dev_pool(conn, eid)
    r = h.execute(conn, eid, partitions=2)
    return eid, r.review_dedup_version_id, frozen


def _read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def test_ac13_dev_pool_frozen_before_first_run(conn, world):
    eid, rd, frozen = world
    users = sorted({r[0] for r in conn.execute("SELECT DISTINCT reviewer_id FROM public.review_staging "
                                               "WHERE extraction_id = %s AND reviewer_id IS NOT NULL", (eid,))},
                   key=lambda u: (hashlib.md5(u.encode()).hexdigest(), u))
    expected = set(users[:len(users) // 5])                     # md5(reviewer_id) 排序的前 20%
    dev = {r[0] for r in conn.execute("SELECT reviewer_id FROM task8.dev_pool WHERE extraction_id = %s", (eid,))}
    assert dev == expected and frozen == {"status": "frozen", "dev_reviewers": len(expected),
                                          "total_reviewers": len(users), "frozen_before_first_run": True}
    again = ev.freeze_dev_pool(conn, eid)
    assert again["status"] == "already_frozen" and again["frozen_before_first_run"] is True


def test_ac13_strata_exclude_dev_pool_and_blind_table_does_not_leak(conn, world, tmp_path):
    eid, rd, _ = world
    out = ev.export_eval_set(conn, rd, tmp_path / "eval")
    dev = {r[0] for r in conn.execute("SELECT reviewer_id FROM task8.dev_pool WHERE extraction_id = %s", (eid,))}
    items = conn.execute("SELECT item_id, stratum, left_ref, right_ref, system_decision FROM task8.eval_item "
                         "WHERE eval_set_id = %s ORDER BY item_id", (out["eval_set_id"],)).fetchall()
    assert len(items) == out["items"] and out["items"] < ev.TARGET_TOTAL      # 小世界里各层都不足，全部入样
    by = {s: [i for i in items if i[1] == s] for s in ev.QUOTA}
    assert all(by[s] for s in ev.QUOTA), {s: len(v) for s, v in by.items()}
    v = h.version(conn, rd)
    dk = v["dedup_stage_key"]
    user_of_record = dict(conn.execute("SELECT record_no, reviewer_id FROM task8.review_clean WHERE clean_stage_key = %s",
                                       (v["clean_stage_key"],)).fetchall())
    user_of_logical = dict(conn.execute("SELECT logical_review_id, reviewer_id FROM task8.logical_review "
                                        "WHERE dedup_stage_key = %s", (dk,)).fetchall())
    for _, s, left, right, _ in items:
        if s in ("V1", "V2"):
            assert user_of_record[int(left)] not in dev and user_of_record[int(right)] not in dev
        elif s != "V5":
            assert user_of_logical[left] not in dev and user_of_logical[right] not in dev
    # 每层都取到了该层全部非开发池项（独立计算：D3 记录、D2 记录、候选）
    d3 = {n for n, u in conn.execute("SELECT m.record_no, l.reviewer_id FROM task8.review_mapping m JOIN task8.logical_review l "
                                     "USING (dedup_stage_key, logical_review_id) WHERE m.dedup_stage_key = %s "
                                     "AND m.match_level = 'D3_family_copy'", (dk,)) if u not in dev}
    assert {int(i[3]) for i in by["V2"]} == d3
    assert {i[4] for i in items} <= set(ev.SYSTEM_DECISION.values())
    rows = _read(tmp_path / "eval" / "eval_items.csv")
    assert tuple(rows[0]) == ev.BLIND_COLUMNS and [r["item_id"] for r in rows] == [i[0] for i in items]
    text = (tmp_path / "eval" / "eval_items.csv").read_text()
    assert not [x for x in LEAKS if x in text] and not any(f"V{k}" in text for k in range(1, 7))
    assert all(len(r["left_text_500"]) <= 500 for r in rows)
    keys = conn.execute("SELECT item_key FROM task8.eval_item WHERE eval_set_id = %s ORDER BY item_id",
                        (out["eval_set_id"],)).fetchall()
    assert [k for (k,) in keys] == sorted((k for (k,) in keys), key=lambda k: hashlib.md5(k.encode()).hexdigest())


def test_ac13_quota_weak_floor_and_fill_order():
    def pool(s, n, weak=0):
        return [{"stratum": s, "item_key": f"{s}:{i}", "left_ref": "a", "right_ref": "b", "side": "record",
                 "weak": i < weak} for i in range(n)]
    pools = {"V1": pool("V1", 5), "V2": pool("V2", 60, weak=12), "V3": pool("V3", 30), "V4": pool("V4", 30),
             "V5": pool("V5", 20), "V6": pool("V6", 20)}
    items, strata = ev.sample_items(pools)
    assert len(items) == 120 and strata["V1"]["actual"] == 5
    assert (strata["V2"]["actual"], strata["V2"]["filled"]) == (35, 10) and strata["V2"]["weak_support"] >= 10
    assert strata["V3"]["filled"] == 0                          # V2 足够补，不轮到 V3
    pools["V2"] = pool("V2", 27, weak=3)                        # V2 只多 2 项：再由 V3 补 8
    items, strata = ev.sample_items(pools)
    assert (strata["V2"]["actual"], strata["V2"]["filled"], strata["V3"]["filled"], strata["V3"]["actual"]) == (27, 2, 8, 28)
    assert strata["V2"]["weak_support"] == 3 and len(items) == 120
    assert [x["item_id"] for x in items] == [f"t8_eval_{k:04d}" for k in range(1, 121)]


def test_ac13_draft_reads_only_blind_table(conn, world, tmp_path):
    eid, rd, _ = world
    ev.export_eval_set(conn, rd, tmp_path / "eval")
    conn.execute("ALTER SCHEMA task8 RENAME TO task8_hidden")   # 草稿生成时 task8 结论表不可读
    try:
        n = ev.write_draft(tmp_path / "eval" / "eval_items.csv", tmp_path / "again.csv")
    finally:
        conn.execute("ALTER SCHEMA task8_hidden RENAME TO task8")
    assert (tmp_path / "again.csv").read_bytes() == (tmp_path / "eval" / "eval_items_draft.csv").read_bytes()
    draft = _read(tmp_path / "again.csv")
    assert n == len(draft) and tuple(draft[0]) == ev.DRAFT_COLUMNS
    assert {r["ai_label"] for r in draft} <= set(ev.LABELS) and all(r["human_label"] == "" for r in draft)
    same = {"question": ev.QUESTION["review"], **{f"{s}_{f}": "x" for s in ("left", "right") for f in ev.SIDE_FIELDS}}
    assert ev.generate_ai_draft(same)["ai_label"] == "same"
    assert ev.generate_ai_draft({**same, "right_summary": "y"}) == {
        "ai_label": "different", "ai_doubt": "true", "ai_reason": ev.generate_ai_draft({**same, "right_summary": "y"})["ai_reason"]}


def _label_file(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(ev.DRAFT_COLUMNS))
        w.writeheader()
        w.writerows(rows)


def test_ac13_import_rejects_unsupported_changes_and_metrics_match_hand_count(conn, world, tmp_path):
    eid, rd, _ = world
    out = ev.export_eval_set(conn, rd, tmp_path / "eval")
    sid = out["eval_set_id"]
    draft = _read(tmp_path / "eval" / "eval_items_draft.csv")
    stratum = dict(conn.execute("SELECT item_id, stratum FROM task8.eval_item WHERE eval_set_id = %s", (sid,)).fetchall())
    # 人工结论：V1、V2、V5 标 same；V3、V6 标 different；V4 第一项 same、其余 unsure
    first_v4 = min(i for i, s in stratum.items() if s == "V4")
    want = {i: ("same" if s in ("V1", "V2", "V5") else "different" if s in ("V3", "V6")
                else "same" if i == first_v4 else "unsure") for i, s in stratum.items()}
    rows = [{**r, "human_label": want[r["item_id"]], "human_evidence": ""} for r in draft]
    _label_file(tmp_path / "bad.csv", rows)
    needs = [r["item_id"] for r in rows if r["human_label"] != "unsure"
             and (r["ai_doubt"] == "true" or r["ai_label"] != r["human_label"])]
    assert needs                                                # 至少有一项存疑或改判
    with pytest.raises(ev.EvalError, match="human_evidence"):
        ev.import_labels(conn, sid, tmp_path / "bad.csv", "付林轩")
    assert conn.execute("SELECT count(*) FROM task8.eval_label").fetchone()[0] == 0       # 校验失败不写入任何行
    for r in rows:
        if r["item_id"] in needs:
            r["human_evidence"] = "核对两侧商品页与原始字段"
    _label_file(tmp_path / "good.csv", rows)
    res = ev.import_labels(conn, sid, tmp_path / "good.csv", "付林轩")
    assert res["imported"] == len(rows)
    with pytest.raises(ev.EvalError, match="已有"):
        ev.import_labels(conn, sid, tmp_path / "good.csv", "付林轩")
    m = ev.metrics(conn, sid)
    n = {s: sum(1 for x in stratum.values() if x == s) for s in ev.QUOTA}
    assert m["labeled_total"] == len(rows) and m["annotators"] == ["付林轩"]
    assert m["merge_precision"] == {"numerator": n["V1"] + n["V2"], "denominator": n["V1"] + n["V2"], "unsure": 0,
                                    "value": 1.0}
    assert m["no_merge_correct"]["value"] == 1.0 and m["no_merge_correct"]["denominator"] == n["V3"] + n["V6"]
    assert m["near_duplicate_same_ratio"] == {"numerator": 1, "denominator": 1, "unsure": n["V4"] - 1, "value": 1.0}
    assert m["stat_group_same_movie_rate"]["value"] == 1.0
    # 分母为 0 记“无定义”：全部 V5 改为 unsure 后重导
    for r in rows:
        if stratum[r["item_id"]] == "V5":
            r["human_label"] = "unsure"
    _label_file(tmp_path / "good2.csv", rows)
    ev.import_labels(conn, sid, tmp_path / "good2.csv", "付林轩", force=True)
    assert ev.metrics(conn, sid)["stat_group_same_movie_rate"] == {"numerator": 0, "denominator": 0,
                                                                    "unsure": n["V5"], "value": "无定义"}
