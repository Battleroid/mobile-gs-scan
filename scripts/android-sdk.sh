#!/usr/bin/env bash
# scripts/android-sdk.sh — print the path to an installed Android
# SDK, or exit non-zero with an install hint.
#
# Used by the Makefile's `android/local.properties` rule. We probe a
# bunch of common locations because there's no single canonical
# install path: Android Studio drops one at ~/Android/Sdk on Linux
# and ~/Library/Android/sdk on macOS, the cmdline-tools approach
# typically lands at ~/.android-sdk, and CI / containers often set
# $ANDROID_SDK_ROOT or $ANDROID_HOME explicitly.
#
# An SDK is considered valid here if it has BOTH:
#   - cmdline-tools/latest    (so `sdkmanager` is reachable)
#   - platforms/              (so at least one API level is installed)
#
# If nothing matches, exits 1 with a hint pointing at
# `make android-sdk-bootstrap`.

set -euo pipefail

candidates=(
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
    echo "        to ~/.android-sdk via Google's cmdline-tools)"
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
