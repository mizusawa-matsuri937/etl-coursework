"""任务 9 血缘链抽查评价（spec R46–R49）：export → draft → import → metrics。写法同任务 8 evaluate。

- 抽样（R46）：在一个 verified 血缘版本里按 md5(选择键) 排序分层取前 N：L1 作品标量 15（5 个标量、有效 tconst、
  推断上界）、L2 作品多值成员 15（类型、导演、主演、演员、别名）、L3 版本字段 10（版本标量、特征成员、商品类别）、
  L4 人物 10（规范名、外部 ID、别名）、L5 评论与商品归属 10（商品所属作品选择与 rd 版本下归属作品的原始评论，两类
  各按 md5 排序后交替排列，保证两类都被抽到）。只抽非空最终值。某层不足时按 L2 → L1 → L3 的顺序从这些层的后续项补足，总数保持 60。
- 审核表 eval_items.csv（R47）：实体、字段、最终值、处理路径（每步操作、rule_id、上游表与键）与原始证据摘录：页面为
  快照路径、正文哈希与该定位的原文（≤ 300 字符）；IMDb 为文件、行号与原行（从原始 gz 文件按行号读出；文件不在
  容器内时标明“文件不可读”并给出装载表的列值）；评论为文件定位、记录哈希与正文前 80 个字符。标注列 label、
  error_type、human_evidence、note。eval_item 只存路径与证据摘录的 SHA-256。
- AI 草稿 eval_items_draft.csv（R49）：只读审核表与原始证据文件（页面快照解压后检索原文、IMDb gz 按行号读行），不连
  数据库、不读 task9 的状态列；逐项写 ai_read（读过哪些文件与核对结果）、ai_label、ai_doubt、ai_reason。
- 导入（R49）：label ∈ correct/incorrect/unsure；incorrect 必须选 error_type；与 AI 建议不同或 AI 存疑的项须填
  human_evidence（外部依据），标为 unsure 的除外；note 不得原样粘贴 ai_reason。校验全部通过才写入。
- 指标（R48）：血缘链正确率 = correct /（correct + incorrect），总体与分层；unsure 单列；分母为 0 记“无定义”。
  只代表抽查样本。
"""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import html
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import psycopg

from etl_task1 import reporting

from .query import Tracer, evidence
from .rules import resolve_snapshot
from .versions import Keys, keys_for_version

QUOTA = {"L1": 15, "L2": 15, "L3": 10, "L4": 10, "L5": 10}
FILL_ORDER = ("L2", "L1", "L3")
LABELS = ("correct", "incorrect", "unsure")
ERROR_TYPES = ("wrong_source", "wrong_value", "missing_step", "wrong_reason")
PAGE_LIMIT, REVIEW_LIMIT = 300, 80
STRATUM_SQL = {
    "L1": "entity_type = 'movie' AND field_name IN ('title', 'original_title', 'release_year', 'runtime_minutes', "
          "'mpaa_rating', 'effective_tconst', 'inferred_upper_bound')",
    "L2": "entity_type = 'movie' AND field_name IN ('genres', 'director', 'starring', 'cast', 'alias')",
    "L3": "entity_type = 'edition'",
    "L4": "entity_type = 'person'",
}
ITEM_COLUMNS = ("item_id", "stratum", "entity_type", "entity_id", "field_name", "member_key", "final_value", "status",
                "path", "evidence")
REVIEW_COLUMNS = (*ITEM_COLUMNS, "label", "error_type", "human_evidence", "note")
DRAFT_COLUMNS = (*ITEM_COLUMNS, "ai_read", "ai_label", "ai_doubt", "ai_reason", "label", "error_type",
                 "human_evidence", "note")
LABELING_GUIDE = """# 任务 9 血缘链抽查标注口径

每行是一条最终字段（或成员、或一条评论与商品的归属）的血缘链。请对照 `evidence` 列给出的原始证据（必要时打开快照文件或
IMDb 原始文件），判断 `path` 描述的处理路径是否能从这些原始证据得出 `final_value`：

- `correct`：证据里确有该值（或经路径中的规范化、对齐、选择规则得到该值），来源与理由成立。
- `incorrect`：须在 `error_type` 选一个：`wrong_source`（证据不是该值的来源）、`wrong_value`（证据得不出该值）、
  `missing_step`（路径缺少必要的处理步骤）、`wrong_reason`（选择理由与证据不符）。
- `unsure`：证据不足以判断。

与 AI 草稿（`ai_label`）不同、或 AI 存疑（`ai_doubt = true`）的项，必须在 `human_evidence` 填外部依据（如核对的页面
位置、IMDb 原行、评论原文），否则导入会被拒绝；标为 `unsure` 的项除外。`note` 不要原样粘贴 `ai_reason`。
"""


class EvalError(RuntimeError):
    """评价输入不合格（确定性错误）。"""


def eval_set_id(lv: str) -> str:
    return f"ev_{lv}"


def eval_dir(output_base: Path, eid: str, lv: str) -> Path:
    return Path(output_base) / "task9" / eid / lv / "eval"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 抽样（R46）

def build_pools(conn: psycopg.Connection, keys: Keys, per: int = 60) -> dict[str, list[dict[str, Any]]]:
    """每层按 md5(选择键) 排序的候选（多取一些用于补足）。"""
    pools: dict[str, list[dict[str, Any]]] = {}
    for s, cond in STRATUM_SQL.items():
        pools[s] = [{"kind": "selection", "key": r[0]} for r in conn.execute(
            f"SELECT selection_id FROM task9.field_selection WHERE stage_key = %s AND value_text IS NOT NULL AND {cond} "
            f"ORDER BY md5(selection_id) LIMIT %s", (keys.selections, per))]
    i = keys.inputs
    prod = [(r[0], {"kind": "selection", "key": r[0]}) for r in conn.execute(
        "SELECT selection_id FROM task9.field_selection WHERE stage_key = %s AND entity_type = 'product_movie' "
        "ORDER BY md5(selection_id) LIMIT %s", (keys.selections, per))]
    rev = [(f"review:{r[0]}", {"kind": "review", "key": f"review:{r[0]}", "movie_id": r[1]}) for r in conn.execute(
        "SELECT rr.source_review_id, w.movie_id FROM task8.logical_review_work w "
        "JOIN task8.review_mapping m ON m.dedup_stage_key = %s AND m.logical_review_id = w.logical_review_id "
        "AND m.mapping_status = 'mapped' JOIN public.review_record rr ON rr.extraction_id = %s "
        "AND rr.record_no = m.record_no WHERE w.attribution_stage_key = %s "
        "ORDER BY md5('review:' || rr.source_review_id) LIMIT %s",
        (i.rd_stage_keys[2], i.extraction_id, i.rd_stage_keys[3], per))]
    pools["L5"] = _interleave([x for _k, x in prod], [x for _k, x in rev])
    return pools


def _interleave(a: list[Any], b: list[Any]) -> list[Any]:
    """两个各自按 md5 排好的候选交替排列（一方用尽后接另一方剩余），保证 L5 同时抽到商品归属与评论归属。"""
    out = []
    for n in range(max(len(a), len(b))):
        out += [x[n] for x in (a, b) if n < len(x)]
    return out


def sample_items(pools: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按配额取前 N；不足的缺额按 L2 → L1 → L3 从这些层的剩余候选补足。"""
    taken = {s: pools.get(s, [])[:q] for s, q in QUOTA.items()}
    short = sum(q - len(taken[s]) for s, q in QUOTA.items())
    filled = {s: 0 for s in QUOTA}
    for s in FILL_ORDER:
        while short > 0 and len(taken[s]) + 0 < len(pools.get(s, [])):
            taken[s].append(pools[s][len(taken[s])])
            filled[s] += 1
            short -= 1
    items, n = [], 0
    for s in QUOTA:
        for x in taken[s]:
            n += 1
            items.append({**x, "stratum": s, "item_id": f"L{n:03d}"})
    strata = {s: {"target": QUOTA[s], "actual": len(taken[s]), "filled": filled[s]} for s in QUOTA}
    return items, strata


# ---------------------------------------------------------------- 审核表（R47）

def _path_text(tree: dict[str, Any], depth: int = 0) -> list[str]:
    out = []
    for e in tree.get("edges", ()):
        p = e["parent"]
        node = p.get("assertion") or p.get("source_record") or {}
        label = (f"{node.get('upstream_table')}:{node.get('upstream_key')}" if "assertion" in p
                 else f"source_record {node.get('source_kind')} {node.get('upstream_table')}:{node.get('upstream_key')}")
        out.append(f"{'  ' * depth}{e['operation']}[{e['rule_id']}|{e['rule_version']}|{e['upstream_task']}] ← {label}")
        out += _path_text(p, depth + 1)
    if tree.get("more_parents"):
        out.append(f"{'  ' * depth}…共 {tree['more_parents']['total']} 个父节点，按操作 {tree['more_parents']['by_operation']}")
    return out


def _page_excerpts(conn: psycopg.Connection, keys: Keys, tree: dict[str, Any]) -> dict[str, str]:
    """路径里页面源记录的直接子断言（抽取断言）原文：source_record_id → 原文（≤ 300 字符）。"""
    out: dict[str, str] = {}

    def walk(t: dict[str, Any]) -> None:
        a = t.get("assertion") or {}
        for e in t.get("edges", ()):
            sr = e["parent"].get("source_record")
            if sr and sr.get("source_kind") == "page_snapshot" and a.get("raw_value"):
                out.setdefault(sr["source_record_id"], a["raw_value"][:PAGE_LIMIT])
            walk(e["parent"])
    walk(tree)
    return out


def _evidence_rows(conn: psycopg.Connection, keys: Keys, tree: dict[str, Any]) -> list[dict[str, Any]]:
    pages = _page_excerpts(conn, keys, tree)
    rows = []
    for e in evidence(tree)[:6]:
        loc = e.get("locator") or {}
        if e["source_kind"] == "page_snapshot":
            rows.append({"kind": "page", "asin": loc.get("asin"), "snapshot_path": loc.get("snapshot_path"),
                         "body_sha256": e.get("content_hash"), "text": pages.get(e["source_record_id"], "")})
        elif e["source_kind"] == "imdb_row":
            rows.append({"kind": "imdb", "file": loc.get("file"), "snapshot": loc.get("snapshot"),
                         "line_no": loc.get("line_no"), "file_sha256": e.get("content_hash"),
                         "table": loc.get("table")})
        elif e["source_kind"] == "review_record":
            rows.append({"kind": "review", "source_review_id": loc.get("source_review_id"),
                         "record_no": loc.get("record_no"), "start_line": loc.get("start_line"),
                         "raw_sha256": loc.get("raw_sha256")})
        else:
            rows.append({"kind": e["source_kind"], "locator": loc})
    return rows


def _imdb_lines(root: str | Path | None, wanted: dict[tuple[str, str], set[int]]) -> dict[tuple[str, str, int], str]:
    """从原始 gz 文件按行号读出原行（行号从 1 起，含表头；每个文件只顺序读一遍到最大行号）。文件不可读时跳过。"""
    from etl_task4.imdb import snapshot_dir
    out: dict[tuple[str, str, int], str] = {}
    for (snap, name), lines in wanted.items():
        path = snapshot_dir(snap, root) / name
        if not path.exists():
            continue
        last = max(lines)
        with gzip.open(path, "rt", encoding="utf-8", newline="") as fh:
            for n, line in enumerate(fh, 1):
                if n in lines:
                    out[(snap, name, n)] = line.rstrip("\n")
                if n >= last:
                    break
    return out


def _imdb_db_row(conn: psycopg.Connection, table: str, snap: str, line_no: int) -> str:
    if table not in ("imdb.title", "imdb.title_aka", "imdb.title_director", "imdb.title_principal", "imdb.name"):
        return ""
    r = conn.execute(f"SELECT row_to_json(x)::text FROM (SELECT * FROM {table} WHERE snapshot_id = %s "
                     f"AND line_no = %s LIMIT 1) x", (snap, line_no)).fetchone()
    return r[0] if r else ""


def item_rows(conn: psycopg.Connection, keys: Keys, items: list[dict[str, Any]], *,
              imdb_root: str | Path | None = None) -> list[dict[str, Any]]:
    t = Tracer(conn, keys, max_parents=3)
    i = keys.inputs
    rows: list[dict[str, Any]] = []
    for x in items:
        if x["kind"] == "selection":
            s = conn.execute("SELECT entity_type, entity_id, field_name, member_key, value_text, status "
                             "FROM task9.field_selection WHERE stage_key = %s AND selection_id = %s",
                             (keys.selections, x["key"])).fetchone()
            tree = t.tree("selection", x["key"])
            row = dict(zip(("entity_type", "entity_id", "field_name", "member_key", "final_value", "status"), s))
            row["path"] = "\n".join(_path_text(tree))
            ev = _evidence_rows(conn, keys, tree)
        else:
            sid = x["key"].split(":", 1)[1]
            v = conn.execute("SELECT v.record_no, v.start_line, v.raw_sha256, v.asin, v.logical_review_id, v.match_level, "
                             "v.family_id, v.movie_id, left(rs.review_text_raw, %s) FROM task9.v_review_lineage v "
                             "LEFT JOIN public.review_staging rs ON rs.extraction_id = v.extraction_id "
                             "AND rs.source_review_id = v.source_review_id WHERE v.extraction_id = %s "
                             "AND v.source_review_id = %s AND v.review_dedup_version_id = %s AND v.movie_id = %s",
                             (REVIEW_LIMIT, i.extraction_id, sid, i.review_dedup_version_id, x["movie_id"])).fetchone()
            row = {"entity_type": "review", "entity_id": sid, "field_name": "movie_id", "member_key": "",
                   "final_value": v[7], "status": v[5],
                   "path": (f"review_record #{v[0]} → task8.review_mapping[{i.rd_stage_keys[2]}] {v[5]} → "
                            f"logical_review {v[4]}（家族 {v[6]}）→ task8.logical_review_work[{i.rd_stage_keys[3]}] "
                            f"→ movie {v[7]}（商品 {v[3]}）")}
            ev = [{"kind": "review", "source_review_id": sid, "record_no": v[0], "start_line": v[1],
                   "raw_sha256": v[2], "asin": v[3], "text_80": v[8] or ""}]
        row.update({"item_id": x["item_id"], "stratum": x["stratum"], "_evidence": ev, "_key": x["key"]})
        rows.append(row)
    wanted: dict[tuple[str, str], set[int]] = {}
    for r in rows:
        for e in r["_evidence"]:
            if e["kind"] == "imdb" and e.get("line_no"):
                wanted.setdefault((e["snapshot"], e["file"]), set()).add(int(e["line_no"]))
    lines = _imdb_lines(imdb_root, wanted)
    for r in rows:
        for e in r["_evidence"]:
            if e["kind"] == "imdb":
                raw = lines.get((e["snapshot"], e["file"], int(e["line_no"] or 0)))
                e["line"] = raw if raw is not None else ""
                if raw is None:
                    e["file_unavailable"] = True
                    e["loaded_row"] = _imdb_db_row(conn, e.get("table") or "", e["snapshot"], int(e["line_no"] or 0))
        r["evidence"] = json.dumps(r.pop("_evidence"), ensure_ascii=False)
    return rows


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    reporting.write_atomic(path, buf.getvalue().encode("utf-8"))


# ---------------------------------------------------------------- AI 草稿（R49）：只读审核表与原始证据文件

def _norm(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip().lower()


def _snapshot_body(path: Path) -> str | None:
    try:
        with gzip.open(path, "rb") as fh:
            return fh.read().decode("utf-8", errors="replace")
    except (FileNotFoundError, OSError, EOFError):
        return None


def generate_ai_draft(row: dict[str, Any], *, snapshot_root: str | Path) -> dict[str, str]:
    """读原始证据（快照正文、IMDb 原行、评论正文摘录），核对最终值与证据原文是否对得上；不连数据库。"""
    ev = json.loads(row["evidence"] or "[]")
    value = _norm(row["final_value"])
    reads, hits, problems = [], 0, []
    for e in ev:
        if e["kind"] == "page":
            path = resolve_snapshot(e.get("snapshot_path") or "", snapshot_root)
            body = _snapshot_body(path)
            if body is None:
                reads.append(f"快照 {path} 不可读")
                problems.append("快照不可读")
                continue
            ok_hash = hashlib.sha256(body.encode("utf-8")).hexdigest() == e.get("body_sha256")
            text = _norm(e.get("text"))
            found = bool(text) and text[:60] in _norm(body)
            reads.append(f"快照 {e.get('asin')}：正文哈希{'一致' if ok_hash else '不一致'}，定位原文"
                         f"{'在正文中找到' if found else '未在正文中找到'}")
            if not ok_hash:
                problems.append(f"{e.get('asin')} 快照正文与登记哈希不一致")
            if not found:
                problems.append(f"{e.get('asin')} 定位原文未找到")
            hits += bool(value and (value in _norm(e.get("text")) or value in _norm(body)))
        elif e["kind"] == "imdb":
            line = e.get("line") or ""
            if not line:
                reads.append(f"IMDb {e.get('file')} 第 {e.get('line_no')} 行：原始文件不可读，只有装载表的列值")
                hits += bool(value and value in _norm(e.get("loaded_row")))
                problems.append("IMDb 原始文件不可读")
                continue
            reads.append(f"IMDb {e.get('file')} 第 {e.get('line_no')} 行：{line[:120]}")
            hits += bool(value and value in _norm(line.replace("\t", " ")))
        elif e["kind"] == "review":
            reads.append(f"评论记录 {e.get('record_no')}（行 {e.get('start_line')}）：{(e.get('text_80') or '')[:80]}")
            hits += 1 if row["entity_type"] == "review" else 0
    if not ev:
        return {"ai_read": "没有原始证据", "ai_label": "unsure", "ai_doubt": "true", "ai_reason": "路径没有落到原始证据"}
    if problems:
        return {"ai_read": "；".join(reads), "ai_label": "unsure", "ai_doubt": "true",
                "ai_reason": "；".join(problems) + "，需要人工打开原始文件核对"}
    if hits:
        return {"ai_read": "；".join(reads), "ai_label": "correct", "ai_doubt": "false",
                "ai_reason": f"最终值在 {hits} 处原始证据里出现，路径的来源成立"}
    return {"ai_read": "；".join(reads), "ai_label": "unsure", "ai_doubt": "true",
            "ai_reason": "最终值没有原样出现在证据里（可能经规范化、对齐或受控词映射），需要人工核对路径中的规则"}


def write_draft(review_csv: Path, draft_csv: Path, *, snapshot_root: str | Path) -> int:
    """只读审核表文件与原始证据文件生成草稿（没有数据库连接）。"""
    with open(review_csv, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = [{**r, **generate_ai_draft(r, snapshot_root=snapshot_root)} for r in rows]
    _write_csv(draft_csv, DRAFT_COLUMNS, out)
    return len(out)


# ---------------------------------------------------------------- 导出、导入、指标

def export_eval_set(conn: psycopg.Connection, lv: str, directory: Path, *, imdb_root: str | Path | None = None,
                    force: bool = False) -> dict[str, Any]:
    """抽样，写 eval_set、eval_item，导出 eval_items.csv 与 LABELING.md。已导入标注时拒绝重导（force 清除后重导）。"""
    row = conn.execute("SELECT status, extraction_id, rule_version FROM task9.lineage_version "
                       "WHERE lineage_version_id = %s", (lv,)).fetchone()
    if row is None or row[0] != "verified":
        raise EvalError(f"血缘版本 {lv} 不存在或尚未通过核对，不能导出评价表")
    sid = eval_set_id(lv)
    labeled = conn.execute("SELECT count(*) FROM task9.eval_label WHERE eval_set_id = %s", (sid,)).fetchone()[0]
    if labeled and not force:
        raise EvalError(f"评价集 {sid} 已导入 {labeled} 条标注；确需重导请加 --force（会清除标注）")
    keys = keys_for_version(conn, lv)
    items, strata = sample_items(build_pools(conn, keys))
    rows = item_rows(conn, keys, items, imdb_root=imdb_root)
    directory.mkdir(parents=True, exist_ok=True)
    _write_csv(directory / "eval_items.csv", REVIEW_COLUMNS, rows)
    reporting.write_atomic(directory / "LABELING.md", LABELING_GUIDE.encode("utf-8"))
    sha = lambda s: hashlib.sha256((s or "").encode("utf-8")).hexdigest()   # noqa: E731
    with conn.transaction():
        conn.execute("DELETE FROM task9.eval_label WHERE eval_set_id = %s", (sid,))
        conn.execute("DELETE FROM task9.eval_item WHERE eval_set_id = %s", (sid,))
        conn.execute("DELETE FROM task9.eval_set WHERE eval_set_id = %s", (sid,))
        conn.execute("INSERT INTO task9.eval_set (eval_set_id, extraction_id, lineage_version_id, rule_version, strata, "
                     "exported_file) VALUES (%s, %s, %s, %s, %s, %s)",
                     (sid, row[1], lv, row[2], json.dumps(strata), str(directory / "eval_items.csv")))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO task9.eval_item (eval_set_id, item_id, stratum, selection_key, entity_type, "
                            "entity_id, field_name, member_key, path_sha256, evidence_sha256) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            [(sid, r["item_id"], r["stratum"], r["_key"], r["entity_type"], r["entity_id"],
                              r["field_name"], r["member_key"] or "", sha(r["path"]), sha(r["evidence"])) for r in rows])
    return {"eval_set_id": sid, "items": len(rows), "strata": strata, "directory": str(directory)}


def _truthy(text: str | None) -> bool:
    return (text or "").strip().lower() in ("true", "1", "yes", "y")


def import_labels(conn: psycopg.Connection, sid: str, csv_path: Path, annotator: str, *, force: bool = False,
                  allow_partial: bool = False) -> dict[str, Any]:
    """校验全部通过才写入 eval_label（R47、R49）。"""
    items = {r[0] for r in conn.execute("SELECT item_id FROM task9.eval_item WHERE eval_set_id = %s", (sid,))}
    if not items:
        raise EvalError(f"评价集 {sid} 没有评价项；先运行 export")
    existing = conn.execute("SELECT count(*) FROM task9.eval_label WHERE eval_set_id = %s", (sid,)).fetchone()[0]
    if existing and not force:
        raise EvalError(f"评价集 {sid} 已有 {existing} 条标注；确需重导请加 --force")
    raw = csv_path.read_bytes()
    rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    errors, seen, out = [], set(), []
    for r in rows:
        iid, label = (r.get("item_id") or "").strip(), (r.get("label") or "").strip()
        etype, evid = (r.get("error_type") or "").strip(), (r.get("human_evidence") or "").strip()
        note = (r.get("note") or "").strip()
        if iid not in items:
            errors.append(f"{iid or '（空）'}：不在评价集 {sid} 中")
            continue
        if iid in seen:
            errors.append(f"{iid}：重复")
            continue
        seen.add(iid)
        if label not in LABELS:
            errors.append(f"{iid}：label 必须是 {LABELS} 之一，实际 {label!r}")
            continue
        if (label == "incorrect") != bool(etype) or (etype and etype not in ERROR_TYPES):
            errors.append(f"{iid}：incorrect 必须且只有 incorrect 填 error_type（{ERROR_TYPES}），实际 {etype!r}")
            continue
        doubt = _truthy(r.get("ai_doubt"))
        changed = (r.get("ai_label") or "").strip() != label
        if label != "unsure" and (doubt or changed) and not evid:
            errors.append(f"{iid}：{'AI 存疑' if doubt else '与 AI 建议不同'}的项须填 human_evidence（外部依据）")
            continue
        if note and note == (r.get("ai_reason") or "").strip():
            errors.append(f"{iid}：note 不得原样粘贴 ai_reason")
            continue
        out.append((sid, iid, label, etype or None, evid or None, doubt, note or None, annotator))
    if not allow_partial and len(seen) != len(items):
        errors.append(f"标注 {len(seen)} 项，评价集有 {len(items)} 项（部分导入请加 --allow-partial）")
    if errors:
        raise EvalError("导入被拒绝：\n" + "\n".join(errors[:50]))
    digest = hashlib.sha256(raw).hexdigest()
    with conn.transaction():
        conn.execute("DELETE FROM task9.eval_label WHERE eval_set_id = %s", (sid,))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO task9.eval_label (eval_set_id, item_id, label, error_type, evidence, ai_doubt, "
                            "note, annotator, import_file_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            [(*o, digest) for o in out])
    return {"eval_set_id": sid, "imported": len(out), "annotator": annotator, "file_sha256": digest}


def _ratio(n: int, d: int) -> float | str:
    return round(n / d, 4) if d else "无定义"


def metrics(conn: psycopg.Connection, sid: str) -> dict[str, Any]:
    """R48：血缘链正确率（总体与分层），unsure 单列不进分母，分母为 0 记“无定义”。"""
    rows = conn.execute("SELECT i.stratum, l.label, l.error_type, l.annotator FROM task9.eval_item i "
                        "JOIN task9.eval_label l USING (eval_set_id, item_id) WHERE i.eval_set_id = %s", (sid,)).fetchall()
    by = {s: {k: 0 for k in LABELS} for s in QUOTA}
    errors: dict[str, int] = {}
    for s, label, etype, _ in rows:
        by[s][label] += 1
        if etype:
            errors[etype] = errors.get(etype, 0) + 1

    def rate(strata) -> dict[str, Any]:
        c = sum(by[s]["correct"] for s in strata)
        d = c + sum(by[s]["incorrect"] for s in strata)
        return {"correct": c, "denominator": d, "unsure": sum(by[s]["unsure"] for s in strata), "value": _ratio(c, d)}

    return {"eval_set_id": sid, "labeled_total": len(rows), "annotators": sorted({r[3] for r in rows}),
            "overall": rate(tuple(QUOTA)), "by_stratum": {s: rate((s,)) for s in QUOTA}, "error_types": errors,
            "note": "指标只代表 60 条分层抽查样本，不代表全量正确率；标注为 AI 预填、人工逐项审核。"}


def render_metrics(m: dict[str, Any]) -> str:
    o = m["overall"]
    lines = [f"# 任务 9 血缘链抽查指标（{m['eval_set_id']}）", "",
             f"- 已标注 {m['labeled_total']} 项，标注人：{'、'.join(m['annotators'])}",
             f"- 总体正确率：{o['value']}（{o['correct']}/{o['denominator']}，unsure {o['unsure']}）",
             f"- 错误类型：{json.dumps(m['error_types'], ensure_ascii=False)}", "",
             "| 层 | 正确率 | correct | 分母 | unsure |", "|---|---|---|---|---|"]
    lines += [f"| {s} | {v['value']} | {v['correct']} | {v['denominator']} | {v['unsure']} |"
              for s, v in m["by_stratum"].items()]
    return "\n".join(lines + ["", m["note"], ""])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m etl_task9.evaluate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ex = sub.add_parser("export", help="抽样并导出审核表（R46、R47）")
    ex.add_argument("--version", required=True)
    ex.add_argument("--force", action="store_true")
    dr = sub.add_parser("draft", help="只读审核表与原始证据文件生成 AI 草稿（R49）")
    dr.add_argument("--version", required=True)
    dr.add_argument("--snapshot-root", default=None)
    im = sub.add_parser("import", help="校验并导入标注（R49）")
    im.add_argument("--version", required=True)
    im.add_argument("--file", required=True)
    im.add_argument("--annotator", required=True)
    im.add_argument("--force", action="store_true")
    me = sub.add_parser("metrics", help="血缘链正确率（R48）")
    me.add_argument("--version", required=True)
    args = ap.parse_args(argv)
    from etl_task1.config import load_settings
    from etl_task1.db import connect
    s = load_settings(os.environ)
    if args.cmd == "draft":
        from .source_records import default_snapshot_root
        with connect(s) as conn:
            eid = conn.execute("SELECT extraction_id FROM task9.lineage_version WHERE lineage_version_id = %s",
                               (args.version,)).fetchone()[0]
        d = eval_dir(s.output_dir, eid, args.version)
        n = write_draft(d / "eval_items.csv", d / "eval_items_draft.csv",
                        snapshot_root=args.snapshot_root or default_snapshot_root())
        out: Any = {"draft_rows": n, "file": str(d / "eval_items_draft.csv")}
    else:
        with connect(s) as conn:
            if args.cmd == "export":
                eid = conn.execute("SELECT extraction_id FROM task9.lineage_version WHERE lineage_version_id = %s",
                                   (args.version,)).fetchone()[0]
                out = export_eval_set(conn, args.version, eval_dir(s.output_dir, eid, args.version), force=args.force)
            elif args.cmd == "import":
                out = import_labels(conn, eval_set_id(args.version), Path(args.file), args.annotator, force=args.force)
            else:
                out = metrics(conn, eval_set_id(args.version))
                eid = conn.execute("SELECT extraction_id FROM task9.lineage_version WHERE lineage_version_id = %s",
                                   (args.version,)).fetchone()[0]
                reporting.write_atomic(eval_dir(s.output_dir, eid, args.version) / "metrics.md",
                                       render_metrics(out).encode("utf-8"))
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
