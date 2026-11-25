#!/bin/sh
set -eu

# 未设置时保留单 worker；显式设置为空或非正整数均拒绝启动。
workers=${UVICORN_WORKERS-1}
case "$workers" in
  ''|*[!0-9]*)
    echo 'UVICORN_WORKERS 必须是正整数' >&2
    exit 1
    ;;
esac
if ! [ "$workers" -gt 0 ] 2>/dev/null; then
  echo 'UVICORN_WORKERS 必须是正整数' >&2
  exit 1
fi

# 一个容器只迁移一次；失败时不会启动对外服务。
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --workers "$workers"
