#!/usr/bin/env bash
# scripts/jdk17.sh — print the path to a JDK 17 installation, or
# exit non-zero with an install hint.
#
# Background: AGP 8.x (Android Gradle Plugin) requires Java 17 to
# run. Most Linux hosts in 2026 still default to Java 11 (Ubuntu
# 22.04 LTS) or Java 21. We probe the common JDK 17 install paths
# rather than force the user to fight `update-alternatives` /
# JAVA_HOME — and the apk-* Make targets `JAVA_HOME=$(bash
# scripts/jdk17.sh) ./gradlew …` so the right toolchain is used
# without touching anything global.
#
# Override knob: `JDK17_HOME` env var short-circuits the search.

set -euo pipefail

# Already exported and points at a JDK 17.
if [[ -n "${JDK17_HOME:-}" && -x "${JDK17_HOME}/bin/javac" ]] \
        && "${JDK17_HOME}/bin/javac" -version 2>&1 | grep -qE '^javac 17\.'; then
    echo "${JDK17_HOME}"
    exit 0
fi

# Common locations across distros.
candidates=(
    "${JAVA_HOME:-}"
    /usr/lib/jvm/java-17-openjdk-amd64
    /usr/lib/jvm/java-17-openjdk-arm64
    /usr/lib/jvm/java-17-openjdk
    /usr/lib/jvm/temurin-17-jdk-amd64
    /usr/lib/jvm/temurin-17-jdk-arm64
    /usr/lib/jvm/temurin-17-jdk
    /usr/lib/jvm/zulu-17-amd64
    /opt/homebrew/opt/openjdk@17
    /opt/homebrew/opt/openjdk@17/libexec/openjdk.jdk/Contents/Home
    /Library/Java/JavaVirtualMachines/temurin-17.jdk/Contents/Home
    /Library/Java/JavaVirtualMachines/zulu-17.jdk/Contents/Home
)

for d in "${candidates[@]}"; do
    [[ -z "$d" || ! -x "$d/bin/javac" ]] && continue
    if "$d/bin/javac" -version 2>&1 | grep -qE '^javac 17\.'; then
        echo "$d"
        exit 0
    fi
done

# macOS — let java_home tell us.
if command -v /usr/libexec/java_home >/dev/null 2>&1; then
    if jh="$(/usr/libexec/java_home -v 17 2>/dev/null)"; then
        echo "$jh"
        exit 0
    fi
fi

# Last resort: scan /usr/lib/jvm wholesale.
if [[ -d /usr/lib/jvm ]]; then
    while IFS= read -r d; do
        if [[ -x "$d/bin/javac" ]] \
                && "$d/bin/javac" -version 2>&1 | grep -qE '^javac 17\.'; then
            echo "$d"
            exit 0
        fi
    done < <(find /usr/lib/jvm -maxdepth 2 -mindepth 1 -type d 2>/dev/null)
fi

# Nothing found — print a helpful hint.
{
    echo "[!] JDK 17 not found on this host."
    echo
    echo "    The Android Gradle Plugin (AGP 8.x) requires Java 17. We"
    echo "    detected:"
    if [[ -n "${JAVA_HOME:-}" ]]; then
        echo "      JAVA_HOME=${JAVA_HOME}"
    fi
    if command -v java >/dev/null 2>&1; then
        java -version 2>&1 | sed 's/^/      /'
    fi
    echo
    echo "    Install JDK 17 (it can sit alongside your default java —"
    echo "    we'll find it without touching update-alternatives):"
    echo "      apt:  sudo apt install openjdk-17-jdk"
    echo "      dnf:  sudo dnf install java-17-openjdk-devel"
    echo "      brew: brew install openjdk@17"
    echo
    echo "    Or set JDK17_HOME=<path-to-jdk17> in your env."
} >&2
exit 1
