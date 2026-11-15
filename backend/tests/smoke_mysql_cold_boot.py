#!/usr/bin/env python3
"""Prove that a first `up` on a fresh volume survives MySQL initialising.

`D10-126`. `docker-compose.yml` publishes a healthcheck on `mysql`, and two services
wait for it with `condition: service_healthy`. On a warm volume that gate is instant.
On a virgin one it is not: a datadir that has never been written has no socket for
`mysqladmin ping` to answer on, so every probe fails until `--initialize` finishes,
and the healthcheck carries exactly one number deciding how long that is tolerated.

The failure has no symptom worth reading. `up` exits 1 with `dependency failed to
start: container ... is unhealthy`, `migrate` never runs — and the second `up`, by
which time the datadir exists, comes up healthy. Whoever ran it concludes the stack
works. So the check has to be a first boot, on a volume nothing has written, and it
has to watch the transition rather than the endpoint.

What is asserted
----------------
Nothing here restates the file. The deadline is taken from the container Docker
actually started — `docker inspect` of `.Config.Healthcheck`, its `StartPeriod`,
`Interval` and `Retries` — so the arithmetic is Compose's own: failed probes are not
counted during `start_period`, and `retries` failures after it are what turn
`starting` into `unhealthy`. Two things must hold:

  * the container is never observed `unhealthy` before it is `healthy`;
  * it reaches `healthy` before that deadline, i.e. inside the window the file
    allows it.

`starting` for a while is expected and is not a failure: the probe owns the number,
never Docker.

The seconds it prints are where `MYSQL_COLD_BOOT_SECONDS` in `test_config.py` came
from. Re-run this on other hardware and that constant is what to update.

Usage
-----
    python backend/tests/smoke_mysql_cold_boot.py

Exit code 0 means both checks held. It runs `mysql` from this repository's own
`docker-compose.yml`, under its own project, its own volume and its own published
port, and removes all three on the way out, so a stack the developer has running is
not touched. It boots `mysql` alone on purpose — a dependent would abort the whole
`up` and hide the number this script exists to print.

It takes about three minutes, which is why `pytest` collects none of it (the file is
not named `test_*.py`) and CI does not run it: the static half in `test_config.py`
holds the line, this is how the constant behind it is re-measured.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.yml"

#: Its own project, volume and port, so a running stack is not disturbed: `down -v`
#: below would otherwise remove a developer's datadir.
PROJECT = "cag-coldboot"
CONTAINER = f"{PROJECT}-mysql-1"
PORT = "13308"

#: The two values `docker-compose.yml` interpolates with `${VAR:?…}`; the container only
#: needs them to exist, and nothing in this script reads the database afterwards.
ENVIRONMENT = {
    "MYSQL_ROOT_PASSWORD": "cold-boot-probe-root",
    "MYSQL_PASSWORD": "cold-boot-probe-app",
    "MYSQL_PORT": PORT,
}

POLL_SECONDS = 3.0
#: A backstop only. The real bound is the healthcheck's own deadline, asserted below;
#: this one exists so a wedged Docker cannot hang the script forever.
GIVE_UP_SECONDS = 900.0


class SmokeError(Exception):
    """A check that did not hold. The message is the report."""


def say(message: str) -> None:
    print(message, flush=True)


def need(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeError(message)


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    # `docker` is deliberately unqualified: it is on PATH here, and an absolute path would
    # be a second thing to keep in step with the machine. Binding argv first also stops
    # bandit's partial-executable-path rule firing, since it only inspects call arguments.
    argv = ["docker", *args]
    return subprocess.run(  # noqa: S603 - argv is this module's own table, never a shell string
        argv,
        capture_output=True,
        text=True,
        check=check,
        env={**os.environ, **ENVIRONMENT},
    )


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return docker(
        "compose",
        "-p",
        PROJECT,
        "-f",
        str(COMPOSE),
        *args,
        check=check,
    )


def status() -> str:
    """The container's health status, or the empty string while it does not exist yet."""
    result = docker(
        "inspect",
        "--format",
        "{{.State.Health.Status}}",
        CONTAINER,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def healthcheck_seconds() -> tuple[float, float]:
    """``(grace, allowed)`` for the healthcheck Docker is actually applying.

    ``grace`` is spent before failures are counted at all; ``allowed`` is the
    ``retries`` window after it. Their sum is the first second Compose can answer
    ``unhealthy``, which is the number `test_config.py` holds a constant against.
    """
    raw = docker("inspect", "--format", "{{json .Config.Healthcheck}}", CONTAINER).stdout
    healthcheck = json.loads(raw)
    need(isinstance(healthcheck, dict), f"unreadable healthcheck from docker inspect: {raw!r}")
    nanoseconds = 1_000_000_000
    grace = float(healthcheck.get("StartPeriod") or 0) / nanoseconds
    window = (
        float(healthcheck.get("Interval") or 0) / nanoseconds * int(healthcheck.get("Retries") or 0)
    )
    return grace, window


def cleanup() -> None:
    compose("down", "-v", "--remove-orphans", check=False)


def watch_until_healthy() -> list[tuple[float, str]]:
    """Poll the health status, returning the transitions and their elapsed seconds."""
    started = time.monotonic()
    give_up = started + GIVE_UP_SECONDS
    transitions: list[tuple[float, str]] = []
    while time.monotonic() < give_up:
        seen = status()
        if seen and (not transitions or transitions[-1][1] != seen):
            transitions.append((time.monotonic() - started, seen))
            say(f"      {transitions[-1][0]:6.1f}s  {seen}")
        if seen == "unhealthy":
            raise SmokeError(
                f"the container was declared `unhealthy` at {time.monotonic() - started:.1f}s "
                "while its datadir was still being initialised: anything waiting on "
                "`condition: service_healthy` is told its dependency failed"
            )
        if seen == "healthy":
            return transitions
        time.sleep(POLL_SECONDS)
    raise SmokeError(f"no health status after {GIVE_UP_SECONDS:.0f}s: is Docker able to pull?")


def main() -> int:
    try:
        cleanup()
        say(f"==> {CONTAINER}: a volume that has never been written")
        compose("up", "-d", "mysql")
        transitions = watch_until_healthy()
        elapsed = transitions[-1][0]

        grace, window = healthcheck_seconds()
        deadline = grace + window
        say(f"      healthy at {elapsed:.1f}s")
        say(
            f"      the file allows {grace:.0f}s of grace plus {window:.0f}s of retries = {deadline:.0f}s"
        )

        need(
            deadline >= elapsed,
            f"this boot took {elapsed:.1f}s and the healthcheck can declare it dead at "
            f"{deadline:.0f}s: on this machine a fresh volume fails its first `up`",
        )
        say("ok    it answered inside the window the file allows")
        say("")
        say(f"      a cold datadir on this machine takes {elapsed:.1f}s")
        say("      `MYSQL_COLD_BOOT_SECONDS` in backend/tests/test_config.py is the floor")
        say("      this number is meant to raise, not to be replaced by it.")
    except SmokeError as error:
        say(f"FAIL  {error}")
        logs = compose("logs", "--tail", "20", "mysql", check=False)
        say((logs.stdout or "") + (logs.stderr or ""))
        return 1
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError) as error:
        say(f"FAIL  {error}")
        return 1
    finally:
        cleanup()
    say("PASS  a virgin datadir is never called dead before it is ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
