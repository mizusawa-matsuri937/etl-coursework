import pytest


@pytest.fixture
def base_env() -> dict[str, str]:
    """满足必需项的最小环境；口令为测试占位值。"""
    return {
        "SOURCE_GZIP_PATH": "/data/source/movies.txt.gz",
        "SOURCE_MANIFEST_PATH": "/data/source/movies.txt.gz.download.json",
        "ETL_DB_HOST": "postgres",
        "ETL_DB_NAME": "etl_test",
        "ETL_DB_USER": "etl_test",
        "ETL_DB_PASSWORD": "placeholder-not-a-secret",
    }
