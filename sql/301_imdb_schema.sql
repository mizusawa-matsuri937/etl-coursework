-- 任务 4：IMDb 参考数据（schema imdb）。
-- 幂等：由 etl_task4.imdb.apply_imdb_schema 通过 public.schema_migration 判断是否已应用。
-- 与任务 1–3 的表没有任何外键；每张表都带 snapshot_id，源记录定位 = (快照, 文件名, line_no, tconst/nconst)（spec R04–R06）。
-- 只装载 titleType 为 movie、tvMovie、video 的作品及其别名、导演、主要演职员与人名（R05）；
-- 其他 5 类（tvSeries 等）只在 title_other 里留标题，仅用于识别 R15 的 non_movie_title_type，不进入候选。

CREATE SCHEMA imdb;

-- 一个固定快照：装载是单个事务，失败整体回滚，状态只会是 loading/loaded/failed 之一。
CREATE TABLE imdb.snapshot (
    snapshot_id    text PRIMARY KEY CHECK (snapshot_id ~ '^imdb_[0-9A-Za-z._-]+$'),
    base_url       text NOT NULL,
    downloaded_at  timestamptz NOT NULL,
    status         text NOT NULL DEFAULT 'loading' CHECK (status IN ('loading', 'loaded', 'failed')),
    loader_version text NOT NULL,
    loaded_at      timestamptz,
    failure_reason text,
    CHECK ((status = 'loaded') = (loaded_at IS NOT NULL))
);

-- 下载记录与装载核对：哈希、大小来自 download.json，装载前逐个重算比对（R04）。
CREATE TABLE imdb.source_file (
    snapshot_id   text NOT NULL REFERENCES imdb.snapshot ON DELETE CASCADE,
    file_name     text NOT NULL,
    url           text NOT NULL,
    size_bytes    bigint NOT NULL CHECK (size_bytes >= 0),
    sha256        text NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    last_modified text,
    rows_read     bigint NOT NULL DEFAULT 0 CHECK (rows_read >= 0),     -- 不含表头
    rows_kept     bigint NOT NULL DEFAULT 0 CHECK (rows_kept >= 0),
    filter_counts jsonb NOT NULL DEFAULT '{}',                          -- 过滤原因 → 行数
    PRIMARY KEY (snapshot_id, file_name)
);

CREATE TABLE imdb.title (
    snapshot_id     text NOT NULL REFERENCES imdb.snapshot ON DELETE CASCADE,
    tconst          text NOT NULL,
    title_type      text NOT NULL CHECK (title_type IN ('movie', 'tvMovie', 'video')),
    primary_title   text,
    original_title  text,
    is_adult        boolean,
    start_year      integer,
    end_year        integer,
    runtime_minutes integer,
    genres          text[],
    line_no         bigint NOT NULL CHECK (line_no >= 2),
    PRIMARY KEY (snapshot_id, tconst)
);

-- R15：不作为电影候选的作品类型，只保留标题与年份。
CREATE TABLE imdb.title_other (
    snapshot_id    text NOT NULL REFERENCES imdb.snapshot ON DELETE CASCADE,
    tconst         text NOT NULL,
    title_type     text NOT NULL,
    primary_title  text,
    original_title text,
    start_year     integer,
    line_no        bigint NOT NULL CHECK (line_no >= 2),
    PRIMARY KEY (snapshot_id, tconst)
);

CREATE TABLE imdb.title_aka (
    snapshot_id text NOT NULL,
    tconst      text NOT NULL,
    ordering    integer NOT NULL,
    title       text,
    region      text,
    language    text,
    types       text,
    attributes  text,
    is_original boolean,
    line_no     bigint NOT NULL CHECK (line_no >= 2),
    PRIMARY KEY (snapshot_id, tconst, ordering),
    FOREIGN KEY (snapshot_id, tconst) REFERENCES imdb.title ON DELETE CASCADE
);

-- 来自 title.crew 的 directors 逗号串；ordinal 是串内序号。
CREATE TABLE imdb.title_director (
    snapshot_id text NOT NULL,
    tconst      text NOT NULL,
    ordinal     integer NOT NULL CHECK (ordinal >= 1),
    nconst      text NOT NULL,
    line_no     bigint NOT NULL CHECK (line_no >= 2),
    PRIMARY KEY (snapshot_id, tconst, ordinal),
    FOREIGN KEY (snapshot_id, tconst) REFERENCES imdb.title ON DELETE CASCADE
);

-- 只装 category 为 actor、actress、director 的行（主要演职员，不是全部演员）。
CREATE TABLE imdb.title_principal (
    snapshot_id text NOT NULL,
    tconst      text NOT NULL,
    ordering    integer NOT NULL,
    nconst      text NOT NULL,
    category    text NOT NULL,
    job         text,
    characters  text,
    line_no     bigint NOT NULL CHECK (line_no >= 2),
    PRIMARY KEY (snapshot_id, tconst, ordering),
    FOREIGN KEY (snapshot_id, tconst) REFERENCES imdb.title ON DELETE CASCADE
);

-- 人名：不加到导演、演员的外键（人名文件最后读取）；装载后由核对检查引用是否全部有效。
CREATE TABLE imdb.name (
    snapshot_id        text NOT NULL REFERENCES imdb.snapshot ON DELETE CASCADE,
    nconst             text NOT NULL,
    primary_name       text,
    birth_year         integer,
    death_year         integer,
    primary_profession text,
    line_no            bigint NOT NULL CHECK (line_no >= 2),
    PRIMARY KEY (snapshot_id, nconst)
);

CREATE INDEX title_principal_nconst_idx ON imdb.title_principal (snapshot_id, nconst);
CREATE INDEX title_director_nconst_idx ON imdb.title_director (snapshot_id, nconst);

INSERT INTO public.schema_migration (version) VALUES ('301');
