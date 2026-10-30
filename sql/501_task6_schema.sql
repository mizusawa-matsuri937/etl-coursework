-- 任务 6：补全上映信息（schema task6）。
-- 幂等：由 etl_task6.run.apply_task6_schema 通过 public.schema_migration 判断是否已应用（须先有 302 与 401）。
-- 只引用 public.extraction、task4.*、task5.person_mapping_version、task5.t4_upgrade_hint、imdb.snapshot，
-- 不在任务 1–5 的表上加列或约束（spec R31）。
-- 输入键 = 抽取 × 任务 4 映射版本 × 任务 5 人物映射版本 × 解析版本 × 规则版本 × IMDb 快照（R01、R27）。
-- 三类时间分开保存（R04）：release_selection 只存作品首次公开发行年份；product_release_date 按 ASIN；
-- product_review_time / movie_review_time 存评论时间；推断上界单独在 inferred_release_bound，不写进选择记录（R24）。

CREATE SCHEMA task6;

-- ------------------------------------------------------------------ 运行与批次

CREATE TABLE task6.run (
    run_id            text PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN ('release', 'evaluate')),
    extraction_id     text NOT NULL REFERENCES public.extraction,
    t4_mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    t5_person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    parser_version    text NOT NULL,
    rule_version      text NOT NULL,
    imdb_snapshot_id  text NOT NULL REFERENCES imdb.snapshot,
    input_key         text NOT NULL,       -- 上面各项的稳定串；已有成功运行时新运行记 reused（R28）
    release_version_id text,               -- 本次运行产生的上映信息版本；reused 或未到择优步骤时为 NULL
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

CREATE INDEX run_input_idx ON task6.run (extraction_id, input_key, status);

-- 与该批数据在同一事务提交的批次审计（R29）；恢复判据是 entity_stage 里已提交的实体。
CREATE TABLE task6.run_batch (
    run_id       text NOT NULL REFERENCES task6.run,
    stage        text NOT NULL,
    batch_no     integer NOT NULL CHECK (batch_no >= 1),
    first_entity text,                    -- ASIN 或 movie_id
    last_entity  text,
    rows_written bigint NOT NULL DEFAULT 0 CHECK (rows_written >= 0),
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, stage, batch_no)
);

-- 每个实体在某阶段、某组版本下是否已处理；与该批数据同事务写入，重跑只处理没有标记的实体。
-- stage_key 是该阶段输入版本的稳定串（design“版本、幂等、分批与编排”）：
--   candidates     = 任务 4 映射版本 | 解析版本 | 人物映射版本 | IMDb 快照
--   cooccur        = 任务 4 映射版本 | 解析版本 | IMDb 快照 | 共现参数版本
--   review_years   = 任务 4 映射版本 | 解析版本 | 提及模式版本
--   product_dates  = 解析版本
--   product_reviews= 任务 4 映射版本 | 解析版本
CREATE TABLE task6.entity_stage (
    extraction_id text NOT NULL REFERENCES public.extraction,
    stage         text NOT NULL CHECK (stage IN ('candidates', 'cooccur', 'review_years', 'product_dates',
                                                'product_reviews')),
    stage_key     text NOT NULL,
    entity_id     text NOT NULL,           -- movie_id 或 ASIN
    run_id        text NOT NULL REFERENCES task6.run,
    batch_no      integer NOT NULL,
    rows_written  integer NOT NULL DEFAULT 0,
    PRIMARY KEY (extraction_id, stage, stage_key, entity_id)
);

CREATE TABLE task6.quality_issue (
    issue_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id        text REFERENCES task6.run,
    extraction_id text NOT NULL REFERENCES public.extraction,
    entity_id     text NOT NULL,           -- movie_id 或 asin
    issue_code    text NOT NULL,           -- year_disagreement、tconst_disagreement、shared_tconst_with_movie、
                                           -- bound_before_release_year、tconst_not_in_snapshot、unknown_date_label 等
    detail        jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX quality_issue_idx ON task6.quality_issue (extraction_id, issue_code, entity_id);

-- ------------------------------------------------------------------ 上映信息版本（R27）

CREATE TABLE task6.release_version (
    release_version_id text PRIMARY KEY,
    extraction_id      text NOT NULL REFERENCES public.extraction,
    parent_version_id  text REFERENCES task6.release_version,
    run_id             text NOT NULL REFERENCES task6.run,
    input_key          text NOT NULL,
    t4_mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    t5_person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    parser_version     text NOT NULL,
    rule_version       text NOT NULL,
    imdb_snapshot_id   text NOT NULL REFERENCES imdb.snapshot,
    counts             jsonb NOT NULL,     -- R33 的数量
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (extraction_id, input_key)
);

ALTER TABLE task6.run ADD FOREIGN KEY (release_version_id) REFERENCES task6.release_version;

-- ------------------------------------------------------------------ 年份候选与线索（R06–R13）

-- 每条候选：来源、原始值、规范值、精度、语义、获取与抽取时间、匹配证据（R13）。
-- stage_key 是 candidates 阶段键；imdb_t6_cooccur 的候选在择优阶段经共现消解后写入，用 cooccur 阶段键。
CREATE TABLE task6.release_candidate (
    candidate_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_id  text NOT NULL REFERENCES public.extraction,
    stage_key      text NOT NULL,
    movie_id       text NOT NULL,
    source         text NOT NULL CHECK (source IN ('imdb_t4_link', 'imdb_t5_hint', 'imdb_t6_cooccur',
                                                   'amazon_title_year')),
    source_key     text NOT NULL,          -- tconst，或 <ASIN>:<断言 ID>
    raw_value      text,
    value_year     integer CHECK (value_year BETWEEN 1800 AND 2100),
    precision      text NOT NULL DEFAULT 'year' CHECK (precision = 'year'),
    semantic       text NOT NULL CHECK (semantic = 'first_release'),
    region         text,                   -- 恒为 NULL（R03）
    flags          text[] NOT NULL DEFAULT '{}',   -- shared_tconst、review_year_tiebreak
    shared_with_movie_id text,
    source_ref     jsonb NOT NULL DEFAULT '{}',    -- IMDb：快照、文件名、行号、tconst、titleType；页面：ASIN、区域、序号、断言 ID、快照与地址
    source_acquired_at timestamptz,
    extracted_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
    match_evidence jsonb NOT NULL DEFAULT '{}',    -- 任务 4 关联依据、线索的提及与决策、共现评分摘要、商品归属
    confidence     numeric(4, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),   -- R13：候选置信度（t6r1 的分档）
    rule_version   text NOT NULL,                  -- R13：产生本候选的规则版本（与阶段键互补）
    run_id         text NOT NULL REFERENCES task6.run,
    UNIQUE (extraction_id, stage_key, movie_id, source, source_key),
    CHECK (value_year IS NOT NULL)                 -- 没有年份就不产生候选（R06）；NULL 年份的作品走 missing
);

-- 任务 5 升级线索的逐条处理结果（R07、R08）；合集成员与多 tconst 只记录。
CREATE TABLE task6.hint_use (
    extraction_id  text NOT NULL REFERENCES public.extraction,
    stage_key      text NOT NULL,
    asin           text NOT NULL,
    member_ordinal integer NOT NULL,
    tconst         text NOT NULL,
    movie_id       text,                   -- 能定位到当前映射版本的作品时记录
    action         text NOT NULL CHECK (action IN ('candidate', 'candidate_shared', 'collection_member',
                                                   'multiple_tconst', 'movie_already_linked')),
    owner_movie_id text,                   -- candidate_shared 时的占用者
    run_id         text NOT NULL REFERENCES task6.run,
    PRIMARY KEY (extraction_id, stage_key, asin, member_ordinal, tconst)
);

-- ------------------------------------------------------------------ 人物共现反查（R08–R10）

-- 每个得分 ≥1 的作品–tconst 对一条，含未通过的条件（R10）。
CREATE TABLE task6.cooccur_score (
    extraction_id       text NOT NULL REFERENCES public.extraction,
    stage_key           text NOT NULL,
    movie_id            text NOT NULL,
    tconst              text NOT NULL,
    score               integer NOT NULL CHECK (score >= 1),
    director_hits       jsonb NOT NULL DEFAULT '[]',  -- [{page_key, assertion_id, asin, nconst, crew_line_no}]
    actor_hits          jsonb NOT NULL DEFAULT '[]',
    title_ok            boolean NOT NULL,
    matched_title_key   text,
    matched_asin        text,              -- 用哪个商品标题键通过标题条件
    key_source          text,              -- primary、original、aka
    title_key_source_line bigint,
    number_guard        boolean,           -- NULL = 标题条件未通过、编号未评估；false = 被编号条件排除
    number_guard_detail text,
    passed              boolean NOT NULL,
    run_id              text NOT NULL REFERENCES task6.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id, tconst)
);

-- 作品层汇总：unique / resolved_by_review_year / ambiguous / none（共现消解在择优阶段做，因为它依赖评论年份提及）。
CREATE TABLE task6.cooccur_result (
    extraction_id  text NOT NULL REFERENCES public.extraction,
    stage_key      text NOT NULL,
    movie_id       text NOT NULL,
    outcome        text NOT NULL CHECK (outcome IN ('unique', 'resolved_by_review_year', 'ambiguous', 'none')),
    chosen_tconst  text,
    passed_tconsts text[] NOT NULL DEFAULT '{}',
    owner_movie_id text,                   -- 选中的 tconst 被另一作品占用时记录
    run_id         text NOT NULL REFERENCES task6.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id)
);

-- ------------------------------------------------------------------ 评论年份提及（R11，只作证据与消歧）

CREATE TABLE task6.review_year_mention (
    extraction_id    text NOT NULL REFERENCES public.extraction,
    stage_key        text NOT NULL,
    movie_id         text NOT NULL,
    year             integer NOT NULL CHECK (year BETWEEN 1800 AND 2100),
    votes            integer NOT NULL CHECK (votes >= 1),   -- 不同评论数
    source_review_ids text[] NOT NULL,
    patterns         jsonb NOT NULL DEFAULT '{}',            -- 各模式的票数
    run_id           text NOT NULL REFERENCES task6.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id, year)
);

-- ------------------------------------------------------------------ 选择记录（R14–R17）

CREATE TABLE task6.release_selection (
    release_version_id text NOT NULL REFERENCES task6.release_version,
    movie_id           text NOT NULL,
    status             text NOT NULL CHECK (status IN ('verified', 'hint_matched', 'cooccur_matched', 'page_reported',
                                                       'missing')),
    missing_reason     text CHECK (missing_reason IN ('no_candidate', 'title_years_disagree', 'hint_ambiguous',
                                                      'cooccur_ambiguous')),
    release_year       integer,
    release_date       date CHECK (release_date IS NULL),    -- 只有年份，不补成 1 月 1 日（R03、R05）
    precision          text NOT NULL CHECK (precision = 'year'),
    region             text CHECK (region IS NULL),
    release_type       text NOT NULL CHECK (release_type = 'first_release'),
    regional_release_missing_reason text NOT NULL CHECK (regional_release_missing_reason = 'no_permitted_dated_source'),
    selected_candidate_id bigint REFERENCES task6.release_candidate,
    considered_candidate_ids bigint[] NOT NULL DEFAULT '{}',
    selection_rule     text NOT NULL,
    reason             text NOT NULL,
    confidence         numeric(4, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    disagreement       jsonb NOT NULL DEFAULT '{}',   -- 与未采用候选的年差、tconst 不同（R16）
    PRIMARY KEY (release_version_id, movie_id),
    CHECK ((status = 'missing') = (release_year IS NULL AND selected_candidate_id IS NULL)),
    CHECK (status = 'missing' OR missing_reason IS NULL)
);

-- ------------------------------------------------------------------ 推断上界（R20–R25）

-- 两种证据各条件的结果，逐条件保存在 checks；未通过的列在 failed_checks。
CREATE TABLE task6.bound_evidence (
    release_version_id text NOT NULL REFERENCES task6.release_version,
    movie_id           text NOT NULL,
    evidence           text NOT NULL CHECK (evidence IN ('review', 'media_release')),
    evidence_time      date NOT NULL,
    evidence_precision text NOT NULL CHECK (evidence_precision IN ('day', 'year')),
    basis              jsonb NOT NULL DEFAULT '{}',   -- review：源评论 ID 与 ASIN；media_release：断言 ID 与 ASIN
    checks             jsonb NOT NULL DEFAULT '{}',   -- B1–B5 或 M1–M4 各自的判定与所用值
    passed             boolean NOT NULL,
    failed_checks      text[] NOT NULL DEFAULT '{}',
    confidence         numeric(4, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    PRIMARY KEY (release_version_id, movie_id, evidence)
);

-- 每个有评论或有商品发售日期的作品一条上界评估（R23）。
CREATE TABLE task6.bound_assessment (
    release_version_id text NOT NULL REFERENCES task6.release_version,
    movie_id           text NOT NULL,
    has_review_evidence boolean NOT NULL,
    has_media_evidence  boolean NOT NULL,
    passed_evidence     text[] NOT NULL DEFAULT '{}',
    chosen_evidence     text CHECK (chosen_evidence IN ('review', 'media_release')),
    PRIMARY KEY (release_version_id, movie_id)
);

-- 至少一种证据成立时另存的推断上界；不写进选择记录、不覆盖已核实年份（R24）。
CREATE TABLE task6.inferred_release_bound (
    release_version_id text NOT NULL REFERENCES task6.release_version,
    movie_id           text NOT NULL,
    bound_time         date NOT NULL,
    bound_precision    text NOT NULL CHECK (bound_precision IN ('day', 'year')),
    bound_kind         text NOT NULL CHECK (bound_kind = 'not_later_than'),
    chosen_evidence    text NOT NULL CHECK (chosen_evidence IN ('review', 'media_release')),
    basis              jsonb NOT NULL DEFAULT '{}',
    conditions         jsonb NOT NULL DEFAULT '{}',   -- 采用证据的条件与值快照
    confidence         numeric(4, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    status             text NOT NULL CHECK (status = 'inferred'),
    rule_version       text NOT NULL,
    PRIMARY KEY (release_version_id, movie_id)
);

-- ------------------------------------------------------------------ 商品层（R18–R20）

-- 商品发售日期按 ASIN 单独保存；kind 区分实体发售与上架时间（R18）。
CREATE TABLE task6.product_release_date (
    extraction_id    text NOT NULL REFERENCES public.extraction,
    parser_version   text NOT NULL,
    assertion_id     bigint NOT NULL REFERENCES task4.field_assertion,
    asin             text NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('media_release', 'listing_first_available')),
    label_raw        text,
    raw_value        text,
    value_date       date,
    precision        text NOT NULL CHECK (precision IN ('day', 'month', 'year')),
    flags            text[] NOT NULL DEFAULT '{}',    -- jan_first_suspect、after_review_period（R19）
    section          text,
    page_ordinal     integer,
    source_record_id bigint,
    run_id           text NOT NULL REFERENCES task6.run,
    PRIMARY KEY (extraction_id, parser_version, assertion_id)
);

-- 每个参与匹配商品（电影类与合集）的最早两条有效评论与评论数（R20）。
CREATE TABLE task6.product_review_time (
    extraction_id         text NOT NULL REFERENCES public.extraction,
    asin                  text NOT NULL,
    product_kind          text NOT NULL CHECK (product_kind IN ('movie', 'collection')),
    earliest_time         timestamptz NOT NULL,
    earliest_source_review_id text NOT NULL,
    second_time           timestamptz,
    review_count          integer NOT NULL,
    run_id                text NOT NULL REFERENCES task6.run,
    PRIMARY KEY (extraction_id, asin)
);

-- 作品层最早评论：只汇总 edition 关系的电影类商品；合集评论不进成员作品（R20）。
CREATE TABLE task6.movie_review_time (
    extraction_id         text NOT NULL REFERENCES public.extraction,
    t4_mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    movie_id              text NOT NULL,
    earliest_review_time  timestamptz NOT NULL,
    earliest_precision    text NOT NULL CHECK (earliest_precision = 'day'),
    earliest_source_review_id text NOT NULL,
    earliest_asin         text NOT NULL,
    second_review_time    timestamptz,
    review_count          integer NOT NULL,
    reviewed_asin_count   integer NOT NULL,
    edition_asin_count    integer NOT NULL,
    run_id                text NOT NULL REFERENCES task6.run,
    PRIMARY KEY (extraction_id, t4_mapping_version_id, movie_id)
);

-- ------------------------------------------------------------------ 评价（R36–R39）

-- 开发池：按 md5(movie_id) 排序前 20% 的作品，冻结后不变；评价样本不含开发池作品。
CREATE TABLE task6.dev_pool (
    extraction_id text NOT NULL REFERENCES public.extraction,
    movie_id      text NOT NULL,
    t4_mapping_version_id text NOT NULL,
    frozen_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (extraction_id, movie_id)
);

CREATE TABLE task6.eval_set (
    eval_set_id        text PRIMARY KEY,
    extraction_id      text NOT NULL REFERENCES public.extraction,
    release_version_id text NOT NULL REFERENCES task6.release_version,
    rule_version       text NOT NULL,
    strata             jsonb NOT NULL,     -- 层 → {target, actual, filled}
    exported_file      text,
    created_at         timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- 系统判定存在库里用于算指标，不导出（R37 的导出不显示系统年份、状态、层与上界）。
CREATE TABLE task6.eval_item (
    eval_set_id text NOT NULL REFERENCES task6.eval_set,
    item_id     text NOT NULL,
    stratum     text NOT NULL,
    movie_id    text NOT NULL,
    tconst      text,
    system_year integer,
    status      text,
    source      text,
    bound_year  integer,
    PRIMARY KEY (eval_set_id, item_id)
);

CREATE TABLE task6.eval_label (
    eval_set_id        text NOT NULL,
    item_id            text NOT NULL,
    true_year          integer,
    year_label         text NOT NULL CHECK (year_label IN ('known', 'unsure')),
    tconst_label       text NOT NULL CHECK (tconst_label IN ('same', 'different', 'unsure', 'na')),
    note               text,
    annotator          text NOT NULL,
    labeled_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_sha256 text NOT NULL,
    PRIMARY KEY (eval_set_id, item_id),
    FOREIGN KEY (eval_set_id, item_id) REFERENCES task6.eval_item,
    CHECK ((year_label = 'known') = (true_year IS NOT NULL))
);

INSERT INTO public.schema_migration (version) VALUES ('501');
