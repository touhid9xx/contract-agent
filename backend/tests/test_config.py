"""Fail-fast configuration validation."""

from __future__ import annotations

import ast
import base64
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core.config import (
    DEV_BLIND_INDEX_KEY_PLACEHOLDER,
    DEV_ENCRYPTION_KEY_PLACEHOLDER,
    DEV_SECRET_PLACEHOLDER,
    MAX_RETIRED_ENCRYPTION_KEYS,
    Settings,
)

#: Which worker processes the stack starts, from `docker-compose.yml`. Reused rather
#: than parsed again: that file already answers "which modules are processes" for
#: `test_worker_wiring`, and a second reader would be a second thing to disagree.
from tests.test_worker_wiring import _started

VALID_PRODUCTION_SECRET = "a" * 40
VALID_PRODUCTION_ORIGINS = "https://app.example.com"
#: Production has to name an absolute document store: the default is relative,
#: which is right for a checkout and wrong for three processes with three working
#: directories (see ``Settings._reject_insecure_production_defaults``).
PRODUCTION_DOCUMENT_PATH = "/var/lib/contract-agent/documents"


def make_production(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "secret_key": VALID_PRODUCTION_SECRET,
        "cors_origins": VALID_PRODUCTION_ORIGINS,
        "document_store_path": PRODUCTION_DOCUMENT_PATH,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_development_defaults_are_permissive() -> None:
    settings = Settings(app_env="development", log_json=False)

    assert settings.is_production is False
    assert settings.json_logs is False
    assert settings.secret_key == DEV_SECRET_PLACEHOLDER


def test_json_logs_default_to_on_in_production() -> None:
    settings = make_production()

    assert settings.json_logs is True


def test_explicit_log_json_overrides_environment_default() -> None:
    assert Settings(app_env="test", log_json=True).json_logs is True


def test_production_rejects_placeholder_secret() -> None:
    with pytest.raises(ValidationError, match="SECRET_KEY"):
        make_production(secret_key=DEV_SECRET_PLACEHOLDER)


def test_production_rejects_short_secret() -> None:
    with pytest.raises(ValidationError, match="at least 32"):
        make_production(secret_key="short-but-not-placeholder")


def test_production_rejects_plain_text_logs() -> None:
    with pytest.raises(ValidationError, match="LOG_JSON"):
        make_production(log_json=False)


def test_production_rejects_loopback_cors_origin() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        make_production(cors_origins="https://app.example.com,http://localhost:3000")


def test_production_rejects_a_relative_document_store_path() -> None:
    with pytest.raises(ValidationError, match="absolute path"):
        make_production(document_store_path="var/documents")


def test_a_relative_document_store_path_is_fine_in_development() -> None:
    # The default, in the environment that wants documents beside the checkout.
    assert Settings(app_env="development", log_json=False).document_store_path == "var/documents"


def test_wildcard_cors_is_rejected_everywhere() -> None:
    with pytest.raises(ValidationError, match=r"'\*' is not permitted"):
        Settings(app_env="development", cors_origins="*")


def test_invalid_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError, match="LOG_LEVEL"):
        Settings(app_env="development", log_level="LOUD")


def test_log_level_is_normalised() -> None:
    assert Settings(app_env="development", log_level=" debug ").log_level == "DEBUG"


def test_api_prefix_requires_leading_slash() -> None:
    with pytest.raises(ValidationError, match="must start with"):
        Settings(app_env="development", api_v1_prefix="api/v1")


def test_api_prefix_rejects_trailing_slash() -> None:
    with pytest.raises(ValidationError, match="must not end with"):
        Settings(app_env="development", api_v1_prefix="/api/v1/")


def test_access_token_lifetime_is_bounded() -> None:
    """The <=120 minute cap is what keeps access shorter than refresh."""
    with pytest.raises(ValidationError, match="less than or equal to 120"):
        Settings(app_env="development", access_token_expire_minutes=121)


def test_cors_origin_list_is_split_and_trimmed() -> None:
    settings = Settings(app_env="development", cors_origins=" http://a.test , http://b.test, ")

    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]


def test_kafka_servers_are_split() -> None:
    settings = Settings(app_env="development", kafka_bootstrap_servers="a:9092, b:9092")

    assert settings.kafka_servers == ["a:9092", "b:9092"]


def test_encryption_key_shape_detection() -> None:
    # Empty is legal, not valid: the API process never stores field values, so a
    # missing key must not stop it booting. ``build_cipher`` refuses at the point
    # of use instead (fail-closed).
    assert Settings(app_env="development").fernet_key_is_valid is False

    # 32 raw bytes, url-safe base64 encoded -> structurally a valid Fernet key.
    valid_key = base64.urlsafe_b64encode(bytes(32)).decode("ascii")
    assert Settings(app_env="development", encryption_key=valid_key).fernet_key_is_valid is True


def test_a_malformed_encryption_key_is_refused_at_startup() -> None:
    # A malformed key is worse than a missing one: it would fail at the first
    # encrypt, i.e. mid-write, rather than at boot (D7-01).
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY"):
        Settings(app_env="development", encryption_key="not-base64!!")

    # Right alphabet, wrong length.
    short_key = base64.urlsafe_b64encode(bytes(16)).decode("ascii")
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY"):
        Settings(app_env="development", encryption_key=short_key)


# --- retired encryption keys (rotation) -----------------------------------
# ``ENCRYPTION_KEY_PREVIOUS`` is the difference between "rotating the key is
# possible" and "rotating the key silently orphans every stored address". It is
# decrypt-only, so a bad entry cannot corrupt a write — it makes a readable row
# unreadable, which is why it is validated at boot rather than at first use.


def _key(seed: int) -> str:
    """A structurally valid Fernet key, distinct per seed."""
    return base64.urlsafe_b64encode(bytes([seed]) * 32).decode("ascii")


def test_retired_keys_are_split_and_trimmed() -> None:
    settings = Settings(
        app_env="development",
        encryption_key=_key(1),
        encryption_key_previous=f" {_key(2)} , {_key(3)}, ",
    )

    assert settings.retired_encryption_keys == [_key(2), _key(3)]


def test_no_retired_keys_is_the_steady_state() -> None:
    assert Settings(app_env="development", encryption_key=_key(1)).retired_encryption_keys == []


def test_a_malformed_retired_key_is_refused_at_startup() -> None:
    # Named by position, never by value: an old key is still a secret.
    with pytest.raises(ValidationError, match="entry 2"):
        Settings(
            app_env="development",
            encryption_key=_key(1),
            encryption_key_previous=f"{_key(2)}, not-a-key",
        )


def test_retired_keys_without_a_current_key_are_refused() -> None:
    # Retired keys only decrypt, so this configuration could read rows and never
    # seal one — including the rows a reseal run was about to re-wrap.
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY is empty"):
        Settings(app_env="development", encryption_key="", encryption_key_previous=_key(2))


def test_the_current_key_cannot_also_be_retired() -> None:
    with pytest.raises(ValidationError, match="appears in ENCRYPTION_KEY_PREVIOUS"):
        Settings(
            app_env="development",
            encryption_key=_key(1),
            encryption_key_previous=f"{_key(1)},{_key(2)}",
        )


def test_the_same_retired_key_cannot_be_listed_twice() -> None:
    with pytest.raises(ValidationError, match="same key twice"):
        Settings(
            app_env="development",
            encryption_key=_key(1),
            encryption_key_previous=f"{_key(2)},{_key(2)}",
        )


def test_more_retired_keys_than_the_cap_is_refused() -> None:
    # Every decrypt walks the list, and it sits on the send path. A rotation that
    # is finished carries none, so exceeding the cap means one never finished.
    too_many = ",".join(_key(seed) for seed in range(2, 2 + MAX_RETIRED_ENCRYPTION_KEYS + 1))

    with pytest.raises(ValidationError, match="at most"):
        Settings(
            app_env="development",
            encryption_key=_key(1),
            encryption_key_previous=too_many,
        )


def test_exactly_the_cap_of_retired_keys_is_allowed() -> None:
    allowed = ",".join(_key(seed) for seed in range(2, 2 + MAX_RETIRED_ENCRYPTION_KEYS))

    settings = Settings(
        app_env="development",
        encryption_key=_key(1),
        encryption_key_previous=allowed,
    )

    assert len(settings.retired_encryption_keys) == MAX_RETIRED_ENCRYPTION_KEYS


def test_a_zero_dispatch_budget_is_refused_at_startup() -> None:
    """0 reads as "no retries" and behaves as "never sends".

    ``dispatch_notification`` parks the row when ``attempts >= retry_max`` before
    the transport is reached, so a budget of 0 would park every row on its first
    attempt. It is refused at boot rather than left to look configured.
    """
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        Settings(app_env="development", notification_retry_max=0)


def test_the_scheduler_batch_and_interval_are_bounded() -> None:
    with pytest.raises(ValidationError, match="greater than 0"):
        Settings(app_env="development", notification_batch_size=0)
    with pytest.raises(ValidationError, match="less than or equal to 300"):
        Settings(app_env="development", notification_poll_interval_seconds=301)

    # Defaults are usable without configuration, so the worker boots out of the box.
    settings = Settings(app_env="development")
    assert settings.notification_batch_size == 50
    assert settings.notification_poll_interval_seconds == 5.0


def test_a_hosted_agent_backend_is_not_representable() -> None:
    """The product LLM is local by rule, so a cloud backend cannot even be named.

    ``AgentBackend`` is a closed literal rather than a free string: there is no
    configuration that adds a hosted provider, so "no cloud LLM in the product"
    is enforced by the type instead of by review.
    """
    with pytest.raises(ValidationError, match="agent_backend"):
        # The ignore is the point: a type checker refuses this call, which is
        # exactly the guarantee being tested at runtime.
        Settings(app_env="development", agent_backend="openai")  # type: ignore[arg-type]


def test_a_remote_ollama_endpoint_must_be_tls() -> None:
    """The feature vector is personal data even with no identifier in it.

    A plaintext hop to a remote model would let whoever sits on the network see
    who is being contacted about which contract, so a deployable environment
    refuses the shape at boot instead of trusting the deployer to remember.
    """
    with pytest.raises(ValidationError, match="OLLAMA_BASE_URL must be https"):
        make_production(
            agent_backend="ollama",
            ollama_base_url="http://gpu.example.com:11434",
        )


def test_a_loopback_ollama_endpoint_may_be_plain_http() -> None:
    """A sidecar on the same host is not crossing a network, so http is allowed."""
    settings = make_production(
        agent_backend="ollama",
        ollama_base_url="http://127.0.0.1:11434",
    )

    assert settings.agent_backend == "ollama"


def test_a_tls_remote_ollama_endpoint_is_accepted() -> None:
    settings = make_production(
        agent_backend="ollama",
        ollama_base_url="https://gpu.example.com:11434",
    )

    assert settings.ollama_base_url == "https://gpu.example.com:11434"


# --- the env file nobody should be reading by accident ----------------------

#: The file `docker compose` interpolates, and the one a deployer is told to copy.
EXAMPLE_ENV = Path(__file__).resolve().parents[2] / ".env.example"

#: The stack that reads the example, and the second consumer this file answers to:
#: `docker compose` parses it before it builds an image or starts a container.
COMPOSE_FILE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

#: Entries in `.env.example` that are not settings. They belong to the stack's own
#: interpolation, so the application never reads them and no field declares them.
#: ``PROMETHEUS_PORT`` is the port the scrape target publishes (``D10-111``); the
#: ``MYSQL_*`` block is the database the stack creates for itself, which the example
#: has to carry because two of those values are written ``${VAR:?…}`` — a refusal,
#: not a default.
COMPOSE_ONLY_KEYS = frozenset(
    {
        "PROMETHEUS_PORT",
        "MYSQL_DATABASE",
        "MYSQL_PASSWORD",
        "MYSQL_PORT",
        "MYSQL_ROOT_PASSWORD",
        "MYSQL_USER",
    }
)


def _example_env_entries() -> list[tuple[str, str]]:
    """Every ``KEY=value`` line in `.env.example`, in file order, comments out."""
    entries: list[tuple[str, str]] = []
    for line in EXAMPLE_ENV.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        entries.append((key.strip(), value.strip()))
    return entries


def test_a_dotenv_in_the_working_directory_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ambient files must not reach the settings these tests assert.

    ``Settings`` reads ``.env`` from the working directory by design, and the repo
    root is both where `docker compose` needs that file and where `pytest` runs.
    A suite that honoured it would change behaviour with an untracked file and say
    nothing about it: ``D10-115`` measured seven failures that way, in two files,
    from values no test had passed.

    ``CORS_ORIGINS=*`` is the sharpest probe available — it is refused in *every*
    environment (see ``test_wildcard_cors_is_rejected_everywhere``), so reading the
    file is a validation error and ignoring it is a quiet pass. This test is also
    the only thing that would notice the pin in ``conftest`` going away, because CI
    has no ``.env`` for the ordinary tests to trip over.
    """
    (tmp_path / ".env").write_text("CORS_ORIGINS=*\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # Constructed, not just imported: the file is resolved at instantiation time.
    assert Settings(app_env="development").cors_origins != "*"


def test_the_example_env_file_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cp .env.example .env`` has to produce a file the application can boot on.

    ``D10-114`` shipped ``LOG_JSON=`` — an empty value pydantic refuses to parse
    into a ``bool`` — so the copy the compose gate makes was a checkout where every
    ``Settings()`` raised before a single test ran. The suite could not see it: each
    test pins the settings it asserts, and the copy was made in a compose check
    rather than here.

    Every value in the example is also the field's default, deliberately, so the
    only thing left to assert is that the file *loads* — which is exactly what
    broke. ``APP_ENV`` is unset for the length of this test because the suite pins
    it to ``test`` and an exported variable outranks the file; the example declares
    ``development``, so production-only refusals cannot confound the result.
    """
    assert EXAMPLE_ENV.is_file(), "the example env file is part of the deployment"

    entries = dict(_example_env_entries())
    # `conftest` pins APP_ENV=test for the whole run and an exported variable
    # outranks the file, so the one value that must come *from* the file is unset
    # here. It is restored by the fixture, before any other test runs.
    monkeypatch.delenv("APP_ENV", raising=False)
    settings = Settings(_env_file=EXAMPLE_ENV)  # type: ignore[call-arg]

    assert settings.app_env == entries["APP_ENV"]
    assert settings.is_production is False


def test_the_example_env_file_names_only_settings_that_exist() -> None:
    """A misspelled key is ignored in silence, so the example has to be checked.

    ``Settings`` is configured ``extra="ignore"``: ``LOG_LEVL=INFO`` in a `.env`
    changes nothing and reports nothing, which is the worst shape for the one file
    whose job is to say which knobs exist. ``PROMETHEUS_PORT`` is the single entry
    that is not a setting — it belongs to ``docker-compose.yml``'s interpolation
    (``D10-111``) — so it is named rather than quietly tolerated.
    """
    declared = set(Settings.model_fields)
    unknown = (
        {key.lower() for key, _ in _example_env_entries()}
        - declared
        - {key.lower() for key in COMPOSE_ONLY_KEYS}
    )

    assert unknown == set(), f".env.example names settings that do not exist: {sorted(unknown)}"


def _compose_variables(pattern: str) -> set[str]:
    """Variable names ``docker-compose.yml`` interpolates, matching ``pattern``."""
    return set(re.findall(pattern, COMPOSE_FILE.read_text(encoding="utf-8")))


def test_the_example_env_file_supplies_every_variable_compose_requires() -> None:
    """``cp .env.example .env`` has to be a stack that starts, not a file that boots.

    ``docker-compose.yml`` writes two variables as ``${VAR:?set VAR in .env}``. That
    is not a default with a warning attached: Compose refuses to interpolate, stops
    before it builds an image or starts a container, and prints an instruction to set
    a variable in a file the example never offered a line for. The symptom is what a
    developer reports as ``there is no container and no image`` — ``up`` answers with
    a parse error, and ``docker image ls`` has no ``contract-agent-api:dev``, because
    the build never began.

    ``test_the_example_env_file_loads`` cannot see it: it asks whether the copy boots
    the *application*, and ``Settings`` never reads a compose variable. The only other
    gate is CI's ``docker compose config -q``, which passes both values on the command
    line, so it never depends on the file a deployer copies.

    Derived from the Compose file rather than listed here, so a third ``${VAR:?}``
    added for any reason fails this test until the example carries it — and the
    failure names the variable instead of leaving it to be discovered at ``up``.
    """
    required = _compose_variables(r"\$\{([A-Z][A-Z0-9_]*):\?")
    assert required, "no required interpolation found: has docker-compose.yml changed shape?"

    supplied = {key for key, value in _example_env_entries() if value}

    assert required <= supplied, (
        f".env.example does not set {sorted(required - supplied)}, which docker-compose.yml "
        "refuses to run without: `cp .env.example .env` cannot start the stack"
    )


def test_every_compose_only_key_is_one_the_compose_file_reads() -> None:
    """The allowlist is only honest while Compose still reads the keys held in it.

    ``test_the_example_env_file_names_only_settings_that_exist`` uses
    ``COMPOSE_ONLY_KEYS`` to forgive entries that are not settings, which makes the
    set a place a mistake could hide: a misspelled ``MYSQL_PASSWROD`` would be
    forgiven there, ignored by Compose, and the stack would keep asking for the value
    it had been given. Asserting the other direction keeps every member earning its
    place, and is what would notice a renamed variable after a Compose edit.
    """
    referenced = _compose_variables(r"\$\{([A-Z][A-Z0-9_]*)")
    stray = COMPOSE_ONLY_KEYS - referenced

    assert stray == set(), (
        f".env.example excuses keys docker-compose.yml never reads: {sorted(stray)}"
    )


# --- what the stack waits for before it starts, not just what it builds -----

#: Compose's duration syntax: `300s`, `1m30s`, `1h`, or a bare count for `retries`.
DURATION = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")

#: How long a virgin MySQL datadir took to start answering on this machine, in
#: seconds. Measured rather than assumed: `backend/tests/smoke_mysql_cold_boot.py`
#: starts `mysql` from this compose file on a throwaway volume and prints the
#: number, so the one figure below can be re-taken instead of re-counted.
MYSQL_COLD_BOOT_SECONDS = 188


def _seconds(raw: str) -> int:
    """A Compose duration as whole seconds."""
    text = raw.strip()
    if text.isdigit():
        return int(text)
    match = DURATION.fullmatch(text)
    assert text, f"docker-compose.yml carries an empty duration: {raw!r}"
    assert match is not None, (
        f"docker-compose.yml carries a duration this reader cannot read: {raw!r}"
    )
    parts = [int(part) if part else 0 for part in match.groups()]
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


def _compose_services() -> dict[str, list[str]]:
    """Service name -> the lines of its own block, read off the file's indentation.

    Anchor merges (``<<: *worker``) are deliberately not resolved: this reads the
    block a service *writes down*, so a value supplied by an anchor is reported
    missing rather than inherited. That is the direction a guard should err in — it
    costs a false failure when someone moves a key into an anchor, and it cannot
    silently lose a key that moved out of one.
    """
    services: dict[str, list[str]] = {}
    block: list[str] | None = None
    under_services = False
    for line in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            under_services = line.strip() == "services:"
            block = None
            continue
        if not under_services:
            continue
        header = re.fullmatch(r"  ([\w-]+):", line)
        if header is not None:
            block = []
            services[header.group(1)] = block
        elif block is not None:
            block.append(line)
    return services


def _healthcheck(block: list[str]) -> dict[str, str]:
    """The scalar keys under one service's ``healthcheck:``, e.g. ``start_period``."""
    values: dict[str, str] = {}
    inside = False
    for line in block:
        stripped = line.strip()
        if stripped == "healthcheck:":
            inside = True
            continue
        if not inside:
            continue
        if not line.startswith("      "):
            break
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def test_the_database_cannot_be_declared_dead_while_it_is_initialising() -> None:
    """A first `up` on a fresh volume must not fail because MySQL is still booting.

    A virgin datadir has no socket to answer ``mysqladmin ping`` on, so every probe
    fails until ``--initialize`` finishes, and the healthcheck has one number that
    decides how long that is tolerated. ``D10-126`` measured both sites: the datadir
    answered at 188.1s, while ``30s + 12 x 10s`` declared the container ``unhealthy``
    at 147.8s — and ``docker compose up -d mysql migrate`` exited 1 with
    ``dependency failed to start: container ... is unhealthy``, with ``migrate``
    never running. Nothing caught it, because the obvious check is a second ``up``,
    by which time the datadir exists and the stack looks healthy.

    The deadline here is the arithmetic Compose itself applies — during
    ``start_period`` failed probes are not counted at all, then ``retries`` further
    failures are what turn ``starting`` into ``unhealthy`` — rather than the value of
    ``start_period`` alone, so the assertion says at which second the container can
    first be called dead.
    """
    health = _healthcheck(_compose_services()["mysql"])
    deadline = _seconds(health["start_period"]) + _seconds(health["interval"]) * int(
        health["retries"]
    )

    assert deadline >= MYSQL_COLD_BOOT_SECONDS, (
        f"the mysql healthcheck can be judged `unhealthy` {deadline}s after `up`, and a "
        f"virgin datadir was measured at {MYSQL_COLD_BOOT_SECONDS}s: a first `up` on a "
        "fresh volume fails with `dependency failed to start` and `migrate` never runs"
    )


def test_every_service_a_dependency_waits_for_gives_it_time_to_start() -> None:
    """``condition: service_healthy`` is a gate, and the gate has to be one a boot survives.

    ``depends_on`` with a health condition waits for ``healthy`` and gives up on
    ``unhealthy``. A service whose healthcheck has no grace therefore makes every
    dependent's start depend on it being up within ``retries`` intervals — true of a
    warm container, false of a first boot. The services are read out of the compose
    file rather than listed, so a new dependency on a new service is covered the day
    it is written.
    """
    targets = set(
        re.findall(
            r"^[ ]+([\w-]+):\n[ ]+condition: service_healthy",
            COMPOSE_FILE.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    )
    assert targets, "no `condition: service_healthy` found: has docker-compose.yml changed?"

    services = _compose_services()
    unknown = {name for name in targets if name not in services}
    assert not unknown, (
        f"docker-compose.yml waits on services it does not define: {sorted(unknown)}"
    )

    without_grace = {name for name in targets if "start_period" not in _healthcheck(services[name])}
    assert not without_grace, (
        f"{sorted(without_grace)} are waited for with `condition: service_healthy` but declare "
        "no `start_period`, so a dependent fails on the first slow boot"
    )


# --- what the stack needs to start, not just what Settings accepts ----------

WORKERS_DIR = Path(__file__).resolve().parents[2] / "backend" / "app" / "workers"
CORE_DIR = Path(__file__).resolve().parents[2] / "backend" / "app" / "core"

#: An all-caps token inside a refusal message, e.g. ``ENCRYPTION_KEY``.
ENV_TOKEN = re.compile(r"[A-Z][A-Z0-9_]{2,}")


def _example_env_values() -> dict[str, str]:
    """`.env.example` as ``KEY -> value``; a commented-out line is not an entry."""
    return dict(_example_env_entries())


def _refused_fields(function: ast.FunctionDef) -> set[str]:
    """The settings fields named in the messages this function raises.

    A builder that refuses to work without a key says so in the sentence an operator
    reads, so that sentence is where the requirement is recorded. Reading it here
    means the check cannot drift from the message the operator is given: if the
    message is wrong, this test is wrong in the same direction, in the same place.
    """
    fields: set[str] = set()
    for node in ast.walk(function):
        if not isinstance(node, ast.Raise):
            continue
        parts = ast.walk(node.exc) if node.exc is not None else iter(())
        for part in parts:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                for token in ENV_TOKEN.findall(part.value):
                    if token.lower() in Settings.model_fields:
                        fields.add(token.lower())
    return fields


def _fail_closed_builders() -> dict[str, set[str]]:
    """Builder function -> the settings fields it refuses to run without."""
    builders: dict[str, set[str]] = {}
    for path in sorted(CORE_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            fields = _refused_fields(node)
            if fields:
                builders[node.name] = fields
    return builders


def _built_at_start_up() -> dict[str, list[str]]:
    """Worker module -> the variables its own construction cannot do without.

    Only ``__init__`` and ``main`` are read, because only they run at start-up: a
    builder called from a tick is a refusal per row, not a container that never comes
    up. ``ExtractionWorker`` is the one that builds in ``__init__`` — deliberately, so
    a missing key fails at start-up instead of once per contract — and that is why a
    copy of the example cannot be validated against ``Settings`` alone.
    """
    builders = _fail_closed_builders()
    needed: dict[str, list[str]] = {}
    for service, (stem, _host, _port) in _started().items():
        path = WORKERS_DIR / f"{stem}.py"
        assert path.is_file(), (
            f"docker-compose.yml starts `{service}` from app.workers.{stem}, "
            f"which does not exist at {path}"
        )
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        fields: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name not in {"__init__", "main"}:
                continue
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                    fields |= builders.get(call.func.id, set())
        if fields:
            needed[f"app.workers.{stem}"] = sorted(field.upper() for field in fields)
    return needed


def test_the_example_env_file_carries_what_every_worker_needs_at_start_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy of the example has to start the stack, not merely boot one process.

    The report that found this was ``no container and no image``, which was Compose
    refusing to interpolate. One layer in there is a quieter version of the same
    thing: the containers start and one of them never finishes. ``ExtractionWorker``
    builds its cipher and its contact blind index in ``__init__``, so a copy whose
    ``ENCRYPTION_KEY`` is empty — the example used to ship exactly that — or whose
    ``CONTACT_BLIND_INDEX_KEY`` is commented out — also exactly what it shipped —
    restarts the ``extractor`` service forever, and ``docker compose ps`` shows
    twelve services up.

    ``test_the_example_env_file_loads`` cannot see it: ``Settings`` permits both keys
    to be absent, because the API fails closed at the point of use instead. Nor can
    ``docker compose config -q``, which never constructs a worker. The requirement is
    read out of ``app/core``'s own refusal messages and the workers' own constructors
    rather than listed here, so a worker that starts building a keyed object at
    start-up fails this test until the example carries the variable.
    """
    needed = _built_at_start_up()
    assert needed, (
        "no worker builds a fail-closed object at start-up, in the whole tree: that is "
        "the parser, not the stack"
    )

    # The suite pins APP_ENV=test for the whole run and an exported variable outranks
    # the file, so the one value that must come from the file is unset here.
    monkeypatch.delenv("APP_ENV", raising=False)
    settings = Settings(_env_file=EXAMPLE_ENV)  # type: ignore[call-arg]

    missing = {
        module: [name for name in names if not getattr(settings, name.lower())]
        for module, names in needed.items()
    }
    missing = {module: names for module, names in missing.items() if names}

    assert not missing, (
        f"`.env.example` leaves {missing} unset, and those workers build that key in "
        "`__init__`: `cp .env.example .env && docker compose up` starts containers that "
        "crash-loop before they do any work, which reads as a stack that never came up"
    )


def test_the_example_env_file_carries_the_dev_keys_production_refuses() -> None:
    """Two files have to name the same two keys, and this is what holds them together.

    The example ships a key for the extractor; the refusal in
    ``Settings._reject_insecure_production_defaults`` is what makes shipping one
    acceptable. Either alone is worse than neither: a key in the example with no
    refusal is a credential that follows a copy into a deployment, and a refusal with
    no key is a stack that cannot start. Comparing the values is what stops the pair
    drifting apart — changing one without the other now fails here rather than
    silently leaving a published key that production accepts.
    """
    values = _example_env_values()

    assert values["ENCRYPTION_KEY"] == DEV_ENCRYPTION_KEY_PLACEHOLDER
    assert values["CONTACT_BLIND_INDEX_KEY"] == DEV_BLIND_INDEX_KEY_PLACEHOLDER


def test_production_rejects_the_examples_encryption_key() -> None:
    with pytest.raises(ValidationError, match="ENCRYPTION_KEY"):
        make_production(encryption_key=DEV_ENCRYPTION_KEY_PLACEHOLDER)


def test_production_rejects_the_examples_blind_index_key() -> None:
    with pytest.raises(ValidationError, match="CONTACT_BLIND_INDEX_KEY"):
        make_production(contact_blind_index_key=DEV_BLIND_INDEX_KEY_PLACEHOLDER)
