import pytest

from etl_task1.config import MIB, ConfigError, load_settings


def test_defaults(base_env):
    s = load_settings(base_env)
    assert (s.scope, s.sample_max_records) == ("full", None)
    assert (s.batch_rows, s.batch_bytes) == (10_000, 32 * MIB)
    assert s.db_port == 5432 and s.encoding == "utf-8" and s.fault_injection is None


@pytest.mark.parametrize("key", ["SOURCE_GZIP_PATH", "SOURCE_MANIFEST_PATH", "ETL_DB_HOST",
                                 "ETL_DB_NAME", "ETL_DB_USER", "ETL_DB_PASSWORD"])
def test_missing_required(base_env, key):
    del base_env[key]
    with pytest.raises(ConfigError, match=key):
        load_settings(base_env)


@pytest.mark.parametrize("key,value", [("ETL_BATCH_ROWS", "0"), ("ETL_BATCH_ROWS", "abc"),
                                       ("ETL_BATCH_BYTES", "-1"), ("ETL_DB_PORT", "x"),
                                       ("ETL_SCOPE", "partial"), ("ETL_ENCODING", "latin-1")])
def test_invalid_values(base_env, key, value):
    with pytest.raises(ConfigError):
        load_settings({**base_env, key: value})


def test_sample_scope_requires_explicit_limit(base_env):
    with pytest.raises(ConfigError, match="sample"):
        load_settings({**base_env, "ETL_SCOPE": "sample"})


def test_full_scope_rejects_record_limit(base_env):
    with pytest.raises(ConfigError, match="full"):
        load_settings({**base_env, "ETL_SCOPE": "full", "ETL_SAMPLE_MAX_RECORDS": "100"})
    s = load_settings({**base_env, "ETL_SCOPE": "sample", "ETL_SAMPLE_MAX_RECORDS": "100"})
    assert s.sample_max_records == 100


def test_fault_injection_only_in_test_env(base_env):
    with pytest.raises(ConfigError, match="故障注入"):
        load_settings({**base_env, "ETL_FAULT_INJECTION": "before_commit"})
    s = load_settings({**base_env, "ETL_ENV": "test", "ETL_FAULT_INJECTION": "before_commit"})
    assert s.fault_injection == "before_commit"


def test_password_not_leaked(base_env):
    s = load_settings(base_env)
    secret = base_env["ETL_DB_PASSWORD"]
    assert secret not in repr(s)
    assert secret not in s.dsn()
    assert secret not in s.effective_config_json()
    assert secret in s.dsn(with_password=True)


def test_effective_config_excludes_paths_and_tracks_batch_params(base_env):
    a = load_settings(base_env)
    b = load_settings({**base_env, "SOURCE_GZIP_PATH": "/other/movies.txt.gz", "ETL_OUTPUT_DIR": "/x"})
    c = load_settings({**base_env, "ETL_BATCH_ROWS": "3"})
    assert a.effective_config_json() == b.effective_config_json()
    assert a.effective_config_json() != c.effective_config_json()
