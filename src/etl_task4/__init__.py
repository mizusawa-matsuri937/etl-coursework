"""任务 4：定义并识别同一电影。把任务 3 的电影类商品与 IMDb 公开数据匹配，生成稳定的 movie_id 与商品–作品映射。"""

NORM_VERSION = "t4n1"        # 标题键、说明词表、姓名键、合集拆分（normalize.py）
RULE_VERSION = "t4r1"        # 候选、特征权重、阈值与判定（match.py）
IMDB_LOADER_VERSION = "t4i1"  # IMDb 过滤与装载规则（imdb.py）

DEFAULT_T3_RULE_VERSION = "t3r2"   # 默认输入：任务 3 规则版本（spec R01）
CURRENT_RULE_VERSION = "t4r2"      # 正式运行与 DAG 参数的默认规则版本（t4r1 保留，作为开发池调整前的对照；见 match.RULESETS）
