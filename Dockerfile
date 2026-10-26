# 基础镜像固定为多架构 index 摘要（linux/arm64 与 amd64），tag 仅供阅读。
FROM apache/airflow:3.3.2-python3.12@sha256:9df9c8be4096b9cc626bd7cb1f2b8c712eef66c59c615f8c7e6200871ca10bd1

COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock

# 源码、测试、DAG 与 SQL 由 compose 以绑定挂载提供，不进入镜像。
ENV PYTHONPATH=/opt/etl/src \
    PYTHONUNBUFFERED=1
WORKDIR /opt/etl
