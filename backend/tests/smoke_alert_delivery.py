#!/usr/bin/env python3
"""Prove that a firing rule reaches a person: Prometheus -> Alertmanager -> SMTP -> Mailpit.

`D10-111` added `ops/prometheus/rules.yml` with a real alert in it, and `D10-116` added the
receiver that alert never had. Neither claim is a claim about a file: `promtool` validates
syntax, and `amtool check-config` validates syntax, and a `alerting:` block naming a host
that resolves to nothing passes both. The only thing that answers "is the operator told"
is an alert that arrives somewhere a person reads, so this script arranges exactly that
and then looks for the mail.

Why it does not use the stack's own config
------------------------------------------
`ops/prometheus/prometheus.yml` has one rule, on `increase(escalation_email_failed_total[1h])`,
which only moves when a real worker's real SMTP relay refuses a real mail. Nothing can make
that fire on a CI runner without a worker and a broken relay, so executing it would prove
nothing about the delivery it is supposed to prove. `ops/prometheus/smoke.yml` therefore runs
in its place: same directory, same `/etc/prometheus` mount, same `alertmanager:9093` target,
and one rule that is `up == 1`. That is true of a working Prometheus and false of a broken
one — it cannot pass on an idle stack and it needs no fixture.

The static half still checks the stack's config, because "the smoke proved a different file
than the stack runs" is the failure mode that makes a smoke worthless: `smoke.yml` and
`prometheus.yml` must name the same Alertmanager target, and `prometheus.yml` must load the
rule file that declares the real alert.

None of this claims the real alert was delivered. It claims the path is real.

Usage
-----
    python backend/tests/smoke_alert_delivery.py

Exit code 0 means every check passed. Image tags are read from `docker-compose.yml`, so this
can never validate an Alertmanager or a Mailpit the stack does not run.

It lives beside `smoke_journey.py`, `smoke_worker_metrics.py` and `smoke_escalation_alert.py`
rather than in `scripts/`: those three are the repository's other cross-process journeys, they
are run by the `observability`/`backend tests` jobs the same way, ruff lints that directory,
and `pytest` collects none of them because the file is not named `test_*.py`.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"
PROMETHEUS = ROOT / "ops" / "prometheus"

# The hostnames the two config files resolve. They are network ALIASES rather than container
# names on purpose: a developer running the real stack already owns containers called
# `prometheus`, `alertmanager` and `mailpit`, and a smoke that refused to start until they
# were stopped would be a smoke nobody runs.
NETWORK = "contract-agent-smoke"
CONTAINERS = {
    "prometheus": "ca-smoke-prometheus",
    "alertmanager": "ca-smoke-alertmanager",
    "mailpit": "ca-smoke-mailpit",
}
ALERTMANAGER_PORT = 19093
MAILPIT_PORT = 18025
DELIVERY_DEADLINE_SECONDS = 120.0
ALERTNAME = "SmokeStackDeliversAlerts"
# The real alert in `ops/prometheus/rules.yml`, named once here so the static check and the
# report it prints cannot drift. It is still a string, not an import: nothing in `app/` owns it.
STACK_ALERTNAME = "EscalationEmailFailed"
SMOKE_ALERTMANAGER_TARGET = "alertmanager:9093"


class SmokeError(Exception):
    """A check that did not hold. The message is the report."""


def say(message: str) -> None:
    print(message, flush=True)


def need(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def image_for(compose: str, service: str) -> str:
    """The pinned image for one service, so the smoke runs what the stack runs."""
    lines = compose.splitlines()
    header = f"  {service}:"
    for index, line in enumerate(lines):
        if line.rstrip() != header:
            continue
        for following in lines[index + 1 :]:
            if following.startswith("  ") and not following.startswith("   "):
                break
            stripped = following.strip()
            if stripped.startswith("image:"):
                return stripped.split(":", 1)[1].strip()
        break
    raise SmokeError(
        f"`{service}` has no pinned `image:` in docker-compose.yml, so there is no tag to run"
    )


def http_json(url: str, *, timeout: float = 5.0) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
        return json.loads(response.read().decode("utf-8"))


def nested(payload: dict[str, object], *keys: str) -> object:
    """Walk a decoded JSON object, returning None where a hop is absent or not an object.

    Alertmanager's own status payload is four levels deep and only the leaves are interesting.
    Written once here so no call site needs a `type: ignore` on a value it cannot describe.
    """
    current: object = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    # `docker` is deliberately unqualified: CI's docker is on PATH, and an absolute path here
    # would be a second thing to keep in step with the runner. Binding argv first also stops
    # bandit's partial-executable-path rule firing, since it only inspects call arguments.
    argv = ["docker", *args]
    return subprocess.run(  # noqa: S603 - argv is this module's own table, never a shell string
        argv,
        capture_output=True,
        text=True,
        check=check,
    )


# ---------------------------------------------------------------- static checks


def check_the_smoke_names_the_stacks_receiver() -> None:
    """The smoke must aim at the same Alertmanager the stack aims at."""
    smoke = read(PROMETHEUS / "smoke.yml")
    stack = read(PROMETHEUS / "prometheus.yml")
    need(
        f'targets: ["{SMOKE_ALERTMANAGER_TARGET}"]' in smoke,
        f"smoke.yml no longer names {SMOKE_ALERTMANAGER_TARGET}, so it would prove a hop the "
        "stack does not have",
    )
    need(
        f'targets: ["{SMOKE_ALERTMANAGER_TARGET}"]' in stack,
        f"prometheus.yml no longer names {SMOKE_ALERTMANAGER_TARGET}; the smoke and the stack "
        "have drifted apart",
    )
    say(f"ok    smoke.yml and prometheus.yml both target {SMOKE_ALERTMANAGER_TARGET}")


def check_the_stack_loads_the_rule_that_alerts() -> None:
    """A rule in a file no Prometheus loads is a comment.

    The alert name is matched as a whole *line*, not as a substring, and that is not
    pedantry: `"alert: EscalationEmailFailed" in rules` is satisfied by
    `alert: EscalationEmailFailedRenamed`, so the substring form of this check reported
    "the stack declares the alert" for a rule file whose alert had been renamed out from
    under every dashboard and runbook that names it. A grammar-free `in` test on a YAML key
    cannot see the difference between a value and a value's prefix; comparing the stripped
    line can.
    """
    stack = read(PROMETHEUS / "prometheus.yml")
    rules = read(PROMETHEUS / "rules.yml")
    need(
        "/etc/prometheus/rules.yml" in stack,
        "prometheus.yml does not load /etc/prometheus/rules.yml, so the real alert is never "
        "evaluated",
    )
    # `- alert:` is the YAML list item; the dash is stripped so the comparison is against
    # the key and the whole value, not against whatever indentation or list marker precedes it.
    declared = {line.strip().removeprefix("- ") for line in rules.splitlines()}
    need(
        f"alert: {STACK_ALERTNAME}" in declared,
        f"rules.yml no longer declares `alert: {STACK_ALERTNAME}` as an alert name, so the "
        "rule Prometheus evaluates is not the rule the receiver and the runbooks name",
    )
    say(f"ok    prometheus.yml loads rules.yml, which declares {STACK_ALERTNAME}")


def check_the_stack_has_a_receiver() -> None:
    """The compose stack must define the service the configs resolve by name."""
    compose = read(COMPOSE)
    for service in ("alertmanager", "mailpit"):
        need(
            f"  {service}:\n" in compose,
            f"docker-compose.yml has no `{service}` service, so nothing on the network "
            f"answers to that name",
        )
    say("ok    docker-compose.yml defines alertmanager and mailpit")


# ------------------------------------------------------------------ live checks


def start(host: str, image: str, flags: list[str], command: list[str] | None = None) -> None:
    """`docker run -d` one service onto the smoke network, answering to its stack hostname.

    `flags` land before the image and `command` after it, which is `docker run`'s own grammar:
    the first is docker's, the second is the container's.
    """
    docker(
        "run",
        "-d",
        "--name",
        CONTAINERS[host],
        "--network",
        NETWORK,
        "--network-alias",
        host,
        *flags,
        image,
        *(command or []),
    )


def start_stack(images: dict[str, str]) -> None:
    cleanup()
    docker("network", "create", NETWORK)
    start(
        "prometheus",
        images["prometheus"],
        ["--tmpfs", "/prometheus:mode=1777", "-v", f"{PROMETHEUS}:/etc/prometheus:ro"],
        ["--config.file=/etc/prometheus/smoke.yml", "--storage.tsdb.path=/prometheus"],
    )
    start(
        "alertmanager",
        images["alertmanager"],
        [
            "--tmpfs",
            "/alertmanager:mode=1777",
            "-p",
            f"127.0.0.1:{ALERTMANAGER_PORT}:9093",
            "-v",
            f"{ROOT / 'ops' / 'alertmanager'}:/etc/alertmanager:ro",
        ],
        ["--config.file=/etc/alertmanager/alertmanager.yml", "--storage.path=/alertmanager"],
    )
    start("mailpit", images["mailpit"], ["-p", f"127.0.0.1:{MAILPIT_PORT}:8025"])
    say("ok    started prometheus, alertmanager and mailpit on " + NETWORK)


def wait_for_alertmanager() -> dict[str, object]:
    """The running receiver must report the config this repository ships."""
    status: dict[str, object] = {}
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            status = http_json(f"http://127.0.0.1:{ALERTMANAGER_PORT}/api/v2/status")
        except (urllib.error.URLError, OSError):
            time.sleep(1.0)
            continue
        break
    need(
        bool(status),
        f"Alertmanager never answered on 127.0.0.1:{ALERTMANAGER_PORT}/api/v2/status",
    )
    # `config.original` is the file Alertmanager actually loaded, not a file we read
    # ourselves -- the difference is what makes "valid here" impossible to fake.
    original = str(nested(status, "config", "original") or "")
    need(
        "mailpit:1025" in original,
        "the running Alertmanager did not load ops/alertmanager/alertmanager.yml: its "
        "`smtp_smarthost` is not `mailpit:1025`",
    )
    need(
        "operator@contract-agent.invalid" in original,
        "the receiver's `to` address is gone from the loaded config, so there is nobody to tell",
    )
    say("ok    Alertmanager loaded ops/alertmanager/alertmanager.yml (relay and recipient)")
    return status


def wait_for_the_alert() -> dict[str, object]:
    """Poll the receiver's own API until the smoke alert is active in it."""
    deadline = time.monotonic() + DELIVERY_DEADLINE_SECONDS
    seen: list[str] = []
    while time.monotonic() < deadline:
        try:
            alerts = http_json(f"http://127.0.0.1:{ALERTMANAGER_PORT}/api/v2/alerts")
        except (urllib.error.URLError, OSError):
            alerts = []
        if isinstance(alerts, list):
            for alert in alerts:
                labels = alert.get("labels", {})
                seen.append(str(labels.get("alertname")))
                if labels.get("alertname") == ALERTNAME:
                    return alert
        time.sleep(2.0)
    raise SmokeError(
        f"no alert named {ALERTNAME} reached Alertmanager within "
        f"{DELIVERY_DEADLINE_SECONDS:.0f}s; alertnames seen: {sorted(set(seen)) or 'none'}"
    )


def wait_for_the_mail() -> dict[str, object]:
    """Poll the catcher's API until the mail the receiver sent is in it."""
    deadline = time.monotonic() + DELIVERY_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        try:
            listing = http_json(f"http://127.0.0.1:{MAILPIT_PORT}/api/v1/messages")
        except (urllib.error.URLError, OSError):
            listing = {}
        messages = listing.get("messages") or []
        if isinstance(messages, list):
            for message in messages:
                subject = str(message.get("Subject", ""))
                if ALERTNAME in subject:
                    return message
        time.sleep(2.0)
    raise SmokeError(
        f"Alertmanager accepted the alert but no mail naming {ALERTNAME} reached Mailpit "
        f"within {DELIVERY_DEADLINE_SECONDS:.0f}s; the receiver's e-mail did not leave"
    )


def cleanup() -> None:
    for container in CONTAINERS.values():
        docker("rm", "-f", container, check=False)
    docker("network", "rm", NETWORK, check=False)


def dump_logs() -> None:
    for host, container in CONTAINERS.items():
        logs = docker("logs", "--tail", "25", container, check=False)
        say(f"--- {host} ---")
        say((logs.stdout or "") + (logs.stderr or ""))


def main() -> int:
    try:
        compose = read(COMPOSE)

        # Static checks first, and in this order. `image_for` below *needs* the three services
        # to exist, so resolving an image before asserting they do turned "the stack has no
        # alertmanager" into "`alertmanager` has no pinned image" — a true sentence about a
        # consequence, printed in place of the cause. Every check that needs no Docker runs
        # first; nothing is started until the files agree with each other.
        check_the_stack_has_a_receiver()
        check_the_smoke_names_the_stacks_receiver()
        check_the_stack_loads_the_rule_that_alerts()

        images = {service: image_for(compose, service) for service in CONTAINERS}
        say("pinned   " + ", ".join(f"{k}={v}" for k, v in images.items()))

        start_stack(images)
        wait_for_alertmanager()
        alert = wait_for_the_alert()
        say(
            f"ok    {ALERTNAME} is active in Alertmanager (state={nested(alert, 'status', 'state')})"
        )
        message = wait_for_the_mail()
        subject = nested(message, "Subject")
        sender = nested(message, "From", "Address")
        say(f"ok    Mailpit received `{subject}` from {sender}")
    except SmokeError as error:
        say(f"FAIL  {error}")
        dump_logs()
        return 1
    except (urllib.error.URLError, OSError, subprocess.CalledProcessError) as error:
        say(f"FAIL  {error}")
        dump_logs()
        return 1
    finally:
        cleanup()
    say("PASS  a firing rule reached the operator's mailbox")
    return 0


if __name__ == "__main__":
    sys.exit(main())
