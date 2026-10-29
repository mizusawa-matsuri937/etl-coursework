"""任务 4 评价工具（spec R32–R35，AC13、AC15）：开发池冻结、评价表与簇抽查表导出、标注导入、指标。

流程（命令都在容器内、工作目录 /opt/etl 运行；AI 不标注，标注由用户完成）：
    python -m etl_task4.evaluate freeze                       # 冻结开发池：先于任何调参，只做一次
    python -m etl_task4.evaluate export                       # 导出 120 对评价表（规则版本此时必须已冻结）
    python -m etl_task4.evaluate import <eval_pairs.csv> --annotator <姓名>
    python -m etl_task4.evaluate clusters-export              # 导出 20 个多商品作品簇的抽查表
    python -m etl_task4.evaluate clusters-import <cluster_sample.csv> --annotator <姓名>
    python -m etl_task4.evaluate metrics                      # Precision、Recall、F1、实例、复核覆盖率与簇抽查结果

开发池按 md5(标题键) 排序的前 20% 标题键，只用于调阈值；评价样本只从其余标题键抽取。导出的评价表不含系统判定（避免锚定），
系统判定存在库里的 eval_pair。指标只代表评价样本（分层抽样刻意多取难例），不代表全量准确率。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import heapq
import io
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

from .check import latest_mapping_version
from .report import _briefs, mapping_row, output_root

DEV_FRACTION = 5                 # 前 1/5 = 20% 的标题键属于开发池
DEFAULT_PLAN = {"S1": 30, "S2": 5, "S3": 20, "S4": 25, "S5": 15, "S6": 15, "S7": 10}   # 共 120 对
FILL_ORDER = ("S1", "S4")        # 某层不足时按此顺序补足
SIMILARITY = 0.6                 # S5：标题键字符三元组的 Jaccard 相似度下限
COMMON_GRAM = 250                # 出现在超过这么多标题键里的三元组不用来生成 S5 候选（太常见，没有区分度）
CLUSTER_PLAN = {"3+": 10, "2": 10}
LABELS = ("same", "different", "unsure")
PAIR_COLUMNS = ("pair_id", "stratum", "left_entity", "right_entity", "left_title", "right_title", "context",
                "left_directors", "right_directors", "left_actors", "right_actors", "left_runtime", "right_runtime",
                "left_title_year", "right_title_year", "left_tconst", "right_tconst", "left_imdb_url",
                "right_imdb_url", "left_amazon_url", "right_amazon_url", "label", "note")
CLUSTER_COLUMNS = ("cluster_no", "movie_id", "asin", "title", "directors", "actors", "runtime_min", "title_year",
                   "tconst", "imdb_url", "amazon_url", "cluster_label", "outlier", "note")
LABELING = """# 标注口径

评价表 `eval_pairs.csv`：每行一对商品（或“合集成员 + 商品”），在 `label` 列填 `same`、`different` 或 `unsure`，需要时写 `note`。

- **same（同一部创作作品）**：两边是同一部作品的不同版本。DVD、Blu-ray、VHS、不同语言或字幕、地区码与制式、包装、发行商与再发行，
  导演剪辑版、加长版、未分级版、修复版、重制版、上色版、周年版、不同长度版本，都算同一部。
- **different（不同作品）**：翻拍、续集、同名不同作品（例如 1960 年与 1983 年的 Breathless，动画版与真人版的 Cinderella），
  以及完全不同的作品。
- **unsure**：页面与 IMDb 的证据都无法判断时标 `unsure` 并写 `note`；不计入指标，单独报告数量。
- 合集成员对（`left_entity` 形如 `ASIN#序号`）：问的是“这个成员是不是右边这个商品所代表的作品”，不是问整个合集。
- 依据：以页面（`*_amazon_url`、标题、导演、演员、片长）和 IMDb（`*_imdb_url`）为准，不要凭记忆；本表不显示系统的判定。
- 只有一位标注人，没有第二标注人；分歧处理方式是：证据不足就标 `unsure`。导入时会自动检查明显矛盾（例如双方关联同一部 IMDb 作品、
  导演一致，却标成 `different`），矛盾项只列出供复看，不会改动你的标注。

簇抽查表 `cluster_sample.csv`：每个多商品作品簇若干行（每行一个商品）。逐簇在该簇任一行的 `cluster_label` 填 `same_work`
（全部是同一部作品）或 `mixed`（混入了不同作品）；`mixed` 时在混入的商品所在行的 `outlier` 列填 `x`。
"""


class EvalError(RuntimeError):
    """评价流程的前置条件不满足或输入文件不合格（确定性错误）。"""


def eval_dir(output_dir: Path, eid: str, mapping_version_id: str) -> Path:
    return output_root(output_dir) / eid / mapping_version_id / "eval"


def eval_set_id(mapping_version_id: str) -> str:
    return f"ev_{mapping_version_id}"


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


# ---------------------------------------------------------------- 开发池（R32）

def _entity_keys(conn: psycopg.Connection, m: dict[str, Any]) -> dict[str, str]:
    """参与匹配的商品与合集成员 → 标题键（空键不参与）。"""
    return {e: k for e, k in conn.execute(
        """SELECT a.entity_id, a.normalized_value FROM task4.field_assertion a
           WHERE a.extraction_id = %s AND a.parser_version = %s AND a.norm_version = %s AND a.field_name = 'title'
             AND a.normalized_value <> '' AND (a.entity_type = 'collection_member' OR EXISTS (
                 SELECT 1 FROM task4.product_edition e WHERE e.extraction_id = a.extraction_id AND e.asin = a.entity_id
                   AND e.t3_rule_version = %s AND e.match_scope = 'matched'))""",
        (m["extraction_id"], m["parser_version"], m["norm_version"], m["t3_rule_version"])).fetchall()}


def freeze_dev_pool(conn: psycopg.Connection, extraction_id: str, *, t3_rule_version: str, parser_version: str,
                    norm_version: str) -> dict[str, Any]:
    """冻结开发池：全部参与匹配的标题键按 md5 排序，前 20%（向上取整）属于开发池。已冻结时什么都不做、返回已有的。"""
    have = conn.execute("SELECT count(*), min(norm_version), min(frozen_at) FROM task4.dev_pool WHERE extraction_id = %s",
                        (extraction_id,)).fetchone()
    if have[0]:
        keys = conn.execute("SELECT count(DISTINCT title_key) FROM task4.dev_pool WHERE extraction_id = %s",
                            (extraction_id,)).fetchone()[0]
        return {"status": "already_frozen", "entities": have[0], "dev_title_keys": keys, "norm_version": have[1],
                "frozen_at": have[2]}
    m = {"extraction_id": extraction_id, "t3_rule_version": t3_rule_version, "parser_version": parser_version,
         "norm_version": norm_version}
    keys = _entity_keys(conn, m)
    distinct = sorted({k for k in keys.values()}, key=lambda k: (_md5(k), k))
    n_dev = -(-len(distinct) // DEV_FRACTION)
    dev = set(distinct[:n_dev])
    rows = [(extraction_id, e, k, norm_version) for e, k in sorted(keys.items()) if k in dev]
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO task4.dev_pool (extraction_id, asin, title_key, norm_version) "
                            "VALUES (%s, %s, %s, %s)", rows)
    return {"status": "frozen", "entities": len(rows), "dev_title_keys": len(dev), "title_keys": len(distinct),
            "norm_version": norm_version}


def dev_title_keys(conn: psycopg.Connection, extraction_id: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT DISTINCT title_key FROM task4.dev_pool WHERE extraction_id = %s",
                                       (extraction_id,)).fetchall()}


# ---------------------------------------------------------------- 评价样本（R33）

def _trigrams(key: str) -> frozenset[str]:
    padded = f"  {key} "
    return frozenset(padded[i:i + 3] for i in range(len(padded) - 2))


def _similar_key_pairs(keys: dict[str, list[str]], limit: int) -> list[tuple[str, str]]:
    """标题键不同、三元组 Jaccard ≥ 阈值的键对（仅保留按 md5 排序最靠前的 limit 个）。倒排索引 + 跳过过于常见的三元组。"""
    grams = {k: _trigrams(k) for k in keys}
    index: dict[str, list[str]] = defaultdict(list)
    for k in sorted(grams):
        for g in grams[k]:
            index[g].append(k)
    best: list[tuple[str, str, str]] = []
    for k in sorted(grams):
        shared: dict[str, int] = defaultdict(int)
        for g in grams[k]:
            posting = index[g]
            if len(posting) > COMMON_GRAM:
                continue
            for other in posting:
                if other > k:
                    shared[other] += 1
        for other, n in shared.items():
            if n / (len(grams[k]) + len(grams[other]) - n) >= SIMILARITY:
                best.append((_md5(f"S5:{k}:{other}"), k, other))
        if len(best) > 20 * limit:
            best = heapq.nsmallest(5 * limit, best)
    return [(a, b) for _, a, b in heapq.nsmallest(5 * limit, best)]


def _pair_id(stratum: str, left: str, right: str) -> str:
    return "p_" + _md5(f"{stratum}:{left}:{right}")[:12]


def _load_world(conn: psycopg.Connection, m: dict[str, Any]) -> dict[str, Any]:
    v, eid, nv, sid, rv = (m[k] for k in ("mapping_version_id", "extraction_id", "norm_version", "imdb_snapshot_id",
                                          "rule_version"))
    keys = _entity_keys(conn, m)
    movie_of = {a: (mid, basis) for a, mid, basis in conn.execute(
        "SELECT asin, movie_id, basis FROM task4.product_movie WHERE mapping_version_id = %s AND "
        "relation_type = 'edition'", (v,)).fetchall()}
    contains = {(a, o): mid for a, o, mid in conn.execute(
        "SELECT asin, member_ordinal, movie_id FROM task4.product_movie WHERE mapping_version_id = %s AND "
        "relation_type = 'contains'", (v,)).fetchall()}
    movies = {mid: (status, tconst, cnt) for mid, status, tconst, cnt in conn.execute(
        "SELECT movie_id, status, tconst, edition_count FROM task4.movie WHERE mapping_version_id = %s", (v,)).fetchall()}
    imdb: dict[str, list[tuple]] = defaultdict(list)           # 实体 → [(tconst, generator, tier, decision_id)]
    for left, tconst, gen, tier, did in conn.execute(
            """SELECT c.left_entity, c.right_id, c.generator, d.tier, d.decision_id FROM task4.candidate c
               JOIN task4.merge_decision d USING (candidate_id)
               WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND d.rule_version = %s
                 AND c.right_kind = 'imdb'""", (eid, nv, sid, rv)).fetchall():
        imdb[left].append((tconst, gen, tier, did))
    g3 = {(a, b): (tier, reason, did) for a, b, tier, reason, did in conn.execute(
        """SELECT c.left_entity, c.right_id, d.tier, d.reason, d.decision_id FROM task4.candidate c
           JOIN task4.merge_decision d USING (candidate_id)
           WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND d.rule_version = %s
             AND c.right_kind = 'product'""", (eid, nv, sid, rv)).fetchall()}
    return {"keys": keys, "movie_of": movie_of, "contains": contains, "movies": movies, "imdb": imdb, "g3": g3}


def _candidate_pairs(w: dict[str, Any], pool_ok) -> dict[str, list[tuple]]:
    """各层的候选对：(pair_id, 左, 右, 预测同一, 系统判定档)。已按层内 md5(pair_id) 排序，同一对只出现在编号最小的层里。"""
    keys, movie_of, g3 = w["keys"], w["movie_of"], w["g3"]
    members: dict[str, list[str]] = defaultdict(list)
    for a, (mid, _) in movie_of.items():
        members[mid].append(a)
    out: dict[str, list[tuple]] = {s: [] for s in DEFAULT_PLAN}
    seen: set[frozenset[str]] = set()

    def add(stratum: str, left: str, right: str, predicted: bool, tier: str) -> None:
        pair = frozenset((left, right))
        if pair in seen or not (pool_ok(left) and pool_ok(right)):
            return
        seen.add(pair)
        out[stratum].append((_pair_id(stratum, left, right), left, right, predicted, tier))

    def system_same(a: str, b: str) -> bool:
        return movie_of[a][0] == movie_of[b][0] and "human" not in (movie_of[a][1], movie_of[b][1])

    def tier_of(a: str, b: str) -> str:
        x = g3.get((a, b)) or g3.get((b, a))
        return x[0] if x else ("auto" if movie_of[a][0] == movie_of[b][0] else "none")

    clusters = sorted(((mid, sorted(asins)) for mid, asins in members.items() if len(asins) > 1))
    for want_status, stratum in (("imdb_linked", "S1"), ("page_only", "S2")):
        for mid, asins in clusters:
            if w["movies"][mid][0] != want_status:
                continue
            for i, a in enumerate(asins):
                for b in asins[i + 1:]:
                    add(stratum, a, b, system_same(a, b), tier_of(a, b))
    for (a, b), (tier, _reason, _did) in sorted(g3.items()):
        if a in movie_of and b in movie_of and tier == "review":
            add("S3", a, b, system_same(a, b), "review")
    for (a, b), (tier, _reason, _did) in sorted(g3.items()):
        if a in movie_of and b in movie_of and tier != "review" and movie_of[a][0] != movie_of[b][0]:
            add("S4", a, b, system_same(a, b), tier)
    # S5：标题键不同但相似，各取每个键下 ASIN 最小的商品
    by_key: dict[str, list[str]] = defaultdict(list)
    for a in sorted(movie_of):
        if a in keys and pool_ok(a):
            by_key[keys[a]].append(a)
    for ka, kb in _similar_key_pairs(by_key, DEFAULT_PLAN["S5"]):
        a, b = by_key[ka][0], by_key[kb][0]
        add("S5", a, b, system_same(a, b), tier_of(a, b))
    # S6：合集成员对标题键相同的电影类商品
    products_by_key: dict[str, list[str]] = defaultdict(list)
    for a in sorted(movie_of):
        if a in keys:
            products_by_key[keys[a]].append(a)
    for ent in sorted(e for e in keys if "#" in e):
        base, ordinal = ent.split("#")
        for a in products_by_key.get(keys[ent], [])[:1]:
            cm = w["contains"].get((base, int(ordinal)))
            tiers = [t for _, _, t, _ in w["imdb"].get(ent, [])]
            add("S6", ent, a, cm is not None and cm == movie_of[a][0],
                "auto" if cm is not None else ("review" if "review" in tiers else ("reject" if tiers else "none")))
    # S7：一方只命中另一方 IMDb 作品的别名
    primary_hits: dict[str, list[str]] = defaultdict(list)            # tconst → 主标题命中的商品
    alias_hits: dict[str, list[str]] = defaultdict(list)
    for ent, hits in w["imdb"].items():
        if ent in movie_of:
            for tconst, gen, _tier, _did in hits:
                (primary_hits if gen == "G1" else alias_hits)[tconst].append(ent)
    for tconst in sorted(alias_hits):
        for a in sorted(alias_hits[tconst]):
            for b in sorted(primary_hits.get(tconst, [])):
                if a != b and keys.get(a) != keys.get(b):
                    add("S7", a, b, system_same(a, b), tier_of(a, b))
    for s in out:
        out[s].sort(key=lambda r: (_md5(r[0]), r[0]))
    return out


def select_pairs(cands: dict[str, list[tuple]], plan: dict[str, int]) -> tuple[list[tuple], dict[str, Any]]:
    """每层取前 N；不足的按 S1、S4 的顺序补足，实际层数记入评价集（design“评价设计”）。"""
    chosen: list[tuple] = []
    strata: dict[str, Any] = {}
    leftovers: dict[str, list[tuple]] = {}
    for s, n in plan.items():
        got = cands[s][:n]
        chosen += [(s, *r) for r in got]
        leftovers[s] = cands[s][n:]
        strata[s] = {"target": n, "available": len(cands[s]), "selected": len(got), "filled": 0}
    deficit = sum(n for n in plan.values()) - len(chosen)
    for s in FILL_ORDER:
        take = leftovers.get(s, [])[:max(deficit, 0)]
        chosen += [(s, *r) for r in take]
        strata[s]["filled"] = len(take)
        deficit -= len(take)
    strata["_total"] = {"target": sum(plan.values()), "selected": len(chosen), "unfilled": max(deficit, 0)}
    return chosen, strata


def _pair_context(conn: psycopg.Connection, m: dict[str, Any], pairs: list[tuple]) -> dict[str, dict[str, Any]]:
    """每个实体（商品或合集成员）的展示信息。"""
    ents = sorted({e for p in pairs for e in (p[2], p[3])})
    bases = sorted({e.split("#")[0] for e in ents})
    briefs = _briefs(conn, m, sorted(set(ents) | set(bases)))
    links: dict[str, str] = {}
    for left, tconst in conn.execute(
            """SELECT c.left_entity, c.right_id FROM task4.candidate c JOIN task4.merge_decision d USING (candidate_id)
               WHERE c.extraction_id = %s AND c.norm_version = %s AND c.imdb_snapshot_id = %s AND d.rule_version = %s
                 AND c.right_kind = 'imdb' AND d.tier = 'auto' AND c.left_entity = ANY(%s)""",
            (m["extraction_id"], m["norm_version"], m["imdb_snapshot_id"], m["rule_version"], ents)).fetchall():
        links[left] = tconst
    out = {}
    for e in ents:
        base = e.split("#")[0]
        b = briefs[e]
        host = briefs[base]
        out[e] = {"title": b["title"], "directors": " | ".join(host["directors"]), "actors": " | ".join(host["actors"]),
                  "runtime": host["runtime_min"] if "#" not in e else None,
                  "title_year": b["title_year"], "tconst": links.get(e),
                  "imdb_url": f"https://www.imdb.com/title/{links[e]}/" if e in links else "",
                  "amazon_url": f"https://www.amazon.com/dp/{base}", "host_title": host["title"] if "#" in e else None}
    return out


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> dict[str, Any]:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    reporting.write_atomic(path, buf.getvalue().encode("utf-8"))
    return {**reporting.file_digest(path), "file": str(path), "rows": len(rows)}


def _ensure_eval_set(conn: psycopg.Connection, m: dict[str, Any]) -> str:
    eid = eval_set_id(m["mapping_version_id"])
    conn.execute("INSERT INTO task4.eval_set (eval_set_id, extraction_id, mapping_version_id, rule_version, strata) "
                 "VALUES (%s, %s, %s, %s, '{}') ON CONFLICT DO NOTHING",
                 (eid, m["extraction_id"], m["mapping_version_id"], m["rule_version"]))
    return eid


def export_pairs(conn: psycopg.Connection, mapping_version_id: str, directory: Path, *,
                 plan: dict[str, int] | None = None, force: bool = False) -> dict[str, Any]:
    """导出评价表（不含系统判定）。同一映射版本只抽一次：评价集已存在时按库里的样本重新写出，且不覆盖已有的文件。"""
    plan = plan or DEFAULT_PLAN
    m = mapping_row(conn, mapping_version_id)
    dev = dev_title_keys(conn, m["extraction_id"])
    if not dev:
        raise EvalError("尚未冻结开发池，先运行 python -m etl_task4.evaluate freeze")
    dest = directory / "eval_pairs.csv"
    if dest.exists() and not force:
        raise EvalError(f"{dest} 已存在（可能已有标注）；不覆盖。确需重写请加 --force")
    set_id = _ensure_eval_set(conn, m)
    stored = conn.execute("SELECT pair_id, stratum, left_entity, right_entity FROM task4.eval_pair WHERE eval_set_id = %s "
                          "ORDER BY pair_id", (set_id,)).fetchall()
    if stored:
        chosen = [(s, pid, a, b, None, None) for pid, s, a, b in stored]
        strata = conn.execute("SELECT strata FROM task4.eval_set WHERE eval_set_id = %s", (set_id,)).fetchone()[0]
    else:
        w = _load_world(conn, m)
        cands = _candidate_pairs(w, lambda e: w["keys"].get(e) is not None and w["keys"][e] not in dev)
        chosen, strata = select_pairs(cands, plan)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO task4.eval_pair (eval_set_id, pair_id, stratum, left_entity, right_entity, "
                    "left_movie_id, right_movie_id, predicted_same, system_tier) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [(set_id, pid, s, a, b,
                      (w["contains"].get((a.split("#")[0], int(a.split("#")[1]))) if "#" in a else w["movie_of"][a][0]),
                      w["movie_of"][b][0], pred, tier) for s, pid, a, b, pred, tier in chosen])
            conn.execute("UPDATE task4.eval_set SET strata = %s WHERE eval_set_id = %s", (Jsonb(strata), set_id))
    ctx = _pair_context(conn, m, chosen)
    rows = []
    for s, pid, a, b, *_ in sorted(chosen, key=lambda r: (r[0], r[1])):
        la, rb = ctx[a], ctx[b]
        rows.append({"pair_id": pid, "stratum": s, "left_entity": a, "right_entity": b,
                     "left_title": la["title"], "right_title": rb["title"],
                     "context": f"合集商品 {a.split('#')[0]}：{la['host_title']}" if "#" in a else "",
                     "left_directors": la["directors"], "right_directors": rb["directors"],
                     "left_actors": la["actors"], "right_actors": rb["actors"],
                     "left_runtime": la["runtime"], "right_runtime": rb["runtime"],
                     "left_title_year": la["title_year"], "right_title_year": rb["title_year"],
                     "left_tconst": la["tconst"], "right_tconst": rb["tconst"],
                     "left_imdb_url": la["imdb_url"], "right_imdb_url": rb["imdb_url"],
                     "left_amazon_url": la["amazon_url"], "right_amazon_url": rb["amazon_url"], "label": "", "note": ""})
    info = _write_csv(dest, PAIR_COLUMNS, rows)
    reporting.write_atomic(directory / "LABELING.md", LABELING.encode("utf-8"))
    conn.execute("UPDATE task4.eval_set SET exported_file = %s WHERE eval_set_id = %s", (str(dest), set_id))
    return {"eval_set_id": set_id, "pairs": len(rows), "by_stratum": {k: v for k, v in strata.items()},
            "dev_title_keys": len(dev), "file": info}


# ---------------------------------------------------------------- 标注导入（R33）

def _read_labeled(path: Path) -> tuple[list[dict[str, str]], str]:
    raw = Path(path).read_bytes()
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))), hashlib.sha256(raw).hexdigest()


def import_labels(conn: psycopg.Connection, mapping_version_id: str, path: Path, annotator: str) -> dict[str, Any]:
    """写入已标注的行：pair 集合必须与导出时一致，空白跳过；同一对以后导入的为准。矛盾项只列出，不改标注。"""
    if not annotator.strip():
        raise EvalError("必须提供标注人 --annotator")
    set_id = eval_set_id(mapping_version_id)
    expected = {r[0]: r[1:] for r in conn.execute(
        "SELECT pair_id, left_entity, right_entity, stratum FROM task4.eval_pair WHERE eval_set_id = %s",
        (set_id,)).fetchall()}
    if not expected:
        raise EvalError(f"评价集 {set_id} 不存在，先运行 evaluate export")
    rows, digest = _read_labeled(path)
    got = {r["pair_id"] for r in rows}
    if got != set(expected):
        raise EvalError(f"pair 集合与导出不一致：多出 {sorted(got - set(expected))[:5]}，缺少 {sorted(set(expected) - got)[:5]}")
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
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO task4.eval_label (eval_set_id, pair_id, label, annotator, note, import_file_sha256)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (eval_set_id, pair_id) DO UPDATE SET label = EXCLUDED.label, annotator = EXCLUDED.annotator,
                       note = EXCLUDED.note, labeled_at = clock_timestamp(), import_file_sha256 = EXCLUDED.import_file_sha256
                   WHERE eval_label.import_file_sha256 <> EXCLUDED.import_file_sha256""", labeled)
    by = {r["pair_id"]: r for r in rows}
    contradictions = []
    for _, pid, label, *_ in labeled:
        r = by[pid]
        same_t = r["left_tconst"] and r["left_tconst"] == r["right_tconst"]
        diff_t = r["left_tconst"] and r["right_tconst"] and r["left_tconst"] != r["right_tconst"]
        shared_dir = bool({x.strip().lower() for x in r["left_directors"].split("|") if x.strip()} &
                          {x.strip().lower() for x in r["right_directors"].split("|") if x.strip()})
        if label == "different" and same_t and shared_dir:
            contradictions.append({"pair_id": pid, "label": label, "why": "双方关联同一部 IMDb 作品且导演一致"})
        elif label == "same" and diff_t:
            contradictions.append({"pair_id": pid, "label": label, "why": "双方关联了不同的 IMDb 作品"})
    return {"eval_set_id": set_id, "file_sha256": digest, "rows": len(rows), "labeled": len(labeled),
            "unlabeled": blank, "unsure": sum(1 for x in labeled if x[2] == "unsure"), "contradictions": contradictions}


# ---------------------------------------------------------------- 指标（R34）

def prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    """Precision = TP/(TP+FP)，Recall = TP/(TP+FN)，F1 = 2PR/(P+R)；任一分母为 0 时记 None（报告写“无定义”）。"""
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = 2 * p * r / (p + r) if p is not None and r is not None and p + r else None
    undefined = [k for k, v in (("precision", p), ("recall", r), ("f1", f)) if v is None]
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f,
            "undefined": undefined, "note": "无定义（分母为 0）：" + "、".join(undefined) if undefined else None}


def metrics(conn: psycopg.Connection, mapping_version_id: str) -> dict[str, Any]:
    m = mapping_row(conn, mapping_version_id)
    set_id = eval_set_id(mapping_version_id)
    es = conn.execute("SELECT rule_version, strata, exported_file, created_at FROM task4.eval_set WHERE eval_set_id = %s",
                      (set_id,)).fetchone()
    if es is None:
        raise EvalError(f"评价集 {set_id} 不存在，先运行 evaluate export")
    rows = conn.execute(
        """SELECT p.pair_id, p.stratum, p.left_entity, p.right_entity, p.predicted_same, p.system_tier, l.label,
                  l.annotator, l.note FROM task4.eval_pair p LEFT JOIN task4.eval_label l USING (eval_set_id, pair_id)
           WHERE p.eval_set_id = %s ORDER BY p.pair_id""", (set_id,)).fetchall()
    cols = ("pair_id", "stratum", "left", "right", "predicted", "tier", "label", "annotator", "note")
    items = [dict(zip(cols, r)) for r in rows]
    labeled = [i for i in items if i["label"]]
    decided = [i for i in labeled if i["label"] in ("same", "different")]

    def counts(sub: list[dict[str, Any]]) -> dict[str, Any]:
        tp = sum(1 for i in sub if i["predicted"] and i["label"] == "same")
        fp = sum(1 for i in sub if i["predicted"] and i["label"] == "different")
        fn = sum(1 for i in sub if not i["predicted"] and i["label"] == "same")
        tn = sum(1 for i in sub if not i["predicted"] and i["label"] == "different")
        return {**prf(tp, fp, fn), "tn": tn, "pairs": len(sub)}

    overall = counts(decided)
    by_stratum = {s: {**counts([i for i in decided if i["stratum"] == s]),
                      "unsure": sum(1 for i in labeled if i["stratum"] == s and i["label"] == "unsure"),
                      "unlabeled": sum(1 for i in items if i["stratum"] == s and not i["label"])}
                  for s in sorted({i["stratum"] for i in items})}
    briefs = _briefs(conn, m, sorted({e.split("#")[0] for i in items for e in (i["left"], i["right"])}))

    def instance(i: dict[str, Any]) -> dict[str, Any]:
        d = {"pair_id": i["pair_id"], "stratum": i["stratum"], "system_tier": i["tier"], "note": i["note"]}
        for side in ("left", "right"):
            b = briefs[i[side].split("#")[0]]
            d[side] = {"entity": i[side], "title": b["title"], "directors": b["directors"][:2],
                       "title_year": b["title_year"], "runtime_min": b["runtime_min"], "url": b["final_url"],
                       "snapshot_path": b["snapshot_path"]}
        return d

    false_merges = [instance(i) for i in decided if i["predicted"] and i["label"] == "different"]
    missed = [instance(i) for i in decided if not i["predicted"] and i["label"] == "same"]
    # 人工复核覆盖率与待复核率（单独报告）
    pending = int(m["counts"]["review_pending_products"])
    movies = int(m["counts"]["participating_products"])
    reviewed = int(conn.execute(
        """SELECT count(*) FROM task4.product_match_status s WHERE s.mapping_version_id = %s AND s.status = 'review_pending'
             AND EXISTS (SELECT 1 FROM task4.human_decision h WHERE h.extraction_id = %s AND
                         (h.left_entity = s.asin OR h.right_entity = s.asin))""",
        (mapping_version_id, m["extraction_id"])).fetchone()[0])
    review = {"review_pending_products": pending, "participating_movie_products": movies,
              "review_pending_rate": pending / movies if movies else None,
              "review_items_with_human_decision": reviewed,
              "review_coverage": reviewed / pending if pending else None,
              "note": "复核覆盖率 = 有人工决策的待复核商品数 / 待复核商品数；待复核率 = 待复核商品数 / 参与匹配的电影类商品数"}
    clusters = cluster_results(conn, set_id)
    annotators = sorted({i["annotator"] for i in labeled} | set(clusters["annotators"]))
    rule_note = None
    if es[0] != m["rule_version"]:
        rule_note = f"评价集导出时的规则版本是 {es[0]}，当前映射版本使用 {m['rule_version']}；指标按导出时的预测计算"
    latest = latest_mapping_version(conn, m["extraction_id"])
    if latest != mapping_version_id:
        rule_note = (rule_note + "；" if rule_note else "") + f"此后又生成了映射版本 {latest}，评价集已被看过，不再独立"
    return {
        "eval_set_id": set_id, "mapping_version_id": mapping_version_id, "rule_version": es[0],
        "pairs": len(items), "labeled": len(labeled), "unlabeled": len(items) - len(labeled),
        "unsure": sum(1 for i in labeled if i["label"] == "unsure"), "annotators": annotators,
        "overall": overall, "by_stratum": by_stratum, "strata_design": es[1],
        "false_merges": false_merges, "missed_merges": missed, "review": review, "clusters": clusters,
        "scope_note": ("指标只代表这个评价样本（分层抽样，难例比例高于全量，开发池标题键已排除），"
                       "不能代表全量准确率；“不确定”不计入指标，单独计数。"),
        "rule_note": rule_note,
    }


def render_metrics(out: dict[str, Any]) -> str:
    pct = lambda x: "无定义" if x is None else f"{x * 100:.1f}%"  # noqa: E731
    o = out["overall"]
    lines = ["# 任务 4 评价结果", "",
             f"- 评价集 `{out['eval_set_id']}`，映射版本 `{out['mapping_version_id']}`，规则 {out['rule_version']}",
             f"- 标注人：{', '.join(out['annotators']) or '（尚无标注）'}；已标注 {out['labeled']} / {out['pairs']} 对，"
             f"不确定 {out['unsure']}，未标注 {out['unlabeled']}",
             f"- 范围：{out['scope_note']}"]
    if out["rule_note"]:
        lines.append(f"- 注意：{out['rule_note']}")
    lines += ["", "## 自动合并的 Precision / Recall / F1", "",
              "| 范围 | TP | FP | FN | TN | Precision | Recall | F1 |", "|---|---|---|---|---|---|---|---|",
              f"| 合计 | {o['tp']} | {o['fp']} | {o['fn']} | {o['tn']} | {pct(o['precision'])} | {pct(o['recall'])} | {pct(o['f1'])} |"]
    for s, b in out["by_stratum"].items():
        lines.append(f"| {s} | {b['tp']} | {b['fp']} | {b['fn']} | {b['tn']} | {pct(b['precision'])} | {pct(b['recall'])} | {pct(b['f1'])} |")
    lines += ["", "分母为 0 的指标记为“无定义”，不记为 0 或 1。", "", "## 误合并实例（系统判同一、人工判不同）", ""]
    lines += [f"- `{i['pair_id']}` {i['left']['title']} / {i['right']['title']}（{i['left']['entity']} / {i['right']['entity']}）"
              for i in out["false_merges"]] or ["- 无"]
    lines += ["", "## 漏合并实例（系统未合并、人工判同一）", ""]
    lines += [f"- `{i['pair_id']}` {i['left']['title']} / {i['right']['title']}（系统判定 {i['system_tier']}）"
              for i in out["missed_merges"]] or ["- 无"]
    r = out["review"]
    lines += ["", "## 人工复核", "",
              f"- 待复核商品 {r['review_pending_products']} 个，待复核率 {pct(r['review_pending_rate'])}",
              f"- 有人工决策的待复核商品 {r['review_items_with_human_decision']} 个，复核覆盖率 {pct(r['review_coverage'])}"]
    c = out["clusters"]
    lines += ["", "## 簇抽查", "", f"- 抽查 {c['sampled']} 个多商品作品簇，已标注 {c['labeled']} 个，混入不同作品的簇 {c['mixed']} 个"]
    lines += [f"  - `{x['movie_id']}`：{x['problem_asins']} {x['note'] or ''}" for x in c["mixed_clusters"]]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 簇抽查（R35）

def export_clusters(conn: psycopg.Connection, mapping_version_id: str, directory: Path, *,
                    plan: dict[str, int] | None = None, force: bool = False) -> dict[str, Any]:
    plan = plan or CLUSTER_PLAN
    m = mapping_row(conn, mapping_version_id)
    dev = dev_title_keys(conn, m["extraction_id"])
    if not dev:
        raise EvalError("尚未冻结开发池，先运行 python -m etl_task4.evaluate freeze")
    dest = directory / "cluster_sample.csv"
    if dest.exists() and not force:
        raise EvalError(f"{dest} 已存在（可能已有标注）；不覆盖。确需重写请加 --force")
    set_id = _ensure_eval_set(conn, m)
    keys = _entity_keys(conn, m)
    members: dict[str, list[str]] = defaultdict(list)
    for mid, a in conn.execute(
            "SELECT pm.movie_id, pm.asin FROM task4.product_movie pm JOIN task4.movie mv ON mv.mapping_version_id = "
            "pm.mapping_version_id AND mv.movie_id = pm.movie_id WHERE pm.mapping_version_id = %s AND "
            "pm.relation_type = 'edition' AND mv.edition_count > 1 ORDER BY pm.movie_id, pm.asin COLLATE \"C\"",
            (mapping_version_id,)).fetchall():
        members[mid].append(a)
    pool = {mid: asins for mid, asins in members.items() if all(keys.get(a) and keys[a] not in dev for a in asins)}
    big = sorted((mid for mid, a in pool.items() if len(a) >= 3), key=lambda x: (_md5(x), x))
    small = sorted((mid for mid, a in pool.items() if len(a) == 2), key=lambda x: (_md5(x), x))
    take_big, take_small = big[:plan["3+"]], small[:plan["2"]]
    lack_big, lack_small = plan["3+"] - len(take_big), plan["2"] - len(take_small)      # 一类不足时由另一类补
    take_small += small[len(take_small):len(take_small) + max(lack_big, 0)]
    take_big += big[len(take_big):len(take_big) + max(lack_small, 0)]
    short = plan["3+"] + plan["2"] - len(take_big) - len(take_small)
    chosen = sorted(take_big + take_small, key=lambda x: (_md5(x), x))
    stored = conn.execute("SELECT movie_id FROM task4.cluster_sample WHERE eval_set_id = %s ORDER BY movie_id",
                          (set_id,)).fetchall()
    if stored:
        chosen = [r[0] for r in stored]
    else:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.executemany("INSERT INTO task4.cluster_sample (eval_set_id, movie_id, asin_count) VALUES (%s, %s, %s)",
                                [(set_id, mid, len(members[mid])) for mid in chosen])
    asins = sorted({a for mid in chosen for a in members[mid]})
    briefs = _briefs(conn, m, asins)
    tconst = dict(conn.execute("SELECT movie_id, tconst FROM task4.movie WHERE mapping_version_id = %s AND movie_id = ANY(%s)",
                               (mapping_version_id, chosen)).fetchall())
    rows = []
    for n, mid in enumerate(chosen, start=1):
        for a in members[mid]:
            b = briefs[a]
            rows.append({"cluster_no": n, "movie_id": mid, "asin": a, "title": b["title"],
                         "directors": " | ".join(b["directors"]), "actors": " | ".join(b["actors"]),
                         "runtime_min": b["runtime_min"], "title_year": b["title_year"], "tconst": tconst.get(mid),
                         "imdb_url": f"https://www.imdb.com/title/{tconst[mid]}/" if tconst.get(mid) else "",
                         "amazon_url": f"https://www.amazon.com/dp/{a}", "cluster_label": "", "outlier": "", "note": ""})
    info = _write_csv(dest, CLUSTER_COLUMNS, rows)
    reporting.write_atomic(directory / "LABELING.md", LABELING.encode("utf-8"))
    return {"eval_set_id": set_id, "clusters": len(chosen), "products": len(rows),
            "big_clusters": sum(1 for x in chosen if len(members[x]) >= 3),
            "pairs_clusters": sum(1 for x in chosen if len(members[x]) == 2), "short_of_plan": short,
            "eligible": {"3+": len(big), "2": len(small)}, "file": info}


def import_clusters(conn: psycopg.Connection, mapping_version_id: str, path: Path, annotator: str) -> dict[str, Any]:
    if not annotator.strip():
        raise EvalError("必须提供标注人 --annotator")
    set_id = eval_set_id(mapping_version_id)
    expected = {r[0]: r[1] for r in conn.execute("SELECT movie_id, asin_count FROM task4.cluster_sample WHERE "
                                                 "eval_set_id = %s", (set_id,)).fetchall()}
    if not expected:
        raise EvalError(f"评价集 {set_id} 没有簇抽查样本，先运行 evaluate clusters-export")
    rows, digest = _read_labeled(path)
    by: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by[r["movie_id"]].append(r)
    if set(by) != set(expected):
        raise EvalError(f"簇集合与导出不一致：多出 {sorted(set(by) - set(expected))[:5]}，缺少 {sorted(set(expected) - set(by))[:5]}")
    labeled, blank = [], 0
    for mid, rs in by.items():
        marks = {(r.get("cluster_label") or "").strip().lower() for r in rs} - {""}
        if not marks:
            blank += 1
            continue
        if len(marks) > 1 or not marks <= {"same_work", "mixed"}:
            raise EvalError(f"簇 {mid} 的 cluster_label 必须是单一的 same_work 或 mixed，实际 {sorted(marks)}")
        mixed = marks == {"mixed"}
        outliers = sorted(r["asin"] for r in rs if (r.get("outlier") or "").strip())
        if not mixed and outliers:
            raise EvalError(f"簇 {mid} 标了 same_work，却有 outlier 行 {outliers}")
        notes = " ".join(sorted({(r.get("note") or "").strip() for r in rs} - {""})) or None
        labeled.append((set_id, mid, mixed, outliers, annotator.strip(), notes, digest))
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO task4.cluster_label (eval_set_id, movie_id, mixed, problem_asins, annotator, note,
                       import_file_sha256) VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (eval_set_id, movie_id) DO UPDATE SET mixed = EXCLUDED.mixed,
                       problem_asins = EXCLUDED.problem_asins, annotator = EXCLUDED.annotator, note = EXCLUDED.note,
                       labeled_at = clock_timestamp(), import_file_sha256 = EXCLUDED.import_file_sha256
                   WHERE cluster_label.import_file_sha256 <> EXCLUDED.import_file_sha256""", labeled)
    return {"eval_set_id": set_id, "clusters": len(expected), "labeled": len(labeled), "unlabeled": blank,
            "mixed": sum(1 for x in labeled if x[2]), "file_sha256": digest}


def cluster_results(conn: psycopg.Connection, set_id: str) -> dict[str, Any]:
    sampled = conn.execute("SELECT count(*) FROM task4.cluster_sample WHERE eval_set_id = %s", (set_id,)).fetchone()[0]
    rows = conn.execute("SELECT movie_id, mixed, problem_asins, annotator, note FROM task4.cluster_label WHERE "
                        "eval_set_id = %s ORDER BY movie_id", (set_id,)).fetchall()
    mixed = [{"movie_id": r[0], "problem_asins": r[2], "note": r[4]} for r in rows if r[1]]
    return {"sampled": int(sampled), "labeled": len(rows), "mixed": len(mixed), "mixed_clusters": mixed,
            "annotators": sorted({r[3] for r in rows}),
            "note": "簇抽查用来发现传递合并造成的混入，不计入 Precision（R35）"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务 4 评价工具")
    ap.add_argument("cmd", choices=("freeze", "export", "import", "metrics", "clusters-export", "clusters-import"))
    ap.add_argument("csv", nargs="?")
    ap.add_argument("--extraction-id")
    ap.add_argument("--mapping-version")
    ap.add_argument("--annotator", default="")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的导出文件（会丢失其中的标注）")
    args = ap.parse_args(argv)
    s = load_settings()
    try:
        with connect(s) as conn:
            eid = args.extraction_id or latest_full_extraction(conn)
            v = args.mapping_version or latest_mapping_version(conn, eid)
            m = mapping_row(conn, v)
            directory = eval_dir(s.output_dir, eid, v)
            if args.cmd == "freeze":
                out: dict[str, Any] = freeze_dev_pool(conn, eid, t3_rule_version=m["t3_rule_version"],
                                                      parser_version=m["parser_version"], norm_version=m["norm_version"])
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
