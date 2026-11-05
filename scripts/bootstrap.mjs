/**
 * One-command developer bootstrap.
 *
 * Creates the Python virtual environment, installs pinned backend dependencies,
 * installs root + frontend Node dependencies, and registers the git hooks.
 * Idempotent: safe to re-run at any time.
 *
 *   npm run bootstrap        # everything
 *   npm run bootstrap -- --skip-node
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const VENV_DIR = path.join(ROOT, "venv");
const IS_WINDOWS = process.platform === "win32";
const VENV_PYTHON = path.join(
  VENV_DIR,
  IS_WINDOWS ? "Scripts" : "bin",
  IS_WINDOWS ? "python.exe" : "python",
);

const skipNode = process.argv.includes("--skip-node");

/** Run a command, inheriting stdio; throws on non-zero exit. */
function run(command, args, options = {}) {
  process.stdout.write(`\n» ${command} ${args.join(" ")}\n`);
  const result = spawnSync(command, args, {
    cwd: ROOT,
    stdio: "inherit",
    shell: IS_WINDOWS,
    ...options,
  });
  if (result.error) {
    throw new Error(`${command} failed to start: ${result.error.message}`);
  }
  if (result.status !== 0) {
    throw new Error(`${command} ${args.join(" ")} exited with ${result.status}`);
  }
}

function findSystemPython() {
  for (const candidate of IS_WINDOWS ? ["python", "py"] : ["python3", "python"]) {
    const probe = spawnSync(candidate, ["--version"], { stdio: "ignore" });
    if (probe.status === 0) return candidate;
  }
  throw new Error("No Python interpreter found on PATH (need >= 3.11)");
}

function ensureVenv() {
  if (fs.existsSync(VENV_PYTHON)) {
    process.stdout.write("✓ virtualenv already present\n");
    return;
  }
  run(findSystemPython(), ["-m", "venv", "venv"]);
}

function main() {
  process.stdout.write("Contract-Expiry Notification Agent — bootstrap\n");

  ensureVenv();
  run(VENV_PYTHON, ["-m", "pip", "install", "--upgrade", "pip"]);
  run(VENV_PYTHON, ["-m", "pip", "install", "-r", "backend/requirements-dev.txt"]);

  if (!skipNode) {
    run("npm", ["install", "--no-audit", "--fund=false"]);
    run("npm", ["--prefix", "frontend", "install", "--no-audit", "--fund=false"]);
    // The Phase 0 demo is typechecked by a pre-commit hook, so its dependencies
    // are part of the gate rather than an optional extra for `npm run demo`.
    run("npm", ["--prefix", "demo/frontend", "install", "--no-audit", "--fund=false"]);
  }

  const preCommit = path.join(
    VENV_DIR,
    IS_WINDOWS ? "Scripts" : "bin",
    IS_WINDOWS ? "pre-commit.exe" : "pre-commit",
  );
  run(preCommit, ["install", "--install-hooks"]);

  process.stdout.write("\n✓ bootstrap complete\n");
  process.stdout.write("  next: npm run dev          (frontend)\n");
  process.stdout.write("  next: npm run demo         (Phase 0 mocked demo)\n");
}

try {
  main();
} catch (error) {
  console.error(`\n✗ bootstrap failed: ${error instanceof Error ? error.message : error}`);
  process.exit(1);
}
