#!/usr/bin/env node
/**
 * Phase 0 demo launcher — one command boots the whole mocked product.
 *
 *   npm run demo              # backend + frontend
 *   npm run demo:backend      # backend only
 *   npm run demo:frontend     # frontend only (assumes backend already up)
 *
 * Nothing here touches a real database, Kafka, Ollama, SMTP, SMS or voice
 * provider. The backend serves static JSON fixtures from demo/fixtures/.
 */
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import net from "node:net";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const FRONTEND_DIR = path.join(ROOT, "demo", "frontend");
const BACKEND_DIR = path.join(ROOT, "demo", "backend");
let BACKEND_PORT = Number(process.env.DEMO_BACKEND_PORT ?? 8000);
let FRONTEND_PORT = Number(process.env.DEMO_FRONTEND_PORT ?? 3000);

const args = process.argv.slice(2);
const onlyArg = args.find((arg) => arg.startsWith("--only="));
const only = onlyArg ? onlyArg.split("=")[1] : "all";

const isWindows = process.platform === "win32";
const children = [];
let shuttingDown = false;

const colors = {
  reset: "\u001b[0m",
  dim: "\u001b[2m",
  bold: "\u001b[1m",
  cyan: "\u001b[36m",
  green: "\u001b[32m",
  yellow: "\u001b[33m",
  red: "\u001b[31m",
};

function log(prefix, color, message) {
  process.stdout.write(`${color}${prefix}${colors.reset} ${message}\n`);
}

function fail(message, hint) {
  log("✖", colors.red, message);
  if (hint) log(" ", colors.dim, hint);
  shutdown(1);
}

/** Resolve the project's virtualenv interpreter, falling back to PATH python. */
function resolvePython() {
  const candidates = isWindows
    ? [path.join(ROOT, "venv", "Scripts", "python.exe"), path.join(ROOT, ".venv", "Scripts", "python.exe")]
    : [path.join(ROOT, "venv", "bin", "python"), path.join(ROOT, ".venv", "bin", "python")];

  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }
  return isWindows ? "python" : "python3";
}

/**
 * A port counts as free only if nothing accepts connections on either address
 * family. Binding a test listener is not enough: on Windows another process can
 * hold `[::]:3000` while a `127.0.0.1:3000` bind still succeeds, and `next dev`
 * (which binds `::`) then dies with EADDRINUSE.
 */
function checkPortFree(port) {
  const hosts = ["127.0.0.1", "::1"];
  return hosts.reduce(
    (previous, host) =>
      previous.then(
        (free) =>
          new Promise((resolve) => {
            if (!free) return resolve(false);
            const socket = net.connect({ port, host });
            const done = (result) => {
              socket.removeAllListeners();
              socket.destroy();
              resolve(result);
            };
            socket.setTimeout(700);
            socket.once("connect", () => done(false));
            socket.once("error", () => done(true));
            socket.once("timeout", () => done(true));
          }),
      ),
    Promise.resolve(true),
  );
}

/**
 * Prefer the requested port, but fall back to the next free one so another
 * local project (or Docker) holding 3000 cannot block GATE 1 review.
 */
async function findPort(preferred, label) {
  for (let port = preferred; port < preferred + 20; port += 1) {
    if (await checkPortFree(port)) {
      if (port !== preferred) {
        log("!", colors.yellow, `Port ${preferred} is busy — ${label} will use ${port} instead.`);
      }
      return port;
    }
  }
  fail(`No free port found in ${preferred}-${preferred + 19} for the ${label}.`, `Set DEMO_${label.toUpperCase()}_PORT to a free port.`);
  return preferred;
}

function run(label, color, command, commandArgs, options = {}) {
  const child = spawn(command, commandArgs, {
    cwd: options.cwd ?? ROOT,
    shell: isWindows, // resolves npm.cmd / python.exe shims on Windows
    stdio: ["ignore", "pipe", "pipe"],
    env: { ...process.env, ...(options.env ?? {}) },
  });

  const relay = (stream, target, streamColor) => {
    let buffer = "";
    stream.on("data", (chunk) => {
      buffer += chunk.toString();
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (line.trim()) target.write(`${streamColor}${label}${colors.reset} ${line}\n`);
      }
    });
  };

  relay(child.stdout, process.stdout, color);
  relay(child.stderr, process.stderr, color);

  child.on("error", (error) => fail(`Failed to start ${label}: ${error.message}`));
  child.on("exit", (code) => {
    if (shuttingDown) return;
    if (code !== 0) fail(`${label} exited with code ${code}.`, "Fix the error above, then run npm run demo again.");
  });

  children.push(child);
  return child;
}

async function waitForBackend(timeoutMs = 40_000) {
  const deadline = Date.now() + timeoutMs;
  const url = `http://127.0.0.1:${BACKEND_PORT}/health`;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) return true;
    } catch {
      // backend not listening yet
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  return false;
}

function shutdown(code = 0) {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of children) {
    if (!child.killed) {
      try {
        if (isWindows) spawn("taskkill", ["/pid", String(child.pid), "/T", "/F"], { stdio: "ignore" });
        else child.kill("SIGTERM");
      } catch {
        // best effort
      }
    }
  }
  setTimeout(() => process.exit(code), 250);
}

async function main() {
  process.stdout.write(
    `\n${colors.bold}${colors.cyan}Contract Expiry Notification Agent — Phase 0 MOCKED demo${colors.reset}\n` +
      `${colors.dim}No database, Kafka, Ollama, email, SMS or voice provider is contacted.${colors.reset}\n\n`,
  );

  const startBackend = only === "all" || only === "backend";
  const startFrontend = only === "all" || only === "frontend";

  if (startFrontend && !existsSync(path.join(FRONTEND_DIR, "node_modules"))) {
    fail("demo/frontend/node_modules is missing.", "Run: npm --prefix demo/frontend install");
    return;
  }

  if (startBackend) {
    BACKEND_PORT = await findPort(BACKEND_PORT, "backend");
  }
  if (startFrontend) {
    FRONTEND_PORT = await findPort(FRONTEND_PORT, "frontend");
  }

  if (startBackend) {
    const python = resolvePython();
    log("»", colors.cyan, `backend  →  uvicorn (${path.relative(ROOT, python) || python})`);
    run("backend", colors.cyan, python, ["-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", String(BACKEND_PORT)], {
      cwd: BACKEND_DIR,
    });

    const healthy = await waitForBackend();
    if (!healthy) {
      fail("The mocked backend did not become healthy in time.", "Check the backend output above (is FastAPI installed in venv?).");
      return;
    }
    log("✓", colors.green, `backend ready at http://127.0.0.1:${BACKEND_PORT}/health`);
  }

  if (startFrontend) {
    log("»", colors.cyan, `frontend →  next dev on :${FRONTEND_PORT}`);
    run("frontend", colors.yellow, "npm", ["run", "dev", "--", "-p", String(FRONTEND_PORT)], {
      cwd: FRONTEND_DIR,
      env: { DEMO_API_URL: `http://127.0.0.1:${BACKEND_PORT}` },
    });
  }

  process.stdout.write(
    `\n${colors.bold}Demo ready:${colors.reset} http://localhost:${FRONTEND_PORT}\n` +
      `${colors.dim}Press Ctrl+C to stop everything.${colors.reset}\n\n`,
  );
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));

main().catch((error) => fail(error instanceof Error ? error.message : String(error)));
