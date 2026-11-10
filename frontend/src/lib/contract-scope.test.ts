/**
 * The vocabulary of a scoped address, pinned by a table.
 *
 * `D10-36` put a contract's scope in the query string and then had to record the
 * cost: `contract-scope.ts` is pure, total and free of React, its parsing rules
 * were hand-checked against `Number()`'s traps, and nothing but `tsc` exercised
 * them. A compiler proves the shapes line up; it cannot prove that `"0"` is
 * refused or that `"12abc"` does not become contract 12. This file is that test.
 *
 * The tables below are also where the `"07"` ruling is pinned (`D10-76`): a
 * zero-padded id is the same contract, so `"012"` is 12 and `"0"` still is not
 * an id at all.
 *
 * It passes a stub to `readContractScope` instead of a router, which is what the
 * structural `ReadableParams` type in the module was written for.
 */
import { describe, expect, it } from "vitest";

import {
  CONTRACT_PARAM,
  RESOLVED_PARAM,
  contractIdFromInput,
  inputNamesAContract,
  readContractScope,
  readResolved,
  withContractScope,
  withResolved,
} from "./contract-scope";

/** The only part of `URLSearchParams` the module reads, so a test can spell it. */
function params(query: Record<string, string> = {}): { get(name: string): string | null } {
  return { get: (name: string) => query[name] ?? null };
}

/** An address this module wrote, read back the way a screen reads it. */
function queryOf(address: string): URLSearchParams {
  const [, query = ""] = address.split("?");
  return new URLSearchParams(query);
}

/** Every input that is no scope at all: absent, and present but empty. */
const NO_SCOPE: (string | null)[] = [null, ""];

/** Inputs that name a contract, with the id they name. */
const A_CONTRACT: [string, number][] = [
  ["1", 1],
  ["12", 12],
  ["9007199254740991", Number.MAX_SAFE_INTEGER],
  // Padded with zeros and read as the same contract, which is the ruling
  // `D10-76` records: the server answers `GET /contracts/01` with 200, and the
  // detail page has always opened `/contracts/07` as contract 7.
  ["07", 7],
  ["012", 12],
  ["070", 70],
  ["0007", 7],
];

/**
 * Inputs that cannot be a contract id.
 *
 * These are the shapes `Number()` would rescue into a *different* contract —
 * `" 12"`, `"12 "`, `"1.5"`, `"1e3"` and `"0x10"` are 12, 12, 1.5, 1000 and 16 —
 * plus the tails it cannot rescue (`"12abc"` is `NaN`, and `Number.parseInt` reads
 * it as 12 anyway) and the zero id, which no contract has. Padding a *zero* is no
 * rescue either: `"00"` and `"000"` are still id zero.
 *
 * `"012"` used to sit in this list and no longer does: padding a non-zero number
 * with zeros is the one spelling that cannot change what it names (`D10-76`).
 */
const MALFORMED = [
  "0",
  "00",
  "000",
  "12abc",
  "abc",
  "1.5",
  "-1",
  "+12",
  " 12",
  "12 ",
  "0x10",
  "1e3",
  "12,",
  "9007199254740992",
];

describe("readContractScope", () => {
  it("reads a missing or empty parameter as no scope at all", () => {
    for (const raw of NO_SCOPE) {
      const query = raw === null ? params() : params({ [CONTRACT_PARAM]: raw });
      expect(readContractScope(query), `raw=${String(raw)}`).toEqual({ kind: "none" });
    }
  });

  it("reads a whole positive number as that contract", () => {
    for (const [raw, id] of A_CONTRACT) {
      expect(readContractScope(params({ [CONTRACT_PARAM]: raw })), `raw=${raw}`).toEqual({
        kind: "contract",
        id,
      });
    }
  });

  it("refuses anything else rather than guessing an id out of it", () => {
    for (const raw of MALFORMED) {
      // `raw` is echoed back, because the screen names the bad value it refused.
      expect(readContractScope(params({ [CONTRACT_PARAM]: raw })), `raw=${raw}`).toEqual({
        kind: "invalid",
        raw,
      });
    }
  });

  it("knows nothing about other parameters", () => {
    expect(readContractScope(params({ [RESOLVED_PARAM]: "1" }))).toEqual({ kind: "none" });
  });
});

describe("readResolved", () => {
  it("reads 1 and only 1 as yes", () => {
    expect(readResolved(params({ [RESOLVED_PARAM]: "1" }))).toBe(true);
  });

  it("reads anything else as the working queue", () => {
    for (const raw of ["0", "true", "yes", "11", "", " 1"]) {
      expect(readResolved(params({ [RESOLVED_PARAM]: raw })), `raw=${raw}`).toBe(false);
    }
    expect(readResolved(params())).toBe(false);
  });
});

describe("withContractScope", () => {
  it("leaves the base alone when there is no scope", () => {
    expect(withContractScope("/en/agent", undefined)).toBe("/en/agent");
  });

  it("spells the parameter the module parses", () => {
    expect(withContractScope("/en/agent", 12)).toBe(`/en/agent?${CONTRACT_PARAM}=12`);
  });
});

describe("withResolved", () => {
  it("leaves the base alone when the flag is off", () => {
    expect(withResolved("/en/escalations", false)).toBe("/en/escalations");
  });

  it("keeps an existing query string instead of starting a second one", () => {
    expect(withResolved("/en/x?a=1", true)).toBe(`/en/x?a=1&${RESOLVED_PARAM}=1`);
  });
});

describe("an address this module writes reads back as the same scope", () => {
  it("round-trips every id the parser accepts", () => {
    for (const [, id] of A_CONTRACT) {
      expect(readContractScope(queryOf(withContractScope("/en/agent", id)))).toEqual({
        kind: "contract",
        id,
      });
    }
  });

  it("keeps both filters when they are composed", () => {
    const address = withResolved(withContractScope("/en/escalations", 7), true);
    expect(address).toBe(`/en/escalations?${CONTRACT_PARAM}=7&${RESOLVED_PARAM}=1`);
    const query = queryOf(address);
    expect(readContractScope(query)).toEqual({ kind: "contract", id: 7 });
    expect(readResolved(query)).toBe(true);
  });

  it("round-trips the absence of a scope", () => {
    expect(readContractScope(queryOf(withContractScope("/en/agent", undefined)))).toEqual({
      kind: "none",
    });
    expect(readResolved(queryOf(withResolved("/en/escalations", false)))).toBe(false);
  });
});

describe("contractIdFromInput", () => {
  it("accepts an id with the whitespace a typed box collects", () => {
    expect(contractIdFromInput("12")).toBe(12);
    expect(contractIdFromInput(" 12 ")).toBe(12);
  });

  it("refuses everything the parser would call malformed", () => {
    // Only text that is malformed *before* trimming. The box trims and the
    // address does not, so `" 12 "` is the one input the two paths read
    // differently — deliberately, and tested on its own below.
    const malformedOnceTrimmed = MALFORMED.filter((raw) => raw.trim() === raw);
    for (const raw of ["", "   ", ...malformedOnceTrimmed]) {
      expect(contractIdFromInput(raw), `raw=${raw}`).toBeUndefined();
    }
  });

  it("forgives the surrounding whitespace the parser would not", () => {
    // A typed id is a draft in a box; a query string is an address someone may
    // have pasted. Only the box gets to trim, so this difference is stated here
    // rather than left for the two paths to drift into.
    expect(contractIdFromInput(" 012 ")).toBe(12);
    expect(contractIdFromInput(" 0 ")).toBeUndefined();
    expect(contractIdFromInput(" 1 ")).toBe(1);
    expect(readContractScope(params({ [CONTRACT_PARAM]: " 1 " }))).toEqual({
      kind: "invalid",
      raw: " 1 ",
    });
  });

  it("agrees with the parser on the same text", () => {
    const candidates = ["", "   ", "0", "1", "12", "012", "12abc", " 12 ", "9007199254740992"];
    for (const raw of candidates) {
      const typed = contractIdFromInput(raw);
      const scope = readContractScope(params({ [CONTRACT_PARAM]: raw.trim() }));
      // The way in cannot offer an id the way back out would then refuse.
      expect(typed, `raw=${raw}`).toBe(scope.kind === "contract" ? scope.id : undefined);
    }
  });
});

describe("inputNamesAContract", () => {
  it("keeps the submit button live for an empty box or a whole number", () => {
    for (const raw of ["", "   ", "1", "12", " 12 ", "07", " 012 "]) {
      expect(inputNamesAContract(raw), `raw=${raw}`).toBe(true);
    }
  });

  it("disables it as soon as the box holds no usable id", () => {
    for (const raw of ["0", "00", "abc", "12abc", "1.5", "9007199254740992"]) {
      expect(inputNamesAContract(raw), `raw=${raw}`).toBe(false);
    }
  });
});
