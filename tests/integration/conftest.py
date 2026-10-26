"""集成测试夹具：连接独立的 etl_test 库；每个测试前清空业务表。"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import psycopg
import pytest

from etl_task1.config import Settings, load_settings
from etl_task1.db import apply_schema, connect
from etl_task1.source import verify_source

import build_fixtures as bf

pytestmark = pytest.mark.integration

TABLES = ("quality_issue", "quarantine_record", "review_staging", "review_record", "etl_batch",
          "etl_run", "extraction", "source_file")


@pytest.fixture(scope="session")
def base_settings() -> Settings:
    s = load_settings(os.environ)
    if s.env != "test" or not s.db_name.endswith("_test"):
        pytest.fail("集成测试只允许连接 ETL_ENV=test 且库名以 _test 结尾的数据库")
    return s


@pytest.fixture(scope="session", autouse=True)
def schema(base_settings):
    with connect(base_settings) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        assert apply_schema(conn) is True
    yield


@pytest.fixture
def conn(base_settings, schema):
    with connect(base_settings) as c:
        c.execute(f"TRUNCATE {', '.join(TABLES)} CASCADE")
        yield c


@pytest.fixture
def fixture_files(tmp_path):
    return bf.build(tmp_path / "fixture")


@pytest.fixture
def make_settings(base_settings, fixture_files, tmp_path):
    gz, manifest = fixture_files

    def make(**over) -> Settings:
        defaults = dict(source_gzip_path=gz, source_manifest_path=manifest,
                        output_dir=tmp_path / "out", batch_rows=3)
        return dataclasses.replace(base_settings, **{**defaults, **over})

    return make


@pytest.fixture
def verify(fixture_files):
    gz, manifest = fixture_files
    return lambda s=None: verify_source(s.source_gzip_path if s else gz,
                                        s.source_manifest_path if s else manifest)
