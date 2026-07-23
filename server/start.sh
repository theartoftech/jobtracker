#!/usr/bin/env sh
set -eu

mkdir -p /data/uploads /etc/nginx/auth /run/nginx

cd /app
python3 -m server.app &
api_pid="$!"

trap 'kill "$api_pid" 2>/dev/null || true' INT TERM EXIT

nginx -g 'daemon off;'
