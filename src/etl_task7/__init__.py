"""任务 7：处理不同来源的信息冲突。为任务 4 映射版本的作品与商品版本建立可复核的多源、多版本字段冲突消解流程。"""

RULE_VERSION = "t7r1"                    # 阈值、置信度、词表与决策顺序（rules.py）
RULE_VERSION_RELAXED = "t7r2"            # 姓名兼容放宽（预规范化 + 最低等级 LS，见 rules.name_level）；t7r1 保留，cv0001 可复现
CURRENT_RULE_VERSION = "t7r2"            # 正式运行与 DAG 参数的默认规则版本（见 rules.RULESETS）
DEFAULT_T4_MAPPING_VERSION = "map0002"   # 默认输入：任务 4 映射版本（spec R01）
DEFAULT_PMAP = "pmap0001"                # 默认输入：任务 5 人物映射版本
DEFAULT_T6_RELEASE_VERSION = "rv0001"    # 默认输入：任务 6 上映版本
DEFAULT_IMDB_SNAPSHOT = "imdb_2026-10-29"
T4_NORM_VERSION = "t4n1"                 # 读任务 4 断言与 IMDb 标题键时用的标准化版本
