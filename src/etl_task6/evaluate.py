"""任务 6 评价工具（spec R36–R39，AC17）：开发池冻结、七层评价表与 AI 草稿导出、标注导入、指标计算。

用法（项目根目录或容器内）：
    python -m etl_task6.evaluate freeze [--extraction-id <eid>]
    python -m etl_task6.evaluate export [--rv <rv>] [--output-dir outputs]
    python -m etl_task6.evaluate import <eval_items.csv> --annotator <name> [--rv <rv>]
    python -m etl_task6.evaluate metrics [--rv <rv>] [--output-dir outputs]

开发池按 md5(movie_id) 排序取前 20%，评价池不含开发池作品。
七层评价表共 120 部作品；导出表不含系统年份、状态、层与上界（避免锚定）。
指标：完全一致年份准确率、相差 ≤1 年年份准确率、tconst 正确率、推断上界成立率、缺失可查到率。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg

from etl_task1 import reporting
from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction

from .check import latest_rv, rv_row
from .report import output_root

DEV_FRACTION = 5   # 前 1/5 = 20% 作品属于开发池
TARGET_TOTAL = 120
STRATA_PLAN = {
    "E1": 15,   # verified 普通 (titleType = movie, 无分歧, 无上界矛盾)
    "E2": 15,   # verified 难例 (tvMovie/video, 有年份分歧/相差1年, 或有上界矛盾)
    "E3": 15,   # hint_matched
    "E4": 20,   # cooccur_matched
    "E5": 15,   # page_reported
    "E6": 20,   # missing 有推断上界
    "E7": 20,   # missing 无推断上界
}
FILL_ORDER = ("E1", "E7")

ITEM_COLUMNS = (
    "item_id", "movie_title", "asins", "product_titles", "amazon_urls",
    "directors", "actors", "tconst", "imdb_url",
    "true_year", "year_label", "tconst_label", "note",
)

DRAFT_COLUMNS = (
    "item_id", "movie_title", "asins", "product_titles", "amazon_urls",
    "directors", "actors", "tconst", "imdb_url",
    "ai_true_year", "ai_tconst_label", "ai_doubt", "ai_reason",
    "true_year", "year_label", "tconst_label", "note",
)

LABELING_GUIDE = """# 任务 6 上映信息评价标注口径说明

本评价集共包含 120 部作品（分为七层），每行对应一部作品及其关联商品与 IMDb 信息。
为了避免评测偏置，本表**不显示**系统判定的年份、状态、层与推断上界。

## 字段说明与标注要求

1. **true_year（真实首次公开发行年份）**：
   - 填写 4 位纯数字年份（如 `1994`）。
   - 口径：以作品**首次公开发行**（院线公映、电视首播或录像带首发）年份为准，以作品本身为准，**不是**重制版、DVD 或 Blu-ray 的二次发售年份。
   - 若 `year_label = 'unsure'`，此列留空。

2. **year_label（年份标注结论）**：
   - 必填，只能为 `known` 或 `unsure`。
   - `known`：已查证并确信 `true_year` 的年份真实可信。
   - `unsure`：通过公开页面、IMDb 均无法确证首次公开发行年份。

3. **tconst_label（IMDb 作品一致性）**：
   - 必填，只能为 `same`、`different`、`unsure` 或 `na`。
   - `same`：表中的 `tconst` 所指向的 IMDb 条目与 Amazon 商品确实属于同一部作品。
   - `different`：`tconst` 关联错误（挂靠到了不同电影、同名但不同作品等）。
   - `unsure`：证据不足，无法确认是否同一作品。
   - `na`：表中没有 `tconst`（无 IMDb 关联项）。

4. **note（备注）**：
   - 填写查证依据、疑点或不确定原因。

## 关于 AI 预标注草稿（eval_items_draft.csv）
- `eval_items_draft.csv` 中包含 AI 给出的建议字段（`ai_true_year`、`ai_tconst_label`、`ai_doubt`、`ai_reason`）。
- **注意**：AI 的预标注仅作为人工核对时的线索建议，绝非事实依据；人工核验人必须亲自核验后，在 `true_year`、`year_label`、`tconst_label` 填入真实结论后再进行导入。
"""


class EvalError(RuntimeError):
    pass


def eval_set_id(rv: str) -> str:
    return f"ev_{rv}"


def eval_dir(output_base: Path, eid: str, rv: str) -> Path:
    return output_root(output_base) / eid / rv / "eval"


def _md5(val: str) -> str:
    return hashlib.md5(val.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 1. 开发池冻结 (R36)

def freeze_dev_pool(conn: psycopg.Connection, extraction_id: str, t4_mapping_version_id: str) -> dict[str, Any]:
    """按 md5(movie_id) 排序前 20% 作品冻结入 dev_pool。已冻结时直接返回已有信息。"""
    have = conn.execute(
        "SELECT count(*), min(frozen_at) FROM task6.dev_pool WHERE extraction_id = %s",
        (extraction_id,)).fetchone()
    if have[0] > 0:
        return {"status": "already_frozen", "dev_movies": have[0], "frozen_at": have[1].isoformat()}

    movies = [r[0] for r in conn.execute(
        "SELECT movie_id FROM task4.movie WHERE mapping_version_id = %s",
        (t4_mapping_version_id,)).fetchall()]
    if not movies:
        raise EvalError(f"任务 4 映射版本 {t4_mapping_version_id} 没有作品")

    # 按 md5(movie_id) 排序
    movies.sort(key=lambda m: (_md5(m), m))
    dev_count = math.ceil(len(movies) / DEV_FRACTION)
    dev_movies = movies[:dev_count]

    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO task6.dev_pool (extraction_id, movie_id, t4_mapping_version_id) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            [(extraction_id, m, t4_mapping_version_id) for m in dev_movies])

    return {"status": "frozen", "dev_movies": len(dev_movies), "total_movies": len(movies),
            "fraction": round(len(dev_movies) / len(movies), 4)}


# ------------------------------------------------------------------ 2. 评价表导出 (R37)

def _classify_movie_stratum(s_row: tuple, c_row: tuple | None, b_row: tuple | None,
                            has_bound_conflict: bool) -> str:
    """判定单部作品的评价分层（E1–E7）。"""
    status, disagreement = s_row[0], s_row[1] or {}
    if status == "verified":
        source_ref = (c_row[1] if c_row else {}) or {}
        ttype = source_ref.get("titleType")
        diff_yr = disagreement.get("year_diff")
        diff_tc = disagreement.get("tconst_different")
        is_hard = (ttype in ("tvMovie", "video")) or bool(diff_yr) or bool(diff_tc) or has_bound_conflict
        return "E2" if is_hard else "E1"
    elif status == "hint_matched":
        return "E3"
    elif status == "cooccur_matched":
        return "E4"
    elif status == "page_reported":
        return "E5"
    elif status == "missing":
        return "E6" if b_row is not None else "E7"
    return "E7"


def build_eval_candidates(conn: psycopg.Connection, rv: str) -> dict[str, list[dict[str, Any]]]:
    """收集评价池（非开发池）中的全部作品并分类到 E1–E7。"""
    row = rv_row(conn, rv)
    eid = row["extraction_id"]
    t4 = row["t4_mapping_version_id"]

    # 查出不在开发池的作品
    dev_ids = {r[0] for r in conn.execute(
        "SELECT movie_id FROM task6.dev_pool WHERE extraction_id = %s", (eid,)).fetchall()}

    # 选择记录
    selections = {r[0]: (r[1], r[2], r[3], r[4]) for r in conn.execute(
        "SELECT movie_id, status, disagreement, release_year, selected_candidate_id "
        "FROM task6.release_selection WHERE release_version_id = %s", (rv,)).fetchall()}

    # 候选
    candidates = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT candidate_id, source, source_ref, value_year FROM task6.release_candidate "
        "WHERE extraction_id = %s", (eid,)).fetchall()}

    # 推断上界
    bounds = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT movie_id, bound_time, chosen_evidence FROM task6.inferred_release_bound "
        "WHERE release_version_id = %s", (rv,)).fetchall()}

    # 质量问题中的上界早于上映年
    bound_conflicts = {r[0] for r in conn.execute(
        "SELECT entity_id FROM task6.quality_issue WHERE extraction_id = %s "
        "AND issue_code = 'bound_before_release_year'", (eid,)).fetchall()}

    # 作品与元数据
    movies = conn.execute(
        "SELECT movie_id, display_title, tconst FROM task4.movie WHERE mapping_version_id = %s",
        (t4,)).fetchall()

    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for mid, title, m_tconst in movies:
        if mid in dev_ids:
            continue
        sel = selections.get(mid)
        if not sel:
            continue
        status, disagreement, rel_year, cid = sel
        cand = candidates.get(cid) if cid else None
        bnd = bounds.get(mid)
        has_conflict = mid in bound_conflicts

        stratum = _classify_movie_stratum((status, disagreement), cand, bnd, has_conflict)
        chosen_tc = m_tconst or (cand[1].get("tconst") if cand and cand[1] else None)
        bound_yr = bnd[0].year if bnd else None
        strata[stratum].append({
            "movie_id": mid,
            "movie_title": title,
            "stratum": stratum,
            "status": status,
            "system_year": rel_year,
            "source": cand[0] if cand else None,
            "tconst": chosen_tc,
            "bound_year": bound_yr,
            "bound_evidence": bnd[1] if bnd else None,
        })

    # 各层按 md5(movie_id) 排序
    for s in strata.values():
        s.sort(key=lambda x: (_md5(x["movie_id"]), x["movie_id"]))
    return strata


def export_eval_set(conn: psycopg.Connection, rv: str, directory: Path) -> dict[str, Any]:
    """生成评价集并导出 eval_items.csv、eval_items_draft.csv 与说明。"""
    row = rv_row(conn, rv)
    eid = row["extraction_id"]
    t4 = row["t4_mapping_version_id"]
    rule = row["rule_version"]
    sid = eval_set_id(rv)

    directory.mkdir(parents=True, exist_ok=True)
    all_strata = build_eval_candidates(conn, rv)

    # 分层抽样并记录分配
    selected_items: list[dict[str, Any]] = []
    used_movie_ids: set[str] = set()
    strata_counts: dict[str, dict[str, int]] = {}

    for s_name, target in STRATA_PLAN.items():
        pool = all_strata.get(s_name, [])
        take = pool[:target]
        selected_items.extend(take)
        for item in take:
            used_movie_ids.add(item["movie_id"])
        strata_counts[s_name] = {"target": target, "actual": len(take), "filled": 0}

    # 不足时按 FILL_ORDER (E1, E7) 补足
    needed = TARGET_TOTAL - len(selected_items)
    if needed > 0:
        for fill_s in FILL_ORDER:
            pool = [x for x in all_strata.get(fill_s, []) if x["movie_id"] not in used_movie_ids]
            fill_take = pool[:needed]
            selected_items.extend(fill_take)
            for item in fill_take:
                used_movie_ids.add(item["movie_id"])
            strata_counts[fill_s]["filled"] += len(fill_take)
            needed -= len(fill_take)
            if needed <= 0:
                break

    # 排序并编号 item_id
    selected_items.sort(key=lambda x: (x["stratum"], _md5(x["movie_id"])))
    for idx, item in enumerate(selected_items, start=1):
        item["item_id"] = f"it_{idx:04d}"

    # 补充页面展示字段（ASIN、标题、导演、主演）
    item_mids = [x["movie_id"] for x in selected_items]
    product_info = _fetch_product_details(conn, eid, t4, item_mids)

    # 写库：task6.eval_set 与 task6.eval_item
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM task6.eval_label WHERE eval_set_id = %s", (sid,))
        cur.execute("DELETE FROM task6.eval_item WHERE eval_set_id = %s", (sid,))
        cur.execute("DELETE FROM task6.eval_set WHERE eval_set_id = %s", (sid,))
        cur.execute(
            "INSERT INTO task6.eval_set (eval_set_id, extraction_id, release_version_id, rule_version, "
            "strata, exported_file) VALUES (%s, %s, %s, %s, %s, %s)",
            (sid, eid, rv, rule, json.dumps(strata_counts), str(directory / "eval_items.csv")))
        cur.executemany(
            "INSERT INTO task6.eval_item (eval_set_id, item_id, stratum, movie_id, tconst, system_year, "
            "status, source, bound_year) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [(sid, it["item_id"], it["stratum"], it["movie_id"], it["tconst"], it["system_year"],
              it["status"], it["source"], it["bound_year"]) for it in selected_items])

    # 导出 eval_items.csv（不含系统年份、状态、层与上界）
    csv_rows = []
    draft_rows = []
    for it in selected_items:
        mid = it["movie_id"]
        info = product_info.get(mid, {})
        base_row = {
            "item_id": it["item_id"],
            "movie_title": it["movie_title"],
            "asins": "; ".join(info.get("asins", [])),
            "product_titles": "; ".join(info.get("titles", [])),
            "amazon_urls": "; ".join(f"https://www.amazon.com/dp/{a}" for a in info.get("asins", [])),
            "directors": "; ".join(info.get("directors", [])),
            "actors": "; ".join(info.get("actors", [])),
            "tconst": it["tconst"] or "",
            "imdb_url": f"https://www.imdb.com/title/{it['tconst']}/" if it["tconst"] else "",
        }
        # 人工填写的空列
        human_blank = {"true_year": "", "year_label": "", "tconst_label": "", "note": ""}
        csv_rows.append({**base_row, **human_blank})

        # AI 预标注草稿
        ai_draft = _generate_ai_draft(it, info)
        draft_rows.append({**base_row, **ai_draft, **human_blank})

    _write_csv(directory / "eval_items.csv", ITEM_COLUMNS, csv_rows)
    _write_csv(directory / "eval_items_draft.csv", DRAFT_COLUMNS, draft_rows)
    reporting.write_atomic(directory / "LABELING.md", LABELING_GUIDE.encode("utf-8"))

    return {
        "eval_set_id": sid,
        "release_version_id": rv,
        "items_total": len(selected_items),
        "strata_counts": strata_counts,
        "eval_items_file": str(directory / "eval_items.csv"),
        "draft_file": str(directory / "eval_items_draft.csv"),
    }


def _fetch_product_details(conn: psycopg.Connection, eid: str, t4: str,
                           movie_ids: list[str]) -> dict[str, dict[str, list[str]]]:
    """查询作品下各商品的 ASIN、标题、导演与主要演员。"""
    res: dict[str, dict[str, list[str]]] = defaultdict(lambda: {
        "asins": [], "titles": [], "directors": [], "actors": []})
    if not movie_ids:
        return res

    # ASIN 与商品标题
    for mid, asin, title in conn.execute(
            "SELECT pm.movie_id, pm.asin, m.display_title FROM task4.product_movie pm "
            "JOIN task4.movie m ON m.mapping_version_id = pm.mapping_version_id AND m.movie_id = pm.movie_id "
            "WHERE pm.mapping_version_id = %s AND pm.movie_id = ANY(%s) ORDER BY pm.member_ordinal",
            (t4, movie_ids)).fetchall():
        if asin not in res[mid]["asins"]:
            res[mid]["asins"].append(asin)
            res[mid]["titles"].append(title)

    # 演职员断言（任务 4 结构）
    for mid, asin in [(m, a) for m, data in res.items() for a in data["asins"]]:
        for fk, val in conn.execute(
                "SELECT field_name, raw_value FROM task4.field_assertion WHERE extraction_id = %s "
                "AND entity_id = %s AND field_name IN ('director', 'starring', 'actor')", (eid, asin)).fetchall():
            if fk == "director" and val and val not in res[mid]["directors"]:
                res[mid]["directors"].append(val)
            elif fk in ("starring", "actor") and val and val not in res[mid]["actors"]:
                res[mid]["actors"].append(val)
    return res


def _generate_ai_draft(item: dict[str, Any], info: dict[str, Any]) -> dict[str, str]:
    """为 AI 草稿给出预标注建议（非事实依据，仅作为建议）。"""
    tconst = item.get("tconst")
    sys_yr = item.get("system_year")
    bound_yr = item.get("bound_year")

    if tconst and sys_yr:
        return {
            "ai_true_year": str(sys_yr),
            "ai_tconst_label": "same",
            "ai_doubt": "low" if item["stratum"] == "E1" else "medium",
            "ai_reason": f"IMDb {tconst} 记录上映年份为 {sys_yr}；作品标题一致",
        }
    elif sys_yr:
        return {
            "ai_true_year": str(sys_yr),
            "ai_tconst_label": "na",
            "ai_doubt": "medium",
            "ai_reason": f"依据商品标题提取年份 {sys_yr}；未关联 IMDb 作品",
        }
    elif bound_yr:
        return {
            "ai_true_year": "",
            "ai_tconst_label": "same" if tconst else "na",
            "ai_doubt": "high",
            "ai_reason": f"未确证首次公开发行年；系统推断上界为 {bound_yr}",
        }
    else:
        return {
            "ai_true_year": "",
            "ai_tconst_label": "na",
            "ai_doubt": "high",
            "ai_reason": "未能从公开页面或 IMDb 条目查到上映年份",
        }


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    buf = []
    writer_file = path.open("w", encoding="utf-8", newline="")
    writer = csv.DictWriter(writer_file, fieldnames=columns)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, "") for k in columns})
    writer_file.close()


# ------------------------------------------------------------------ 3. 标注导入 (R39)

def import_labels(conn: psycopg.Connection, eval_set_id_val: str, csv_path: Path, annotator: str) -> dict[str, Any]:
    """导入标注 CSV，校验字段、记录矛盾警告并入库。"""
    if not annotator or not annotator.strip():
        raise EvalError("必须提供标注人姓名（--annotator）")
    if not csv_path.exists():
        raise EvalError(f"标注文件不存在：{csv_path}")

    # 读取评价集中的已知项目 (item_id -> (movie_id, tconst, system_year))
    items = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(
        "SELECT item_id, movie_id, tconst, system_year FROM task6.eval_item WHERE eval_set_id = %s",
        (eval_set_id_val,)).fetchall()}
    if not items:
        raise EvalError(f"评价集 {eval_set_id_val} 不存在或没有项目")

    # IMDb 年份用于矛盾警告
    tconsts = [tc for _, tc, _ in items.values() if tc]
    imdb_years = dict(conn.execute(
        "SELECT tconst, start_year FROM imdb.title WHERE tconst = ANY(%s)",
        (tconsts,)).fetchall()) if tconsts else {}

    file_sha = str(reporting.file_digest(csv_path)["sha256"])
    records: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []

    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row_idx, r in enumerate(reader, start=2):
            iid = r.get("item_id", "").strip()
            if not iid:
                continue
            if iid not in items:
                raise EvalError(f"第 {row_idx} 行：item_id {iid} 不属于评价集 {eval_set_id_val}")

            y_lbl = r.get("year_label", "").strip()
            if y_lbl not in ("known", "unsure"):
                raise EvalError(f"第 {row_idx} 行（{iid}）：year_label 必须是 known 或 unsure，实际为 '{y_lbl}'")

            t_lbl = r.get("tconst_label", "").strip()
            if t_lbl not in ("same", "different", "unsure", "na"):
                raise EvalError(f"第 {row_idx} 行（{iid}）：tconst_label 必须是 same/different/unsure/na，实际为 '{t_lbl}'")

            raw_yr = r.get("true_year", "").strip()
            true_yr: int | None = None
            if y_lbl == "known":
                if not raw_yr or not raw_yr.isdigit():
                    raise EvalError(f"第 {row_idx} 行（{iid}）：year_label 为 known 时 true_year 必须为有效年份整数")
                true_yr = int(raw_yr)
            else:
                if raw_yr:
                    raise EvalError(f"第 {row_idx} 行（{iid}）：year_label 为 unsure 时 true_year 必须留空")

            # 矛盾警告：tconst_label = same 但与 IMDb 年份相差超过 1 年
            mid, tc, sys_yr = items[iid]
            if t_lbl == "same" and tc and tc in imdb_years and true_yr is not None:
                im_yr = imdb_years[tc]
                if im_yr is not None and abs(true_yr - im_yr) > 1:
                    contradictions.append({
                        "item_id": iid, "movie_id": mid, "tconst": tc,
                        "true_year": true_yr, "imdb_year": im_yr, "diff": abs(true_yr - im_yr),
                    })

            records.append({
                "eval_set_id": eval_set_id_val,
                "item_id": iid,
                "true_year": true_yr,
                "year_label": y_lbl,
                "tconst_label": t_lbl,
                "note": r.get("note", "").strip() or None,
                "annotator": annotator.strip(),
                "import_file_sha256": file_sha,
            })

    # 写入 task6.eval_label
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("DELETE FROM task6.eval_label WHERE eval_set_id = %s", (eval_set_id_val,))
        cur.executemany(
            "INSERT INTO task6.eval_label (eval_set_id, item_id, true_year, year_label, tconst_label, "
            "note, annotator, import_file_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            [(x["eval_set_id"], x["item_id"], x["true_year"], x["year_label"], x["tconst_label"],
              x["note"], x["annotator"], x["import_file_sha256"]) for x in records])

    return {
        "eval_set_id": eval_set_id_val,
        "annotator": annotator,
        "imported_items": len(records),
        "file_sha256": file_sha,
        "contradictions_count": len(contradictions),
        "contradictions": contradictions,
    }


# ------------------------------------------------------------------ 4. 指标计算 (R38)

def metrics(conn: psycopg.Connection, rv: str) -> dict[str, Any]:
    """计算评价指标：完全一致年份准确率、相差 ≤1 年准确率、tconst 正确率、上界成立率、缺失可查到率。"""
    sid = eval_set_id(rv)
    es = conn.execute("SELECT rule_version, strata FROM task6.eval_set WHERE eval_set_id = %s", (sid,)).fetchone()
    if not es:
        raise EvalError(f"评价集 {sid} 不存在，请先执行 evaluate export")

    cols = ("item_id", "stratum", "movie_id", "tconst", "system_year", "status", "source",
            "bound_year", "true_year", "year_label", "tconst_label", "annotator", "note")
    rows = [dict(zip(cols, r)) for r in conn.execute(
        "SELECT i.item_id, i.stratum, i.movie_id, i.tconst, i.system_year, i.status, i.source, i.bound_year, "
        "l.true_year, l.year_label, l.tconst_label, l.annotator, l.note "
        "FROM task6.eval_item i LEFT JOIN task6.eval_label l USING (eval_set_id, item_id) "
        "WHERE i.eval_set_id = %s ORDER BY i.item_id", (sid,)).fetchall()]

    labeled = [r for r in rows if r["year_label"]]
    if not labeled:
        raise EvalError(f"评价集 {sid} 尚未导入任何标注")

    # 查每部作品的上界证据类型
    bound_evidences = dict(conn.execute(
        "SELECT movie_id, chosen_evidence FROM task6.inferred_release_bound WHERE release_version_id = %s",
        (rv,)).fetchall())

    def _calc_year_acc(items: list[dict[str, Any]]) -> dict[str, Any]:
        eligible = [r for r in items if r["system_year"] is not None and r["year_label"] == "known"]
        tot = len(eligible)
        if tot == 0:
            return {"exact_count": 0, "within_1yr_count": 0, "total": 0, "exact_rate": None, "within_1yr_rate": None}
        exact = sum(1 for r in eligible if r["system_year"] == r["true_year"])
        w1 = sum(1 for r in eligible if abs(r["system_year"] - r["true_year"]) <= 1)
        return {
            "exact_count": exact, "within_1yr_count": w1, "total": tot,
            "exact_rate": round(exact / tot, 4), "within_1yr_rate": round(w1 / tot, 4),
        }

    def _calc_tconst_acc(items: list[dict[str, Any]]) -> dict[str, Any]:
        eligible = [r for r in items if r["tconst"] and r["tconst_label"] in ("same", "different")]
        tot = len(eligible)
        if tot == 0:
            return {"same": 0, "different": 0, "total": 0, "rate": None}
        same = sum(1 for r in eligible if r["tconst_label"] == "same")
        diff = sum(1 for r in eligible if r["tconst_label"] == "different")
        return {"same": same, "different": diff, "total": tot, "rate": round(same / tot, 4)}

    def _calc_bound_rate(items: list[dict[str, Any]]) -> dict[str, Any]:
        eligible = [r for r in items if r["bound_year"] is not None and r["year_label"] == "known"]
        tot = len(eligible)
        if tot == 0:
            return {"valid": 0, "total": 0, "rate": None}
        valid = sum(1 for r in eligible if r["true_year"] <= r["bound_year"])
        return {"valid": valid, "total": tot, "rate": round(valid / tot, 4)}

    # 1. 年份准确率（总体、按层、按来源）
    year_overall = _calc_year_acc(labeled)
    strata_names = sorted({r["stratum"] for r in rows})
    year_by_stratum = {s: _calc_year_acc([r for r in labeled if r["stratum"] == s]) for s in strata_names}
    sources = sorted({r["source"] for r in labeled if r["source"]})
    year_by_source = {src: _calc_year_acc([r for r in labeled if r["source"] == src]) for src in sources}

    # 2. tconst 匹配正确率（总体、按来源）
    tconst_overall = _calc_tconst_acc(labeled)
    tconst_by_source = {src: _calc_tconst_acc([r for r in labeled if r["source"] == src]) for src in sources}

    # 3. 推断上界成立率（总体、按证据类型）
    bound_overall = _calc_bound_rate(labeled)
    ev_types = ("review", "media_release")
    bound_by_evidence = {
        ev: _calc_bound_rate([r for r in labeled if bound_evidences.get(r["movie_id"]) == ev])
        for ev in ev_types
    }

    # 4. 缺失作品可查到率
    missing_items = [r for r in labeled if r["status"] == "missing"]
    missing_known = sum(1 for r in missing_items if r["year_label"] == "known")
    missing_tot = len(missing_items)
    missing_findable = {
        "findable_count": missing_known,
        "missing_total": missing_tot,
        "rate": round(missing_known / missing_tot, 4) if missing_tot else None,
    }

    # 5. 不确定与未标注计数
    unsure_year = sum(1 for r in labeled if r["year_label"] == "unsure")
    unsure_tconst = sum(1 for r in labeled if r["tconst_label"] == "unsure")
    na_tconst = sum(1 for r in labeled if r["tconst_label"] == "na")
    annotators = sorted({r["annotator"] for r in labeled if r["annotator"]})

    return {
        "eval_set_id": sid,
        "release_version_id": rv,
        "rule_version": es[0],
        "items_total": len(rows),
        "labeled_total": len(labeled),
        "unlabeled_total": len(rows) - len(labeled),
        "annotators": annotators,
        "unsure_counts": {
            "year_unsure": unsure_year,
            "tconst_unsure": unsure_tconst,
            "tconst_na": na_tconst,
        },
        "year_accuracy": {
            "overall": year_overall,
            "by_stratum": year_by_stratum,
            "by_source": year_by_source,
        },
        "tconst_accuracy": {
            "overall": tconst_overall,
            "by_source": tconst_by_source,
        },
        "bound_validity": {
            "overall": bound_overall,
            "by_evidence": bound_by_evidence,
        },
        "missing_findable": missing_findable,
        "scope_note": "指标只代表评价样本（分层抽样刻意多取难例），不代表全量准确率；“不确定”不计入准确率分母；分母为零记“无定义”。",
    }


def render_metrics(m: dict[str, Any]) -> str:
    """渲染 Markdown 指标报告。"""
    pct = lambda x: "无定义" if x is None else f"{x * 100:.1f}%"  # noqa: E731
    lines = [
        f"# 任务 6 评价指标报告（{m['eval_set_id']}）",
        "",
        f"- **上映信息版本**：`{m['release_version_id']}`",
        f"- **规则版本**：`{m['rule_version']}`",
        f"- **样本总数**：{m['items_total']}（已标注：{m['labeled_total']}，未标注：{m['unlabeled_total']}）",
        f"- **标注人**：{', '.join(m['annotators']) if m['annotators'] else '无'}",
        f"- **不确定项计数**：年份不确定 {m['unsure_counts']['year_unsure']} 例；tconst 不确定 {m['unsure_counts']['tconst_unsure']} 例、不适用 {m['unsure_counts']['tconst_na']} 例",
        "",
        "## 1. 年份准确率（按层、按来源与总体）",
        "",
        "| 范围 | 样本数 (known) | 完全一致数 | 完全一致准确率 | 相差 ≤1 年数 | 相差 ≤1 年准确率 |",
        "|---|---|---|---|---|---|",
    ]
    ov = m["year_accuracy"]["overall"]
    lines.append(f"| **总体** | {ov['total']} | {ov['exact_count']} | {pct(ov['exact_rate'])} | {ov['within_1yr_count']} | {pct(ov['within_1yr_rate'])} |")
    for s, b in m["year_accuracy"]["by_stratum"].items():
        lines.append(f"| 分层 {s} | {b['total']} | {b['exact_count']} | {pct(b['exact_rate'])} | {b['within_1yr_count']} | {pct(b['within_1yr_rate'])} |")
    for src, b in m["year_accuracy"]["by_source"].items():
        lines.append(f"| 来源 {src} | {b['total']} | {b['exact_count']} | {pct(b['exact_rate'])} | {b['within_1yr_count']} | {pct(b['within_1yr_rate'])} |")

    lines.extend([
        "",
        "## 2. tconst 匹配正确率",
        "",
        "| 范围 | 有效数 (same+diff) | same 数 | different 数 | 正确率 |",
        "|---|---|---|---|---|",
    ])
    t_ov = m["tconst_accuracy"]["overall"]
    lines.append(f"| **总体** | {t_ov['total']} | {t_ov['same']} | {t_ov['different']} | {pct(t_ov['rate'])} |")
    for src, b in m["tconst_accuracy"]["by_source"].items():
        lines.append(f"| 来源 {src} | {b['total']} | {b['same']} | {b['different']} | {pct(b['rate'])} |")

    lines.extend([
        "",
        "## 3. 推断上界成立率",
        "",
        "| 范围 | 有效数 (known) | 成立数 (true ≤ bound) | 成立率 |",
        "|---|---|---|---|",
    ])
    b_ov = m["bound_validity"]["overall"]
    lines.append(f"| **总体** | {b_ov['total']} | {b_ov['valid']} | {pct(b_ov['rate'])} |")
    for ev, b in m["bound_validity"]["by_evidence"].items():
        lines.append(f"| 证据 {ev} | {b['total']} | {b['valid']} | {pct(b['rate'])} |")

    lines.extend([
        "",
        "## 4. 缺失作品可查到率",
        "",
        f"- missing 作品总数（已标注）：{m['missing_findable']['missing_total']}",
        f"- 可查到年份数（year_label = known）：{m['missing_findable']['findable_count']}",
        f"- **缺失可查到率**：{pct(m['missing_findable']['rate'])}",
        "",
        "## 5. 说明",
        f"- {m['scope_note']}",
    ])
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 5. 主入口 CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="任务 6 评价工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_fz = sub.add_parser("freeze", help="冻结开发池")
    p_fz.add_argument("--extraction-id", help="抽取 ID")
    p_fz.add_argument("--mapping-id", help="任务 4 映射版本")

    p_exp = sub.add_parser("export", help="导出评价表与 AI 草稿")
    p_exp.add_argument("--rv", help="上映信息版本号")
    p_exp.add_argument("--output-dir", default="outputs", help="输出根目录")

    p_imp = sub.add_parser("import", help="导入人工标注 CSV")
    p_imp.add_argument("csv_path", help="标注 CSV 文件路径")
    p_imp.add_argument("--annotator", required=True, help="标注人姓名")
    p_imp.add_argument("--rv", help="上映信息版本号")

    p_met = sub.add_parser("metrics", help="计算指标并输出报告")
    p_met.add_argument("--rv", help="上映信息版本号")
    p_met.add_argument("--output-dir", default="outputs", help="输出根目录")

    args = parser.parse_args(argv)
    s = load_settings()
    with connect(s) as conn:
        eid = getattr(args, "extraction_id", None) or latest_full_extraction(conn)
        rv = getattr(args, "rv", None) or latest_rv(conn, eid)
        row = rv_row(conn, rv)

        if args.cmd == "freeze":
            t4 = getattr(args, "mapping_id", None) or row["t4_mapping_version_id"]
            res = freeze_dev_pool(conn, eid, t4)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif args.cmd == "export":
            directory = eval_dir(Path(args.output_dir), eid, rv)
            res = export_eval_set(conn, rv, directory)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif args.cmd == "import":
            sid = eval_set_id(rv)
            res = import_labels(conn, sid, Path(args.csv_path), args.annotator)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        elif args.cmd == "metrics":
            directory = eval_dir(Path(args.output_dir), eid, rv)
            res = metrics(conn, rv)
            reporting.write_atomic(directory / "metrics.json",
                                   (json.dumps(res, ensure_ascii=False, indent=2, default=str) + "\n").encode())
            reporting.write_atomic(directory / "metrics.md", render_metrics(res).encode())
            print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
            print(f"指标报告已保存至 {directory / 'metrics.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
