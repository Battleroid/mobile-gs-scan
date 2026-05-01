# Contributing

Single-user, single-repo studio. The conventions below keep the
changelog readable and the CI / release tooling working cleanly.

## Branch naming

Every PR branch must be prefixed with the kind of change it makes:

| prefix       | what                                            |
| ------------ | ----------------------------------------------- |
| `feature/`   | new user-visible capability                     |
| `fix/`       | bug fix                                         |
| `chore/`     | tooling / deps / build / refactor-only          |
| `docs/`      | docs / README only                              |
| `refactor/`  | internal restructure, no behavior change        |
| `release/`   | release-prep branches                           |
| `claude/`    | branches Claude opens automatically             |

The `branch-name` workflow validates this on every PR.

## Commit messages

Short imperative subject (under 72 chars), blank line, then body
that explains the *why*, not the *what*.

## CI

- `ci.yml` — every PR + push to main: web build, worker type-check,
  android debug APK assemble.
- `build-images.yml` — push to main + `v*` tags: publishes
  `ghcr.io/<owner>/mobile-gs-scan-{base,api,worker-gs,web}` tagged
  `latest` + `sha-<short>` (and `vN.N.N` on tags).
- `release.yml` — `v*` tags: drafts a GitHub Release with a
  changelog grouped by branch prefix.

## Cutting a release

```bash
git checkout main && git pull
# Bump version (worker/pyproject.toml, web/package.json, android/app
# build.gradle.kts versionName) on a release/ branch, PR'd into main.
git tag v0.X.Y
git push --tags
```

## Adding new docker services

Add a corresponding build job to `.github/workflows/build-images.yml`
that follows the existing `metadata-action` + `build-push-action`
pattern.
