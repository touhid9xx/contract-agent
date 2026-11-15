"""The alert rules against the code that writes the series they read, compared instead of trusted.

``ops/prometheus/rules.yml`` is validated twice and compared zero times. ``promtool check rules``
proves the file is a well-formed rules file. ``promtool test rules`` drives each expression with
synthetic series and proves the arithmetic: that ``> 0`` fires on ``1`` and stays quiet on ``0``,
and that the ``outcome="relay_failed"`` matcher ignores the explained parks. Neither of them reads
the application. The test file feeds its own label values — it *is* the authority on what a scrape
body says — so a renamed token passes both.

That gap has a specific shape, and it is the shape ``D10-122`` found in the worker wiring: a name
written down in two places, each validated in a compartment, neither compared with the other. Here
the two places are ``app/core/metrics.py``, where ``OUTBOX_PARK_RELAY_FAILED`` is spelled once and
every writer and every unit test reads that constant, and ``rules.yml``, which spells
``"relay_failed"`` out as a string. Rename the constant's *value* to ``"relay_error"`` and:

* ``ruff``, ``mypy`` and ``bandit`` are unaffected;
* every test that asserts a park was counted still passes, because they all read the constant;
* ``promtool check rules`` still passes, because the file is still well-formed;
* ``promtool test rules`` still passes, because its synthetic series still says ``relay_failed``.

And the shipped alert now matches a label value nothing publishes. It cannot fire. It is not noisy
— it is silent, which is indistinguishable from "the relay delivered everything it was given". The
operator finds out when the thing the alert was for happens.

So nothing is restated here, everything is derived: the families come from ``REGISTRY``, the values
that can appear in a label come from the *call sites* that write them (read out of the AST of
``app/``), and the rules come from the shipped file. No metric name and no token appears in this
file as a literal.

**What this does not cover, stated rather than implied.** It does not check the expressions
themselves — the comparison, the threshold, the window, ``for:``, or the annotations — because
``rules.test.yml`` already pins the arithmetic against real evaluator behaviour rather than against
a second copy of my opinion. It does not read ``smoke-rules.yml``, which exists to prove the
delivery path and is not shipped to a deployment. It cannot tell whether a rule's series is ever
non-zero: an alert on a counter that only a code path nobody calls can increment is well-formed,
correctly named and permanently quiet, and no static check can see that. And it says nothing about
whether the Prometheus that loads the file is running.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

from app.core import metrics
from app.db.enums import NotificationStatus

ROOT = Path(__file__).resolve().parents[2]
RULES = ROOT / "ops" / "prometheus" / "rules.yml"
RULE_TESTS = ROOT / "ops" / "prometheus" / "rules.test.yml"
APP = ROOT / "backend" / "app"

#: ``- alert: Name`` — the one line that starts a rule. Everything the rule says lives in the
#: block that follows it, up to the next heading or the end of the file.
ALERT_HEADING = re.compile(r"(?m)^\s*-\s+alert:\s*(\S+)\s*$")

#: ``expr: increase(escalation_email_failed_total[1h]) > 0`` — the whole expression, on one line
#: in this file, which is why a line-anchored reader is enough and a PromQL parser is not.
EXPRESSION = re.compile(r"(?m)^\s+expr:\s*(.+?)\s*$")

#: A series read by an expression, in either of the two shapes this file uses: ``name{...}`` for
#: a label matcher and ``name[...]`` for a range selector. PromQL functions are followed by ``(``
#: and so cannot match, which is what keeps ``increase`` out of the family names.
SERIES_READ = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*[\[\{]")

#: ``outcome="relay_failed"`` — a label matcher, keyed by label name.
MATCHER = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"([^"]*)"')

#: ``alertname: OutboxEventsParked`` — how a case in ``rules.test.yml`` says which rule it drives.
ALERTNAME = re.compile(r"(?m)^\s+alertname:\s*(\S+)\s*$")

#: ``exp_labels:`` and the map beneath it. The indentation of the key is captured so the map ends
#: where the indentation drops, which is how a nested block is read without a YAML parser the test
#: job would then have to install (``test_readme`` and ``test_worker_wiring`` take the same line).
EXPECTED_LABELS = re.compile(r"(?m)^(\s+)(?:-\s+)?exp_labels:\s*$")
LABEL_LINE = re.compile(r"(?m)^\s+([a-z][a-z0-9_]*):\s*(\S+)\s*$")

#: The calls that write a sample. ``set``/``inc``/``dec`` on a gauge, ``inc``/``observe`` on a
#: counter and a histogram; all four take label values as keyword arguments.
WRITER_CALLS = frozenset({"set", "inc", "dec", "observe"})


@dataclass(frozen=True)
class _Rule:
    """One rule from ``rules.yml``, reduced to what can be compared with the code."""

    name: str
    expression: str
    series: tuple[str, ...]
    matchers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Published:
    """What the application can put in a label, and where it typed a token instead of naming it."""

    values: dict[tuple[str, str], frozenset[str]]
    literals: dict[tuple[str, str], tuple[str, ...]]


def _rules() -> list[_Rule]:
    """Every rule in the shipped file, with the families it reads and the labels it matches."""

    text = RULES.read_text(encoding="utf-8")
    headings = list(ALERT_HEADING.finditer(text))
    assert headings, (
        f"{RULES.name} declares no `- alert:` rule at all. An alerting stack with no rule pages "
        "nobody, and every check in this file would pass vacuously"
    )

    rules: list[_Rule] = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[heading.end() : end]
        expression = EXPRESSION.search(block)
        assert expression is not None, (
            f"{RULES.name} declares `{heading.group(1)}` with no `expr:`. `promtool check rules` "
            "accepts a rule body with no expression and Prometheus then evaluates nothing under "
            "that name, so the alert is a name and a threshold and no query"
        )
        expr = expression.group(1)
        rules.append(
            _Rule(
                name=heading.group(1),
                expression=expr,
                series=tuple(SERIES_READ.findall(expr)),
                matchers=tuple(MATCHER.findall(expr)),
            )
        )
    return rules


def _declared() -> dict[str, metrics._Family]:
    """Family name -> the family object, out of the registry the scrape route renders."""

    return {family.name: family for family in metrics.REGISTRY}


def _token_of(node: ast.expr) -> str | None:
    """The label value a keyword argument resolves to, or ``None`` when it is computed.

    Two shapes resolve. A named token — ``outcome=OUTBOX_PARK_RELAY_FAILED`` — is read out of
    :mod:`app.core.metrics`, which is where it is spelled once. A bare string literal resolves too,
    and is reported separately by :func:`_published`, because it is the spelling that makes the
    shipped rule and the writer disagree without either of them changing.

    Anything else is a value this tree computes: a channel from the sender registry, a loop
    variable, a status read off a row. Those are deliberately unresolved. No rule may match on one
    of them, because the set of values cannot be read off the call site — and this file's whole
    argument is that a matcher must be compared with something a reader can find.
    """

    if isinstance(node, ast.Name):
        token = getattr(metrics, node.id, None)
        return token if isinstance(token, str) else None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _published() -> _Published:
    """What ``app/`` can write into each family's labels, read from the calls that write them."""

    families = {
        name: family
        for name, family in vars(metrics).items()
        if isinstance(family, metrics._Family)
    }
    values: dict[tuple[str, str], set[str]] = {}
    literals: dict[tuple[str, str], list[str]] = {}

    for path in sorted(APP.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            if not isinstance(target, ast.Attribute) or target.attr not in WRITER_CALLS:
                continue
            owner = target.value
            if not isinstance(owner, ast.Name) or owner.id not in families:
                continue
            family = families[owner.id].name
            for keyword in node.keywords:
                if keyword.arg is None:
                    continue  # ``**labels``: the family's own label check owns that path
                token = _token_of(keyword.value)
                if token is None:
                    continue
                values.setdefault((family, keyword.arg), set()).add(token)
                if isinstance(keyword.value, ast.Constant):
                    where = f"{path.relative_to(ROOT)}:{node.lineno}"
                    literals.setdefault((family, keyword.arg), []).append(
                        f"{where} passes {keyword.arg}={token!r}"
                    )

    return _Published(
        values={key: frozenset(found) for key, found in values.items()},
        literals={key: tuple(found) for key, found in literals.items()},
    )


def _expected_label_values(alertname: str) -> dict[str, set[str]]:
    """Label -> the values ``rules.test.yml`` says Prometheus will put there for that alert."""

    text = RULE_TESTS.read_text(encoding="utf-8")
    cases = list(ALERTNAME.finditer(text))
    expected: dict[str, set[str]] = {}

    for index, case in enumerate(cases):
        if case.group(1) != alertname:
            continue
        end = cases[index + 1].start() if index + 1 < len(cases) else len(text)
        block = text[case.end() : end]
        for keys in EXPECTED_LABELS.finditer(block):
            indent = len(keys.group(1))
            # `keys.end()` is the end of the `exp_labels:` line, so the remainder starts on the
            # newline that ends it; the map's own lines start after that. Reading the first
            # element of `splitlines()` as a member of the map is what made this reader find
            # nothing at all and call the file unproven.
            for line in block[keys.end() :].partition("\n")[2].splitlines():
                # The map ends where the indentation drops, and at the next list item: a
                # ``- exp_annotations:`` under the same alert is a sibling of this map, not
                # a deeper part of it.
                if not line.strip() or not line.startswith(" " * (indent + 1)):
                    break
                if line.lstrip().startswith("- "):
                    break
                label = LABEL_LINE.match(line)
                if label is not None:
                    expected.setdefault(label.group(1), set()).add(label.group(2).strip("\"'"))
    return expected


def test_every_series_a_rule_reads_is_a_family_the_code_declares() -> None:
    """A rule on a family nothing declares is a query over an empty name."""

    declared = _declared()
    for rule in _rules():
        assert rule.series, (
            f"{RULES.name}'s `{rule.name}` names no metric family in `{rule.expression}`. This "
            "reader only sees `name{...}` and `name[...]`, so either the expression reaches its "
            "series some other way (`absent(...)`, a recording rule) and the reader needs widening "
            "before the rule can be checked, or the family name lost its label matcher and the "
            "rule now scans every series in the deployment"
        )
        unknown = sorted(set(rule.series) - set(declared))
        assert not unknown, (
            f"{RULES.name}'s `{rule.name}` reads {unknown}, which no family in "
            f"app/core/metrics.py declares. The rule is well-formed and permanently quiet: "
            f"Prometheus evaluates a name nothing publishes to no data. Declared: "
            f"{sorted(declared)}"
        )


def test_every_label_a_rule_matches_is_a_label_its_family_carries() -> None:
    """``{outcome=...}`` on a family with no ``outcome`` label matches nothing, silently."""

    declared = _declared()
    for rule in _rules():
        for label, _ in rule.matchers:
            carries = [declared[name].labelnames for name in rule.series if name in declared]
            assert any(label in labels for labels in carries), (
                f"{RULES.name}'s `{rule.name}` matches on `{label}=`, but "
                f"{sorted(set(rule.series) & set(declared))} carries "
                f"{sorted({name for labels in carries for name in labels})}. A matcher on a label "
                "the family never sets selects no series, and the alert cannot fire for a reason "
                "no test that feeds its own labels can see"
            )


def test_every_label_value_a_rule_matches_is_one_the_code_publishes() -> None:
    """The one comparison neither `promtool` mode makes: the file's token against the writer's."""

    published = _published()
    declared = _declared()

    for rule in _rules():
        for label, value in rule.matchers:
            for name in rule.series:
                if name not in declared or label not in declared[name].labelnames:
                    continue
                can_write = published.values.get((name, label), frozenset())
                assert can_write, (
                    f"app/ never passes `{label}=` to {name}, so `{RULES.name}`'s `{rule.name}` "
                    f"matches a label nothing writes. Either the writer stopped labelling its "
                    "samples — in which case the family is unqueryable — or it computes the value "
                    "instead of naming a token, which no rule may match on"
                )
                assert value in can_write, (
                    f"{RULES.name}'s `{rule.name}` matches {label}={value!r}, but the code that "
                    f"writes {name} can only put {sorted(can_write)} there. This is the failure "
                    "with no symptom: the rule is well-formed, promtool's own test still passes "
                    "because it feeds its own label values, and the shipped alert matches a series "
                    "no process publishes. It cannot fire, and it cannot look different from a "
                    "deployment where nothing is wrong"
                )


def test_every_label_value_the_rule_test_expects_is_one_the_code_publishes() -> None:
    """The same comparison, applied to what `promtool test rules` asserts Prometheus will emit."""

    published = _published()
    declared = _declared()

    checked = 0
    for rule in _rules():
        for label, expected in _expected_label_values(rule.name).items():
            families = [name for name in rule.series if name in declared]
            if not any(label in declared[name].labelnames for name in families):
                continue  # `severity`, `instance`: not ours, so not this file's business
            can_write = published.values.get((families[0], label), frozenset())
            for value in sorted(expected):
                checked += 1
                assert value in can_write, (
                    f"{RULE_TESTS.name} expects `{rule.name}` to fire with {label}={value!r}, but "
                    f"app/ can only put {sorted(can_write)} in {families[0]}'s {label}. The case "
                    "proves the rule works for a label value no code produces, so it is green and "
                    "the shipped alert is dead — the two facts the file exists to keep apart"
                )

    assert checked, (
        f"{RULE_TESTS.name} expects no label value on any of this tree's own families. Every case "
        "asserts either `exp_alerts: []` or none at all, so nothing here proves the alert can fire"
    )


def test_every_rule_has_a_promtool_case_and_every_case_names_a_rule() -> None:
    """A fourth rule can be added to the file and no test will ever evaluate it."""

    declared = {rule.name for rule in _rules()}
    driven = set(ALERTNAME.findall(RULE_TESTS.read_text(encoding="utf-8")))

    unproven = sorted(declared - driven)
    orphaned = sorted(driven - declared)
    assert not (unproven + orphaned), (
        f"{RULES.name} declares {sorted(declared)} and {RULE_TESTS.name} drives {sorted(driven)}. "
        f"Unproven: {unproven} — Prometheus will evaluate that rule in a deployment and nothing "
        "has ever evaluated it here, so its first run is the one that matters. "
        f"Orphaned: {orphaned} — a case asserting about an alert that no longer exists, which "
        "promtool reports as a failure only if the name is gone, never as a stale expectation"
    )


def test_the_writers_name_their_tokens_instead_of_typing_them() -> None:
    """A literal at a call site is a second spelling, and the rules file is the third."""

    published = _published()
    matched = {
        (name, label) for rule in _rules() for label, _ in rule.matchers for name in rule.series
    }

    typed = [line for key in sorted(matched) for line in published.literals.get(key, ())]
    assert not typed, (
        "These call sites write a label value as a string literal rather than naming the token in "
        "app/core/metrics.py:\n  " + "\n  ".join(typed) + "\n"
        "A literal there is a second spelling that no unit test compares with the first, so "
        "renaming the token leaves this one behind and the metric moves to a label value the "
        "alert does not match. Pass the constant"
    )


def test_the_undelivered_tokens_are_the_statuses_the_database_stores() -> None:
    """The docstring in `metrics.py` claims these are the stored values. Prose is not a check."""

    assert NotificationStatus.FAILED.value == metrics.NOTIFICATION_UNDELIVERED_FAILED, (
        "app/core/metrics.py's `NOTIFICATION_UNDELIVERED_FAILED` no longer equals "
        f"NotificationStatus.FAILED.value ({NotificationStatus.FAILED.value!r}), so an operator "
        "reading the gauge beside a row in the database has two vocabularies to translate between "
        "for one state — which is the reason that module gives for naming them this way"
    )
    assert NotificationStatus.SUPPRESSED.value == metrics.NOTIFICATION_UNDELIVERED_SUPPRESSED, (
        "app/core/metrics.py's `NOTIFICATION_UNDELIVERED_SUPPRESSED` no longer equals "
        f"NotificationStatus.SUPPRESSED.value ({NotificationStatus.SUPPRESSED.value!r})"
    )
