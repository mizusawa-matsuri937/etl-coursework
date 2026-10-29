"""任务 4 评价工具（spec AC13）：开发池冻结、评价表与簇抽查表导出、标注导入、指标。"""

from __future__ import annotations

import csv
import hashlib

import pytest
from task4 import imdb_files as ifx

from _task4_helpers import P, build, inputs_for, load_imdb, match_products, reset_task4
from etl_task4 import evaluate as ev
from etl_task4.cluster import movie_id_for
from etl_task4.runner import execute_match

pytestmark = pytest.mark.integration

A = "B0T4M00"


def products():
    return match_products() + [
        P(f"{A}M01", "movie", "Alien [DVD]", directors=("Ridley Scott",), actors=("Sigourney Weaver",),
          runtime="1 hour and 57 minutes"),
        P(f"{A}M02", "movie", "Alien (Special Edition)", directors=("Ridley Scott",), actors=("Sigourney Weaver",),
          runtime="1 hour and 57 minutes")]


@pytest.fixture
def world(conn, make_settings, tmp_path):
    fx = build(conn, make_settings, tmp_path, products())
    reset_task4(conn)
    inputs = inputs_for(fx, load_imdb(conn, tmp_path, ifx.MATCH))
    r = execute_match(conn, inputs)
    return fx, r.mapping_version_id, tmp_path / "eval"


def freeze_by_hand(conn, eid, *entity_ids):
    """指定哪些实体在开发池里，让用例不依赖 md5 的次序。"""
    for e in entity_ids:
        key = conn.execute("SELECT normalized_value FROM task4.field_assertion WHERE entity_id = %s AND "
                           "field_name = 'title'", (e,)).fetchone()[0]
        conn.execute("INSERT INTO task4.dev_pool (extraction_id, asin, title_key, norm_version) VALUES (%s, %s, %s, 't4n1')",
                     (eid, e, key))


def read_rows(path):
    return list(csv.DictReader(path.open(encoding="utf-8")))


def label_file(path, labels, note=None):
    """按 pair_id → 标签 把导出的表标注后另存。"""
    rows = read_rows(path)
    for r in rows:
        if r["pair_id"] in labels:
            r["label"] = labels[r["pair_id"]]
            if note:
                r["note"] = note
    out = path.with_name("labeled.csv")
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return out


SMALL = {"S1": 3, "S2": 1, "S3": 1, "S4": 2, "S5": 1, "S6": 2, "S7": 1}


# ---------------------------------------------------------------- 开发池

def test_dev_pool_is_the_first_fifth_of_title_keys_by_md5_and_stays_frozen(conn, world):
    fx, v, _ = world
    kw = dict(t3_rule_version="t3r2", parser_version="t3p1", norm_version="t4n1")
    r = ev.freeze_dev_pool(conn, fx.eid, **kw)
    keys = {e: k for e, k in conn.execute(
        "SELECT entity_id, normalized_value FROM task4.field_assertion WHERE field_name = 'title' AND normalized_value <> ''").fetchall()}
    distinct = sorted(set(keys.values()), key=lambda k: (hashlib.md5(k.encode()).hexdigest(), k))
    n = -(-len(distinct) // 5)
    assert r["status"] == "frozen" and r["dev_title_keys"] == n == len(distinct) // 5 + 1
    dev = {x[0] for x in conn.execute("SELECT DISTINCT title_key FROM task4.dev_pool").fetchall()}
    assert dev == set(distinct[:n])
    # 同一标题键下的实体整组落在开发池里（按标题键而不是按 ASIN 划分）
    rows = {x[0]: x[1] for x in conn.execute("SELECT asin, title_key FROM task4.dev_pool").fetchall()}
    assert set(rows) == {e for e, k in keys.items() if k in dev}
    before = conn.execute("SELECT count(*), min(frozen_at) FROM task4.dev_pool").fetchone()
    again = ev.freeze_dev_pool(conn, fx.eid, **kw)
    assert again["status"] == "already_frozen" and conn.execute(
        "SELECT count(*), min(frozen_at) FROM task4.dev_pool").fetchone() == before


def test_export_requires_a_frozen_dev_pool(conn, world):
    fx, v, d = world
    with pytest.raises(ev.EvalError, match="冻结开发池"):
        ev.export_pairs(conn, v, d)
    with pytest.raises(ev.EvalError, match="冻结开发池"):
        ev.export_clusters(conn, v, d)


# ---------------------------------------------------------------- 评价表

def test_export_has_the_planned_strata_excludes_the_dev_pool_and_hides_system_decisions(conn, world):
    fx, v, d = world
    freeze_by_hand(conn, fx.eid, f"{A}B01", f"{A}B02")          # Breathless 在开发池：这一对不能进评价表
    out = ev.export_pairs(conn, v, d, plan=SMALL)
    rows = read_rows(d / "eval_pairs.csv")
    assert out["pairs"] == len(rows) == out["by_stratum"]["_total"]["selected"]
    assert {r["pair_id"] for r in rows} == {x[0] for x in conn.execute("SELECT pair_id FROM task4.eval_pair").fetchall()}
    by = {s: [r for r in rows if r["stratum"] == s] for s in SMALL}
    st = out["by_stratum"]
    for s in SMALL:                                                   # 各层数量：选中数 + 补足数
        assert len(by[s]) == st[s]["selected"] + st[s]["filled"]
    assert (st["S1"]["selected"], st["S2"]["selected"], st["S6"]["selected"]) == (3, 1, 2)
    assert st["S1"]["available"] >= 3 and st["S4"]["selected"] >= 1
    # 评价表不含开发池的商品
    assert not any(f"{A}B0" in r["left_entity"] + r["right_entity"] for r in rows)
    assert all(r["label"] == "" and r["note"] == "" for r in rows)
    assert not any("tier" in c or "predicted" in c or "system" in c for c in rows[0])        # 不显示系统判定
    # 每行都有双方的原始标题、页面地址（可核对）
    assert all(r["left_title"] and r["right_title"] and r["left_amazon_url"].startswith("https://www.amazon.com/dp/")
               for r in rows)
    s6 = by["S6"]
    assert all("#" in r["left_entity"] and r["context"].startswith("合集商品") and r["right_entity"] == f"{A}M01"
               for r in s6)
    imdb_rows = [r for r in rows if r["left_tconst"]]
    assert imdb_rows and all(r["left_imdb_url"] == f"https://www.imdb.com/title/{r['left_tconst']}/" for r in imdb_rows)
    # 系统判定存在库里
    pred = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT pair_id, predicted_same, system_tier FROM task4.eval_pair")}
    assert all(pred[r["pair_id"]][0] == (r["stratum"] in ("S1", "S2") or r["stratum"] == "S6")
               for r in rows if r["stratum"] in ("S1", "S2"))
    assert (d / "LABELING.md").read_text().startswith("# 标注口径")
    # 再次导出：不覆盖已有文件，也不重新抽样
    with pytest.raises(ev.EvalError, match="不覆盖"):
        ev.export_pairs(conn, v, d, plan=SMALL)
    ev.export_pairs(conn, v, d, plan=SMALL, force=True)
    assert [r["pair_id"] for r in read_rows(d / "eval_pairs.csv")] == [r["pair_id"] for r in rows]


def test_shortfall_in_a_stratum_is_filled_from_s1_then_s4_and_recorded(conn, world):
    fx, v, d = world
    freeze_by_hand(conn, fx.eid, f"{A}I01")
    out = ev.export_pairs(conn, v, d, plan={"S1": 2, "S2": 1, "S3": 1, "S4": 1, "S5": 1, "S6": 1, "S7": 20})
    st = out["by_stratum"]
    assert st["S7"]["selected"] < 20 and st["_total"]["target"] == 27
    assert st["S1"]["filled"] + st["S4"]["filled"] == st["_total"]["selected"] - sum(
        st[s]["selected"] for s in ("S1", "S2", "S3", "S4", "S5", "S6", "S7"))
    assert st["S1"]["filled"] > 0 and st["_total"]["unfilled"] == st["_total"]["target"] - st["_total"]["selected"]


# ---------------------------------------------------------------- 标注导入与指标

def test_import_and_metrics_match_a_hand_computation_with_instances_and_unsure(conn, world):
    fx, v, d = world
    freeze_by_hand(conn, fx.eid, f"{A}I01")
    ev.export_pairs(conn, v, d, plan=SMALL)
    rows = read_rows(d / "eval_pairs.csv")
    pred = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT pair_id, predicted_same, system_tier FROM task4.eval_pair")}
    pos = [r["pair_id"] for r in rows if pred[r["pair_id"]][0]]
    neg = [r["pair_id"] for r in rows if not pred[r["pair_id"]][0]]
    assert len(pos) >= 4 and len(neg) >= 3
    # 预测同一的：前 3 个判 same（TP）、1 个判 different（FP）、1 个 unsure；预测不同的：1 个判 same（FN），其余 different（TN）
    labels = {pos[0]: "same", pos[1]: "same", pos[2]: "same", pos[3]: "different"}
    if len(pos) > 4:
        labels[pos[4]] = "unsure"
    labels[neg[0]] = "same"
    labels.update({n: "different" for n in neg[1:]})
    path = label_file(d / "eval_pairs.csv", labels, note="手工")
    imp = ev.import_labels(conn, v, path, "张三")
    assert imp["labeled"] == len(labels) and imp["unsure"] == (1 if len(pos) > 4 else 0)
    assert imp["unlabeled"] == len(rows) - len(labels)
    m = ev.metrics(conn, v)
    tp, fp, fn = 3, 1, 1
    o = m["overall"]
    assert (o["tp"], o["fp"], o["fn"]) == (tp, fp, fn) and o["tn"] == len(neg) - 1
    assert o["precision"] == pytest.approx(tp / (tp + fp)) and o["recall"] == pytest.approx(tp / (tp + fn))
    assert o["f1"] == pytest.approx(2 * (tp / 4) * (tp / 4) / (tp / 4 + tp / 4))
    assert [i["pair_id"] for i in m["false_merges"]] == [pos[3]]
    assert [i["pair_id"] for i in m["missed_merges"]] == [neg[0]]
    assert m["false_merges"][0]["left"]["title"] and m["false_merges"][0]["left"]["url"]
    assert m["unsure"] == (1 if len(pos) > 4 else 0) and m["annotators"] == ["张三"]
    assert m["review"]["review_pending_rate"] == pytest.approx(m["review"]["review_pending_products"] / 20)
    assert m["review"]["review_coverage"] == 0.0           # 没有任何人工决策
    assert sum(b["tp"] for b in m["by_stratum"].values()) == tp
    assert "只代表这个评价样本" in m["scope_note"]
    md = ev.render_metrics(m)
    assert "误合并实例" in md and "漏合并实例" in md and "张三" in md
    # 同一个文件再导入一次：不改变结果；换一份文件（改一个标注）以后导入的为准
    ev.import_labels(conn, v, path, "张三")
    assert ev.metrics(conn, v)["overall"]["tp"] == tp
    labels[pos[2]] = "different"
    ev.import_labels(conn, v, label_file(d / "eval_pairs.csv", labels), "张三")
    o2 = ev.metrics(conn, v)["overall"]
    assert (o2["tp"], o2["fp"]) == (tp - 1, fp + 1)


def test_no_predicted_merge_makes_precision_and_f1_undefined(conn, world):
    fx, v, d = world
    freeze_by_hand(conn, fx.eid, f"{A}I01")
    ev.export_pairs(conn, v, d, plan=SMALL)
    conn.execute("UPDATE task4.eval_pair SET predicted_same = false WHERE eval_set_id = %s", (ev.eval_set_id(v),))
    rows = read_rows(d / "eval_pairs.csv")
    labels = {rows[0]["pair_id"]: "same", rows[1]["pair_id"]: "different"}
    ev.import_labels(conn, v, label_file(d / "eval_pairs.csv", labels), "张三")
    o = ev.metrics(conn, v)["overall"]
    assert (o["tp"], o["fp"], o["fn"]) == (0, 0, 1)
    assert o["precision"] is None and o["f1"] is None and o["recall"] == 0.0
    assert set(o["undefined"]) == {"precision", "f1"} and "无定义" in o["note"]
    assert "无定义" in ev.render_metrics(ev.metrics(conn, v))
    # 没有任何“同一”标注：Recall 也无定义
    ev.import_labels(conn, v, label_file(d / "eval_pairs.csv", {rows[1]["pair_id"]: "different"}), "李四")
    assert ev.prf(0, 0, 0)["undefined"] == ["precision", "recall", "f1"]
    assert ev.prf(2, 0, 0) == ev.prf(2, 0, 0) | {"precision": 1.0, "recall": 1.0, "f1": 1.0}


def test_import_rejects_wrong_pair_sets_bad_labels_and_flags_contradictions(conn, world):
    fx, v, d = world
    freeze_by_hand(conn, fx.eid, f"{A}I01")
    ev.export_pairs(conn, v, d, plan=SMALL)
    rows = read_rows(d / "eval_pairs.csv")
    with pytest.raises(ev.EvalError, match="annotator"):
        ev.import_labels(conn, v, d / "eval_pairs.csv", " ")
    with pytest.raises(ev.EvalError, match="之一"):
        ev.import_labels(conn, v, label_file(d / "eval_pairs.csv", {rows[0]["pair_id"]: "maybe"}), "张三")
    short = d / "short.csv"
    with short.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows[1:])
    with pytest.raises(ev.EvalError, match="不一致"):
        ev.import_labels(conn, v, short, "张三")
    assert conn.execute("SELECT count(*) FROM task4.eval_label").fetchone()[0] == 0
    # 双方关联同一部 IMDb 作品、导演一致却标成 different：列为矛盾项，但标注原样保存
    same_work = next(r for r in rows if r["left_tconst"] and r["left_tconst"] == r["right_tconst"]
                     and set(r["left_directors"].split(" | ")) & set(r["right_directors"].split(" | ")))
    out = ev.import_labels(conn, v, label_file(d / "eval_pairs.csv", {same_work["pair_id"]: "different"}), "张三")
    assert [c["pair_id"] for c in out["contradictions"]] == [same_work["pair_id"]]
    assert conn.execute("SELECT label FROM task4.eval_label WHERE pair_id = %s", (same_work["pair_id"],)).fetchone() \
        == ("different",)


# ---------------------------------------------------------------- 簇抽查

def test_cluster_sample_export_import_and_results(conn, world):
    fx, v, d = world
    freeze_by_hand(conn, fx.eid, f"{A}F01", f"{A}F02")       # 页面簇 Unlisted Film Two 在开发池
    out = ev.export_clusters(conn, v, d, plan={"3+": 1, "2": 1})
    rows = read_rows(d / "cluster_sample.csv")
    clusters = {r["movie_id"] for r in rows}
    assert out["clusters"] == len(clusters) == 2 and out["big_clusters"] == 1 and out["pairs_clusters"] == 1
    assert movie_id_for("imdb:tt0000101") in clusters                                 # 四个版本的 Casablanca
    assert movie_id_for(f"asin:{A}F01") not in clusters                               # 开发池里的簇不抽
    assert {r["asin"] for r in rows if r["movie_id"] == movie_id_for("imdb:tt0000101")} == \
        {f"{A}A01", f"{A}A02", f"{A}A03", f"{A}J01"}
    with pytest.raises(ev.EvalError, match="不覆盖"):
        ev.export_clusters(conn, v, d)
    # 标注：Casablanca 簇混入了 J01；另一个簇全部同一部
    casa = movie_id_for("imdb:tt0000101")
    for r in rows:
        r["cluster_label"] = "mixed" if r["movie_id"] == casa and r["asin"] == f"{A}A01" else \
            ("same_work" if r["movie_id"] != casa and r["asin"] == rows[-1]["asin"] else "")
        r["outlier"] = "x" if r["asin"] == f"{A}J01" else ""
    path = d / "clusters_labeled.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    res = ev.import_clusters(conn, v, path, "张三")
    assert res["labeled"] == 2 and res["mixed"] == 1 and res["unlabeled"] == 0
    c = ev.cluster_results(conn, ev.eval_set_id(v))
    assert c["sampled"] == 2 and c["mixed"] == 1 and c["mixed_clusters"][0]["problem_asins"] == [f"{A}J01"]
    ev.export_pairs(conn, v, d, plan=SMALL)
    m = ev.metrics(conn, v)
    assert m["clusters"]["mixed"] == 1 and "张三" in m["annotators"]
    assert "簇抽查" in ev.render_metrics(m)
    # same_work 的簇不能带 outlier
    for r in rows:
        if r["cluster_label"] == "same_work":
            r["outlier"] = "x"
    bad = d / "bad.csv"
    with bad.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    with pytest.raises(ev.EvalError, match="outlier"):
        ev.import_clusters(conn, v, bad, "张三")
