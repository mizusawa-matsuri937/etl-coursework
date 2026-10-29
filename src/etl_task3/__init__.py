"""任务 3：电影页面筛选。从任务 2 的快照抽取原始字段，按有版本的规则给每个产品分类（不发网络请求）。"""

PARSER_VERSION = "t3p1"      # 证据区域与抽取方式（extract.py）
LABEL_MAP_VERSION = "t3m1"   # 标签 → 字段键映射（extract.LABEL_MAP）
RULE_VERSION = "t3r2"        # 分类规则、模式与阈值（rules.py）；t3r1 → t3r2 见 design“规则版本 t3r2”
