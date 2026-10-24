#!/usr/bin/env sh
set -eu

cd /app
python3 -m server.runtime_config
nginx -t
mkdir -p /data/uploads /etc/nginx/auth /run/nginx
python3 -m server.app &
api_pid="$!"

trap 'kill "$api_pid" 2>/dev/null || true' INT TERM EXIT

nginx -g 'daemon off;'
