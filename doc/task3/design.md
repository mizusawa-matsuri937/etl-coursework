# 任务 3 设计思路（电影页面筛选）

状态：2026-10-29 编写计划；同日用户同意假设按建议执行，并实现、测试、正式运行。本文按实际实现更新。需求、可观察行为与 AC 以 [spec](spec.md) 为准；本文说明如何实现与为什么这样选。

## 整体思路

任务 3 是“原始层 → 暂存层”的第一步：把任务 2 的页面快照（原始层）变成结构化的抽取字段和分类结论（暂存层），为任务 4 以后的合并与补全准备输入。整条链路不发网络请求，只读 `task2` 的清单、尝试日志和本地快照。

分三层做，每层结果都入库、都可追溯：

1. **输入分组**（R03、R04）：用一条 SQL 把清单 253,059 行按状态、最后一次尝试的模板和最终域名分成 8 组。只有 `dp_page` 组（20,661 个）往下走；其余七组直接写待确认分类，依据是清单状态与尝试日志。这样“每个产品都有分类”由构造保证，分母永远是全部产品。
2. **页面抽取**（R05–R07）：逐个解压快照、核对哈希，只在 7 个证据区域内抽字段，原样存入 `task3.page_field`。抽取不做判断，只回答“页面上写了什么、写在哪”。
3. **规则分类**（R08–R17）：只读 `page_field`，按固定优先级的规则得出类别、证据级别、命中规则和信号。规则改动只需重跑这一层（R25）。

抽取与分类分开的理由：抽取是慢的一步（解压 + 解析约 1 MB 的 HTML），分类是快的一步（读几十行字段）。分开后改规则不用重新解析 18 GB 的页面，而且两个版本的差异可以直接用 SQL 比较；代价是多一张字段表（估计 20 万行量级）。

由 Airflow 编排检查、解析与分类、核对、报告和门禁，与任务 1、任务 2 的 DAG 同构（R27）。

## 输入依据与规划时快照

- 任务依据：作业任务 3 与第五、八、十部分；交接文档 `doc/handoffs/2026-10-29-task2-part2-to-task3.md` 第 2–6 节。
- 输入分组（2026-10-29 正式库只读查询，合计 253,059）：

  | 输入组 | 条件 | 数量 |
  |---|---|---|
  | `dp_page` | `success`、最终地址 `https://www.amazon.com/`、最后一次尝试 `page_template='dp_product_title'` | 20,661 |
  | `automation_title` | `success`、模板 `automation_title` | 1,493 |
  | `other_site` | `success`、最终地址不是 `www.amazon.com`（co.jp 160、sg 7） | 167 |
  | `access_restricted` | 状态 `access_restricted`（captcha 3,310、checkpoint 730、service_error 112） | 4,152 |
  | `not_found` | 状态 `not_found`（含 1 个 amazon.sg 的 404） | 2,378 |
  | `timeout` | 状态 `timeout` | 213 |
  | `parse_failed` | 状态 `parse_failed` | 62 |
  | `not_collected` | 状态 `not_collected` | 223,933 |

- 评论口径（同日只读查询）：任务 1 读取 7,911,696 条；清单 `records_total` 之和 7,911,684，差的 12 条是没有产品标识的隔离记录；有效 7,849,565、隔离 62,131（其中带 ASIN 的 62,119）。R22 的核对式为“按类之和 7,911,684 + 无产品记录 12 = 7,911,696”。
- 全量只读探查（2026-10-29，全部 20,661 个 `dp_page` 快照，哈希全部一致，用时约 7 分钟）：面包屑首级 `Movies & TV` 19,557、`CDs & Vinyl` 700、无面包屑 155、Electronics 98、Books 49、其他 102；有 Genre 6,535、有 Director 9,216、有片长 13,335。用下文默认规则的原型估计：电影 9,580（结构化 6,560、推断 3,020）、待确认 7,468（证据不足 7,412、无视频证据 56）、演唱会或其他 1,561、合集 1,205、剧集 787、非电影 60。
- 规则开发样本（同日，`dp_page` 按 `md5(asin)` 排序的前 1,500 个；规则模式只按这部分页面观察调整）：

  | 观察 | 数量 / 1,500 |
  |---|---|
  | 面包屑首级 `Movies & TV` | 1,414（第二级 Featured Categories 1,398，第三级多为 DVD 914、Blu-ray 59） |
  | 面包屑首级为其他（CDs & Vinyl 52、Electronics 4、Video Games 2 等） | 72 |
  | 无面包屑 | 14 |
  | 有商品概览 `#productOverview_feature_div` | 1,498（其中有 Genre 503、Format 966、Runtime 696） |
  | 有商品详情要点 | 1,491（Studio 1,186、Actors 1,060、Release date 1,031、Media Format 1,008、Run time 965、Number of discs 882、MPAA rating 800、Director 701） |
  | 旧版商品详情表 `#productDetails_*` | 9 |
  | 署名行 `#bylineInfo`（形如 “X (Actor), Y (Director) Rated: R Format: DVD”） | 1,482 |

  - 有 4 个页面的概览 Genre 单元格里混入了一段内联脚本文本，这是 R06 要排除 `script` 的直接原因。
  - 原型（不是实现）在开发样本上：电影 709（结构化 511、推断 198）、待确认 520、演唱会或其他 123、合集 77、剧集 66、非电影 5。待确认的大多是只有标题和载体的旧 VHS 页面，例如 “Hoffa [VHS]”，页面上没有任何作品级字段。
  - 原型暴露的已知误判：教学片（“Introduction to the NIKON D70…”）、单口喜剧有导演和正片片长时会被判为电影；双碟组合装的片长是两倍（Brokeback Mountain 显示 4 小时 30 分），落入待确认；动画剧集分卷（“Basilisk, Vol. 2”）可能被判为电影；电视电影（“The Tenth Circle”，类别标签含 Television）按优先级落入剧集。这些在人工抽查里度量，不为凑一致率去逐个补特例。
- 以上都是规划参考；全量分布以 T3-04 正式运行（AC11）为准。
- 正式运行实测（2026-10-29，`t3_full_1`，规则 `t3r1`）：电影 8,808（MV1 6,072、MV2 2,736）、演唱会或其他 3,256、合集 861、剧集 835、非电影 25、dp_page 内待确认 6,876（PD1 6,814、PX2 43、PX1 19）；dp_page 识别覆盖率 66.72%，全部产品 5.45%。比原型估计的待确认少、演唱会或其他多，主要因为面包屑第 4 级的类别标签（Special Interests 等）参与了判定。
- 抽查表导出后浏览时注意到的问题（来自评价样本，按开发与评价分开的原则不改 `t3r1`，留作 `t3r2` 候选，由人工抽查度量）：标题里的载体列表 “(Blu-ray 3D / Blu-ray / DVD)” 命中合集的 “ / ”；占位署名 “Artist Not Provided” 被当作演员；归在 CDs & Vinyl 下、没有载体字段的影片（“Fire (A Deepa Mehta Film)”）判为非电影。

## 组件与职责

| 组件 | 职责 |
|---|---|
| `sql/201_task3_schema.sql` | 创建 schema `task3` 与下文各表、约束、索引；登记迁移 `201` |
| `sql/task3_input_groups.sql` | 以 `extraction_id` 为参数，给出每个 ASIN 的输入组、快照路径、哈希、最终地址、采集时间、尝试序号 |
| `src/etl_task3/extract.py` | 纯函数：HTML 字节 → 区域字段列表（lxml，先删 `script`/`style`）；标签映射表与 `LABEL_MAP_VERSION` |
| `src/etl_task3/rules.py` | 纯函数：字段列表 → 分类结果（类别、证据级别、`rule_id`、信号、冲突信号）；模式、阈值与 `RULE_VERSION` 集中在此 |
| `src/etl_task3/classify.py` | 输入分组、分批读取快照与写库、幂等与锁、规则版本差异、核对 |
| `src/etl_task3/report.py` | 统计 SQL 的组织、`summary.json`、`report.md`、CSV 导出；抽查表导出与标注导入 |
| `src/etl_task3/pipeline.py`、`dags/amazon_products_task3_classify.py` | DAG 步骤函数与编排 |
| `src/etl_task3/check.py` | 正式库只读核对命令（AC11），供人工复核 |
| `tests/fixtures/task3/` | 合成页面、清单行与预期值说明 |

复用而不复制：`etl_task1.config.load_settings`、`etl_task1.db.connect` / `advisory_key`、`etl_task1.reporting.write_atomic` / `file_digest` / `utc_now`、`etl_task1.pipeline.is_transient` / `gate_decision`、`etl_task2.pipeline` 的 `run_step` 与失败文件写法、`etl_task2.crawl.crawler.resolve_snapshot`（快照路径相对 `/opt/etl`，交接文档第 9 节）。

## 数据模型

全部表在 schema `task3`，外键指向 `public.extraction` 与 `task2.product_manifest`，不在任务 1、任务 2 的表上加列或约束。

**`task3.classify_run`**：每次运行的审计。

- `run_id`、`extraction_id`、`parser_version`、`label_map_version`、`rule_version`、`dag_id`、`airflow_run_id`、`status`（`running`/`succeeded`/`failed`/`reused`/`interrupted`）、`params`（jsonb：`limit`、`batch_size`）、`parsed`、`classified`、`started_at`、`finished_at`、`failure_reason`。

**`task3.page_parse`**：每个 `dp_page` ASIN、每个解析版本一行。主键 `(extraction_id, asin, parser_version)`。

- `content_hash`、`snapshot_path`、`final_url`、`fetched_at`、`attempt_no`（复制自任务 2，便于证据直接定位）。
- `parse_status`：`parsed`、`snapshot_missing`、`snapshot_hash_mismatch`；`sections_present`（jsonb，7 个区域各自是否存在）；`field_count`；`run_id`。
- 约束：`parse_status <> 'parsed' OR field_count >= 0`；非 `parsed` 时不得有字段行（由写入逻辑保证，核对步骤复查）。

**`task3.page_field`**：抽取字段，一个值一行。主键 `(extraction_id, asin, parser_version, section, ordinal)`。

- `section`：`title`、`breadcrumb`、`detail_bullets`、`product_overview`、`product_details`、`byline`、`format_swatch`。
- `ordinal`：区域内从 1 开始的顺序（面包屑按层级，详情按列表顺序）。
- `label_raw`（标题、面包屑为 NULL）、`value_raw`、`field_key`（标签映射结果，未知标签为 NULL）。
- 不另设字段 ID：区域 + 序号在一个快照内唯一且稳定，配合 `content_hash` 就能回到原页面位置。

**`task3.product_classification`**：每个 ASIN、每个规则版本一行（含七组无页面的行）。主键 `(extraction_id, asin, rule_version)`。

- `input_group`、`category`、`basis_level`（`structured`/`inferred`/`missing`）、`rule_id`、`pending_reason`、`parser_version`（无页面行为 NULL）、`run_id`、`classified_at`。
- 约束：`category` 六值；`(category = 'pending') = (basis_level = 'missing')`；`(category = 'pending') = (pending_reason IS NOT NULL)`；`input_group <> 'dp_page'` 时 `category = 'pending'`。

**`task3.classification_evidence`**：每条信号一行。主键 `(extraction_id, asin, rule_version, seq)`。

- `signal`（如 `breadcrumb_root`、`video_carrier`、`tv_genre`、`series_title`、`director`、`feature_runtime`、`film_rating`）、`supports`（该信号指向的类别）、`used`（是否为命中规则的条件；为假即冲突证据）。
- 页面信号：`section`、`ordinal` 外键到 `page_field`，另存 `value_raw` 的副本与解析出的数值（如片长分钟）以便直接阅读。
- 非页面信号存在 `source_ref`（jsonb）里：无页面行为 `signal='manifest_status'`，存清单状态、`status_reason`、最后一次 `attempt_no`、页面模板与最终地址，可连到 `task2.fetch_attempt`；快照异常行为 `snapshot_check`；页面上没有任何信号时为 `no_signals`（见“分类规则”）。

**`task3.quality_issue`**：`(extraction_id, asin, parser_version, issue_code)`，记录快照缺失、哈希不一致、证据区域全部缺失等，带 `detail`（jsonb）。

**`task3.manual_review`**：人工抽查。主键 `(extraction_id, asin, rule_version)`。

- `auto_category`（导出时的自动结论副本）、`human_category`、`note`、`reviewer`（用户填写的姓名或代号）、`reviewed_at`、`sample_stratum`、`import_file_hash`。
- 只存人工结论，不回写 `product_classification`（R16、R28）。

估计规模：字段约每页 30–40 行，全量约 70–80 万行；分类 253,059 行；证据约每个非待确认产品 4–6 行。都在几百 MB 以内。

## 字段键与标签映射（R07、R20、R21）

标签映射是一张常量表，版本 `LABEL_MAP_VERSION`。规划时的映射（开发样本里出现的标签）：

| 字段键 | 来源（区域：标签） | 说明 |
|---|---|---|
| `title` | title | 商品标题，含载体后缀，不清洗 |
| `breadcrumb` | breadcrumb（按序号） | 首级用于判断商品大类 |
| `genre` | product_overview: Genre | 多值保留原串，不拆分 |
| `media_format` | detail_bullets: Media Format；product_overview: Format；byline 中 “Format:” 之后；format_swatch 选中项 | 载体与画面格式 |
| `runtime` | detail_bullets: Run time；product_overview: Runtime | |
| `director` / `actors` / `writers` / `producers` | detail_bullets: Director / Actors / Writers / Producers；byline 中带 (Director)/(Actor) 的署名 | 本任务只存原串，人物实体在任务 5 |
| `contributor` | product_overview: Contributor | 不分角色的署名 |
| `mpaa_rating` | detail_bullets: MPAA rating；byline 中 “Rated:” 之后 | |
| `studio` | detail_bullets: Studio；product_overview: Studio / Manufacturer | 发行商，属于版本信息 |
| `language` / `subtitles` / `dubbed` | detail_bullets 同名；product_overview: Language | |
| `number_of_discs` | detail_bullets: Number of discs；product_overview: Number Of Discs | |
| `aspect_ratio` | detail_bullets: Aspect Ratio | |
| `product_release_date` | detail_bullets: Release date / Date First Available / Publication date；product_overview: Publication Date | 商品发售日期候选，不是上映日期（R21） |
| `upc` | detail_bullets: UPC；product_overview: UPC | 版本标识，全量探查 3,288 页 |
| `edition` | detail_bullets: Edition | 剪辑版、纪念版等文字，37 页 |

- 署名行要拆成多个字段行：按 “名字 (角色)” 切分；“Rated:”“Format:” 各自一行。拆分只按页面上的固定标记，不猜测。
- 一个字段键可以有多个来源行，覆盖率按“该产品至少有一行非空值”计。
- 新标签出现时只加映射、升 `LABEL_MAP_VERSION`，不需要重新解析（映射在写分类前用 SQL 视图重算即可）。为了简单，第一版把 `field_key` 物化在 `page_field` 上；升映射版本时用一条 UPDATE 重算，并在运行审计里记录。

## 与作业第五部分字段的对照

任务 3 只把页面上有的值连同出处原样存下（R07、H09），补全与规范化在后续任务完成；查不到的保留 NULL 与缺失原因。

| 第五部分字段 | 任务 3 提供的原始证据 | 由谁完成 |
|---|---|---|
| source_review_id、reviewer_id、profile_name、helpfulness、score、review_time、summary、review_text | 不涉及 | 任务 1 已完成 |
| product_id / edition_id | 分类与字段都以 ASIN 为键 | ASIN 已保留；edition_id 在任务 4 定义 |
| movie_id | 电影类产品清单（ASIN 级） | 任务 4 |
| title / original_title | `title`（含载体后缀的商品标题） | 任务 4 标准化与匹配；原名、别名需第三方数据（任务 6） |
| release_date / release_year | `product_release_date`（仅商品发售日期候选） | 任务 6；页面上没有电影上映日期 |
| genres | `genre` 原串 | 任务 4–6 定义受控词表与多值拆分 |
| directors、starring / cast | `director`、`actors`、`contributor`；署名行按页面顺序逐个保存 | 任务 5 建 person_id 与主演判定 |
| edition | `media_format`、`language`、`subtitles`、`dubbed`、`number_of_discs`、`aspect_ratio`、`studio`、`upc`、`edition`、`product_release_date` | 任务 4 组成结构化版本；地区码在页面上没有单独字段，只零星出现在标题里 |

## 分类规则（R09–R15）

规则在 `rules.py` 里写成有序列表，每条有 `rule_id`、类别、证据级别和条件。下表是第一版（`RULE_VERSION = t3r1`）的默认取值；数值阈值来自开发样本的观察，不是调优结果。

先由字段算出信号。“类别标签”指概览 Genre 按逗号、斜杠切开的各项，加上面包屑第 2 级起的各级（全量探查中第 4 级有 Drama 1,579、Special Interests 1,121、Music Videos & Concerts 733、Exercise & Fitness 432 等，覆盖面比 Genre 广）；标签一律小写后精确比较。

| 信号 | 指向 | 定义 |
|---|---|---|
| `breadcrumb_root` | 首级不是 Movies & TV 时指向 non_movie | 面包屑序号 1 的值 |
| `video_carrier` | 中性 | `media_format` 任一来源含 DVD、Blu-ray、VHS、HD DVD、UMD、LaserDisc、Prime Video 或电视制式 NTSC、PAL（“DVD Audio” 除外）；或面包屑第 2 级起有载体名 |
| `tv_category` | tv_series | 类别标签为 TV、Television、TV Series、Reality TV、TV News Programming |
| `concert_category` | concert_other | 类别标签为 Music Video(s) & Concerts、Exercise & Fitness、`exercise_` 前缀、Special Interest(s)、Sports、Performing Arts、Instructional、Safety Training DVDs & Videos |
| `film_category` | movie | 类别标签为 Drama、Comedy、Action & Adventure、Horror、Mystery & Thrillers、Romance、Science Fiction、Fantasy、Westerns、Military & War、Crime、Animation、Kids & Family、Documentary、Musicals、Foreign Films 等 |
| `series_title` | tv_series | 标题含 Season(s) + 数字或序数词、Complete … Series/Season、Episode(s)、TV Series、Mini-Series |
| `concert_title` | concert_other | 标题含 Live at/in/from、in Concert、Concert、Unplugged、Workout、Yoga、Pilates、Aerobics、Fitness、Learn to、How to、Instructional、Lesson(s) |
| `collection_title` | collection | 先删掉 “(The) Criterion Collection”，再匹配 Double/Triple Feature、Trilogy、Quadrilogy、Anthology、N-Movie/N Film(s)、Movie/Film Collection、Collection、Box Set、N-Pack，或标题含 “ / ” |
| `box_set`、`multi_disc` | collection | `media_format` 含 Box set；`number_of_discs` ≥ 2。只记信号，不单独构成合集 |
| `director`、`actors` | movie | 详情要点或署名行有对应署名 |
| `feature_runtime` / `other_runtime` | movie / 中性 | 片长解析为分钟后在 40–240 之间 / 之外 |
| `film_rating` | movie | 分级以 G、PG、PG-13、R、NC-17、X 开头（NR、Unrated 不算） |

再按顺序匹配（“作品级信号”指 `director`、`actors`、`film_rating`、`film_category`）：

| rule_id | 类别 / 级别 | 条件 |
|---|---|---|
| `NM1` | non_movie / structured | 首级存在且不是 Movies & TV，无 `video_carrier`，也没有 `series_title`、三种类别标签、`director`、`actors` |
| `PX1` | pending（`conflicting_evidence`） | 同上但有其中任一信号 |
| `PX2` | pending（`no_video_evidence`） | 无面包屑且无 `video_carrier` |
| `TV1` | tv_series / structured | `tv_category` |
| `TV2` | tv_series / inferred | `series_title` |
| `CO1` | concert_other / structured | `concert_category` |
| `CO2` | concert_other / inferred | `concert_title` |
| `CL2` | collection / inferred | `collection_title` |
| `MV1` | movie / structured | `director` + `feature_runtime` + (`actors` 或 `film_rating` 或 `film_category`) |
| `MV2` | movie / inferred | `feature_runtime` + 至少 1 项作品级信号；或完全没有片长字段但至少 2 项作品级信号 |
| `PD1` | pending（`insufficient_evidence`） | 以上都不满足 |

规则表以外的三类 `rule_id`：`NP1`（无页面的七组，依据是清单状态与最后一次尝试）、`SN1`/`SN2`/`SN3`（快照缺失、哈希不一致、解析异常，依据是快照检查结果）。页面上一个信号都没有时（例如只有标题、没有面包屑），记一条 `no_signals` 证据，写明抽到的字段数与字段键，保证每个分类都有可查的依据——这一点是测试库端到端运行时发现的：任务 2 的离线假页面只有标题，起初这类待确认行没有任何证据，核对失败。

实现时在开发样本上做过以下修正（都有页面实例为据）：
- **Blank Video Media 不是空白带**：这个面包屑下的 4 个开发样本都是电影（“Marathon Man [VHS]” 等），原拟的“非视频类别 → 非电影”规则删除。
- **非视频首级不等于非电影**：“Ocean Girl: Season 2” 被归在 Cell Phones & Accessories 下，格式只有 PAL。因此电视制式也算视频载体，首级不是视频类但有剧集、类别、署名信号的判为冲突待确认（`PX1`），只有什么视频信号都没有才判非电影。
- **Box set 多碟不是合集**：“Battle History of the USMC: Korea”“Thorn Birds [VHS]” 都是单部纪录片或迷你剧，原拟的 `CL1`（Box set + 多碟 → 结构化合集）删除，合集只来自标题模式，因而只有推断级。
- **内嵌商品类型不采用**：页面脚本里有 `productType = 'PHYSICAL_MOVIE' / 'PHYSICAL_TV_SERIES'`。开发样本里标为 TV_SERIES 的页面混有瑜伽、演唱会、纪录片和电影（“Waitress!”），噪声太大；而且它在脚本里，按 R06 也不取。

- **面包屑首级不决定视频节目**：全量探查中首级为 `CDs & Vinyl` 的 700 页大多是 VHS/DVD 影片（如 “Toy Story (Spanish Edition) [VHS]”、“Panic in the Streets”），起初“音乐类首级 + 视频载体 → 演唱会”的写法因此放弃。有视频载体就按视频节目往下判，首级不是 Movies & TV 时记一条 `breadcrumb_root` 冲突证据；电影仍要作品级证据，所以电子产品配套光盘之类不会仅凭载体成为电影。
- **为什么用优先级而不是打分**：每个结论都能用一条规则说清楚“因为 A 且 B”，人工抽查时一眼能看出错在哪条；打分要解释权重，阈值也更难复核。代价是边界情况靠顺序裁决，例如“电视类型 + 导演 + 正片片长”判为剧集，电视电影因此会落进剧集。这类情况把电影信号记为冲突证据（`used=false`），抽查和任务 4 都能查到。
- **剧集优先于合集**：“One Tree Hill – The Complete Second and Third Seasons (2 Pack)” 同时命中两类，按剧集处理，因为作业要求剧集不得与电影混合计数。
- **演唱会优先于电影**：音乐会录像常有导演和正片片长，不先排除就会被误判为电影。
- **“证据级别”的含义**：`structured` 只说明条件全部来自页面的结构化字段，不等于已核实；作业里的“已核实”在本任务只指人工抽查一致的产品，报告分开写（R16、R19）。
- 改阈值或模式就升 `RULE_VERSION`（如 `t3r2`），按 R25 重算并比较差异。

## 规则版本 t3r2（2026-10-29）

用户复核 t3r1 通过后，修复浏览抽查表时发现的三类已知问题。模式改动集中在 `rules.py`，`RULE_VERSION` 升为 `t3r2`；解析版本 `t3p1` 不变，所以正式库只重算分类（运行 `t3_full_r2_1`，35 秒）。

| 问题（t3r1） | 修复（t3r2） | 例子 |
|---|---|---|
| 标题里的载体列表命中合集的 “ / ” | 匹配合集模式前删去只由载体与画面格式词组成的括号组（Blu-ray、3D、DVD、VHS、Digital Copy、Combo、Widescreen、NTSC、PAL、Region N 等，以 `/ + , & and` 连接） | “Happy Feet Two (Blu-ray 3D / Blu-ray / DVD)” |
| 占位署名算作导演或演员 | “Various”“Various Artists”“Artist Not Provided”“n/a”“Not Available”“Unknown”“None”“not specified”“-” 记为中性信号 `placeholder_credit`，不算作品级证据 | 全量约 1,600 处 |
| 非视频首级、无载体的影片判为非电影 | `VIDEO_HINTS` 加入标题里的载体字样 `title_carrier`、Film/Movie 字样 `film_title`、演出与合集标题模式；有这些信号就判冲突待确认（`PX1`）。这些信号只阻止误判为非电影，不作电影依据（R09） | “Fire (A Deepa Mehta Film)”“Morgan Heritage: Live in Europe 2003 [DVD]” |

版本差异（`python -m etl_task3.classify diff --from t3r1 --to t3r2`，结果在 `outputs/task3-run/diff-t3r1-t3r2.json`）：共 212 个产品变化，MV1→MV2 124、MV2→PD1 61、MV1→PD1 18（占位署名）；NM1→PX1 8；CL2→MV1 1。其余 252,847 个不变。

- **仍是非电影的 17 个**：页面首级为 CDs & Vinyl、Electronics 等，且没有任何视频信号，例如 “Dragon's Lair”“Unleashed”。其中一部分可能是 Amazon 把 ASIN 挪作他用，但页面证据只能支持非电影，不另加特例。
- **开发与评价分开**：t3r2 的三处修改来自 t3r1 抽查表，因此 t3r1 抽查样本对 t3r2 不再是独立的评价集。t3r2 抽查表按 `md5(asin || 't3r2')` 重新抽样，与 t3r1 抽查表重叠 16 个；用 t3r2 抽查结果评价时，应剔除这 16 个或单独说明。

## 数据流

1. **检查输入**（R01、R03、R29）：应用迁移 `201`；确认抽取存在且有全量清单；用 `task3_input_groups.sql` 计算 8 组的数量，要求之和等于清单行数；记录运行前状态。确定性失败：抽取不存在、清单为空、分组之和不等。
2. **解析与分类**（R04–R17、R24、R26）：
   - 取会话级 advisory lock `(etl_task3, extraction_id, rule_version)`，拿不到即确定性失败（AC08）。
   - 先一次性写七组无页面 ASIN 的待确认分类与证据（23 万行，`INSERT … SELECT`，单事务）；已存在则跳过。
   - 再按 ASIN（`COLLATE "C"`）keyset 分批取 `dp_page` 中缺少本版本分类的行，默认每批 500 个。对每个 ASIN：解析版本的 `page_parse` 已存在则直接读字段，否则读快照 → 核哈希 → 抽取。然后跑规则。
   - 一批的 `page_parse`、`page_field`、`quality_issue`、`product_classification`、`classification_evidence` 用 `COPY` 进临时表再 `INSERT … ON CONFLICT DO NOTHING`，同一事务提交。已提交的行本身就是检查点，不另设检查点表。
   - `limit` 参数限制本次处理的 `dp_page` 数量，用于试跑或用户选择的部分运行；未处理的 ASIN 没有分类行，核对步骤会如实报告未完成，门禁失败，不会把部分运行冒充全量。
3. **核对**（R02、R22）：只读事务内检查——每个清单 ASIN 恰好一行分类；六类之和等于清单行数；非待确认行至少一条 `used=true` 证据；证据外键全部有效；`parsed` 页面的字段数大于 0；评论数按类之和与任务 1 读取数一致。
4. **收尾报告**（R18–R23）：`all_done` 触发，写 `summary.json`、`report.md` 和两个 CSV；上游失败时写到 `outputs/task3/_failures/<run_key>/`。
5. **门禁**：复用 `gate_decision`，任一上游失败则 DAG failed。

规则版本差异与抽查不在 DAG 主链：

- `python -m etl_task3.classify diff --from t3r1 --to t3r2`：输出类别变化的 ASIN、新旧 `rule_id`，作为 R25 的证据。
- `python -m etl_task3.report review-export` / `review-import <csv>`：见“人工抽查”。

## 统计口径（R18–R22）

`summary.json` 的主要块，全部限定抽取与规则版本：

- `collection_status`：清单六种状态的数量、合计、成功采集率（`success / 253,059`）。
- `source_kind`：`live_crawl`（任务 2 爬虫取得并保存快照的成功页，等于 `success` 数）、`offline_snapshot`（0）、`third_party`（0），分开列出，不相加成一个指标。
- `input_groups`：8 组数量与合计。
- `classification`：六类数量；类别 × 输入组、类别 × 证据级别两张交叉表；`movie_structured`、`movie_inferred`；识别覆盖率两种分母（全部产品、`dp_page`）；`pending_reason` 分布。
- `field_coverage`：每个字段键在三种分母下的有值数与比例（电影类产品、`dp_page` 中 `parsed` 的页面、全部产品），以及缺失数。字段值全部来自页面，`inferred` 列恒为 0，列出是为了与作业口径对齐。
- `reviews_by_category`：每类的 `records_total`、`records_valid`、`records_quarantined` 之和，加上无产品标识记录数，与任务 1 读取数比对。
- `manual_review`（导入后）：每类样本数、一致数、一致率、不一致实例列表；注明只代表抽查样本。

## 人工抽查（R28）

- **抽样**：从 `dp_page` 的分类结果中，每个类别按 `md5(asin || rule_version)` 排序取前 20 个，跳过开发样本（`md5(asin)` 排序前 1,500 个）；某类不足 20 个就全取。最多 120 个。
- **导出**：`outputs/task3/<抽取>/<规则版本>/review/review_sample.csv`，每行带 ASIN、快照路径、商品链接、标题、自动类别、`rule_id`、全部 `used` 与冲突信号的原值，以及空的 `human_category`、`note` 两列。不复制整页 HTML。
- **标注**：用户对照快照（或 `original_url`）填写 `human_category`（六值之一），不确定时填 `pending` 并写备注。抽查表在 `outputs/` 下，不入库。
- **导入**：校验 ASIN 集合与导出时一致、类别取值合法，写 `manual_review` 并记文件哈希；可重复导入同一文件（同哈希不重复写），新文件覆盖同一 ASIN 的旧标注并保留时间。
- **为什么不让 AI 标注**：作业规定 AI 生成的值不能作为事实来源；抽查的意义正在于由人来判断规则是否可信。

## 异常与恢复

| 情况 | 处理 |
|---|---|
| 快照缺失或哈希不一致 | 该 ASIN 为待确认并记质量问题，不中断运行（R05） |
| 页面能解析但 7 个区域全缺 | `parse_status='parsed'`、字段只有空集合，分类落到 `PX2` 或 `PD1`，记质量问题 `no_evidence_sections` |
| lxml 解析异常 | 视为确定性错误中的个例：记质量问题、该 ASIN 待确认（`parse_error`），不中断整批 |
| 数据库暂时断连 | `is_transient` 判定，Airflow 最多重试 2 次；已提交批次保留，重试从下一批续接 |
| 进程被强制终止 | 未提交批次无部分行；`classify_run` 停在 `running`，下次运行把同版本遗留的 `running` 标为 `interrupted` 后续接 |
| 两个运行同时启动 | 第二个拿不到 advisory lock，确定性失败 |
| 规则或映射变更 | 升版本；旧版本行保留，`diff` 列出受影响 ASIN（R25） |

## 资源预算

以下为预算，不是实测：

- **耗时**：规划探查脚本（lxml 全树解析、单进程）在容器里处理开发样本 1,500 页约 30 秒，全量 20,661 页约 7 分钟（实测，未写库）；写库与分类预计再加几分钟。全量 DAG 预计 15–20 分钟。
- **内存**：一次只解压一个快照，峰值为单页量级（几 MB），加上一批字段行的缓冲；探查时容器常驻约 130 MB。
- **磁盘**：新表与索引预计几百 MB；导出 CSV 几十 MB，放在被忽略的 `outputs/`。快照只读，不复制。
- 由于全量只需十几分钟，正式运行默认处理全部 `dp_page`；`limit` 只用于试跑，或在用户选择只跑一部分时使用（spec H12）。
- 实测（2026-10-29 正式库）：`classify_products` 329 s（20,661 页、42 批，含 232,398 行无页面分类），核对 1.6 s，整次 DAG 约 5 分 40 秒；`task3` 表含索引 404 MB（字段 449,000 行、证据 438,223 行）；`classification.csv` 22,198,399 字节。

## 验证顺序

先用单元测试锁定抽取与规则（AC03、AC04），再在 `etl_test` 用合成夹具验证分组、快照异常、证据、评论汇总、幂等与恢复（AC01、AC02、AC05–AC08），然后验证 DAG 与报告（AC09、AC10，真实 scheduler 经 `compose.e2e.yaml` 写测试库），最后在正式库全量运行与只读核对（AC11），导出抽查表交用户标注（AC12），做交付检查（AC13）。

## AI 辅助说明

计划、代码与规则模式由 Claude（Claude Code）生成。验证方式：单元与集成测试、正式库只读核对脚本，以及用户按 checklist 命令复核和人工抽查标注。分类结论只来自页面证据与确定性规则，不使用 AI 判断任何产品的类别。
