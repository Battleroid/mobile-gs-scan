#!/usr/bin/env bash
# scripts/android-sdk.sh — print the path to an installed Android
# SDK, or exit non-zero with an install hint.
#
# Used by the Makefile's `android/local.properties` rule. Probe
# order:
#
#   1. <repo>/android/.android-sdk    (the repo-local install that
#      `make android-sdk-bootstrap` writes; gitignored. Wins so a
#      user with Android Studio installed system-wide still gets a
#      reproducible per-repo SDK.)
#   2. $ANDROID_SDK_ROOT / $ANDROID_HOME (explicit overrides)
#   3. ~/Android/Sdk    (Android Studio Linux default)
#   4. ~/Library/Android/sdk    (Android Studio macOS default)
#   5. ~/.android-sdk   (older user-wide cmdline-tools install)
#   6. /opt/android-sdk, /usr/local/lib/android/sdk, /usr/lib/android-sdk
#
# An SDK is "valid" if it has BOTH cmdline-tools/latest (so
# sdkmanager is reachable) and platforms/ (so at least one API
# level is installed).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

candidates=(
    "$REPO_ROOT/android/.android-sdk"
    "${ANDROID_SDK_ROOT:-}"
    "${ANDROID_HOME:-}"
    "$HOME/Android/Sdk"
    "$HOME/Library/Android/sdk"
    "$HOME/.android-sdk"
    "/opt/android-sdk"
    "/usr/local/lib/android/sdk"
    "/usr/lib/android-sdk"
)

for d in "${candidates[@]}"; do
    [[ -z "$d" || ! -d "$d" ]] && continue
    if [[ -d "$d/cmdline-tools/latest" && -d "$d/platforms" ]]; then
        echo "$d"
        exit 0
    fi
done

{
    echo "[!] Android SDK not found."
    echo
    echo "    Pick one of:"
    echo "      • run \`make android-sdk-bootstrap\` (unattended install"
    echo "        to android/.android-sdk in this repo, gitignored)"
    echo "      • install Android Studio — drops a usable SDK at"
    echo "        ~/Android/Sdk (Linux) or ~/Library/Android/sdk (macOS)"
    echo "      • export ANDROID_SDK_ROOT=<path> if you already have"
    echo "        one elsewhere"
    if [[ -n "${ANDROID_SDK_ROOT:-}" || -n "${ANDROID_HOME:-}" ]]; then
        echo
        echo "    (We saw ANDROID_SDK_ROOT/HOME set, but the path"
        echo "    didn't have cmdline-tools/latest + platforms/ — looks"
        echo "    half-installed.)"
    fi
} >&2
exit 1
