-- 任务 5：人物姓名归一与消歧（schema task5）。
-- 幂等：由 etl_task5.run.apply_task5_schema 通过 public.schema_migration 判断是否已应用（须先有 302）。
-- 只引用 public.extraction、task2.product_manifest、task4.*、imdb.snapshot，不在任务 1–4 的表上加列或约束（spec R46）。
-- 所有读取必须带抽取 ID；输入键 = 抽取 × 任务 4 映射版本 × 解析版本 × 规范化版本 × 规则版本 × IMDb 快照 × 人工决策集（R01、R42）。
-- 本文件随任务 5 各步骤分阶段追加表（T5-01：运行、提及、质量问题、人工决策；之后是外部 ID、人物组、人物映射、MoviePerson、评价）。

CREATE SCHEMA task5;

-- ------------------------------------------------------------------ 运行与批次

CREATE TABLE task5.run (
    run_id            text PRIMARY KEY,
    kind              text NOT NULL CHECK (kind IN ('person', 'human_import', 'evaluate')),
    extraction_id     text NOT NULL REFERENCES public.extraction,
    t4_mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    parser_version    text NOT NULL,
    norm_version      text NOT NULL,
    rule_version      text NOT NULL,
    imdb_snapshot_id  text NOT NULL REFERENCES imdb.snapshot,
    human_set_hash    text NOT NULL,
    input_key         text NOT NULL,       -- 上面七项的稳定串；已有成功运行时新运行记 reused（R42）
    person_mapping_version_id text,        -- 本次运行产生的人物映射版本；reused 或未到聚类步骤时为 NULL（外键随 person_mapping_version 表追加）
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

CREATE INDEX run_input_idx ON task5.run (extraction_id, input_key, status);

-- 与该批数据在同一事务提交的批次审计（R43）；恢复判据是 entity_stage 里已提交的实体。
CREATE TABLE task5.run_batch (
    run_id       text NOT NULL REFERENCES task5.run,
    stage        text NOT NULL,
    batch_no     integer NOT NULL CHECK (batch_no >= 1),
    first_asin   text,
    last_asin    text,
    rows_written bigint NOT NULL DEFAULT 0 CHECK (rows_written >= 0),
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (run_id, stage, batch_no)
);

-- 每个实体在某阶段、某组版本下是否已处理；与该批数据同事务写入，重跑只处理没有标记的实体。
-- stage_key：mentions 只取解析与规范化版本；其余阶段再加任务 4 映射版本、规则版本与 IMDb 快照（design“运行与版本”）。
CREATE TABLE task5.entity_stage (
    extraction_id text NOT NULL REFERENCES public.extraction,
    stage         text NOT NULL CHECK (stage IN ('mentions', 'credits', 'filmography', 'decide_pair')),
    stage_key     text NOT NULL,
    entity_id     text NOT NULL,           -- ASIN，或 decide_pair 阶段的 block:<生成器>:<分块键>
    run_id        text NOT NULL REFERENCES task5.run,
    batch_no      integer NOT NULL,
    rows_written  integer NOT NULL DEFAULT 0,
    PRIMARY KEY (extraction_id, stage, stage_key, entity_id)
);

CREATE TABLE task5.quality_issue (
    issue_id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id        text REFERENCES task5.run,
    extraction_id text NOT NULL REFERENCES public.extraction,
    entity_id     text NOT NULL,
    issue_code    text NOT NULL,           -- ambiguous_names、block_overflow、edge_blocked、human_vs_veto、order_conflict、two_nconst_in_group 等
    detail        jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX quality_issue_idx ON task5.quality_issue (extraction_id, issue_code, entity_id);

-- ------------------------------------------------------------------ 人物提及（R02–R07）

-- 每个导演、演员名字一行。mention_key = <ASIN>:<区域>:<页面字段序号>:<列表位置>，可直接定位 task3.page_field；
-- 唯一键含解析与规范化版本，所以只改规则版本不会重新抽取提及（AC09）。
CREATE TABLE task5.person_mention (
    extraction_id    text NOT NULL REFERENCES public.extraction,
    parser_version   text NOT NULL,
    norm_version     text NOT NULL,
    mention_key      text NOT NULL,
    asin             text NOT NULL,
    product_kind     text NOT NULL CHECK (product_kind IN ('movie', 'collection')),
    section          text NOT NULL CHECK (section IN ('byline', 'detail_bullets')),
    page_ordinal     integer NOT NULL CHECK (page_ordinal >= 1),    -- task3.page_field.ordinal
    list_position    integer NOT NULL CHECK (list_position >= 1),   -- 逗号串内的位置；署名行为 1
    label_raw        text,
    role_class       text NOT NULL CHECK (role_class IN ('director', 'actor')),
    is_byline        boolean NOT NULL,
    byline_rank      integer CHECK (byline_rank >= 1),              -- 署名行内同标签的第几行；详情要点为 NULL
    raw_value        text NOT NULL,                                 -- 原样保留（R06）
    display_name     text NOT NULL,                                 -- 倒序、去昵称括号称谓之后（保留后缀）
    nickname         text,
    disambiguator    text,                                          -- 罗马数字括号段，如 III
    paren_text       text,                                          -- 其他括号段原文
    suffix           text,
    honorific        text,
    strict_key       text NOT NULL,
    loose_key        text NOT NULL,
    tokenset_key     text NOT NULL,
    family_key       text NOT NULL,
    initial_key      text NOT NULL,
    flags            jsonb NOT NULL DEFAULT '{}',                   -- reversed、encoding_damaged
    status           text NOT NULL CHECK (status IN ('resolvable', 'placeholder', 'ambiguous')),
    t4_assertion_id  bigint REFERENCES task4.field_assertion,       -- 能对上任务 4 的导演、演员断言时记录
    source_record_id bigint REFERENCES task4.source_record,         -- 页面快照
    run_id           text NOT NULL REFERENCES task5.run,
    PRIMARY KEY (extraction_id, parser_version, norm_version, mention_key),
    FOREIGN KEY (extraction_id, asin) REFERENCES task2.product_manifest,
    CHECK (is_byline = (section = 'byline')),
    CHECK (byline_rank IS NULL OR is_byline)
);

CREATE INDEX person_mention_strict_idx ON task5.person_mention (extraction_id, norm_version, strict_key);
CREATE INDEX person_mention_family_idx ON task5.person_mention (extraction_id, norm_version, family_key);
CREATE INDEX person_mention_asin_idx ON task5.person_mention (extraction_id, asin);

-- ------------------------------------------------------------------ 人工决策（R29）

-- 必须合并、不得合并、移出；人工决策集的哈希进入输入键。left_entity 是 mention_key；
-- right_entity 是 mention_key 或 imdb:<nconst>，detach 为 NULL。
CREATE TABLE task5.human_decision (
    human_decision_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_id      text NOT NULL REFERENCES public.extraction,
    set_hash           text NOT NULL,            -- 导入这条决策之后整个决策集的哈希
    kind               text NOT NULL CHECK (kind IN ('must_link', 'cannot_link', 'detach')),
    left_entity        text NOT NULL,
    right_entity       text,
    note               text,
    annotator          text NOT NULL,
    decided_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_sha256 text NOT NULL,
    run_id             text REFERENCES task5.run,
    CHECK ((kind = 'detach') = (right_entity IS NULL))
);

CREATE UNIQUE INDEX human_decision_key_idx ON task5.human_decision
    (extraction_id, kind, left_entity, (coalesce(right_entity, '')));

-- ------------------------------------------------------------------ 外部人物 ID（R10–R16）

-- 候选：提及与 IMDb 人物的一个可能对应。route：credits（作品演职员）或 filmography（已核实人物的作品年表反查）。
-- stage_key 同 entity_stage 的 credits 阶段键，所以换规则版本、映射版本或快照得到新的候选集。
CREATE TABLE task5.link_candidate (
    link_candidate_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_id     text NOT NULL REFERENCES public.extraction,
    stage_key         text NOT NULL,
    mention_key       text NOT NULL,
    route             text NOT NULL CHECK (route IN ('credits', 'filmography')),
    tconst            text NOT NULL,
    nconst            text NOT NULL,
    category          text NOT NULL,          -- IMDb 的 director、actor、actress；年表反查记提及的角色类别
    level             text NOT NULL CHECK (level IN ('X1', 'X2', 'X3', 'X4')),
    cross_role        boolean NOT NULL DEFAULT false,
    work_movie_id     text,                   -- 商品所在（或合集成员所属）的任务 4 作品
    anchor_link_decision_id bigint,           -- 年表反查：该 nconst 在哪条途径一判定里被核实
    matched_title_key text,
    title_key_source  text CHECK (title_key_source IN ('primary', 'original', 'aka')),
    evidence          jsonb NOT NULL DEFAULT '{}',   -- IMDb 证据：表、序号、行号、人名行号；年表：成员序号、IMDb 年份、商品标题年份、标题键行号
    UNIQUE (extraction_id, stage_key, mention_key, route, tconst, nconst)
);

CREATE INDEX link_candidate_nconst_idx ON task5.link_candidate (extraction_id, stage_key, nconst);

-- R13：每个候选一条决策（未采用的也保存）；同一候选、同一规则版本只有一条。一条提及最多一个 auto（两条途径合计）。
CREATE TABLE task5.link_decision (
    link_decision_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    link_candidate_id bigint NOT NULL REFERENCES task5.link_candidate,
    rule_version      text NOT NULL,
    run_id            text NOT NULL REFERENCES task5.run,
    tier              text NOT NULL CHECK (tier IN ('auto', 'review', 'reject')),
    reason            text NOT NULL,
    confidence        numeric(4, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    thresholds        jsonb NOT NULL,
    decided_by        text NOT NULL DEFAULT 'system' CHECK (decided_by IN ('system', 'human')),
    decided_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (link_candidate_id, rule_version)
);

ALTER TABLE task5.link_candidate ADD FOREIGN KEY (anchor_link_decision_id) REFERENCES task5.link_decision;

-- R16：任务 4 升级线索。只记录，不被任何步骤读来修改作品归属。
CREATE TABLE task5.t4_upgrade_hint (
    extraction_id     text NOT NULL REFERENCES public.extraction,
    stage_key         text NOT NULL,
    asin              text NOT NULL,
    member_ordinal    integer NOT NULL,       -- 合集成员序号，电影类商品为 0
    tconst            text NOT NULL,
    mention_keys      text[] NOT NULL,
    link_decision_ids bigint[] NOT NULL,
    mention_count     integer NOT NULL CHECK (mention_count >= 2),
    PRIMARY KEY (extraction_id, stage_key, asin, member_ordinal, tconst)
);

-- ------------------------------------------------------------------ 作品内人物组与跨作品候选（R18、R19、R25）

CREATE TABLE task5.person_group (
    extraction_id text NOT NULL REFERENCES public.extraction,
    stage_key     text NOT NULL,
    group_key     text NOT NULL,            -- <作品单位>|<组内最小 mention_key>
    work_unit     text NOT NULL,            -- movie:<movie_id>，或合集未归属时 collection:<ASIN>
    movie_id      text,
    role_classes  text[] NOT NULL,
    strict_key    text NOT NULL,
    loose_key     text NOT NULL,
    tokenset_key  text NOT NULL,
    family_key    text NOT NULL,
    initial_key   text NOT NULL,
    nconst        text,                     -- 组内自动关联的外部 ID（最多一个）
    nconst_route  text,
    year          integer,
    publishers    text[] NOT NULL DEFAULT '{}',
    rarity        jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (extraction_id, stage_key, group_key)
);

CREATE INDEX person_group_strict_idx ON task5.person_group (extraction_id, stage_key, strict_key);

CREATE TABLE task5.group_member (
    extraction_id text NOT NULL,
    stage_key     text NOT NULL,
    group_key     text NOT NULL,
    mention_key   text NOT NULL,
    reason        text NOT NULL CHECK (reason IN ('same_strict', 'same_tokenset', 'linked_same_nconst', 'singleton')),
    PRIMARY KEY (extraction_id, stage_key, mention_key),
    FOREIGN KEY (extraction_id, stage_key, group_key) REFERENCES task5.person_group
);

CREATE TABLE task5.person_candidate (
    person_candidate_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    extraction_id text NOT NULL REFERENCES public.extraction,
    stage_key     text NOT NULL,
    group_a       text NOT NULL,
    group_b       text NOT NULL,
    generator     text NOT NULL CHECK (generator IN ('C1', 'C2', 'C3', 'C4')),
    UNIQUE (extraction_id, stage_key, group_a, group_b),
    CHECK (group_a < group_b)
);

-- R25：每个跨作品候选、每个规则版本、每一轮至多一行；候选的最终判定是最大轮次那一行。
CREATE TABLE task5.person_decision (
    person_decision_id  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_candidate_id bigint NOT NULL REFERENCES task5.person_candidate,
    rule_version        text NOT NULL,
    round               integer NOT NULL CHECK (round >= 1),
    run_id              text NOT NULL REFERENCES task5.run,
    tier                text NOT NULL CHECK (tier IN ('auto', 'review', 'reject')),
    score               numeric(6, 2) NOT NULL,
    confidence          numeric(4, 2) NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    veto                text[] NOT NULL DEFAULT '{}',
    reason              text NOT NULL,
    features            jsonb NOT NULL,
    thresholds          jsonb NOT NULL,
    decided_by          text NOT NULL DEFAULT 'system' CHECK (decided_by IN ('system', 'human')),
    decided_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (person_candidate_id, rule_version, round),
    CHECK (tier <> 'auto' OR cardinality(veto) = 0)
);

-- ------------------------------------------------------------------ 人物、映射版本与别名（R24–R29）

-- R26：person_id = ps_ + sha256(anchor_key) 前 16 位，不含原始 ID；按锚点登记，生成后不复用。
CREATE TABLE task5.person_identity (
    anchor_key  text PRIMARY KEY,           -- imdb:nm… 或 mention:<mention_key>
    person_id   text NOT NULL UNIQUE CHECK (person_id ~ '^ps_[0-9a-f]{16}$'),
    created_run text NOT NULL REFERENCES task5.run,
    created_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE task5.person_mapping_version (
    person_mapping_version_id text PRIMARY KEY,
    extraction_id         text NOT NULL REFERENCES public.extraction,
    parent_version_id     text REFERENCES task5.person_mapping_version,
    run_id                text NOT NULL REFERENCES task5.run,
    input_key             text NOT NULL,
    t4_mapping_version_id text NOT NULL REFERENCES task4.mapping_version,
    parser_version        text NOT NULL,
    norm_version          text NOT NULL,
    rule_version          text NOT NULL,
    imdb_snapshot_id      text NOT NULL REFERENCES imdb.snapshot,
    human_set_hash        text NOT NULL,
    counts                jsonb NOT NULL,   -- R29、R40 的数量
    created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (extraction_id, input_key)
);

ALTER TABLE task5.run ADD FOREIGN KEY (person_mapping_version_id) REFERENCES task5.person_mapping_version;
ALTER TABLE task5.human_decision ADD COLUMN person_mapping_version_id text REFERENCES task5.person_mapping_version;

CREATE TABLE task5.person (
    person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    person_id          text NOT NULL REFERENCES task5.person_identity (person_id),
    status             text NOT NULL CHECK (status IN ('external_verified', 'page_only', 'review_pending')),
    has_external_id    boolean NOT NULL,
    external_route     text CHECK (external_route IN ('credits', 'filmography', 'co_credit', 'human')),
    anchor_key         text NOT NULL,
    canonical_name     text NOT NULL,
    canonical_source   text NOT NULL,       -- imdb:<nconst> 或 mention:<mention_key>
    mention_count      integer NOT NULL,
    work_count         integer NOT NULL,
    pending_candidates integer NOT NULL DEFAULT 0,
    PRIMARY KEY (person_mapping_version_id, person_id),
    CHECK (has_external_id = (status = 'external_verified'))
);

CREATE TABLE task5.person_external_id (
    person_mapping_version_id text NOT NULL,
    person_id          text NOT NULL,
    system             text NOT NULL CHECK (system = 'imdb'),
    external_id        text NOT NULL,       -- nconst
    snapshot_id        text NOT NULL REFERENCES imdb.snapshot,
    name_line_no       bigint,
    birth_year         integer,
    death_year         integer,
    primary_profession text,
    basis_decision_ids bigint[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (person_mapping_version_id, person_id, system),
    FOREIGN KEY (person_mapping_version_id, person_id) REFERENCES task5.person
);

CREATE TABLE task5.mention_person (
    person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    extraction_id  text NOT NULL,
    parser_version text NOT NULL,
    norm_version   text NOT NULL,
    mention_key    text NOT NULL,
    person_id      text NOT NULL,
    group_key      text NOT NULL,
    attribution    text NOT NULL CHECK (attribution IN ('movie', 'unattributed')),
    basis          text NOT NULL CHECK (basis IN ('credits', 'filmography', 'within_work', 'co_credit', 'human',
                                                  'singleton')),
    round          integer,
    PRIMARY KEY (person_mapping_version_id, mention_key),
    FOREIGN KEY (person_mapping_version_id, person_id) REFERENCES task5.person,
    FOREIGN KEY (extraction_id, parser_version, norm_version, mention_key) REFERENCES task5.person_mention
);

CREATE INDEX mention_person_person_idx ON task5.mention_person (person_mapping_version_id, person_id);

CREATE TABLE task5.person_alias (
    person_mapping_version_id text NOT NULL,
    person_id    text NOT NULL,
    alias        text NOT NULL,
    alias_types  text[] NOT NULL,
    source       text NOT NULL CHECK (source IN ('page', 'imdb')),
    occurrences  integer NOT NULL,
    PRIMARY KEY (person_mapping_version_id, person_id, alias),
    FOREIGN KEY (person_mapping_version_id, person_id) REFERENCES task5.person
);

-- 由新旧两个人物映射版本逐提及对比生成（R44）。
CREATE TABLE task5.mapping_event (
    event_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    mention_key text,
    event       text NOT NULL CHECK (event IN ('create', 'merge', 'split', 'move', 'retire')),
    from_person_id text,
    to_person_id   text,
    reason      text,
    decision_ref   text                    -- link_decision:<id>、person_decision:<id> 或 human_decision:<id>
);

CREATE INDEX mapping_event_idx ON task5.mapping_event (person_mapping_version_id, event);

-- 迭代每一轮的结果（R21）与一致性检查记录（R23）。
CREATE TABLE task5.iteration_round (
    person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    round               integer NOT NULL,
    new_auto_edges      integer NOT NULL,
    new_merged_persons  integer NOT NULL,
    blocked_edges       integer NOT NULL,
    started_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at         timestamptz,
    PRIMARY KEY (person_mapping_version_id, round)
);

CREATE TABLE task5.cluster_check (
    check_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    round       integer,
    group_a     text NOT NULL,
    group_b     text NOT NULL,
    conflict    text NOT NULL CHECK (conflict IN ('two_external_ids', 'era_conflict', 'different_disambiguator')),
    action      text NOT NULL,             -- edge_blocked、kept_by_human
    resolved_by text NOT NULL CHECK (resolved_by IN ('system', 'human'))
);

-- ------------------------------------------------------------------ MoviePerson（R31–R34）

-- 以（人物映射版本，作品，人物，角色）为主键；同一人在同一作品可以有多个角色。
-- credit_order：director、starring 取署名行序号，cast 有署名行来源时取其序号，只有详情要点来源时为 NULL；多个来源不一致取最小并设 order_conflict。
CREATE TABLE task5.movie_person (
    person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    movie_id       text NOT NULL,
    person_id      text NOT NULL,
    role           text NOT NULL CHECK (role IN ('director', 'starring', 'cast')),
    credit_order   integer CHECK (credit_order >= 1),
    order_conflict boolean NOT NULL DEFAULT false,
    source_labels  text[] NOT NULL,           -- byline:Actor、detail_bullets:Actors、collection:<ASIN>
    mention_count  integer NOT NULL CHECK (mention_count >= 1),
    PRIMARY KEY (person_mapping_version_id, movie_id, person_id, role),
    FOREIGN KEY (person_mapping_version_id, person_id) REFERENCES task5.person
);

CREATE INDEX movie_person_person_idx ON task5.movie_person (person_mapping_version_id, person_id);

-- 每条关系的全部来源提及（R34）。
CREATE TABLE task5.movie_person_source (
    person_mapping_version_id text NOT NULL,
    movie_id      text NOT NULL,
    person_id     text NOT NULL,
    role          text NOT NULL,
    mention_key   text NOT NULL,
    asin          text NOT NULL,
    byline_rank   integer,
    list_position integer NOT NULL,
    PRIMARY KEY (person_mapping_version_id, movie_id, person_id, role, mention_key),
    FOREIGN KEY (person_mapping_version_id, movie_id, person_id, role) REFERENCES task5.movie_person,
    FOREIGN KEY (person_mapping_version_id, mention_key) REFERENCES task5.mention_person
);

-- ------------------------------------------------------------------ 评价（R35–R38）

-- 开发池：按 md5(姓氏键) 排序的前 20% 姓氏键，冻结后不变；评价对的两边姓氏键都不得在池内。
CREATE TABLE task5.dev_pool (
    extraction_id text NOT NULL REFERENCES public.extraction,
    family_key    text NOT NULL,
    norm_version  text NOT NULL,
    frozen_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (extraction_id, norm_version, family_key)
);

CREATE TABLE task5.eval_set (
    eval_set_id               text PRIMARY KEY,
    extraction_id             text NOT NULL REFERENCES public.extraction,
    person_mapping_version_id text NOT NULL REFERENCES task5.person_mapping_version,
    rule_version              text NOT NULL,
    strata                    jsonb NOT NULL,       -- 层 → {target, actual, filled}
    exported_file             text,
    created_at                timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE task5.eval_pair (
    eval_set_id     text NOT NULL REFERENCES task5.eval_set,
    pair_id         text NOT NULL,
    stratum         text NOT NULL,
    left_mention    text NOT NULL,           -- 左右两个组的代表提及
    right_mention   text NOT NULL,
    left_group      text NOT NULL,
    right_group     text NOT NULL,
    left_person     text NOT NULL,
    right_person    text NOT NULL,
    predicted_same  boolean NOT NULL,        -- 导出时的人物映射版本里双方是否同一 person_id（含人工决策的合并不算，见 evaluate）
    route           text,                    -- credits、filmography、co_credit（合并途径），未合并为 NULL
    PRIMARY KEY (eval_set_id, pair_id)
);

CREATE TABLE task5.eval_label (
    eval_set_id        text NOT NULL,
    pair_id            text NOT NULL,
    label              text NOT NULL CHECK (label IN ('same', 'different', 'unsure')),
    annotator          text NOT NULL,
    note               text,
    labeled_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_sha256 text NOT NULL,
    PRIMARY KEY (eval_set_id, pair_id),
    FOREIGN KEY (eval_set_id, pair_id) REFERENCES task5.eval_pair
);

CREATE TABLE task5.cluster_sample (
    eval_set_id   text NOT NULL REFERENCES task5.eval_set,
    person_id     text NOT NULL,
    work_count    integer NOT NULL,
    from_iteration boolean NOT NULL DEFAULT false,   -- 人物含经合作者佐证合入的提及
    PRIMARY KEY (eval_set_id, person_id)
);

CREATE TABLE task5.cluster_label (
    eval_set_id        text NOT NULL,
    person_id          text NOT NULL,
    mixed              boolean NOT NULL,     -- true = 混入了不同的人
    problem_mentions   text[] NOT NULL DEFAULT '{}',
    annotator          text NOT NULL,
    note               text,
    labeled_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    import_file_sha256 text NOT NULL,
    PRIMARY KEY (eval_set_id, person_id),
    FOREIGN KEY (eval_set_id, person_id) REFERENCES task5.cluster_sample
);

INSERT INTO public.schema_migration (version) VALUES ('401');
