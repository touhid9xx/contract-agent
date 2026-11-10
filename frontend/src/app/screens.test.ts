/**
 * Every screen renders under a spec, or is named here with the reason it does not.
 *
 * Why this file exists: `README.md` carried the sentence "**7 of the 9 web screens
 * have no test**" long after the ninth spec had landed, and nothing could see it,
 * because a count in prose has no second copy to disagree with. Twelve amendments
 * to `STATUS.md` had counted the screens up, one at a time, by hand. A number a
 * person re-counts is a number that drifts (`D10-69`); a number written into the
 * tree is not, so this asserts the *relation* instead and never a count.
 *
 * The rule is one line: a directory holding `page.tsx` holds `page.test.tsx` too,
 * unless the screen is listed in `UNTESTED` below with a reason. Adding a tenth
 * screen without a spec fails here rather than in a sentence nobody re-reads.
 *
 * Like its neighbours, every assertion is relational — no screen name, no count,
 * and no exempt path is written down twice. `UNTESTED` is the one list, and it is
 * checked in both directions: a named exemption that no longer exists is as much
 * a defect as a screen with no spec, because a stale exemption silently widens
 * the hole it was written to document.
 *
 * **What this does not cover, stated rather than implied.** It proves a spec file
 * *exists* beside each screen; it cannot prove that spec renders anything. A
 * `page.test.tsx` reduced to `it("does nothing")` passes here and would have to be
 * caught by reading it. It also says nothing about whether the screen is reachable,
 * linked, or correct — only that the project's own rule, one screen one spec, still
 * holds as the screen list changes.
 */
import { existsSync, readdirSync } from "node:fs";
import { dirname, join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const APP = dirname(fileURLToPath(import.meta.url));

/**
 * Screens that deliberately have no spec, and why.
 *
 * `app/page.tsx` is the root redirect: it renders nothing and its only behaviour
 * is `redirect("/${defaultLocale}")`, which the locale layout's own spec covers
 * from the other side. Testing a redirect against a doubled router would assert
 * that the file calls the function it visibly calls.
 */
const UNTESTED = new Map<string, string>([
  ["page.tsx", "root redirect to the default locale; renders nothing"],
]);

/** Every `page.tsx` under the app directory, relative to it and slash-separated. */
function screens(dir = APP, found: string[] = []): string[] {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) screens(path, found);
    else if (entry.name === "page.tsx") found.push(relative(APP, path).split(sep).join("/"));
  }
  return found;
}

describe("the screen list", () => {
  it("walks a non-empty app tree, so the checks below cannot pass vacuously", () => {
    const found = screens();

    // A floor of two rather than the real figure: one redirect plus at least one
    // locale screen. Pinning today's count would put the drifting number back.
    expect(found.length).toBeGreaterThan(1);
    expect(found).toContain("page.tsx");
    expect(found.some((path) => path.startsWith("[locale]/"))).toBe(true);
  });

  it("gives every screen a spec, except the ones named with a reason", () => {
    const missing = screens().filter((path) => {
      if (UNTESTED.has(path)) return false;
      const spec = join(APP, dirname(path), "page.test.tsx");
      return !existsSync(spec);
    });

    expect(missing).toEqual([]);
  });

  it("names only screens that exist, so an exemption cannot outlive its screen", () => {
    const found = new Set(screens());
    const stale = [...UNTESTED.keys()].filter((path) => !found.has(path));

    expect(stale).toEqual([]);
  });

  it("keeps no orphan spec for a screen that is gone", () => {
    const orphans: string[] = [];
    const walk = (dir: string): void => {
      for (const entry of readdirSync(dir, { withFileTypes: true })) {
        const path = join(dir, entry.name);
        if (entry.isDirectory()) walk(path);
        else if (entry.name === "page.test.tsx" && !existsSync(join(dir, "page.tsx"))) {
          orphans.push(relative(APP, path).split(sep).join("/"));
        }
      }
    };
    walk(APP);

    expect(orphans).toEqual([]);
  });
});
