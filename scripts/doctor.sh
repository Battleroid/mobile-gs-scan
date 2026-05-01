#!/usr/bin/env bash
# Preflight: docker, gpu, nvidia-container-toolkit. Set DOCTOR_GPU_PROBE=1
# to also probe containerized GPU access (pulls ~150 MB on first run).

set -uo pipefail

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

fail=0
check() {
    local label="$1"; shift
    if "$@" >/dev/null 2>&1; then green "  ok  $label"; else red "  err $label"; fail=$((fail+1)); fi
}

echo "host:"
check "docker installed"             command -v docker
check "docker daemon reachable"      docker info
check "docker compose v2 available"  docker compose version
echo
echo "gpu:"
check "nvidia-smi installed"         command -v nvidia-smi
check "gpu visible to host"          nvidia-smi
check "docker has nvidia runtime"    bash -c "docker info 2>/dev/null | grep -Eq 'Runtimes:.*nvidia|nvidia[[:space:]]*runc'"

if [ "${DOCTOR_GPU_PROBE:-0}" = "1" ]; then
    echo
    echo "container gpu access (~150 MB pull on first run):"
    if docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi >/dev/null 2>&1; then
        green "  ok  containerized nvidia-smi sees the gpu"
    else
        red "  err containerized nvidia-smi failed — check nvidia-container-toolkit"
        fail=$((fail+1))
    fi
else
    echo; yellow "  --  containerized GPU probe skipped (set DOCTOR_GPU_PROBE=1 to run)"
fi

echo
if [ $fail -gt 0 ]; then
    red "$fail check(s) failed."; exit 1
fi
green "all required checks passed."
