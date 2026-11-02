"""候选与选择记录的公共部分（spec R07–R12）：候选对象、选择记录的构造与写入。

候选在内存里决策，决策后一次写入 task7.field_candidate（状态已定）；候选 ID 在批内预先从序列取号，
这样选择记录可以直接引用它们。选择记录以 dict 形式保存在阶段结果的 field_selections 里，finalize_version 物化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

import psycopg
from psycopg.types.json import Jsonb

from .rules import CANDIDATE_STATUSES, CONFLICT_TYPES, SELECTION_STATUSES
from .run import RunContext, copy_rows

CAND_COLUMNS = ("candidate_id", "extraction_id", "stage_key", "entity_type", "entity_id", "field_name", "source",
                "source_key", "raw_value", "normalized_value", "precision", "source_ref", "source_acquired_at",
                "match_reliability", "status", "status_reason", "rule_version", "run_id")


class IdAllocator:
    """批内的候选 ID 分配：成块从序列取号，用不完的号留空（序列本来就允许有间隙）。"""

    def __init__(self, conn: psycopg.Connection, block: int = 500):
        self.conn, self.block, self._ids = conn, block, []

    def take(self) -> int:
        if not self._ids:
            rows = self.conn.execute("SELECT nextval('task7.candidate_id_seq') FROM generate_series(1, %s)",
                                     (self.block,)).fetchall()
            self._ids = [r[0] for r in rows][::-1]
        return self._ids.pop()


@dataclass
class Cand:
    cid: int
    entity_type: str                       # movie | edition
    entity_id: str
    field_name: str
    source: str
    source_key: str
    raw_value: str | None
    normalized_value: str | None
    precision: str
    source_ref: dict[str, Any]
    reliability: float
    acquired_at: datetime | None = None
    status: str = "conflict_pending"
    reason: str | None = None

    def mark(self, status: str, reason: str | None = None) -> None:
        assert status in CANDIDATE_STATUSES, status
        self.status, self.reason = status, reason


def new_cand(alloc: IdAllocator, entity_type: str, entity_id: str, field_name: str, source: str, source_key: str,
             raw: str | None, normalized: str | None, precision: str, source_ref: dict[str, Any],
             reliability: float, acquired_at: datetime | None = None) -> Cand:
    return Cand(alloc.take(), entity_type, entity_id, field_name, source, source_key, raw, normalized, precision,
                source_ref, round(float(reliability), 2), acquired_at)


def selection(status: str, value: Any, selected: Iterable[Cand], considered: Iterable[Cand], conflict_type: str,
              rule: str, reason: str, confidence: float, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    """一条选择记录（R11）：状态互斥为 resolved、conflict_unresolved、missing（R12）。
    status = missing 当且仅当没有候选；存在候选但无法采用时为 conflict_unresolved，胜出值为 NULL。"""
    assert status in SELECTION_STATUSES, status
    assert conflict_type in CONFLICT_TYPES, conflict_type
    sel_ids = [c.cid for c in selected]
    con_ids = [c.cid for c in considered]
    assert (status == "resolved") == (value is not None), (status, value)
    assert status == "resolved" or not sel_ids
    assert (status == "missing") == (not con_ids), (status, con_ids)
    if status == "resolved":
        assert sel_ids and set(sel_ids) <= set(con_ids)
    return {"status": status, "winning_value": None if value is None else str(value),
            "selected_candidate_ids": sel_ids, "considered_candidate_ids": con_ids,
            "conflict_type": conflict_type, "selection_rule": rule, "reason": reason,
            "confidence": round(float(confidence), 2) if status == "resolved" else 0.0,
            "disagreement_detail": detail or {}}


def settle(cands: Iterable[Cand], adopted: Iterable[Cand] = (), superseded: Iterable[Cand] = (),
           rejected: Iterable[Cand] = (), reason: str | None = None) -> None:
    """按决策结果给候选定状态：胜出值相同者 adopted，别名或更泛化写法 superseded_by_specific，被排除的 rejected；
    其余保持 conflict_pending。reason 只写在 superseded 与 rejected 的候选上（说明为什么没有被采用）。"""
    for c in adopted:
        c.mark("adopted")
    for c in superseded:
        c.mark("superseded_by_specific", reason)
    for c in rejected:
        c.mark("rejected", reason)


def write_candidates(conn: psycopg.Connection, ctx: RunContext, stage_key: str, cands: list[Cand]) -> int:
    rows = [(c.cid, ctx.inputs.extraction_id, stage_key, c.entity_type, c.entity_id, c.field_name, c.source,
             c.source_key, c.raw_value, c.normalized_value, c.precision, Jsonb(c.source_ref), c.acquired_at,
             c.reliability, c.status, c.reason, ctx.inputs.rule_version, ctx.run_id) for c in cands]
    copy_rows(conn, "task7.field_candidate", CAND_COLUMNS, rows)
    return len(rows)
