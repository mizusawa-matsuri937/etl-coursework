"""任务 5：人物姓名归一与消歧。为任务 4 作品的页面人物生成稳定的 person_id，确认 IMDb 外部人物 ID，建立 MoviePerson。"""

NORM_VERSION = "t5n1"        # 姓名拆分与比较键（names.py）
RULE_VERSION = "t5r1"        # 外部 ID 与跨作品判定的等级、得分、阈值与开关（rules.py）

DEFAULT_T4_MAPPING_VERSION = "map0002"   # 默认输入：任务 4 映射版本（spec R01）
T4_NORM_VERSION = "t4n1"                 # 读任务 4 断言与 IMDb 标题键时用的标准化版本
CURRENT_RULE_VERSION = "t5r1"            # 正式运行与 DAG 参数的默认规则版本（见 rules.RULESETS）
