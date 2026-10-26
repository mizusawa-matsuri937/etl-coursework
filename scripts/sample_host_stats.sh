#!/usr/bin/env bash
# 宿主侧资源采样：容器 CPU/内存 与 PostgreSQL 数据/WAL 目录大小，每隔 N 秒一行 CSV，直到被终止。
# 用法：scripts/sample_host_stats.sh <输出csv> [间隔秒=5]
set -u
out="${1:?输出 CSV 路径}"; every="${2:-5}"
pg="$(docker compose ps -q postgres)"
echo "ts,container,cpu_pct,mem_usage,mem_pct,pgdata_kb,pg_wal_kb" > "$out"
while true; do
  ts="$(date -u +%FT%TZ)"
  sizes="$(docker exec "$pg" sh -c 'du -sk /var/lib/postgresql 2>/dev/null | cut -f1; du -sk /var/lib/postgresql/*/docker/pg_wal /var/lib/postgresql/*/pg_wal /var/lib/postgresql/data/pg_wal 2>/dev/null | head -1 | cut -f1' | tr '\n' ' ')"
  set -- $sizes
  docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}},{{.MemPerc}}' | while IFS= read -r line; do
    echo "$ts,$line,${1:-},${2:-}" >> "$out"
  done
  sleep "$every"
done
