/**
 * Bridge for pre-commit hooks that need to drive a frontend toolchain.
 * pre-commit cannot express "run this npm script in a subdirectory" cleanly,
 * so the hook calls this instead.
 *
 * The repository holds two Next.js apps — the real one at `frontend/` and the
 * Phase 0 demo at `demo/frontend/` — so the project directory is an argument
 * rather than a constant. It defaults to `frontend`, which leaves the original
 * single-app call sites working unchanged.
 */
import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DEFAULT_PROJECT = "frontend";

const ALLOWED = new Set(["typecheck", "lint", "test"]);

const task = process.argv[2];
const projectName = process.argv[3] ?? DEFAULT_PROJECT;

if (!task || !ALLOWED.has(task)) {
  console.error(
    `usage: node scripts/run-frontend-check.mjs <${[...ALLOWED].join("|")}> [project dir]`,
  );
  process.exit(2);
}

const projectDir = path.resolve(ROOT, projectName);
// The directory comes from a hook definition, but a typo there would otherwise
// point npm at an unrelated tree, so keep it inside the repository.
const relative = path.relative(ROOT, projectDir);
if (relative.startsWith("..") || path.isAbsolute(relative)) {
  console.error(`refusing to run outside the repository: ${projectName}`);
  process.exit(2);
}

const result = spawnSync("npm", ["run", task], {
  cwd: projectDir,
  stdio: "inherit",
  // npm is a .cmd shim on Windows, so a shell is required there.
  shell: process.platform === "win32",
});

if (result.error) {
  console.error(`failed to start npm: ${result.error.message}`);
  process.exit(1);
}

process.exit(result.status ?? 1);
