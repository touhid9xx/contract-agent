/**
 * Bridge for pre-commit hooks that need to drive the frontend toolchain.
 * pre-commit cannot express "run this npm script in a subdirectory" cleanly,
 * so the hook calls this instead.
 */
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FRONTEND_DIR = path.join(ROOT, "frontend");

const ALLOWED = new Set(["typecheck", "lint"]);

const task = process.argv[2];
if (!task || !ALLOWED.has(task)) {
  console.error(
    `usage: node scripts/run-frontend-check.mjs <${[...ALLOWED].join("|")}>`,
  );
  process.exit(2);
}

const result = spawnSync("npm", ["run", task], {
  cwd: FRONTEND_DIR,
  stdio: "inherit",
  // npm is a .cmd shim on Windows, so a shell is required there.
  shell: process.platform === "win32",
});

if (result.error) {
  console.error(`failed to start npm: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status ?? 1);
