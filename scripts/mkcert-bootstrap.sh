#!/usr/bin/env bash
# scripts/mkcert-bootstrap.sh — idempotent mkcert install + cert pair
# generation. Outputs caddy/certs/{cert.pem, key.pem, rootCA.pem,
# rootCA.crt, .env.bootstrap}.
#
# Env knobs:
#   STUDIO_HOSTNAME (default studio.local)
#   STUDIO_LAN_IP   (auto-detected if missing)
#   CERTS_DIR       (default caddy/certs)

set -euo pipefail

CERTS_DIR="${CERTS_DIR:-caddy/certs}"
HOSTNAME_DEFAULT="${STUDIO_HOSTNAME:-studio.local}"

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
plus()   { printf '\033[36m[+] %s\033[0m\n' "$*"; }

ensure_mkcert() {
    if command -v mkcert >/dev/null 2>&1; then return 0; fi
    plus "mkcert not found — installing"
    case "$(uname -s)" in
        Linux)
            local SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
            if command -v apt-get >/dev/null 2>&1; then
                $SUDO apt-get update -y >/dev/null
                $SUDO apt-get install -y libnss3-tools ca-certificates curl >/dev/null
                if ! $SUDO apt-get install -y mkcert >/dev/null 2>&1; then
                    install_mkcert_from_github "$SUDO"
                fi
            elif command -v dnf >/dev/null 2>&1; then
                $SUDO dnf install -y nss-tools curl >/dev/null
                install_mkcert_from_github "$SUDO"
            else
                install_mkcert_from_github sudo
            fi
            ;;
        Darwin)
            command -v brew >/dev/null 2>&1 && brew install mkcert nss || {
                red "    install brew or mkcert manually"; exit 1; }
            ;;
        *) red "    unsupported OS"; exit 1 ;;
    esac
}

ensure_qrencode() {
    if command -v qrencode >/dev/null 2>&1; then return 0; fi
    plus "qrencode not found — attempting install (best-effort)"
    case "$(uname -s)" in
        Linux)
            local SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
            if command -v apt-get >/dev/null 2>&1; then
                $SUDO apt-get install -y qrencode >/dev/null 2>&1 || true
            elif command -v dnf >/dev/null 2>&1; then
                $SUDO dnf install -y qrencode >/dev/null 2>&1 || true
            fi
            ;;
        Darwin) brew install qrencode >/dev/null 2>&1 || true ;;
    esac
}

install_mkcert_from_github() {
    local SUDO="${1:-}"
    local mkarch
    case "$(uname -m)" in
        x86_64|amd64) mkarch=amd64 ;;
        aarch64|arm64) mkarch=arm64 ;;
        armv7l|armhf) mkarch=arm ;;
        *) red "unknown arch"; exit 1 ;;
    esac
    plus "downloading mkcert v1.4.4 (${mkarch})"
    $SUDO curl -fsSL -o /usr/local/bin/mkcert \
        "https://github.com/FiloSottile/mkcert/releases/download/v1.4.4/mkcert-v1.4.4-linux-${mkarch}"
    $SUDO chmod +x /usr/local/bin/mkcert
}

ensure_root_ca() {
    plus "installing local root CA into the system trust store"
    mkcert -install || yellow "[!] mkcert -install failed; phone trust still OK via .pem download"
}

detect_lan_ip() {
    local ip="${STUDIO_LAN_IP:-}"
    [[ -n "$ip" ]] && { echo "$ip"; return; }
    if command -v ip >/dev/null 2>&1; then
        ip=$(ip -4 -o addr show 2>/dev/null \
            | awk '$2 !~ /^(lo|docker|br-|veth|virbr|tun|tap)/ {split($4, a, "/"); print a[1]; exit}')
    fi
    if [[ -z "$ip" ]] && command -v ipconfig >/dev/null 2>&1; then
        ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)
    fi
    echo "$ip"
}

generate_cert() {
    local lan_ip="$1"
    mkdir -p "$CERTS_DIR"
    local sans=("$HOSTNAME_DEFAULT" localhost 127.0.0.1)
    [[ -n "$lan_ip" ]] && sans+=("$lan_ip")
    plus "generating cert for: ${sans[*]}"
    mkcert -cert-file "$CERTS_DIR/cert.pem" -key-file "$CERTS_DIR/key.pem" "${sans[@]}" >/dev/null
}

copy_root_ca() {
    local caroot; caroot=$(mkcert -CAROOT)
    [[ -f "$caroot/rootCA.pem" ]] || { red "rootCA.pem missing"; exit 1; }
    cp "$caroot/rootCA.pem" "$CERTS_DIR/rootCA.pem"
    cp "$caroot/rootCA.pem" "$CERTS_DIR/rootCA.crt"
    chmod 644 "$CERTS_DIR/rootCA.pem" "$CERTS_DIR/rootCA.crt"
}

ensure_mkcert
ensure_qrencode
ensure_root_ca
LAN_IP=$(detect_lan_ip)
generate_cert "$LAN_IP"
copy_root_ca

{
    [[ -n "$LAN_IP" ]] && echo "STUDIO_LAN_IP=$LAN_IP"
    echo "STUDIO_HOSTNAME=$HOSTNAME_DEFAULT"
} > "$CERTS_DIR/.env.bootstrap"

green ""
green "[+] cert pair: $CERTS_DIR/{cert,key}.pem"
green "[+] root CA:  $CERTS_DIR/rootCA.pem"
[[ -n "$LAN_IP" ]] && green "[+] LAN IP:    $LAN_IP"
green "[+] hostname:  $HOSTNAME_DEFAULT"
