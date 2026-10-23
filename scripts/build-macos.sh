#!/bin/bash
set -euo pipefail

# Build a native .app with Apple's command-line tools; no Xcode project or Node runtime.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_ROOT="$REPO_ROOT/build"
APP_ROOT="$BUILD_ROOT/CaiBao.app"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "彩宝桌宠需要 macOS。网页版可通过 http://127.0.0.1:8000/companion 打开。" >&2
  exit 1
fi
if ! xcrun --find swiftc >/dev/null 2>&1; then
  echo "请先安装 Apple Command Line Tools：xcode-select --install" >&2
  exit 1
fi
mkdir -p "$APP_ROOT/Contents/MacOS" "$APP_ROOT/Contents/Resources" "$BUILD_ROOT/swift-cache"
# Some macOS updates leave a newer SDK selected than the installed Swift compiler.
# Probe existing SDKs rather than changing the machine's developer-tool selection.
SDK_PATH="${CAIBAO_MACOS_SDK_PATH:-$(xcrun --show-sdk-path)}"
SDK_PROBE="$BUILD_ROOT/sdk-probe.swift"
printf 'import Foundation\n' > "$SDK_PROBE"
check_sdk() {
  xcrun swiftc -typecheck -sdk "$1" -module-cache-path "$BUILD_ROOT/swift-cache" \
    "$SDK_PROBE" > "$BUILD_ROOT/sdk-check.log" 2>&1
}
if ! check_sdk "$SDK_PATH"; then
  if [[ -n "${CAIBAO_MACOS_SDK_PATH:-}" ]]; then
    echo "指定 SDK 与 Swift 不兼容，请查看 build/sdk-check.log。" >&2
    exit 1
  fi
  MATCHED=false
  while IFS= read -r candidate; do
    if [[ "$candidate" != "$SDK_PATH" ]] && check_sdk "$candidate"; then
      SDK_PATH="$candidate"
      MATCHED=true
      break
    fi
  done < <(find "$(dirname "$SDK_PATH")" -maxdepth 1 -type d -name 'MacOSX*.sdk' | sort -r)
  if [[ "$MATCHED" != true ]]; then
    echo "找不到与 Swift 兼容的 macOS SDK，请更新 Apple Command Line Tools；详情见 build/sdk-check.log。" >&2
    exit 1
  fi
fi
xcrun swiftc -O -sdk "$SDK_PATH" -module-cache-path "$BUILD_ROOT/swift-cache" \
  -target "$(uname -m)-apple-macosx13.0" \
  -framework AppKit -framework WebKit -framework AVFoundation \
  "$REPO_ROOT/desktop/macos/main.swift" -o "$APP_ROOT/Contents/MacOS/CaiBao"
cp "$REPO_ROOT/desktop/macos/Info.plist" "$APP_ROOT/Contents/Info.plist"
# An ad-hoc local signature is enough for running a locally built app.
codesign --force --sign - "$APP_ROOT"
echo "已构建：$APP_ROOT"
