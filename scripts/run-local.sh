#!/bin/bash
set -euo pipefail
umask 077
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
PYTHON="${CAIBAO_PYTHON:-$REPO_ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo '请先安装依赖：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt' >&2
  exit 1
fi
# Private generated defaults; explicit environment/.env configuration takes priority.
# No source/eval of .env, and never print API keys or the generated auth secret.
export CAIBAO_LOCAL_ROOT="$REPO_ROOT/data/local"
mkdir -p "$CAIBAO_LOCAL_ROOT"
chmod 700 "$CAIBAO_LOCAL_ROOT"
"$PYTHON" - <<'PY'
import os
import secrets
from pathlib import Path
path = Path(os.environ['CAIBAO_LOCAL_ROOT']) / 'auth-secret'
try:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
except FileExistsError:
    pass
else:
    with os.fdopen(fd, 'w') as file:
        file.write(secrets.token_urlsafe(48))
PY
# pydantic reads the project .env. Export only fields absent there and in the environment.
eval "$("$PYTHON" - <<'PY'
import os
import shlex
from pathlib import Path
from dotenv import dotenv_values
root = Path(os.environ['CAIBAO_LOCAL_ROOT'])
configured = dotenv_values('.env')
defaults = {
 'DATABASE_URL': 'sqlite:///' + str(root / 'caibao.db'),
 'DB_LEGACY_INIT_ENABLED': 'true',
 'AUTH_JWT_SECRET': (root / 'auth-secret').read_text(),
 'UPLOAD_ROOT_DIR': str(root / 'uploads'),
 'DEV_ADMIN_ENABLED': 'false',
}
for key, value in defaults.items():
    if key not in os.environ and key not in configured:
        print(f'export {key}={shlex.quote(value)}')
PY
)"
echo '彩宝服务：http://127.0.0.1:8000/companion （Ctrl+C 停止）'
exec "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
