# 任务 3 合成夹具

`pages.py` 生成仿照 amazon.com 经典模板的最小产品页（非真实商品），供 spec AC03、AC04 及集成测试使用：

- 每页都有一段含 “DVD Blu-ray Prime Video Movies & TV VHS” 的导航、一段含 Television、Season、Double Feature、
  Director、Run time 的商品描述，以及 `<head>` 里一段 `productType = 'PHYSICAL_TV_SERIES'` 脚本。这些都不在
  证据区域内，抽取与分类不得受其影响。
- `EXTRACT_PAGE` / `EXTRACT_EXPECTED`：AC03 的页面与逐项预期字段。
- `CASES`：AC04 的 18 个页面（后 3 个是规则 t3r2 修复的已知问题）与预期类别、证据级别、`rule_id`、待确认原因。预期按 spec 手写，不由实现生成。

集成测试在临时目录现场写 gzip 快照，并在 `etl_test` 里用 SQL 模拟任务 2 的爬取结果（`tests/integration/_task3_helpers.py`），
不提交任何真实页面。
