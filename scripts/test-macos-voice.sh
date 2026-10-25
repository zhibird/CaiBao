#!/bin/bash
set -euo pipefail

# Pure Foundation state-machine tests. Do not open the app, access the microphone,
# query system permissions, or link Apple's capture/recognition implementations.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$REPO_ROOT/build/native-voice-tests"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "原生语音状态机测试需要 macOS Swift 工具链。" >&2
  exit 1
fi
mkdir -p "$TEST_ROOT/swift-cache"
SDK_PATH="${CAIBAO_MACOS_SDK_PATH:-$(xcrun --show-sdk-path)}"
printf 'import Foundation\n' > "$TEST_ROOT/sdk-probe.swift"
check_sdk() {
  xcrun swiftc -typecheck -sdk "$1" -module-cache-path "$TEST_ROOT/swift-cache" \
    "$TEST_ROOT/sdk-probe.swift" > "$TEST_ROOT/sdk-check.log" 2>&1
}
if ! check_sdk "$SDK_PATH"; then
  MATCHED=false
  if [[ -z "${CAIBAO_MACOS_SDK_PATH:-}" ]]; then
    while IFS= read -r candidate; do
      if [[ "$candidate" != "$SDK_PATH" ]] && check_sdk "$candidate"; then
        SDK_PATH="$candidate"; MATCHED=true; break
      fi
    done < <(find "$(dirname "$SDK_PATH")" -maxdepth 1 -type d -name 'MacOSX*.sdk' | sort -r)
  fi
  if [[ "$MATCHED" != true ]]; then
    echo "Swift SDK不兼容，请查看 build/native-voice-tests/sdk-check.log。" >&2
    exit 1
  fi
fi
xcrun swiftc -sdk "$SDK_PATH" -module-cache-path "$TEST_ROOT/swift-cache" \
  -target "$(uname -m)-apple-macosx13.0" \
  "$REPO_ROOT/desktop/macos/NativeListeningController.swift" \
  "$REPO_ROOT/tests/macos/NativeListeningTests.swift" \
  -o "$TEST_ROOT/NativeListeningTests"
"$TEST_ROOT/NativeListeningTests"
