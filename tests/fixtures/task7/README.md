# 任务 7 合成夹具

全部为手写的合成数据（非真实商品与作品），预期值按 spec 的 AC 表手算，不由实现生成。与任务 6 不同，任务 7 的夹具**不运行上游流水线**：`world.py` 只定义规格类（`Imdb`、`Movie`、`Release`、`Ed`、`World`），测试在自己的模块里组装案例，`tests/integration/_task7_helpers.seed` 把案例直接写成任务 3–6 与 IMDb 的上游表行（抽取与清单、`task3.page_field`、`imdb.*` 与 `task4.imdb_title_key`、`task4` 映射与断言、`task5` 人物映射、`task6` 上映版本）。每个测试开始时 `reset` 清空 `task7` 与上游表，案例之间互不影响。

`Seeded.inputs()` 给出任务 7 的默认输入：抽取 `sha256("task7-fixture")`、映射版本 `map0002`、人物映射版本 `pmap0001`、上映版本 `rv0001`、IMDb 快照 `imdb_2026-10-29`。`World(decoys=True)` 另外写入其他映射版本 `map0001`、上映版本 `rv0002`（年份整体偏移）、规范化版本 `t4n0` 与解析版本 `t3p0` 的断言、旧 IMDb 快照 `imdb_2026-01-01`，用来证明任务 7 只读指定版本（AC01）。

## 案例与 AC 场景

| 测试文件 | 案例世界 | 对应 AC 场景 |
|---|---|---|
| `test_task7_editions.py` | `_scope_world`：五类任务 6 上映状态、只经 contains 出现的成员作品、无关联合集 `B0EMPTY`、`tv_series` 商品 `B0TV`、干扰版本 | AC01 版本层范围 |
| `test_task7_editions.py` | `_attribute_world`：同一作品的 DVD 宽屏版与 VHS 全屏版、含该作品的合集 | AC02 版本属性隔离 |
| `test_task7_editions.py` | `_ac09_world`：E1 发行商 `Warner Home Video` 与 `Warner Bros.`、发售日与上架日；E2 `Sony Pictures` 与 `Universal Studios`；E3 多区域多值特征；E4 带标记的发售日；E5 包装 `2-Disc Set` | AC09 |
| `test_task7_link_assess.py` | `_ac21_world`：L1 弱匹配 Y∧RT，L2 Y∧D，L3 T∧RT，L4 `John Cherry` 对 `John R. Cherry III`，L5 只亮 RT，L6 强关联 Y∧RT，L7 页面人物无 `nconst` 但姓名相同，L8 `Emma VHS` 与 `Departed, The`，L9 评论证据早于 IMDb 年，L10 无可比证据 | AC21 |
| `test_task7_link_assess.py` | `_ac10_world`：P1 已熔断、P2 标题相等且导演兼容（同一对两条问题）、P3 标题相等但导演不兼容 | AC10 |
| `test_task7_movies_scalar.py` | `_scope_world`：五类上映状态、只经 contains 出现的成员、干扰版本 | AC01 作品层 |
| `test_task7_movies_scalar.py` | `_attribute_world`：`Seven Samurai` 的 DVD 版、VHS 版与含该作品的合集 | AC02 作品层 |
| `test_task7_movies_scalar.py` | `_ac03_world`：M1 `Seven Samurai`、M2 `The Kid`、M3 `red shadow` 对 `crimson ninja`、M4 已熔断 `Wrong Trousers` | AC03 |
| `test_task7_movies_scalar.py` | `_ac04_world`：Y1–Y8 年份决策表 | AC04 |
| `test_task7_movies_scalar.py` | `_ac05_world`：R1 四个版本的片长差异、R2 单碟 135 分钟、R3 92 与 94、R4 85 与 118、R5 已熔断 | AC05 |
| `test_task7_movies_scalar.py` | `_ac06_world`：A1 简称与全称、P1 具体分级对未评级、P2 `G` 对 `PG`、A2 未知分级、A3 未知与已知并存、A4 同 ASIN 不一致 | AC06 |
