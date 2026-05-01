#!/usr/bin/env bash
# scripts/android-sdk-bootstrap.sh — unattended Android SDK install.
#
# Downloads Google's cmdline-tools to ~/.android-sdk (override with
# ANDROID_SDK_ROOT), accepts the SDK licenses, and installs the
# components android/app/build.gradle.kts asks for:
#   - platform-tools          (adb, fastboot)
#   - platforms;android-35    (compileSdk / targetSdk in build.gradle.kts)
#   - build-tools;35.0.0      (AGP 8.7.2's expected build-tools rev)
#
# Idempotent: re-running on a complete install is a no-op past the
# license-accept step.
#
# Knobs:
#   ANDROID_SDK_ROOT — override the install root (default ~/.android-sdk).
#   PLATFORM_VERSION — Android API level to install (default 35).
#   BUILD_TOOLS_VERSION — build-tools to install (default 35.0.0).
#   CMDLINE_TOOLS_VERSION — cmdline-tools build number to download
#                           (default 13114758, the v17.0 release).

set -euo pipefail

SDK_ROOT="${ANDROID_SDK_ROOT:-${ANDROID_HOME:-$HOME/.android-sdk}}"
PLATFORM_VERSION="${PLATFORM_VERSION:-35}"
BUILD_TOOLS_VERSION="${BUILD_TOOLS_VERSION:-35.0.0}"
CMDLINE_TOOLS_VERSION="${CMDLINE_TOOLS_VERSION:-13114758}"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
plus()   { printf '\033[36m[+] %s\033[0m\n' "$*"; }

case "$(uname -s)" in
    Linux)  os=linux ;;
    Darwin) os=mac ;;
    *) red "unsupported OS: $(uname -s)"; exit 1 ;;
esac

# ── 1. download + extract cmdline-tools if missing ────────────────
if [[ ! -x "$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager" ]]; then
    plus "downloading cmdline-tools to $SDK_ROOT"
    mkdir -p "$SDK_ROOT/cmdline-tools"
    url="https://dl.google.com/android/repository/commandlinetools-${os}-${CMDLINE_TOOLS_VERSION}_latest.zip"
    tmpzip="$(mktemp --suffix=.zip)"
    trap 'rm -f "$tmpzip"' EXIT

    if ! command -v unzip >/dev/null 2>&1; then
        red "    \`unzip\` not found — install via apt/brew first"
        exit 1
    fi
    curl -fsSL "$url" -o "$tmpzip"
    unzip -q "$tmpzip" -d "$SDK_ROOT/cmdline-tools"
    rm -f "$tmpzip"
    trap - EXIT

    # Google's zip extracts to cmdline-tools/cmdline-tools/. The
    # sdkmanager `latest` channel expects cmdline-tools/latest/, so
    # we rename. Skip if `latest` already exists from a prior run.
    if [[ -d "$SDK_ROOT/cmdline-tools/cmdline-tools" \
            && ! -d "$SDK_ROOT/cmdline-tools/latest" ]]; then
        mv "$SDK_ROOT/cmdline-tools/cmdline-tools" \
           "$SDK_ROOT/cmdline-tools/latest"
    fi
else
    plus "cmdline-tools already at $SDK_ROOT/cmdline-tools/latest"
fi

SDKMANAGER="$SDK_ROOT/cmdline-tools/latest/bin/sdkmanager"
if [[ ! -x "$SDKMANAGER" ]]; then
    red "sdkmanager missing at $SDKMANAGER after install"
    exit 1
fi

# ── 2. accept all SDK licenses ────────────────────────────────────
# `yes` floods `y` into the prompts; sdkmanager noops on already-
# accepted licenses. We toss its chatty output but keep its non-zero
# exit fail the script if something's actually wrong with the
# download.
plus "accepting SDK licenses"
yes | "$SDKMANAGER" --licenses --sdk_root="$SDK_ROOT" >/dev/null 2>&1 || true

# ── 3. install required components ────────────────────────────────
plus "installing platform-tools, platforms;android-$PLATFORM_VERSION, build-tools;$BUILD_TOOLS_VERSION"
"$SDKMANAGER" --sdk_root="$SDK_ROOT" \
    "platform-tools" \
    "platforms;android-$PLATFORM_VERSION" \
    "build-tools;$BUILD_TOOLS_VERSION"

green ""
green "[+] Android SDK installed at $SDK_ROOT"
green "    The Makefile will write android/local.properties from this"
green "    path on the next \`make apk-*\` invocation."
