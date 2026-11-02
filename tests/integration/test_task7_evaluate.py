"""任务 7 评价工具（spec R61–R66，AC15）：开发池冻结、七层抽样与 conflict_unresolved 保底、盲测表不泄露系统结果、
AI 草稿不读选择表且 ai_reason 不入库、缺依据的存疑项被拒绝、指标与手算一致。

抽样逻辑（pick、sample_items）与指标（metrics_of）是纯函数，用合成候选池按 120 项真实配额验证；
需要数据库的部分用 eval_world（约 40 部作品）并把配额缩小到 14 项（monkeypatch），其余逻辑相同。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task7_evaluate.py
"""

from __future__ import annotations

import csv
import hashlib
import inspect
import json
from collections import Counter
from pathlib import Path

import pytest

import _task7_helpers as h
from _task7_worlds import eval_world, small_world
from etl_task7 import evaluate as ev
from etl_task7.evaluate import EvalError
from etl_task7.runner import execute_conflict
from task7 import world as w

pytestmark = pytest.mark.integration

SMALL_SLOTS = {"title": 2, "release_year": 2, "runtime_minutes": 2, "director": 1, "credit_align": 1, "billing": 1,
               "genres": 1, "mpaa_rating": 2, "publisher": 1, "product_release_date": 1}


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def shrink_plan(monkeypatch) -> int:
    """把 120 项配额缩成 14 项（每个抽样位 1–2 项），用于小世界。"""
    slots = tuple({**s, "n": SMALL_SLOTS[s["kind"]], "reserve": None if s["reserve"] is None else (s["reserve"][0], 1)}
                  for s in ev.SLOTS)
    layers: dict[str, int] = {}
    for s in slots:
        layers[s["layer"]] = layers.get(s["layer"], 0) + s["n"]
    monkeypatch.setattr(ev, "SLOTS", slots)
    monkeypatch.setattr(ev, "LAYER_PLAN", layers)
    monkeypatch.setattr(ev, "TARGET_TOTAL", sum(layers.values()))
    return sum(layers.values())


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------ 合成候选池（纯函数测试）

def synthetic_pool(kind: str, n: int, buckets: tuple[str, ...], unresolved: int = 0) -> list[dict]:
    """某抽样位的合成候选池：n 项，桶轮流分配，前 unresolved 项为 conflict_unresolved。"""
    out = []
    for k in range(n):
        bad = k < unresolved
        b = "conflict_unresolved" if bad else buckets[k % len(buckets)]
        field = f"credit_align:cast:ps_{k}" if kind == "credit_align" else (f"billing:cast:ps_{k}" if kind == "billing" else kind)
        out.append(ev._entry("edition" if kind in ("publisher", "product_release_date") else "movie", f"e_{kind}_{k}",
                             field, b, "conflict_unresolved" if bad else "resolved", None if bad else "v", "none"))
    return out


def full_pools(**short: int) -> dict[str, list[dict]]:
    pools = {}
    for s in ev.SLOTS:
        count = short.get(s["kind"], 60)
        buckets = s["buckets"] if s["reserve"] is None else (*s["buckets"], s["reserve"][0])
        pools[s["kind"]] = synthetic_pool(s["kind"], count, buckets, unresolved=6 if s["floor"] else 0)
    return pools


def test_ac15_quota_plan_is_seven_layers_totalling_120():
    layers = Counter()
    for s in ev.SLOTS:
        layers[s["layer"]] += s["n"]
    assert dict(layers) == ev.LAYER_PLAN == {"E1": 15, "E2": 15, "E3": 20, "E4": 20, "E5": 15, "E6": 15, "E7": 20}
    assert sum(layers.values()) == ev.TARGET_TOTAL == 120
    e4 = {s["kind"]: s["n"] for s in ev.SLOTS if s["layer"] == "E4"}
    e7 = {s["kind"]: s["n"] for s in ev.SLOTS if s["layer"] == "E7"}
    assert e4 == {"director": 8, "credit_align": 8, "billing": 4} and e7 == {"publisher": 10, "product_release_date": 10}
    assert ev.FILL_ORDER == ("E3", "E4", "E1", "E7")
    assert {s["layer"] for s in ev.SLOTS if s["floor"]} == {"E1", "E2", "E3", "E6"}


def test_ac15_sampling_fills_every_layer_and_keeps_unresolved_floor():
    items, strata = ev.sample_items(full_pools())
    assert len(items) == 120 and len({(x["entity_id"], x["target_field"]) for x in items}) == 120
    assert {k: v["actual"] for k, v in strata.items()} == ev.LAYER_PLAN
    assert all(v["filled"] == 0 for v in strata.values())
    for layer in ("E1", "E2", "E3", "E6"):
        got = [x for x in items if x["stratum"] == layer and x["unresolved"]]
        assert len(got) >= 4, layer
    credit = [x for x in items if base(x) == "credit_align"]
    assert len(credit) == 8 and sum(1 for x in credit if x["bucket"] == "L4") >= 4          # 署名对齐至少一半取 L4
    assert sum(1 for x in items if base(x) == "billing") == 4
    assert sum(1 for x in items if base(x) == "director") == 8
    assert sum(1 for x in items if x["stratum"] == "E5" and x["bucket"] == "has_unmapped") >= 7   # 含未映射词面的优先
    assert items == ev.sample_items(full_pools())[0]                       # 确定性：同输入同输出


def base(x: dict) -> str:
    return ev.base_field(x["target_field"])


def test_ac15_unresolved_floor_is_min_of_available_and_four():
    pools = full_pools()
    pools["title"] = synthetic_pool("title", 40, ("none", "formatting_or_alias"), unresolved=2)     # 只有 2 项可用
    items, _ = ev.sample_items(pools)
    assert sum(1 for x in items if x["stratum"] == "E1" and x["unresolved"]) == 2
    pools["title"] = synthetic_pool("title", 40, ("none", "formatting_or_alias"), unresolved=0)     # 一项也没有
    items, strata = ev.sample_items(pools)
    assert sum(1 for x in items if x["stratum"] == "E1" and x["unresolved"]) == 0 and strata["E1"]["actual"] == 15


def test_ac15_shortfall_is_filled_in_e3_e4_e1_e7_order():
    pools = full_pools(genres=10)                                          # E5 少 5 项
    items, strata = ev.sample_items(pools)
    assert len(items) == 120
    assert strata["E5"]["actual"] == 10 and strata["E3"]["filled"] == 5 and strata["E3"]["actual"] == 25
    assert all(strata[k]["filled"] == 0 for k in ("E1", "E2", "E4", "E6", "E7"))
    pools = full_pools(genres=10, runtime_minutes=20)                      # E3 没有余项，转 E4
    items, strata = ev.sample_items(pools)
    assert len(items) == 120 and strata["E3"]["filled"] == 0 and strata["E4"]["filled"] == 5


# ------------------------------------------------------------------ 指标（手算）

def lab(stratum, field, status, sys_value, action, value=None):
    return {"stratum": stratum, "target_field": field, "system_status": status, "system_value": sys_value,
            "human_action": action, "human_winning_value": value}


def test_ac15_metrics_match_hand_calculation_and_undefined_denominators():
    rows = [lab("E2", "release_year", "resolved", "1999", "resolve", " 1999 "),        # 动作对、值对
            lab("E2", "release_year", "resolved", "1999", "resolve", "2000"),          # 动作对、值错
            lab("E3", "runtime_minutes", "resolved", "100", "keep_conflict"),          # 动作错
            lab("E3", "runtime_minutes", "conflict_unresolved", None, "keep_conflict"),  # 动作对
            lab("E1", "title", "conflict_unresolved", None, "resolve", "X"),           # 动作错
            lab("E1", "title", "resolved", "A", "unsure"),
            lab("E1", "title", "conflict_unresolved", None, "unsure"),
            lab("E6", "mpaa_rating", "conflict_unresolved", None, "keep_conflict")]    # 动作对
    m = ev.metrics_of(rows)
    assert (m["labeled"], m["unsure"], m["decided"]) == (8, 2, 6)
    assert (m["action_correct"], m["action_accuracy"]) == (4, round(4 / 6, 4))
    assert (m["value_pairs"], m["value_correct"], m["value_accuracy"]) == (2, 1, 0.5)
    assert (m["system_conflict"], m["conflict_both"], m["conflict_precision"]) == (3, 2, round(2 / 3, 4))
    assert (m["human_conflict"], m["conflict_recalled"], m["conflict_recall"]) == (3, 2, round(2 / 3, 4))
    only_unsure = ev.metrics_of([lab("E1", "title", "resolved", "A", "unsure")])
    assert only_unsure["decided"] == 0 and only_unsure["unsure"] == 1
    assert only_unsure["action_accuracy"] is None and only_unsure["value_accuracy"] is None
    assert only_unsure["conflict_precision"] is None and only_unsure["conflict_recall"] is None


def test_ac15_value_comparison_is_field_aware():
    n = ev.norm_value
    assert n("release_year", "1999 ") == n("release_year", "1999") and n("runtime_minutes", "097") != n("runtime_minutes", "98")
    assert n("title", "  The  Film ") == n("title", "the film")
    assert n("genres", "Drama|Action") == n("genres", " action | DRAMA ")
    assert n("director", "Alfred Hitchcock|Jess Franco") == n("director", "jess franco | alfred hitchcock")
    assert n("director", "Alfred Hitchcock") != n("director", "Alred Hitchcock")
    assert n("product_release_date", "2003-05-20") == n("product_release_date", "2003-05-20")
    for field, bad in (("release_year", "nineteen"), ("product_release_date", "2003/05/20"), ("genres", "Drama|Gloom"),
                       ("credit_align", "Gary Busey"), ("mpaa_rating", "Rated R"), ("billing", "first")):
        with pytest.raises(ValueError):
            ev.validate_value(field, bad)
    for field, good in (("release_year", "1999"), ("product_release_date", "2003-05-20"), ("genres", "Drama|Sci-Fi"),
                        ("credit_align", "nm0000001"), ("credit_align", "none"), ("mpaa_rating", "pg-13"), ("billing", "2")):
        ev.validate_value(field, good)


# ------------------------------------------------------------------ AI 草稿只接收盲测行

def test_ac15_ai_draft_takes_only_a_blind_row_and_flags_doubt():
    assert list(inspect.signature(ev.generate_ai_draft).parameters) == ["row"]            # 没有连接，读不到选择表
    ctx = json.dumps({"movie_title": "Seven Samurai", "tconst": "tt1", "imdb_title": "Seven Samurai"})

    def draft(field, evidence, context=ctx):
        return ev.generate_ai_draft({"target_field": field, "entity_context": context,
                                     "raw_candidates_json": json.dumps(evidence)})

    d = draft("release_year", [{"source": "imdb_t4_link", "source_key": "tt1", "value": "1954", "year": 1954},
                               {"source": "amazon_title_year", "source_key": "a", "value": "1954", "year": 1954}])
    assert (d["ai_action"], d["ai_winning_value"], d["ai_doubt"]) == ("resolve", "1954", "false") and d["ai_reason"]
    d = draft("release_year", [{"source": "imdb_t4_link", "source_key": "tt1", "value": "1954", "year": 1954},
                               {"source": "amazon_title_year", "source_key": "a", "value": "1998", "year": 1998}])
    assert (d["ai_action"], d["ai_doubt"]) == ("keep_conflict", "true")
    d = draft("runtime_minutes", [{"source": "imdb_runtime", "source_key": "tt1", "value": 82},
                                  {"source": "page_runtime", "source_key": "a", "value": "1 hour and 24 minutes"}])
    assert (d["ai_action"], d["ai_winning_value"], d["ai_doubt"]) == ("resolve", "82", "false")      # 84 分钟，差 2
    d = draft("genres", [{"source": "imdb_genres", "source_key": "tt1", "value": ["Comedy"]},
                         {"source": "page_breadcrumb", "source_key": "a", "value": "Movies & TV"},
                         {"source": "page_genre", "source_key": "b", "value": "Science Fiction"}])
    assert (d["ai_winning_value"], d["ai_doubt"]) == ("Comedy|Sci-Fi", "false")                       # 噪声词不算未识别
    d = draft("mpaa_rating", [{"source": "page_rating", "source_key": "a", "value": "R (Restricted)"},
                              {"source": "page_rating", "source_key": "b", "value": "PG (Parental Guidance)"}])
    assert (d["ai_action"], d["ai_doubt"]) == ("keep_conflict", "true")
    d = draft("mpaa_rating", [{"source": "page_rating", "source_key": "a", "value": "R (Restricted)"},
                              {"source": "page_rating", "source_key": "b", "value": "NR (Not Rated)"}])
    assert (d["ai_action"], d["ai_winning_value"]) == ("resolve", "R")
    d = draft("credit_align:cast:ps_1", [{"source": "page_credit", "source_key": "ps_1", "value": ["Garey Busey"]},
                                         {"source": "imdb_credit", "source_key": "nm1", "value": "Gary Busey"},
                                         {"source": "imdb_credit", "source_key": "nm2", "value": "Someone Else"}])
    assert (d["ai_action"], d["ai_winning_value"]) == ("resolve", "nm1") and d["ai_doubt"] == "true"
    d = draft("billing:cast:ps_1", [{"source": "page_billing", "source_key": "a", "value": 1},
                                    {"source": "page_billing", "source_key": "b", "value": 2},
                                    {"source": "page_billing", "source_key": "c", "value": 1}])
    assert (d["ai_action"], d["ai_winning_value"], d["ai_doubt"]) == ("resolve", "1", "true")


# ------------------------------------------------------------------ 开发池冻结

def test_ac15_freeze_applies_the_migration_and_is_idempotent(conn):
    s = h.seed(conn, small_world(10))
    conn.execute("DROP SCHEMA task7 CASCADE")
    conn.execute("DELETE FROM public.schema_migration WHERE version = '601'")
    ev.apply_task7_schema(conn)                                           # freeze 命令内部做的第一步
    first = ev.freeze_dev_pool(conn, s.eid, "map0002")
    assert first["status"] == "frozen" and first["dev_movies"] == 2 and first["total_movies"] == 10     # ceil(10 / 5)
    assert first["frozen_before_first_run"] is True
    movies = sorted((r[0] for r in conn.execute("SELECT movie_id FROM task4.movie WHERE mapping_version_id = 'map0002'")),
                    key=lambda m: (md5(m), m))
    frozen = sorted(r[0] for r in conn.execute("SELECT movie_id FROM task7.dev_pool WHERE extraction_id = %s", (s.eid,)))
    assert frozen == sorted(movies[:2])                                    # md5(movie_id) 排序的前 20%
    again = ev.freeze_dev_pool(conn, s.eid, "map0002")
    assert again["status"] == "already_frozen" and again["dev_movies"] == 2
    assert conn.execute("SELECT count(*) FROM task7.dev_pool").fetchone()[0] == 2


# ------------------------------------------------------------------ 导出：排除开发池、盲测、草稿

@pytest.fixture
def evaluated(conn, monkeypatch, tmp_path):
    total = shrink_plan(monkeypatch)
    s = h.seed(conn, eval_world())
    res = ev.freeze_dev_pool(conn, s.eid, "map0002")
    assert res["status"] == "frozen"
    r = execute_conflict(conn, s.inputs(), batch_size=7)
    assert r.status == "succeeded", r
    out = ev.export_eval_set(conn, r.conflict_version_id, tmp_path / "eval")
    return conn, s, r.conflict_version_id, tmp_path / "eval", out, total


def test_ac15_export_excludes_the_dev_pool_and_never_runs_before_freezing(conn, monkeypatch, tmp_path):
    shrink_plan(monkeypatch)
    s = h.seed(conn, eval_world())
    r = execute_conflict(conn, s.inputs(), batch_size=7)
    with pytest.raises(EvalError, match="开发池尚未冻结"):
        ev.export_eval_set(conn, r.conflict_version_id, tmp_path / "eval")


def test_ac15_export_samples_by_quota_without_dev_pool_movies_or_editions(evaluated):
    conn, s, cv, directory, out, total = evaluated
    assert out["items_total"] == total == 14 and out["frozen_before_first_run"] is True
    dev = {r[0] for r in conn.execute("SELECT movie_id FROM task7.dev_pool")}
    assert len(dev) == -(-len(s.world.movies) // 5)
    items = conn.execute("SELECT item_id, stratum, entity_type, entity_id, target_field, system_status FROM task7.eval_item "
                         "WHERE eval_set_id = %s", (out["eval_set_id"],)).fetchall()
    assert len(items) == 14
    assert Counter(i[1] for i in items) == {"E1": 2, "E2": 2, "E3": 2, "E4": 3, "E5": 1, "E6": 2, "E7": 2}
    for _id, _stratum, etype, eid, _field, _status in items:
        if etype == "movie":
            assert eid not in dev
        else:
            movies = conn.execute("SELECT movie_ids FROM task7.edition_resolved WHERE conflict_version_id = %s AND asin = %s",
                                  (cv, eid)).fetchone()[0]
            assert not dev & set(movies)                                    # 任一关联作品在开发池的 ASIN 排除
    assert all(status in ("resolved", "conflict_unresolved") for *_, status in items)
    strata = conn.execute("SELECT strata FROM task7.eval_set WHERE eval_set_id = %s", (out["eval_set_id"],)).fetchone()[0]
    assert strata["E1"]["target"] == 2 and strata["E4"]["kinds"] == {"director": 1, "credit_align": 1, "billing": 1}
    for layer in ("E1", "E2", "E3", "E6"):
        want = min(strata[layer]["available_unresolved"], 2)               # 缩小后保底按 min(可用, 4) ∧ 配额
        got = sum(1 for i in items if i[1] == layer and i[5] == "conflict_unresolved")
        assert got >= want, layer
    unresolved = {i[1] for i in items if i[5] == "conflict_unresolved"}
    assert {"E2", "E3"} <= unresolved                                      # 熔断作品的年份与片长（Hl*）被保底选中


def test_ac15_blind_table_hides_system_output_and_ids_are_shuffled(evaluated):
    conn, s, cv, directory, out, _ = evaluated
    blind = read_csv(directory / "eval_items.csv")
    assert list(blind[0]) == list(ev.BLIND_COLUMNS) and len(blind) == 14
    assert all(c not in blind[0] for c in ("stratum", "system_value", "system_status", "conflict_type", "ai_reason"))
    assert all(not r["human_action"] and not r["human_winning_value"] for r in blind)
    ids = [r["item_id"] for r in blind]
    assert ids == [f"t7_eval_{n:04d}" for n in range(1, 15)]
    keys = [md5(f"{r['entity_id']}:{r['target_field']}") for r in blind]
    assert keys == sorted(keys)                                             # 编号按 md5(entity_id:target_field) 全局打乱
    text = (directory / "eval_items.csv").read_text(encoding="utf-8")
    for word in ("conflict_unresolved", "cross_source_mismatch_halt", "adopted", "superseded_by_specific", "E1"):
        assert word not in text
    for r in blind:
        evidence = json.loads(r["raw_candidates_json"])
        assert evidence == sorted(evidence, key=lambda e: (e["source"], e["source_key"]))
        assert json.loads(r["entity_context"])


def test_ac15_blind_evidence_is_independent_of_candidate_state_and_never_reads_task7(evaluated, tmp_path):
    conn, s, cv, directory, out, _ = evaluated
    before = (directory / "eval_items.csv").read_bytes()
    conn.execute("UPDATE task7.field_candidate SET status = 'rejected', candidate_id = -candidate_id")   # 打乱候选状态与顺序
    conn.execute("UPDATE task7.movie_field_selection SET winning_value = 'LEAK', reason = 'LEAK' "
                 "WHERE status = 'resolved'")
    again = ev.export_eval_set(conn, cv, tmp_path / "eval2", force=True)
    assert (tmp_path / "eval2" / "eval_items.csv").read_bytes() == before
    assert again["items_total"] == 14

    class Spy:
        def __init__(self, inner):
            self.inner, self.sqls = inner, []

        def execute(self, sql, params=None):
            self.sqls.append(str(sql))
            return self.inner.execute(sql, params)

    spy = Spy(conn)
    items = conn.execute("SELECT item_id, entity_type, entity_id, target_field FROM task7.eval_item").fetchall()
    inputs = ev.inputs_of(ev.cv_row(conn, cv))
    for item_id, etype, eid, field in items:
        item = {"item_id": item_id, "entity_type": etype, "entity_id": eid, "target_field": field}
        if field.startswith("billing:"):
            item["page_person_ids"] = [field.split(":")[2]]
        ev.blind_row(spy, inputs, item)
    assert spy.sqls and not [q for q in spy.sqls if "task7." in q]         # 盲测证据只读上游表


def test_ac15_draft_is_derived_from_blind_rows_and_reason_stays_in_the_draft(evaluated):
    conn, s, cv, directory, out, _ = evaluated
    drafts = read_csv(directory / "eval_items_draft.csv")
    blind = read_csv(directory / "eval_items.csv")
    assert len(drafts) == len(blind) == 14 and list(drafts[0]) == list(ev.DRAFT_COLUMNS)
    for d, b in zip(drafts, blind):
        assert {k: d[k] for k in ev.BLIND_COLUMNS[:6]} == {k: b[k] for k in ev.BLIND_COLUMNS[:6]}
        regenerated = ev.generate_ai_draft(b)                              # 只用盲测行就能复现，无需数据库
        assert {k: d[k] for k in regenerated} == regenerated
        assert d["ai_reason"] and d["ai_doubt"] in ("true", "false")
        assert not d["human_action"] and not d["human_winning_value"]
    cols = {r[0] for r in conn.execute("SELECT column_name FROM information_schema.columns "
                                       "WHERE table_schema = 'task7' AND table_name IN ('eval_item', 'eval_label')")}
    assert "ai_reason" not in cols and "ai_doubt" in cols


# ------------------------------------------------------------------ 导入：存疑项需依据，ai_reason 不入库

def labeled_rows(directory: Path, conn, sid) -> list[dict[str, str]]:
    """把草稿填成标注：人工结论取系统自己的答案（使一切一致），存疑项附依据。"""
    sysrows = {r[0]: r[1:] for r in conn.execute(
        "SELECT item_id, system_status, system_value FROM task7.eval_item WHERE eval_set_id = %s", (sid,))}
    rows = read_csv(directory / "eval_items_draft.csv")
    for r in rows:
        status, value = sysrows[r["item_id"]]
        r["human_action"] = "resolve" if status == "resolved" else "keep_conflict"
        r["human_winning_value"] = value if status == "resolved" else ""
        r["human_evidence"] = "https://example.invalid/evidence" if r["ai_doubt"] == "true" or r["ai_action"] != r["human_action"] else ""
        r["note"] = "人工核对"
    return rows


def write_rows(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=list(ev.DRAFT_COLUMNS))
        wr.writeheader()
        wr.writerows(rows)
    return path


def test_ac15_import_rejects_doubtful_or_changed_items_without_evidence(evaluated, tmp_path):
    conn, s, cv, directory, out, _ = evaluated
    sid = out["eval_set_id"]
    drafts = read_csv(directory / "eval_items_draft.csv")
    doubtful = next(r["item_id"] for r in drafts if r["ai_doubt"] == "true")
    calm = next(r["item_id"] for r in drafts if r["ai_doubt"] == "false" and r["ai_action"] == "resolve")

    rows = labeled_rows(directory, conn, sid)
    by_id = {r["item_id"]: r for r in rows}
    by_id[doubtful]["human_evidence"] = ""
    with pytest.raises(EvalError, match=f"{doubtful}.*human_evidence"):
        ev.import_labels(conn, sid, write_rows(tmp_path / "a.csv", rows), "付林轩")
    assert conn.execute("SELECT count(*) FROM task7.eval_label").fetchone()[0] == 0          # 拒绝时一条都不写

    by_id[doubtful]["human_action"] = "unsure"                              # 标为 unsure 则不需要依据
    by_id[doubtful]["human_winning_value"] = ""
    rows2 = labeled_rows(directory, conn, sid)
    rows2 = [by_id[doubtful] if r["item_id"] == doubtful else r for r in rows2]
    by2 = {r["item_id"]: r for r in rows2}
    by2[calm]["human_action"] = "keep_conflict"                              # 改判（AI 建议 resolve）且无依据
    by2[calm]["human_winning_value"] = ""
    by2[calm]["human_evidence"] = ""
    with pytest.raises(EvalError, match=f"{calm}.*与 AI 建议不同"):
        ev.import_labels(conn, sid, write_rows(tmp_path / "b.csv", rows2), "付林轩")
    by2[calm]["human_evidence"] = "官方资料"
    res = ev.import_labels(conn, sid, write_rows(tmp_path / "c.csv", rows2), "付林轩")
    assert res["imported_items"] == 14 and res["by_action"]["unsure"] == 1 and res["ai_reference"] == "有"
    assert conn.execute("SELECT count(*) FROM task7.eval_label WHERE annotator = '付林轩'").fetchone()[0] == 14


def test_ac15_import_validates_format_and_refuses_copied_ai_reason(evaluated, tmp_path):
    conn, s, cv, directory, out, _ = evaluated
    sid = out["eval_set_id"]
    rows = labeled_rows(directory, conn, sid)
    first = rows[0]
    first["note"] = first["ai_reason"]
    with pytest.raises(EvalError, match="ai_reason"):
        ev.import_labels(conn, sid, write_rows(tmp_path / "n.csv", rows), "付林轩")
    first["note"] = "人工核对"
    first["human_action"] = "maybe"
    with pytest.raises(EvalError, match="human_action"):
        ev.import_labels(conn, sid, write_rows(tmp_path / "o.csv", rows), "付林轩")
    first["human_action"] = "keep_conflict"
    first["human_winning_value"] = "不应填值"
    with pytest.raises(EvalError, match="留空"):
        ev.import_labels(conn, sid, write_rows(tmp_path / "p.csv", rows), "付林轩")
    rows = labeled_rows(directory, conn, sid)
    with pytest.raises(EvalError, match="缺少 1 项"):
        ev.import_labels(conn, sid, write_rows(tmp_path / "q.csv", rows[1:]), "付林轩")
    with pytest.raises(EvalError, match="标注人"):
        ev.import_labels(conn, sid, write_rows(tmp_path / "r.csv", rows), " ")
    res = ev.import_labels(conn, sid, write_rows(tmp_path / "s.csv", rows[1:]), "付林轩", allow_partial=True)
    assert res["imported_items"] == 13 and res["unlabeled_items"] == 1
    with pytest.raises(EvalError, match="已有导入的标注"):
        ev.export_eval_set(conn, cv, tmp_path / "again")
    ev.export_eval_set(conn, cv, tmp_path / "again", force=True)           # --force 才会清除标注
    assert conn.execute("SELECT count(*) FROM task7.eval_label").fetchone()[0] == 0


def test_ac15_metrics_from_database_match_a_hand_count(evaluated, tmp_path):
    conn, s, cv, directory, out, _ = evaluated
    sid = out["eval_set_id"]
    with pytest.raises(EvalError, match="尚未导入"):
        ev.metrics(conn, cv)
    rows = labeled_rows(directory, conn, sid)
    # 手工制造差异：第 1 项改判成 unsure；第 2 项（若系统 resolved）人工改成 keep_conflict；其余与系统一致
    rows[0].update(human_action="unsure", human_winning_value="")
    resolved = [r for r in rows[1:] if r["human_action"] == "resolve"]
    resolved[0].update(human_action="keep_conflict", human_winning_value="", human_evidence="已核实证据互相矛盾")
    ev.import_labels(conn, sid, write_rows(tmp_path / "m.csv", rows), "付林轩")
    m = ev.metrics(conn, cv)
    sysrows = {r[0]: r[1:] for r in conn.execute("SELECT item_id, stratum, target_field, system_status FROM task7.eval_item "
                                                 "WHERE eval_set_id = %s", (sid,))}
    wrong_item = resolved[0]["item_id"]
    decided = [r for r in rows if r["human_action"] != "unsure"]
    ok = [r for r in decided if r["item_id"] != wrong_item]                  # 除改判项外，动作都与系统一致
    assert (m["labeled_total"], m["overall"]["unsure"], m["overall"]["decided"]) == (14, 1, 13)
    assert m["overall"]["action_correct"] == len(ok) == 12 and m["overall"]["action_accuracy"] == round(12 / 13, 4)
    pairs = [r for r in decided if sysrows[r["item_id"]][2] == "resolved" and r["human_action"] == "resolve"]
    assert m["overall"]["value_pairs"] == len(pairs) and m["overall"]["value_correct"] == len(pairs)     # 值与系统一致
    assert m["overall"]["value_accuracy"] == 1.0
    sys_keep = [r for r in decided if sysrows[r["item_id"]][2] == "conflict_unresolved"]
    assert m["overall"]["system_conflict"] == len(sys_keep) and m["overall"]["conflict_precision"] == 1.0
    human_keep = [r for r in decided if r["human_action"] == "keep_conflict"]
    assert m["overall"]["human_conflict"] == len(human_keep)
    assert m["overall"]["conflict_recall"] == round(len(sys_keep) / len(human_keep), 4)       # 多出的一项系统没保留
    assert set(m["by_stratum"]) == {"E1", "E2", "E3", "E4", "E5", "E6", "E7"}
    assert sum(g["labeled"] for g in m["by_stratum"].values()) == 14
    assert set(m["by_field"]) >= {"title", "release_year", "runtime_minutes", "director", "credit_align", "billing",
                                  "genres", "mpaa_rating", "publisher", "product_release_date"}
    assert sum(g["labeled"] for g in m["by_field"].values()) == 14 and m["dev_pool_overlap"] == 0
    assert m["annotators"] == ["付林轩"] and "分层样本" not in m["scope_note"] and "评价样本" in m["scope_note"]
    md = ev.render_metrics(m)
    assert "总体" in md and "按层" in md and "按字段" in md and "%" in md
