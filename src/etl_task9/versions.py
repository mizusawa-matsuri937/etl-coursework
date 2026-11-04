"""血缘版本、阶段键解析与版本差异（spec R34、R36、R40）。

- 血缘版本 lv0001…：记录输入键、R01 各版本与上游阶段键（inputs）、五个阶段键、规则版本与计数；finalize_version
  只写 unverified，verify 通过后 mark_verified 才标 verified（R40）。同一输入键已有未核实的版本时在原版本上重写
  （版本号不变），已核实的不能重写（应当 reused）。大表按阶段键保存，被多个版本共用，物化版本不复制它们。
- 阶段键解析（Keys）：查询与核对都按 (sources, assertions, derivations, selections, impact) 五个短键读；可由血缘版本
  给出，或在还没有版本时（演练、调试）由输入版本直接推出（RunContext.key）。
- 版本差异（R34）：两个血缘版本按（实体类型、实体、字段、成员）对齐 field_selection，列出新增、删除，以及最终值、
  状态、trace_status、决策（规则、理由、置信度）、采用断言（按“上游表:值:上游状态”签名比较，避免任务 7 断言 ID 含版本号带来的假差异）与
  来源集合（field_impact 的源记录，ID 跨版本稳定）的变化。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .rules import STAGES
from .run import RunContext, RunInputs, Task9InputError, check_inputs, require_stage_complete, stage_complete

KEY_COLUMNS = {s: f"{s}_stage_key" for s in STAGES}


@dataclass(frozen=True)
class Keys:
    """一组阶段短键与其输入；lineage_version_id 在由版本解析时给出。"""
    inputs: RunInputs
    sources: str
    assertions: str
    derivations: str
    selections: str
    impact: str
    lineage_version_id: str | None = None

    def as_json(self) -> dict[str, Any]:
        return {"lineage_version_id": self.lineage_version_id, **{s: getattr(self, s) for s in STAGES}}


def inputs_from_json(d: dict[str, Any]) -> RunInputs:
    d = dict(d)
    for k in ("cv_stage_keys", "rd_stage_keys"):
        if d.get(k) is not None:
            d[k] = tuple(d[k])
    names = {f.name for f in dataclasses.fields(RunInputs)}
    return RunInputs(**{k: v for k, v in d.items() if k in names})


def keys_for_inputs(conn: psycopg.Connection, inputs: RunInputs) -> Keys:
    """由输入版本推出五个阶段键（不要求已有血缘版本）。"""
    filled = check_inputs(conn, inputs) if inputs.cv_stage_keys is None else inputs
    ctx = RunContext("", filled)
    return Keys(filled, *(ctx.key(s) for s in STAGES))


def keys_for_version(conn: psycopg.Connection, lineage_version_id: str) -> Keys:
    row = conn.execute(f"SELECT inputs, {', '.join(KEY_COLUMNS.values())} FROM task9.lineage_version "
                       "WHERE lineage_version_id = %s", (lineage_version_id,)).fetchone()
    if row is None:
        raise Task9InputError(f"血缘版本 {lineage_version_id} 不存在")
    return Keys(inputs_from_json(row[0]), *row[1:], lineage_version_id=lineage_version_id)


def resolve(conn: psycopg.Connection, lineage_version_id: str | None = None, inputs: RunInputs | None = None, *,
            need: str = "selections") -> Keys:
    """查询用的阶段键：给出版本号时取该版本；否则取与输入相同的最新 verified 版本；都没有时由输入推出。
    need 阶段（含）之前的阶段都须已完成，否则确定性失败。"""
    if lineage_version_id:
        keys = keys_for_version(conn, lineage_version_id)
    else:
        base = keys_for_inputs(conn, inputs or RunInputs())
        row = None
        if conn.execute("SELECT to_regclass('task9.lineage_version')").fetchone()[0]:
            row = conn.execute("SELECT lineage_version_id FROM task9.lineage_version WHERE extraction_id = %s "
                               "AND input_key = %s AND status = 'verified'",
                               (base.inputs.extraction_id, base.inputs.key())).fetchone()
        keys = dataclasses.replace(base, lineage_version_id=row[0]) if row else base
    for s in STAGES[:STAGES.index(need) + 1]:
        if not stage_complete(conn, getattr(keys, s)):
            raise Task9InputError(f"阶段 {s}（{getattr(keys, s)}）尚未完成，不能查询")
    return keys


# ---------------------------------------------------------------- 血缘版本（R36、R40）

# 物化版本时记录、verify 与 check 时重算的上游行数（只取本版本输入范围内的行，R03）
UPSTREAM_COUNTS = {
    "task2.product_manifest": "SELECT count(*) FROM task2.product_manifest WHERE extraction_id = %(eid)s",
    "task3.product_classification": "SELECT count(*) FROM task3.product_classification WHERE extraction_id = %(eid)s "
                                    "AND rule_version = %(t3)s",
    "task4.product_movie": "SELECT count(*) FROM task4.product_movie WHERE mapping_version_id = %(map)s",
    "task4.field_assertion": "SELECT count(*) FROM task4.field_assertion WHERE extraction_id = %(eid)s",
    "task5.person": "SELECT count(*) FROM task5.person WHERE person_mapping_version_id = %(pmap)s",
    "task5.mention_person": "SELECT count(*) FROM task5.mention_person WHERE person_mapping_version_id = %(pmap)s",
    "task6.release_selection": "SELECT count(*) FROM task6.release_selection WHERE release_version_id = %(rv)s",
    "task7.movie_resolved": "SELECT count(*) FROM task7.movie_resolved WHERE conflict_version_id = %(cv)s",
    "task7.edition_resolved": "SELECT count(*) FROM task7.edition_resolved WHERE conflict_version_id = %(cv)s",
    "task8.dedup_version": "SELECT count(*) FROM task8.dedup_version WHERE review_dedup_version_id = %(rd)s "
                           "AND status = 'verified'",
    "imdb.source_file": "SELECT count(*) FROM imdb.source_file WHERE snapshot_id = %(snap)s",
}


def upstream_counts(conn: psycopg.Connection, inputs) -> dict[str, int]:
    p = {"eid": inputs.extraction_id, "t3": inputs.t3_rule_version, "map": inputs.t4_mapping_version_id,
         "pmap": inputs.t5_person_mapping_version_id, "rv": inputs.t6_release_version_id,
         "cv": inputs.conflict_version_id, "rd": inputs.review_dedup_version_id, "snap": inputs.imdb_snapshot_id}
    return {t: conn.execute(sql, p).fetchone()[0] for t, sql in UPSTREAM_COUNTS.items()}



def next_version_id(conn: psycopg.Connection) -> str:
    n = conn.execute("SELECT count(*) FROM task9.lineage_version").fetchone()[0]
    return f"lv{n + 1:04d}"


def version_counts(conn: psycopg.Connection, keys: dict[str, str]) -> dict[str, Any]:
    """版本计数：各层行数与分类计数、非空值覆盖率（R36、R44）。"""
    def tally(sql: str, key: str) -> dict[str, int]:
        return {str(k): v for k, v in conn.execute(sql, (key,)).fetchall()}

    one = lambda sql, key: conn.execute(sql, (key,)).fetchone()[0]   # noqa: E731
    total, ok = conn.execute("SELECT count(*), count(*) FILTER (WHERE trace_status IN ('traced', 'relocated', 'manual')) "
                             "FROM task9.field_selection WHERE stage_key = %s AND value_text IS NOT NULL",
                             (keys["selections"],)).fetchone()
    return {
        "source_records": tally("SELECT source_kind, count(*) FROM task9.source_record WHERE stage_key = %s "
                                "GROUP BY 1 ORDER BY 1", keys["sources"]),
        "assertions": tally("SELECT level, count(*) FROM task9.assertion WHERE stage_key = %s GROUP BY 1 ORDER BY 1",
                            keys["assertions"]),
        "derivations": tally("SELECT operation, count(*) FROM task9.derivation WHERE stage_key = %s "
                             "GROUP BY 1 ORDER BY 1", keys["derivations"]),
        "select_edges": one("SELECT count(*) FROM task9.derivation WHERE stage_key = %s", keys["selections"]),
        "selections": tally("SELECT status, count(*) FROM task9.field_selection WHERE stage_key = %s "
                            "GROUP BY 1 ORDER BY 1", keys["selections"]),
        "trace_status": tally("SELECT trace_status, count(*) FROM task9.field_selection WHERE stage_key = %s "
                              "GROUP BY 1 ORDER BY 1", keys["selections"]),
        "non_null": total, "non_null_traceable": ok, "coverage": round(ok / total, 4) if total else None,
        "relocations": tally("SELECT target_kind || ':' || consistent, count(*) FROM task9.relocation "
                             "WHERE stage_key = %s GROUP BY 1 ORDER BY 1", keys["derivations"]),
        "field_impact": one("SELECT count(*) FROM task9.field_impact WHERE stage_key = %s", keys["impact"]),
    }


def finalize_version(conn: psycopg.Connection, ctx: RunContext) -> tuple[str, dict[str, Any]]:
    """R36、R40：单事务写血缘版本（unverified）。五个阶段都须完成。返回 (版本号, 计数)。"""
    i = ctx.inputs
    keys = {s: require_stage_complete(conn, ctx, s) for s in STAGES}
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext('etl_task9_versions'))")
        counts = {**version_counts(conn, keys), "upstream_counts": upstream_counts(conn, i)}
        existing = conn.execute("SELECT lineage_version_id, status FROM task9.lineage_version "
                                "WHERE extraction_id = %s AND input_key = %s", (i.extraction_id, i.key())).fetchone()
        if existing is not None and existing[1] == "verified":
            raise Task9InputError(f"血缘版本 {existing[0]} 已通过核对，应当复用而不是重写")
        if existing is not None:
            lv = existing[0]
            conn.execute(f"UPDATE task9.lineage_version SET run_id = %s, inputs = %s, counts = %s, "
                         f"created_at = clock_timestamp(), "
                         f"{', '.join(f'{c} = %s' for c in KEY_COLUMNS.values())} WHERE lineage_version_id = %s",
                         (ctx.run_id, Jsonb(i.as_json()), Jsonb(counts), *keys.values(), lv))
        else:
            lv = next_version_id(conn)
            parent = conn.execute("SELECT lineage_version_id FROM task9.lineage_version WHERE extraction_id = %s "
                                  "ORDER BY created_at DESC, lineage_version_id DESC LIMIT 1",
                                  (i.extraction_id,)).fetchone()
            conn.execute(f"INSERT INTO task9.lineage_version (lineage_version_id, extraction_id, parent_version_id, "
                         f"run_id, input_key, inputs, rule_version, {', '.join(KEY_COLUMNS.values())}, counts) "
                         f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                         (lv, i.extraction_id, parent[0] if parent else None, ctx.run_id, i.key(), Jsonb(i.as_json()),
                          i.rule_version, *keys.values(), Jsonb(counts)))
        conn.execute("UPDATE task9.run SET lineage_version_id = %s WHERE run_id = %s", (lv, ctx.run_id))
    return lv, counts


def mark_verified(conn: psycopg.Connection, lv: str, run_id: str, counts: dict[str, Any]) -> None:
    """R40：verify 通过后单事务把版本标为 verified、运行标为 succeeded。"""
    with conn.transaction():
        conn.execute("UPDATE task9.lineage_version SET status = 'verified', verified_at = clock_timestamp() "
                     "WHERE lineage_version_id = %s", (lv,))
        conn.execute("UPDATE task9.run SET status = 'succeeded', counts = counts || %s, finished_at = clock_timestamp(), "
                     "failure_reason = NULL, lineage_version_id = %s WHERE run_id = %s", (Jsonb(counts), lv, run_id))


# ---------------------------------------------------------------- 版本差异（R34）

_SIDE = """
SELECT s.selection_id, s.entity_type, s.entity_id, s.field_name, s.member_key, s.value_text, s.status, s.trace_status,
       concat_ws(' | ', s.decision_rule, s.reason, s.confidence::text) AS decision,
       (SELECT string_agg(a.upstream_table || ':' || coalesce(a.normalized_value, a.raw_value, '') || ':'
                          || coalesce(a.upstream_status, ''), ' ; ' ORDER BY 1)
        FROM unnest(s.selected_assertion_ids) u(id)
        JOIN task9.assertion a ON a.stage_key = %({p}ak)s AND a.assertion_id = u.id) AS sig,
       i.srcs
FROM task9.field_selection s
LEFT JOIN (SELECT selection_id, array_agg(source_record_id ORDER BY source_record_id) AS srcs
           FROM task9.field_impact WHERE stage_key = %({p}ik)s GROUP BY selection_id) i
       ON i.selection_id = s.selection_id
WHERE s.stage_key = %({p}lk)s"""

DIFF_KINDS = ("added", "removed", "value", "status", "trace_status", "decision", "selected", "sources")


def diff_keys(conn: psycopg.Connection, old: Keys, new: Keys, *, limit: int = 50) -> dict[str, Any]:
    """两组阶段键（两个血缘版本）的选择级差异。返回各类变化的计数、按（实体类型、字段）的分布与前 limit 条明细。"""
    p = {"oak": old.assertions, "oik": old.impact, "olk": old.selections,
         "nak": new.assertions, "nik": new.impact, "nlk": new.selections}
    rows = conn.execute(
        f"""WITH o AS ({_SIDE.format(p='o')}), n AS ({_SIDE.format(p='n')})
            SELECT coalesce(n.entity_type, o.entity_type), coalesce(n.entity_id, o.entity_id),
                   coalesce(n.field_name, o.field_name), coalesce(n.member_key, o.member_key),
                   o.selection_id IS NULL, n.selection_id IS NULL,
                   o.value_text, n.value_text, o.status, n.status, o.trace_status, n.trace_status, o.decision, n.decision,
                   o.sig, n.sig,
                   o.srcs, n.srcs
            FROM o FULL JOIN n ON n.selection_id = o.selection_id
            WHERE o.selection_id IS NULL OR n.selection_id IS NULL OR o.value_text IS DISTINCT FROM n.value_text
               OR o.status IS DISTINCT FROM n.status OR o.trace_status IS DISTINCT FROM n.trace_status
               OR o.decision IS DISTINCT FROM n.decision OR o.sig IS DISTINCT FROM n.sig OR o.srcs IS DISTINCT FROM n.srcs
            ORDER BY 1, 2, 3, 4""", p).fetchall()
    counts = {k: 0 for k in DIFF_KINDS}
    by_field: dict[str, int] = {}
    details = []
    for (et, eid, field, member, added, removed, ov, nv, ost, nst, otr, ntr, odec, ndec, osig, nsig, osrc, nsrc) in rows:
        if added:
            kinds = ["added"]
        elif removed:
            kinds = ["removed"]
        else:
            kinds = [k for k, a, b in (("value", ov, nv), ("status", ost, nst), ("trace_status", otr, ntr),
                                       ("decision", odec, ndec),
                                       ("selected", osig, nsig), ("sources", osrc, nsrc)) if a != b]
        for k in kinds:
            counts[k] += 1
        by_field[f"{et}.{field}"] = by_field.get(f"{et}.{field}", 0) + 1
        if len(details) < limit:
            osrc, nsrc = set(osrc or ()), set(nsrc or ())
            details.append({"entity_type": et, "entity_id": eid, "field_name": field, "member_key": member,
                            "changes": kinds, "old_value": ov, "new_value": nv, "old_status": ost, "new_status": nst,
                            "old_trace_status": otr, "new_trace_status": ntr, "old_decision": odec,
                            "new_decision": ndec, "old_selected": osig,
                            "new_selected": nsig, "sources_removed": sorted(osrc - nsrc),
                            "sources_added": sorted(nsrc - osrc)})
    stages = {s: [getattr(old, s), getattr(new, s)] for s in STAGES if getattr(old, s) != getattr(new, s)}
    return {"old": old.as_json(), "new": new.as_json(), "changed_stage_keys": stages, "changed_selections": len(rows),
            "counts": counts, "by_field": dict(sorted(by_field.items())), "details": details}


def diff_versions(conn: psycopg.Connection, old: str, new: str, *, limit: int = 50) -> dict[str, Any]:
    return diff_keys(conn, keys_for_version(conn, old), keys_for_version(conn, new), limit=limit)
