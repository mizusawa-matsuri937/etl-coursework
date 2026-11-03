"""任务 9 血缘链抽查评价工具（spec AC18；R46–R49）。预期值手算或直接对照导出文件与上游表。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_evaluate.py
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json

import pytest

import _task9_helpers as h
from etl_task9.evaluate import (QUOTA, EvalError, eval_set_id, export_eval_set, import_labels, metrics,
                                sample_items, write_draft)
from etl_task9.runner import execute_lineage


@pytest.fixture
def ev(conn, make_settings, tmp_path):
    t = h.seed(conn, make_settings, tmp_path)
    h.add_products(conn, t)
    t.review_rows = h.add_review_mapping(conn, t)
    t.lv = execute_lineage(conn, t.inputs(), snapshot_root=t.snapshot_root, snapshot_workers=2,
                           batch_size=500).lineage_version_id
    t.dir = tmp_path / "eval"
    t.out = export_eval_set(conn, t.lv, t.dir, imdb_root="/nonexistent")
    with open(t.dir / "eval_items.csv", encoding="utf-8", newline="") as fh:
        t.rows = list(csv.DictReader(fh))
    return t


def _imdb_root(ev, tmp_path, *, conn):
    """为只有 IMDb 证据的项按装载表行写出原始 gz 文件（行号同装载行，其余行为占位），重新导出（force）。"""
    files: dict[tuple[str, str], dict[int, str]] = {}
    for r in ev.rows:
        evs = json.loads(r["evidence"])
        if evs and all(e["kind"] == "imdb" for e in evs):
            for e in evs:
                vals = json.loads(e["loaded_row"]).values()
                files.setdefault((e["snapshot"], e["file"]), {})[int(e["line_no"])] = \
                    "\t".join("\\N" if v is None else str(v) for v in vals)
    root = tmp_path / "imdb"
    for (snap, name), lines in files.items():
        path = root / snap.removeprefix("imdb_") / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            for n in range(1, max(lines) + 1):
                fh.write(lines.get(n, "placeholder") + "\n")
    out = export_eval_set(conn, ev.lv, ev.dir, imdb_root=root, force=True)
    ev.rows = _read(ev.dir / "eval_items.csv")
    return out


def _read(path):
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _write(path, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def test_ac18_quota_and_fill_order():
    """配额 L1 15、L2 15、L3 10、L4 10、L5 10；L4 少 3、L5 少 4 时，7 个缺额先由 L2 的剩余候选补（L2 只剩 5 个），
    L1 没有剩余，再由 L3 补 2 个，总数仍为 60。"""
    pools = {"L1": [{"key": f"a{n}"} for n in range(15)], "L2": [{"key": f"b{n}"} for n in range(20)],
             "L3": [{"key": f"c{n}"} for n in range(30)], "L4": [{"key": f"d{n}"} for n in range(7)],
             "L5": [{"key": f"e{n}"} for n in range(6)]}
    items, strata = sample_items(pools)
    assert sum(QUOTA.values()) == 60 and len(items) == 60
    assert {s: (v["actual"], v["filled"]) for s, v in strata.items()} == \
        {"L1": (15, 0), "L2": (20, 5), "L3": (12, 2), "L4": (7, 0), "L5": (6, 0)}
    assert [x["key"] for x in items if x["stratum"] == "L2"] == [f"b{n}" for n in range(20)]   # 按池内 md5 顺序
    assert len({x["item_id"] for x in items}) == 60


def test_ac18_export_strata_by_md5_and_review_table(conn, ev):
    """夹具导出：每层取 md5(选择键) 最小的非空值；审核表每项有处理路径与原始证据摘录（页面原文 ≤ 300 字符、IMDb 文件
    不可读时给出装载表行、评论正文前 80 个字符），eval_item 存路径与证据的 SHA-256。"""
    keys = conn.execute("SELECT selections_stage_key FROM task9.lineage_version WHERE lineage_version_id = %s",
                        (ev.lv,)).fetchone()[0]
    l1 = [r[0] for r in conn.execute(
        "SELECT selection_id FROM task9.field_selection WHERE stage_key = %s AND entity_type = 'movie' AND value_text "
        "IS NOT NULL AND field_name IN ('title', 'original_title', 'release_year', 'runtime_minutes', 'mpaa_rating', "
        "'effective_tconst', 'inferred_upper_bound') ORDER BY md5(selection_id) LIMIT 15", (keys,))]
    got = [r[0] for r in conn.execute("SELECT selection_key FROM task9.eval_item WHERE eval_set_id = %s "
                                      "AND stratum = 'L1' ORDER BY item_id", (eval_set_id(ev.lv),))]
    assert got[:len(l1)] == l1
    assert len(ev.rows) == ev.out["items"] == sum(v["actual"] for v in ev.out["strata"].values())
    assert {r["stratum"] for r in ev.rows} == set(QUOTA)
    kinds = set()
    for r in ev.rows:
        assert r["path"] and r["final_value"]
        for e in json.loads(r["evidence"]):
            kinds.add(e["kind"])
            if e["kind"] == "page":
                assert len(e["text"]) <= 300 and e["body_sha256"] and e["snapshot_path"]
            if e["kind"] == "imdb":
                assert e["file_unavailable"] and e["loaded_row"]
            if e["kind"] == "review":
                assert len(e.get("text_80", "")) <= 80 and e["raw_sha256"]
    assert {"page", "imdb", "review"} <= kinds
    stored = dict(conn.execute("SELECT item_id, path_sha256 FROM task9.eval_item WHERE eval_set_id = %s",
                               (eval_set_id(ev.lv),)).fetchall())
    assert all(stored[r["item_id"]] == hashlib.sha256(r["path"].encode()).hexdigest() for r in ev.rows)
    assert "label" in ev.rows[0] and "trace_status" not in ev.rows[0]          # 不给标注人看系统状态列
    assert (ev.dir / "LABELING.md").exists()


def test_ac18_draft_reads_raw_evidence_files(ev):
    """草稿只读审核表与原始证据文件（不传数据库连接）：每条页面证据都记录读快照的结果；改动一个快照后，该项的
    ai_read 写明正文哈希不一致；IMDb 原始文件不可读的项标 unsure 并存疑。"""
    n = write_draft(ev.dir / "eval_items.csv", ev.dir / "draft.csv", snapshot_root=ev.snapshot_root)
    draft = _read(ev.dir / "draft.csv")
    assert n == len(draft) == len(ev.rows)
    for d in draft:
        ev_ = json.loads(d["evidence"])
        pages = [e for e in ev_ if e["kind"] == "page"]
        assert d["ai_read"].count("快照") >= len(pages)
        assert d["ai_label"] in ("correct", "unsure") and d["ai_doubt"] in ("true", "false")
        if any(e["kind"] == "imdb" for e in ev_):
            assert "IMDb 原始文件不可读" in d["ai_reason"] and d["ai_label"] == "unsure"
    target = next(d for d in draft if any(e["kind"] == "page" for e in json.loads(d["evidence"])))
    asin = next(e["asin"] for e in json.loads(target["evidence"]) if e["kind"] == "page")
    (ev.snapshot_root / "snapshots" / f"{asin}.html.gz").write_bytes(gzip.compress(b"changed"))
    write_draft(ev.dir / "eval_items.csv", ev.dir / "draft2.csv", snapshot_root=ev.snapshot_root)
    again = {d["item_id"]: d for d in _read(ev.dir / "draft2.csv")}
    assert f"快照 {asin}：正文哈希不一致" in again[target["item_id"]]["ai_read"]
    assert "快照正文与登记哈希不一致" in again[target["item_id"]]["ai_reason"]


def test_ac18_draft_reads_imdb_raw_lines(conn, ev, tmp_path):
    """IMDb 原始文件可读时，审核表给出原行，草稿逐项写出读到的行；最终值出现在原行里的项建议 correct、不存疑。"""
    _imdb_root(ev, tmp_path, conn=conn)
    write_draft(ev.dir / "eval_items.csv", ev.dir / "draft.csv", snapshot_root=ev.snapshot_root)
    only = [d for d in _read(ev.dir / "draft.csv")
            if (x := json.loads(d["evidence"])) and all(e["kind"] == "imdb" for e in x)]
    assert only
    for d in only:
        assert all(e["line"] and not e.get("file_unavailable") for e in json.loads(d["evidence"]))
        assert d["ai_read"].startswith("IMDb ")
    good = [d for d in only if d["ai_label"] == "correct"]
    assert good and all(d["ai_doubt"] == "false" for d in good)


def _labeled(ev):
    write_draft(ev.dir / "eval_items.csv", ev.dir / "draft.csv", snapshot_root=ev.snapshot_root)
    rows = _read(ev.dir / "draft.csv")
    for r in rows:                       # 默认：采纳 AI 建议；存疑项填外部依据
        r["label"] = r["ai_label"]
        r["human_evidence"] = "核对快照原文与 IMDb 原行" if r["ai_doubt"] == "true" else ""
    return rows


def test_ac18_import_rejects_changes_without_evidence(conn, ev, tmp_path):
    """改判（与 AI 建议不同）没有外部依据、incorrect 没有错误类型、note 原样粘贴 ai_reason、标注值不在闭集，都整批
    拒绝；标为 unsure 的改判不需要依据；校验通过后全部导入。"""
    _imdb_root(ev, tmp_path, conn=conn)
    rows = _labeled(ev)
    sid = eval_set_id(ev.lv)
    bad = [dict(r) for r in rows]
    i = next(k for k, r in enumerate(bad) if r["ai_doubt"] == "false")
    bad[i]["label"] = "incorrect"
    bad[i]["error_type"] = "wrong_value"
    _write(ev.dir / "bad1.csv", bad)
    with pytest.raises(EvalError, match="与 AI 建议不同的项须填 human_evidence"):
        import_labels(conn, sid, ev.dir / "bad1.csv", "付林轩")
    bad[i]["human_evidence"] = "快照里是另一个值"
    bad[i]["error_type"] = ""
    _write(ev.dir / "bad2.csv", bad)
    with pytest.raises(EvalError, match="error_type"):
        import_labels(conn, sid, ev.dir / "bad2.csv", "付林轩")
    bad[i]["error_type"] = "wrong_value"
    bad[0]["note"] = bad[0]["ai_reason"]
    _write(ev.dir / "bad3.csv", bad)
    with pytest.raises(EvalError, match="note 不得原样粘贴"):
        import_labels(conn, sid, ev.dir / "bad3.csv", "付林轩")
    bad[0]["note"] = ""
    bad[1]["label"] = "maybe"
    _write(ev.dir / "bad4.csv", bad)
    with pytest.raises(EvalError, match="label 必须是"):
        import_labels(conn, sid, ev.dir / "bad4.csv", "付林轩")
    assert conn.execute("SELECT count(*) FROM task9.eval_label").fetchone()[0] == 0
    bad[1]["label"], bad[1]["human_evidence"] = "unsure", ""
    _write(ev.dir / "good.csv", bad)
    out = import_labels(conn, sid, ev.dir / "good.csv", "付林轩")
    assert out["imported"] == len(rows)
    assert conn.execute("SELECT label, error_type FROM task9.eval_label WHERE item_id = %s",
                        (bad[i]["item_id"],)).fetchone() == ("incorrect", "wrong_value")


def test_ac18_metrics_match_hand_count_and_undefined_denominator(conn, ev):
    """指标与手算一致：正确率 = correct /（correct + incorrect），unsure 单列；L4 全部 unsure 时分母为 0 记“无定义”。"""
    rows = _labeled(ev)
    for n, r in enumerate(rows):
        r["error_type"], r["human_evidence"] = "", "人工核对"
        if r["stratum"] == "L4":
            r["label"] = "unsure"
        elif n % 4 == 0:
            r["label"], r["error_type"] = "incorrect", "missing_step"
        else:
            r["label"] = "correct"
    _write(ev.dir / "labels.csv", rows)
    import_labels(conn, eval_set_id(ev.lv), ev.dir / "labels.csv", "付林轩")
    m = metrics(conn, eval_set_id(ev.lv))
    c = sum(r["label"] == "correct" for r in rows)
    w = sum(r["label"] == "incorrect" for r in rows)
    assert m["overall"]["correct"] == c and m["overall"]["denominator"] == c + w
    assert m["overall"]["value"] == round(c / (c + w), 4)
    assert m["overall"]["unsure"] == sum(r["stratum"] == "L4" for r in rows)
    if any(r["stratum"] == "L4" for r in rows):
        assert m["by_stratum"]["L4"]["value"] == "无定义"
    for s in ("L1", "L2", "L3", "L5"):
        cs = sum(r["label"] == "correct" for r in rows if r["stratum"] == s)
        ws = sum(r["label"] == "incorrect" for r in rows if r["stratum"] == s)
        assert m["by_stratum"][s]["value"] == (round(cs / (cs + ws), 4) if cs + ws else "无定义")
    assert m["error_types"] == {"missing_step": w} and m["annotators"] == ["付林轩"]


def test_ac18_cli_export_draft_import_metrics(conn, ev, tmp_path, monkeypatch, capsys):
    """命令行四步：export 写到 outputs/task9/<抽取>/<lv>/eval/，draft 生成 eval_items_draft.csv，import 导入，metrics 写
    metrics.md 并声明只代表抽查样本。"""
    from etl_task9 import evaluate
    monkeypatch.setenv("ETL_OUTPUT_DIR", str(tmp_path / "outputs"))
    d = tmp_path / "outputs" / "task9" / ev.eid / ev.lv / "eval"
    assert evaluate.main(["export", "--version", ev.lv, "--force"]) == 0
    assert json.loads(capsys.readouterr().out)["items"] == len(ev.rows)
    assert evaluate.main(["draft", "--version", ev.lv, "--snapshot-root", str(ev.snapshot_root)]) == 0
    assert json.loads(capsys.readouterr().out)["draft_rows"] == len(ev.rows)
    rows = _read(d / "eval_items_draft.csv")
    for r in rows:
        r["label"], r["human_evidence"] = "unsure", ""
    _write(d / "labels.csv", rows)
    assert evaluate.main(["import", "--version", ev.lv, "--file", str(d / "labels.csv"), "--annotator", "付林轩"]) == 0
    assert json.loads(capsys.readouterr().out)["imported"] == len(rows)
    assert evaluate.main(["metrics", "--version", ev.lv]) == 0
    m = json.loads(capsys.readouterr().out)
    assert m["overall"]["value"] == "无定义" and m["overall"]["unsure"] == len(rows)
    assert "不代表全量正确率" in (d / "metrics.md").read_text()
