"""The two facts `README.md` states about the tree, checked against the tree.

`README.md` is the front door, and until this file it was the one document in the
repository that nothing read. Two of its present-tense claims had gone false: it said
`.github/workflows/ci.yml` runs **seven jobs** when the workflow defines nine — the
`observability` and `image` jobs were added without it — and it said the migration chain
was at head **`0007`** when `D10-120` had already shipped `0008`. Neither sentence had a
second copy in the tree to disagree with it, which is the whole of why both survived.

A test rather than a more careful edit, because that edit had already been made once.
`D10-114` corrected the same README sentence from `0006` to `0007` and it was wrong
again one slice later; `frontend/src/app/screens.test.ts` exists for the same failure on
the same file. `D10-69` states the rule the repo follows from it: a number a person
re-counts is a number that drifts, and a number written into the tree is not.

So nothing is re-counted here and nothing is written down twice. The job count comes
from `ci.yml`'s own `jobs:` block and the revision comes from Alembic's script directory;
the README is only the claim under test.

**What this does not cover, stated rather than implied.** It checks the *count* of jobs
and the *head revision*, not the words around them. A job could be renamed, or split in
two and another merged into it, and the count stay nine with the README describing the
old shape. Catching that would mean writing all nine job ids into the README — the
duplication this file exists to avoid — so it is left uncaught rather than paid for. The
same holds for every other claim in the README: that the frontend has no container, that
`P2-F` has no code, that every screen has a spec. `screens.test.ts` checks the last of
those independently, and nothing checks the rest.
"""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"

#: Spelled-out counts, because the README writes prose. Digits are accepted too, so the
#: claim stays checkable if someone prefers "**9 jobs**".
NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}

#: The two claims, quoted from the README with the variable part captured. Both anchors
#: are deliberately short: they are the *smallest* phrase a rewording is still likely to
#: keep, and a missing anchor fails loudly rather than silently checking nothing.
JOBS_CLAIM = re.compile(r"runs \*\*(?P<count>[A-Za-z]+|\d+)\s+jobs\*\*")
HEAD_CLAIM = re.compile(r"currently at head `(?P<head>\d+)`")


def _count_of(word: str) -> int:
    """The integer a count word or digit string denotes."""

    if word.isdigit():
        return int(word)
    try:
        return NUMBER_WORDS[word.lower()]
    except KeyError:
        raise AssertionError(
            f"README.md counts the jobs as {word!r}, which this test cannot read. Spell it "
            "out (nine) or use digits (9), or extend NUMBER_WORDS if a larger pipeline "
            "makes that necessary"
        ) from None


def _job_ids() -> list[str]:
    """Every job id `ci.yml` defines, in file order, read from its own ``jobs:`` block."""

    text = WORKFLOW.read_text(encoding="utf-8")
    jobs = re.search(r"^jobs:\s*$", text, re.M)
    assert jobs is not None, f"{WORKFLOW} has no `jobs:` block to count"
    blocks = re.split(r"(?m)^  (?=[a-z0-9_-]+:\s*$)", text[jobs.end() :])[1:]
    return [block.split(":", 1)[0] for block in blocks]


def _migration_head() -> str:
    """The single revision the migration chain ends at, per Alembic itself.

    ``ScriptDirectory`` reads the scripts and follows ``down_revision``; it does not open
    a database and does not execute ``migrations/env.py``, so this stays a pure read of
    the tree. The fixture-free form matters: ``test_migrations`` proves the *database*
    reaches this head by applying the chain, which is a different claim.
    """

    config = AlembicConfig()
    config.set_main_option("script_location", str(ROOT / "backend" / "migrations"))
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None, "the migration scripts declare no head at all"
    return head


def test_the_readme_counts_the_jobs_the_workflow_defines() -> None:
    """Seven became nine when `observability` and `image` landed; nothing said so."""

    claim = JOBS_CLAIM.search(README.read_text(encoding="utf-8"))
    assert claim is not None, (
        "README.md no longer says how many jobs ci.yml runs. Either keep the sentence "
        "checkable — `runs **nine jobs**` — or delete this test with it, because a claim "
        "this file cannot find is a claim it silently stops checking"
    )

    jobs = _job_ids()
    assert len(jobs) > 1, (
        f"parsed {jobs} out of {WORKFLOW.name}: that is the parser being wrong, not the "
        "README, and the comparison below would be vacuous"
    )
    assert _count_of(claim.group("count")) == len(jobs), (
        f"README.md says ci.yml runs {claim.group('count')} jobs; it defines {len(jobs)} — "
        f"{', '.join(jobs)}. Update the sentence to match, or the count is a number the "
        "next reader will trust and the next slice will outdate"
    )


def test_the_readme_names_the_head_the_migration_chain_is_at() -> None:
    """The README said `0006` for several slices, then `0007` for one."""

    claim = HEAD_CLAIM.search(README.read_text(encoding="utf-8"))
    assert claim is not None, (
        "README.md no longer says which revision the migration chain ends at. Either keep "
        "the sentence checkable — the words `currently at head` followed by the revision "
        "in backticks — or delete this test with it"
    )

    head = _migration_head()
    assert claim.group("head") == head, (
        f"README.md says the chain is at head {claim.group('head')!r}; "
        f"backend/migrations/versions ends at {head!r}. Name the new revision, or drop the "
        "claim and let `test_migrations` be the only place the head is stated"
    )
