#!/usr/bin/env sh
set -eu

mkdir -p /data/uploads /etc/nginx/auth /run/nginx

python3 /app/server/app.py &
api_pid="$!"

trap 'kill "$api_pid" 2>/dev/null || true' INT TERM EXIT

nginx -g 'daemon off;'
