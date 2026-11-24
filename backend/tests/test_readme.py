"""The facts `README.md` states about the tree, and the two copies of one of them.

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

**The milestone statement is the exception, and it is a different kind of check.** The
README says which Phase-2 milestones have code *twice* — as prose in the status block at
the top, and as the `GATE 3` row of the delivery table — and the two copies had already
disagreed when the third test here was written. `D10-137` amended the prose to say P2-I's
dashboard had landed and left the table reading *P2-I's new views none*, so one file said
both things on one page. Nothing compared them, which is the failure this whole file
exists for, one copy further in. The test does not decide which copy is right; it proves
they name the same milestones, and that together with the pair the table calls
unspecified they partition the range the table itself declares — so a milestone cannot
land in one statement and not the other, and a new one cannot arrive unaccounted for.

**What this does not cover, stated rather than implied.** Of the two claims checked
against the tree it reads the *count* of jobs and the *head revision*, not the words
around them. A job could be renamed, or split in two and another merged into it, and the
count stay nine with the README describing the old shape. Catching that would mean writing
all nine job ids into the README — the duplication this file exists to avoid — so it is
left uncaught rather than paid for. The same holds for every other claim in the README:
that one command brings the stack up, that `P2-K` and `P2-L` are specified nowhere, that
every screen has a spec. `screens.test.ts` checks the last of those independently, and
nothing checks the rest. The milestone test is the narrowest of the three and must not be
read as wider: it proves the paragraph and the table *agree about* a milestone, never that
the milestone has the code it is credited with — the tree is never consulted for that, and
only a test that could tell would make the claim safe to leave unguarded. What it does buy
is that a slice cannot land in one of the two statements alone: `D10-144` moved `P2-F`
from one bucket to the other, and had it been edited in the paragraph only, this file
would have failed rather than let the README describe two different repositories.
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

#: The milestone claim, read from both places the README states it. Each anchor is the
#: smallest phrase a rewording is likely to keep; a missing one fails loudly below rather
#: than silently checking nothing.
RANGE_CLAIM = re.compile(r"P2-(?P<first>[A-Z]) … P2-(?P<last>[A-Z])")
PROSE_LANDED = re.compile(
    r"(?P<count>[A-Za-z]+|\d+) of the (?P<total>[A-Za-z]+|\d+) milestones have landed "
    r"slices\*\* — (?P<list>[^—]+?) — recorded"
)
PROSE_ABSENT = re.compile(r"What has no code at all is (?P<list>.+?)\.(?:\s|$)")

#: The three states the delivery table's `GATE 3` row sorts milestones into, with a regex
#: for the phrase that introduces each. Regexes rather than literals for one reason: how
#: many milestones sit in a state changes as slices land, so a literal `have` demanded that
#: the row read `P2-F have no code at all` the moment P2-H shipped. A guard that makes the
#: README ungrammatical is a guard the next author deletes, taking the check with it.
ROW_STATES = (
    (re.compile(r"ha(?:ve|s) landed slices"), "landed"),
    (re.compile(r"ha(?:ve|s) no code at all"), "no code"),
    (re.compile(r"unspecified"), "unspecified"),
)

#: A full id (`P2-F`) or the shorthand the table uses after the first one (`-F`), each
#: optionally carrying a possessive gloss (`P2-I's new views`). The gloss is dropped.
FULL_ID = re.compile(r"P2-(?P<letter>[A-Z])(?:'s\b.*)?\Z")
SHORT_ID = re.compile(r"-(?P<letter>[A-Z])(?:'s\b.*)?\Z")

#: The words that mean "and none of them", for a milestone list that has emptied out.
NO_MILESTONES = {"nothing", "none"}


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


def _unwrapped() -> str:
    """`README.md` with its blockquote markers folded away, so a claim split across two
    `>` lines still reads as one sentence."""

    return re.sub(r"\n>\s?", " ", README.read_text(encoding="utf-8"))


def _claim(pattern: re.Pattern[str], text: str, shape: str) -> re.Match[str]:
    """The claim, or a failure naming the shape the README stopped having."""

    claim = pattern.search(text)
    assert claim is not None, (
        f"README.md no longer states the milestone position as {shape}. Either keep it "
        "checkable or delete the test that reads it, because a claim this file cannot "
        "find is a claim it silently stops checking"
    )
    return claim


def _milestone_id(token: str) -> str:
    """`P2-F`, or the table's shorthand `-F`, normalised to a full id."""

    token = token.strip()
    for pattern in (FULL_ID, SHORT_ID):
        match = pattern.match(token)
        if match:
            return f"P2-{match.group('letter')}"
    raise AssertionError(
        f"cannot read {token!r} as a milestone. Write it as `P2-F`, or as the table's short "
        "`-F`, or extend FULL_ID/SHORT_ID if the naming changes"
    )


def _milestones(listed: str) -> set[str]:
    """The ids in a comma / `and` / `/` separated list, parenthetical glosses dropped.

    The glosses matter: the prose writes `P2-F (voice and answering-machine detection) and
    P2-H (...)`, and splitting that on `and` without removing the brackets reads three
    tokens, one of which is not a milestone.

    :data:`NO_MILESTONES` is the empty set spelled out. It exists because the "no code"
    bucket empties the day the last milestone lands a slice, and a guard that could only be
    satisfied by naming an unbuilt milestone would be deleted that day — taking the
    agreement check with it, which is the failure this file exists to prevent.
    """

    listed = re.sub(r"\([^)]*\)", "", listed).replace(" and ", ", ")
    tokens = [token.strip() for chunk in listed.split(",") for token in chunk.split("/")]
    return {
        _milestone_id(token) for token in tokens if token and token.lower() not in NO_MILESTONES
    }


def _table_milestones(text: str) -> dict[str, set[str]]:
    """The three states the delivery table's `GATE 3` row sorts milestones into."""

    row = next(
        (line for line in text.splitlines() if "GATE 3" in line and "in progress" in line),
        None,
    )
    assert row is not None, (
        "README.md no longer has a `GATE 3` row saying it is in progress. Either keep the "
        "row checkable — `**GATE 3** — in progress: ...` — or delete this test with it"
    )

    found: dict[str, set[str]] = {}
    for segment in row.split(";"):
        # Everything before `in progress:` is the phase cell, and it carries the range
        # (`P2-A … P2-L`) — an id list that is not a statement about any milestone.
        segment = segment.split("in progress:", 1)[-1]
        for phrase, state in ROW_STATES:
            match = phrase.search(segment)
            if match is not None:
                found[state] = _milestones(segment[: match.start()])

    assert set(found) == {state for _, state in ROW_STATES}, (
        f"parsed {sorted(found)} out of the `GATE 3` row. It must name all three states — "
        "landed, no code, unspecified — or the comparison below is vacuous"
    )
    return found


def _declared_range(text: str) -> set[str]:
    """Every milestone the table's own `P2-A … P2-L` range covers."""

    claim = _claim(RANGE_CLAIM, text, "`P2-A … P2-L`")
    first, last = ord(claim.group("first")), ord(claim.group("last"))
    assert first <= last, f"the declared range runs backwards: {claim.group(0)!r}"
    return {f"P2-{chr(code)}" for code in range(first, last + 1)}


def test_the_readme_counts_the_milestones_it_lists() -> None:
    """`eight of the twelve` must be a count of the list beside it, not a memory of one."""

    text = _unwrapped()
    claim = _claim(
        PROSE_LANDED, text, "`N of the M milestones have landed slices** — … — recorded`"
    )
    landed = _milestones(claim.group("list"))
    declared = _declared_range(text)

    assert _count_of(claim.group("count")) == len(landed), (
        f"README.md says {claim.group('count')} milestones have landed and then lists "
        f"{len(landed)} — {', '.join(sorted(landed))}. Update the word to match the list "
        "beside it, or the number is what the next reader trusts and the next slice outdates"
    )
    assert _count_of(claim.group("total")) == len(declared), (
        f"README.md says there are {claim.group('total')} milestones; the range it declares "
        f"covers {len(declared)} — {', '.join(sorted(declared))}. Widen the range, or fix "
        "the word"
    )


def test_the_readmes_two_milestone_statements_agree() -> None:
    """`D10-137` amended the prose and left the table saying P2-I's views had no code.

    The two statements are eight hundred lines apart and nothing compared them, which is
    how one README came to say that P2-I had shipped and that it had not. This test is
    about agreement, not about truth: it cannot tell whether `P2-F` has code, only that
    both statements believe the same thing about it.
    """

    text = _unwrapped()
    claim = _claim(
        PROSE_LANDED, text, "`N of the M milestones have landed slices** — … — recorded`"
    )
    absent = _claim(PROSE_ABSENT, text, "`What has no code at all is …`")
    table = _table_milestones(text)
    declared = _declared_range(text)

    landed = _milestones(claim.group("list"))
    unbuilt = _milestones(absent.group("list"))

    assert landed == table["landed"], (
        f"the status paragraph says {', '.join(sorted(landed))} have landed; the `GATE 3` "
        f"row says {', '.join(sorted(table['landed']))}. Both are in this file, so one of "
        "them is a claim the reader will trust and the other is a contradiction"
    )
    assert unbuilt == table["no code"], (
        f"the status paragraph says {', '.join(sorted(unbuilt))} have no code; the `GATE 3` "
        f"row says {', '.join(sorted(table['no code']))}"
    )

    overlap = table["landed"] & table["no code"]
    assert not overlap, (
        f"{', '.join(sorted(overlap))} is called both landed and unbuilt by the same row"
    )

    accounted = table["landed"] | table["no code"] | table["unspecified"]
    missing, extra = declared - accounted, accounted - declared
    assert not missing, (
        f"the range says {', '.join(sorted(declared))}; the `GATE 3` row accounts for "
        f"{', '.join(sorted(accounted))} and never mentions {', '.join(sorted(missing))}. "
        "Every milestone in the range is landed, unbuilt, or unspecified — pick one"
    )
    assert not extra, (
        f"the `GATE 3` row names {', '.join(sorted(extra))}, which is outside the range the "
        "table itself declares. Widen the range, or drop the milestone from the row"
    )


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
