"""任务 9 的只读查询（spec R28–R31、R33）：field、product、review、source、person、impact 六个子命令，输出 JSON。

所有查询都限定一组阶段键（血缘版本，或还没有版本时由输入版本推出，见 versions.resolve），只读 task9 与上游表。
处理路径用 trace_up 从选择或断言沿父边向上展开到源记录：每个节点给出断言或源记录摘要，每条边给出操作、rule_id、
规则版本、上游任务、上游运行与时间、角色与是否重放；父节点很多时（如推断上界的全部依赖评论）只展开前
max_parents 个并给出总数。evidence 是展开到的源记录清单（定位、哈希、获取时间与路径长度）。

命令（项目根目录）：

    docker compose --profile crawl run --rm -T crawler python -m etl_task9.query field --movie-id <movie_id> --field release_year
    docker compose --profile crawl run --rm -T crawler python -m etl_task9.query product --asin <ASIN>
    docker compose --profile crawl run --rm -T crawler python -m etl_task9.query review --source-review-id <ID>
    docker compose --profile crawl run --rm -T crawler python -m etl_task9.query source --tconst <tconst>
    docker compose --profile crawl run --rm -T crawler python -m etl_task9.query person --person-id <person_id>
    docker compose --profile crawl run --rm -T crawler python -m etl_task9.query impact --rule <rule_id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import psycopg

from .impact import RERUN, rerun_plan, rule_impact
from .rules import assertion_id, selection_id
from .run import RunInputs, Task9InputError
from .versions import Keys, resolve

SEL_COLS = ("selection_id", "entity_type", "entity_id", "field_name", "member_key", "value_text", "status",
            "missing_reason", "selected_assertion_ids", "considered_assertion_ids", "decision_rule", "reason",
            "confidence", "decision_version", "trace_status", "target_ref")
AS_COLS = ("assertion_id", "level", "entity_type", "entity_id", "field_name", "member_key", "raw_value",
           "normalized_value", "source_record_id", "field_locator", "extract_rule_version", "rule_id",
           "upstream_status", "upstream_reason", "upstream_table", "upstream_key", "upstream_run_id", "confidence")
SR_COLS = ("source_record_id", "source_kind", "locator", "content_hash", "hash_scope", "acquired_at",
           "acquired_at_basis", "upstream_table", "upstream_key", "evidence_status")
EDGE_COLS = ("parent_kind", "parent_id", "operation", "ol_type", "ol_subtype", "rule_id", "rule_version",
             "upstream_task", "upstream_run_id", "occurred_at", "confidence", "role", "relocated", "detail")


def _rows(conn: psycopg.Connection, sql: str, params: Any, cols: tuple[str, ...]) -> list[dict[str, Any]]:
    return [dict(zip(cols, r)) for r in conn.execute(sql, params).fetchall()]


class Tracer:
    """沿父边向上展开处理路径（带缓存，只读）。"""

    def __init__(self, conn: psycopg.Connection, keys: Keys, *, max_parents: int = 5, max_depth: int = 12):
        self.conn, self.keys = conn, keys
        self.max_parents, self.max_depth = max_parents, max_depth
        self._edges: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def edges(self, kind: str, cid: str) -> list[dict[str, Any]]:
        k = (kind, cid)
        if k not in self._edges:
            self._edges[k] = _rows(self.conn, f"SELECT {', '.join(EDGE_COLS)} FROM task9.derivation "
                                              "WHERE stage_key = ANY(%s) AND child_kind = %s AND child_id = %s "
                                              "ORDER BY role NULLS LAST, operation, parent_id",
                                   ([self.keys.derivations, self.keys.selections], kind, cid), EDGE_COLS)
        return self._edges[k]

    def assertion(self, aid: str) -> dict[str, Any] | None:
        r = _rows(self.conn, f"SELECT {', '.join(AS_COLS)} FROM task9.assertion WHERE stage_key = %s AND assertion_id = %s",
                  (self.keys.assertions, aid), AS_COLS)
        return r[0] if r else None

    def source(self, sid: str) -> dict[str, Any] | None:
        r = _rows(self.conn, f"SELECT {', '.join(SR_COLS)} FROM task9.source_record WHERE stage_key = %s "
                             "AND source_record_id = %s", (self.keys.sources, sid), SR_COLS)
        return r[0] if r else None

    def tree(self, kind: str, nid: str, depth: int = 0) -> dict[str, Any]:
        """kind ∈ assertion、source_record、selection。"""
        if kind == "source_record":
            return {"source_record": self.source(nid) or {"source_record_id": nid, "missing": True}}
        node: dict[str, Any] = {"assertion": self.assertion(nid)} if kind == "assertion" else {"selection_id": nid}
        if kind == "assertion" and node["assertion"] is None:
            return {"assertion": {"assertion_id": nid, "missing": True}}
        if depth >= self.max_depth:
            node["truncated"] = True
            return node
        es = self.edges(kind, nid)
        shown = es[:self.max_parents] if len(es) > self.max_parents else es
        # 直接依赖（role）优先展示：推断上界的最早与第二早评论
        if len(es) > self.max_parents:
            shown = [e for e in es if e["role"] == "direct_dependency"]
            shown += [e for e in es if e["role"] != "direct_dependency"][:max(0, self.max_parents - len(shown))]
        node["edges"] = [{**{k: e[k] for k in EDGE_COLS if k not in ("parent_kind", "parent_id")},
                          "parent": self.tree(e["parent_kind"], e["parent_id"], depth + 1)} for e in shown]
        if len(es) > len(shown):
            node["more_parents"] = {"total": len(es), "shown": len(shown),
                                    "by_operation": _count(e["operation"] for e in es)}
        return node


def _count(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for x in items:
        out[x] = out.get(x, 0) + 1
    return out


def evidence(tree: dict[str, Any], depth: int = 0, path: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """树里展开到的源记录（定位、哈希、获取时间、路径长度与经过的操作），按出现顺序去重。"""
    out: list[dict[str, Any]] = []
    if "source_record" in tree:
        sr = tree["source_record"]
        return [{"source_record_id": sr.get("source_record_id"), "source_kind": sr.get("source_kind"),
                 "locator": sr.get("locator"), "content_hash": sr.get("content_hash"),
                 "hash_scope": sr.get("hash_scope"), "acquired_at": sr.get("acquired_at"),
                 "acquired_at_basis": sr.get("acquired_at_basis"), "evidence_status": sr.get("evidence_status"),
                 "depth": depth, "operations": list(path)}]
    for e in tree.get("edges", ()):
        out += evidence(e["parent"], depth + 1, path + (f"{e['operation']}:{e['rule_id']}",))
    seen, uniq = set(), []
    for x in out:
        if x["source_record_id"] not in seen:
            seen.add(x["source_record_id"])
            uniq.append(x)
    return uniq


def _selection(conn: psycopg.Connection, keys: Keys, where: str, params: tuple) -> list[dict[str, Any]]:
    return _rows(conn, f"SELECT {', '.join(SEL_COLS)} FROM task9.field_selection WHERE stage_key = %s AND {where} "
                       "ORDER BY field_name, member_key", (keys.selections, *params), SEL_COLS)


def _explain(t: Tracer, sel: dict[str, Any], *, considered: bool = True) -> dict[str, Any]:
    tree = t.tree("selection", sel["selection_id"])
    sel = dict(sel)
    sel["path"] = tree
    sel["evidence"] = evidence(tree)
    if considered:
        others = [a for a in sel["considered_assertion_ids"] if a not in set(sel["selected_assertion_ids"])]
        sel["not_selected"] = []
        for a in others:
            ct = t.tree("assertion", a)
            sel["not_selected"].append({"candidate": ct.get("assertion"), "path": ct, "evidence": evidence(ct)})
    return sel


# ---------------------------------------------------------------- field（R28）

def query_field(conn: psycopg.Connection, keys: Keys, movie_id: str, field: str, *, max_parents: int = 5,
                member_limit: int = 50) -> dict[str, Any]:
    t = Tracer(conn, keys, max_parents=max_parents)
    sels = _selection(conn, keys, "entity_type = 'movie' AND entity_id = %s AND field_name = %s", (movie_id, field))
    if not sels:
        raise Task9InputError(f"作品 {movie_id} 在该血缘版本中没有字段 {field} 的选择")
    out: dict[str, Any] = {"query": "field", "movie_id": movie_id, "field": field, "keys": keys.as_json(),
                           "members": len(sels), "selections": [_explain(t, s) for s in sels[:member_limit]]}
    if field == "release_year":   # 推断上界是独立的参考（作业示例：上映年份缺失只有条件性上界）
        b = _selection(conn, keys, "entity_type = 'movie' AND entity_id = %s AND field_name = 'inferred_upper_bound'",
                       (movie_id,))
        if b:
            bt = Tracer(conn, keys, max_parents=3)
            out["related_inferred_upper_bound"] = _explain(bt, b[0], considered=False)
    return out


# ---------------------------------------------------------------- product 与 review（R29）

PRODUCT_COLS = ("collection_status", "status_reason", "fetched_at", "snapshot_path", "content_hash", "category",
                "category_rule_id", "category_basis", "input_group", "product_kind", "match_scope", "match_status",
                "match_reason", "member_ordinal", "movie_id", "relation_type", "movie_basis", "decision_id",
                "human_decision_id", "edition_title", "runtime_minutes", "mpaa_rating", "publisher",
                "product_release_date", "discs", "media_format", "language", "subtitles", "aspect_ratio", "region",
                "cut_type", "video_system")


def query_product(conn: psycopg.Connection, keys: Keys, asin: str) -> dict[str, Any]:
    i = keys.inputs
    m = conn.execute("SELECT status, status_reason, snapshot_path FROM task2.product_manifest "
                     "WHERE extraction_id = %s AND asin = %s", (i.extraction_id, asin)).fetchone()
    out: dict[str, Any] = {"query": "product", "asin": asin, "keys": keys.as_json()}
    if m is None:
        return {**out, "collection": "not_in_manifest"}
    rows = _rows(conn, f"SELECT {', '.join(PRODUCT_COLS)} FROM task9.v_product_lineage WHERE extraction_id = %s "
                       "AND asin = %s AND (mapping_version_id = %s OR mapping_version_id IS NULL) "
                       "AND (conflict_version_id = %s OR conflict_version_id IS NULL) ORDER BY member_ordinal",
                 (i.extraction_id, asin, i.t4_mapping_version_id, i.conflict_version_id), PRODUCT_COLS)
    head = rows[0] if rows else {}
    collected = m[2] is not None
    out.update({"collection": "collected" if collected else "not_collected", "collection_status": m[0],
                "status_reason": m[1]})
    out["category"] = {k: head.get(k) for k in ("category", "category_rule_id", "category_basis", "input_group")}
    out["edition"] = {k: head.get(k) for k in PRODUCT_COLS[19:]} if head.get("product_kind") else None
    out["product_kind"], out["match_scope"] = head.get("product_kind"), head.get("match_scope")
    out["match_status"], out["match_reason"] = head.get("match_status"), head.get("match_reason")
    out["movies"] = [{k: r[k] for k in ("member_ordinal", "movie_id", "relation_type", "movie_basis", "decision_id",
                                        "human_decision_id")} for r in rows if r.get("movie_id")]
    sr = conn.execute(f"SELECT {', '.join(SR_COLS)} FROM task9.source_record WHERE stage_key = %s "
                      "AND upstream_table = 'task2.product_manifest' AND upstream_key = %s",
                      (keys.sources, f"{i.extraction_id}|{asin}")).fetchone()
    out["source_record"] = dict(zip(SR_COLS, sr)) if sr else None
    out["selections"] = [{k: s[k] for k in ("entity_type", "field_name", "member_key", "value_text", "status",
                                            "missing_reason", "trace_status", "decision_version")}
                         for s in _selection(conn, keys, "entity_type IN ('edition', 'product_movie') AND entity_id = %s",
                                             (asin,))]
    return out


REVIEW_COLS = ("record_no", "start_line", "end_line", "start_offset", "end_offset", "raw_sha256", "classification",
               "product_id", "review_time_utc", "review_dedup_version_id", "dedup_version_status", "mapping_status",
               "match_level", "asin", "logical_review_id", "family_id", "family_basis", "representative_record_no",
               "copy_count", "movie_id", "also_on_collection", "stat_group_id")


def query_review(conn: psycopg.Connection, keys: Keys, source_review_id: str) -> dict[str, Any]:
    i = keys.inputs
    rows = _rows(conn, f"SELECT {', '.join(REVIEW_COLS)} FROM task9.v_review_lineage WHERE extraction_id = %s "
                       "AND source_review_id = %s AND review_dedup_version_id = %s ORDER BY movie_id NULLS LAST",
                 (i.extraction_id, source_review_id, i.review_dedup_version_id), REVIEW_COLS)
    if not rows:
        raise Task9InputError(f"抽取 {i.extraction_id} 中没有源评论 {source_review_id}")
    head = rows[0]
    out: dict[str, Any] = {"query": "review", "source_review_id": source_review_id, "keys": keys.as_json(),
                           "record": {k: head[k] for k in REVIEW_COLS[:9]},
                           "mapping": {k: head[k] for k in REVIEW_COLS[9:19]},
                           "movies": [{k: r[k] for k in ("movie_id", "also_on_collection", "stat_group_id")}
                                      for r in rows if r["movie_id"]]}
    aid = assertion_id("public.review_staging", f"{i.extraction_id}|{source_review_id}")
    deps = conn.execute(
        "SELECT d.child_id, d.operation, d.role, d.relocated, a.entity_id, a.normalized_value FROM task9.derivation d "
        "JOIN task9.assertion a ON a.stage_key = %s AND a.assertion_id = d.child_id "
        "WHERE d.stage_key = %s AND d.parent_kind = 'assertion' AND d.parent_id = %s ORDER BY 1",
        (keys.assertions, keys.derivations, aid)).fetchall()
    out["bound_dependencies"] = [{"bound_assertion_id": c, "operation": op, "role": role, "relocated": rel,
                                  "movie_id": mv, "bound": val} for c, op, role, rel, mv, val in deps]
    sr = conn.execute(f"SELECT {', '.join(SR_COLS)} FROM task9.source_record WHERE stage_key = %s "
                      "AND upstream_table = 'public.review_record' AND upstream_key = %s",
                      (keys.sources, f"{i.extraction_id}|{source_review_id}")).fetchone()
    out["source_record"] = dict(zip(SR_COLS, sr)) if sr else None
    return out


# ---------------------------------------------------------------- source（R30）

def source_ids(conn: psycopg.Connection, keys: Keys, *, source_record_id: str | None = None, asin: str | None = None,
               tconst: str | None = None, nconst: str | None = None,
               source_review_id: str | None = None) -> list[tuple[str, str, Any]]:
    """把输入解析为源记录 (ID, 种类, 定位)。tconst 取该作品的 title、crew、principals、akas 行；nconst 取人名行与
    该人的演职员行。"""
    sk, eid = keys.sources, keys.inputs.extraction_id
    base = "SELECT source_record_id, source_kind, locator FROM task9.source_record WHERE stage_key = %s AND "
    if source_record_id:
        return conn.execute(base + "source_record_id = %s", (sk, source_record_id)).fetchall()
    if asin:
        return conn.execute(base + "upstream_table = 'task2.product_manifest' AND upstream_key = %s",
                            (sk, f"{eid}|{asin}")).fetchall()
    if source_review_id:
        return conn.execute(base + "upstream_table = 'public.review_record' AND upstream_key = %s",
                            (sk, f"{eid}|{source_review_id}")).fetchall()
    if tconst:
        return conn.execute(base + "source_kind = 'imdb_row' AND locator->>'tconst' = %s ORDER BY upstream_table, "
                                   "source_record_id", (sk, tconst)).fetchall()
    if nconst:
        return conn.execute(base + "source_kind = 'imdb_row' AND (locator->>'nconst' = %s "
                                   "OR locator->'nconsts' ? %s) ORDER BY upstream_table, source_record_id",
                            (sk, nconst, nconst)).fetchall()
    raise ValueError("需要源记录 ID、ASIN、tconst、nconst 或源评论 ID 之一")


def query_source(conn: psycopg.Connection, keys: Keys, *, limit: int = 200, **ref: Any) -> dict[str, Any]:
    srcs = source_ids(conn, keys, **ref)
    ids = [s[0] for s in srcs]
    rows = conn.execute(
        "SELECT s.selection_id, s.entity_type, s.entity_id, s.field_name, s.member_key, s.value_text, s.status, "
        "s.trace_status, min(i.depth), array_agg(i.source_record_id ORDER BY i.source_record_id) "
        "FROM task9.field_impact i JOIN task9.field_selection s ON s.stage_key = %s AND s.selection_id = i.selection_id "
        "WHERE i.stage_key = %s AND i.source_record_id = ANY(%s) "
        "GROUP BY 1, 2, 3, 4, 5, 6, 7, 8 ORDER BY s.entity_type, s.entity_id, s.field_name, s.member_key",
        (keys.selections, keys.impact, ids)).fetchall()
    cols = ("selection_id", "entity_type", "entity_id", "field_name", "member_key", "value", "status", "trace_status",
            "depth", "via_source_record_ids")
    by_field = _count(f"{r[1]}.{r[3]}" for r in rows)
    entities = sorted({(r[1], r[2]) for r in rows})
    return {"query": "source", "input": {k: v for k, v in ref.items() if v}, "keys": keys.as_json(),
            "source_records": [{"source_record_id": a, "source_kind": b, "locator": c} for a, b, c in srcs],
            "affected_selections": len(rows), "affected_entities": len(entities), "by_field": by_field,
            "rerun_if_changed": rerun_plan(sorted({s[1] for s in srcs} & set(RERUN))),
            "selections": [dict(zip(cols, r)) for r in rows[:limit]]}


# ---------------------------------------------------------------- person（R31）

def query_person(conn: psycopg.Connection, keys: Keys, person_id: str, *, limit: int = 100) -> dict[str, Any]:
    i = keys.inputs
    t = Tracer(conn, keys, max_parents=8)
    sels = _selection(conn, keys, "entity_type = 'person' AND entity_id = %s", (person_id,))
    if not sels:
        raise Task9InputError(f"人物 {person_id} 在该血缘版本中没有选择")
    out: dict[str, Any] = {"query": "person", "person_id": person_id, "keys": keys.as_json()}
    canon = [s for s in sels if s["field_name"] == "canonical_name"]
    out["canonical_name"] = _explain(t, canon[0], considered=False) if canon else None
    out["external_ids"] = [_explain(t, s, considered=False) for s in sels if s["field_name"] == "external_id"]
    out["aliases"] = [{k: s[k] for k in ("member_key", "decision_rule", "reason", "trace_status")}
                      | {"mention_assertion_ids": s["selected_assertion_ids"]}
                      for s in sels if s["field_name"] == "alias"]
    pmap = i.t5_person_mapping_version_id
    mentions = conn.execute("SELECT mention_key, basis, round, group_key, attribution FROM task5.mention_person "
                            "WHERE person_mapping_version_id = %s AND person_id = %s ORDER BY mention_key",
                            (pmap, person_id)).fetchall()
    credits = []
    for key, basis, rnd, group, attribution in mentions[:limit]:
        mp = t.tree("assertion", assertion_id("task5.mention_person", f"{pmap}|{key}"))
        mention = None
        for e in mp.get("edges", ()):
            if e["operation"] == "merge":
                mention = e["parent"]
        credits.append({"mention_key": key, "basis": basis, "round": rnd, "group_key": group,
                        "attribution": attribution,
                        "raw_value": ((mention or {}).get("assertion") or {}).get("raw_value"),
                        "page_locator": ((mention or {}).get("assertion") or {}).get("field_locator"),
                        "merge_edges": [{k: e[k] for k in ("operation", "rule_id", "rule_version", "detail")}
                                        for e in mp.get("edges", ())],
                        "evidence": evidence(mp)})
    out["credits"] = credits
    out["mention_count"] = len(mentions)
    out["mapping_events"] = [dict(zip(("event", "mention_key", "from_person_id", "to_person_id", "reason",
                                       "decision_ref"), r)) for r in conn.execute(
        "SELECT event, mention_key, from_person_id, to_person_id, reason, decision_ref FROM task5.mapping_event "
        "WHERE person_mapping_version_id = %s AND (to_person_id = %s OR from_person_id = %s) ORDER BY event_id",
        (pmap, person_id, person_id)).fetchall()]
    _edition, _link, _scalar, multi = i.cv_stage_keys
    works = conn.execute(
        "SELECT movie_id, role, status, source_coverage, credit_order FROM task7.movie_person_resolved "
        "WHERE extraction_id = %s AND stage_key = %s AND person_id = %s ORDER BY movie_id, role",
        (i.extraction_id, multi, person_id)).fetchall()
    sids = [selection_id("movie", m, role, person_id) for m, role, *_ in works]
    trace = dict(conn.execute("SELECT selection_id, trace_status FROM task9.field_selection WHERE stage_key = %s "
                              "AND selection_id = ANY(%s)", (keys.selections, sids)).fetchall())
    out["works"] = [{"movie_id": m, "role": role, "status": st, "source_coverage": cov, "credit_order": order,
                     "selection_id": sid, "trace_status": trace.get(sid)}
                    for (m, role, st, cov, order), sid in zip(works, sids)]
    out["movie_count"] = len({w[0] for w in works})
    return out


# ---------------------------------------------------------------- 命令行

def _connect() -> psycopg.Connection:
    from etl_task1.config import load_settings
    from etl_task1.db import connect
    return connect(load_settings(os.environ))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m etl_task9.query")
    ap.add_argument("--lineage-version", default=None, help="血缘版本（默认：与默认输入相同的 verified 版本，"
                                                            "没有时由输入版本推出阶段键）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("field", help="作品字段：最终值、候选、原始证据与理由（R28）")
    f.add_argument("--movie-id", required=True)
    f.add_argument("--field", required=True)
    f.add_argument("--max-parents", type=int, default=5)
    p = sub.add_parser("product", help="商品：采集状态、类别、版本属性、作品与匹配状态（R29）")
    p.add_argument("--asin", required=True)
    r = sub.add_parser("review", help="原始评论：文件定位、逻辑评论、去重层次、家族与作品（R29）")
    r.add_argument("--source-review-id", required=True)
    s = sub.add_parser("source", help="源记录影响的全部最终字段与路径长度（R30）")
    g = s.add_mutually_exclusive_group(required=True)
    for name in ("source-record-id", "asin", "tconst", "nconst", "source-review-id"):
        g.add_argument("--" + name)
    s.add_argument("--limit", type=int, default=200)
    pe = sub.add_parser("person", help="人物：规范名来源、原始署名、外部 ID、归并过程与参与作品（R31）")
    pe.add_argument("--person-id", required=True)
    im = sub.add_parser("impact", help="规则影响：依赖某条规则或规则版本的字段（R33）")
    im.add_argument("--rule", default=None)
    im.add_argument("--rule-version", default=None)
    im.add_argument("--limit", type=int, default=50)
    args = ap.parse_args(argv)
    with _connect() as conn:
        need = "impact" if args.cmd == "source" else "selections"
        keys = resolve(conn, args.lineage_version, RunInputs(), need=need)
        if args.cmd == "field":
            out = query_field(conn, keys, args.movie_id, args.field, max_parents=args.max_parents)
        elif args.cmd == "product":
            out = query_product(conn, keys, args.asin)
        elif args.cmd == "review":
            out = query_review(conn, keys, args.source_review_id)
        elif args.cmd == "source":
            out = query_source(conn, keys, limit=args.limit, source_record_id=args.source_record_id, asin=args.asin,
                               tconst=args.tconst, nconst=args.nconst, source_review_id=args.source_review_id)
        elif args.cmd == "person":
            out = query_person(conn, keys, args.person_id)
        else:
            out = rule_impact(conn, keys, rule_id=args.rule, rule_version=args.rule_version, limit=args.limit)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
