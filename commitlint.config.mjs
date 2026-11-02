/** Commit message rules. Conventional Commits, enforced by husky. */
export default {
  extends: ["@commitlint/config-conventional"],
  rules: {
    // Milestone scopes are lowercase tokens: infra, db, auth, bus, obs, web, sec…
    "scope-case": [2, "always", "lower-case"],
    "subject-case": [2, "always", ["sentence-case", "lower-case", "start-case"]],
    "header-max-length": [2, "always", 100],
    "body-max-line-length": [0, "always", "Infinity"],
  },
};
