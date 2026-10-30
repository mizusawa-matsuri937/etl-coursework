"""任务 6：补全上映信息。为任务 4 映射版本的作品补全首次公开发行年份、商品发售日期、最早评论时间与推断上界。"""

RULE_VERSION = "t6r1"        # 择优顺序、置信度、共现评分、评论计票门槛、B3 天数与上界置信度（rules.py）
DEFAULT_T4_MAPPING_VERSION = "map0002"   # 默认输入：任务 4 映射版本（spec R01）
DEFAULT_PMAP = "pmap0001"               # 默认输入：任务 5 人物映射版本（只读其升级线索）
T4_NORM_VERSION = "t4n1"                # 读任务 4 断言与 IMDb 标题键时用的标准化版本
MENTION_PATTERN_VERSION = "t6m1"        # 评论年份提及的模式集合（review_years.py）
COOCCUR_PARAM_VERSION = "t6c1"          # 共现评分的分数、标题与编号条件（rules.py）
CURRENT_RULE_VERSION = "t6r1"            # 正式运行与 DAG 参数的默认规则版本（见 rules.RULESETS）
