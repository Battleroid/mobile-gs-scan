# mobile-gs-scan — local dev convenience targets.
#
# Default path (`make up`) builds images from source. Pre-built GHCR
# images are not published yet; once they are, `docker-compose.prebuilt.yml`
# pins them and `make up` will switch to pulling instead of building.
#
# Quick reference:
#   make doctor          — preflight: docker, gpu, nvidia-container-toolkit
#   make up              — build + start the studio (foreground)
#   make up-d            — same, but daemonized
#   make up-https        — start over HTTPS via Caddy + mkcert
#                          (required for phone capture; mobile browsers
#                          refuse getUserMedia on plain http)
#   make https-certs     — regenerate the mkcert cert pair on demand
#   make build           — rebuild every container image (no `up`)
#   make rebuild         — clean rebuild — drop layer cache + rebuild
#   make down            — stop + remove containers (keeps volumes)
#   make logs            — tail logs from every service
#   make ps              — list running services
#   make restart         — restart the stack without rebuilding
#   make clean           — DESTRUCTIVE: down + delete the named volumes
#   make shell-api       — exec a bash shell in the api container
#   make shell-gs        — same, for the worker-gs container
#
#   ─ android ─
#   make android-bootstrap — `gradle wrapper` so ./gradlew is available
#   make apk-debug         — build debug APK
#   make apk-release       — build release APK (unsigned)
#   make apk-install       — build debug APK + install on attached device
#   make android-clean     — clean android build outputs
#   make android-lint      — run android lint (no -Werror)

COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")
ANDROID_DIR := android
GRADLEW := $(ANDROID_DIR)/gradlew

.PHONY: help doctor up up-d build rebuild up-https up-https-summary https-certs \
        down logs ps restart clean shell-api shell-gs \
        android-bootstrap apk-debug apk-release apk-install android-clean android-lint

help:
	@awk 'BEGIN{FS=":.*##"; printf "mobile-gs-scan targets:\n"} \
	     /^[a-zA-Z0-9_-]+:.*##/ {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# Auto-create .env on first invocation. Idempotent — won't overwrite an
# existing file. Every other target depends on this so a fresh clone
# doesn't error with "no .env file" on the first command.
.env:
	@cp .env.example .env
	@echo "[+] created .env from .env.example"

doctor: ## preflight: check docker, gpu, nvidia-container-toolkit
	@bash scripts/doctor.sh

up: .env ## build + start (foreground)
	$(COMPOSE) up --build

up-d: .env ## same as up, but daemonized
	$(COMPOSE) up -d --build
	@echo "[+] stack started. open http://localhost:3000"
	@echo "    tail logs: make logs"
	@echo "    stop:      make down"

build: .env ## (re)build every container image
	$(COMPOSE) --profile https build

rebuild: .env ## clean rebuild — drop layer cache, rebuild everything
	$(COMPOSE) --profile https build --no-cache

# HTTPS via Caddy + a mkcert-issued cert. Required for `getUserMedia`
# (and thus the /m/<token> mobile-web PWA + the Android cleartext-only
# fallback) to work from a phone.
#
# Single-command path: `make up-https` does everything — bootstraps
# mkcert + cert pair + root CA, rebuilds the web image with an empty
# NEXT_PUBLIC_API_BASE so the bundle resolves the api origin from
# window.location at runtime, brings up the stack with the https
# profile, and prints the URLs the phone needs to visit.
up-https: .env caddy/certs/cert.pem ## start with HTTPS via Caddy + mkcert (one-shot)
	@# Down with `--profile https` so caddy IS in scope of the
	@# teardown — without the profile flag, compose v2 treats
	@# profile-gated services as out-of-scope and won't remove
	@# their containers. The `-` prefix makes the command non-fatal
	@# so a fresh-clone first run (nothing to remove) doesn't error.
	-$(COMPOSE) --profile https down --remove-orphans 2>/dev/null
	NEXT_PUBLIC_API_BASE= $(COMPOSE) --profile https build
	@$(MAKE) -s up-https-summary
	NEXT_PUBLIC_API_BASE= $(COMPOSE) --profile https up --remove-orphans

# Cert bootstrap. Make picks this up as a prereq for up-https + only
# runs it when caddy/certs/cert.pem is missing OR older than the
# bootstrap script (so editing the script forces a regenerate).
caddy/certs/cert.pem: scripts/mkcert-bootstrap.sh
	@bash scripts/mkcert-bootstrap.sh

https-certs: ## (re)generate https certs via mkcert
	@rm -f caddy/certs/cert.pem caddy/certs/key.pem
	@$(MAKE) -s caddy/certs/cert.pem

up-https-summary:
	@echo
	@echo "════════════════════════════════════════════════════════════════"
	@echo " HTTPS studio about to start. From the phone:"
	@echo
	@if [ -f caddy/certs/.env.bootstrap ]; then \
	  . caddy/certs/.env.bootstrap; \
	  if [ -n "$$STUDIO_LAN_IP" ]; then \
	    CA_URL="http://$$STUDIO_LAN_IP/mkcert-rootCA.crt"; \
	    NEW_URL="https://$$STUDIO_LAN_IP/captures/new"; \
	  else \
	    CA_URL="http://$$STUDIO_HOSTNAME/mkcert-rootCA.crt"; \
	    NEW_URL="https://$$STUDIO_HOSTNAME/captures/new"; \
	  fi; \
	  echo "  1. trust the local CA — scan or visit"; \
	  echo "       $$CA_URL"; \
	  if command -v qrencode >/dev/null 2>&1; then \
	    qrencode -t ANSI256 -m 1 "$$CA_URL"; \
	  else \
	    echo "       (install qrencode for a scannable QR)"; \
	  fi; \
	  echo "     Android: tap the file to install."; \
	  echo "     iOS: General → VPN & Device Mgmt + Cert Trust Settings."; \
	  echo; \
	  echo "  2. open the studio from a desktop browser to start a"; \
	  echo "     capture. The /captures/new page generates a phone-"; \
	  echo "     pairing QR you scan with the phone camera:"; \
	  echo "       $$NEW_URL"; \
	  if command -v qrencode >/dev/null 2>&1; then \
	    qrencode -t ANSI256 -m 1 "$$NEW_URL"; \
	  fi; \
	else \
	  echo "  visit http://<host-lan-ip>/mkcert-rootCA.crt to trust the CA,"; \
	  echo "  then https://<host-lan-ip>/captures/new from a desktop browser"; \
	fi
	@echo "════════════════════════════════════════════════════════════════"
	@echo

down: ## stop + remove containers (keeps named volumes)
	$(COMPOSE) --profile https down

logs: ## tail logs from every service
	$(COMPOSE) logs -f --tail=100

ps: ## list running services
	$(COMPOSE) ps

restart: ## restart the stack without rebuilding
	$(COMPOSE) restart

clean: ## DESTRUCTIVE: down + delete the named volumes (uploads, models cache, sqlite db)
	@echo "[!] this removes ALL containers, networks, and named volumes"
	@echo "    (captured frames, scene artifacts, models cache, sqlite db)."
	@read -r -p "    are you sure? type 'yes' to proceed: " ans; \
	 if [ "$$ans" = "yes" ]; then \
	   $(COMPOSE) --profile https down -v; \
	 else \
	   echo "[+] aborted."; \
	 fi

shell-api: ## exec a bash shell in the api container
	$(COMPOSE) exec api bash

shell-gs: ## exec a bash shell in the worker-gs container
	$(COMPOSE) exec worker-gs bash

# ─── Android ──────────────────────────────────────────────────────────
android-bootstrap: ## generate ./gradlew (one-time, needs system gradle)
	@if [ -x $(GRADLEW) ]; then \
	  echo "[+] $(GRADLEW) already exists — skipping"; \
	else \
	  if ! command -v gradle >/dev/null 2>&1; then \
	    echo "[!] system 'gradle' missing. install via apt/brew/sdkman, then re-run."; \
	    exit 1; \
	  fi; \
	  cd $(ANDROID_DIR) && gradle wrapper --gradle-version 8.10; \
	  echo "[+] wrapper bootstrapped — apk-* targets will use $(GRADLEW)"; \
	fi

apk-debug: $(GRADLEW) ## build debug APK (android/app/build/outputs/apk/debug/)
	cd $(ANDROID_DIR) && ./gradlew :app:assembleDebug
	@echo "[+] APK at $(ANDROID_DIR)/app/build/outputs/apk/debug/app-debug.apk"

apk-release: $(GRADLEW) ## build release APK (unsigned)
	cd $(ANDROID_DIR) && ./gradlew :app:assembleRelease
	@echo "[+] APK at $(ANDROID_DIR)/app/build/outputs/apk/release/"

apk-install: apk-debug ## build + install debug APK on an attached device via adb
	@if ! command -v adb >/dev/null 2>&1; then \
	  echo "[!] adb missing — install android-platform-tools"; exit 1; \
	fi
	adb install -r $(ANDROID_DIR)/app/build/outputs/apk/debug/app-debug.apk

android-clean: ## clean android build outputs
	@if [ -x $(GRADLEW) ]; then cd $(ANDROID_DIR) && ./gradlew clean; \
	else rm -rf $(ANDROID_DIR)/build $(ANDROID_DIR)/app/build $(ANDROID_DIR)/.gradle; fi

android-lint: $(GRADLEW) ## run android lint (no -Werror)
	cd $(ANDROID_DIR) && ./gradlew :app:lintDebug

$(GRADLEW):
	@echo "[!] $(GRADLEW) is missing. run \`make android-bootstrap\` first."
	@exit 1
