# mobile-gs-scan — local dev convenience targets.
#
# Two ways to bring up the stack:
#
#   1. Local-build path:    `make build`  → `make up`
#      Builds every image from this checkout. Good for hacking on
#      worker / web / Dockerfile changes.
#
#   2. Pull-from-registry:  `make pull`   → `make up-pull`
#      Pulls pre-built images from ghcr.io (published by the
#      build-images CI workflow on each push to main / v* tags). Good
#      for fast first-run on a machine that just needs to use the
#      studio without rebuilding the CUDA/nerfstudio stack.
#
# `make up` itself never rebuilds and never pulls. It just starts what
# you already have locally — by intent, so a second `make up` after
# code changes doesn't quietly do a full rebuild on you.
#
# Quick reference:
#   make doctor              — preflight: docker, gpu, nvidia-container-toolkit
#   make build               — build every image locally (base first, then rest)
#   make rebuild             — like build, but `--no-cache`
#   make pull                — pull pre-built images from ghcr.io
#   make up                  — start the stack (assumes images exist locally)
#   make up-d                — same, daemonized
#   make up-build            — build then up (chainable convenience)
#   make up-pull             — pull then up
#   make up-https            — start over HTTPS via Caddy + mkcert
#   make https-certs         — regenerate the mkcert cert pair on demand
#   make down                — stop + remove containers (keeps volumes)
#   make logs                — tail logs from every service
#   make ps                  — list running services
#   make restart             — restart the stack without rebuilding
#   make clean               — DESTRUCTIVE: down + delete the named volumes
#   make shell-api           — exec a bash shell in the api container
#   make shell-gs            — same, for the worker-gs container
#
#   ─ android ─
#   make android-bootstrap     — `gradle wrapper` so ./gradlew is available
#   make android-sdk-bootstrap — install Android SDK into android/.android-sdk
#   make apk-debug             — build debug APK
#   make apk-release           — build release APK (unsigned)
#   make apk-install           — build debug APK + install on attached device
#   make android-clean         — clean android build outputs
#   make android-lint          — run android lint (no -Werror)

COMPOSE := $(shell docker compose version >/dev/null 2>&1 && echo "docker compose" || echo "docker-compose")

# Overlay that flips the `image:` references in docker-compose.yml from
# the local-build tags (mobile-gs-scan/{base,api,worker-gs,web}:latest)
# to the ghcr.io paths the CI workflow publishes to. Applied via -f
# wherever we want the registry path: `make pull` and `make up-pull`.
PREBUILT := -f docker-compose.yml -f docker-compose.prebuilt.yml

ANDROID_DIR := android
GRADLEW := $(ANDROID_DIR)/gradlew

.PHONY: help doctor build rebuild pull up up-d up-build up-pull \
        up-https up-https-summary https-certs \
        down logs ps restart clean shell-api shell-gs \
        android-bootstrap android-sdk-bootstrap _require_android_sdk \
        apk-debug apk-release apk-install android-clean android-lint

help:
	@awk 'BEGIN{FS=":.*##"; printf "mobile-gs-scan targets:\n"} \
	     /^[a-zA-Z0-9_-]+:.*##/ {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

# Auto-create .env on first invocation. Idempotent — won't overwrite an
# existing file. Every other target depends on this so a fresh clone
# doesn't error with "no .env file" on the first command.
.env:
	@cp .env.example .env
	@echo "[+] created .env from .env.example"

doctor: ## preflight: check docker, gpu, nvidia-container-toolkit
	@bash scripts/doctor.sh

# ─── Build ────────────────────────────────────────────────────────────
#
# `base` is profile-gated to `build` so it doesn't run as a service —
# it only exists as a build target for the shared image that api +
# worker-gs FROM. We MUST build it explicitly first; if api or
# worker-gs build before `mobile-gs-scan/base:latest` exists locally,
# their FROM step will go to docker.io looking for it, hit a 401, and
# fail with "pull access denied, repository does not exist". The
# two-step build below is the fix.

build: .env ## build every image locally (base first, then api/worker-gs/web)
	$(COMPOSE) --profile build build base
	$(COMPOSE) --profile https build

rebuild: .env ## clean rebuild — drop layer cache
	$(COMPOSE) --profile build build --no-cache base
	$(COMPOSE) --profile https build --no-cache

pull: .env ## pull pre-built images from ghcr.io (uses prebuilt overlay)
	$(COMPOSE) $(PREBUILT) --profile https pull

# ─── Up ───────────────────────────────────────────────────────────────

up: .env ## start the stack (assumes images already built or pulled)
	$(COMPOSE) up

up-d: .env ## same as up, daemonized
	$(COMPOSE) up -d
	@echo "[+] stack started. open http://localhost:3000"
	@echo "    tail logs: make logs"
	@echo "    stop:      make down"

up-build: .env ## build everything locally + start
	@$(MAKE) -s build
	$(COMPOSE) up

up-pull: .env ## pull from ghcr.io + start (uses prebuilt image tags)
	@$(MAKE) -s pull
	$(COMPOSE) $(PREBUILT) up

# HTTPS via Caddy + a mkcert-issued cert. The current capture flow
# (web drag-drop / video upload + Android HTTP upload) doesn't need
# HTTPS, but `up-https` is still around for operators who want to
# front the studio with TLS for any other reason (corporate network
# policy, an internal CA, etc.).
#
# Single-command path: `make up-https` does everything — bootstraps
# mkcert + cert pair + root CA, builds (locally) with an empty
# NEXT_PUBLIC_API_BASE so the bundle resolves the api origin from
# window.location at runtime, and brings up the stack with the https
# profile. Builds base first for the same reason `make build` does.
up-https: .env caddy/certs/cert.pem ## start with HTTPS via Caddy + mkcert (one-shot)
	-$(COMPOSE) --profile https down --remove-orphans 2>/dev/null
	NEXT_PUBLIC_API_BASE= $(COMPOSE) --profile build build base
	NEXT_PUBLIC_API_BASE= $(COMPOSE) --profile https build
	@$(MAKE) -s up-https-summary
	NEXT_PUBLIC_API_BASE= $(COMPOSE) --profile https up --remove-orphans

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

# ─── Lifecycle ────────────────────────────────────────────────────────

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
#
# Two one-time bootstraps before the apk-* targets work:
#
#   make android-bootstrap       — generates ./gradlew (needs system
#                                  `gradle` binary — apt/brew/sdkman).
#   make android-sdk-bootstrap   — downloads Google's cmdline-tools to
#                                  android/.android-sdk and installs
#                                  the platform-tools / API 35 / build-
#                                  tools 35.0.0 components AGP wants.
#                                  Repo-local + gitignored, so no
#                                  global state, ~3-5 GB per clone.
#
# After both, `make apk-debug` works end-to-end. apk-* targets pick up
# the SDK location automatically via scripts/android-sdk.sh, and the
# Makefile writes android/local.properties from it.

# Probe the host (and the in-repo path) for an Android SDK install.
# `?=` so an explicit env override (ANDROID_SDK_ROOT=/opt/...) wins.
ANDROID_SDK_ROOT ?= $(shell bash scripts/android-sdk.sh 2>/dev/null)

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

android-sdk-bootstrap: ## install Android SDK to android/.android-sdk (gitignored)
	@bash scripts/android-sdk-bootstrap.sh

# Internal: fail-fast if scripts/android-sdk.sh couldn't find an SDK.
# Re-runs the script so the user sees the install hint on stderr.
_require_android_sdk:
	@if [ -z "$(ANDROID_SDK_ROOT)" ]; then \
	  bash scripts/android-sdk.sh; exit 1; \
	fi

# Generate android/local.properties from $(ANDROID_SDK_ROOT). Gradle's
# canonical way to find the SDK on a per-checkout basis. The rule's
# prereq on the probe script means: if the probe is updated, the
# file gets regenerated.
$(ANDROID_DIR)/local.properties: scripts/android-sdk.sh _require_android_sdk
	@printf '# Auto-generated by Make. Points gradle at the Android SDK.\n# Safe to delete; will be re-created on the next make apk-*.\nsdk.dir=%s\n' "$(ANDROID_SDK_ROOT)" > $@
	@echo "[+] wrote $@ → sdk.dir=$(ANDROID_SDK_ROOT)"

apk-debug: $(GRADLEW) $(ANDROID_DIR)/local.properties ## build debug APK (android/app/build/outputs/apk/debug/)
	cd $(ANDROID_DIR) && ./gradlew :app:assembleDebug
	@echo "[+] APK at $(ANDROID_DIR)/app/build/outputs/apk/debug/app-debug.apk"

apk-release: $(GRADLEW) $(ANDROID_DIR)/local.properties ## build release APK (unsigned)
	cd $(ANDROID_DIR) && ./gradlew :app:assembleRelease
	@echo "[+] APK at $(ANDROID_DIR)/app/build/outputs/apk/release/"

# adb resolution: prefer system adb (faster path, common case on
# Android Studio hosts), fall back to the SDK's bundled adb at
# $(ANDROID_SDK_ROOT)/platform-tools/adb. The bundled one is what
# `make android-sdk-bootstrap` installs alongside the platforms /
# build-tools, so it's always there on a freshly-bootstrapped clone
# even if the user never installed `android-platform-tools`.
apk-install: apk-debug ## build + install debug APK on an attached device via adb
	@bundled="$(ANDROID_SDK_ROOT)/platform-tools/adb"; \
	 if command -v adb >/dev/null 2>&1; then \
	   adb_bin="$$(command -v adb)"; \
	 elif [ -x "$$bundled" ]; then \
	   adb_bin="$$bundled"; \
	 else \
	   echo "[!] adb missing — neither on \$$PATH nor in the SDK at"; \
	   echo "    $$bundled"; \
	   echo "    run \`make android-sdk-bootstrap\` to populate the bundled SDK,"; \
	   echo "    or install android-platform-tools system-wide."; \
	   exit 1; \
	 fi; \
	 echo "[+] using $$adb_bin"; \
	 "$$adb_bin" install -r $(ANDROID_DIR)/app/build/outputs/apk/debug/app-debug.apk

android-clean: ## clean android build outputs
	@if [ -x $(GRADLEW) ]; then cd $(ANDROID_DIR) && ./gradlew clean; \
	else rm -rf $(ANDROID_DIR)/build $(ANDROID_DIR)/app/build $(ANDROID_DIR)/.gradle; fi

android-lint: $(GRADLEW) $(ANDROID_DIR)/local.properties ## run android lint (no -Werror)
	cd $(ANDROID_DIR) && ./gradlew :app:lintDebug

$(GRADLEW):
	@echo "[!] $(GRADLEW) is missing. run \`make android-bootstrap\` first."
	@exit 1
