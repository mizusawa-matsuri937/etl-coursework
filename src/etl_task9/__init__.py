"""任务 9：字段级数据血缘。在不重算任务 1–8 的前提下，把各任务的定位、候选与选择翻译成统一的
“源记录 → 字段断言 → 派生边 → 字段选择”四层结构（schema task9，迁移 801）。"""

RULE_VERSION = "t9r1"                    # 血缘翻译、定位串与判定口径（rules.py）
CURRENT_RULE_VERSION = "t9r1"            # 正式运行与 DAG 参数的默认规则版本
DEFAULT_EXTRACTION = "f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8"   # spec R01 的全量抽取
DEFAULT_T3_RULE = "t3r2"                 # 任务 3 分类规则版本
DEFAULT_PARSER = "t3p1"                  # 任务 3 解析版本
DEFAULT_T4_MAPPING_VERSION = "map0002"   # 任务 4 映射版本
DEFAULT_T4_NORM = "t4n1"                 # 任务 4 规范化版本
DEFAULT_PMAP = "pmap0001"                # 任务 5 人物映射版本
DEFAULT_T6_RELEASE_VERSION = "rv0001"    # 任务 6 上映版本
DEFAULT_CONFLICT_VERSION = "cv0003"      # 任务 7 冲突消解版本
DEFAULT_DEDUP_VERSION = "rd0001"         # 任务 8 去重版本
DEFAULT_IMDB_SNAPSHOT = "imdb_2026-10-29"
