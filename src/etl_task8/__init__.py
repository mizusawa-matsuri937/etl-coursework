"""任务 8：评论清洗与重复识别。为任务 1 的全部原始评论记录判定异常、识别评论家族与重复，形成逻辑评论与 ReviewMapping。"""

RULE_VERSION = "t8r1"                    # 阈值、比较键、核验与选择规则（rules.py）
CURRENT_RULE_VERSION = "t8r1"            # 正式运行与 DAG 参数的默认规则版本（见 rules.RULESETS）
DEFAULT_EXTRACTION = "f1e8b845d8a2294867227193e55f4d3c9ac51bafb24daf0bb6873f19e370d6d8"   # spec R01 的全量抽取
DEFAULT_T1_RULES = "task1-rules-1"       # 该抽取的任务 1 规则版本（public.extraction.rules_version）
DEFAULT_T3_RULE = "t3r2"                 # 默认输入：任务 3 分类规则版本
DEFAULT_CONFLICT_VERSION = "cv0003"      # 默认输入：任务 7 冲突消解版本
