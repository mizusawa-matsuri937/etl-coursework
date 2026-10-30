# 任务 6 设计思路（补全上映信息）

状态：2026-10-30 编写计划；同日用户认为无候选作品偏多，要求补充来源，本文加入“无候选作品的补充来源”一节，以及人物共现反查、评论年份提及、被占用 `tconst` 的线索、商品发售日期上界的设计，规则编号随 spec 改为 R01–R39。同日完成 T6-01 至 T6-07 初版实现后经独立审查（见 [review](review.md)），本文文末补充“审查发现与修复设计（T6-08 至 T6-11）”，待用户审批后执行修复。需求、可观察行为与 AC 以 [spec](spec.md) 为准；本文说明怎么实现、为什么这样选。择优顺序、置信度、共现反查的分数与条件、评论计票门槛、B3 的天数与上界置信度分档都是**规划默认值**（规则版本 `t6r1`），只在开发池上调整、评价表导出前冻结；资源数字都是**估计**，实测在 T6-07 / T6-11 补记。规划时的数字来自 2026-10-30 正式库只读查询与只读探查脚本（下文标“实测”或“模拟”），正式数字以 AC18 为准。

## 整体思路

任务 6 是集成层里的“缺失信息补全”：输入是任务 4 的作品映射 `map0002`（作品与 `tconst`）、任务 3/4 的页面字段与断言（标题、标题年份、导演、演员、商品发售日期）、任务 5 的升级线索、任务 1 的有效评论与 IMDb 快照；输出是作品的上映信息（候选、选择记录、推断上界）与商品层的日期（集成层与目标层）。全程离线，不联网。

先定语义，再取值：

| 时间 | 语义 | 粒度 | 本任务的来源 | 去向 |
|---|---|---|---|---|
| 首次公开发行年份 | 作品第一次公开发行（院线、电视首播或直接发行录像，不分地区） | 作品 | IMDb `startYear`（经任务 4 关联、任务 5 线索或本任务的人物共现反查确认作品）；Amazon 商品标题年份 | `release_candidate` → `release_selection` |
| 地区院线日期、完整首映日期 | 某地区院线上映日 / 首映日 | 作品 × 地区 | 无获准且带日期的来源（H01） | 缺失，原因 `no_permitted_dated_source` |
| 商品发售日期 | 这个 DVD、Blu-ray、VHS 商品的发售日；或 Amazon 上架日 | 商品（ASIN） | 页面 `Release date`、`Publication date`、`Date First Available` | `product_release_date` |
| 最早评论时间 | 该作品在 Amazon 的第一条评论 | 作品 / 商品 | 任务 1 有效评论 | `movie_review_time`、`product_review_time` |
| 评论年份提及 | 评论人在正文里写的年份（如 “this 1950 classic”） | 作品 | 任务 1 有效评论正文 | `review_year_mention`（只作证据与消歧） |
| 推断上界 | “首次公开发行不晚于该时间” | 作品 | 最早评论时间（B1–B5）或最早商品发售日（M1–M4），取较早者 | `bound_evidence` → `inferred_release_bound`（`status = inferred`） |

不同语义的时间不放进同一张候选表，也不互相择优（R04）：选择记录只读“首次公开发行年份”的候选；商品发售日期和评论时间各在自己的表里，只用来推断上界；评论年份提及只作证据。任务 7 的跨来源冲突（例如 IMDb 年份与页面标题年份差很多）在此基础上处理，本任务只记录并按固定顺序采用。

分七步，每一步的结果都入库、都可追溯：

1. **年份候选**（R06–R08、R12–R13）：IMDb 关联作品的 `startYear`、任务 5 线索指向的 IMDb 作品的 `startYear`、商品标题年份。
2. **人物共现反查**（R08–R10）：对没有 IMDb 关联的作品，按页面导演、演员在 IMDb 演职员里反查作品，标题吻合且唯一时产生候选。
3. **商品发售日期**（R18–R19）：把任务 4 的 `edition.product_release_date` 断言按页面标签分成发售与上架两类，标出可疑值。
4. **评论时间与年份提及**（R11、R20）：按商品算最早与第二早有效评论，再按作品汇总；从评论正文抽年份提及并计票。
5. **择优与状态**（R14–R17）：按固定顺序采用候选，写选择记录、状态、缺失原因与分歧质量问题，生成上映信息版本。
6. **推断上界**（R21–R26）：对每部作品评估评论与发售日期两种证据，成立的取较早者另存；矛盾写质量问题。
7. **报告、查询与评价**（R33–R39）：导出覆盖率、实例与查询结果；另有命令导出评价表、导入标注、计算指标。

**为什么只做年份**（R03，H01）：作业明确 IMDb 公开基础表只提供上映年份；带完整日期与地区的来源要么没有开放接口（豆瓣），要么需要联网抓取或注册 API key（Wikidata、TMDb），用户选择本轮不联网。所以 `release_date` 全部为 NULL，只有 `release_year` 与 `precision = year`，地区院线日期整体缺失并写原因；不把商品发售日期或评论时间拿来“补日期”。

**为什么任务 5 线索可以当候选**（R07、R08，H03）：线索的含义是“这个没有 IMDb 关联的商品上，至少两位署名人物经作品年表反查都指向同一部 IMDb 作品”，证据是“多位已核实的人 + 同标题键作品”，比单靠标题强，但它不是任务 4 的作品判定，所以单列来源 `imdb_t5_hint`、状态 `hint_matched`、置信度低于直接关联，评价单独成层。线索 `tconst` 已被另一部作品占用（实测 40 条）说明任务 4 漏合并：两部“不同作品”其实是同一部。年份取同一 `tconst` 并不会错，所以照样作候选，但标 `shared_tconst` 并写质量问题，让漏合并在报告里可见，留给任务 4 升级。

**为什么加人物共现反查**（R09，H11）：修订前估算有 3,873 部作品没有任何年份候选。抽样它们的标题发现，相当一部分是 IMDb 里有的电影，只是任务 4 的标题键没对上：标题带有 “VHS”“Walt Disney Film Classics” 之类没被剥掉的尾巴（如 “Bagdad Cafe VHS”、“Treasure Island Walt Disney Film Classics The Fantastic Adventure Series”），或冠词倒置（“Lobos de Washington, Los”）。任务 4 的候选以标题键相等为入口，所以这些作品连候选都没有。共现反查反过来从人出发：页面写的导演、演员在 IMDb 里共同出现在哪部作品，再要求该作品的标题实词都在商品标题里出现。“同一位导演 + 标题吻合”或“两位演员 + 标题吻合”同时指向同一部作品，误中的概率很低；规划时模拟见 H11 与下文。它只为本任务提供年份证据，不改任务 4 的作品映射。

**为什么推断上界要设条件，并加上商品发售日期**（R21–R22，H02、H13）：最早评论时间只在“评论确实是这部作品的、时间可信”时才意味着作品此前已公开。实测有三类反例：翻拍或映射错位（The Punisher 2004 的最早评论在 2000 年）、提前评论（Kill Bill: Vol. 2 的最早评论在 2003-12）、孤立的早期评论，条件 B1–B5 分别针对它们。DVD、VHS 的发售本身就是一次公开发行，所以“首次公开发行不晚于最早的发售日”在语义上比评论更直接；实测违例率 0.5%，与评论相当。两种证据各自评估，取成立者中较早的时间，对没有年份的作品至少能给出“某年之前已发行”。预售没有标记，只能讨论、不能宣称排除（H07）。

## 输入依据与规划时快照

- 任务依据：作业任务 6 与第五、八、十部分；交接文档 `doc/handoffs/2026-10-30-task5-to-task6.md` 第 2、3 节。
- Git：任务 5 经 PR #5 合并到 `main`（`1625ad9`）；本任务分支 `task6-release-info` 从该提交建立。
- **实测**（2026-10-30，正式库 `etl_movies` 只读查询；与交接文档一致处不重复列出）：

| 项目 | 实测 | 与交接文档的差异 |
|---|---|---|
| 迁移版本 | `001,101,102,103,201,301,302,401` | 一致 |
| 映射版本 | `map0001`（`t4r1`）、`map0002`（`t4r2`） | 一致 |
| `map0002` 作品 | 8,659 部：`imdb_linked` 4,473（有 `tconst`，`tconst` 互不相同）、`page_only` 4,186；经 `edition` 的 8,472 部，只经 `contains` 出现的 187 部 | 交接文档写“8,730 个商品 → 8,472 部作品”，未计只经 `contains` 出现的 187 部；本任务以 8,659 为作品分母，另报 8,472 |
| IMDb 关联作品的 `startYear` | 4,473 部全部非空；`titleType`：`movie` 3,863、`tvMovie` 335、`video` 275 | — |
| 商品–作品关系 | `edition` 8,730（`auto_decision` 4,544、`singleton` 4,186），`contains` 230 行（223 部成员作品） | 一致 |
| 任务 4 匹配状态（ASIN） | `singleton` 7,333、`review_pending` 899、`merged` 498、`collection_unsplit` 488、`collection_split` 372 | — |
| 商品标题年份（`title_year` 断言） | 190 条（商品 189、合集成员 1）；有标题年份的作品：IMDb 关联 91 部、页面独有 62 部；与 IMDb 年份比较（94 条）：相同 87、相差 1 年 7、相差超过 1 年 0；没有作品内标题年份不一致 | — |
| 商品发售日期（`edition.product_release_date`） | 9,558 条、7,940 个 ASIN，规范值全部为 `YYYY-MM-DD`；按页面标签：`Release date` 7,664、`Date First Available` 1,724、`Publication Date` 111、`Publication date` 59 | 交接文档只写了总数；`Date First Available` 是上架日期，语义不同（R18） |
| 商品发售日期可疑值 | `Release date` 中 1 月 1 日 342 条（4.5%），晚于 2012 年的 429 条；最早 1931-01-01，最晚 2027-01-01 | — |
| 有效评论（任务 1 `review_staging`） | 时间范围 1997-08-20 至 2012-10-25，全部为 UTC 零点（精度为日）；电影类商品 298,769 条（8,718 个 ASIN、8,461 部作品），合集商品 860 个、16,241 条 | 交接文档的 301,326 条是 `review_record` 行数，含 2,557 条隔离记录；本任务只用有效评论 |
| 最早评论与 IMDb 年份 | 有评论的 IMDb 关联作品 4,281 部：最早评论年份早于 IMDb 年份 24 部、同年 746、晚于 3,511；排除孤立点后早于 IMDb 年份的 17 部 | — |
| 最早 `media_release` 与 IMDb 年份 | 有 `media_release` 的 IMDb 关联作品 3,459 部：最早发售年份早于 IMDb 年份 17 部（早 2 年以上 5 部）、同年 485 | — |
| 最早评论与商品发售日期 | 同时有评论与 `Release date` 的 7,041 个 ASIN 中，最早评论早于该 ASIN 发售日的 3,265 个，早一年以上的 1,967 个 | 说明 Amazon 合并展示各版本评论，不能用“评论早于本商品发售日”判定预售（H07） |
| 第二早评论间隔（按作品） | 只有一条评论 1,502、第二条在 365 天内 5,157、超过 365 天 1,802 | — |
| 任务 5 升级线索 | 335 行（332 个 ASIN、328 个 `tconst`）：电影类商品 293 行（`tconst` 未被占用 253、已被另一作品占用 40），合集成员 42 行（未占用 26、已占用 16）；没有一部作品得到多个 `tconst`；线索年份与标题年份同时存在的 2 部，均一致 | 交接文档只写了 335 |

- 最早评论早于 IMDb 年份的例子（实测）：Nautilus 2000 / 1999-12-01、Once Upon a Time in Mexico 2003 / 1999-03-16、The Punisher 2004 / 2000-06-17、The Omen 2006 / 2000-04-19、Sleepless Nights 2022（`video`）/ 2003-09-29、Kill Bill: Vol. 2 2004 / 2003-12-18、Harry Potter and the Half-Blood Prince 2009 / 2008-05-29。前几部多为翻拍或映射到同名的另一部作品，后两部是上映前的提前评论。

## 无候选作品的补充来源（2026-10-30 用户要求）

修订前的规则（IMDb 关联、未占用的线索、标题年份）估算有 3,873 部作品（44.7%）没有任何年份候选：其中 3,120 部的商品是任务 4 的 `singleton`、753 部是 `review_pending`；2,269 部在任务 4 连一个 IMDb 候选都没有。逐个评估了只用已有数据（不联网、不重爬）能补充的来源，规划时的只读探查结果如下：

| 方案 | 做法 | 规划时验证（模拟） | 能补的无候选作品 | 结论 |
|---|---|---|---|---|
| A 人物共现反查 | 按页面导演、演员严格键在 IMDb 演职员中反查作品；导演 2 分、演员 1 分、≥2 分；IMDb 标题键实词 ⊆ 商品标题键；编号排除；唯一 | 用在任务 4 已关联的 4,544 个商品上：唯一命中 4,287 个，全部等于已关联的 `tconst`，年份全部一致（这些商品较容易，偏乐观）；与线索同时命中的 183 部全部指向同一 `tconst` | 约 689 部（其中 21 部与已占用线索重合、指向同一 `tconst`），新增约 668 部，`tconst` 已被占用的约 65 部 | **采用**（R09–R10），单列状态与评价层 |
| B 已占用 `tconst` 的线索 | 线索 `tconst` 属于另一作品时照样取年份，标 `shared_tconst` | 两部作品是同一部，年份取同一 `tconst`；与方案 A 重合的 21 部全部一致 | 40 部（其中 19 部方案 A 没覆盖） | **采用**（R08，修改 H03） |
| C 评论正文年份提及 | 评论正文按固定模式抽年份，按作品计票（≥2 票、占比 ≥60%） | 在 IMDb 关联作品上，选出的年份与 IMDb 年份一致 249 / 315（79%）、相差 ≤1 年 261 / 315；单条提及中括号年份只有 25% 一致（多是比较别的片） | 约 139 部（单独使用时） | **不单独采用**，79% 不够作年份；记录为证据 |
| C′ 评论年份消歧 | 一部作品有多个 IMDb 候选时，用评论年份唯一选出一部 | 在任务 4 已关联作品的全部 IMDb 候选上模拟：唯一选出 620 部，其中 600 部（96.8%）是已关联的 `tconst`；候选多于一部的 369 部中正确 349 部（94.6%） | 共现反查多部命中的约 85 部中的一部分（规划未逐项估算） | **采用**为共现多候选的消歧（R10） |
| D 商品描述年份 | 从页面快照的商品描述抽年份 | 按作品唯一取值与 IMDb 年份一致 79 / 129（61%），常见的是 “(VHS, 1994)” 这类载体年份 | 约 137 部 | **不采用**（H14） |
| E 标题末尾裸年份 | 商品标题末尾不带括号的年份 | 只有 53 条，多为 DVD 年份或赛事年份（如 “Saw III … 2007 DVD”、“WWE SummerSlam 1999”） | <53 部 | **不采用**（H14） |
| F 任务 4 复核档候选 | 直接采用任务 4 判为复核的 IMDb 候选 | 等于在任务 6 放宽任务 4 的阈值，且复核档多是“只有标题相同”；714 个商品有复核档候选，没有独立验证手段 | — | **不采用**；其中有人物佐证的已被方案 A 覆盖 |
| G 商品发售日期上界 | 最早 `media_release` 作“不晚于”证据 | 违例 17 / 3,459（0.5%） | 不补年份；无候选作品中有商品日期断言的 3,297 部 | **采用**为第二种上界证据（R22） |
| H 联网来源 | Wikidata（CC0，需按标题与人名查询）、TMDb（需 API key） | 未验证 | 未知 | 用户本轮不联网（H01），记为后续可选 |

修订后的估算：`verified` 4,473、`hint_matched` 293、`cooccur_matched` 约 668、`page_reported` 约 60、`missing` 约 3,165（36.6%）。剩下的作品多数在 IMDb 里查不到同名同人的作品：抽样的标题以动画分卷（“Mobile Suit Gundam Seed … Vol. 6”）、电视节目合辑（“The Carol Burnett Show - Collector's Edition”）、纪录片与教学片、演出录像为主，这类作品在 IMDb 基础表里往往没有对应的 `movie`/`tvMovie`/`video` 条目，也就不在本任务能确认的范围内；对它们只能给出推断上界，并照实报告缺失。

## 开源参考

按 CLAUDE.md 要求，动手前对比了几个开源项目与公开数据规范的做法（2026-10-30 检索）：

| 项目 | 借鉴什么 | 不直接用的原因 |
|---|---|---|
| [scrapinghub/dateparser](https://github.com/scrapinghub/dateparser)（[设置文档](https://dateparser.readthedocs.io/en/latest/settings.html)） | 解析结果单独返回 `period`（`day`/`month`/`year`），缺日时用 `PREFER_DAY_OF_MONTH` 补成当月首日、末日或当前日 | 正是作业禁止的“补成 1 月 1 日”的做法：它把精度和补全混在一个日期对象里。我们只借“精度与值分开返回”的思路，年份不变成日期；页面日期已由任务 4 规范为 `YYYY-MM-DD`，不需要再解析 |
| [python-edtf](https://pypi.org/project/edtf/)（PyPI `edtf`，实现美国国会图书馆 EDTF；GitHub 有多个分支，如 [ccnmtl/python-edtf](https://github.com/ccnmtl/python-edtf)） | 用精度/掩码表示不完整日期（`1985`、`1985-06`、`198X`），并按精度导出区间的上下界 | 新增依赖要重新锁定 `requirements.lock`；我们只需要 `year`/`month`/`day` 三档和“不晚于”上界，用两列（值 + 精度）表达，便于 SQL 查询与约束 |
| [Wikidata 的 publication date（P577）](https://www.wikidata.org/wiki/Property:P577) 与时间值模型 | 时间值带精度（9 年、10 月、11 日），上映日期用“地区”等限定符区分，一部作品可有多条日期 | 本轮不联网（H01）；借“精度随值保存、地区与类型作为限定”的模型，`release_selection` 留 `region`、`release_type` 列，当前只有 `first_release` 与 NULL 地区 |
| [TMDb release_dates](https://developer.themoviedb.org/docs/region-support)（[类型说明](https://tmdb.org/bible/movie/59f3b16d9251414f20000009)，开源客户端 [tmdb-api](https://docs.rs/tmdb-api/latest/src/tmdb_api/common/release_date.rs.html) 的枚举） | 上映类型分首映、限映、院线、数字、实体（VHS/DVD/Blu-ray）、电视六类，按国家给日期；“主上映日”按限映/院线 → 首映的顺序取 | 需要 API key 与逐片请求（H01）；借“实体发行（DVD 等）是一种独立的发行类型，不与院线日期混排”的分类，作为 R04、R18 的依据，以及“实体发行也是一次公开发行”作为 R22 的依据；借“按类型固定优先序”的择优方式（R14） |
| [IMDb Non-Commercial Datasets](https://developer.imdb.com/non-commercial-datasets) 数据字典 | `startYear` 为作品的发行年份（剧集为开播年份），`\N` 为缺失 | 只限个人、非商业使用；文件与导出不入仓库（R32） |
| [moj-analytical-services/splink](https://github.com/moj-analytical-services/splink)、[allenai/S2AND](https://github.com/allenai/S2AND)（任务 5 已对比） | 共现反查借“用多条分块规则生成候选、以合作者重叠为强特征”的思路：先用人名分块找作品，再用标题作必要条件 | 都是概率或训练模型；本任务用确定性规则，阈值随 `t6r1` 冻结 |

结论：不新增依赖，自写“值 + 精度 + 语义 + 来源”的小模型；择优用固定顺序而不是加权打分，理由是各来源语义相同、强弱分明，固定顺序可解释、可在报告里逐条说明。代价是不能表达“多个弱来源一致胜过一个强来源”，这类情况在本数据里很少（标题年份与 IMDb 年份相差超过 1 年的实测为 0），并以 `year_disagreement` 暴露。

## 组件与职责

| 组件 | 职责 |
|---|---|
| `sql/501_task6_schema.sql` | schema `task6` 的下文各表、约束与索引；登记迁移 `501` |
| `src/etl_task6/rules.py` | 纯函数：候选置信度、择优顺序、状态与缺失原因、分歧判定、共现评分与标题、编号条件、评论计票门槛、B1–B5、M1–M4 与上界置信度、商品日期标记；`RuleSet`、`RULESETS`、`CURRENT_RULE_VERSION = t6r1` |
| `src/etl_task6/candidates.py` | IMDb 关联、线索与标题年份三类候选与线索处理记录（R06–R08、R12–R13），按作品分批 |
| `src/etl_task6/cooccur.py` | 人物共现反查：页面姓名键 → IMDb 人物 → 演职员 → 作品评分、标题与编号条件、唯一性、评论年份消歧（R08–R10），按作品分批 |
| `src/etl_task6/review_years.py` | 评论正文年份提及的抽取与计票（R11），按作品分批 |
| `src/etl_task6/product_dates.py` | 商品发售日期与标记（R18–R19），按商品分批 |
| `src/etl_task6/reviews.py` | 商品层最早、第二早评论（按商品分批）与作品层汇总（R20） |
| `src/etl_task6/select.py` | 择优、选择记录、质量问题、上映信息版本、版本差异（R14–R17、R27） |
| `src/etl_task6/bounds.py` | 两种上界证据评估、推断上界、`bound_before_release_year`（R21–R25） |
| `src/etl_task6/run.py`、`runner.py` | 迁移应用、输入键、运行审计、抽取锁、批次与实体阶段标记、分步骤编排 |
| `src/etl_task6/report.py`、`query.py`、`check.py` | 报告、实例与 R26 的计数；两个查询（R34）；正式库只读核对（AC18） |
| `src/etl_task6/evaluate.py` | 开发池冻结、评价表导出、标注导入、指标（R36–R39） |
| `src/etl_task6/pipeline.py`、`dags/amazon_movies_task6_release.py` | DAG 步骤函数与编排 |
| `scripts/seed_task6_e2e.py` | 为真实 scheduler 端到端准备合成数据（写 `etl_test`） |
| `tests/fixtures/task6/` | 合成页面字段、任务 4 映射与断言、任务 5 线索、评论（含正文）与 IMDb 行，预期值说明 |

复用而不复制：

- `etl_task1`：`config.load_settings`、`db.connect`、`db.advisory_key`、`reporting.write_atomic`、`reporting.file_digest`、`reporting.utc_now`、`pipeline.is_transient`、`pipeline.gate_decision`。
- `etl_task2.pipeline.run_step` 与失败文件写法。
- `etl_task4.normalize`：`strict_name`（页面与 IMDb 姓名用同一严格键，与任务 4、5 一致）、`title_key`；商品标题键直接读任务 4 的 `title` 断言，IMDb 标题键直接读 `task4.imdb_title_key`（含别名与来源行）。
- `etl_task4.evaluate.prf` 的“无定义”约定（本任务的指标是比例，不是 P/R，自写 `ratio()`，分母为 0 同样返回“无定义”）；`etl_task4.run`、`etl_task5.run` 的锁、批次与实体标记写法（任务 6 的表单独建，不写 `task4`、`task5`）。
- 只读：`task4.movie`、`task4.product_movie`、`task4.product_match_status`、`task4.product_edition`、`task4.field_assertion`（`title`、`title_year`、`director`、`actor`、`edition.product_release_date`）、`task4.imdb_title_key`、`task4.source_record`、`task3.page_field`（取原始标签）、`task5.person_mapping_version`、`task5.t4_upgrade_hint`、`task5.link_decision`、`imdb.title`、`imdb.title_director`、`imdb.title_principal`、`imdb.name`、`imdb.snapshot`、`imdb.source_file`、`public.review_staging`。

## 数据模型

作业第八部分的实体与本任务表的对应：

| 作业实体 | 本任务的表 | 说明 |
|---|---|---|
| Movie 的上映信息 | `task6.release_selection`（按上映信息版本） | `release_year`、`release_date`、`precision`、`region`、`release_type`、`status` |
| FieldAssertion | `task6.release_candidate` | 每条候选的原值、规范值、精度、语义、来源与证据 |
| FieldSelection | `task6.release_selection` 的采用候选、参与候选与理由 | 每部作品一条 |
| MergeDecision（年份所依据的作品确认） | `task6.cooccur_score`、`task6.hint_use` | 共现反查与线索的确认证据，未采用的同样保存；不写任务 4 |
| ProductEdition 的商品发售信息 | `task6.product_release_date` | 按 ASIN，与作品年份分开 |
| 推断 | `task6.bound_evidence`、`task6.bound_assessment`、`task6.inferred_release_bound` | 两种证据、条件评估与推断上界分开 |
| SourceRecord | 复用 `task4.source_record`（页面快照）与 `imdb.source_file` + 行号；评论用任务 1 的 `source_review_id` | 不另建 |
| ETLRun 与 QualityIssue | `task6.run`、`task6.run_batch`、`task6.quality_issue` | — |

外键指向 `public.extraction`、`task4.mapping_version`、`task4.field_assertion`、`task4.source_record`、`task5.person_mapping_version`、`imdb.snapshot`，不在任务 1–5 的表上加列或约束（R31）。

**运行与版本**

- `task6.run`：`run_id`，`kind`（`release`/`evaluate`），输入键各项，`status`（`running`/`succeeded`/`failed`/`reused`/`interrupted`），`params`（`batch_size`、`limit`），计数、起止时间、`dag_id`、`airflow_run_id`、`failure_reason`。
- 输入键：（`extraction_id`, `t4_mapping_version_id`, `parser_version`, `t5_person_mapping_version_id`, `imdb_snapshot_id`, `rule_version`）。已有成功运行时新运行记 `reused`（R28）。
- `task6.run_batch`、`task6.entity_stage`：与任务 4、5 相同的批次审计与实体阶段标记。阶段键：候选 = 映射版本 + 解析版本 + 人物映射版本 + IMDb 快照；共现反查 = 映射版本 + 解析版本 + IMDb 快照 + 共现参数（分数、标题与编号条件的版本）；评论年份提及 = 抽取 + 映射版本 + 提及模式版本；商品发售日期 = 抽取 + 解析版本；评论时间 = 抽取 + 映射版本。择优、上界的阈值与置信度属于规则版本，只在全局步骤使用，所以只改这些时不重新抽取（AC13）；改共现参数或提及模式会换阶段键、重做对应阶段。
- `task6.release_version`：`release_version_id`（`rv0001`…），`parent_version_id`、`run_id`、输入键各项、`counts`（R33 的数量）、`created_at`。

**候选与作品确认证据**

- `task6.release_candidate`：唯一键（阶段键, `movie_id`, `source`, `source_key`）。
  - `source`：`imdb_t4_link`、`imdb_t5_hint`、`imdb_t6_cooccur`、`amazon_title_year`；`source_key` 为 `tconst` 或 `<ASIN>:<断言 ID>`。
  - 值：`raw_value`（原文，如 `1999`）、`value_year`、`precision`（本任务恒为 `year`，列上有 CHECK）、`semantic = first_release`、`region`（NULL）。
  - 标记：`flags`（`shared_tconst`、`review_year_tiebreak`）、`shared_with_movie_id`。
  - 来源：`source_ref` jsonb（IMDb：快照、文件名、行号、`tconst`、`titleType`；页面：ASIN、区域、页面字段序号、断言 ID、`source_record_id`、最终地址）、`source_acquired_at`（IMDb 快照下载时间或页面采集时间）、`extracted_at`（本次抽取时间）。
  - 匹配证据：`match_evidence` jsonb（`imdb_t4_link`：任务 4 的 `product_movie.basis`、`decision_id`、关联的 ASIN；`imdb_t5_hint`：线索的 `mention_keys`、`link_decision_ids`、`mention_count`、人物映射版本；`imdb_t6_cooccur`：对应 `cooccur_score` 行的 ID 与摘要；`amazon_title_year`：商品经 `edition` 关系归到该作品）。
- `task6.hint_use`：主键（阶段键, `asin`, `member_ordinal`, `tconst`）；`movie_id`、`action`（`candidate`/`candidate_shared`/`collection_member`/`multiple_tconst`/`movie_already_linked`）、`owner_movie_id`（`tconst` 被占用时的占用者）。
- `task6.cooccur_score`：唯一键（阶段键, `movie_id`, `tconst`）；`score`、`director_hits`、`actor_hits`（数组：页面断言 ID、姓名严格键、IMDb `nconst`、演职员文件与行号）、`title_ok`、`matched_title_key` 与 `imdb_title_key.source_line`、`key_source`、`number_guard`（被编号条件排除时记商品标题里的编号）、`passed`；作品层汇总 `task6.cooccur_result`：主键（阶段键, `movie_id`）；`outcome`（`unique`/`ambiguous`/`resolved_by_review_year`/`none`）、`chosen_tconst`、`passed_tconsts`、`owner_movie_id`。
- `task6.review_year_mention`：主键（阶段键, `movie_id`, `year`）；`votes`（不同评论数）、`source_review_ids`（数组）、`patterns`（各模式票数）；作品层 `share` 与是否达到计票门槛在全局步骤按规则版本计算。

**选择与推断**

- `task6.release_selection`：主键（`release_version_id`, `movie_id`）。
  - `status`：`verified`/`hint_matched`/`cooccur_matched`/`page_reported`/`missing`；`missing_reason`。
  - `release_year`、`release_date`（恒为 NULL）、`precision`、`region`（NULL）、`release_type = first_release`、`regional_release_missing_reason = no_permitted_dated_source`。
  - `selected_candidate_id`、`considered_candidate_ids`（数组）、`selection_rule`（如 `t6r1:order:imdb_t4_link>imdb_t5_hint>imdb_t6_cooccur>amazon_title_year`）、`reason`、`confidence`、`disagreement` jsonb（与未采用候选的年差、`tconst` 不同）。
  - CHECK：`status = missing` 当且仅当 `release_year` 与 `selected_candidate_id` 为 NULL；`precision = year` 时 `release_date` 为 NULL。
- `task6.bound_evidence`：主键（`release_version_id`, `movie_id`, `evidence`）；`evidence`（`review`/`media_release`）、`evidence_time`、`evidence_precision`（`day`；`jan_first_suspect` 时 `year`）、`basis`（源评论 ID 与 ASIN，或断言 ID 与 ASIN）、`checks` jsonb（B1–B5 或 M1–M4 各自的结果与所用值）、`passed`、`failed`（数组）、`confidence`。
- `task6.bound_assessment`：主键（`release_version_id`, `movie_id`）；`has_review_evidence`、`has_media_evidence`、`passed_evidence`（数组）、`chosen_evidence`。
- `task6.inferred_release_bound`：主键（`release_version_id`, `movie_id`）；`bound_time`、`bound_precision`、`bound_kind = not_later_than`、`chosen_evidence`、`basis`、`conditions`（成立证据的条件快照）、`confidence`、`status = inferred`、`rule_version`。没有外键指向 `release_selection` 的值列，也不被择优读取（R24）。

**商品层**

- `task6.product_release_date`：主键（`extraction_id`, `parser_version`, `assertion_id`）；`asin`、`kind`（`media_release`/`listing_first_available`）、`label_raw`、`raw_value`、`value_date`、`precision`（`day`）、`flags`（`jan_first_suspect`、`after_review_period`）、`section`、`page_ordinal`、`source_record_id`。
- `task6.product_review_time`：主键（`extraction_id`, `asin`）；`product_kind`（`movie`/`collection`）、`earliest_time`、`earliest_source_review_id`、`second_time`、`review_count`。
- `task6.movie_review_time`：主键（`extraction_id`, `t4_mapping_version_id`, `movie_id`）；`earliest_review_time`、`earliest_precision = day`、`earliest_source_review_id`、`earliest_asin`、`second_review_time`、`review_count`、`reviewed_asin_count`、`edition_asin_count`。由 `product_review_time` 汇总：作品的第二早评论一定在各商品的前两条之中，所以商品层只存两条即可。
- `task6.quality_issue`：运行、实体、类型（`year_disagreement`、`tconst_disagreement`、`shared_tconst_with_movie`、`bound_before_release_year`、`tconst_not_in_snapshot`、`unknown_date_label` 等）与细节。

**评价**：`task6.dev_pool`（冻结的 `movie_id`）、`task6.eval_set`、`task6.eval_item`（层、作品、导出时的上映信息版本、系统年份、状态、来源与上界，用于计算，不导出）、`task6.eval_label`（`true_year`、`year_label`（`known`/`unsure`）、`tconst_label`（`same`/`different`/`unsure`/`na`）、`note`、`annotator`、`import_file_sha256`）。

## 年份候选（R06–R08、R12–R13）

- **`imdb_t4_link`**：`task4.movie.tconst` 非空时读 `imdb.title`（同一快照）；`start_year` 为 NULL 不产生候选。证据取该作品 `edition` 关系中 `basis = auto_decision` 的商品与决策 ID；只经 `contains` 出现的成员作品取 `contains` 关系的决策。置信度 0.95（H05）。
- **`imdb_t5_hint`**：读 `pmap0001` 对应阶段键（`map0002|t3p1|t5n1|t5r1|imdb_2026-10-29`，由 `task5.person_mapping_version` 的列拼出）的线索，逐条分类：
  1. `member_ordinal > 0`：`collection_member`；
  2. 商品所在作品已有 `tconst`：`movie_already_linked`（实测 0 条，保留防御）；
  3. 同一作品经不同商品得到多个 `tconst`：全部记 `multiple_tconst`（实测 0 部）；
  4. 线索 `tconst` 是映射版本中另一部作品的 `tconst`：`candidate_shared`，产生候选并标 `shared_tconst`、记占用者，写质量问题 `shared_tconst_with_movie`（R08）；
  5. 其余：`candidate`，产生候选，置信度 0.80。
- **`amazon_title_year`**：读 `field_name = 'title_year'` 的断言，`entity_type = product_edition` 的经 `edition` 关系归到作品，`collection_member` 的经 `contains` 成员序号归到成员作品；置信度 0.60。
- 来源获取时间：IMDb 取 `imdb.snapshot.downloaded_at`，页面取 `task4.product_edition.fetched_at`；抽取时间为本次运行时间（R13）。

## 人物共现反查（R08–R10）

**对象**：没有 `tconst`、也没有 `imdb_t5_hint` 候选（`candidate` 或 `candidate_shared`）的作品；只用其 `edition` 商品（合集成员作品不做，合集页面的人物无法归属到成员）。

**步骤**（规则 `t6r1` 的默认值，H11）：

1. 页面姓名：该作品各商品的 `director`、`actor` 断言（任务 4 规范值即严格键；`name_ambiguous`、`placeholder` 不用）。
2. IMDb 人物：`imdb.name.primary_name` 经 `strict_name` 得到严格键，只保留与页面姓名键相同的人（模拟时全量 14,922 个页面键对应 20,530 个 `nconst`）；启动时一次算好并按快照缓存在内存中。
3. 作品评分：页面导演键命中 `title_director` 的某个 `nconst` 记 2 分（同一作品只计一次），每个页面演员键命中 `title_principal`（`actor`/`actress`/`self`）记 1 分；得分 ≥2 的作品进入下一步。
4. 标题条件：该作品在 `task4.imdb_title_key`（同快照、`t4n1`）里有某个标题键，去掉停用词（冠词、介词与 `vhs`、`dvd`、`blu`、`ray`、`edition`、`collection`、`vol`、`volume`、`special`、`widescreen`、`full`、`screen`）后的实词集合非空且是商品标题键实词集合的子集。
5. 编号条件：商品标题键里有续集或卷号（阿拉伯数字或 `ii`–`x`，不含四位年份），而该标题键没有同一组编号时排除（防止 “The Land Before Time VIII” 命中第一部）。
6. 唯一性：通过 3–5 的作品恰好一部 → `unique`；多于一部时，若评论年份提及达到门槛的年份恰好等于其中一部的 `startYear` → `resolved_by_review_year`，候选标 `review_year_tiebreak`；否则 `ambiguous`，不产生候选，作品若无其他候选则 `missing`（`cooccur_ambiguous`）。
7. 被占用：选中的 `tconst` 属于映射版本中另一作品时，候选标 `shared_tconst`，写 `shared_tconst_with_movie`。

每个得分 ≥1 的作品–`tconst` 对都写 `cooccur_score`（含未通过的条件），便于查询与评价复看（R10）。置信度 0.75；经评论年份消歧的 0.70。

**为什么这些条件**：导演同名而作品不同很常见（同名导演、同一导演的多部作品），所以必须叠加标题；标题“IMDb 实词 ⊆ 商品实词”容忍商品标题后面的载体、系列说明，但要求 IMDb 标题的每个实词都在；只有一位演员同名（1 分）不够，因为配角常出现在大量作品里。模拟中 4,544 个已关联商品没有一例唯一命中指向别的作品，与线索同时命中的 183 部全部一致；代价是覆盖受页面署名完整性限制（没有导演、演员署名的作品无法反查）。

## 评论年份提及（R11）

- 范围：作品 `edition` 商品的有效评论的 `summary` 与 `text`。
- 模式（`t6m1`，不区分大小写，年份 1900–2012）：`(YYYY)`；`YYYY` 或 `YYYY's` 后接 film、movie、version、original、classic、feature 与常见类型词；made / filmed / shot / premiered / produced in `YYYY`；released (theatrically) in `YYYY`。
- 计票：每条评论每个年份一票，年份晚于该评论年份的不计；按作品记各年份的票数、源评论 ID 与模式分布。
- 门槛（规则版本）：最高票年份至少 2 票、占该作品全部年份票数 ≥60%，且没有并列最高。
- 用途：只用于共现反查多部命中时的消歧，以及在查询、评价导出中作为参考；不产生候选、不参与择优（R14、R17）。评论正文不导出，只导出源评论 ID 与票数。
- 实测耗时：全部 298,769 条评论的正则抽取在 PostgreSQL 中约 16 秒（规划时只读探查）。

## 择优与状态（R14–R17）

1. 收集作品的全部候选（各自的阶段键）。
2. 有 `imdb_t4_link` → 采用，`verified`；否则有 `imdb_t5_hint` → 采用，`hint_matched`；否则有 `imdb_t6_cooccur` → 采用，`cooccur_matched`；否则商品标题年份全部相同 → 采用（取断言 ID 最小的一条为代表），`page_reported`；标题年份不一致 → `missing`（`title_years_disagree`）；有 `multiple_tconst` 线索或共现 `ambiguous` 且没有其他可用候选 → `missing`（`hint_ambiguous` / `cooccur_ambiguous`）；什么都没有 → `missing`（`no_candidate`）。
3. 采用值与每条未采用候选比较：相差 1 年计数，相差超过 1 年写 `disagreement` 与质量问题 `year_disagreement`；线索与共现指向不同 `tconst` 时写 `tconst_disagreement`（R16）。
4. `confidence` 取采用候选的置信度；`reason` 写成可读的一句，例如“采用 IMDb tt0043067 startYear 1950（人物共现反查：导演 Byron Haskin、演员 Bobby Driscoll 命中，标题 ‘Treasure Island’ 吻合，唯一）”。

选择记录只读 `release_candidate`；SQL 层面没有任何路径从 `product_release_date`、`movie_review_time`、`review_year_mention` 或推断上界取值（R17），核对脚本检查所有非 NULL 的 `release_year` 都等于其 `selected_candidate_id` 的 `value_year`。

## 商品发售日期（R18–R19）

- 从 `task4.field_assertion`（`edition.product_release_date`）出发，按断言的 `source_locator`（区域、序号）回查 `task3.page_field.label_raw`，标签不区分大小写：`release date`、`publication date` → `media_release`；`date first available` → `listing_first_available`；其他标签（实测没有）→ 质量问题 `unknown_date_label`，不入表。
- 规范值沿用任务 4（`YYYY-MM-DD`，精度 `day`）；只有年份或年月的规范值（实测没有）按实际精度保存，不补日。
- 标记：月日为 01-01 → `jan_first_suspect`；日期晚于有效评论的最大时间（实测 2012-10-25）→ `after_review_period`（多为后来的重新发行，页面是 2026 年采集的当前版本）。
- 商品发售日期在报告、查询、R26 的计数、评价导出的“参考信息”与 M1–M4 的上界证据里出现，不进年份候选。

## 评论时间与推断上界（R20–R26）

**商品层**：对参与匹配的商品（电影类与合集），从 `public.review_staging`（`classification = valid`，用 `(extraction_id, product_id)` 索引）取每个 ASIN 按（`review_time_utc`, `source_review_id`）排序的前两条与条数。按商品分批，默认 1,000 个 ASIN 一批（实测全量聚合约 7 秒）。

**作品层**：只汇总 `edition` 关系的电影类商品；合集商品的评论不进入成员作品（与任务 4、5 一致，R20）。

**评论证据条件（B，`t6r1` 默认值）**：

| 条件 | 判定 | 针对的问题 |
|---|---|---|
| B1 映射已确认 | 作品的 `edition` 商品在 `task4.product_match_status` 中都不是 `review_pending` | 作品归属未定，评论可能属于别的作品 |
| B2 非合集 | 作品至少有一个 `edition` 商品；评论只来自 `edition` 商品 | 合集评论说的可能是另一部成员作品 |
| B3 不孤立 | 第二早评论存在，且距最早评论不超过 365 天 | 孤立的早期评论（错标商品、年份错误）；也排除只有一条评论的作品 |
| B4 不矛盾 | 作品已采用 IMDb 来源年份 Y（`verified`、`hint_matched`、`cooccur_matched`）时，最早评论年份 ≥ Y；没有这类年份时视为通过 | 翻拍或映射错位（最早评论早于作品年份）；`page_reported` 年份不参与比较，因为标题年份可能是版本年份 |
| B5 时间可信 | 最早评论时间在该抽取有效评论的时间范围内，且不早于 1997-01-01 | 解析异常的时间 |

**发售日期证据条件（M，`t6r1` 默认值）**：

| 条件 | 判定 | 针对的问题 |
|---|---|---|
| M1 映射已确认 | 同 B1 | 同 B1 |
| M2 只用实体发行 | 只取作品 `edition` 商品的 `media_release`；`listing_first_available` 不用；合集商品不用 | 上架日期可能是预售上架，早于真正发行 |
| M3 取最早 | 取最早的 `media_release`；带 `jan_first_suspect` 时只保留年份、精度 `year` | 1 月 1 日可能是只有年份的占位 |
| M4 不矛盾 | 作品已采用 IMDb 来源年份 Y 时，该日期年份 ≥ Y | 映射错位或页面日期错误 |

- **合并**：两种证据各自评估；都成立时取较早的时间（比较时按各自精度的最晚时刻，即年精度的 2001 视为 2001-12-31 与日精度比较，保留原精度），记 `chosen_evidence`。
- **置信度**（工程默认）：发售日期证据 0.7（`jan_first_suspect` 时 0.6）；评论证据 0.6，最早评论后 365 天内的评论不足 3 条时 0.4；推断上界的置信度取采用的那种证据。上界是“不晚于”，对没有年份的作品只说明“某时间前已公开”，不代替年份。
- **矛盾**：B4 或 M4 不成立时写 `bound_before_release_year`，细节含作品、`tconst`、年份、证据类型与定位；不改任务 4，作为任务 4 升级或任务 7 的线索（R25）。
- **为什么 B4、M4 不用 `page_reported`**：标题年份是商品自述，有时是重新发行年份；用它否决上界会误伤。
- **为什么不用商品发售日期判断预售**（R26，H07）：实测 46% 的 ASIN 最早评论早于本商品发售日，原因是 Amazon 把同一作品各版本的评论合并展示（SNAP 数据也继承了这一点），这些评论多是对旧版本或院线上映的评论，不是预售。数据里没有任何预售标记，因此报告只给出：合集商品评论数（不归入作品）、多版本作品（实测 240 部）中最早评论所在商品与最早 `media_release` 所在商品是否相同、最早评论早于作品全部 `media_release` 日期的作品数；并说明这些评论在 B1–B5 下仍可能是提前评论（如 Kill Bill: Vol. 2），评论证据因此置信度不高于 0.6。

## 版本、幂等、分批与编排（R27–R30）

**分批**：

- 年份候选、共现反查与评论年份提及按作品分批（默认 1,000 部一批，按 `movie_id` 排序），实体为 `movie_id`，阶段名分别为 `candidates`、`cooccur`、`review_years`；线索处理与候选同批。
- 商品发售日期按商品分批（默认 1,000 个 ASIN），阶段名 `product_dates`；商品层评论按商品分批，阶段名 `product_reviews`。
- 作品层评论汇总、共现消歧（依赖评论年份提及）、择优、上界评估与推断上界、质量问题、版本计数是全局步骤，在一个事务里生成整个上映信息版本（规模约 8,659 部作品，允许一次提交）。
- 每批的目标行、实体阶段标记与 `run_batch` 在同一事务提交；中断后同一输入键重跑跳过已标记的实体；全局步骤没有提交就从头重做（AC14）。

**锁**：按抽取加 advisory lock（与任务 4、5 不同的键），拿不到锁抛 `RunBusy`，确定性失败。

**版本与差异**：同一输入键已有成功运行 → `reused`。只改择优或上界的阈值与置信度 → 各阶段键不变而复用，只重做全局步骤，生成新版本（父版本为上一版本）。`python -m etl_task6.report diff <旧> <新>` 列出状态、年份、上界或上界条件变化的作品与原因（例如“B3 天数 365 → 180”“输入映射版本变化”）。

**DAG `amazon_movies_task6_release`**：`check_inputs → candidates → cooccur → product_dates → review_times → select → bounds → verify → finalize_report → gate`。

- 参数：抽取、任务 4 映射版本、任务 5 人物映射版本、IMDb 快照、`rule_version`、`batch_size`、`limit`。
- `check_inputs`：映射版本存在且属于该抽取；人物映射版本基于同一映射版本；IMDb 快照为 `loaded`，且 `title_director`、`title_principal`、`name` 与 `t4n1` 的 `imdb_title_key` 存在；迁移 `302`、`401`、`501` 已应用（缺 `501` 时应用）。
- `cooccur` 依赖 `candidates`（要知道哪些作品已有线索候选）；`review_times` 同时做商品层评论与评论年份提及。
- `select` 生成上映信息版本，`bounds` 在同一版本上写上界，靠 `task6.run.airflow_run_id` 找回同一条运行（任务 4、5 的做法）。
- `verify` 只读核对 AC18 的约束；`finalize_report` 与 `gate` 的触发规则为 `all_done`，门禁只在上游全部成功时通过。
- 暂时错误（连接中断、死锁）最多重试 2 次；确定性错误（输入缺失、约束违反、锁被占用）不重试。
- `limit` 只限制候选阶段的作品数，之后的步骤因仍有未处理作品而确定性失败，不生成上映信息版本（试跑不冒充全量）。

**命令**（不进 DAG，由人触发）：`python -m etl_task6.evaluate freeze|export|import|metrics`；`python -m etl_task6.report summary|examples|export|diff`；`python -m etl_task6.query movie <movie_id>`、`query gaps [--status missing|inferred]`；`python -m etl_task6.check`。

**故障注入**：`before_commit:<阶段>:<批号>` 与 `transient_once:<标记文件>`，仅 `ETL_ENV=test`。

## 统计、报告与查询（R33–R35）

`summary.json` 与 `report.md` 包含：

- 作品数（8,659 = 经 `edition` 8,472 + 只经 `contains` 187）；`release_year` 按状态的作品数与占比，`missing` 按原因；同样口径按参与匹配的商品（作品有年份的商品数 / 9,590）与全部产品（/ 253,059）各报一次覆盖率。
- 候选：按来源计数；`year_disagreement`、`tconst_disagreement` 数与年差分布；相差 1 年的数；`titleType` 分布。
- 线索处理：按 `action` 计数，`candidate_shared` 前 20 例（含占用者）。
- 共现反查：参与作品数、`unique`/`ambiguous`/`resolved_by_review_year`/`none` 计数、被标题条件与编号条件排除的数、`shared_tconst` 数与前 20 例。
- 评论年份提及：有提及的作品数、达到计票门槛的作品数。
- 地区院线日期与完整首映日期：覆盖 0，缺失原因 `no_permitted_dated_source`。
- 商品发售日期：按 `kind` 与标记计数，有发售日期的商品数。
- 评论时间与上界：有 `earliest_review_time` 的作品数与占比；两种证据的评估数、各条件未通过数（另报“首个未通过条件”的互斥分布）；推断上界数（按采用的证据与置信度），其中无年份作品的数；`bound_before_release_year` 数（按证据类型）与前 20 例。
- R26 一节：合集商品与其评论数、多版本作品的最早评论商品比较、最早评论早于全部 `media_release` 的作品数，以及“数据没有预售标记”的说明。
- 实例（`examples.md`，R35）：按 `md5(movie_id)` 在每类中取前 3（评价已标注后优先取已标注的并注明结论），每例给作品标题、候选与来源定位（IMDb 文件与行号、页面字段与快照、共现命中的姓名与演职员行号）、采用理由、上界证据与条件。

导出文件：`summary.json`、`report.md`、`release_selection.csv`、`release_candidates.csv`、`cooccur.csv`、`product_release_dates.csv`、`bounds.csv`、`hint_use.csv`、`examples.md`，写入 `outputs/task6/<抽取>/<上映信息版本>/<run_key>/` 并记录哈希；不含评论正文与整页 HTML（评论只给源评论 ID、时间与年份票数）。

**查询**（R34）：`query movie <movie_id>` 输出 JSON：作品与商品、全部候选（含未采用）、共现评分记录、评论年份票数、选择记录、上界证据与推断上界、各商品发售日期、作品与商品的最早评论；`query gaps --status missing|inferred` 列出缺失或有推断上界的作品与原因（作业第十部分的查询之三）。

## 评价设计（R36–R39）

**开发池与评价池**：首次全量（`t6r1`）后，按 `md5(movie_id)` 排序，前 20% 的作品冻结为开发池，记入 `dev_pool`；调整 H05、H11、H12 的门槛、B3 天数或置信度分档时只看开发池（AI 观察，不产生标注、不算评价），调整记在本文新增的“规则调整记录”。评价表导出前冻结规则版本。

**120 部作品的分层**（评价池内每层按 `md5(movie_id)` 取前 N，不足时按 E1、E7 的顺序补足，实际层数写入评价集）：

| 层 | 内容 | 部数 | 主要检验 |
|---|---|---|---|
| E1 | `verified`，`titleType = movie`，无分歧、无上界矛盾 | 15 | IMDb 年份与关联的基本正确率 |
| E2 | `verified` 中的难例：`titleType` 为 `tvMovie`/`video`，或有 `year_disagreement`、相差 1 年、上界矛盾 | 15 | 发行语义与映射错位 |
| E3 | `hint_matched`（含 `shared_tconst`） | 15 | 线索关联的作品是否正确 |
| E4 | `cooccur_matched`（含 `shared_tconst` 与经评论年份消歧的） | 20 | 共现反查的作品是否正确（新来源、规模最大） |
| E5 | `page_reported` | 15 | 商品标题年份是否是作品年份（估算约 60 部，评价池约 48 部） |
| E6 | 没有年份、有推断上界 | 20 | 上界是否成立、能否查到真实年份 |
| E7 | 没有年份、没有推断上界 | 20 | 缺失是否真的查不到（漏补率） |

- 上界成立率在所有层中有推断上界、且标注了真实年份的作品上计算（E1–E5 中的推断上界也计入），按证据类型分开，不只是 E6。
- **导出**：`outputs/task6/<抽取>/<上映信息版本>/eval/eval_items.csv`。每行含 `item_id`、作品标题、各商品 ASIN、商品标题与 `https://www.amazon.com/dp/<ASIN>`、导演与主演（来自任务 4 断言，帮助确认作品）、系统关联或匹配到的 `tconst` 与 `https://www.imdb.com/title/<tconst>/`，空列 `true_year`、`year_label`（`known`/`unsure`）、`tconst_label`（`same`/`different`/`unsure`/`na`）、`note`。不导出系统年份、状态、来源、层与上界（R37）。
- **标注口径**：`true_year` 是作品首次公开发行（院线、电视首播或直接发行录像）的年份，以作品本身为准，不是 DVD 发行年；`tconst_label` 判断 IMDb 作品与 Amazon 商品是否同一作品；证据不足标 `unsure` 并写 `note`。
- **AI 预标注**（H04）：另存 `eval_items_draft.csv`，带 `ai_true_year`、`ai_tconst_label`、`ai_doubt`、`ai_reason`；依据只用表中字段与公开页面，AI 给出的年份只是建议，不作为事实来源，付林轩核查后才导入，标注人为付林轩；工具、用途与人工验证方式记入报告（R39）。
- **分歧处理**：只有一位标注人；导入时列出明显矛盾（`tconst_label = same` 但 `true_year` 与该 `tconst` 的 IMDb 年份相差超过 1 年），供标注人复看，不改动标注。

**指标**（R38）：

- 年份准确率（完全一致）= 系统年份等于 `true_year` 的作品数 / 系统有年份且 `year_label = known` 的作品数；另报“相差 ≤1 年”的同口径比例；按层、按来源与总体。
- `tconst` 正确率 = `same` / (`same` + `different`)，按来源（任务 4 关联、线索、共现反查）分开。
- 缺失可查到率 = `missing` 作品中 `year_label = known` 的数 / `missing` 作品中已标注（不含 `unsure`）的数；它是漏补的上界估计。
- 推断上界成立率 = `true_year` ≤ 上界年份的作品数 / 有推断上界且 `year_label = known` 的作品数，按证据类型。
- `unsure` 单独计数、不进分母；分母为 0 记“无定义”。报告注明样本分层刻意多取难例，指标只代表评价样本。

## 资源预算与实测（T6-07 实际测量）

| 项目 | 规划估计 | 实测（2026-10-30 正式库全量） | 依据 / 差异说明 |
|---|---|---|---|
| 年份候选 | 约 5,600 条 | 5,795 行 | 包含直接关联 4,473、线索 293、共现候选 873、标题年份 156 |
| 共现评分记录 | 数万行 | 188,980 行 (`cooccur_score`) | 4,186 部非关联作品的得分扫描，其中 871 部唯一命中、3 部经年份消歧 |
| 共现反查内存与耗时 | 1–2 分钟 | 44.39 秒（初版 `8ec58727…` 实测，修复后在 T6-11 更新） | 快照流式扫描 `imdb.name` 全量 2,050,319 行并按待反查作品页面姓名键过滤，未发生 OOM |
| 评论年份提及 | 约 2.9 万条提及、3,700 部作品 | 8,814 条提及、2,765 部作品 | 规划估算包含全部文本提及，实测严格按 `t6m1` 四类固定模式抽取并按作品每评论每年份等权计 1 票 |
| 线索处理记录 | 335 行 | 294 行 (`hint_use`) | 候选 253、共享 40、合集成员 1 |
| 商品发售日期 | 约 9,600 行 | 9,558 行 (`product_release_date`) | 实体发售 7,834、上架日期 1,724 |
| 商品层评论 | 约 9,600 行 | 9,578 行 (`product_review_time`) | 覆盖 8,461 部有评论的电影作品 |
| 选择、证据与推断 | 选择 8,659、上界证据约 1.6 万、推断约 7,000 | 选择 8,659、证据 15,397、推断 6,978 | 评估 8,468 部作品；与估算高度吻合 |
| 全量运行 | 10 分钟以内 | 约 86 秒（纯业务处理，Airflow 总耗时约 2 分半） | 调度开销与各阶段串行提交 |
| schema `task6` | 200 MB 以内 | 150 MB | 包含 jsonb 证据与全部索引 |
| 重跑（`reused`） | 1 分钟以内 | 2 毫秒（审计记录插入耗时） | 审计标记为 `reused`，未写入任何业务表 |

## 异常与恢复

| 情况 | 处理 |
|---|---|
| 映射版本不存在或不属于该抽取、人物映射版本不基于该映射版本、IMDb 快照未装载或缺演职员、人名、标题键 | `check_inputs` 确定性失败，下游 `upstream_failed` |
| 关联的 `tconst` 在快照里找不到 | 不产生候选，质量问题 `tconst_not_in_snapshot`，作品按其他候选择优 |
| `startYear` 为 NULL | 不产生候选，作品可能为 `missing`（`no_candidate`） |
| 线索为合集成员或多 `tconst` | 只记 `hint_use`，不产生候选 |
| 线索或共现的 `tconst` 被占用 | 产生候选并标 `shared_tconst`，质量问题 |
| 共现反查多部满足、评论年份不能唯一选出 | `ambiguous`，不产生候选 |
| 商品日期标签未知或规范值无法解析 | 不入表，质量问题 |
| 一部作品出现两条同来源同键的候选 | 唯一约束失败，确定性失败（说明上游数据异常） |
| 评论时间超出范围 | B5 不通过，评论证据不成立 |
| 中断 | 已提交批次保留，重跑跳过；全局步骤整体重做 |
| 修了影响候选或共现的代码 | 已有阶段标记不会重算：升规则版本不够，要清空 `task6` 后从头重跑（任务 5 T5-07 的教训），清空前后核对任务 1–5 行数 |

## 已知局限与风险

- **只有年份**：没有一部作品有完整日期或地区院线日期；作业允许的“指定地区院线上映日期”整体缺失（H01）。
- **IMDb 年份的语义**：`startYear` 是 IMDb 认定的发行年份，可能是电影节首映年，也可能是 `video`、`tvMovie` 的首次发行或播出年；本任务不细分，`titleType` 保存为证据，E2 层检验。
- **年份正确依赖作品确认**：任务 4 关联、线索或共现反查错了，年份也会错；上界矛盾与评价的 `tconst_label` 只能暴露一部分。
- **共现反查的误挂**：同名导演与同名翻拍同时出现时可能挂到另一版本（如同一导演重拍自己的作品，标题相同）；模拟只在较容易的已关联商品上做过，真实误差要看 E4 层。它也会把任务 4 本该合并却没合并的作品挂到同一 `tconst`（`shared_tconst`），这是任务 4 的问题，本任务只暴露。
- **缺失仍多**：估算约 36.6% 的作品没有年份，主要是 IMDb 基础表里没有对应作品的动画分卷、电视合辑、纪录片、教学片与演出录像；推断上界只说明“不晚于”，不能当年份；继续减少缺失需要联网来源（H01）或任务 4 升级。
- **预售无法识别**：见 H07；评论证据可能被提前评论拉早，置信度因此不高于 0.6。
- **商品标题年份**：可能是重新发行或修复版的年份；只在没有 IMDb 来源时采用，实测与 IMDb 一致性很高（94 条中相差超过 1 年的 0 条），但样本小。
- **评价有偏、标注来自 AI 预标注**：分层多取难例；AI 草稿经付林轩核查后导入，AI 与系统可能用相近的证据（IMDb 页面），指标可能偏乐观，照实报告。

## 与后续任务的接口

- 任务 7（来源冲突）：`release_candidate` 已保存同语义的全部候选与年差，`year_disagreement`、`tconst_disagreement` 是冲突入口；跨语义的比较（例如商品发售日期早于首次发行年份、推断上界早于年份）在任务 7 处理。
- 任务 8（评论去重）：`earliest_review_time` 与评论年份提及用有效评论、未去重；同一评论被复制到多个版本时时间相同，最早时间不受影响，但评论数与年份票数会偏多（计票按作品内不同评论 ID，复制评论会重复计票，任务 8 去重后可重算）。
- 任务 9（字段级血缘）：`release_selection` → `release_candidate` → IMDb 文件行号或页面字段与快照，共现候选再到 `cooccur_score` 的姓名与演职员行号；推断上界 → 源评论 ID 或商品发售日期断言；`query movie` 是上映字段血缘的雏形。
- 任务 4 升级：`hint_use` 与 `cooccur_result` 中 `shared_tconst` 的作品（任务 4 漏合并）、`cooccur_matched` 的作品（任务 4 漏关联）与上界矛盾的作品，可作为新映射版本的输入。

## 实现中的修订（T6-01，2026-10-30）

计划批准后实现时发现的问题与调整，均不改变规则语义，无需用户确认：

1. **`release_candidate` 增加 `confidence` 与 `rule_version` 列**（R13 要求每条候选记录置信度与规则版本，原表结构遗漏；择优采用的置信度同时保存在选择记录里）。候选行在 `candidates` 与 `cooccur.resolve` 两处写入时一并带上。
2. **`cooccur_score.number_guard` 允许 NULL**：标题条件未通过的作品没有评估编号条件（原列 NOT NULL 会写入失败）；NULL 表示“未评估”，`false` 仍表示“被编号条件排除”。
3. **线索的阶段键动态取人物映射版本的规则版本**（`run.hint_stage_key`）：原先硬编码 `t5n1|t5r1`，换人物映射版本（如 `t5r9`）时会错误地读到旧键下的线索；改为从 `task5.person_mapping_version` 的行拼出，与任务 5 写入时一致。
4. **幂等与重跑**：商品层评论时间按商品（与映射版本无关），换映射版本重跑时批内先删后写；作品层评论时间、共现消解的 `cooccur_result` 同输入键先删后写；共现候选与已有选择记录存在外键，改为 `ON CONFLICT DO NOTHING` 幂等插入（同阶段键内容一致）；上界三表在同一上映信息版本上重跑（Airflow 步骤重试）时先删后写。`select` 先插入 `release_version` 再写 `release_selection`（外键顺序）。
5. **共现反查的对象为所有没有 `tconst` 的作品（含已有线索候选的作品）**：原设计写“没有 IMDb 关联、也没有线索候选的作品”，但 R16 的 `tconst_disagreement`（线索与共现指向不同 `tconst`）与 AC08 的 L 案例要求两者并存；择优仍先取线索，多出的共现候选只作分歧证据。
6. **AC01 换人物映射版本的期望从 missing 改为 cooccur_matched**：线索随人物映射版本消失（`imdb_t5_hint` 候选为空），但共现反查不读任务 5，仍唯一命中同一作品——这是 R09 与 R07 相互独立的自然结果，checklist 与夹具说明已按此更新。

夹具侧（测试内、不影响正式库）：任务 4 匹配用 **t4r2**（与正式库 `map0002` 同规则；默认 t4r1 的导演权重 3 使“标题 + 导演严格重叠”只有 5 分、合集成员到不了自动关联）；合集成员的 `contains` 关系要求成员满足 R22，夹具合集页面署名两位成员各自的导演；`FORCE_MERGE` 增加 Multi Hint Saga（标题年份 1999/2001 的多 tconst 线索所在合并，任务 4 因年份冲突不会自动合并这两商品）。

## 实现中的修订（T6-02，2026-10-30）

7. **`cooccur.resolve` 补齐共现命中作品行**：`World` 预取的 `imdb.title` 行只含任务 4 关联与线索的 tconst（`involved`），共现命中的 tconst（如夹具的 Ambi Two）取不到 `start_year`，导致消歧比较全部落空（一律 `ambiguous`）与选中后不产生候选。修复为 `resolve` 开始时把本阶段键下评分记录涉及的 tconst 补进作品行映射。测试覆盖：`test_ac06_tiebreak_resolves_ambiguity`（消歧生效）与 `test_ac05_director_hit_unique_candidate`（unique 选中产生候选）。

## 实现中的修订（T6-03，2026-10-30）

8. **`product_dates` 页面标签键补 section**：标签字典的键原为 `(asin, ordinal)`，而 `task3.page_field` 的 `ordinal` 是 section 内序号，title / byline / detail_bullets 的行同为 ordinal 1 相互覆盖，导致 14 条商品日期断言全部读不到标签、误判 `unknown_date_label`（`product_release_date` 表为空，先前测试只断言“剧集无行”与“release_date 全 NULL”，恰好没暴露）。改为 `(asin, section, ordinal)` 三元组。测试覆盖：`test_ac02_semantics_and_precision`（A 的两个商品日期与标记）。

## 实现中的修订（T6-04，2026-10-30）

9. **`reviews.movie_review_times` 正确收集多商品前两早评论**：作品层计算第二早评论时，原实现仅将最早商品的评论与其余商品的第二早评论比较，漏掉了其余商品自身的最早评论（例如商品 1 最早 2000-12-01，商品 2 最早 2001-03-02、第二早 2001-05-10 时，第二早应为 2001-03-02 而不是 2001-05-10）。改为收集各 edition 商品的所有前两早评论候选统一排序取第 2 条。测试覆盖：`test_ac10_movie_earliest_and_second`。
10. **`bounds.build_bounds` 上界评估范围覆盖全部有评论或商品发售日期的作品**：原实现仅遍历有评论或有 `media_release` 的作品，对于仅有 `Date First Available`（`listing_first_available`）且无评论的作品未写入评估记录；按 R23 要求其仍是有发售日期的作品，写入评估记录（`has_review_evidence = False`、`has_media_evidence = False`、`chosen_evidence = None`）。测试覆盖：`test_ac11_listing_first_available_not_media_bound`。
11. **夹具隔离评论与反例修正**：
    - `_task6_helpers.py`：原夹具通过 `score=b"6"` 试图生成隔离评论，但任务 1 中分值错误为警告而非隔离，导致隔离评论误入 `review_staging`；改为向正文字节尾部追加 `\x00`（`NUL_BYTE` 错误），确保正确进入 `quarantine_record` 且不入有效评论。测试覆盖：`test_ac10_movie_without_reviews`、`test_ac11_m4_media_before_release_year`（避免 CLS001 混入评论矛盾）。
    - `world.py`：`B0T6ANC001` 补充 365 天内的第二条评论满足 B3，使反例专门针对 B5（1970 早于 1997）。测试覆盖：`test_ac11_b5_ancient_review_time`。

## 审查发现与修复设计（T6-08 至 T6-11，2026-10-30）

针对独立审查报告（[review](review.md)）发现的 12 项问题（ISSUE-01 至 ISSUE-12），按模块对症制定如下修复方案：

1. **共现反查标题键排序与完整遍历（修复 ISSUE-04，关联 R09、R10、AC21）**：
   - `cooccur._title_keys_of`：原 SQL `ORDER BY tconst, key_source, aka_ordering` 按 ASCII 字母序把 `'aka'` 排在 `'original'` 与 `'primary'` 之前，导致正式库 1,581 条标题命中 100% 记为 `aka`。改为 `ORDER BY tconst, CASE key_source WHEN 'primary' THEN 1 WHEN 'original' THEN 2 ELSE 3 END, aka_ordering, source_line`。
   - `cooccur.score_movies`：原双层循环在遇到第一个满足 `cooccur_title_ok(tk, p)` 的 `(tk, p)` 对时，即使 `cooccur_number_guard(tk, p)` 为 `False` 也立即 `break`，导致带编号续集（如 `mv_02f5be7f1018409b` 对应 `tt0132451`，先命中无编号别名 `curse of the puppet master`）跳过后面带编号的合法键 `puppet master 6` 而被误杀。改为：先遍历寻找同时满足 `cooccur_title_ok(tk, p)` **且** `cooccur_number_guard(tk, p)` 的首个 `(tk, p)` 对；若存在则 `title_ok = True, guard_ok = True`；若不存在任何同时满足两者的键，但存在仅满足 `cooccur_title_ok` 的键，则记录首个 `title_ok = True, guard_ok = False` 的排除证据。
2. **共现反查单实词别名保护（修复 ISSUE-05，关联 R09、AC21）**：
   - `rules.cooccur_title_ok`：原逻辑仅判断 `words <= content_words(product_key)`，导致 IMDb 单实词简称/外文别名（如 `legend`、`wallace`、`beatles`、`gambler`）跨作品子集匹配同一导演/主演的另一部含更多核心实词的作品（如 `New Legend of Shaolin`、`Wallace and Gromit: The Wrong Trousers`、`Beatles' Biggest Secrets`、`Kenny Rogers The Gambler: The Adventure Continues`）。
   - 修复为：当 IMDb 标题键实词数为 1（`len(words) == 1`）时，仅允许与同样只有 1 个实词的商品标题键精确相等匹配（`words == p_words`，载体/冠词等停用词已由 `COOCCUR_STOPWORDS` 剥离），禁止单实词别名作为子集跨作品匹配含其他额外实词的多词商品标题；保留 `len(words) >= 2` 且实词总字符数 `>= 6` 的标题键按子集匹配（`words <= p_words`，受 `cooccur_number_guard` 约束）。
3. **共现阶段键纳入 `RuleSet` 评分参数（修复 ISSUE-08，关联 R27、R28、AC21）**：
   - `run.Inputs.stage_key("cooccur")`：原实现只拼入常量 `COOCCUR_PARAM_VERSION`（`"t6c1"`），未包含 `RuleSet` 的 `cooccur_director_points`、`cooccur_actor_points`、`cooccur_min_score`。改为从 `ruleset(self.rule_version)` 读取这三个参数拼入阶段键（如 `f"{COOCCUR_PARAM_VERSION}:{r.cooccur_director_points}:{r.cooccur_actor_points}:{r.cooccur_min_score}"`），确保只改 B3 天数等下游参数时 `cooccur` 仍复用，而修改共现评分参数时触发 `cooccur` 重算。
4. **报告统计、版本差异、只读查询、核对脚本与 DAG 警告（修复 ISSUE-06、ISSUE-09、ISSUE-10、ISSUE-12，关联 R24、R27、R30、R33、R34、AC22）**：
   - `report.py`：
     - 将 `source_ref->>'titleType'` 修正为 `source_ref->>'title_type'`；
     - 用独立 `SELECT COUNT(*)` 统计 `candidate_shared_count`，不直接对 `LIMIT 20` 的样本列表取 `len()`；
     - 当显式传入 `--run-id` 且在 `task6.run` 中查不到时直接抛出 `ValueError`（拒绝用虚假的 `run_id` 覆盖报告元数据）；未传 `--run-id` 按 `release_version_id` 查找时限定 `AND status = 'succeeded' ORDER BY started_at DESC LIMIT 1`，防止取到 `counts` 只有 `{"reused_run": ...}` 的二次复用运行；
     - 查询 `release_candidate`、`hint_use`、`cooccur_score`、`cooccur_result`、`review_year_mention`、`product_release_date` 时均按该版本对应的 `stage_key` / `parser_version` 过滤；
     - `diff_versions` 对 `release_selection` 与 `inferred_release_bound` 均改为 `FULL OUTER JOIN`，完整报告跨版本新增或移除的作品与推断上界。
   - `query.py`：`query_movie` 补充查询 `task6.product_review_time`（返回 `product_reviews` 列表），在 `cooccur.scores` 中补充返回 `matched_title_key`、`matched_asin`、`key_source`、`title_key_source_line`、`number_guard_detail`，并为各阶段表查询加上 `stage_key` / `parser_version` 过滤。
   - `check.py`：去掉永假的 `s.release_year::text = b.bound_time::text` 条件，改为核查非 `missing` 选择记录的 `selected_candidate_id` 存在且年份一致，以及 `cooccur_matched` 作品对应的 `cooccur_score.passed = true` 且 `cooccur_result.outcome IN ('unique', 'resolved_by_review_year')`。
   - `dags/amazon_movies_task6_release.py`：优先从 `airflow.sdk.exceptions` 导入 `AirflowFailException`，消除 Airflow 3 弃用警告。
5. **评价集分层、盲测导出与非循环 AI 草稿（修复 ISSUE-01、ISSUE-02、ISSUE-03、ISSUE-11，关联 R36–R39、AC23）**：
   - `evaluate._classify_movie_stratum`：改为读取 `source_ref.get("title_type")`、`disagreement.get("year_diff_1")`、`disagreement.get("year_disagreement")`、`disagreement.get("tconst_disagreement")`。
   - `evaluate._fetch_product_details`：改为关联 `task4.field_assertion`（`entity_type = 'product_edition' AND field_name = 'title'`）读取每个 ASIN 的原始商品标题 `raw_value`（保留括号里的年份供 `E5` 标注核查）。
   - `evaluate.export_eval_set`：各层按配额抽样（不足时按 `E1 → E7` 补足）后，按 `(_md5(x["movie_id"]), x["movie_id"])` 全局排序再分配 `it_0001`–`it_0120`（`task6.eval_item` 仍记录真实 `stratum` 供导入后算分，但导出的 `eval_items.csv` 与 `eval_items_draft.csv` 行顺序与 `item_id` 不再按层扎堆，彻底消除行号区间对分层、状态和上界有无的泄露）。
   - `evaluate._generate_ai_draft`：彻底移除对 `item["system_year"]`、`item["status"]`、`item["bound_year"]` 的读取，改为只根据作品标题、商品原始标题断言、页面导演/演员断言以及候选 `tconst` 在 `imdb.title` / `imdb.title_director` / `imdb.title_principal` / `task4.imdb_title_key` 中的公开记录做独立核查：
     - 若有 `tconst`：查询 `imdb.title` 的 `primary_title`、`original_title`、`start_year` 及演职员姓名，独立检查商品标题与演职员是否与该 `tconst` 吻合；吻合时填 `ai_true_year = str(start_year)`、`ai_tconst_label = "same"`、`ai_doubt = False`；若标题或演职员存在冲突（如单实词错配或年份冲突），标 `ai_tconst_label = "different"` 或 `ai_doubt = True`；
     - 若无 `tconst`：从商品原始标题断言中用正则提取括号四位年份 `(19xx|20xx)`，若各商品括号年份一致则填入 `ai_true_year`、`ai_tconst_label = "na"`；否则保持 `ai_true_year = ""`、`ai_tconst_label = "na"`、`ai_doubt = True`，提示人工核查（严禁用 `product_release_date`、`movie_review_time`、`review_year_mention` 或 `inferred_release_bound` 填充 `ai_true_year`）。
