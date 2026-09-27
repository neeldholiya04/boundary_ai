import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

// The repo keeps one settings file at the root. Next.js only reads .env files from this folder, so
// pick up the NEXT_PUBLIC_* values from ../../.env here. Only NEXT_PUBLIC_* (never secrets) are
// read, real environment variables win, and a missing file (e.g. in the Docker build) is skipped.
function rootPublicEnv() {
  const file = resolve(dirname(fileURLToPath(import.meta.url)), "../../.env");
  if (!existsSync(file)) return {};
  const env = {};
  for (const line of readFileSync(file, "utf8").split("\n")) {
    const match = line.match(/^\s*(NEXT_PUBLIC_[A-Z0-9_]+)\s*=\s*(.*?)\s*$/);
    if (!match) continue;
    const [, key, raw] = match;
    const value = raw.replace(/^(['"])(.*)\1$/, "$2");
    if (value && process.env[key] === undefined) env[key] = value;
  }
  return env;
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  distDir: "build-output",
  env: rootPublicEnv()
};

export default nextConfig;
