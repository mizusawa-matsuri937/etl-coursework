# 任务 5 合成夹具

全部为手写的合成数据（非真实商品和人物），预期值按 spec 与 design 手算，不由实现生成。IMDb 一侧由 `tests/fixtures/task4/imdb_files.build_snapshot` 在测试时现场写成 `.tsv.gz` 并装载，任务 4 映射版本由测试运行 `etl_task4.runner.execute_match` 得到，所以任务 5 读到的都是真实的任务 4 结构。

## `people.py`：人物提及（AC01、AC02 的入库部分）

商品 ASIN 以 `B0T5M00` 开头；映射结果（已核对）：A01、A02 关联 Casablanca（tt0000301），B01 关联 Men in Black（tt0000302），K01 是合集（成员 Alien tt0000303、Aliens tt0000304 都关联），C01、D01、E01、F01 为 `page_only`，T01 是剧集。

| 商品 | 内容 | 提及 |
|---|---|---|
| A01 | 署名行导演 “Curtiz, Michael”、演员 “Bogart, Humphrey”“Ingrid Bergman”；详情要点导演 “Michael Curtiz”（有署名行导演，不读）、演员三人；编剧、制片署名 | 6 |
| A02 | 署名行导演、两位演员 | 3 |
| B01 | 署名行 “Tommy Jones”“Will Smith”；详情要点四位演员含 “Robert Downey, Jr.” | 7 |
| C01 | 署名行导演 Pat Poe、演员 Ann Lee | 2 |
| D01 | 没有署名行，详情要点导演 “Sylvester Stallone”、演员两位 | 3 |
| E01 | 署名行导演和演员都是 “Various”；详情要点演员 “Arlt, Lewis, Brown, Blair” | 6（placeholder 2、ambiguous 4） |
| F01 | 署名行演员 “Robert Wilson (III)” | 1 |
| K01 | 合集，署名行两位导演、两位演员（Lance Henriksen 不在任何成员的 IMDb 演职员里） | 4 |
| T01 | 剧集，不参与匹配 | 0 |

合计 32 条提及：resolvable 26、placeholder 2、ambiguous 4；导演 8、演员 24；署名行 18、详情要点 14；有任务 4 断言的 21（署名行全部加 D01 的详情要点 3）。

## `links.py`：外部 ID（AC03、AC04）

- `CREDITS_IMDB` / `credits_products()`（ASIN 前缀 `B0T5L00`）。A01 Calm Harbor：Pakula（X3）、“Bogart, Humphrey”（X1）、Tommy Jones（X3）、Thorton（X4 复核）、Ann Strict（X1）；B01 Gray Lantern：替换字符的 Girard（X4 复核）、Co Director（X1）、两位 Robert Young（复核 multiple_in_credits）、Cyd（X1）；C01 Silver Mirror：Kay Real（X1）、Dee Cross 只是 IMDb 导演（cross_role 复核）、Ann Actor（X1）、Andy Wachowski 与作品之外的 Zed Outside 没有候选；D01 没有 IMDb 关联。13 个候选：auto 8、review 5。
- `FILM_IMDB` / `film_products()`（前缀 `B0T5F00`）。V01、V02 关联 IMDb，核实 Ava Verified（两个同名 nconst）与 Bea Verified；U01–U08、K01 的页面写了与 IMDb 导演不同的 “Wrong Person”，任务 4 因导演不相交否决关联，保持 `page_only`。U01 主标题命中（auto 0.85）；U02 只命中别名（auto 0.80）；U03 两部同名作品、U04 两个同名已核实人物（review）；U05 从未核实、U06 角色不同（无候选）；U07 年份相差 3 年（review）；U08 两位演员指向同一作品（auto + 升级线索）；K01 合集第 2 个成员命中（auto，成员序号 2）。

## `resolve.py`：归并（AC05、AC06、AC07）

- `case_products()`（前缀 `B0T5R00`）：A 同一作品两个版本；B 导演兼演员；C 两部作品同名演员、无合作者；D 两位同名合作者；E 同名但两个 nconst；F 年代冲突（nconst 1980 年出生，页面作品 1950 年）；G 链（X 无 ID，Y、Z 各关联不同 nconst，三者有共同合作者）；H “J. Smith” 对 “John Smith”。
- `iter_products()`（前缀 `B0T5I00`）：作品 P、Q、R 关联 IMDb，P–Q 共同的已核实演员 Vee One/Two，P–R 共同的 Vee Three/Four；同名演员 Ann Alpha 三部都有，Bea Bee 只在 Q、R 且没有其他共同点。预期迭代 3 轮，合并边 2、1、0。
- `switch_products()`（前缀 `B0T5S00`）：稀有名 + 同名合作者的一对（Rora Rare / Shared Mate），发行商相同的一对（Pam Pub）。

## `movie_person.py`：MoviePerson（AC08）

- `MP_IMDB` / `mp_products()`（前缀 `B0T5P00`）。Harbor Lights（tt0005001，导演 Dir One，演员 Ann Star、Bea Lead、Cy Support）有两个单品 W01、W02，也是合集 K01 的第 1 个成员；Harbor Nights（tt0005002，导演 Dir Two，演员 Dan Other）只在合集里。
- W01：署名行导演 Dir One，演员 Ann Star（第 1）、Bea Lead（第 2）；详情要点演员 “Ann Star, Bea Lead, Cy Support, Dir One”（Dir One 是导演兼演员）。W02：署名行顺序相反，Bea Lead 第 1、Ann Star 第 2。K01：署名行导演 Dir One、Dir Two，演员 Ann Star、Dan Other、Zed Nobody（Zed Nobody 不在任何成员的 IMDb 演职员里；Dan Other 让第 2 个成员也有演员佐证，任务 4 才会把它关联到 tt0005002）。
- 预期（手算）：Harbor Lights 的 `director` 为 Dir One（署名顺序 1，三个来源）；Dir One 同时有 `cast`（只有详情要点来源，署名顺序 NULL），所以是多角色人物；Ann Star 的 `starring` 来源为 W01 第 1、W02 第 2、K01 第 1，取最小 1 并标 `order_conflict`，`cast` 另有 W01 详情要点来源，标签含 `byline:Actor`、`detail_bullets:Actors`、`collection:B0T5P00K01`；Bea Lead 的署名顺序在 W01、W02 为 2、1，取 1 并标冲突；Cy Support 只有 `cast`、署名顺序 NULL、没有 `starring`。合集里 Dir Two 经 `contains` 归到 Harbor Nights（`director`，署名顺序 2，标签含 `collection:B0T5P00K01`），Dan Other 同样归到 Harbor Nights（`starring`/`cast`，署名顺序 2）；Zed Nobody 为 `unattributed`，人物存在、没有关系。
