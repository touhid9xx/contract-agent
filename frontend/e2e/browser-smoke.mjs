#!/usr/bin/env node
/**
 * A real browser renders every screen, or this fails.
 *
 * Why this file exists: every screen already has a spec, and `app/screens.test.ts`
 * says in its own docstring what those specs cannot do — they render under jsdom,
 * against a doubled API, with a doubled router. Ten of them agreeing proves the
 * components compose; it does not prove a browser has ever loaded one. Nothing in
 * this repository had, so "the screens work" rested on tests that never executed a
 * single line of the framework they were testing: no server render, no hydration,
 * no i18n catalogue resolved by the real provider, no `fetch` through the Next
 * rewrite to the FastAPI app, no stylesheet.
 *
 * So this runs the real thing, end to end, and asserts the two things a browser
 * alone can answer:
 *
 *   1. **Every screen renders.** The route list is not written down here — it is
 *      walked out of `src/app/[locale]`, the same way `screens.test.ts` walks it,
 *      so a screen added tomorrow is visited without editing this file. A dynamic
 *      segment with no seed value is a hard failure rather than a silent skip.
 *   2. **The app is alive, not just painted.** After signing in through the real
 *      form, the contracts list must show the row this script seeded through the
 *      real API. That row only appears once client JavaScript has run, hydrated,
 *      called React Query, and fetched through the Next proxy from the seeded
 *      token. Server-rendered HTML cannot produce it.
 *
 * The journey is a person's, not a shortcut's: sign in at the form, then click the
 * link the home screen offers for each destination. The client session lives in
 * memory only (`lib/session.ts`), so reaching a screen by URL would mean reloading
 * and arriving signed out — clicking is both what a user does and the only way the
 * authenticated screen is what gets rendered.
 *
 * Nothing here is a unit under test: the production `next start` server, the real
 * FastAPI app on a real socket, a migrated SQLite database and Chromium. Exit 0
 * means all of it agreed. Requires the `playwright` browsers
 * (`npx playwright install chromium`) and a frontend build; a build made against a
 * different API address is rebuilt, because `next build` fixes the proxy target.
 * That rebuilt `.next` is removed on the way out, so this script never leaves a
 * build behind that proxies to a port only this run listened on.
 *
 *   node e2e/browser-smoke.mjs            # headless
 *   SMOKE_HEADED=1 node e2e/browser-smoke.mjs
 *
 * Env: SMOKE_API_PORT (8000, or any free port when 8000 is taken),
 * SMOKE_WEB_PORT (3100), SMOKE_PYTHON (repo venv).
 */

import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { connect, createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND = resolve(HERE, "..");
const ROOT = resolve(FRONTEND, "..");
const BACKEND = join(ROOT, "backend");

// Chosen at boot, not assumed here: 8000 is a popular default, and when something
// else already holds it the browser's `/api/*` traffic reaches *that* server and
// comes back as a rejected password. `chooseApiPort()` states the rule.
let API_PORT = Number(process.env.SMOKE_API_PORT ?? 8000);
const WEB_PORT = Number(process.env.SMOKE_WEB_PORT ?? 3100);
const WEB = `http://127.0.0.1:${WEB_PORT}`;
let API = `http://127.0.0.1:${API_PORT}`;
const LOCALE = "en";

/**
 * The port this run rebuilt `.next` for, or null when it reused the build it
 * found. A rebuilt `.next` records *this run's* API address in
 * `.next/routes-manifest.json`, so leaving it on disk after the run means the
 * next `npm run start` proxies every `/api/*` call to a port nothing listens on
 * any more — and says so only as failed sign-ins. `finally` removes it.
 */
let rebuiltForPort = null;

// The account this script creates, and the contract it seeds. Both are asserted
// against, so they are written once and read from one place.
const TENANT_SLUG = "browser-smoke";
const TENANT_NAME = "Browser Smoke Ltd";
const ADMIN_EMAIL = "admin@browser-smoke.test";
const ADMIN_PASSWORD = "browser-smoke-not-a-credential";
const CUSTOMER_REF = "Browser Smoke Customer";

/**
 * How to invoke npm.
 *
 * On Windows `npm` is a `.cmd` shim, and Node refuses to execute one without a
 * shell — `spawnSync("npm", …)` fails with `ENOENT`, which reads like npm is not
 * installed. `stop()` already expects a shell on Windows (`/T` exists for it), so
 * asking for one changes nothing else.
 */
const NPM = process.platform === "win32" ? "npm.cmd" : "npm";
const NPM_OPTIONS = process.platform === "win32" ? { shell: true } : {};

/** Filled in by a dynamic route segment; `need()` fails the run if it is missing. */
const SEGMENT_VALUES = new Map([["id", null]]);

const children = [];
let tempRoot = null;

// --- small helpers ---------------------------------------------------------

function say(line = "") {
  process.stdout.write(`${line}\n`);
}

/** Assert with a message a stranger can act on. */
function need(condition, message) {
  if (!condition) throw new Error(message);
}

function python() {
  if (process.env.SMOKE_PYTHON) return process.env.SMOKE_PYTHON;
  for (const candidate of [
    join(ROOT, "venv", "Scripts", "python.exe"),
    join(ROOT, "venv", "bin", "python"),
  ]) {
    if (existsSync(candidate)) return candidate;
  }
  return "python";
}

/** A port nothing is listening on, taken from the ephemeral range. */
async function freePort() {
  const server = createServer();
  await new Promise((done, fail) => {
    server.once("error", fail);
    server.listen(0, "127.0.0.1", done);
  });
  const { port } = server.address();
  await new Promise((done) => server.close(done));
  return port;
}

/**
 * Whether nothing is answering on `port`.
 *
 * Asking by *connecting*, not by binding. On Windows `SO_REUSEADDR` behaves like
 * `SO_REUSEPORT`: a second listener can bind a port that is already taken, so a
 * bind probe reports "free" while another server is happily answering on it. That
 * is not a theory — it is how the first run of this file ended up posting a login
 * to a Docker stack left over from an earlier session and calling the wrong
 * password. A refused connection is the only signal that means free.
 */
async function portIsFree(port) {
  return await new Promise((done) => {
    const socket = connect({ port, host: "127.0.0.1" });
    const finish = (free) => {
      socket.destroy();
      done(free);
    };
    socket.setTimeout(2_000);
    socket.once("connect", () => finish(false));
    socket.once("timeout", () => finish(true));
    socket.once("error", () => finish(true));
  });
}

/**
 * The port the API will listen on — and never somebody else's.
 *
 * `next build` bakes `API_PROXY_URL` into the rewrite (`next.config.ts`), so the
 * browser's `/api/*` requests go where that build pointed, not where this run
 * listens. Defaulting to 8000 and hoping it is free is how a run ends up talking
 * to a Docker stack left over from another session: it fails at sign-in with
 * "invalid tenant, email or password", which reads like a defect in this script
 * and sends the next hour in the wrong place. So an explicit `SMOKE_API_PORT`
 * wins (and is refused if taken, because CI sets it deliberately), 8000 is used
 * only when genuinely free, and otherwise any free port will do.
 */
async function chooseApiPort() {
  const requested = process.env.SMOKE_API_PORT;
  if (requested) {
    const port = Number(requested);
    need(Number.isInteger(port) && port > 0, `SMOKE_API_PORT="${requested}" is not a port`);
    need(await portIsFree(port), `SMOKE_API_PORT=${port} is already in use: stop its holder first`);
    return port;
  }
  // A fixed ladder, in order, rather than "whatever is free": the build records
  // the address it was made for, so returning the same port on a second run means
  // no rebuild. An ephemeral port would invalidate the build every single time.
  for (const port of [8000, 8010, 8011, 8012, 8013]) {
    if (await portIsFree(port)) {
      if (port !== 8000) say(`· 127.0.0.1:8000 is already in use, so the API gets ${port}`);
      return port;
    }
  }
  const port = await freePort();
  say(`· every usual API port is taken, so the API gets ${port}`);
  return port;
}

/** Run a command to completion and refuse to continue when it fails. */
function run(executable, args, options) {
  const result = spawnSync(executable, args, { stdio: "inherit", ...options });
  if (result.error) throw new Error(`could not start ${executable}: ${result.error.message}`);
  need(result.status === 0, `${executable} ${args.join(" ")} exited ${result.status}`);
}

/** Start a long-lived process, recording it so `finally` can stop it. */
function start(executable, args, options) {
  const child = spawn(executable, args, { stdio: ["ignore", "pipe", "pipe"], ...options });
  const log = [];
  const capture = (chunk) => log.push(chunk.toString());
  child.stdout.on("data", capture);
  child.stderr.on("data", capture);
  child.log = () => log.join("");
  children.push(child);
  return child;
}

/**
 * Stop a child and everything it spawned.
 *
 * `npm run start` runs under a shell on Windows, so killing the shell leaves the
 * server holding the port and the next run fails for a reason that looks like a
 * code defect. `/T` walks the tree.
 */
function stop(child) {
  if (!child || child.killed || child.exitCode !== null) return;
  if (process.platform === "win32" && child.pid) {
    spawnSync("taskkill", ["/pid", String(child.pid), "/T", "/F"], { stdio: "ignore" });
  } else {
    child.kill("SIGKILL");
  }
}

/** Poll `url` until it answers with `ok`, or fail with the child's own output. */
async function waitForHttp(url, ok, { what, child, timeoutMs = 180_000 }) {
  const deadline = Date.now() + timeoutMs;
  let last = "no answer";
  while (Date.now() < deadline) {
    if (child && child.exitCode !== null) {
      throw new Error(`${what} exited (${child.exitCode}):\n${child.log()}`);
    }
    try {
      const response = await fetch(url, { redirect: "manual" });
      if (ok(response.status)) return response;
      last = `status ${response.status}`;
    } catch (error) {
      last = error instanceof Error ? error.message : String(error);
    }
    await new Promise((done) => setTimeout(done, 250));
  }
  throw new Error(`${what} never answered ${url} (${last})\n${child ? child.log() : ""}`);
}

async function postJson(url, body, token) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(token ? { authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  need(response.ok, `POST ${url} answered ${response.status}: ${text.slice(0, 400)}`);
  return JSON.parse(text);
}

// --- the route list, walked out of the tree --------------------------------

/** Every `page.tsx` under `src/app/[locale]`, as a URL path with a leading slash. */
function screens() {
  const root = join(FRONTEND, "src", "app", "[locale]");
  const found = [];
  const walk = (dir) => {
    for (const entry of readdirSync(dir, { withFileTypes: true })) {
      const path = join(dir, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (entry.name === "page.tsx") {
        const segments = relative(root, dirname(path)).split(sep).filter(Boolean);
        const url = [LOCALE, ...segments].join("/");
        found.push(url === LOCALE ? `/${LOCALE}` : `/${url}`);
      }
    }
  };
  walk(root);
  found.sort();
  return found;
}

/**
 * Turn a route template into a URL, or fail.
 *
 * A dynamic segment needs a real id, and there is no default that would be
 * honest: `/contracts/[id]` rendered against a made-up id is exactly the screen
 * that renders "not found" and passes. So an unfilled segment is an error naming
 * what to add to `SEGMENT_VALUES`.
 */
function urlFor(route) {
  const filled = route.replace(/\[([^\]]+)\]/g, (_, name) => {
    const value = SEGMENT_VALUES.get(name);
    need(
      value !== null && value !== undefined,
      `route ${route} needs a value for [${name}]: add one to SEGMENT_VALUES`,
    );
    return String(value);
  });
  return filled;
}

// --- browser diagnostics ---------------------------------------------------

/**
 * Collect what the browser would show a user as a defect, and nothing else.
 *
 * The three sources are deliberately separate: an uncaught exception (the page
 * is broken), a console error React or Next logged (something is wrong even
 * though the paint finished), and an API response the screen could not use.
 * A 401 is expected while signed out and is filtered by the caller, not here.
 */
function watch(page) {
  const problems = [];
  page.on("pageerror", (error) => problems.push(`uncaught: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const url = message.location()?.url ?? "";
    const text = message.text();
    // A missing favicon is reported as a console error in Chromium and is not a
    // defect in any screen; every other failed resource is.
    if (url.endsWith("favicon.ico") && text.includes("Failed to load resource")) return;
    problems.push(`console: ${text}`);
  });
  page.on("requestfailed", (request) => {
    const failure = request.failure()?.errorText ?? "failed";
    // Aborted requests are how the router cancels a navigation it replaced.
    if (failure.includes("net::ERR_ABORTED")) return;
    problems.push(`request failed: ${request.url()} (${failure})`);
  });
  page.on("response", (response) => {
    const status = response.status();
    if (status < 400) return;
    const url = response.url();
    if (!url.includes("/api/") && !url.includes("/health")) return;
    problems.push(`API ${status}: ${url}`);
  });
  return problems;
}

function assertClean(problems, what) {
  need(problems.length === 0, `${what}:\n  - ${problems.join("\n  - ")}`);
}

/**
 * Text a browser shows when the screen itself failed, rather than the data in it.
 *
 * These are the strings Next prints for a thrown render, a missing route and a
 * crash in the server component tree. A screen that renders one of them is not a
 * screen that rendered.
 */
const BROKEN_PAGE_MARKERS = [
  "Application error",
  "Internal Server Error",
  "This page could not be found",
];

async function assertVisible(page, what) {
  const text = (await page.locator("body").innerText()).trim();
  need(text.length > 0, `${what}: the page rendered no text at all`);
  for (const marker of BROKEN_PAGE_MARKERS) {
    need(!text.includes(marker), `${what}: the browser showed "${marker}"`);
  }
  return text;
}

/**
 * Wait until `text` is on the page, then stop waiting.
 *
 * `waitForLoadState("networkidle")` is not a substitute: after a client-side
 * navigation the document never loads again, so the page is *already* idle and the
 * call returns before React Query has even started its fetch. Waiting for the text
 * is the only thing that waits for the right moment — and when it never arrives it
 * can quote what the screen did show, which a timeout alone cannot.
 */
async function waitForText(page, text, what) {
  try {
    await page.getByText(text).first().waitFor({ timeout: 30_000 });
  } catch {
    const seen = (await page.locator("body").innerText()).trim().slice(0, 600);
    throw new Error(`${what} never showed "${text}". It showed:\n${seen}`);
  }
}

// --- the board's geometry --------------------------------------------------

/**
 * The dashboard panels that must share the top row at this viewport.
 *
 * Read out of the i18n catalogue rather than written down here, for the reason the
 * route list is walked out of the tree: a heading is copy, and copy gets reworded.
 * A literal here would go red the day someone retitled a panel, and it would say
 * the layout broke when nothing had moved.
 *
 * These three are the panels the board renders unconditionally; the rest depend on
 * whether there is anything to show, so a count of them would not be stable.
 */
const BOARD_PANELS = ["totalsHeading", "expiringHeading", "reminderHeading"];

function boardHeadings() {
  const path = join(FRONTEND, "src", "i18n", "messages", `${LOCALE}.json`);
  const catalogue = JSON.parse(readFileSync(path, "utf8"));
  return BOARD_PANELS.map((key) => {
    const text = catalogue.dashboard?.[key];
    need(typeof text === "string" && text.length > 0, `the catalogue has no dashboard.${key}`);
    return text;
  });
}

/**
 * Every panel card on the loaded board, in DOM order.
 *
 * All of them, rather than the three fixed panels the row check below names: the
 * claim being measured is that the board *tiles*, and three boxes cannot tell a
 * board from three boxes with four panels piled on top of them.
 */
async function boardCards(page) {
  const cards = page.locator("div.rounded-card");
  const count = await cards.count();
  need(count >= 3, `the dashboard rendered ${count} card(s), fewer than its three fixed panels`);
  const boxes = [];
  for (let i = 0; i < count; i += 1) {
    const box = await cards.nth(i).boundingBox();
    need(box, `the dashboard's card ${i} has no box`);
    boxes.push({ index: i, ...box });
  }
  return boxes;
}

/** The distinct left edges the cards landed on, i.e. the columns actually used. */
function columnXs(boxes) {
  const xs = [];
  for (const { x } of boxes) {
    if (!xs.some((at) => Math.abs(at - x) <= 1)) xs.push(x);
  }
  return xs.sort((a, b) => a - b);
}

/** Whether two boxes share more than a pixel of area. */
function overlapping(a, b) {
  const across = Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x);
  const down = Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y);
  return across > 1 && down > 1;
}

/**
 * Assert the board uses exactly `expected` columns and nothing sits on anything.
 *
 * Two defects this catches that a same-row check cannot: a panel pushed into a
 * fourth column (a fourth left edge), and panels whose columns shadow each other
 * (the right edge of one past the left edge of the next).
 */
async function assertBoardColumns(page, expected, width) {
  const boxes = await boardCards(page);
  const where = boxes
    .map((box) => `#${box.index} at x=${Math.round(box.x)} y=${Math.round(box.y)}`)
    .join(", ");

  const xs = columnXs(boxes);
  need(
    xs.length === expected,
    `at ${width}px the dashboard's ${boxes.length} panels used ${xs.length} column(s), ` +
      `not ${expected} (${where})`,
  );

  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      need(
        !overlapping(boxes[i], boxes[j]),
        `at ${width}px the dashboard's panels #${boxes[i].index} and #${boxes[j].index} ` +
          `overlap (${where})`,
      );
    }
  }
  return boxes;
}

/**
 * Measure where the board's panels actually landed.
 *
 * Every other check in this file asks whether a screen rendered. That question is
 * deaf to layout: seven cards stacked down a single narrow column render
 * flawlessly, and every screen spec and every "is there text" assertion passes. The
 * board was exactly that, so "the panels tile a grid" needs boxes measured, not a
 * stylesheet inspected — a class name in the HTML says what was asked for, not what
 * the browser did with it.
 *
 * `div.rounded-card` is the handle the screen specs already use to find a `Card`
 * (`closest(".rounded-card")`), because that class is the only structural mark the
 * primitive leaves behind.
 */
async function assertBoardIsGrid(page) {
  const headings = boardHeadings();
  // The board is rendered only once the fetch lands, so wait for the first panel
  // rather than measuring the loading card that stands in for it.
  await waitForText(page, headings[0], "the dashboard board");

  // The whole board, not just the three panels named below: every card must land
  // on one of xl's three columns and none may sit on another.
  await assertBoardColumns(page, 3, 1280);

  const boxes = [];
  for (const text of headings) {
    const card = page
      .locator("div.rounded-card")
      .filter({ has: page.getByRole("heading", { name: text, exact: true }) })
      .first();
    const box = await card.boundingBox();
    need(box, `the dashboard has no panel headed "${text}"`);
    boxes.push({ text, ...box });
  }

  const where = boxes
    .map((box) => `"${box.text}" at x=${Math.round(box.x)} y=${Math.round(box.y)}`)
    .join(", ");

  // xl is 1280px in Tailwind's default scale and `main()` opens the context at
  // exactly that width, so `xl:grid-cols-3` applies to this run by construction.
  const top = boxes[0];
  need(
    boxes.every((box) => Math.abs(box.y - top.y) <= 1),
    `the dashboard board is stacked instead of tiled (${where}). At a 1280px viewport ` +
      `these ${boxes.length} panels must share one top row.`,
  );

  for (let i = 1; i < boxes.length; i += 1) {
    need(
      boxes[i].x > boxes[i - 1].x,
      `the dashboard panels are not laid out left to right (${where})`,
    );
  }

  const last = boxes[boxes.length - 1];
  const span = last.x + last.width - top.x;
  need(
    span > 768,
    `the dashboard board spans ${Math.round(span)}px, no wider than the 768px reading ` +
      `column it was widened out of, so the panels are not using the page (${where})`,
  );

  /*
    Then the other half of `md:grid-cols-2 xl:grid-cols-3`, at a width where only the
    `md` rule applies. A board that tiles at xl and collapses below it is still a
    defect, and it is the one a laptop at default zoom actually shows. Measured by
    resizing the live page rather than reloading, so the two readings describe the
    same rendered board; the viewport is put back so the passes that follow run at
    the width they expect.
  */
  await page.setViewportSize({ width: 900, height: 900 });
  await settleLayout(page);
  await assertBoardColumns(page, 2, 900);
  await page.setViewportSize({ width: 1280, height: 900 });
  await settleLayout(page);
}

/**
 * Wait for the browser to re-run layout after a viewport change.
 *
 * `boundingBox()` flushes layout itself, so this guards the frame in which the
 * viewport change has been applied but the next layout has not been produced —
 * two animation frames, because one can be the frame that was already in flight.
 */
async function settleLayout(page) {
  await page.evaluate(
    () =>
      new Promise((settled) => {
        requestAnimationFrame(() => requestAnimationFrame(settled));
      }),
  );
}

// --- the two passes --------------------------------------------------------

/**
 * Every route, loaded by URL while signed out.
 *
 * This is the pass that answers the claim in the ledger: a browser renders the
 * screen. It costs one full page load per route and asserts the server render,
 * hydration, the i18n catalogue and the stylesheet all arrived.
 */
async function rendersWhileSignedOut(page, route, problems) {
  problems.length = 0;
  const response = await page.goto(`${WEB}${urlFor(route)}`, { waitUntil: "load" });
  need(response, `${route}: no response`);
  need(response.status() === 200, `${route}: answered HTTP ${response.status()}`);
  await page.waitForLoadState("networkidle");
  await assertVisible(page, route);
  assertClean(problems, route);
}

/** Fill the real sign-in form and land on the home screen, signed in. */
async function signIn(page) {
  await page.goto(`${WEB}/${LOCALE}/login`, { waitUntil: "load" });
  await page.fill("#tenant", TENANT_SLUG);
  await page.fill("#email", ADMIN_EMAIL);
  await page.fill("#password", ADMIN_PASSWORD);
  await page.click('button[type="submit"]');
  await page.waitForURL(`${WEB}/${LOCALE}`, { timeout: 30_000 });
  // The account card is the only proof the session took: it renders values that
  // exist nowhere else on the page and come from the signed-in principal.
  await page.getByText(ADMIN_EMAIL).first().waitFor({ timeout: 30_000 });
}

/**
 * Reach a screen the way a person does: sign in, then click the link the home
 * screen offers for it. Reached by URL instead, the in-memory session is gone on
 * load and the screen under test would be the signed-out one.
 */
async function reachSignedIn(page, route, problems) {
  problems.length = 0;
  await signIn(page);
  await page.click(`a[href="${route}"]`);
  await page.waitForURL(`${WEB}${route}`, { timeout: 30_000 });
  await page.waitForLoadState("networkidle");
  const text = await assertVisible(page, route);
  assertClean(problems, route);
  return text;
}

// --- boot ------------------------------------------------------------------

function backendEnv(databasePath, documentsPath) {
  return {
    ...process.env,
    APP_ENV: "development",
    DATABASE_URL: `sqlite+pysqlite:///${databasePath.replace(/\\/g, "/")}`,
    DOCUMENT_STORE_PATH: documentsPath,
    // `.env.example`'s two placeholders, for the reason that file gives: the
    // cipher and the blind index are built eagerly, so an empty value here is a
    // process that never starts.
    ENCRYPTION_KEY: "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    CONTACT_BLIND_INDEX_KEY: "browser-smoke-not-a-credential-blind-index-key",
    LOG_LEVEL: "WARNING",
  };
}

async function bootBackend() {
  tempRoot = mkdtempSync(join(tmpdir(), "contract-agent-browser-smoke-"));
  const databasePath = join(tempRoot, "smoke.sqlite");
  const documentsPath = join(tempRoot, "documents");
  const env = backendEnv(databasePath, documentsPath);
  const py = python();

  say(`· migrating ${databasePath}`);
  run(py, ["-m", "alembic", "upgrade", "head"], { cwd: BACKEND, env });

  say(`· creating the admin ${ADMIN_EMAIL}`);
  const created = spawnSync(
    py,
    [
      "-m",
      "app.cli",
      "create-admin",
      "--tenant-slug",
      TENANT_SLUG,
      "--tenant-name",
      TENANT_NAME,
      "--email",
      ADMIN_EMAIL,
      "--password-stdin",
    ],
    { cwd: BACKEND, env, input: `${ADMIN_PASSWORD}\n`, stdio: ["pipe", "inherit", "inherit"] },
  );
  need(created.status === 0, `create-admin exited ${created.status}`);

  say(`· starting the API on ${API}`);
  const api = start(
    py,
    ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(API_PORT)],
    { cwd: BACKEND, env },
  );
  await waitForHttp(`${API}/health`, (status) => status === 200, { what: "the API", child: api });
  // `/health` answering is not proof it was *our* process answering: a server
  // already on the port replies first and the child's failure is observed a
  // moment later. If ours is gone, the port belongs to somebody else.
  need(
    api.exitCode === null,
    `the API exited, so ${API} belongs to another process:\n${api.log()}`,
  );
  return api;
}

/**
 * The API address the built frontend actually proxies to, read from the build.
 *
 * `next.config.ts` resolves `API_PROXY_URL` with `||`, and `next build` writes the
 * result into `.next/routes-manifest.json`. Reading it back is the only honest way
 * to answer "where will this build send `/api/*`" — `process.env` here says what
 * this shell holds, not what the build was made with.
 */
function builtProxy() {
  const manifestPath = join(FRONTEND, ".next", "routes-manifest.json");
  need(existsSync(manifestPath), "frontend/ is not built: run `npm run build` in frontend/ first");
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  const rule = (manifest.rewrites?.afterFiles ?? []).find(
    (entry) => entry.source === "/api/:path*",
  );
  need(rule, `the build has no "/api/:path*" rewrite, so no screen could reach the API`);
  const target = new URL(rule.destination);
  return {
    href: `${target.protocol}//${target.host}`,
    port: Number(target.port || (target.protocol === "https:" ? 443 : 80)),
  };
}

/**
 * Build the frontend against this run's API, when the existing build points
 * somewhere else.
 *
 * A rebuild is slow and this script is meant to be cheap, so the common case — a
 * build made with the default, port 8000 free — does nothing at all. Every other
 * case rebuilds rather than let the browser read a stale address, because a run
 * proxying to a port nothing listens on fails as "the contracts list did not show
 * the seeded row", which names a symptom and not the cause.
 */
function ensureBuiltFor(apiPort) {
  const built = builtProxy();
  if (built.port === apiPort) return;
  say(`· the build proxies to ${built.href}, but this run serves the API on ${API}`);
  say(`  rebuilding the frontend with API_PROXY_URL=${API}`);
  run(NPM, ["run", "build"], {
    cwd: FRONTEND,
    env: { ...process.env, API_PROXY_URL: API },
    ...NPM_OPTIONS,
  });
  const rebuilt = builtProxy();
  need(rebuilt.port === apiPort, `after rebuilding, the frontend still proxies to ${rebuilt.href}`);
  rebuiltForPort = apiPort;
}

async function bootFrontend() {
  say(`· starting the web app on ${WEB}`);
  const web = start(NPM, ["run", "start"], { cwd: FRONTEND, ...NPM_OPTIONS });
  await waitForHttp(`${WEB}/${LOCALE}/login`, (status) => status === 200, {
    what: "the web app",
    child: web,
    timeoutMs: 120_000,
  });
  return web;
}

/**
 * Seed one contract through the real API, not the database.
 *
 * Inserting the row directly would prove the screen can render a row that no
 * write path produces. This goes through `POST /contracts` with a token from
 * `POST /auth/login` — the same two calls the browser makes — so a contract the
 * list shows is a contract the API agreed to store.
 */
async function seed() {
  const session = await postJson(`${API}/api/v1/auth/login`, {
    tenant: TENANT_SLUG,
    email: ADMIN_EMAIL,
    password: ADMIN_PASSWORD,
  });
  const created = await postJson(
    `${API}/api/v1/contracts`,
    {
      customer_ref: CUSTOMER_REF,
      start_date: "2024-01-01",
      end_date: "2030-01-01",
      timezone: "Europe/London",
    },
    session.access_token,
  );
  say(`· seeded contract ${created.id} (${CUSTOMER_REF})`);
  SEGMENT_VALUES.set("id", created.id);
  return created.id;
}

// --- main ------------------------------------------------------------------

async function main() {
  const routes = screens();
  need(
    routes.length > 1,
    "walked the app tree and found no screens, so nothing below would mean anything",
  );
  say(`${routes.length} screens found under src/app/[locale]`);

  let browser = null;
  try {
    // The port is decided before anything boots: the frontend build has to agree
    // with it, and a rebuild is a minute we would rather not spend behind a
    // migration and an admin bootstrap.
    API_PORT = await chooseApiPort();
    API = `http://127.0.0.1:${API_PORT}`;
    ensureBuiltFor(API_PORT);
    await bootBackend();
    const contractId = await seed();
    await bootFrontend();

    browser = await chromium.launch({ headless: process.env.SMOKE_HEADED !== "1" });
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    const problems = watch(page);

    // Pass 1 — every screen renders for a signed-out browser.
    const signedOut = [];
    for (const route of routes) {
      await rendersWhileSignedOut(page, route, problems);
      signedOut.push(route);
      say(`  ok  ${urlFor(route)}`);
    }

    // Pass 2 — the same screens, reached by signing in and clicking, carrying the
    // seeded row. The detail screen has no link on the home page, so it is reached
    // from the list it belongs to.
    await reachSignedIn(page, `/${LOCALE}/contracts`, problems);
    // Server-rendered HTML cannot contain this row: it exists only because client
    // JavaScript hydrated, asked React Query, and fetched through the Next proxy
    // with the seeded token. If the proxy pointed anywhere else, it never appears.
    await waitForText(page, CUSTOMER_REF, `the contracts list (client-side fetching)`);
    say(`  ok  /${LOCALE}/contracts shows the seeded row`);

    problems.length = 0;
    await page.click(`a[href="/${LOCALE}/contracts/${contractId}"]`);
    await page.waitForURL(`${WEB}/${LOCALE}/contracts/${contractId}`, { timeout: 30_000 });
    await waitForText(page, CUSTOMER_REF, "the contract detail screen");
    await assertVisible(page, "the contract detail screen");
    assertClean(problems, "the contract detail screen");
    say(`  ok  /${LOCALE}/contracts/${contractId}`);

    // The board is the one screen whose claim is a shape and not a sentence: it has
    // to tile. It is measured below instead of merely visited, which is why it is
    // not in the loop's list.
    await reachSignedIn(page, `/${LOCALE}/dashboard`, problems);
    await assertBoardIsGrid(page);
    say(`  ok  /${LOCALE}/dashboard tiles its board`);

    for (const route of routes) {
      if (
        route === `/${LOCALE}` ||
        route === `/${LOCALE}/contracts` ||
        route === `/${LOCALE}/login` ||
        route === `/${LOCALE}/dashboard`
      ) {
        continue;
      }
      if (route.includes("[")) continue; // dynamic screens are covered above
      await reachSignedIn(page, route, problems);
      say(`  ok  ${route} (signed in)`);
    }

    say("");
    say(
      `pass: ${signedOut.length} screens rendered signed out, and the signed-in journey followed.`,
    );
  } finally {
    if (browser) await browser.close();
    for (const child of children.reverse()) stop(child);
    if (tempRoot) rmSync(tempRoot, { recursive: true, force: true });
    // Deliberately after the frontend server is stopped: `next start` holds the
    // directory open on Windows, and a half-deleted `.next` is worse than none.
    // A failed removal is reported and not thrown — it happens inside `finally`,
    // where a throw would replace the real failure with this one.
    if (rebuiltForPort !== null) {
      try {
        rmSync(join(FRONTEND, ".next"), { recursive: true, force: true });
        say(
          `· removed .next: it was built for ${API}, and no later \`npm run start\` ` +
            `should proxy there`,
        );
      } catch (error) {
        say(`! could not remove the run-specific .next: ${error.message}`);
        say(`  run \`npm run build\` in frontend/ before serving it again`);
      }
    }
  }
}

main().then(
  () => process.exit(0),
  (error) => {
    process.stderr.write(
      `\nbrowser smoke failed: ${error instanceof Error ? error.message : error}\n`,
    );
    process.exit(1);
  },
);
