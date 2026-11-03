"""任务 8 评价工具（spec R45–R49，AC13、AC17）：开发池、六层 120 项盲测表、AI 预标注草稿、导入校验与指标。

    python -m etl_task8.evaluate freeze                       # 首次全量运行前冻结开发池（md5(reviewer_id) 前 20% 用户）
    python -m etl_task8.evaluate export [--version rd0001]    # 抽样、写 eval_set/eval_item，导出盲测表、草稿与标注说明
    python -m etl_task8.evaluate draft <eval_items.csv> <eval_items_draft.csv>   # 只读盲测表重新生成 AI 草稿
    python -m etl_task8.evaluate import <labeled.csv> --annotator 付林轩 [--version rd0001] [--allow-partial] [--force]
    python -m etl_task8.evaluate metrics [--version rd0001]

分层（R46）：V1 同 ASIN 变体（D2）15、V2 家族复制（D3，其中 weak_support 至少 10）25、V3 跨家族同内容（C1）20、
V4 近似评论（C2）20、V5 统计组内作品对 20、V6 未成候选的同用户同 ASIN 不同评论 20；抽样排除开发池用户，层内按
md5(item_key) 排序取前 N 项；某层不足时按 V2 → V3 → V4 补足到 120。系统层次只存在 task8.eval_item（算指标用），
盲测表只有两侧原始字段，编号按 md5(item_key) 全局打乱（R47）。AI 草稿只读盲测表（generate_ai_draft 没有数据库参数），
不读 task8 结论表；导入时存疑（ai_doubt）或与 AI 建议不同的项必须有外部依据，标为 unsure 的除外（R49）。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import psycopg

from etl_task1 import reporting
from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction

from .check import latest_version, version_row
from .report import output_root
from .rules import word_jaccard, word_set
from .run import apply_task8_schema

DEV_FRACTION = 5                       # 前 1/5 = 20% 用户属于开发池（R45）
TARGET_TOTAL = 120
QUOTA = {"V1": 15, "V2": 25, "V3": 20, "V4": 20, "V5": 20, "V6": 20}
WEAK_FLOOR = 10                        # V2 中 weak_support 至少 10 项
FILL_ORDER = ("V2", "V3", "V4")        # R46：不足时的补足顺序
LABELS = ("same", "different", "unsure")
SYSTEM_DECISION = {"V1": "merged", "V2": "merged", "V3": "separate", "V4": "candidate", "V5": "grouped",
                   "V6": "separate"}
TEXT_LIMIT = 500
SIDE_FIELDS = ("reviewer_id", "review_date", "score", "summary", "text_500", "asin", "product_title", "product_category")
BLIND_COLUMNS = ("item_id", "question", *(f"left_{f}" for f in SIDE_FIELDS), *(f"right_{f}" for f in SIDE_FIELDS))
DRAFT_COLUMNS = (*BLIND_COLUMNS, "ai_label", "ai_doubt", "ai_reason", "human_label", "human_evidence", "note")
QUESTION = {"review": "两侧是否同一条用户评论（same / different / unsure）",
            "movie": "两侧是否同一部作品（same / different / unsure）"}

LABELING_GUIDE = """# 任务 8 评论去重评价标注口径说明

每行一对：`question` 为 review 类时判断“两侧是否同一个用户写的同一条评论”（平台在不同商品版本上的复制、同一商品上的重复提交
都算 same；同一用户对不同商品或不同作品写的不同评论、或者内容相近但是另写的一条，算 different）；为 movie 类时判断
“两侧是否同一部作品”（同一部片的不同发行版本、不同译名算 same）。拿不准就填 unsure。

- `ai_label`、`ai_doubt`、`ai_reason` 是 AI 只读本表生成的草稿，只是线索，不是事实。
- 在 `human_label` 填 same / different / unsure；`ai_doubt` 为 true 的项，或你的结论与 `ai_label` 不同的项，必须在
  `human_evidence` 写可核实的外部依据（例如商品页、IMDb 链接或你核对的具体字段），否则导入拒绝；标为 unsure 的项除外。
- `note` 可写备注，不要原样粘贴 `ai_reason`。
- 本表不含系统的判定、层次或候选类型；指标只代表分层样本。
"""


class EvalError(RuntimeError):
    """评价输入不合格（确定性错误）。"""


def eval_set_id(rd: str) -> str:
    return f"ev_{rd}"


def eval_dir(output_base: Path, eid: str, rd: str) -> Path:
    return output_root(output_base) / eid / rd / "eval"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 开发池（R45）

def freeze_dev_pool(conn: psycopg.Connection, extraction_id: str) -> dict[str, Any]:
    """按 md5(reviewer_id) 排序取前 20% 用户冻结到 task8.dev_pool；已冻结时返回已有结果。须在首次全量运行前执行。"""
    apply_task8_schema(conn)
    total = conn.execute("SELECT count(DISTINCT reviewer_id) FROM public.review_staging WHERE extraction_id = %s "
                         "AND reviewer_id IS NOT NULL", (extraction_id,)).fetchone()[0]
    have = conn.execute("SELECT count(*), min(frozen_at) FROM task8.dev_pool WHERE extraction_id = %s",
                        (extraction_id,)).fetchone()
    first_version = conn.execute("SELECT min(created_at) FROM task8.dedup_version WHERE extraction_id = %s",
                                 (extraction_id,)).fetchone()[0]
    if have[0]:
        return {"status": "already_frozen", "dev_reviewers": have[0], "total_reviewers": total,
                "frozen_before_first_run": first_version is None or have[1] < first_version}
    with conn.transaction():
        n = conn.execute(
            "INSERT INTO task8.dev_pool (extraction_id, reviewer_id) SELECT %s, reviewer_id FROM ("
            "SELECT reviewer_id, row_number() OVER (ORDER BY md5(reviewer_id), reviewer_id COLLATE \"C\") rn FROM ("
            "SELECT DISTINCT reviewer_id FROM public.review_staging WHERE extraction_id = %s AND reviewer_id IS NOT NULL"
            ") u) x WHERE rn * %s <= %s", (extraction_id, extraction_id, DEV_FRACTION, total)).rowcount
    return {"status": "frozen", "dev_reviewers": n, "total_reviewers": total,
            "frozen_before_first_run": first_version is None}


# ---------------------------------------------------------------- 抽样（R46）

def build_pools(conn: psycopg.Connection, rd: str, per_stratum: int = TARGET_TOTAL) -> dict[str, list[dict[str, Any]]]:
    """各层候选项（已排除开发池用户），层内按 md5(item_key) 排序，每层最多 per_stratum 项。
    每项 {stratum, item_key, left_ref, right_ref, side（record | logical | movie）, weak}。"""
    row = version_row(conn, rd)
    eid = row["extraction_id"]
    fk, dk, ak = row["family_stage_key"], row["dedup_stage_key"], row["attribution_stage_key"]
    if not conn.execute("SELECT EXISTS (SELECT 1 FROM task8.dev_pool WHERE extraction_id = %s)", (eid,)).fetchone()[0]:
        raise EvalError(f"抽取 {eid} 还没有冻结开发池；先运行 python -m etl_task8.evaluate freeze")
    p = {"eid": eid, "fk": fk, "dk": dk, "ak": ak, "n": per_stratum}
    not_dev = "NOT EXISTS (SELECT 1 FROM task8.dev_pool d WHERE d.extraction_id = %(eid)s AND d.reviewer_id = {col})"

    def items(stratum: str, sql: str, side: str) -> list[dict[str, Any]]:
        return [{"stratum": stratum, "item_key": k, "left_ref": str(a), "right_ref": str(b), "side": side,
                 "weak": bool(w)} for k, a, b, w in conn.execute(sql, p)]

    pools = {
        # V1：D2 记录与同一逻辑评论在同一 ASIN 上的首条
        "V1": items("V1", f"""
            SELECT 'V1:' || f.first_rn || ':' || m.record_no AS k, f.first_rn, m.record_no, false
            FROM task8.review_mapping m
            JOIN task8.logical_review l ON l.dedup_stage_key = m.dedup_stage_key AND l.logical_review_id = m.logical_review_id
            JOIN LATERAL (SELECT min(x.record_no) first_rn FROM task8.review_mapping x WHERE x.dedup_stage_key = m.dedup_stage_key
                          AND x.logical_review_id = m.logical_review_id AND x.asin = m.asin) f ON true
            WHERE m.dedup_stage_key = %(dk)s AND m.match_level = 'D2_same_asin_variant' AND {not_dev.format(col='l.reviewer_id')}
            ORDER BY md5('V1:' || f.first_rn || ':' || m.record_no) LIMIT %(n)s""", "record"),
        # V2：D3 记录与代表记录；weak 表示所在家族为多 ASIN 的 weak_support。两类各取前 n 项（只取一类会被 weak 占满）
        "V2": items("V2", f"""
            SELECT * FROM (SELECT * FROM (
                SELECT 'V2:' || l.representative_record_no || ':' || m.record_no AS k, l.representative_record_no,
                       m.record_no, r.weak_support AND r.asin_count > 1 AS weak
                FROM task8.review_mapping m
                JOIN task8.logical_review l ON l.dedup_stage_key = m.dedup_stage_key
                     AND l.logical_review_id = m.logical_review_id
                JOIN task8.review_family r ON r.family_stage_key = %(fk)s AND r.family_id = l.family_id
                WHERE m.dedup_stage_key = %(dk)s AND m.match_level = 'D3_family_copy' AND r.weak_support
                  AND r.asin_count > 1 AND {not_dev.format(col='l.reviewer_id')}) w
                ORDER BY md5(k) LIMIT %(n)s) a
            UNION ALL
            SELECT * FROM (SELECT * FROM (
                SELECT 'V2:' || l.representative_record_no || ':' || m.record_no AS k, l.representative_record_no,
                       m.record_no, false AS weak
                FROM task8.review_mapping m
                JOIN task8.logical_review l ON l.dedup_stage_key = m.dedup_stage_key
                     AND l.logical_review_id = m.logical_review_id
                JOIN task8.review_family r ON r.family_stage_key = %(fk)s AND r.family_id = l.family_id
                WHERE m.dedup_stage_key = %(dk)s AND m.match_level = 'D3_family_copy'
                  AND NOT (r.weak_support AND r.asin_count > 1) AND {not_dev.format(col='l.reviewer_id')}) s
                ORDER BY md5(k) LIMIT %(n)s) b""", "record"),
        # V3：跨家族同内容（C1）的前两条逻辑评论
        "V3": items("V3", f"""
            SELECT 'V3:' || c.candidate_id, c.logical_review_ids[1], c.logical_review_ids[2], false
            FROM task8.dup_candidate c WHERE c.stage_key = %(dk)s AND c.candidate_type = 'cross_family_same_content'
            AND {not_dev.format(col='c.reviewer_id')} ORDER BY md5('V3:' || c.candidate_id) LIMIT %(n)s""", "logical"),
        # V4：近似评论（家族内与统计组内）
        "V4": items("V4", f"""
            SELECT 'V4:' || c.candidate_id, c.logical_review_ids[1], c.logical_review_ids[2], false
            FROM task8.dup_candidate c WHERE c.stage_key IN (%(dk)s, %(ak)s) AND c.candidate_type = 'near_duplicate'
            AND {not_dev.format(col='c.reviewer_id')} ORDER BY md5('V4:' || c.candidate_id) LIMIT %(n)s""", "logical"),
        # V5：多作品统计组的连边（作品对）
        # V5：统计组的连边；同一对作品可能同时有 likely_duplicate_movie 与 shared_logical_review 两条依据，按作品对去重
        "V5": items("V5", """
            SELECT k, a, b, false FROM (
                SELECT DISTINCT 'V5:' || least(e->>'a', e->>'b') || ':' || greatest(e->>'a', e->>'b') AS k,
                       least(e->>'a', e->>'b') AS a, greatest(e->>'a', e->>'b') AS b
                FROM task8.stat_group g, jsonb_array_elements(g.basis) e
                WHERE g.attribution_stage_key = %(ak)s AND g.movie_count > 1) x
            ORDER BY md5(k) LIMIT %(n)s""", "movie"),
        # V6：同一用户在同一 ASIN 上的两条逻辑评论，彼此不是候选
        "V6": items("V6", f"""
            WITH g AS (SELECT l.reviewer_id, m.asin, min(m.logical_review_id) a, max(m.logical_review_id) b
                       FROM task8.review_mapping m JOIN task8.logical_review l ON l.dedup_stage_key = m.dedup_stage_key
                       AND l.logical_review_id = m.logical_review_id
                       WHERE m.dedup_stage_key = %(dk)s AND m.mapping_status = 'mapped' AND {not_dev.format(col='l.reviewer_id')}
                       GROUP BY 1, 2 HAVING count(DISTINCT m.logical_review_id) >= 2)
            SELECT DISTINCT ON (md5('V6:' || a || ':' || b)) 'V6:' || a || ':' || b, a, b, false FROM g
            WHERE NOT EXISTS (SELECT 1 FROM task8.dup_candidate c WHERE c.stage_key IN (%(dk)s, %(ak)s)
                              AND c.logical_review_ids @> ARRAY[g.a, g.b])
            ORDER BY md5('V6:' || a || ':' || b) LIMIT %(n)s""", "logical"),
    }
    return pools


def sample_items(pools: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """R46 的配额与补足：V2 先取 weak_support 至少 WEAK_FLOOR 项；某层不足时按 V2 → V3 → V4 补足到 120。"""
    chosen: dict[str, list[dict[str, Any]]] = {}
    for s, quota in QUOTA.items():
        pool = pools.get(s, [])
        if s == "V2":
            weak = sorted((x for x in pool if x["weak"]), key=lambda x: _md5(x["item_key"]))[:WEAK_FLOOR]
            rest = sorted((x for x in pool if x not in weak), key=lambda x: _md5(x["item_key"]))
            chosen[s] = weak + rest[:quota - len(weak)]
        else:
            chosen[s] = sorted(pool, key=lambda x: _md5(x["item_key"]))[:quota]
    filled = {s: 0 for s in QUOTA}
    for s in FILL_ORDER:
        short = TARGET_TOTAL - sum(len(v) for v in chosen.values())
        if short <= 0:
            break
        taken = {x["item_key"] for x in chosen[s]}
        extra = sorted((x for x in pools.get(s, []) if x["item_key"] not in taken), key=lambda x: _md5(x["item_key"]))
        chosen[s] += extra[:short]
        filled[s] = len(extra[:short])
    strata = {s: {"target": QUOTA[s], "actual": len(chosen[s]), "filled": filled[s],
                  **({"weak_support": sum(1 for x in chosen[s] if x["weak"])} if s == "V2" else {})} for s in QUOTA}
    items = [x for s in QUOTA for x in chosen[s]]
    items.sort(key=lambda x: _md5(x["item_key"]))           # R47：全局打乱
    for k, x in enumerate(items, start=1):
        x["item_id"] = f"t8_eval_{k:04d}"
    return items, strata


# ---------------------------------------------------------------- 盲测表（R47）

def _record_side(conn: psycopg.Connection, eid: str, cv: str, rd: str, record_no: int) -> dict[str, Any]:
    r = conn.execute(
        "SELECT s.reviewer_id, (s.review_time_utc AT TIME ZONE 'UTC')::date, s.score, s.summary_raw, "
        "left(s.review_text_raw, %s), s.product_id, e.edition_title, p.product_category "
        "FROM public.review_record r JOIN public.review_staging s ON s.extraction_id = r.extraction_id "
        "AND s.source_review_id = r.source_review_id "
        "LEFT JOIN task7.edition_resolved e ON e.conflict_version_id = %s AND e.asin = s.product_id "
        "LEFT JOIN task8.product_review_stats p ON p.review_dedup_version_id = %s AND p.asin = s.product_id "
        "WHERE r.extraction_id = %s AND r.record_no = %s", (TEXT_LIMIT, cv, rd, eid, record_no)).fetchone()
    return dict(zip(SIDE_FIELDS, (r[0], str(r[1]) if r[1] else "", r[2], r[3], r[4], r[5], r[6], r[7])))


def _movie_side(conn: psycopg.Connection, cv: str, movie_id: str) -> dict[str, Any]:
    title = conn.execute("SELECT title FROM task7.movie_resolved WHERE conflict_version_id = %s AND movie_id = %s",
                         (cv, movie_id)).fetchone()
    eds = conn.execute("SELECT asin, edition_title FROM task7.edition_resolved WHERE conflict_version_id = %s "
                       "AND product_kind = 'movie' AND movie_ids[1] = %s ORDER BY asin", (cv, movie_id)).fetchall()
    return {"reviewer_id": "", "review_date": "", "score": "", "summary": "", "text_500": "",
            "asin": " ".join(a for a, _ in eds), "product_title": title[0] if title else " | ".join(t for _, t in eds if t),
            "product_category": "movie"}


def blind_rows(conn: psycopg.Connection, rd: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    row = version_row(conn, rd)
    eid, cv, dk = row["extraction_id"], row["conflict_version_id"], row["dedup_stage_key"]
    rep = lambda lid: conn.execute("SELECT representative_record_no FROM task8.logical_review WHERE dedup_stage_key = %s "  # noqa: E731
                                   "AND logical_review_id = %s", (dk, lid)).fetchone()[0]
    out = []
    for x in items:
        if x["side"] == "movie":
            left, right = _movie_side(conn, cv, x["left_ref"]), _movie_side(conn, cv, x["right_ref"])
        else:
            ln, rn = ((int(x["left_ref"]), int(x["right_ref"])) if x["side"] == "record"
                      else (rep(x["left_ref"]), rep(x["right_ref"])))
            left, right = _record_side(conn, eid, cv, rd, ln), _record_side(conn, eid, cv, rd, rn)
        d = {"item_id": x["item_id"], "question": QUESTION["movie" if x["side"] == "movie" else "review"]}
        d.update({f"left_{k}": ("" if v is None else v) for k, v in left.items()})
        d.update({f"right_{k}": ("" if v is None else v) for k, v in right.items()})
        out.append(d)
    return out


# ---------------------------------------------------------------- AI 草稿（R49：只读盲测行）

def _norm_title(t: str) -> str:
    t = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", (t or "").casefold())
    return " ".join(re.findall(r"[a-z0-9]+", t))


def generate_ai_draft(row: dict[str, Any]) -> dict[str, str]:
    """只依据一行盲测表给出建议：(ai_label, ai_doubt, ai_reason)。不访问数据库，不知道系统层次。"""
    def draft(label: str, doubt: bool, reason: str) -> dict[str, str]:
        return {"ai_label": label, "ai_doubt": "true" if doubt else "false", "ai_reason": reason}

    if row["question"] == QUESTION["movie"]:
        a, b = _norm_title(row["left_product_title"]), _norm_title(row["right_product_title"])
        if a and a == b:
            return draft("same", False, "两侧作品标题去掉括号说明后相同")
        return draft("unsure", True, "标题不同或缺失，需要查商品页或 IMDb 核对")
    same_user = row["left_reviewer_id"] == row["right_reviewer_id"]
    fields = ("review_date", "score", "summary", "text_500")
    diff = [f for f in fields if str(row[f"left_{f}"]) != str(row[f"right_{f}"])]
    if same_user and not diff:
        return draft("same", False, "同一用户，日期、评分、摘要与正文前 500 字符都相同")
    if not same_user:
        return draft("different", False, "用户不同")
    if diff == ["summary"] or (diff and "text_500" not in diff):
        return draft("different", True, f"同一用户、正文相同，但 {'、'.join(diff)} 不同；需要核对是否针对不同商品或作品")
    sim = word_jaccard(word_set(row["left_text_500"]), word_set(row["right_text_500"]))
    if sim >= 0.9:
        return draft("same", True, f"同一用户、正文词集 Jaccard {sim:.2f}，可能是同一意见的重贴或修改版")
    return draft("different", False, f"同一用户、正文词集 Jaccard {sim:.2f}，内容不同")


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(columns), extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    reporting.write_atomic(path, buf.getvalue().encode("utf-8"))


def write_draft(blind_csv: Path, draft_csv: Path) -> int:
    """只读盲测表文件生成草稿（没有数据库连接）。"""
    with open(blind_csv, encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = [{**r, **generate_ai_draft(r), "human_label": "", "human_evidence": "", "note": ""} for r in rows]
    _write_csv(draft_csv, DRAFT_COLUMNS, out)
    return len(out)


# ---------------------------------------------------------------- 导出、导入、指标

def export_eval_set(conn: psycopg.Connection, rd: str, directory: Path, *, force: bool = False) -> dict[str, Any]:
    """抽样并写 eval_set、eval_item，导出 eval_items.csv、eval_items_draft.csv、LABELING.md。
    已导入标注时拒绝重导（force 时清除标注与评价项后重导）。"""
    row = version_row(conn, rd)
    if row["status"] != "verified":
        raise EvalError(f"去重版本 {rd} 尚未通过核对，不能导出评价表")
    sid = eval_set_id(rd)
    labeled = conn.execute("SELECT count(*) FROM task8.eval_label WHERE eval_set_id = %s", (sid,)).fetchone()[0]
    if labeled and not force:
        raise EvalError(f"评价集 {sid} 已导入 {labeled} 条标注；确需重导请加 --force（会清除标注）")
    items, strata = sample_items(build_pools(conn, rd))
    blind = blind_rows(conn, rd, items)
    directory.mkdir(parents=True, exist_ok=True)
    _write_csv(directory / "eval_items.csv", BLIND_COLUMNS, blind)
    n = write_draft(directory / "eval_items.csv", directory / "eval_items_draft.csv")
    reporting.write_atomic(directory / "LABELING.md", LABELING_GUIDE.encode("utf-8"))
    with conn.transaction():
        conn.execute("DELETE FROM task8.eval_label WHERE eval_set_id = %s", (sid,))
        conn.execute("DELETE FROM task8.eval_item WHERE eval_set_id = %s", (sid,))
        conn.execute("DELETE FROM task8.eval_set WHERE eval_set_id = %s", (sid,))
        conn.execute("INSERT INTO task8.eval_set (eval_set_id, extraction_id, review_dedup_version_id, rule_version, "
                     "strata, exported_file) VALUES (%s, %s, %s, %s, %s, %s)",
                     (sid, row["extraction_id"], rd, row["rule_version"], json.dumps(strata),
                      str(directory / "eval_items.csv")))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO task8.eval_item (eval_set_id, item_id, stratum, item_key, left_ref, right_ref, "
                            "system_decision) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                            [(sid, x["item_id"], x["stratum"], x["item_key"], x["left_ref"], x["right_ref"],
                              SYSTEM_DECISION[x["stratum"]]) for x in items])
    return {"eval_set_id": sid, "items": len(items), "draft_rows": n, "strata": strata, "directory": str(directory)}


def _truthy(text: str | None) -> bool:
    return (text or "").strip().lower() in ("true", "1", "yes", "y")


def import_labels(conn: psycopg.Connection, sid: str, csv_path: Path, annotator: str, *, allow_partial: bool = False,
                  force: bool = False) -> dict[str, Any]:
    """校验通过才全部写入 eval_label：标注值闭集；存疑或改判（与 ai_label 不同）的项必须有 human_evidence，unsure 除外。"""
    items = {r[0] for r in conn.execute("SELECT item_id FROM task8.eval_item WHERE eval_set_id = %s", (sid,))}
    if not items:
        raise EvalError(f"评价集 {sid} 没有评价项；先运行 export")
    existing = conn.execute("SELECT count(*) FROM task8.eval_label WHERE eval_set_id = %s", (sid,)).fetchone()[0]
    if existing and not force:
        raise EvalError(f"评价集 {sid} 已有 {existing} 条标注；确需重导请加 --force")
    raw = csv_path.read_bytes()
    rows = list(csv.DictReader(raw.decode("utf-8-sig").splitlines()))
    errors, seen, out = [], set(), []
    for r in rows:
        iid, label = (r.get("item_id") or "").strip(), (r.get("human_label") or "").strip()
        evidence = (r.get("human_evidence") or "").strip()
        if iid not in items:
            errors.append(f"{iid or '（空）'}：不在评价集 {sid} 中")
            continue
        if iid in seen:
            errors.append(f"{iid}：重复")
            continue
        seen.add(iid)
        if label not in LABELS:
            errors.append(f"{iid}：human_label 必须是 {LABELS} 之一，实际 {label!r}")
            continue
        doubt = _truthy(r.get("ai_doubt"))
        changed = (r.get("ai_label") or "").strip() != label
        if label != "unsure" and (doubt or changed) and not evidence:
            errors.append(f"{iid}：{'AI 存疑' if doubt else '与 AI 建议不同'}的项须填 human_evidence（外部依据）")
            continue
        if (r.get("note") or "").strip() and (r.get("note") or "").strip() == (r.get("ai_reason") or "").strip():
            errors.append(f"{iid}：note 不得原样粘贴 ai_reason")
            continue
        out.append((sid, iid, label, evidence or None, doubt, (r.get("note") or "").strip() or None, annotator))
    if not allow_partial and len(seen) != len(items):
        errors.append(f"标注 {len(seen)} 项，评价集有 {len(items)} 项（部分导入请加 --allow-partial）")
    if errors:
        raise EvalError("导入被拒绝：\n" + "\n".join(errors[:50]))
    digest = hashlib.sha256(raw).hexdigest()
    with conn.transaction():
        conn.execute("DELETE FROM task8.eval_label WHERE eval_set_id = %s", (sid,))
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO task8.eval_label (eval_set_id, item_id, label, evidence, ai_doubt, note, "
                            "annotator, import_file_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            [(*o, digest) for o in out])
    return {"eval_set_id": sid, "imported": len(out), "annotator": annotator, "file_sha256": digest}


def _ratio(n: int, d: int) -> float | str:
    return round(n / d, 4) if d else "无定义"


def metrics(conn: psycopg.Connection, sid: str) -> dict[str, Any]:
    """R48：合并精确率（V1、V2 标 same）、不合并正确率（V3、V6 标 different）、近似评论同一性比例（V4 标 same，只描述）、
    统计组同片率（V5 标 same）；unsure 单独计数不进分母，分母为 0 记“无定义”。"""
    rows = conn.execute("SELECT i.stratum, l.label, l.annotator FROM task8.eval_item i JOIN task8.eval_label l "
                        "USING (eval_set_id, item_id) WHERE i.eval_set_id = %s", (sid,)).fetchall()
    by: dict[str, dict[str, int]] = {s: {k: 0 for k in LABELS} for s in QUOTA}
    for s, label, _ in rows:
        by[s][label] += 1

    def rate(strata: tuple[str, ...], want: str) -> dict[str, Any]:
        n = sum(by[s][want] for s in strata)
        d = sum(by[s]["same"] + by[s]["different"] for s in strata)
        return {"numerator": n, "denominator": d, "unsure": sum(by[s]["unsure"] for s in strata), "value": _ratio(n, d)}

    return {
        "eval_set_id": sid, "labeled_total": len(rows), "annotators": sorted({a for *_, a in rows}),
        "merge_precision": rate(("V1", "V2"), "same"),
        "no_merge_correct": rate(("V3", "V6"), "different"),
        "near_duplicate_same_ratio": rate(("V4",), "same"),
        "stat_group_same_movie_rate": rate(("V5",), "same"),
        "by_stratum": by,
        "note": "指标只代表分层样本，不代表全量准确率；V4 的 same 比例只说明近似评论里同一意见重贴的比例，不是错误率；"
                "标注为 AI 预填、人工逐项审核。",
    }


def render_metrics(m: dict[str, Any]) -> str:
    lines = [f"# 任务 8 评价指标（{m['eval_set_id']}）", "", f"- 已标注 {m['labeled_total']} 项，标注人：{'、'.join(m['annotators'])}"]
    for k, name in (("merge_precision", "合并精确率（V1、V2）"), ("no_merge_correct", "不合并正确率（V3、V6）"),
                    ("near_duplicate_same_ratio", "近似评论同一性比例（V4，只描述）"),
                    ("stat_group_same_movie_rate", "统计组同片率（V5）")):
        v = m[k]
        lines.append(f"- {name}：{v['value']}（{v['numerator']}/{v['denominator']}，unsure {v['unsure']}）")
    lines += ["", "| 层 | same | different | unsure |", "|---|---|---|---|"]
    lines += [f"| {s} | {c['same']} | {c['different']} | {c['unsure']} |" for s, c in m["by_stratum"].items()]
    return "\n".join(lines + ["", m["note"], ""])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 8 评价工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_freeze = sub.add_parser("freeze", help="冻结开发池")
    p_export = sub.add_parser("export", help="抽样并导出盲测表与草稿")
    p_export.add_argument("--force", action="store_true")
    p_draft = sub.add_parser("draft", help="只读盲测表生成 AI 草稿")
    p_draft.add_argument("blind_csv")
    p_draft.add_argument("draft_csv")
    p_import = sub.add_parser("import", help="导入审核后的标注")
    p_import.add_argument("csv_path")
    p_import.add_argument("--annotator", required=True)
    p_import.add_argument("--allow-partial", action="store_true")
    p_import.add_argument("--force", action="store_true")
    p_metrics = sub.add_parser("metrics", help="计算指标并写 metrics.json、metrics.md")
    for p in (p_freeze, p_export, p_import, p_metrics):
        p.add_argument("--extraction-id", help="抽取 ID")
    for p in (p_export, p_import, p_metrics):
        p.add_argument("--version", help="去重版本号（默认最近一次）")
    args = parser.parse_args(argv)
    if args.cmd == "draft":
        print(json.dumps({"draft_rows": write_draft(Path(args.blind_csv), Path(args.draft_csv))}, ensure_ascii=False))
        return 0
    s = load_settings()
    with connect(s) as conn:
        eid = args.extraction_id or latest_full_extraction(conn)
        if args.cmd == "freeze":
            out: Any = freeze_dev_pool(conn, eid)
        else:
            rd = args.version or latest_version(conn, eid)
            directory = eval_dir(s.output_dir, eid, rd)
            if args.cmd == "export":
                out = export_eval_set(conn, rd, directory, force=args.force)
            elif args.cmd == "import":
                out = import_labels(conn, eval_set_id(rd), Path(args.csv_path), args.annotator,
                                    allow_partial=args.allow_partial, force=args.force)
            else:
                out = metrics(conn, eval_set_id(rd))
                reporting.write_atomic(directory / "metrics.json",
                                       (json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode())
                reporting.write_atomic(directory / "metrics.md", render_metrics(out).encode())
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
