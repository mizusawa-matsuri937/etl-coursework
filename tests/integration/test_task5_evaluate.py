"""任务 5 评价工具（spec AC13）：开发池冻结、评价表分层与不泄露系统判定、标注导入、指标、簇抽查。

夹具是 combined_inputs(extra_films=True)：AC01–AC08 的商品加作品年表夹具、同一演员倒序与正序的两部作品、一对近似名。
预期值按夹具手算或由库中的 eval_pair 与标注逐对数出来。
"""

from __future__ import annotations

import csv
import math
from pathlib import Path

import pytest

from _task5_helpers import combined_inputs, inputs_for, make_world
from etl_task4.evaluate import EvalError
from etl_task5 import evaluate as ev, names, report
from etl_task5.runner import execute_resolve

pytestmark = pytest.mark.integration

PLAN = {"S1": 1, "S2": 2, "S3": 2, "S4": 3, "S5": 1, "S6": 2, "S7": 2, "S8": 2}


@pytest.fixture
def ran(conn, make_settings, tmp_path):
    products, imdb = combined_inputs(extra_films=True)
    w = make_world(conn, make_settings, tmp_path, products, imdb)
    pm = execute_resolve(conn, inputs_for(w), batch_size=7).person_mapping_version_id
    return w, pm


@pytest.fixture
def exported(conn, ran, tmp_path):
    w, pm = ran
    m = report.pmap_row(conn, pm)
    ev.freeze_dev_pool(conn, w.fx.eid, parser_version=m["parser_version"], norm_version=m["norm_version"])
    out = ev.export_pairs(conn, pm, tmp_path / "eval", plan=PLAN)
    return w, pm, out, tmp_path / "eval"


def read(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def write(path, rows, columns):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=columns)
        wr.writeheader()
        wr.writerows(rows)


def predicted(conn, pm):
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT pair_id, predicted_same, route FROM task5.eval_pair WHERE eval_set_id = %s", (ev.eval_set_id(pm),))}


# ---------------------------------------------------------------- 开发池

def test_ac13_dev_pool_is_the_first_fifth_of_family_keys_by_md5_and_stays_frozen(conn, ran):
    w, pm = ran
    m = report.pmap_row(conn, pm)
    with pytest.raises(EvalError, match="freeze"):
        ev.export_pairs(conn, pm, Path("/tmp/never"))
    out = ev.freeze_dev_pool(conn, w.fx.eid, parser_version=m["parser_version"], norm_version=m["norm_version"])
    keys = sorted({r[0] for r in conn.execute("SELECT family_key FROM task5.person_mention WHERE status = 'resolvable' AND "
                                              "family_key <> ''")}, key=lambda k: (ev._md5(k), k))
    expected = set(keys[:math.ceil(len(keys) / 5)])
    assert out["status"] == "frozen" and out["dev_family_keys"] == len(expected) == 10 and out["family_keys"] == len(keys)
    assert ev.dev_family_keys(conn, w.fx.eid) == expected
    again = ev.freeze_dev_pool(conn, w.fx.eid, parser_version=m["parser_version"], norm_version=m["norm_version"])
    assert again["status"] == "already_frozen" and ev.dev_family_keys(conn, w.fx.eid) == expected      # 冻结后不变


# ---------------------------------------------------------------- 评价表

def test_ac13_export_excludes_dev_pool_hides_system_judgement_and_follows_the_plan(conn, exported):
    w, pm, out, d = exported
    dev = ev.dev_family_keys(conn, w.fx.eid)
    rows = read(d / "eval_pairs.csv")
    assert len(rows) == out["pairs"] == 13 + 2
    for r in rows:                                                        # 两边姓氏键都不在开发池
        assert names.parse(r["left_raw"]).family not in dev and names.parse(r["right_raw"]).family not in dev
        assert r["label"] == "" and r["left_asin"] != r["right_asin"]
    header = set(rows[0])
    assert not {"predicted", "predicted_same", "route", "tier", "system", "reason", "score"} & header     # 不显示系统判定
    by = {}
    for r in rows:
        by[r["stratum"]] = by.get(r["stratum"], 0) + 1
    st = out["strata"]
    assert by == {"S1": 1, "S2": 2, "S3": 2, "S4": 5, "S5": 1, "S6": 2, "S8": 2}        # S7 候选都在开发池，由 S4 补足 2 个
    assert st["S7"] == {"target": 2, "available": 0, "actual": 0, "filled": 0}
    assert st["S4"]["actual"] == 3 and st["S4"]["filled"] == 2 and st["_short_after_fill"] == 0
    assert all(r["filmography_work"] and "imdb.com/title/tt" in r["filmography_url"] for r in rows if r["stratum"] == "S8")
    assert not any(r["filmography_work"] for r in rows if r["stratum"] != "S8")
    assert all(r["left_imdb_url"].startswith("https://www.imdb.com/name/nm") for r in rows if r["stratum"] in ("S1", "S3", "S8"))
    assert all(r["left_amazon_url"].startswith("https://www.amazon.com/dp/") for r in rows)
    draft = read(d / "eval_pairs_draft.csv")
    assert [r["pair_id"] for r in draft] == [r["pair_id"] for r in rows]
    assert {"ai_label", "ai_doubt", "ai_reason"} <= set(draft[0]) and all(r["ai_label"] == "" for r in draft)
    pred = predicted(conn, pm)
    assert {p for p, (same, route) in pred.items() if same and route == "filmography"} == {r["pair_id"] for r in rows if r["stratum"] == "S8"}
    assert all(pred[r["pair_id"]][0] for r in rows if r["stratum"] in ("S1", "S2", "S8")) and \
        not any(pred[r["pair_id"]][0] for r in rows if r["stratum"] in ("S3", "S5", "S6"))
    assert (d / "LABELING.md").exists()


def test_ac13_export_does_not_overwrite_an_existing_file_and_a_forced_rewrite_keeps_the_same_sample(conn, exported):
    w, pm, out, d = exported
    before = read(d / "eval_pairs.csv")
    with pytest.raises(EvalError, match="已存在"):
        ev.export_pairs(conn, pm, d, plan=PLAN)
    again = ev.export_pairs(conn, pm, d, plan=PLAN, force=True)
    assert [r["pair_id"] for r in read(d / "eval_pairs.csv")] == [r["pair_id"] for r in before] and again["pairs"] == len(before)


# ---------------------------------------------------------------- 标注导入与指标

def label_file(conn, pm, d, tmp_path, fn):
    rows = read(d / "eval_pairs.csv")
    pred = predicted(conn, pm)
    for r in rows:
        r["label"], r["note"] = fn(r, pred[r["pair_id"]][0])
    path = tmp_path / "labeled.csv"
    write(path, rows, list(rows[0]))
    return path, rows


def test_ac13_import_then_metrics_match_hand_counts_by_stratum_and_by_route(conn, exported, tmp_path):
    w, pm, out, d = exported
    seen = {"fp": None, "fn": None, "unsure": None}

    def fn(r, pred):
        if pred and r["stratum"] == "S2" and seen["fp"] is None:              # 一个误合并：系统合并、人工判不同
            seen["fp"] = r["pair_id"]
            return "different", "故意的误合并"
        if not pred and r["stratum"] == "S4" and seen["fn"] is None:          # 一个漏合并：系统没合并、人工判同一人
            seen["fn"] = r["pair_id"]
            return "same", "故意的漏合并"
        if r["stratum"] == "S5" and seen["unsure"] is None:
            seen["unsure"] = r["pair_id"]
            return "unsure", "证据不足"
        return ("same" if pred else "different"), ""

    path, rows = label_file(conn, pm, d, tmp_path, fn)
    res = ev.import_labels(conn, pm, path, "tester")
    assert res["labeled"] == len(rows) and res["unsure"] == 1 and res["unlabeled"] == 0
    m = ev.metrics(conn, pm)
    pred = predicted(conn, pm)
    labs = {r["pair_id"]: r["label"] for r in rows}
    tp = sum(1 for p, (s, _) in pred.items() if s and labs[p] == "same")
    fp = sum(1 for p, (s, _) in pred.items() if s and labs[p] == "different")
    fn_ = sum(1 for p, (s, _) in pred.items() if not s and labs[p] == "same")
    assert (fp, fn_) == (1, 1) and tp == sum(1 for r in rows if pred[r["pair_id"]][0]) - 1
    o = m["overall"]
    assert (o["tp"], o["fp"], o["fn"]) == (tp, fp, fn_)
    assert o["precision"] == pytest.approx(tp / (tp + fp)) and o["recall"] == pytest.approx(tp / (tp + fn_))
    assert o["f1"] == pytest.approx(2 * tp / (2 * tp + fp + fn_))
    assert m["unsure"] == 1 and m["labeled"] == len(rows) and m["annotators"] == ["tester"]
    assert set(m["by_stratum"]) == {"S1", "S2", "S3", "S4", "S5", "S6", "S8"}
    assert m["by_stratum"]["S2"]["fp"] == 1 and m["by_stratum"]["S4"]["fn"] == 1 and m["by_stratum"]["S5"]["pairs"] == 0
    assert {"credits", "filmography", "co_credit"} <= set(m["by_route"]) and m["by_route"]["filmography"]["tp"] == 2
    assert m["by_route"]["co_credit"]["fp"] == 1 and m["by_route"]["co_credit"]["fn"] == 0
    assert [i["pair_id"] for i in m["false_merges"]] == [seen["fp"]] and [i["pair_id"] for i in m["missed_merges"]] == [seen["fn"]]
    assert m["false_merges"][0]["route"] == "co_credit" and m["missed_merges"][0]["stratum"] == "S4"
    rv = m["review"]
    assert rv["review_pending_rate"] > 0 and rv["review_coverage"] == 0 and rv["review_candidates"] > 0
    text = ev.render_metrics(m)
    assert "Precision" in text and "误合并实例" in text and seen["fp"] in text


def test_ac13_review_coverage_counts_candidates_with_a_human_decision(conn, exported):
    w, pm, out, d = exported
    a = conn.execute("SELECT mention_key FROM task5.person_mention WHERE asin = 'B0T5R00C01' AND raw_value = 'Sam Solo'").fetchone()[0]
    b = conn.execute("SELECT mention_key FROM task5.person_mention WHERE asin = 'B0T5R00C02' AND raw_value = 'Sam Solo'").fetchone()[0]
    conn.execute("INSERT INTO task5.human_decision (extraction_id, set_hash, kind, left_entity, right_entity, annotator, "
                 "import_file_sha256) VALUES (%s, 'x', 'cannot_link', %s, %s, 't', 'y')", (w.fx.eid, a, b))
    path = d / "x.csv"
    write(path, [{"pair_id": p, "label": ""} for p in predicted(conn, pm)], ["pair_id", "label"])
    ev.import_labels(conn, pm, path, "tester")
    rv = ev.metrics(conn, pm)["review"]
    assert rv["human_reviewed_candidates"] == 1 and rv["review_coverage"] == pytest.approx(1 / rv["review_candidates"], abs=1e-6)


def test_ac13_no_predicted_merge_gives_undefined_precision_recall_and_f1(conn, exported, tmp_path):
    w, pm, out, d = exported
    conn.execute("UPDATE task5.eval_pair SET predicted_same = false, route = NULL WHERE eval_set_id = %s", (ev.eval_set_id(pm),))
    path, rows = label_file(conn, pm, d, tmp_path, lambda r, pred: ("different", ""))
    ev.import_labels(conn, pm, path, "tester")
    o = ev.metrics(conn, pm)["overall"]
    assert (o["tp"], o["fp"], o["fn"]) == (0, 0, 0) and o["tn"] == len(rows)
    assert o["precision"] is None and o["recall"] is None and o["f1"] is None
    assert set(o["undefined"]) == {"precision", "recall", "f1"} and "无定义" in o["note"]
    assert "无定义" in ev.render_metrics(ev.metrics(conn, pm))


def test_ac13_import_rejects_mismatched_pairs_and_bad_labels_and_lists_contradictions_without_changing_them(conn, exported, tmp_path):
    w, pm, out, d = exported
    rows = read(d / "eval_pairs.csv")
    write(tmp_path / "short.csv", rows[1:], list(rows[0]))
    with pytest.raises(EvalError, match="pair 集合"):
        ev.import_labels(conn, pm, tmp_path / "short.csv", "tester")
    rows[0]["label"] = "maybe"
    write(tmp_path / "bad.csv", rows, list(rows[0]))
    with pytest.raises(EvalError, match="label 必须"):
        ev.import_labels(conn, pm, tmp_path / "bad.csv", "tester")
    with pytest.raises(EvalError, match="annotator"):
        ev.import_labels(conn, pm, tmp_path / "bad.csv", " ")
    assert conn.execute("SELECT count(*) FROM task5.eval_label").fetchone()[0] == 0
    path, rows = label_file(conn, pm, d, tmp_path, lambda r, pred: ("different" if r["stratum"] in ("S1", "S8") else
                                                                    "same" if r["stratum"] == "S3" else "", ""))
    res = ev.import_labels(conn, pm, path, "tester")
    why = {c["why"] for c in res["contradictions"]}
    assert why == {"双方关联同一个 IMDb 人物", "双方关联了不同的 IMDb 人物"}
    assert {c["label"] for c in res["contradictions"]} == {"different", "same"}
    assert conn.execute("SELECT count(*) FROM task5.eval_label WHERE label IN ('same', 'different')").fetchone()[0] == res["labeled"]


# ---------------------------------------------------------------- 簇抽查

def test_ac13_cluster_sample_picks_multi_work_people_outside_the_dev_pool_and_prefers_iteration_people(conn, exported, tmp_path):
    w, pm, out, d = exported
    res = ev.export_clusters(conn, pm, d, plan={"3+": 2, "2": 3})
    rows = read(d / "cluster_sample.csv")
    people = {r["person_id"] for r in rows}
    assert len(people) == res["persons"] == 5 and res["from_iteration"] >= 1
    dev = ev.dev_family_keys(conn, w.fx.eid)
    for pid in people:
        units = conn.execute("SELECT count(DISTINCT g.work_unit) FROM task5.mention_person mp JOIN task5.person_group g ON "
                             "g.group_key = mp.group_key WHERE mp.person_mapping_version_id = %s AND mp.person_id = %s",
                             (pm, pid)).fetchone()[0]
        fams = {names.parse(r["raw"]).family for r in rows if r["person_id"] == pid}
        assert units >= 2 and not fams & dev
    assert any(r["basis"] == "co_credit" and r["round"] for r in rows)
    with pytest.raises(EvalError, match="已存在"):
        ev.export_clusters(conn, pm, d, plan={"3+": 2, "2": 3})
    rows_by_person: dict[str, list] = {}
    for r in rows:
        rows_by_person.setdefault(r["person_id"], []).append(r)
    first, second = sorted(rows_by_person)[:2]
    for r in rows_by_person[first]:
        r["cluster_label"] = "same_person"
    rows_by_person[second][0]["cluster_label"] = "mixed"
    rows_by_person[second][-1]["outlier"] = "x"
    write(tmp_path / "clusters.csv", rows, list(rows[0]))
    res = ev.import_clusters(conn, pm, tmp_path / "clusters.csv", "tester")
    assert res["labeled"] == 2 and res["mixed"] == 1
    c = ev.metrics(conn, pm)["clusters"]
    assert c["sampled"] == 5 and c["labeled"] == 2 and c["mixed"] == 1
    assert c["mixed_persons"][0]["person_id"] == second and c["mixed_persons"][0]["problem_mentions"] == [rows_by_person[second][-1]["mention_key"]]
    bad = [dict(r) for r in rows]
    bad[0]["cluster_label"] = "mixed"
    for r in bad:
        r["outlier"] = ""
    write(tmp_path / "bad_clusters.csv", bad, list(bad[0]))
    with pytest.raises(EvalError, match="outlier"):
        ev.import_clusters(conn, pm, tmp_path / "bad_clusters.csv", "tester")
