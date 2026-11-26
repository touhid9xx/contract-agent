"""Guards on the Grafana dashboards this repository ships (D10-155).

The dashboards are files in `ops/grafana/`, and Grafana loads them without complaint when
they are wrong. A panel naming a series that no longer exists renders "No data"; a panel
naming a datasource uid nobody provisioned renders "No data" too; a panel matching a label
value no writer can produce renders "No data" and waits for ever. None of the three is an
error anywhere in the stack: `/api/health` answers 200, the container is healthy, and the
screen reads exactly like a quiet deployment.

This file is the same instrument `test_alert_rules.py` is, pointed at the other artefact
that names series and label values. The rules file was the first place a matcher could
disagree with `app/core/metrics.py` without either side changing; the dashboards are the
second. It reuses that module's readers rather than writing a second AST walk — `_declared`
is the registry as name -> family, and `_published` is what `app/` can actually write into
each label, read off the call sites that write them.

What this file cannot say: that a dashboard renders. Every assertion here is about the
text of the JSON against the text of the code. The tag that runs is proved by the container
smoke in `.github/workflows/ci.yml`, which asks this Grafana whether it serves the uid.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from app.core import metrics
from tests.test_alert_rules import MATCHER, _declared, _published

ROOT = Path(__file__).resolve().parents[2]
GRAFANA = ROOT / "ops" / "grafana"
DATASOURCES = GRAFANA / "provisioning" / "datasources" / "prometheus.yml"
PROVIDERS = GRAFANA / "provisioning" / "dashboards" / "dashboards.yml"
DASHBOARDS = GRAFANA / "dashboards"
COMPOSE = ROOT / "docker-compose.yml"

#: PromQL words a panel may use that are not series. Deliberately short: a panel that needs
#: a function not listed here should add it below, in the same change that justifies it, so
#: a typo'd series name is never excused by a broad allowlist.
PROMQL_WORDS = frozenset(
    {
        "avg",
        "bool",
        "by",
        "count",
        "delta",
        "histogram_quantile",
        "ignoring",
        "increase",
        "max",
        "min",
        "offset",
        "on",
        "rate",
        "sum",
        "topk",
        "without",
    }
)

#: Grafana interpolates this before Prometheus sees the query, so it is syntax, not a series.
GRAFANA_VARIABLES = frozenset({"__rate_interval", "__interval", "__range"})

#: Labels Prometheus attaches to every scraped sample. They are never declared in
#: `metrics.py`, because they describe the scrape rather than the metric.
TARGET_LABELS = frozenset({"job", "instance"})

#: Series Prometheus publishes about itself. A dashboard that plots `up` is making a claim
#: about scraping, not about this code, so the family checks below have nothing to compare.
PROMETHEUS_OWN = frozenset({"up"})

#: The label a histogram bucket carries. Prometheus adds it; `metrics.py` does not.
BUCKET_LABEL = "le"

#: The suffixes an exposition of one histogram family expands into.
HISTOGRAM_SUFFIXES = ("_bucket", "_count", "_sum")

QUOTED = re.compile(r'"[^"]*"')
#: A range selector or subquery window — `[1h]`, `[5m]`, `[$__rate_interval]`. Stripped before
#: identifiers are read, because `[1h]` contains `h` and `[5m]` contains `m`, and either would
#: be reported as an undeclared series by a scan that only removed quoted strings.
WINDOWED = re.compile(r"\[[^\]]*\]")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DURATION = re.compile(r"now-(\d+)([smhd])")
SECONDS_PER_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}
RETENTION = re.compile(r"--storage\.tsdb\.retention\.time=(\d+)([smhd])")


def _datasources() -> dict[str, str]:
    """Provisioned datasource name -> uid, read from the file Grafana is handed."""

    document = yaml.safe_load(DATASOURCES.read_text(encoding="utf-8"))
    return {entry["name"]: entry["uid"] for entry in document["datasources"]}


def _provider() -> dict[str, Any]:
    """The single file provider that loads `ops/grafana/dashboards`."""

    document = yaml.safe_load(PROVIDERS.read_text(encoding="utf-8"))
    providers = document["providers"]
    assert len(providers) == 1, f"expected one dashboard provider, found {len(providers)}"
    return dict(providers[0])


def _dashboards() -> list[dict[str, Any]]:
    """Every provisioned dashboard, parsed. A malformed file fails here rather than in Grafana."""

    found = [
        json.loads(path.read_text(encoding="utf-8")) for path in sorted(DASHBOARDS.glob("*.json"))
    ]
    return found


def _panels(dashboard: dict[str, Any]) -> list[dict[str, Any]]:
    """Panels, including any nested inside a row.

    Rows are not used by the dashboard shipped today, and that is exactly why this walks
    them: a nested panel must not become the one place a query escapes these checks.
    """

    panels = list(dashboard.get("panels", []))
    return panels + [child for panel in panels for child in panel.get("panels", [])]


def _targets(dashboard: dict[str, Any]) -> list[tuple[str, str]]:
    """(panel title, expression) for every target carrying an expression."""

    found: list[tuple[str, str]] = []
    for panel in _panels(dashboard):
        title = str(panel.get("title", "<untitled>"))
        for target in panel.get("targets", []):
            expression = target.get("expr")
            if expression:
                found.append((title, str(expression)))
    return found


def _identifiers(expression: str) -> set[str]:
    """Every identifier in an expression with strings and windows removed first.

    Both subtractions matter. Without the quoted one, `up{job="workers"}` yields `workers` and a
    label value would be reported as an undeclared series; without the windowed one, `[1h]`
    yields `h` and so does `[5m]` its `m`.
    """

    return set(IDENTIFIER.findall(WINDOWED.sub("[]", QUOTED.sub('""', expression))))


def _histogram_families() -> set[str]:
    """Families that expand into `_bucket`/`_sum`/`_count`.

    Asked as "does it have `observe`" rather than by the private class name, because
    `observe` is the method that makes it one and is already the marker `test_alert_rules`
    uses to find the writers.
    """

    return {family.name for family in metrics.REGISTRY if hasattr(family, "observe")}


def _known_series() -> set[str]:
    """Every series name the registry can put on the wire, histogram parts expanded."""

    names = set(_declared())
    for name in _histogram_families():
        names |= {name + suffix for suffix in HISTOGRAM_SUFFIXES}
    return names


def _label_names() -> set[str]:
    """Every label a query may group by or match on: declared ones plus the scrape's own."""

    names = set(TARGET_LABELS) | {BUCKET_LABEL}
    for family in metrics.REGISTRY:
        names |= set(family.labelnames)
    return names


def _base_family(identifier: str) -> str | None:
    """The declared family a series name belongs to, or ``None`` when it is not one."""

    declared = _declared()
    if identifier in declared:
        return identifier
    for suffix in HISTOGRAM_SUFFIXES:
        base = identifier[: -len(suffix)] if identifier.endswith(suffix) else ""
        if base in declared:
            return base
    return None


def test_every_dashboard_is_a_file_with_a_uid_a_title_and_panels() -> None:
    dashboards = _dashboards()
    assert dashboards, (
        f"{DASHBOARDS.relative_to(ROOT)} holds no dashboard, which would make every check "
        "below pass by measuring nothing"
    )

    uids = [dashboard["uid"] for dashboard in dashboards]
    assert len(uids) == len(set(uids)), f"two provisioned dashboards share a uid: {uids}"

    for dashboard in dashboards:
        where = dashboard["uid"]
        assert dashboard["title"].strip(), f"{where} has no title"
        assert _panels(dashboard), f"{where} has no panels"
        # The file is the record. An `editable` dashboard invites an edit that is lost on the
        # next provisioner tick, which teaches an operator the screen is not the config.
        assert dashboard["editable"] is False, f"{where} is editable, so the UI can drift from ops/"
        assert "templating" in dashboard, f"{where} declares no templating block"


def test_every_panel_queries_the_datasource_the_provisioning_defines() -> None:
    """The quietest failure in the file: a uid nothing provisioned renders every panel empty.

    Grafana generates a uid per datasource when the provisioning omits one, so a dashboard
    naming a literal uid and a datasource file that does not state that uid disagree on
    every boot — with no error anywhere, and every panel reading "No data".
    """

    provisioned = set(_datasources().values())
    assert provisioned, "the datasource provisioning file defines no uid to compare against"

    wrong: list[str] = []
    for dashboard in _dashboards():
        for panel in _panels(dashboard):
            entries: list[dict[str, Any]] = [panel, *panel.get("targets", [])]
            for entry in entries:
                datasource = entry.get("datasource") or {}
                uid = datasource.get("uid")
                if not uid:
                    # Falls back to the datasource marked `isDefault`, which the provisioning
                    # file states, so there is still exactly one intended target.
                    continue
                if uid not in provisioned:
                    where = f"{dashboard['uid']} / {panel.get('title', '<untitled>')}"
                    wrong.append(f"{where} asks for datasource {uid!r}")

    assert not wrong, (
        "these queries name a datasource uid the provisioning file never defines, so every "
        f"panel renders 'No data' while the container reports healthy: {wrong}. Provisioned "
        f"uids are {sorted(provisioned)} in {DATASOURCES.relative_to(ROOT)}"
    )


def test_every_series_a_panel_reads_is_one_the_code_declares_or_prometheus_owns() -> None:
    allowed = PROMQL_WORDS | GRAFANA_VARIABLES | _label_names()
    known = _known_series()

    unknown: dict[str, list[str]] = {}
    for dashboard in _dashboards():
        for title, expression in _targets(dashboard):
            for identifier in sorted(_identifiers(expression)):
                if identifier in allowed or identifier in known or identifier in PROMETHEUS_OWN:
                    continue
                unknown.setdefault(identifier, []).append(f"{dashboard['uid']} / {title}")

    assert not unknown, (
        "these identifiers are neither a series `app/core/metrics.py` declares, a PromQL "
        f"word, nor a label: {unknown}. A renamed family would leave a panel reading 'No data' "
        "for ever, which looks exactly like a quiet deployment."
    )


def test_every_label_a_panel_matches_is_one_its_family_carries() -> None:
    declared = _declared()
    histograms = _histogram_families()

    errors: list[str] = []
    for dashboard in _dashboards():
        for title, expression in _targets(dashboard):
            families = {
                base for base in (_base_family(name) for name in _identifiers(expression)) if base
            }
            if len(families) != 1:
                # A multi-family or Prometheus-only expression is not skipped for convenience:
                # there is no single family whose label set would be the thing to compare with,
                # and inventing a union would excuse a label no family carries. The shipped
                # dashboard keeps one family per panel so this check always has an answer.
                continue
            family = declared[families.pop()]
            allowed = set(family.labelnames) | TARGET_LABELS
            if family.name in histograms:
                allowed.add(BUCKET_LABEL)
            for label, _value in MATCHER.findall(expression):
                if label not in allowed:
                    errors.append(
                        f"{dashboard['uid']} / {title} matches on {label!r}, which "
                        f"{family.name} does not carry (it has {list(family.labelnames)})"
                    )

    assert not errors, f"{errors}"


def test_every_label_value_a_panel_matches_is_one_the_code_publishes() -> None:
    """The same argument the alert rules make: a matcher must name a token a writer emits.

    `_published` reads the writer call sites, so a value that only exists in a dashboard or a
    rule file is caught here — that is the shape of a panel waiting for a series that will
    never arrive.
    """

    published = _published()
    declared = _declared()

    errors: list[str] = []
    for dashboard in _dashboards():
        for title, expression in _targets(dashboard):
            families = {
                base for base in (_base_family(name) for name in _identifiers(expression)) if base
            }
            if len(families) != 1:
                continue
            family = declared[families.pop()]
            for label, value in MATCHER.findall(expression):
                if label not in family.labelnames:
                    continue  # the label check above owns that report
                could_write = published.values.get((family.name, label), frozenset())
                if value not in could_write:
                    errors.append(
                        f"{dashboard['uid']} / {title} matches {label}={value!r}, but no "
                        f"writer in app/ passes that token (it passes {sorted(could_write)})"
                    )

    assert not errors, f"{errors}"


def test_every_family_the_code_declares_has_a_panel() -> None:
    """A family nobody plots is a series written, scraped, and read by nobody.

    Neither vacuous nor a style rule: `metrics.py` argues about what each family is *for*, and
    the cheapest way for that argument to go stale is for the panel to be deleted while the
    counter keeps counting. A family meant to have no panel belongs in the exemption below
    with the reason, which is a sentence somebody has to write.
    """

    deliberately_unplotted: dict[str, str] = {}

    plotted: set[str] = set()
    for dashboard in _dashboards():
        for _title, expression in _targets(dashboard):
            plotted |= {
                base for base in (_base_family(name) for name in _identifiers(expression)) if base
            }

    missing = set(_declared()) - plotted - set(deliberately_unplotted)
    assert not missing, (
        f"these declared families are on no panel: {sorted(missing)}. Add a panel, or add the "
        "family to `deliberately_unplotted` in this test with the reason it has none."
    )

    stale = set(deliberately_unplotted) - set(_declared())
    assert not stale, (
        f"`deliberately_unplotted` names families that no longer exist: {sorted(stale)}"
    )


def test_the_dashboard_window_fits_the_retention_the_stack_sets() -> None:
    """A dashboard asking for longer than the TSDB keeps shows a gap that reads like an outage.

    Both numbers are read rather than restated: the window from the dashboard JSON, the
    retention from the Prometheus command line in `docker-compose.yml`. Changing either one
    alone fails here instead of on a screen nobody is looking at.
    """

    compose = COMPOSE.read_text(encoding="utf-8")
    retention_match = RETENTION.search(compose)
    assert retention_match, "docker-compose.yml no longer states a TSDB retention for Prometheus"
    retention = int(retention_match.group(1)) * SECONDS_PER_UNIT[retention_match.group(2)]

    for dashboard in _dashboards():
        window = str(dashboard["time"]["from"])
        matched = DURATION.fullmatch(window)
        assert matched, (
            f"{dashboard['uid']} asks for {window!r}, which is not a `now-<n><unit>` window this "
            "check can compare with the retention"
        )
        seconds = int(matched.group(1)) * SECONDS_PER_UNIT[matched.group(2)]
        assert seconds <= retention, (
            f"{dashboard['uid']} asks for {window} but Prometheus keeps only "
            f"{retention_match.group(0).split('=')[-1]}, so the left of every panel is empty"
        )


def test_the_provider_reads_the_directory_the_compose_file_mounts() -> None:
    """The provider's `path` and the compose mount target have to be the same string.

    They are two files that must agree for a dashboard to load at all, and disagreeing is
    silent: Grafana provisions an empty directory happily and serves zero dashboards.
    """

    path = _provider()["options"]["path"]
    compose = COMPOSE.read_text(encoding="utf-8")

    mount = f"./ops/grafana/dashboards:{path}:ro"
    assert mount in compose, (
        f"the dashboard provider reads {path}, but docker-compose.yml does not mount "
        f"`ops/grafana/dashboards` there. Expected a volume line containing {mount!r}."
    )
