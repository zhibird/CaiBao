#!/bin/bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_ROOT="$REPO_ROOT/build/CaiBao.app"
PYTHON="$REPO_ROOT/.venv/bin/python"
HEALTH_URL="http://127.0.0.1:8000/api/v1/health"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "彩宝原生桌宠需要 macOS。" >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "请先创建虚拟环境并安装依赖：python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi
"$REPO_ROOT/scripts/build-macos.sh"

# Reuse an existing service. A service started here is stopped when this launcher exits.
SERVER_PID=""
cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if ! curl --noproxy '*' --fail --silent --max-time 2 "$HEALTH_URL" >/dev/null; then
  cd "$REPO_ROOT"
  "$REPO_ROOT/scripts/run-local.sh" > "$REPO_ROOT/build/service.log" 2>&1 &
  SERVER_PID=$!
  READY=false
  for ((attempt = 0; attempt < 60; attempt++)); do
    if curl --noproxy '*' --fail --silent --max-time 1 "$HEALTH_URL" >/dev/null; then
      READY=true
      break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then break; fi
    sleep 0.5
  done
  if [[ "$READY" != true ]]; then
    echo "本地服务未能启动，请查看 build/service.log。" >&2
    exit 1
  fi
fi

echo "彩宝已就绪。菜单栏爪印可显示、收起或退出桌宠。"
# Running the executable keeps service ownership tied to this launcher's lifetime.
"$APP_ROOT/Contents/MacOS/CaiBao"
