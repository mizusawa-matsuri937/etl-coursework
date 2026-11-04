"""任务 9 的规则版本与纯函数（spec R06–R09、R11、R12、R17、R18，规则版本 t9r1）。不访问数据库。

- ID：source_record_id = 'sr_' + sha256(定位串)[:16]（R08）；assertion_id = 'as_' + sha256(上游表 '|' 上游键)[:16]
  （R12）；selection_id = 'fs_' + sha256(实体类型|实体|字段|成员)[:16]（design 数据模型）。同一定位、同一上游行在
  各阶段键与血缘版本中 ID 不变。构建在 SQL 里批量生成 ID，这里同时给出 SQL 片段，单元与集成测试核对两者一致。
- 定位串（locator_key）：页面 `page|抽取|ASIN|正文哈希`；IMDb `imdb|快照|文件|行号`；评论 `review|抽取|源评论 ID`；
  人工修订 `manual|决策表|决策 ID`。
- 获取时间的来源列（R07）与哈希口径（R06）按源记录种类固定。
- 操作类型闭集与 OpenLineage 列级血缘标注（R17、R18），供派生边使用（T9-02）。
- 页面快照核验（R09）：解压后正文的 SHA-256 与登记值比对，缺失为 snapshot_missing，不一致或 gzip 损坏为
  snapshot_hash_mismatch（与任务 3 读快照的判定相同）。
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

from . import CURRENT_RULE_VERSION, RULE_VERSION

# 各阶段翻译算法的标记：进入阶段签名；改算法须同时登记新的规则版本（同任务 8 的 ALGORITHM）。
# assertions 的 a1 → a2（T9-02 追加集成层翻译器）与 derivations 的 d1 → d2（演练发现 starring 成员沿用 cast 角色下的
# 署名对齐，修正成员的页面边）、selections 的 l1 → l2（演练发现人物页面别名是署名原文，改为按原文或规范名匹配提及；l2 → l3：IMDb 独有人物缺人名时记 missing）都发生在 t9r1 产生任何血缘版本之前，只改阶段键、不换规则版本
# （design“T9-02 实现要点与对计划的修正”）；t9r1 发布（生成 lv0001）之后再改算法必须换规则版本。
ALGORITHM = {"sources": "s1", "assertions": "a2", "derivations": "d2", "selections": "l3", "impact": "i1"}
STAGES = ("sources", "assertions", "derivations", "selections", "impact")
STAGE_PREFIX = {"sources": "s", "assertions": "a", "derivations": "d", "selections": "l", "impact": "i"}

SOURCE_KINDS = ("page_snapshot", "imdb_row", "review_record", "manual_revision")
HASH_SCOPE = {"page_snapshot": "page_body_sha256", "imdb_row": "imdb_file_sha256",
              "review_record": "review_record_sha256", "manual_revision": "import_file_sha256"}
ACQUIRED_BASIS = {"page_snapshot": "task2.product_manifest.fetched_at",          # R07
                  "imdb_row": "imdb.snapshot.downloaded_at",
                  "review_record": "public.source_file.download_completed_at",
                  "manual_revision": "human_decision.decided_at"}
EVIDENCE_STATUSES = ("ok", "snapshot_missing", "snapshot_hash_mismatch", "not_checked")

# IMDb 表 → 原始文件名（imdb.source_file.file_name）；title_director 来自 title.crew 的 directors 串（同一行多位导演）
IMDB_FILES = {"imdb.title": "title.basics.tsv.gz", "imdb.title_aka": "title.akas.tsv.gz",
              "imdb.title_director": "title.crew.tsv.gz", "imdb.title_principal": "title.principals.tsv.gz",
              "imdb.name": "name.basics.tsv.gz"}

ASSERTION_LEVELS = ("extract", "candidate", "derived")
ENTITY_TYPES = ("movie", "edition", "collection_member", "person", "mention", "product_movie", "review",
                "imdb_title", "imdb_name")
TASK4_ENTITY = {"product_edition": "edition", "collection_member": "collection_member"}

# R17 操作类型闭集 → R18 OpenLineage 列级血缘（类型, 子类型）
OPERATIONS = {
    "extract": ("DIRECT", "IDENTITY"),
    "normalize": ("DIRECT", "TRANSFORMATION"),
    "vocab_map": ("DIRECT", "TRANSFORMATION"),
    "split": ("DIRECT", "TRANSFORMATION"),
    "align": ("INDIRECT", "JOIN"),
    "link": ("INDIRECT", "JOIN"),
    "merge": ("INDIRECT", "JOIN"),
    "select": ("INDIRECT", "CONDITIONAL"),
    "union": ("DIRECT", "IDENTITY"),
    "infer": ("DIRECT", "TRANSFORMATION"),
    "aggregate": ("DIRECT", "AGGREGATION"),
    "relocate": ("DIRECT", "IDENTITY"),
}
SELECTION_STATUSES = ("resolved", "conflict_unresolved", "missing", "inferred")       # R24
TRACE_STATUSES = ("traced", "relocated", "manual", "partial", "evidence_unverified", "untraced")   # R25
TRACEABLE = ("traced", "relocated", "manual")


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def source_record_id(locator_key: str) -> str:
    """R08：'sr_' || substr(sha256(locator_key), 1, 16)。"""
    return "sr_" + _sha16(locator_key)


def assertion_id(upstream_table: str, upstream_key: str) -> str:
    """R12：'as_' || substr(sha256(上游表 || '|' || 上游键), 1, 16)。"""
    return "as_" + _sha16(f"{upstream_table}|{upstream_key}")


def selection_id(entity_type: str, entity_id: str, field_name: str, member_key: str = "") -> str:
    return "fs_" + _sha16("|".join((entity_type, entity_id, field_name, member_key)))


def sql_sr_id(locator_key_expr: str) -> str:
    """与 source_record_id 相同的 SQL 表达式（参数是产生定位串的 SQL 表达式）。"""
    return f"('sr_' || substr(encode(sha256(convert_to({locator_key_expr}, 'UTF8')), 'hex'), 1, 16))"


def sql_as_id(table_expr: str, key_expr: str) -> str:
    """与 assertion_id 相同的 SQL 表达式。"""
    return f"('as_' || substr(encode(sha256(convert_to({table_expr} || '|' || {key_expr}, 'UTF8')), 'hex'), 1, 16))"


def page_locator_key(extraction_id: str, asin: str, content_hash: str) -> str:
    return f"page|{extraction_id}|{asin}|{content_hash}"


def imdb_locator_key(snapshot_id: str, file_name: str, line_no: int) -> str:
    return f"imdb|{snapshot_id}|{file_name}|{line_no}"


def review_locator_key(extraction_id: str, source_review_id: str) -> str:
    return f"review|{extraction_id}|{source_review_id}"


def manual_locator_key(table: str, decision_id: int | str) -> str:
    return f"manual|{table}|{decision_id}"


def stage_signature(stage: str, version: str = RULE_VERSION) -> str:
    """阶段规则签名：阶段算法标记与规则版本的摘要（同任务 8 的 RuleSet.signature 写法）。"""
    if stage not in ALGORITHM:
        raise ValueError(f"未知的阶段 {stage!r}")
    raw = json.dumps({"algorithm": ALGORITHM[stage], "rule_version": version}, sort_keys=True)
    return f"{STAGE_PREFIX[stage]}{hashlib.sha256(raw.encode()).hexdigest()[:12]}"


def ol_of(operation: str) -> tuple[str, str]:
    """R18：操作类型 → (OpenLineage 类型, 子类型)；不在闭集内即报错（R17）。"""
    try:
        return OPERATIONS[operation]
    except KeyError:
        raise ValueError(f"操作类型 {operation!r} 不在闭集 {tuple(OPERATIONS)} 内") from None


def resolve_snapshot(path: str, root: str | Path) -> Path:
    """快照路径：绝对路径原样，相对路径相对项目根目录（任务 2 记录的口径，容器内为 /opt/etl）。"""
    p = Path(path)
    return p if p.is_absolute() else Path(root) / p


def snapshot_status(path: Path, expected: str | None, chunk: int = 1 << 20) -> tuple[str, dict[str, Any]]:
    """R09：解压后正文的 SHA-256 与登记值比对，返回 (ok | snapshot_missing | snapshot_hash_mismatch, 细节)。
    流式解压，不把整页读进内存；gzip 损坏按哈希不一致处理（内容不能信，与任务 3 的 read_page 相同）。"""
    h = hashlib.sha256()
    try:
        with gzip.open(path, "rb") as f:
            while True:
                block = f.read(chunk)
                if not block:
                    break
                h.update(block)
    except FileNotFoundError:
        return "snapshot_missing", {"path": str(path)}
    except (OSError, EOFError) as exc:
        return "snapshot_hash_mismatch", {"path": str(path), "expected": expected, "error": str(exc)[:200]}
    actual = h.hexdigest()
    if actual != expected:
        return "snapshot_hash_mismatch", {"path": str(path), "expected": expected, "actual": actual}
    return "ok", {}


assert CURRENT_RULE_VERSION == RULE_VERSION
