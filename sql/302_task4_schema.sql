-- 任务 4：定义并识别同一电影（schema task4）。
-- 幂等：由 etl_task4.run.apply_task4_schema 通过 public.schema_migration 判断是否已应用（须先有 201 与 301）。
-- 只引用 public.extraction、task2.product_manifest、task3.page_*、imdb.snapshot，不在任务 1–3 的表上加列或约束（spec R41）。
-- 所有读取必须带抽取 ID；输入键 = 抽取 × 任务 3 规则版本 × 解析版本 × 标准化版本 × 规则版本 × IMDb 快照 × 人工决策集（R01、R38）。

CREATE SCHEMA task4;

-- ------------------------------------------------------------------ 运行与批次

CREATE TABLE task4.run (
    run_id            text PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN ('match', 'human_import', 'evaluate')),
    extraction_id     text NOT NULL REFERENCES public.extraction,
    t3_rule_version   text NOT NULL,
    parser_version    text NOT NULL,
    norm_version      text NOT NULL,
    rule_version      text NOT NULL,
    imdb_snapshot_id  text NOT NULL REFERENCES imdb.snapshot,
    human_set_hash    text NOT NULL,
    input_key         text NOT NULL,       -- 上面七项的稳定串；已有成功运行时新运行记 reused（R38）
    mapping_version_id text,               -- 本次运行产生的映射版本；reused 或未到聚类步骤时为 NULL
    status            text NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'succeeded', 'failed', 'reused', 'interrupted')),
    params            jsonb NOT NULL DEFAULT '{}',   -- batch_size、limit
    counts            jsonb NOT NULL DEFAULT '{}',   -- 各阶段新写入的行数
    dag_id            text,
    airflow_run_id    text,
    started_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at       timestamptz,
    failure_reason    text,
    CHECK ((status = 'running') = (finished_at IS NULL))
);

CREATE INDEX run_input_idx ON task4.run (extraction_id, input_key, status);

-- 与该批数据在同一事务提交的批次审计（R39）；恢复判据是 entity_stage 里已提交的实体。
CREATE TABLE task4.run_batch (
    run_id       text NOT NULL REFERENCES task4.run,
    stage        text NOT NULL,
    batch_no     integer NOT NULL CHECK (batch_no >= 1),
    first_asin   text,
    last_asin    text,
    rows_written bigint NOT NULL DEFAULT 0 CHECK (rows_written >= 0),
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, stage, batch_no)
);

-- 每个实体在某阶段、某组版本下是否已处理；与该批数据同事务写入，重跑只处理没有标记的实体。
-- stage_key 是该阶段输入版本的稳定串（normalize：解析版本+标准化版本；decide：再加规则版本与 IMDb 快照）。
CREATE TABLE task4.entity_stage (
    extraction_id text NOT NULL REFERENCES public.extraction,
    stage         text NOT NULL CHECK (stage IN ('normalize', 'decide', 'decide_pair')),
    stage_key     text NOT NULL,
    entity_id     text NOT NULL,
    run_id        text NOT NULL REFERENCES task4.run,
    batch_no      integer NOT NULL,
    rows_written  integer NOT NULL DEFAULT 0,
    PRIMARY KEY (extraction_id, stage, stage_key, entity_id)
);

CREATE TABLE task4.quality_issue (
    issue_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id        text REFERENCES task4.run,
    extraction_id text NOT NULL REFERENCES public.extraction,
    entity_id     text NOT NULL,
    issue_code    text NOT NULL,           -- missing_title、name_ambiguous、candidate_overflow、collection_member_unmatched、human_vs_veto 等
    detail        jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX quality_issue_idx ON task4.quality_issue (extraction_id, issue_code, entity_id);

-- ------------------------------------------------------------------ 商品版本、来源记录与字段断言

-- R02：清单里的每个 ASIN 恰好一行；product_id 与 edition_id 都等于 asin，不作 movie_id，不随合并删除。
CREATE TABLE task4.product_edition (
    extraction_id   text NOT NULL,
    t3_rule_version text NOT NULL,
    asin            text NOT NULL,
    product_kind    text NOT NULL CHECK (product_kind IN ('movie', 'non_movie', 'collection', 'tv_series',
                                                         'concert_other', 'pending')),
    basis_level     text NOT NULL,
    match_scope     text NOT NULL CHECK (match_scope IN ('matched', 'not_applicable', 'no_page')),
    parser_version  text,                   -- 有可解析页面时为解析版本
    content_hash    text,                   -- 页面来源：快照哈希、最终地址、采集时间（R06）
    final_url       text,
    fetched_at      timestamptz,
    run_id          text NOT NULL REFERENCES task4.run,
    PRIMARY KEY (extraction_id, t3_rule_version, asin),
    FOREIGN KEY (extraction_id, asin) REFERENCES task2.product_manifest,
    CHECK ((match_scope = 'no_page') = (content_hash IS NULL)),
    CHECK (match_scope <> 'matched' OR product_kind IN ('movie', 'collection'))
);

CREATE INDEX product_edition_scope_idx ON task4.product_edition (extraction_id, t3_rule_version, match_scope, asin);

CREATE TABLE task4.source_record (
    source_record_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_type      text NOT NULL CHECK (source_type IN ('page', 'imdb')),
    locator_key      text NOT NULL,        -- 定位的规范化串；同一来源只登记一次
    locator          jsonb NOT NULL,       -- 页面：抽取、ASIN、快照路径、最终地址；IMDb：快照、文件、行号、tconst/nconst
    content_hash     text NOT NULL,        -- 页面快照哈希，或 IMDb 文件 SHA-256
    acquired_at      timestamptz,
    UNIQUE (source_type, locator_key)
);

CREATE SEQUENCE task4.assertion_id_seq AS bigint;

-- R06：匹配所用字段的原值、规范值、来源与规则。派生值用 parent_assertion_id 指回来源断言（血缘）。
CREATE TABLE task4.field_assertion (
    assertion_id        bigint PRIMARY KEY DEFAULT nextval('task4.assertion_id_seq'),
    extraction_id       text NOT NULL REFERENCES public.extraction,
    parser_version      text NOT NULL,
    norm_version        text NOT NULL,
    entity_type         text NOT NULL CHECK (entity_type IN ('product_edition', 'collection_member')),
    entity_id           text NOT NULL,       -- ASIN，或 ASIN#成员序号
    field_name          text NOT NULL,
    ordinal             integer NOT NULL CHECK (ordinal >= 1),
    raw_value           text,
    normalized_value    text,                -- 占位署名等不参与比较的值为 NULL
    qualifier           text,                -- 说明类别、人物来源与角色（byline、reversed、detail、detail_only、placeholder…）
    extra               jsonb,               -- 宽松键、匹配片段、拆分方式等
    source_record_id    bigint REFERENCES task4.source_record,
    source_locator      jsonb,               -- 任务 3 字段的区域、序号、字段键
    parent_assertion_id bigint REFERENCES task4.field_assertion,
    rule_id             text NOT NULL,       -- 标准化规则编号，如 title.key、descriptor.media、name.strict
    UNIQUE (extraction_id, parser_version, norm_version, entity_id, field_name, ordinal)
);

CREATE INDEX field_assertion_block_idx ON task4.field_assertion (norm_version, field_name, normalized_value);
CREATE INDEX field_assertion_entity_idx ON task4.field_assertion (extraction_id, norm_version, entity_id);

-- IMDb 作品与别名的标题键（R09）；换标准化版本只重算这张表，不重装 IMDb。
CREATE TABLE task4.imdb_title_key (
    imdb_snapshot_id text NOT NULL REFERENCES imdb.snapshot ON DELETE CASCADE,
    norm_version     text NOT NULL,
    tconst           text NOT NULL,
    key_source       text NOT NULL CHECK (key_source IN ('primary', 'original', 'aka')),
    aka_ordering     integer NOT NULL DEFAULT 0,
    title_key        text NOT NULL,
    title_class      text NOT NULL CHECK (title_class IN ('movie', 'other')),   -- other = R15 的非电影作品类型
    region           text,
    language         text,
    source_line      bigint NOT NULL,     -- 来源行号（basics 或 akas 文件）
    PRIMARY KEY (imdb_snapshot_id, norm_version, tconst, key_source, aka_ordering)
);

CREATE INDEX imdb_title_key_idx ON task4.imdb_title_key (imdb_snapshot_id, norm_version, title_key);

-- ------------------------------------------------------------------ 候选与决策

CREATE TABLE task4.candidate (
    candidate_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_id    text NOT NULL REFERENCES public.extraction,
    norm_version     text NOT NULL,
    imdb_snapshot_id text NOT NULL REFERENCES imdb.snapshot,   -- 商品对商品候选同样记录所用快照（G3 的判定依赖 IMDb 关联）
    left_entity      text NOT NULL,              -- ASIN 或 ASIN#成员序号
    right_kind       text NOT NULL CHECK (right_kind IN ('imdb', 'product')),
    right_id         text NOT NULL,              -- tconst 或 ASIN；商品对商品只存 ASIN 较小者在左
    generator        text NOT NULL CHECK (generator IN ('G1', 'G2', 'G3', 'G4'))   -- G4：R15 非电影作品类型
);

CREATE UNIQUE INDEX candidate_key_idx ON task4.candidate
    (extraction_id, norm_version, imdb_snapshot_id, left_entity, right_kind, right_id);
CREATE INDEX candidate_right_idx ON task4.candidate (extraction_id, right_kind, right_id);

-- R20：超过每个商品的候选上限时记录，不静默截断。
CREATE TABLE task4.candidate_overflow (
    extraction_id    text NOT NULL REFERENCES public.extraction,
    norm_version     text NOT NULL,
    imdb_snapshot_id text NOT NULL,
    entity_id        text NOT NULL,
    generator        text NOT NULL,
    candidate_total  integer NOT NULL,
    kept             integer NOT NULL,
    basis            text NOT NULL,
    PRIMARY KEY (extraction_id, norm_version, imdb_snapshot_id, entity_id, generator)
);

-- R27：每个候选一条决策（未采用的也保存）；同一候选、同一规则版本只有一条。
CREATE TABLE task4.merge_decision (
    decision_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    candidate_id bigint NOT NULL REFERENCES task4.candidate,
    rule_version text NOT NULL,
    run_id       text NOT NULL REFERENCES task4.run,
    tier         text NOT NULL CHECK (tier IN ('auto', 'review', 'reject')),
    score        numeric(6, 2) NOT NULL,
    confidence   numeric(4, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    rank         integer,                       -- 在同一左实体的 IMDb 候选中的名次（1 = 最高）
    margin       numeric(6, 2),                 -- 领先第二名的分差
    veto         text[] NOT NULL DEFAULT '{}',  -- 硬冲突原因：director_disjoint、year_conflict、different_external_id
    reason       text,                          -- non_movie_title_type、suggest_join_imdb_cluster、tie 等
    features     jsonb NOT NULL,                -- 每个特征的取值、得分与所用断言或 IMDb 源记录
    thresholds   jsonb NOT NULL,
    decided_by   text NOT NULL DEFAULT 'system' CHECK (decided_by IN ('system', 'human')),
    decided_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (candidate_id, rule_version),
    CHECK (tier <> 'auto' OR cardinality(veto) = 0)
);

CREATE INDEX merge_decision_tier_idx ON task4.merge_decision (rule_version, tier);

-- 人工决策（R30）：必须合并、不得合并、移出；人工决策集的哈希进入输入键。
CREATE TABLE task4.human_decision (
    human_decision_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_id     text NOT NULL REFERENCES public.extraction,
    set_hash          text NOT NULL,            -- 导入这条决策之后整个决策集的哈希
    kind              text NOT NULL CHECK (kind IN ('must_link', 'cannot_link', 'detach')),
    left_entity       text NOT NULL,            -- ASIN
    right_entity      text,                     -- must_link、cannot_link 的另一个 ASIN；detach 为 NULL
    note              text,
    annotator         text NOT NULL,
    decided_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_sha256 text NOT NULL,
    run_id            text REFERENCES task4.run,
    CHECK ((kind = 'detach') = (right_entity IS NULL))
);

CREATE UNIQUE INDEX human_decision_key_idx ON task4.human_decision
    (extraction_id, kind, left_entity, (coalesce(right_entity, '')));

-- ------------------------------------------------------------------ 作品、映射与历史

-- R28：movie_id 由系统生成，按锚点登记，生成后不复用。
CREATE TABLE task4.movie_identity (
    anchor_key  text PRIMARY KEY,            -- imdb:tt… 或 asin:…
    movie_id    text NOT NULL UNIQUE CHECK (movie_id ~ '^mv_[0-9a-f]{16}$'),
    created_run text NOT NULL REFERENCES task4.run,
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE task4.mapping_version (
    mapping_version_id text PRIMARY KEY,
    extraction_id      text NOT NULL REFERENCES public.extraction,
    parent_version_id  text REFERENCES task4.mapping_version,
    run_id             text NOT NULL REFERENCES task4.run,
    input_key          text NOT NULL,
    t3_rule_version    text NOT NULL,
    parser_version     text NOT NULL,
    norm_version       text NOT NULL,
    rule_version       text NOT NULL,
    imdb_snapshot_id   text NOT NULL REFERENCES imdb.snapshot,
    human_set_hash     text NOT NULL,
    counts             jsonb NOT NULL,       -- R29：参与商品、作品、多商品作品、单商品作品、待复核商品
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (extraction_id, input_key)
);

ALTER TABLE task4.run ADD FOREIGN KEY (mapping_version_id) REFERENCES task4.mapping_version;

-- 作品只含身份、状态与一个显示标题（H08）。
CREATE TABLE task4.movie (
    mapping_version_id   text NOT NULL REFERENCES task4.mapping_version,
    movie_id             text NOT NULL REFERENCES task4.movie_identity (movie_id),
    status               text NOT NULL CHECK (status IN ('imdb_linked', 'page_only')),
    anchor_key           text NOT NULL,
    tconst               text,
    display_title        text,
    display_title_source jsonb,
    edition_count        integer NOT NULL DEFAULT 0 CHECK (edition_count >= 0),
    contains_count       integer NOT NULL DEFAULT 0 CHECK (contains_count >= 0),
    PRIMARY KEY (mapping_version_id, movie_id),
    CHECK ((status = 'imdb_linked') = (tconst IS NOT NULL))
);

-- edition = 商品本身就是该作品的一个版本；contains = 合集商品包含该作品（R18）。
CREATE TABLE task4.product_movie (
    mapping_version_id text NOT NULL,
    asin               text NOT NULL,
    member_ordinal     integer NOT NULL CHECK (member_ordinal >= 0),
    movie_id           text NOT NULL,
    relation_type      text NOT NULL CHECK (relation_type IN ('edition', 'contains')),
    basis              text NOT NULL CHECK (basis IN ('auto_decision', 'singleton', 'human')),
    decision_id        bigint REFERENCES task4.merge_decision,
    human_decision_id  bigint REFERENCES task4.human_decision,
    PRIMARY KEY (mapping_version_id, asin, member_ordinal, movie_id),
    FOREIGN KEY (mapping_version_id, movie_id) REFERENCES task4.movie,
    CHECK ((relation_type = 'edition') = (member_ordinal = 0))
);

CREATE INDEX product_movie_movie_idx ON task4.product_movie (mapping_version_id, movie_id);

CREATE TABLE task4.product_match_status (
    mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    asin               text NOT NULL,
    status             text NOT NULL CHECK (status IN ('merged', 'singleton', 'review_pending', 'collection_split',
                                                      'collection_unsplit')),
    reason             text,
    PRIMARY KEY (mapping_version_id, asin)
);

-- 由新旧两个映射版本逐商品对比生成（R29、R31）。
CREATE TABLE task4.mapping_event (
    event_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    event              text NOT NULL CHECK (event IN ('create', 'merge', 'split', 'move', 'retire')),
    asin               text,
    from_movie_id      text,
    to_movie_id        text,
    reason             text,
    decision_id        bigint REFERENCES task4.merge_decision,
    human_decision_id  bigint REFERENCES task4.human_decision
);

CREATE INDEX mapping_event_idx ON task4.mapping_event (mapping_version_id, event);

-- R26：簇内硬冲突的检查记录。
CREATE TABLE task4.cluster_check (
    check_id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    movie_id           text NOT NULL,
    asin_a             text NOT NULL,
    asin_b             text NOT NULL,
    conflict           text NOT NULL CHECK (conflict IN ('director_disjoint', 'year_conflict')),
    action             text NOT NULL,        -- removed_b、removed_a、kept_by_human
    resolved_by        text NOT NULL CHECK (resolved_by IN ('system', 'human'))
);

-- ------------------------------------------------------------------ 评价（T4-06 使用）

CREATE TABLE task4.dev_pool (
    extraction_id text NOT NULL REFERENCES public.extraction,
    asin          text NOT NULL,
    title_key     text NOT NULL,             -- 冻结时（t4n1）的标题键
    norm_version  text NOT NULL,             -- 冻结时的标准化版本
    frozen_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (extraction_id, asin)
);

CREATE TABLE task4.eval_set (
    eval_set_id        text PRIMARY KEY,
    extraction_id      text NOT NULL REFERENCES public.extraction,
    mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    rule_version       text NOT NULL,
    strata             jsonb NOT NULL,       -- 层 → 实际对数（含补足来源）
    exported_file      text,
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE task4.eval_pair (
    eval_set_id     text NOT NULL REFERENCES task4.eval_set,
    pair_id         text NOT NULL,
    stratum         text NOT NULL,
    left_entity     text NOT NULL,
    right_entity    text NOT NULL,
    left_movie_id   text,
    right_movie_id  text,
    predicted_same  boolean NOT NULL,        -- 系统在导出时的映射版本里是否判为同一作品（不含人工决策）
    system_tier     text,
    PRIMARY KEY (eval_set_id, pair_id)
);

CREATE TABLE task4.eval_label (
    eval_set_id        text NOT NULL,
    pair_id            text NOT NULL,
    label              text NOT NULL CHECK (label IN ('same', 'different', 'unsure')),
    annotator          text NOT NULL,
    note               text,
    labeled_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_sha256 text NOT NULL,
    PRIMARY KEY (eval_set_id, pair_id),
    FOREIGN KEY (eval_set_id, pair_id) REFERENCES task4.eval_pair
);

CREATE TABLE task4.cluster_sample (
    eval_set_id text NOT NULL REFERENCES task4.eval_set,
    movie_id    text NOT NULL,
    asin_count  integer NOT NULL,
    PRIMARY KEY (eval_set_id, movie_id)
);

CREATE TABLE task4.cluster_label (
    eval_set_id        text NOT NULL,
    movie_id           text NOT NULL,
    mixed              boolean NOT NULL,     -- true = 混入了不同作品
    problem_asins      text[] NOT NULL DEFAULT '{}',
    annotator          text NOT NULL,
    note               text,
    labeled_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_sha256 text NOT NULL,
    PRIMARY KEY (eval_set_id, movie_id),
    FOREIGN KEY (eval_set_id, movie_id) REFERENCES task4.cluster_sample
);

INSERT INTO public.schema_migration (version) VALUES ('302');
