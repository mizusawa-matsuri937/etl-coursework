"""任务 5 评价工具（spec R35–R38，AC13、AC15）：开发池冻结、评价表与簇抽查表导出、标注导入、指标。

流程（命令都在容器内、工作目录 /opt/etl 运行；AI 只在草稿副本里预标注，标注由用户完成）：
    python -m etl_task5.evaluate freeze                       # 冻结开发池：先于任何调参，只做一次
    python -m etl_task5.evaluate export                       # 导出 120 对评价表（规则版本此时必须已冻结）
    python -m etl_task5.evaluate import <eval_pairs.csv> --annotator <姓名>
    python -m etl_task5.evaluate clusters-export              # 导出 20 个人物的抽查表
    python -m etl_task5.evaluate clusters-import <cluster_sample.csv> --annotator <姓名>
    python -m etl_task5.evaluate metrics                      # Precision、Recall、F1（按层与按途径）、实例、复核覆盖率与簇抽查结果

开发池是按 md5(姓氏键) 排序的前 20% 姓氏键，只用于调阈值与决定特征开关；评价对的两边姓氏键都不在开发池。导出的评价表不含系统判定
（避免锚定），系统判定存在库里的 eval_pair。指标只代表评价样本（分层抽样刻意多取难例），不代表全量准确率。
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from etl_task1 import reporting
from etl_task1.config import load_settings
from etl_task1.db import connect
from etl_task2.crawl.crawler import latest_full_extraction
from etl_task4.evaluate import LABELS, EvalError, _read_labeled, _write_csv, prf

from . import T4_NORM_VERSION
from .check import latest_pmap
from .report import FINAL, output_root, pmap_row

DEV_FRACTION = 5                 # 前 1/5 = 20% 的姓氏键属于开发池
DEFAULT_PLAN = {"S1": 25, "S2": 10, "S3": 15, "S4": 20, "S5": 15, "S6": 10, "S7": 15, "S8": 10}      # 共 120 对
FILL_ORDER = ("S1", "S4")        # 某层不足时按此顺序补足
CLUSTER_PLAN = {"3+": 10, "2": 10}
ITERATION_MIN = 5                # 迭代（合作者佐证）产生的人物至少这么多个进入抽查（不足时有多少取多少）
ROUTE_OF_STRATUM = {"S1": "credits", "S7": "credits", "S8": "filmography", "S2": "co_credit"}
STRATUM_NAMES = {"S1": "别名合并（外部 ID，写法不同）", "S2": "合作者佐证合并", "S3": "同名不同 nconst", "S4": "同名未合并、无硬冲突",
                 "S5": "中间缩写 / 名字缩写 / 姓名顺序", "S6": "近似名", "S7": "外部 ID 合并、写法相同", "S8": "作品年表反查合并"}
PAIR_COLUMNS = ("pair_id", "stratum", "left_mention", "right_mention", "left_raw", "right_raw", "left_role", "right_role",
                "left_asin", "right_asin", "left_product_title", "right_product_title", "left_work", "right_work",
                "left_year", "right_year", "left_collaborators", "right_collaborators", "left_nconst", "right_nconst",
                "left_imdb_url", "right_imdb_url", "left_amazon_url", "right_amazon_url", "filmography_work",
                "filmography_url", "label", "note")
DRAFT_COLUMNS = PAIR_COLUMNS[:-2] + ("ai_label", "ai_doubt", "ai_reason") + PAIR_COLUMNS[-2:]
CLUSTER_COLUMNS = ("cluster_no", "person_id", "mention_key", "raw", "role", "asin", "work_title", "work_year", "basis",
                   "round", "nconst", "imdb_url", "amazon_url", "cluster_label", "outlier", "note")
LABELING = """# 标注口径

评价表 `eval_pairs.csv`：每行一对人物提及（来自两个不同的作品），在 `label` 列填 `same`、`different` 或 `unsure`，需要时写 `note`。

- **same（同一个真实的人）**：两边指的是同一个人（姓名写法不同、倒序、缩写、不同版本署名，都算同一个人）。
- **different（不同的人）**：同名不同人、父子或同姓、名字缩写相同但是不同人。
- **unsure**：页面与 IMDb 的证据都无法判断时标 `unsure` 并写 `note`；不计入指标，单独报告数量。
- 依据：页面（`*_amazon_url`、作品、合作者、角色）和 IMDb 人物页（`*_imdb_url` 的作品年表、出生年份）为准，不要凭记忆；本表不显示系统的判定。
- `filmography_work` 不为空的行：其中一边是经“已核实人物的作品年表”挂靠到 IMDb 人物的，请核对这部作品确实是该商品对应的作品。
- 只有一位标注人，没有第二标注人；证据不足就标 `unsure`。导入时会自动检查明显矛盾（两边关联同一 nconst 却标 `different`，
  两边关联不同 nconst 却标 `same`），矛盾项只列出供复看，不会改动你的标注。
- `eval_pairs_draft.csv` 是 AI 的预标注草稿（`ai_label`、`ai_doubt`、`ai_reason`），依据只用表中字段与公开的 IMDb 人物页；
  请逐条审核后在 `label` 列填你自己的结论，并以你为标注人导入。

簇抽查表 `cluster_sample.csv`：每个人物若干行（每行一条提及）。逐人物在该人物任一行的 `cluster_label` 填 `same_person`
（全部是同一个人）或 `mixed`（混入了不同的人）；`mixed` 时在混入的提及所在行的 `outlier` 列填 `x`。
"""


def eval_dir(output_dir: Path, eid: str, pmap: str) -> Path:
    return output_root(output_dir) / eid / pmap / "eval"


def eval_set_id(pmap: str) -> str:
    return f"ev_{pmap}"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# ---------------------------------------------------------------- 开发池（R35）

def freeze_dev_pool(conn: psycopg.Connection, extraction_id: str, *, parser_version: str, norm_version: str) -> dict[str, Any]:
    """冻结开发池：全部 resolvable 提及的姓氏键按 md5 排序，前 20%（向上取整）属于开发池。已冻结时什么都不做、返回已有的。"""
    have = conn.execute("SELECT count(*), min(norm_version), min(frozen_at) FROM task5.dev_pool WHERE extraction_id = %s",
                        (extraction_id,)).fetchone()
    if have[0]:
        return {"status": "already_frozen", "dev_family_keys": have[0], "norm_version": have[1], "frozen_at": have[2]}
    keys = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT family_key FROM task5.person_mention WHERE extraction_id = %s AND parser_version = %s AND "
        "norm_version = %s AND status = 'resolvable' AND family_key <> ''", (extraction_id, parser_version, norm_version))},
        key=lambda k: (_md5(k), k))
    dev = keys[:-(-len(keys) // DEV_FRACTION)]
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany("INSERT INTO task5.dev_pool (extraction_id, family_key, norm_version) VALUES (%s, %s, %s)",
                        [(extraction_id, k, norm_version) for k in dev])
    return {"status": "frozen", "dev_family_keys": len(dev), "family_keys": len(keys), "norm_version": norm_version}


def dev_family_keys(conn: psycopg.Connection, extraction_id: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT family_key FROM task5.dev_pool WHERE extraction_id = %s", (extraction_id,))}


# ---------------------------------------------------------------- 评价样本（R36）

def _pair_id(stratum: str, a: str, b: str) -> str:
    return _md5(f"{stratum}|{a}|{b}")[:12]


def _load_world(conn: psycopg.Connection, m: dict[str, Any]) -> dict[str, Any]:
    """人物映射版本里所有人物组（代表提及、作品单位、外部 ID 与途径、所属人物）和每个候选的最终判定。"""
    pm, key = m["person_mapping_version_id"], m["link_key"]
    groups: dict[str, dict[str, Any]] = {}
    for gk, unit, movie, roles, family, nconst, route, year, person, basis, raw, role, asin in conn.execute(
            "SELECT g.group_key, g.work_unit, g.movie_id, g.role_classes, g.family_key, g.nconst, g.nconst_route, g.year, "
            "mp.person_id, mp.basis, m.raw_value, m.role_class, m.asin FROM task5.person_group g JOIN task5.mention_person mp ON "
            "mp.person_mapping_version_id = %s AND mp.mention_key = split_part(g.group_key, '|', 2) JOIN task5.person_mention m ON "
            "m.extraction_id = g.extraction_id AND m.parser_version = %s AND m.norm_version = %s AND m.mention_key = mp.mention_key "
            "WHERE g.extraction_id = %s AND g.stage_key = %s", (pm, m["parser_version"], m["norm_version"],
                                                                 m["extraction_id"], key)):
        groups[gk] = {"key": gk, "rep": gk.split("|", 1)[1], "unit": unit, "movie": movie, "roles": list(roles),
                      "family": family, "nconst": nconst, "route": route, "year": year, "person": person,
                      "human": basis == "human", "raw": raw, "role": role, "asin": asin}
    units: dict[str, list[str]] = defaultdict(list)
    for gk, g in groups.items():
        units[g["unit"]].append(gk)
    p = {"eid": m["extraction_id"], "key": key, "rv": m["rule_version"]}
    cands = {(ga, gb): {"id": cid, "tier": tier, "veto": list(veto), "n": feats.get("n_level"),
                        "ext": (feats.get("external") or {})}
             for cid, ga, gb, tier, veto, feats in conn.execute(
                 f"{FINAL} SELECT person_candidate_id, group_a, group_b, tier, veto, features FROM fin", p)}
    return {"groups": groups, "units": units, "cands": cands}


def _candidate_pairs(w: dict[str, Any], ok) -> dict[str, list[tuple]]:
    """各层的候选 (stratum, pair_id, 组 a, 组 b)；两边姓氏键都不在开发池（ok）、来自不同作品单位。"""
    g, out = w["groups"], defaultdict(list)

    def add(s: str, a: str, b: str) -> None:
        a, b = sorted((a, b))
        if g[a]["unit"] != g[b]["unit"] and ok(g[a]) and ok(g[b]):
            out[s].append((s, _pair_id(s, a, b), a, b))

    by_nconst: dict[str, list[str]] = defaultdict(list)
    for k, x in g.items():
        if x["nconst"]:
            by_nconst[x["nconst"]].append(k)
    for gs in by_nconst.values():
        for a, b in itertools.combinations(sorted(gs), 2):
            ra, rb = g[a]["route"], g[b]["route"]
            if {ra, rb} == {"credits", "filmography"}:
                add("S8", a, b)
            elif ra == rb == "credits":
                add("S1" if g[a]["raw"] != g[b]["raw"] else "S7", a, b)
    for (a, b), c in w["cands"].items():
        same_person = g[a]["person"] == g[b]["person"]
        if c["tier"] == "auto" and same_person and not (g[a]["nconst"] and g[a]["nconst"] == g[b]["nconst"]):
            add("S2", a, b)
        if c["n"] == "N1" and c["ext"].get("a") and c["ext"].get("b") and c["ext"]["a"] != c["ext"]["b"]:
            add("S3", a, b)
        elif c["n"] == "N1" and not same_person and not c["veto"]:
            add("S4", a, b)
        elif c["n"] in ("N2", "N3"):
            add("S5", a, b)
        elif c["n"] == "N4":
            add("S6", a, b)
    return out


def select_pairs(cands: dict[str, list[tuple]], plan: dict[str, int]) -> tuple[list[tuple], dict[str, Any]]:
    """每层按 md5(pair_id) 取前 N；同一对只入选一次（按 S1…S8 的顺序占用）；不足按 S1、S4 的顺序补足，实际层数写入返回值。"""
    used: set[tuple[str, str]] = set()
    chosen: list[tuple] = []
    ranked = {s: sorted(rows, key=lambda r: (_md5(r[1]), r[1])) for s, rows in cands.items()}
    strata: dict[str, Any] = {}
    for s in sorted(plan):
        take = []
        for r in ranked.get(s, []):
            if len(take) == plan[s]:
                break
            if (r[2], r[3]) not in used:
                used.add((r[2], r[3]))
                take.append(r)
        chosen += take
        strata[s] = {"target": plan[s], "available": len(ranked.get(s, [])), "actual": len(take), "filled": 0}
    short = sum(v["target"] - v["actual"] for v in strata.values())
    for s in FILL_ORDER:
        for r in ranked.get(s, []):
            if short <= 0:
                break
            if (r[2], r[3]) not in used:
                used.add((r[2], r[3]))
                chosen.append(r)
                strata[s]["filled"] += 1
                short -= 1
    strata["_short_after_fill"] = max(short, 0)
    return chosen, strata


def _pair_route(w: dict[str, Any], a: str, b: str) -> tuple[bool, str | None]:
    """(系统是否判为同一人物, 合并途径)：预测只来自系统判定，涉及人工决策的组不算。"""
    g = w["groups"]
    if g[a]["person"] != g[b]["person"] or g[a]["human"] or g[b]["human"]:
        return False, None
    if g[a]["nconst"] and g[a]["nconst"] == g[b]["nconst"]:
        return True, "filmography" if "filmography" in (g[a]["route"], g[b]["route"]) else "credits"
    return True, "co_credit"


def _contexts(conn: psycopg.Connection, m: dict[str, Any], w: dict[str, Any], keys: list[str]) -> dict[str, dict[str, Any]]:
    """每个组的展示信息：商品标题、作品标题与年份、合作者、nconst 与地址、年表命中的作品。"""
    g = w["groups"]
    asins = sorted({g[k]["asin"] for k in keys})
    titles = dict(conn.execute(
        "SELECT entity_id, raw_value FROM task4.field_assertion WHERE extraction_id = %s AND parser_version = %s AND "
        "norm_version = %s AND field_name = 'title' AND ordinal = 1 AND entity_id = ANY(%s)",
        (m["extraction_id"], m["parser_version"], T4_NORM_VERSION, asins)).fetchall())
    movies = {r[0]: r[1] for r in conn.execute("SELECT movie_id, display_title FROM task4.movie WHERE mapping_version_id = %s",
                                               (m["t4_mapping_version_id"],))}
    nconst = dict(conn.execute("SELECT person_id, external_id FROM task5.person_external_id WHERE person_mapping_version_id = %s",
                               (m["person_mapping_version_id"],)).fetchall())
    films = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT c.mention_key, c.tconst, t.primary_title FROM task5.link_candidate c JOIN task5.link_decision d ON "
        "d.link_candidate_id = c.link_candidate_id AND d.rule_version = %s AND d.tier = 'auto' LEFT JOIN imdb.title t ON "
        "t.snapshot_id = %s AND t.tconst = c.tconst WHERE c.extraction_id = %s AND c.stage_key = %s AND c.route = 'filmography'",
        (m["rule_version"], m["imdb_snapshot_id"], m["extraction_id"], m["link_key"]))}
    out = {}
    for k in keys:
        x = g[k]
        mates = [g[o]["raw"] for o in sorted(w["units"][x["unit"]]) if o != k][:5]
        nc = nconst.get(x["person"])
        film = films.get(x["rep"])
        out[k] = {"raw": x["raw"], "role": x["role"], "asin": x["asin"], "product_title": titles.get(x["asin"], ""),
                  "work": movies.get(x["movie"], "") if x["movie"] else f"（合集 {x['unit'].split(':', 1)[1]}，未归属到作品）",
                  "year": x["year"], "collaborators": "|".join(mates), "nconst": nc or "",
                  "imdb_url": f"https://www.imdb.com/name/{nc}/" if nc else "",
                  "amazon_url": f"https://www.amazon.com/dp/{x['asin']}", "film": film}
    return out


def _ensure_eval_set(conn: psycopg.Connection, m: dict[str, Any]) -> str:
    sid = eval_set_id(m["person_mapping_version_id"])
    conn.execute("INSERT INTO task5.eval_set (eval_set_id, extraction_id, person_mapping_version_id, rule_version, strata) "
                 "VALUES (%s, %s, %s, %s, '{}') ON CONFLICT DO NOTHING",
                 (sid, m["extraction_id"], m["person_mapping_version_id"], m["rule_version"]))
    return sid


def export_pairs(conn: psycopg.Connection, pmap: str, directory: Path, *, plan: dict[str, int] | None = None,
                 force: bool = False) -> dict[str, Any]:
    """导出评价表（不含系统判定）和 AI 预标注草稿的空壳。同一人物映射版本只抽一次：评价集已存在时按库里的样本重新写出，且不覆盖已有的文件。"""
    plan = plan or DEFAULT_PLAN
    m = pmap_row(conn, pmap)
    dev = dev_family_keys(conn, m["extraction_id"])
    if not dev:
        raise EvalError("尚未冻结开发池，先运行 python -m etl_task5.evaluate freeze")
    dest = directory / "eval_pairs.csv"
    if dest.exists() and not force:
        raise EvalError(f"{dest} 已存在（可能已有标注）；不覆盖。确需重写请加 --force")
    set_id = _ensure_eval_set(conn, m)
    w = _load_world(conn, m)
    stored = conn.execute("SELECT pair_id, stratum, left_group, right_group FROM task5.eval_pair WHERE eval_set_id = %s "
                          "ORDER BY pair_id", (set_id,)).fetchall()
    if stored:
        chosen = [(s, pid, a, b) for pid, s, a, b in stored]
        strata = conn.execute("SELECT strata FROM task5.eval_set WHERE eval_set_id = %s", (set_id,)).fetchone()[0]
    else:
        cands = _candidate_pairs(w, lambda x: x["family"] not in dev)
        chosen, strata = select_pairs(cands, plan)
        with conn.transaction(), conn.cursor() as cur:
            rows = []
            for s, pid, a, b in chosen:
                pred, route = _pair_route(w, a, b)
                ga, gb = w["groups"][a], w["groups"][b]
                rows.append((set_id, pid, s, ga["rep"], gb["rep"], a, b, ga["person"], gb["person"], pred, route))
            cur.executemany("INSERT INTO task5.eval_pair (eval_set_id, pair_id, stratum, left_mention, right_mention, left_group, "
                            "right_group, left_person, right_person, predicted_same, route) VALUES (%s, %s, %s, %s, %s, %s, %s, "
                            "%s, %s, %s, %s)", rows)
            cur.execute("UPDATE task5.eval_set SET strata = %s WHERE eval_set_id = %s", (Jsonb(strata), set_id))
    ctx = _contexts(conn, m, w, sorted({k for _, _, a, b in chosen for k in (a, b)}))
    rows = []
    for s, pid, a, b in sorted(chosen, key=lambda r: (r[0], r[1])):
        la, rb = ctx[a], ctx[b]
        film = [(side, c["film"]) for side, c in (("左", la), ("右", rb)) if c["film"] and s == "S8"]
        rows.append({"pair_id": pid, "stratum": s, "left_mention": w["groups"][a]["rep"], "right_mention": w["groups"][b]["rep"],
                     **{f"left_{k}": v for k, v in la.items() if k != "film"}, **{f"right_{k}": v for k, v in rb.items() if k != "film"},
                     "filmography_work": "；".join(f"{side}：{t[0]} {t[1] or ''}" for side, t in film),
                     "filmography_url": "；".join(f"https://www.imdb.com/title/{t[0]}/" for _, t in film), "label": "", "note": ""})
    info = _write_csv(dest, PAIR_COLUMNS, rows)
    draft = _write_csv(directory / "eval_pairs_draft.csv", DRAFT_COLUMNS, rows)
    reporting.write_atomic(directory / "LABELING.md", LABELING.encode("utf-8"))
    conn.execute("UPDATE task5.eval_set SET exported_file = %s WHERE eval_set_id = %s", (str(dest), set_id))
    return {"eval_set_id": set_id, "pairs": len(rows), "strata": strata, "dev_family_keys": len(dev), "file": info,
            "draft_file": draft}


# ---------------------------------------------------------------- 标注导入（R36）

def import_labels(conn: psycopg.Connection, pmap: str, path: Path, annotator: str) -> dict[str, Any]:
    """写入已标注的行：pair 集合必须与导出时一致，空白跳过；同一对以后导入的为准。矛盾项只列出，不改标注。"""
    if not annotator.strip():
        raise EvalError("必须提供标注人 --annotator")
    set_id = eval_set_id(pmap)
    expected = {r[0] for r in conn.execute("SELECT pair_id FROM task5.eval_pair WHERE eval_set_id = %s", (set_id,))}
    if not expected:
        raise EvalError(f"评价集 {set_id} 不存在，先运行 evaluate export")
    rows, digest = _read_labeled(path)
    got = {r["pair_id"] for r in rows}
    if got != expected:
        raise EvalError(f"pair 集合与导出不一致：多出 {sorted(got - expected)[:5]}，缺少 {sorted(expected - got)[:5]}")
    labeled, blank, bad = [], 0, []
    for r in rows:
        label = (r.get("label") or "").strip().lower()
        if not label:
            blank += 1
        elif label not in LABELS:
            bad.append(f"{r['pair_id']}: {label!r}")
        else:
            labeled.append((set_id, r["pair_id"], label, annotator.strip(), (r.get("note") or "").strip() or None, digest))
    if bad:
        raise EvalError(f"label 必须是 {LABELS} 之一：{bad[:5]}")
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO task5.eval_label (eval_set_id, pair_id, label, annotator, note, import_file_sha256)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (eval_set_id, pair_id) DO UPDATE SET label = EXCLUDED.label, annotator = EXCLUDED.annotator,
                   note = EXCLUDED.note, labeled_at = clock_timestamp(), import_file_sha256 = EXCLUDED.import_file_sha256
               WHERE eval_label.import_file_sha256 <> EXCLUDED.import_file_sha256""", labeled)
    by = {r["pair_id"]: r for r in rows}
    contradictions = []
    for _, pid, label, *_ in labeled:
        r = by[pid]
        if label == "different" and r["left_nconst"] and r["left_nconst"] == r["right_nconst"]:
            contradictions.append({"pair_id": pid, "label": label, "why": "双方关联同一个 IMDb 人物"})
        elif label == "same" and r["left_nconst"] and r["right_nconst"] and r["left_nconst"] != r["right_nconst"]:
            contradictions.append({"pair_id": pid, "label": label, "why": "双方关联了不同的 IMDb 人物"})
    return {"eval_set_id": set_id, "file_sha256": digest, "rows": len(rows), "labeled": len(labeled), "unlabeled": blank,
            "unsure": sum(1 for x in labeled if x[2] == "unsure"), "contradictions": contradictions}


# ---------------------------------------------------------------- 指标（R37）

def metrics(conn: psycopg.Connection, pmap: str) -> dict[str, Any]:
    from .report import build_summary

    m = pmap_row(conn, pmap)
    set_id = eval_set_id(pmap)
    es = conn.execute("SELECT rule_version, strata FROM task5.eval_set WHERE eval_set_id = %s", (set_id,)).fetchone()
    if es is None:
        raise EvalError(f"评价集 {set_id} 不存在，先运行 evaluate export")
    cols = ("pair_id", "stratum", "left", "right", "predicted", "route", "label", "annotator", "note")
    items = [dict(zip(cols, r)) for r in conn.execute(
        "SELECT p.pair_id, p.stratum, p.left_mention, p.right_mention, p.predicted_same, p.route, l.label, l.annotator, l.note "
        "FROM task5.eval_pair p LEFT JOIN task5.eval_label l USING (eval_set_id, pair_id) WHERE p.eval_set_id = %s "
        "ORDER BY p.pair_id", (set_id,))]
    labeled = [i for i in items if i["label"]]
    decided = [i for i in labeled if i["label"] in ("same", "different")]

    def counts(sub: list[dict[str, Any]]) -> dict[str, Any]:
        tp = sum(1 for i in sub if i["predicted"] and i["label"] == "same")
        fp = sum(1 for i in sub if i["predicted"] and i["label"] == "different")
        fn = sum(1 for i in sub if not i["predicted"] and i["label"] == "same")
        tn = sum(1 for i in sub if not i["predicted"] and i["label"] == "different")
        return {**prf(tp, fp, fn), "tn": tn, "pairs": len(sub)}

    def route_of(i: dict[str, Any]) -> str:
        return i["route"] or ROUTE_OF_STRATUM.get(i["stratum"], "none")

    def brief(i: dict[str, Any]) -> dict[str, Any]:
        r = conn.execute("SELECT left_person, right_person FROM task5.eval_pair WHERE eval_set_id = %s AND pair_id = %s",
                         (set_id, i["pair_id"])).fetchone()
        return {"pair_id": i["pair_id"], "stratum": i["stratum"], "route": i["route"], "left_mention": i["left"],
                "right_mention": i["right"], "left_person": r[0], "right_person": r[1], "note": i["note"]}

    latest = latest_pmap(conn, m["extraction_id"])
    people = build_summary(conn, pmap)["people"]
    clusters = cluster_results(conn, set_id)
    return {
        "eval_set_id": set_id, "person_mapping_version_id": pmap, "rule_version": es[0], "pairs": len(items),
        "labeled": len(labeled), "unlabeled": len(items) - len(labeled),
        "unsure": sum(1 for i in labeled if i["label"] == "unsure"),
        "annotators": sorted({i["annotator"] for i in labeled}), "strata_plan": es[1],
        "overall": counts(decided),
        "by_stratum": {s: counts([i for i in decided if i["stratum"] == s]) for s in sorted({i["stratum"] for i in items})},
        "by_route": {r: counts([i for i in decided if route_of(i) == r]) for r in
                     sorted({route_of(i) for i in decided})},
        "false_merges": [brief(i) for i in decided if i["predicted"] and i["label"] == "different"],
        "missed_merges": [brief(i) for i in decided if not i["predicted"] and i["label"] == "same"],
        "review": {"review_pending_rate": people["review_pending_rate"], "persons_with_pending_candidates":
                   people["persons_with_pending_candidates"], "pending_candidate_rate": people["pending_candidate_rate"],
                   "review_candidates": people["review_candidates"], "human_reviewed_candidates":
                   people["human_reviewed_candidates"], "review_coverage": people["human_review_coverage"]},
        "clusters": clusters,
        "later_version_note": (f"导出之后又出现了人物映射版本 {latest}；本指标仍是导出时的版本 {pmap} 的结果" if latest != pmap else None),
        "rule_note": ("评价集已被看过，之后若再改规则，指标不再独立" if es[0] != m["rule_version"] else None),
        "scope_note": "指标只代表评价样本（分层抽样刻意多取难例），不代表全量准确率；预测同一只指系统判定（不含人工决策）；"
                      "“不确定”不计入指标"}


def render_metrics(out: dict[str, Any]) -> str:
    pct = lambda x: "无定义" if x is None else f"{x * 100:.1f}%"  # noqa: E731
    row = lambda name, b: (f"| {name} | {b['tp']} | {b['fp']} | {b['fn']} | {b['tn']} | {pct(b['precision'])} | "  # noqa: E731
                           f"{pct(b['recall'])} | {pct(b['f1'])} |")
    head = ["| 范围 | TP | FP | FN | TN | Precision | Recall | F1 |", "|---|---|---|---|---|---|---|---|"]
    lines = ["# 任务 5 评价结果", "",
             f"- 评价集 `{out['eval_set_id']}`，人物映射版本 `{out['person_mapping_version_id']}`，规则 {out['rule_version']}",
             f"- 标注人：{', '.join(out['annotators']) or '（尚无标注）'}；已标注 {out['labeled']} / {out['pairs']} 对，"
             f"不确定 {out['unsure']}，未标注 {out['unlabeled']}", f"- 范围：{out['scope_note']}"]
    lines += [f"- 注意：{out[k]}" for k in ("later_version_note", "rule_note") if out[k]]
    lines += ["", "## 自动合并的 Precision / Recall / F1", ""] + head + [row("合计", out["overall"])]
    lines += [row(s, b) for s, b in out["by_stratum"].items()]
    lines += ["", "按合并途径（未被系统合并的漏合并按其所在层的预期途径计）：", ""] + head + [row(r, b) for r, b in out["by_route"].items()]
    lines += ["", "分母为 0 的指标记为“无定义”，不记为 0 或 1。", "", "## 误合并实例（系统判同一人、人工判不同）", ""]
    lines += [f"- `{i['pair_id']}`（{i['stratum']}，{i['route']}）{i['left_mention']} / {i['right_mention']}" for i in out["false_merges"]] or ["- 无"]
    lines += ["", "## 漏合并实例（系统未合并、人工判同一人）", ""]
    lines += [f"- `{i['pair_id']}`（{i['stratum']}）{i['left_mention']} / {i['right_mention']}" for i in out["missed_merges"]] or ["- 无"]
    r = out["review"]
    lines += ["", "## 人工复核", "", f"- 待复核率（review_pending 人物）{pct(r['review_pending_rate'])}；有待复核候选的人物占比 {pct(r['pending_candidate_rate'])}",
              f"- 人工复核覆盖率 {pct(r['review_coverage'])}（{r['human_reviewed_candidates']} / {r['review_candidates']}）"]
    c = out["clusters"]
    lines += ["", "## 簇抽查", "", f"- 抽查 {c['sampled']} 个人物，已标注 {c['labeled']} 个，混入不同的人的人物 {c['mixed']} 个"]
    lines += [f"  - `{x['person_id']}`：{x['problem_mentions']} {x['note'] or ''}" for x in c["mixed_persons"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 簇抽查（R38）

def export_clusters(conn: psycopg.Connection, pmap: str, directory: Path, *, plan: dict[str, int] | None = None,
                    force: bool = False) -> dict[str, Any]:
    """从评价池抽 20 个跨至少两个作品单位的人物（≥3 部 10 个、2 部 10 个，不足互补；合作者佐证产生的人物优先至少 5 个）。"""
    plan = plan or CLUSTER_PLAN
    m = pmap_row(conn, pmap)
    dev = dev_family_keys(conn, m["extraction_id"])
    if not dev:
        raise EvalError("尚未冻结开发池，先运行 python -m etl_task5.evaluate freeze")
    dest = directory / "cluster_sample.csv"
    if dest.exists() and not force:
        raise EvalError(f"{dest} 已存在（可能已有标注）；不覆盖。确需重写请加 --force")
    set_id = _ensure_eval_set(conn, m)
    w = _load_world(conn, m)
    per: dict[str, dict[str, Any]] = {}
    for x in w["groups"].values():
        p = per.setdefault(x["person"], {"units": set(), "dev": False, "iter": False})
        p["units"].add(x["unit"])
        p["dev"] |= x["family"] in dev
    for pid in {r[0] for r in conn.execute("SELECT DISTINCT person_id FROM task5.mention_person WHERE "
                                           "person_mapping_version_id = %s AND basis = 'co_credit'", (pmap,))}:
        per[pid]["iter"] = True
    pool = sorted(((_md5(pid), pid, len(p["units"]), p["iter"]) for pid, p in per.items() if len(p["units"]) >= 2 and not p["dev"]))
    bucket = lambda n: "3+" if n >= 3 else "2"  # noqa: E731
    stored = [r[0] for r in conn.execute("SELECT person_id FROM task5.cluster_sample WHERE eval_set_id = %s ORDER BY person_id",
                                         (set_id,))]
    if stored:
        picked = [x for x in pool if x[1] in set(stored)]
    else:
        picked, quota = [], dict(plan)
        for x in [y for y in pool if y[3]][:ITERATION_MIN]:               # 迭代产生的人物优先
            picked.append(x)
            quota[bucket(x[2])] = max(quota[bucket(x[2])] - 1, 0)
        for b in ("3+", "2"):
            picked += [x for x in pool if bucket(x[2]) == b and x not in picked][:quota[b]]
        total = sum(plan.values())
        picked += [x for x in pool if x not in picked][:max(total - len(picked), 0)]
        picked = picked[:total]
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany("INSERT INTO task5.cluster_sample (eval_set_id, person_id, work_count, from_iteration) "
                            "VALUES (%s, %s, %s, %s)", [(set_id, x[1], x[2], x[3]) for x in picked])
    ids = [x[1] for x in sorted(picked, key=lambda r: (-r[2], r[0]))]
    rows = []
    for n, pid in enumerate(ids, start=1):
        keys = [r for r in conn.execute(
            "SELECT m.mention_key, m.raw_value, m.role_class, m.asin, mp.basis, mp.round, mp.group_key FROM task5.mention_person mp "
            "JOIN task5.person_mention m ON m.extraction_id = mp.extraction_id AND m.parser_version = mp.parser_version AND "
            "m.norm_version = mp.norm_version AND m.mention_key = mp.mention_key WHERE mp.person_mapping_version_id = %s AND "
            "mp.person_id = %s ORDER BY m.mention_key COLLATE \"C\"", (pmap, pid))]
        ctx = _contexts(conn, m, w, sorted({r[6] for r in keys}))
        for mk, raw, role, asin, basis, rnd, gk in keys:
            c = ctx[gk]
            rows.append({"cluster_no": n, "person_id": pid, "mention_key": mk, "raw": raw, "role": role, "asin": asin,
                         "work_title": c["work"], "work_year": c["year"], "basis": basis, "round": rnd or "",
                         "nconst": c["nconst"], "imdb_url": c["imdb_url"], "amazon_url": c["amazon_url"], "cluster_label": "",
                         "outlier": "", "note": ""})
    info = _write_csv(dest, CLUSTER_COLUMNS, rows)
    reporting.write_atomic(directory / "LABELING.md", LABELING.encode("utf-8"))
    return {"eval_set_id": set_id, "persons": len(ids), "from_iteration": sum(1 for x in picked if x[3]),
            "by_bucket": {b: sum(1 for x in picked if bucket(x[2]) == b) for b in ("3+", "2")}, "file": info}


def import_clusters(conn: psycopg.Connection, pmap: str, path: Path, annotator: str) -> dict[str, Any]:
    if not annotator.strip():
        raise EvalError("必须提供标注人 --annotator")
    set_id = eval_set_id(pmap)
    expected = {r[0] for r in conn.execute("SELECT person_id FROM task5.cluster_sample WHERE eval_set_id = %s", (set_id,))}
    if not expected:
        raise EvalError(f"簇抽查样本 {set_id} 不存在，先运行 evaluate clusters-export")
    rows, digest = _read_labeled(path)
    by: dict[str, dict[str, Any]] = defaultdict(lambda: {"labels": set(), "outliers": [], "notes": []})
    for r in rows:
        b = by[r["person_id"]]
        lab = (r.get("cluster_label") or "").strip().lower()
        if lab:
            b["labels"].add(lab)
        if (r.get("outlier") or "").strip():
            b["outliers"].append(r["mention_key"])
        if (r.get("note") or "").strip():
            b["notes"].append(r["note"].strip())
    if set(by) != expected:
        raise EvalError(f"人物集合与导出不一致：多出 {sorted(set(by) - expected)[:5]}，缺少 {sorted(expected - set(by))[:5]}")
    out, bad = [], []
    for pid, b in by.items():
        if not b["labels"]:
            continue
        if len(b["labels"]) > 1 or not b["labels"] <= {"same_person", "mixed"}:
            bad.append(f"{pid}: {sorted(b['labels'])}")
        elif "mixed" in b["labels"] and not b["outliers"]:
            bad.append(f"{pid}: mixed 须在混入的提及所在行的 outlier 列填 x")
        else:
            out.append((set_id, pid, "mixed" in b["labels"], b["outliers"] if "mixed" in b["labels"] else [],
                        annotator.strip(), "；".join(b["notes"]) or None, digest))
    if bad:
        raise EvalError(f"cluster_label 必须每个人物一致，取值 same_person 或 mixed：{bad[:5]}")
    with conn.transaction(), conn.cursor() as cur:
        cur.executemany(
            """INSERT INTO task5.cluster_label (eval_set_id, person_id, mixed, problem_mentions, annotator, note, import_file_sha256)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (eval_set_id, person_id) DO UPDATE SET mixed = EXCLUDED.mixed,
                   problem_mentions = EXCLUDED.problem_mentions, annotator = EXCLUDED.annotator, note = EXCLUDED.note,
                   labeled_at = clock_timestamp(), import_file_sha256 = EXCLUDED.import_file_sha256""", out)
    return {"eval_set_id": set_id, "file_sha256": digest, "persons": len(expected), "labeled": len(out),
            "mixed": sum(1 for x in out if x[2])}


def cluster_results(conn: psycopg.Connection, set_id: str) -> dict[str, Any]:
    sampled = conn.execute("SELECT count(*), count(*) FILTER (WHERE from_iteration) FROM task5.cluster_sample WHERE eval_set_id = %s",
                           (set_id,)).fetchone()
    rows = conn.execute("SELECT person_id, mixed, problem_mentions, note, annotator FROM task5.cluster_label WHERE eval_set_id = %s "
                        "ORDER BY person_id", (set_id,)).fetchall()
    return {"sampled": sampled[0], "from_iteration": sampled[1], "labeled": len(rows), "mixed": sum(1 for r in rows if r[1]),
            "annotators": sorted({r[4] for r in rows}),
            "mixed_persons": [{"person_id": r[0], "problem_mentions": list(r[2]), "note": r[3]} for r in rows if r[1]]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 5 评价工具")
    ap.add_argument("cmd", choices=("freeze", "export", "import", "metrics", "clusters-export", "clusters-import"))
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--extraction-id")
    ap.add_argument("--pmap")
    ap.add_argument("--annotator", default="")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的导出文件（会丢失其中的标注）")
    args = ap.parse_args(argv)
    s = load_settings()
    try:
        with connect(s) as conn:
            eid = args.extraction_id or latest_full_extraction(conn)
            v = args.pmap or latest_pmap(conn, eid)
            m = pmap_row(conn, v)
            directory = eval_dir(s.output_dir, eid, v)
            if args.cmd == "freeze":
                out: dict[str, Any] = freeze_dev_pool(conn, eid, parser_version=m["parser_version"], norm_version=m["norm_version"])
            elif args.cmd == "export":
                out = export_pairs(conn, v, directory, force=args.force)
            elif args.cmd == "clusters-export":
                out = export_clusters(conn, v, directory, force=args.force)
            elif args.cmd in ("import", "clusters-import"):
                if not args.csv:
                    ap.error(f"{args.cmd} 需要 csv 路径")
                fn = import_labels if args.cmd == "import" else import_clusters
                out = fn(conn, v, Path(args.csv), args.annotator)
            else:
                out = metrics(conn, v)
                reporting.write_atomic(directory / "metrics.json",
                                       (json.dumps(out, ensure_ascii=False, indent=2, default=str) + "\n").encode())
                reporting.write_atomic(directory / "metrics.md", render_metrics(out).encode())
    except EvalError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
