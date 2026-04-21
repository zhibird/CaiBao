#!/usr/bin/env sh
set -eu

mkdir -p /data/uploads
chown -R caibao:caibao /data

echo "[entrypoint] Running database migrations..."
gosu caibao alembic upgrade head
echo "[entrypoint] Migration complete."

echo "[entrypoint] Starting API server..."
exec gosu caibao uvicorn app.main:app --host 0.0.0.0 --port 8000
