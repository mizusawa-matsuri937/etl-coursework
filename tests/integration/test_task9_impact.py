"""任务 9 的影响闭包、来源与规则变化定位、版本差异（spec AC15；R32–R35）。
预期值在测试里沿派生边手工追踪或直接比对上游表，不由实现生成。

命令的工作目录为项目根目录：

    docker compose --profile test run --rm etl-test pytest -q tests/integration/test_task9_impact.py
"""

from __future__ import annotations

import gzip
from collections import defaultdict

import pytest

import _task7_helpers as h7
import _task9_helpers as h
from etl_task7.runner import execute_conflict
from etl_task9.impact import rule_impact, verify_sources
from etl_task9.run import RunContext, check_inputs
from etl_task9.versions import diff_versions, finalize_version, keys_for_version, resolve


@pytest.fixture
def t9(conn, make_settings, tmp_path):
    t = h.seed(conn, make_settings, tmp_path)
    out = h.run(conn, t, through="impact", batch_size=500)
    t.run_id = out["run_id"]
    t.keys = resolve(conn, None, t.inputs(), need="impact")
    return t


# ---------------------------------------------------------------- 来源变化（R32、R35）

def test_ac15_verify_sources_lists_changed_snapshot_fields_and_rerun(conn, t9):
    """改动 B0Lg 的快照内容：verify-sources 列出该快照的源记录、经 field_impact 受影响的字段（含 B0Lg 的版本字段与
    Legend 的作品字段），以及按顺序需重跑的任务 3 至 7（再到任务 8、9）；其余快照仍为 ok。"""
    clean = verify_sources(conn, t9.keys, snapshot_root=t9.snapshot_root, kinds=["page_snapshot"], workers=2)
    assert clean["ok"] and clean["mismatched"] == [] and clean["rerun"] == []
    path = t9.snapshot_root / "snapshots" / "B0Lg.html.gz"
    path.write_bytes(gzip.compress(b"tampered body"))
    out = verify_sources(conn, t9.keys, snapshot_root=t9.snapshot_root, kinds=["page_snapshot"], workers=2,
                         limit=1000)
    assert not out["ok"]
    assert [(m["asin"], m["status"]) for m in out["mismatched"]] == [("B0Lg", "snapshot_hash_mismatch")]
    sid = out["mismatched"][0]["source_record_id"]
    expected = {r[0] for r in conn.execute("SELECT selection_id FROM task9.field_impact WHERE stage_key = %s "
                                           "AND source_record_id = %s", (t9.keys.impact, sid))}
    assert out["affected"]["selections"] == len(expected) > 0
    assert {s["selection_id"] for s in out["affected"]["sample"]} == expected
    assert {"edition.edition_title", "product_movie.movie_id"} <= set(out["affected"]["by_field"])
    assert [r["task"] for r in out["rerun"]] == ["task3", "task4", "task5", "task6", "task7", "task8", "task9"]
    assert out["rerun"][0]["dag_id"] == "amazon_products_task3_classify"


def test_ac15_verify_sources_unavailable_files_are_not_mismatches(conn, t9):
    """夹具的 IMDb 文件不在容器里：记为 unavailable，不算不一致，也不给重跑步骤；评论文件（任务 1 装载器在 tmp 写的
    合成 gzip）重算哈希一致。"""
    out = verify_sources(conn, t9.keys, snapshot_root=t9.snapshot_root, imdb_root="/nonexistent",
                         kinds=["imdb_row", "review_record"])
    assert out["ok"] and out["mismatched"] == [] and out["rerun"] == []
    assert {u["source_kind"] for u in out["unavailable"]} == {"imdb_row"}
    assert [c["status"] for c in out["checked"]["review_record"]] == ["ok"]      # 任务 1 装载器写的合成评论文件


# ---------------------------------------------------------------- 规则变化（R33）

def _manual_rule(conn, keys, rule_id=None, rule_version=None):
    children = defaultdict(list)
    start = set()
    for ck, cid, pk, pid, rid, rv in conn.execute(
            "SELECT child_kind, child_id, parent_kind, parent_id, rule_id, rule_version FROM task9.derivation "
            "WHERE stage_key = ANY(%s)", ([keys.derivations, keys.selections],)):
        children[pid].append((ck, cid))
        if (rule_id is None or rid == rule_id) and (rule_version is None or rv == rule_version):
            start.add((ck, cid))
    seen, stack, sels = set(), list(start), set()
    while stack:
        ck, cid = stack.pop()
        if (ck, cid) in seen:
            continue
        seen.add((ck, cid))
        if ck == "selection":
            sels.add(cid)
        else:
            stack.extend(children.get(cid, ()))
    return sels


def test_ac15_impact_rule_lists_only_dependent_fields(conn, t9):
    """impact --rule：只列出经该规则的边能到达的字段选择（与手工追踪一致）；任务 7 的年份规则只影响 release_year，
    规则版本 t6r1 只影响任务 6 的路径（有效 tconst、年份候选与推断上界），不碰版本字段。"""
    rid = conn.execute("SELECT rule_id FROM task9.derivation WHERE stage_key = %s AND operation = 'select' "
                       "AND child_kind = 'selection' AND rule_id LIKE 'year.table:%%' ORDER BY rule_id LIMIT 1",
                       (t9.keys.selections,)).fetchone()[0]
    out = rule_impact(conn, t9.keys, rule_id=rid, limit=10_000)
    assert {s["selection_id"] for s in out["selections"]} == _manual_rule(conn, t9.keys, rule_id=rid)
    assert set(out["by_field"]) == {"movie.release_year"} and out["affected_selections"] > 0
    t6 = rule_impact(conn, t9.keys, rule_version="t6r1", limit=10_000)
    assert {s["selection_id"] for s in t6["selections"]} == _manual_rule(conn, t9.keys, rule_version="t6r1")
    assert not any(k.startswith("edition.") for k in t6["by_field"]) and "movie.inferred_upper_bound" in t6["by_field"]


# ---------------------------------------------------------------- 版本差异（R34）

def _version(conn, t9, run_id, **over):
    inputs = check_inputs(conn, t9.inputs(**over))
    return finalize_version(conn, RunContext(run_id, inputs))[0]


def _second_cv(conn, monkeypatch, t9):
    """第二个上游 cv 版本：任务 7 自己的运行，规则只改作品片长的容差（runtime_tol_variance 5 → 2，测试登记的规则
    版本 t7x9）；它只进入任务 7 scalar 阶段的签名，edition、link、multivalue 阶段键与默认 cv 相同。"""
    import etl_task7.rules as r7
    monkeypatch.setitem(r7.RULESETS, "t7x9", r7.RULESETS["t7r3"].with_version("t7x9", runtime_tol_variance=2))
    cv = execute_conflict(conn, h7.Seeded(t9.eid, t9.world).inputs(rule_version="t7x9"), batch_size=50)
    h._dedup_versions(conn, [(cv.conflict_version_id, "rd0003", "verified")])
    return cv.conflict_version_id


def test_ac15_report_diff_lists_changes_between_lineage_versions(conn, t9, monkeypatch):
    """两个血缘版本（默认 cv 与只改片长容差规则的第二个 cv）：diff 只在 runtime_minutes 上列出变化，值的变化与任务 7
    两个版本的 movie_resolved 逐行比对一致，采用断言按签名比较（任务 7 候选 ID 换了也不算变化）；同一版本自比无差异。"""
    lv1 = _version(conn, t9, t9.run_id)
    cv2 = _second_cv(conn, monkeypatch, t9)
    over = {"conflict_version_id": cv2, "review_dedup_version_id": "rd0003"}
    out2 = h.run(conn, t9, through="impact", batch_size=500, over=over)
    lv2 = _version(conn, t9, out2["run_id"], **over)
    assert (lv1, lv2) == ("lv0001", "lv0002")
    assert diff_versions(conn, lv1, lv1)["changed_selections"] == 0
    d = diff_versions(conn, lv1, lv2, limit=100_000)
    up = {(m, k) for m, k in conn.execute(
        "SELECT a.movie_id, k FROM task7.movie_field_selection a JOIN task7.movie_field_selection b "
        "ON b.conflict_version_id = %s AND b.movie_id = a.movie_id AND b.field_name = a.field_name "
        "JOIN task7.movie_resolved ra ON ra.conflict_version_id = a.conflict_version_id AND ra.movie_id = a.movie_id "
        "JOIN task7.movie_resolved rb ON rb.conflict_version_id = b.conflict_version_id AND rb.movie_id = b.movie_id "
        "CROSS JOIN LATERAL (VALUES ('value', ra.runtime_minutes IS DISTINCT FROM rb.runtime_minutes), "
        "('status', a.status IS DISTINCT FROM b.status), ('decision', (a.selection_rule, a.reason, a.confidence) "
        "IS DISTINCT FROM (b.selection_rule, b.reason, b.confidence))) v(k, changed) "
        "WHERE a.conflict_version_id = %s AND a.field_name = 'runtime_minutes' AND v.changed", (cv2, t9.cv))}
    assert up, "夹具里应有片长相差 3–5 分钟、规则改变后判定不同的作品"
    got = {(x["entity_id"], k) for x in d["details"] if x["field_name"] == "runtime_minutes"
           for k in x["changes"] if k in ("value", "status", "decision")}
    assert got == up
    assert set(d["by_field"]) == {"movie.runtime_minutes"}, d["by_field"]
    assert d["changed_selections"] == len(d["details"]) > 0
    assert keys_for_version(conn, lv2).inputs.conflict_version_id == cv2


def test_ac15_full_rebuild_diff_matches_prior_rule_impact(conn, t9, monkeypatch):
    """方案 C（用户 2026-11-04 确认）：上游规则变化后新血缘版本全量重建（五个阶段键全部换新）；事前在旧版本上
    `impact --rule RT1 --rule-version t7r3`（片长容差只用于 RT1 的差异分类）定位的受影响字段集合，与 diff 报出的
    变化字段集合相同（不多报、不漏报）；变化的选择都在 impact 列出的选择之内，其余选择的值、状态、决策、采用断言
    签名与来源集合全部不变，也没有新增或删除的选择。"""
    lv1 = _version(conn, t9, t9.run_id)
    before = rule_impact(conn, t9.keys, rule_id="RT1", rule_version="t7r3", limit=1_000_000)
    assert set(before["by_field"]) == {"movie.runtime_minutes"} and before["affected_selections"] > 0
    impacted = {(s["entity_type"], s["entity_id"], s["field_name"], s["member_key"]) for s in before["selections"]}
    total = conn.execute("SELECT count(*) FROM task9.field_selection WHERE stage_key = %s",
                         (t9.keys.selections,)).fetchone()[0]
    assert total > len(impacted)                                     # 有足够多未受影响的选择可供比对

    cv2 = _second_cv(conn, monkeypatch, t9)
    over = {"conflict_version_id": cv2, "review_dedup_version_id": "rd0003"}
    out2 = h.run(conn, t9, through="impact", batch_size=500, over=over)
    lv2 = _version(conn, t9, out2["run_id"], **over)
    k1, k2 = keys_for_version(conn, lv1), keys_for_version(conn, lv2)
    assert all(getattr(k1, s) != getattr(k2, s) for s in ("sources", "assertions", "derivations", "selections",
                                                           "impact"))      # 全量重建，不复用旧阶段键
    d = diff_versions(conn, lv1, lv2, limit=1_000_000)
    assert set(d["by_field"]) == set(before["by_field"])             # 字段集合一致
    assert d["counts"]["added"] == d["counts"]["removed"] == 0
    changed = {(x["entity_type"], x["entity_id"], x["field_name"], x["member_key"]) for x in d["details"]}
    assert len(changed) == d["changed_selections"] > 0
    assert changed <= impacted                                       # 变化都在事前定位的范围内
    n2 = conn.execute("SELECT count(*) FROM task9.field_selection WHERE stage_key = %s",
                      (k2.selections,)).fetchone()[0]
    assert n2 == total                                               # 其余 total - |changed| 个选择逐项不变
