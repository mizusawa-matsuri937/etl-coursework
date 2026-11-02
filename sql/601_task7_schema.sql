-- 任务 7：处理不同来源的信息冲突（schema task7）。
-- 幂等：由 etl_task7.run.apply_task7_schema 通过 public.schema_migration 判断是否已应用（须先有 302、401 与 501）。
-- 只引用 public.extraction、task4.mapping_version、task5.person_mapping_version、task6.release_version、imdb.snapshot，
-- 不在任务 1–6 的表上加列或约束（spec R56）。
-- 输入键 = 抽取 × 任务 4 映射版本 × 任务 5 人物映射版本 × 任务 6 上映版本 × 解析版本 × 规范化版本 × IMDb 快照 × 规则版本（R01、R50）。
-- 阶段键（R51）：edition_resolve → link_assess → movie_scalar / movie_multivalue，下游阶段键包含上游阶段键。
-- 阶段表按 (extraction_id, stage_key) 持久化；conflict_version 登记四个阶段键，finalize_version 从阶段表物化版本表（T7-05）。
-- 两层实体：作品层 Movie（movie_id）与商品版本层 ProductEdition（asin）；版本物理差异不进入作品层冲突（R04、R05）。

CREATE SCHEMA task7;

-- ------------------------------------------------------------------ 运行与批次

CREATE TABLE task7.run (
    run_id            text PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN ('resolve', 'evaluate')),
    extraction_id     text NOT NULL REFERENCES public.extraction,
    t4_mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    t5_person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    t6_release_version_id text NOT NULL REFERENCES task6.release_version,
    parser_version    text NOT NULL,
    norm_version      text NOT NULL,
    imdb_snapshot_id  text NOT NULL REFERENCES imdb.snapshot,
    rule_version      text NOT NULL,
    input_key         text NOT NULL,       -- 上面各项的稳定串；已有通过核对的运行时新运行记 reused（R52）
    conflict_version_id text,              -- 本次运行产生的冲突消解版本；reused 或未到物化步骤时为 NULL
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

CREATE INDEX run_input_idx ON task7.run (extraction_id, input_key, status);

-- 与该批数据在同一事务提交的批次审计（R53）；恢复判据是 entity_stage 里已提交的实体。
CREATE TABLE task7.run_batch (
    run_id       text NOT NULL REFERENCES task7.run,
    stage        text NOT NULL,
    batch_no     integer NOT NULL CHECK (batch_no >= 1),
    first_entity text,                    -- ASIN 或 movie_id
    last_entity  text,
    rows_written bigint NOT NULL DEFAULT 0 CHECK (rows_written >= 0),
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, stage, batch_no)
);

-- 每个实体在某阶段、某阶段键下是否已处理；与该批数据同事务写入，重跑只处理没有标记的实体（R53）。
-- link_assess 的跨作品步骤（共享 tconst 分类）用实体 ID '__shared_tconst__' 标记。
CREATE TABLE task7.entity_stage (
    extraction_id text NOT NULL REFERENCES public.extraction,
    stage         text NOT NULL CHECK (stage IN ('edition_resolve', 'link_assess', 'movie_scalar', 'movie_multivalue')),
    stage_key     text NOT NULL,
    entity_id     text NOT NULL,           -- ASIN 或 movie_id
    run_id        text NOT NULL REFERENCES task7.run,
    batch_no      integer NOT NULL,
    rows_written  integer NOT NULL DEFAULT 0,
    PRIMARY KEY (extraction_id, stage, stage_key, entity_id)
);

CREATE TABLE task7.quality_issue (
    issue_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id        text REFERENCES task7.run,
    extraction_id text NOT NULL REFERENCES public.extraction,
    stage_key     text NOT NULL,           -- 产生该问题的阶段键（多个阶段键可共存）
    entity_id     text NOT NULL,           -- movie_id 或 asin
    issue_code    text NOT NULL,           -- weak_link_single_signal、cross_source_override、severe_runtime_conflict、
                                           -- unknown_mpaa_rating、unmapped_genre_term、unsplit_multi_person 等
    detail        jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX quality_issue_idx ON task7.quality_issue (extraction_id, stage_key, issue_code, entity_id);

-- ------------------------------------------------------------------ 冲突消解版本（R50、R55）

CREATE TABLE task7.conflict_version (
    conflict_version_id text PRIMARY KEY,
    extraction_id       text NOT NULL REFERENCES public.extraction,
    parent_version_id   text REFERENCES task7.conflict_version,
    run_id              text NOT NULL REFERENCES task7.run,
    input_key           text NOT NULL,
    t4_mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    t5_person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    t6_release_version_id text NOT NULL REFERENCES task6.release_version,
    parser_version      text NOT NULL,
    norm_version        text NOT NULL,
    imdb_snapshot_id    text NOT NULL REFERENCES imdb.snapshot,
    rule_version        text NOT NULL,
    edition_stage_key   text NOT NULL,
    link_stage_key      text NOT NULL,
    scalar_stage_key    text NOT NULL,
    multivalue_stage_key text NOT NULL,
    status              text NOT NULL DEFAULT 'unverified' CHECK (status IN ('unverified', 'verified')),
    counts              jsonb NOT NULL DEFAULT '{}',
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    verified_at         timestamptz,
    UNIQUE (extraction_id, input_key),
    CHECK ((status = 'verified') = (verified_at IS NOT NULL))
);

ALTER TABLE task7.run ADD FOREIGN KEY (conflict_version_id) REFERENCES task7.conflict_version;

-- ------------------------------------------------------------------ 标量候选（R07、R08）

CREATE SEQUENCE task7.candidate_id_seq AS bigint;

-- 每个标量字段的全部候选：来源、原始值、规范值、精度、定位、获取时间、匹配可靠度、状态与原因。
-- entity_type = 'movie' 的候选用作品层阶段键（scalar_stage_key），'edition' 的用 edition_stage_key。
CREATE TABLE task7.field_candidate (
    candidate_id       bigint PRIMARY KEY DEFAULT nextval('task7.candidate_id_seq'),
    extraction_id      text NOT NULL REFERENCES public.extraction,
    stage_key          text NOT NULL,
    entity_type        text NOT NULL CHECK (entity_type IN ('movie', 'edition')),
    entity_id          text NOT NULL,          -- movie_id 或 asin
    field_name         text NOT NULL,
    source             text NOT NULL,          -- imdb_t4_link、imdb_t5_hint、imdb_t6_cooccur、imdb_title、amazon_title_year、
                                               -- page_title、page_runtime、page_rating、edition_selection 等
    source_key         text NOT NULL,          -- tconst、<ASIN>:<断言 ID>、<ASIN>:<区域>:<序号> 等
    raw_value          text,
    normalized_value   text,                   -- 规范值；无法规范化（如未知分级）时为 NULL
    precision          text NOT NULL CHECK (precision IN ('year', 'day', 'minute', 'exact')),
    source_ref         jsonb NOT NULL,         -- 来源定位：快照、文件、行号、tconst；页面：ASIN、区域、序号、断言 ID、快照哈希
    source_acquired_at timestamptz,            -- 页面 acquired_at 或 IMDb 快照 downloaded_at（R10：只保存，不参与决胜）
    match_reliability  numeric(4, 2) NOT NULL CHECK (match_reliability BETWEEN 0 AND 1),
    status             text NOT NULL CHECK (status IN ('adopted', 'rejected', 'conflict_pending',
                                                       'superseded_by_specific')),
    status_reason      text,
    rule_version       text NOT NULL,
    run_id             text NOT NULL REFERENCES task7.run,
    extracted_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (extraction_id, stage_key, entity_type, entity_id, field_name, source, source_key)
);

CREATE INDEX field_candidate_entity_idx ON task7.field_candidate (extraction_id, stage_key, entity_id, field_name);

-- ------------------------------------------------------------------ 版本层阶段结果（edition_resolve，R44–R48）

-- 每个 matched 商品一行；标量值与 7 项多值特征随行保存，field_selections 保存六个标量字段的选择记录。
CREATE TABLE task7.edition_stage_result (
    extraction_id         text NOT NULL REFERENCES public.extraction,
    stage_key             text NOT NULL,
    asin                  text NOT NULL,
    product_kind          text NOT NULL CHECK (product_kind IN ('movie', 'collection')),
    movie_ids             text[] NOT NULL DEFAULT '{}',       -- 无关联作品的合集为 '{}'（R06）
    edition_title         text,                               -- 页面原始标题
    edition_clean_title   text,                               -- 清洁规范名
    runtime_minutes       integer,
    mpaa_rating           text,
    publisher             text,
    product_release_date  date,
    media_release_date    date,
    listing_first_available_date date,
    date_flags            text[] NOT NULL DEFAULT '{}',
    discs                 integer,
    media_format          text[] NOT NULL DEFAULT '{}',
    language              text[] NOT NULL DEFAULT '{}',
    subtitles             text[] NOT NULL DEFAULT '{}',
    aspect_ratio          text[] NOT NULL DEFAULT '{}',
    region                text[] NOT NULL DEFAULT '{}',
    cut_type              text[] NOT NULL DEFAULT '{}',
    video_system          text[] NOT NULL DEFAULT '{}',
    attributes_detail     jsonb NOT NULL DEFAULT '{}',        -- 每个多值特征的值 → 来源区域
    field_selections      jsonb NOT NULL,                     -- 字段 → 选择记录
    run_id                text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, asin)
);

-- ------------------------------------------------------------------ 关联评估（link_assess，R14–R19）

CREATE TABLE task7.link_assessment (
    extraction_id        text NOT NULL REFERENCES public.extraction,
    stage_key            text NOT NULL,
    movie_id             text NOT NULL,
    effective_tconst     text NOT NULL,
    tconst_source        text NOT NULL CHECK (tconst_source IN ('imdb_t4_link', 'imdb_t5_hint', 'imdb_t6_cooccur')),
    base_reliability     numeric(4, 2) NOT NULL CHECK (base_reliability BETWEEN 0 AND 1),
    is_weak              boolean NOT NULL,
    sig_year             text NOT NULL CHECK (sig_year IN ('ok', 'bad', 'na')),
    sig_runtime          text NOT NULL CHECK (sig_runtime IN ('ok', 'bad', 'na')),
    sig_director         text NOT NULL CHECK (sig_director IN ('ok', 'bad', 'na')),
    sig_title            text NOT NULL CHECK (sig_title IN ('ok', 'sub', 'bad', 'na')),
    cross_source_mismatch_halt boolean NOT NULL,
    halt_reason          text,                 -- 例如 Y+RT、T+D；未熔断为 NULL
    signal_detail        jsonb NOT NULL DEFAULT '{}',     -- 四个信号各自的输入与比较证据
    run_id               text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id),
    CHECK (cross_source_mismatch_halt = (halt_reason IS NOT NULL)),
    CHECK (NOT cross_source_mismatch_halt OR is_weak)       -- 强关联不熔断（R17）
);

-- 共享 tconst 按 (tconst, movie_id) 去重后的逐对分类（R19）；只记录，不回写任务 4。
CREATE TABLE task7.shared_tconst_result (
    extraction_id    text NOT NULL REFERENCES public.extraction,
    stage_key        text NOT NULL,
    tconst           text NOT NULL,
    movie_id         text NOT NULL,           -- 任务 6 质量问题所在的作品
    other_movie_id   text NOT NULL,           -- 同一 tconst 的占有作品
    link_class       text NOT NULL CHECK (link_class IN ('false_positive_link', 'likely_duplicate_movie',
                                                         'undetermined')),
    basis            jsonb NOT NULL DEFAULT '{}',    -- 两作品的熔断、标题与导演信号
    source_issue_ids bigint[] NOT NULL,       -- task6.quality_issue 的来源问题
    run_id           text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, tconst, movie_id)
);

-- ------------------------------------------------------------------ 作品层标量阶段结果（movie_scalar，R20–R33）

CREATE TABLE task7.movie_scalar_stage_result (
    extraction_id        text NOT NULL REFERENCES public.extraction,
    stage_key            text NOT NULL,
    movie_id             text NOT NULL,
    title                text,
    original_title       text,
    release_year         integer,
    release_precision    text NOT NULL DEFAULT 'year' CHECK (release_precision = 'year'),   -- 只有年份，不补成日期（R24）
    runtime_minutes      integer,
    mpaa_rating          text,
    effective_tconst     text,
    tconst_source        text CHECK (tconst_source IN ('imdb_t4_link', 'imdb_t5_hint', 'imdb_t6_cooccur')),
    cross_source_mismatch_halt boolean NOT NULL DEFAULT false,
    inferred_upper_bound date,                -- 任务 6 推断上界，只作参考，绝不是 release_year 候选（R27）
    inferred_bound_precision text CHECK (inferred_bound_precision IN ('day', 'year')),
    inferred_bound_evidence text,
    field_selections     jsonb NOT NULL,      -- 五个标量字段的选择记录
    run_id               text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id)
);

-- 有 IMDb 片长时逐 edition 商品一行（R29–R31）。
CREATE TABLE task7.runtime_diff (
    extraction_id     text NOT NULL REFERENCES public.extraction,
    stage_key         text NOT NULL,
    movie_id          text NOT NULL,
    asin              text NOT NULL,
    page_runtime      integer NOT NULL,
    imdb_runtime      integer NOT NULL,
    delta             integer NOT NULL,       -- 页面 − IMDb
    diff_class        text NOT NULL CHECK (diff_class IN ('exact', 'tolerance_1_2', 'pal_evidence', 'variance_3_5',
                                                          'cut_or_multidisc', 'unexplained_6_15', 'unexplained_gt15')),
    pal_formula_match boolean NOT NULL,       -- |页面 − round(IMDb × 24/25)| ≤ 1，与是否有 PAL 证据无关（R30）
    has_pal_evidence  boolean NOT NULL,       -- video_system 断言含 PAL
    cut_type          text[] NOT NULL DEFAULT '{}',
    discs             integer,
    run_id            text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id, asin)
);

CREATE TABLE task7.movie_title_alias (
    extraction_id  text NOT NULL REFERENCES public.extraction,
    stage_key      text NOT NULL,
    movie_id       text NOT NULL,
    alias_kind     text NOT NULL CHECK (alias_kind IN ('primary', 'original', 'aka', 'page')),
    alias_ordinal  integer NOT NULL CHECK (alias_ordinal >= 1),
    alias          text NOT NULL,
    region         text,                      -- IMDb aka 来源缺失时为 NULL
    language       text,
    source_ref     jsonb NOT NULL,            -- IMDb：tconst、表、行号；页面：ASIN 与断言 ID
    run_id         text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id, alias_kind, alias_ordinal)
);

-- ------------------------------------------------------------------ 作品层多值阶段结果（movie_multivalue，R34–R43；T7-04 写入）

CREATE TABLE task7.movie_multivalue_stage_result (
    extraction_id   text NOT NULL REFERENCES public.extraction,
    stage_key       text NOT NULL,
    movie_id        text NOT NULL,
    cast_coverage   text NOT NULL CHECK (cast_coverage IN ('page_only', 'imdb_principals_only',
                                                          'page_and_imdb_principals', 'none')),
    genre_count     integer NOT NULL DEFAULT 0,
    director_count  integer NOT NULL DEFAULT 0,
    starring_count  integer NOT NULL DEFAULT 0,
    cast_count      integer NOT NULL DEFAULT 0,
    field_selections jsonb NOT NULL,          -- genres、director、starring、cast 的选择记录
    run_id          text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id)
);

CREATE TABLE task7.movie_genre (
    extraction_id   text NOT NULL REFERENCES public.extraction,
    stage_key       text NOT NULL,
    movie_id        text NOT NULL,
    genre           text NOT NULL,
    source_coverage text NOT NULL CHECK (source_coverage IN ('both', 'imdb_only', 'page_only')),
    raw_terms       text[] NOT NULL DEFAULT '{}',    -- 页面原始词面
    source_ref      jsonb NOT NULL DEFAULT '{}',     -- IMDb tconst、页面 ASIN 与区域
    run_id          text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id, genre)
);

-- 每个页面词面按作品一行（R35）：mapped、noise、unmapped；unmapped 不进入 genres。
CREATE TABLE task7.movie_genre_term (
    extraction_id   text NOT NULL REFERENCES public.extraction,
    stage_key       text NOT NULL,
    movie_id        text NOT NULL,
    section         text NOT NULL CHECK (section IN ('genre', 'breadcrumb')),
    term            text NOT NULL,                   -- 去 see more、还原下划线后的词面
    disposition     text NOT NULL CHECK (disposition IN ('mapped', 'noise', 'unmapped')),
    mapped_genres   text[] NOT NULL DEFAULT '{}',
    asins           text[] NOT NULL DEFAULT '{}',
    run_id          text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id, section, term),
    CHECK ((disposition = 'mapped') = (cardinality(mapped_genres) > 0))
);

CREATE TABLE task7.imdb_person_ext (
    extraction_id     text NOT NULL REFERENCES public.extraction,
    imdb_snapshot_id  text NOT NULL REFERENCES imdb.snapshot,
    person_id         text NOT NULL CHECK (person_id ~ '^ps_[0-9a-f]{16}$'),
    nconst            text NOT NULL,
    primary_name      text,
    birth_year        integer,
    death_year        integer,
    in_task5_identity boolean NOT NULL,        -- 任务 5 已有该锚点时直接取用其 person_id（R40）
    run_id            text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, imdb_snapshot_id, person_id),
    UNIQUE (extraction_id, imdb_snapshot_id, nconst)
);

-- 页面未关联人物与同作品同角色 IMDb 人物的对齐证据（R38、R39）。
CREATE TABLE task7.credit_alignment (
    extraction_id     text NOT NULL REFERENCES public.extraction,
    stage_key         text NOT NULL,
    movie_id          text NOT NULL,
    role              text NOT NULL CHECK (role IN ('director', 'starring', 'cast')),
    page_person_id    text NOT NULL,
    raw_name          text NOT NULL,           -- 页面原始署名，原样保留
    segment           integer NOT NULL CHECK (segment >= 0),    -- 多人串的第几段；单人为 0
    segment_name      text NOT NULL,
    resolved_person_id text,
    nconst            text,
    level             text CHECK (level IN ('L1', 'L2', 'L3', 'LI', 'L4')),
    outcome           text NOT NULL CHECK (outcome IN ('aligned', 'tie_not_aligned', 'no_match',
                                                       'unsplit_multi_person')),
    run_id            text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id, role, page_person_id, segment)
);

CREATE TABLE task7.movie_person_resolved (
    extraction_id    text NOT NULL REFERENCES public.extraction,
    stage_key        text NOT NULL,
    movie_id         text NOT NULL,
    person_id        text NOT NULL,
    role             text NOT NULL CHECK (role IN ('director', 'starring', 'cast')),
    source_coverage  text NOT NULL CHECK (source_coverage IN ('both', 'imdb_only', 'page_only')),
    status           text NOT NULL CHECK (status IN ('adopted', 'conflict_pending')),
    credit_order     integer CHECK (credit_order >= 1),
    imdb_ordering    integer,
    nconst           text,
    raw_names        text[] NOT NULL DEFAULT '{}',
    resolution_note  jsonb NOT NULL DEFAULT '{}',     -- 署名顺序各商品分布（R42）、对齐等级
    run_id           text NOT NULL REFERENCES task7.run,
    PRIMARY KEY (extraction_id, stage_key, movie_id, person_id, role)
);

-- ------------------------------------------------------------------ 冲突消解版本表（finalize_version 物化，T7-05）

-- 冲突类型闭集（R13）；两张选择表共用同一组取值。
CREATE TABLE task7.movie_resolved (
    conflict_version_id  text NOT NULL REFERENCES task7.conflict_version,
    movie_id             text NOT NULL,
    title                text,
    original_title       text,
    release_year         integer,
    release_precision    text NOT NULL DEFAULT 'year' CHECK (release_precision = 'year'),
    runtime_minutes      integer,
    mpaa_rating          text,
    effective_tconst     text,
    tconst_source        text CHECK (tconst_source IN ('imdb_t4_link', 'imdb_t5_hint', 'imdb_t6_cooccur')),
    cross_source_mismatch_halt boolean NOT NULL DEFAULT false,
    cast_coverage        text NOT NULL CHECK (cast_coverage IN ('page_only', 'imdb_principals_only',
                                                               'page_and_imdb_principals', 'none')),
    inferred_upper_bound date,
    inferred_bound_precision text CHECK (inferred_bound_precision IN ('day', 'year')),
    inferred_bound_evidence text,
    genre_count          integer NOT NULL DEFAULT 0,
    director_count       integer NOT NULL DEFAULT 0,
    starring_count       integer NOT NULL DEFAULT 0,
    cast_count           integer NOT NULL DEFAULT 0,
    PRIMARY KEY (conflict_version_id, movie_id)
);

CREATE TABLE task7.edition_resolved (
    conflict_version_id  text NOT NULL REFERENCES task7.conflict_version,
    asin                 text NOT NULL,
    product_kind         text NOT NULL CHECK (product_kind IN ('movie', 'collection')),
    movie_ids            text[] NOT NULL DEFAULT '{}',
    edition_title        text,
    edition_clean_title  text,
    runtime_minutes      integer,
    mpaa_rating          text,
    publisher            text,
    product_release_date date,
    media_release_date   date,
    listing_first_available_date date,
    date_flags           text[] NOT NULL DEFAULT '{}',
    discs                integer,
    media_format         text[] NOT NULL DEFAULT '{}',
    language             text[] NOT NULL DEFAULT '{}',
    subtitles            text[] NOT NULL DEFAULT '{}',
    aspect_ratio         text[] NOT NULL DEFAULT '{}',
    region               text[] NOT NULL DEFAULT '{}',
    cut_type             text[] NOT NULL DEFAULT '{}',
    video_system         text[] NOT NULL DEFAULT '{}',
    attributes_detail    jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (conflict_version_id, asin)
);

-- FieldSelection：每版本 × 实体 × 字段一行（R11）。
--  · (status = 'resolved') = (winning_value IS NOT NULL)；多值字段的 winning_value 是成员的 '|' 连接摘要。
--  · 非 resolved 时 selected_candidate_ids 为空；标量字段 missing 等价于 considered_candidate_ids 为空（R12）。
--  · 多值字段的成员明细在 movie_genre 与 movie_person_resolved，两个候选数组为空。
CREATE TABLE task7.movie_field_selection (
    conflict_version_id text NOT NULL REFERENCES task7.conflict_version,
    movie_id            text NOT NULL,
    field_name          text NOT NULL CHECK (field_name IN ('title', 'original_title', 'release_year',
                                                           'runtime_minutes', 'mpaa_rating', 'genres', 'director',
                                                           'starring', 'cast')),
    status              text NOT NULL CHECK (status IN ('resolved', 'conflict_unresolved', 'missing')),
    winning_value       text,
    selected_candidate_ids bigint[] NOT NULL DEFAULT '{}',
    considered_candidate_ids bigint[] NOT NULL DEFAULT '{}',
    conflict_type       text NOT NULL CHECK (conflict_type IN (
        'none', 'formatting_or_alias', 'unrated_vs_rated', 'minor_tolerance', 'framerate_or_edition',
        'cross_source_override', 'complementary_union', 'director_source_disagreement',
        'billing_conflict_resolved', 'semantic_distinction', 'intra_asin_publisher_conflict',
        'intra_edition_rating_conflict', 'intra_asin_runtime_conflict', 'cross_source_mismatch_halt',
        'severe_mismatch_unresolved', 'intra_source_tie_unresolved')),
    selection_rule      text NOT NULL,
    reason              text NOT NULL,
    confidence          numeric(4, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    disagreement_detail jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (conflict_version_id, movie_id, field_name),
    CHECK ((status = 'resolved') = (winning_value IS NOT NULL)),
    CHECK (status = 'resolved' OR cardinality(selected_candidate_ids) = 0),
    CHECK (field_name IN ('genres', 'director', 'starring', 'cast')
           OR ((status = 'missing') = (cardinality(considered_candidate_ids) = 0))),
    CHECK (field_name IN ('genres', 'director', 'starring', 'cast')
           OR status <> 'resolved' OR (cardinality(selected_candidate_ids) > 0
                                        AND selected_candidate_ids <@ considered_candidate_ids)),
    CHECK (field_name NOT IN ('genres', 'director', 'starring', 'cast')
           OR (cardinality(selected_candidate_ids) = 0 AND cardinality(considered_candidate_ids) = 0))
);

CREATE TABLE task7.edition_field_selection (
    conflict_version_id text NOT NULL REFERENCES task7.conflict_version,
    asin                text NOT NULL,
    field_name          text NOT NULL CHECK (field_name IN ('edition_title', 'runtime_minutes', 'mpaa_rating',
                                                           'publisher', 'product_release_date', 'discs')),
    status              text NOT NULL CHECK (status IN ('resolved', 'conflict_unresolved', 'missing')),
    winning_value       text,
    selected_candidate_ids bigint[] NOT NULL DEFAULT '{}',
    considered_candidate_ids bigint[] NOT NULL DEFAULT '{}',
    conflict_type       text NOT NULL CHECK (conflict_type IN (
        'none', 'formatting_or_alias', 'unrated_vs_rated', 'minor_tolerance', 'framerate_or_edition',
        'cross_source_override', 'complementary_union', 'director_source_disagreement',
        'billing_conflict_resolved', 'semantic_distinction', 'intra_asin_publisher_conflict',
        'intra_edition_rating_conflict', 'intra_asin_runtime_conflict', 'cross_source_mismatch_halt',
        'severe_mismatch_unresolved', 'intra_source_tie_unresolved')),
    selection_rule      text NOT NULL,
    reason              text NOT NULL,
    confidence          numeric(4, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    disagreement_detail jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (conflict_version_id, asin, field_name),
    CHECK ((status = 'resolved') = (winning_value IS NOT NULL)),
    CHECK (status = 'resolved' OR cardinality(selected_candidate_ids) = 0),
    CHECK ((status = 'missing') = (cardinality(considered_candidate_ids) = 0)),
    CHECK (status <> 'resolved' OR (cardinality(selected_candidate_ids) > 0
                                     AND selected_candidate_ids <@ considered_candidate_ids))
);

CREATE TABLE task7.shared_tconst_conflict (
    conflict_version_id text NOT NULL REFERENCES task7.conflict_version,
    tconst              text NOT NULL,
    movie_id            text NOT NULL,
    other_movie_id      text NOT NULL,
    link_class          text NOT NULL CHECK (link_class IN ('false_positive_link', 'likely_duplicate_movie',
                                                            'undetermined')),
    basis               jsonb NOT NULL DEFAULT '{}',
    source_issue_ids    bigint[] NOT NULL,
    PRIMARY KEY (conflict_version_id, tconst, movie_id)
);

-- ------------------------------------------------------------------ 评价（R61–R66；T7-07 使用）

-- 开发池：按 md5(movie_id) 排序前 20% 的作品，首次全量运行前冻结，冻结后不变；评价样本不含开发池。
CREATE TABLE task7.dev_pool (
    extraction_id         text NOT NULL REFERENCES public.extraction,
    movie_id              text NOT NULL,
    t4_mapping_version_id text NOT NULL,
    frozen_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (extraction_id, movie_id)
);

CREATE TABLE task7.eval_set (
    eval_set_id         text PRIMARY KEY,
    extraction_id       text NOT NULL REFERENCES public.extraction,
    conflict_version_id text NOT NULL REFERENCES task7.conflict_version,
    rule_version        text NOT NULL,
    strata              jsonb NOT NULL,        -- 层 → {target, actual, filled}
    exported_file       text,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- 系统判定存在库里用于算指标，不导出（R64：导出不含胜出值、状态、冲突类型与层号）。
CREATE TABLE task7.eval_item (
    eval_set_id    text NOT NULL REFERENCES task7.eval_set,
    item_id        text NOT NULL,
    stratum        text NOT NULL,
    entity_type    text NOT NULL CHECK (entity_type IN ('movie', 'edition')),
    entity_id      text NOT NULL,
    target_field   text NOT NULL,
    system_status  text,
    system_value   text,
    conflict_type  text,
    PRIMARY KEY (eval_set_id, item_id)
);

CREATE TABLE task7.eval_label (
    eval_set_id         text NOT NULL,
    item_id             text NOT NULL,
    human_action        text NOT NULL CHECK (human_action IN ('resolve', 'keep_conflict', 'unsure')),
    human_winning_value text,
    human_evidence      text,
    ai_doubt            boolean NOT NULL DEFAULT false,
    note                text,
    annotator           text NOT NULL,
    labeled_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_sha256  text NOT NULL,
    PRIMARY KEY (eval_set_id, item_id),
    FOREIGN KEY (eval_set_id, item_id) REFERENCES task7.eval_item,
    CHECK ((human_action = 'resolve') = (human_winning_value IS NOT NULL))
);

INSERT INTO public.schema_migration (version) VALUES ('601');
