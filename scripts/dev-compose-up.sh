#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."
mkdir -p data tmp artifacts

export NAS_HOST="${NAS_HOST:-127.0.0.1}"
export NAS_PORT="${NAS_PORT:-445}"
export NAS_USER="${NAS_USER:-dev}"
export NAS_PASSWORD="${NAS_PASSWORD:-dev}"
export NAS_SHARE_PATH="${NAS_SHARE_PATH:-dev}"

docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
