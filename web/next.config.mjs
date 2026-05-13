// Resolve the build label injected into the version chip + Profile-
// style displays. Order of preference:
//   1. APP_BUILD_LABEL env var (set by CI for tagged releases — empty
//      string is honored, yielding a clean ``v0.1.0`` with no SHA).
//   2. Repo-root version.txt file + short git SHA (the dev / master
//      path). The filename matches release-please's ``simple``
//      release-type default so its release PR can bump the file
//      without any extra config.
//   3. Bare "0.1.0" fallback for sandboxed builds with no git context.
//
// Read once at config evaluation time (build) so the resulting string
// is baked into the bundle as ``NEXT_PUBLIC_APP_VERSION``.
import { execSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));

function resolveAppVersion() {
  if (typeof process.env.APP_BUILD_LABEL === "string") {
    return process.env.APP_BUILD_LABEL;
  }
  let base = "0.1.0";
  try {
    base = readFileSync(resolve(__dirname, "..", "version.txt"), "utf8").trim();
  } catch {
    /* repo-root version.txt missing — use the default */
  }
  let sha = "";
  try {
    sha = execSync("git rev-parse --short HEAD", {
      cwd: resolve(__dirname, ".."),
      stdio: ["ignore", "pipe", "ignore"],
    })
      .toString()
      .trim();
  } catch {
    /* git not on PATH or not a checkout — version is just the base */
  }
  return sha ? `${base}+${sha}` : base;
}

const appVersion = resolveAppVersion();

/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  transpilePackages: ["three", "@sparkjsdev/spark"],
  experimental: {
    serverActions: {
      bodySizeLimit: "1024mb",
    },
  },
  env: {
    // Baked into the client bundle so PebbleHeader + any future
    // "About" surface can render the build label without a runtime
    // fetch. NEXT_PUBLIC_* is the Next.js convention for env vars
    // intended to be readable from the browser.
    NEXT_PUBLIC_APP_VERSION: appVersion,
  },
};

export default nextConfig;
