#!/bin/bash
# 仅在数据卷首次初始化时由 PostgreSQL 镜像执行：创建三套互相隔离的角色与数据库。
set -euo pipefail

create_db() {
  local role="$1" password="$2" db="$3"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -v role="$role" -v password="$password" -v db="$db" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'password') \gexec
SELECT format('CREATE DATABASE %I OWNER %I', :'db', :'role') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'db') \gexec
SQL
}

create_db etl_app "$ETL_DB_PASSWORD" etl_movies
create_db airflow "$AIRFLOW_DB_PASSWORD" airflow_meta
create_db etl_test "$TEST_DB_PASSWORD" etl_test
