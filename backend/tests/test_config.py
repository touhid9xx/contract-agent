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
#: ``PROMETHEUS_PORT`` is the port the scrape target publishes (``D10-111``) and
#: ``FRONTEND_PORT`` the port the screen publishes (``D10-128``); the ``MYSQL_*``
#: block is the database the stack creates for itself, which the example has to
#: carry because two of those values are written ``${VAR:?…}`` — a refusal, not a
#: default.
COMPOSE_ONLY_KEYS = frozenset(
    {
        "FRONTEND_PORT",
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


def _children(block: list[str], key: str) -> list[str]:
    """The lines nested under one ``key:`` line of ``block``, stripped.

    The indentation is read off the key itself rather than assumed, because the same
    reader serves a service block (a key at four spaces) and an anchored fragment
    (the same key at two).
    """
    nested: list[str] = []
    indent = -1
    for line in block:
        if indent < 0:
            if line.strip() == key:
                indent = len(line) - len(line.lstrip())
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        nested.append(line.strip())
    return nested


def _anchors() -> dict[str, list[str]]:
    """Anchor name -> the lines of its block, for the file's top-level ``x-…`` keys."""
    anchors: dict[str, list[str]] = {}
    block: list[str] | None = None
    for line in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            header = re.fullmatch(r"x-[\w-]+: &(\w+)", line)
            if header is None:
                block = None
            else:
                block = []
                anchors[header.group(1)] = block
            continue
        if block is not None:
            block.append(line)
    return anchors


def _layers(block: list[str]) -> list[list[str]]:
    """The blocks to read for one service, least specific first.

    A service that writes ``<<: *worker`` inherits that anchor's keys, so a value it
    does not repeat is still in force — and one comment in the anchor is a comment on
    every worker. Merged in this order a key the service writes itself wins, which is
    the direction YAML itself merges.
    """
    anchors = _anchors()
    layers: list[list[str]] = []
    for line in block:
        merge = re.fullmatch(r"\s*<<: \*(\w+)", line)
        if merge is not None and merge.group(1) in anchors:
            layers.append(anchors[merge.group(1)])
    layers.append(block)
    return layers


def _environment(block: list[str]) -> dict[str, str]:
    """``KEY: value`` entries under a service's ``environment:``, inherited included."""
    values: dict[str, str] = {}
    for layer in _layers(block):
        for entry in _children(layer, "environment:"):
            if ":" not in entry:
                continue
            key, value = entry.split(":", 1)
            values[key.strip()] = value.strip()
    return values


def _build_args(block: list[str]) -> dict[str, str]:
    """``KEY: value`` entries under a service's ``build.args:``, inherited included.

    Read in one pass off the indentation rather than by recursing into ``_children``
    twice: that helper strips every line it returns, so a second call would meet the
    nested ``args:`` at the same depth as the ``build:`` it came from and stop there,
    returning nothing.
    """
    values: dict[str, str] = {}
    for layer in _layers(block):
        build_at = args_at = -1
        for line in layer:
            entry = line.strip()
            if not entry:
                continue
            indent = len(line) - len(line.lstrip())
            if indent == 0:
                continue  # the `<<: *worker` merge line itself
            if build_at >= 0 and indent <= build_at:
                build_at = args_at = -1
            if entry == "build:":
                build_at, args_at = indent, -1
                continue
            if build_at < 0:
                continue
            if entry == "args:":
                args_at = indent
                continue
            if args_at >= 0 and indent > args_at and ":" in entry:
                key, value = entry.split(":", 1)
                values[key.strip()] = value.strip()
    return values


def _volume_entries(block: list[str]) -> list[tuple[str, str]]:
    """``(source, container path)`` for every ``volumes:`` entry one service writes.

    The source is kept because the two kinds of mount fail differently. A bind mount
    of a host path is prepared by the checkout; a *named* volume is created by Docker
    at first use, empty and owned by ``root``, so only the named ones need anything
    from the image.
    """
    entries: list[tuple[str, str]] = []
    for layer in _layers(block):
        for entry in _children(layer, "volumes:"):
            if not entry.startswith("- "):
                continue
            parts = entry[2:].split(":", 2)
            if len(parts) >= 2:
                entries.append((parts[0], parts[1]))
    return entries


def _mounts(block: list[str]) -> list[str]:
    """The container paths one service mounts a volume or a file at."""
    return [target for _, target in _volume_entries(block)]


def _healthcheck(block: list[str]) -> dict[str, str]:
    """The scalar keys under one service's ``healthcheck:``, e.g. ``start_period``."""
    values: dict[str, str] = {}
    for entry in _children(block, "healthcheck:"):
        if ":" not in entry:
            continue
        key, value = entry.split(":", 1)
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


# --- the store a read-only container is handed -----------------------------

#: ``${VAR:-default}``: the value Compose substitutes when `.env` is silent.
COMPOSE_DEFAULT = re.compile(r"\$\{(\w+):-([^}]*)\}")


def _mounted_defaults() -> dict[str, str]:
    """Variable -> the path Compose defaults it to *and* mounts somewhere.

    Derived from the Compose file in the direction the defect came from: rather than
    naming ``DOCUMENT_STORE_PATH``, this asks which interpolated variables are backed
    by a mount at all. A second volume-backed setting is covered the day it is
    written, and a moved mount takes its variable with it.
    """
    found: dict[str, str] = {}
    for block in _compose_services().values():
        mounts = _mounts(block)
        for value in _environment(block).values():
            match = COMPOSE_DEFAULT.fullmatch(value)
            if match is not None and match.group(2) in mounts:
                found[match.group(1)] = match.group(2)
    return found


def test_every_service_that_stores_documents_is_given_a_path_it_has_mounted() -> None:
    """A container whose root filesystem is read-only can only write to a mount.

    ``read_only: true`` is set once in the ``worker`` anchor and again on ``api``, so
    the only writable places in either are the ``tmpfs`` at ``/tmp`` and the volumes
    listed below it. A store path that is not one of those is not a slow failure or a
    permissions warning: ``DocumentStore.save`` calls ``mkdir`` and gets
    ``OSError: [Errno 30] Read-only file system``.

    ``D10-127`` shipped that gap in ``.env.example`` rather than here, which is why
    the sibling test below exists as well — but the pair is only safe together. This
    one pins that the value the stack *chooses* is absolute and mounted, so the test
    below cannot pass by the example simply agreeing with a broken default.

    Everything is read out of the file: the carriers are whichever services are handed
    the variable, and the mounts are those services' own (including the anchor they
    merge), so a worker that loses its ``volumes:`` line fails here.
    """
    services = _compose_services()
    carriers = {
        name: _environment(block)["DOCUMENT_STORE_PATH"]
        for name, block in services.items()
        if "DOCUMENT_STORE_PATH" in _environment(block)
    }
    assert carriers, (
        "no service is handed DOCUMENT_STORE_PATH: has docker-compose.yml changed shape?"
    )

    wrong: dict[str, str] = {}
    for name, raw in carriers.items():
        match = COMPOSE_DEFAULT.fullmatch(raw)
        if match is None:
            wrong[name] = (
                f"{raw!r} interpolates with no `:-` fallback, so an operator who has not "
                "set the variable hands every service an empty path"
            )
            continue
        path = match.group(2)
        mounts = _mounts(services[name])
        if not path.startswith("/"):
            wrong[name] = (
                f"falls back to {path!r}, which is not absolute: inside the container it "
                "resolves under the working directory, and that is read-only"
            )
        elif path not in mounts:
            wrong[name] = (
                f"falls back to {path!r}, which it does not mount; it mounts "
                f"{sorted(mounts) or 'nothing'}"
            )

    assert not wrong, (
        f"services are given a document store they cannot write: {wrong}. Every service "
        "reading it runs with `read_only: true`, so nothing outside a mount can be created"
    )


def test_the_example_env_file_does_not_move_a_mounted_default_off_its_mount() -> None:
    """`cp .env.example .env` must not overrule a default the stack chose for a reason.

    ``D10-127``: the example shipped ``DOCUMENT_STORE_PATH=var/documents``. That is
    the right answer for a checkout and the wrong one for a container — Compose's
    ``${DOCUMENT_STORE_PATH:-…}`` is a *fallback*, so a value in `.env` replaces it,
    and the replacement is then handed to every process running with ``read_only:
    true`` from ``/app``. ``var/documents`` resolved to ``/app/var/documents``, which
    cannot be created, so ``POST /api/v1/contracts/document`` answered 500 with
    ``Read-only file system: '/app/var'``. The stack reported twelve services up
    throughout, because the failure lands on the first upload rather than at boot.

    Nothing else could see it. ``Settings`` accepts a relative store in development by
    design, ``test_the_example_env_file_loads`` only asks whether the copy boots, and
    the compose gate never uploads a document. Only the pairing is wrong — the example
    and the defaults each being defensible is exactly why it survived.

    Read against the Compose file rather than against a list, so a second mounted
    setting is held to the same rule.
    """
    mounted = _mounted_defaults()
    assert mounted, (
        "no interpolated default is a path the stack mounts: that is the parser, not "
        "docker-compose.yml"
    )

    values = _example_env_values()
    overriding = {key: values[key] for key in mounted if key in values}

    assert not overriding, (
        f"`.env.example` overrides {sorted(overriding)}, so every service is handed that "
        "value instead of the path it mounts. A container with `read_only: true` cannot "
        "create what a relative path resolves to, and the failure lands on the first "
        "upload while `docker compose ps` still shows a healthy stack"
    )


# --- the directory the image has to own, not just mount ---------------------

ROOT = Path(__file__).resolve().parents[2]

#: Dockerfile -> its path, for every image this repository builds. Named rather than
#: discovered by globbing `Dockerfile*`, which would also pick up
#: `.agent-cache/Dockerfile.smoke` — an image no service runs.
DOCKERFILES = {name: ROOT / name for name in ("Dockerfile.backend", "Dockerfile.frontend")}

#: ``chown [-flags] owner path`` — the owner and the one path it names.
CHOWN = re.compile(r"\bchown\s+(?:-\S+\s+)*(\S+)\s+(/\S*)")


def _declared_volumes() -> set[str]:
    """The volume names the Compose file declares at the top level.

    Needed to tell a named volume from a bind mount: ``documents:/x`` and ``./ops:/x``
    are the same shape, and only the first is created empty and owned by ``root``.
    """
    names: set[str] = set()
    in_volumes = False
    for line in COMPOSE_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            in_volumes = line.strip() == "volumes:"
            continue
        if not in_volumes:
            continue
        header = re.fullmatch(r"  ([\w-]+):", line)
        if header is not None:
            names.add(header.group(1))
    return names


def _image_dockerfile(block: list[str]) -> str | None:
    """The Dockerfile one service is built from, or ``None`` for an image from a registry.

    Read off the service's own ``build:`` block (anchor included), which is what tells
    the two apart: MySQL is ``image: mysql:8.4`` and this repository builds nothing
    that could prepare ``/var/lib/mysql`` for it. A service that only names an
    ``image:`` is therefore out of scope for every test below rather than forgiven by
    a list of names.
    """
    for layer in _layers(block):
        for entry in _children(layer, "build:"):
            if entry.startswith("dockerfile:"):
                return entry.split(":", 1)[1].strip()
    return None


def _services_built_from(dockerfile: str) -> dict[str, list[str]]:
    """Service name -> its block, for the services built from ``dockerfile``.

    The scope has to be the image, not the file: MySQL mounts a named volume of its
    own, and demanding the *Python* image create ``/var/lib/mysql`` would be a test
    asserting something no one should do.
    """
    return {
        name: block
        for name, block in _compose_services().items()
        if _image_dockerfile(block) == dockerfile
    }


def _named_volume_mounts(dockerfile: str) -> dict[str, list[str]]:
    """Container path -> the services of this image mounting a *named* volume there."""
    declared = _declared_volumes()
    found: dict[str, list[str]] = {}
    for name, block in _services_built_from(dockerfile).items():
        for source, target in _volume_entries(block):
            if source in declared:
                found.setdefault(target, []).append(name)
    return found


def _privileged_lines(dockerfile: str) -> tuple[str, list[str]]:
    """``(runtime account, the lines that still run as root)``, read off the Dockerfile.

    The split is the ``USER`` directive, not the whole file: ``mkdir -p`` a directory
    and handing it to the runtime account needs root, so both have to happen *above*
    the line that gives root up. A ``chown`` written below it runs as ``app`` and
    would fail at build time — worth failing here instead, named.
    """
    lines = DOCKERFILES[dockerfile].read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        match = re.fullmatch(r"\s*USER\s+(\S+)", line)
        if match is not None:
            return match.group(1), lines[:index]
    return "", lines


def _owns(owners: dict[str, str], path: str, account: str) -> bool:
    """Whether some ``chown`` gives ``path`` — or a parent of it — to ``account``.

    A parent counts: ``chown -R app:app /var/lib/contract-agent`` covers everything
    created under it, and demanding the exact leaf be named would refuse a correct
    Dockerfile for being concise.
    """
    for target, owner in owners.items():
        if owner.split(":")[0] != account:
            continue
        if path == target or path.startswith(target.rstrip("/") + "/"):
            return True
    return False


@pytest.mark.parametrize("dockerfile", sorted(DOCKERFILES))
def test_every_named_volume_the_image_mounts_is_created_owned_by_its_user(
    dockerfile: str,
) -> None:
    """A right path into a directory the runtime account cannot write is still a 500.

    ``D10-127``: with the store path corrected, ``POST /api/v1/contracts/document``
    still answered 500, now with ``PermissionError: [Errno 13] Permission denied:
    '/var/lib/contract-agent/documents/f8'``. Compose mounted a named volume at a path
    the image never created, so Docker made the directory ``root:root 0755``, every
    service runs as ``app`` (uid 1001), and the store had never held a file.

    Neither earlier test could see it: the path is absolute and mounted, so the guard
    above passes, and the mount succeeds, so ``docker compose ps`` reports healthy.
    The defect is one layer below both — the *owner* of the mount point.

    A named volume is seeded from the image path it covers, ownership included, so the
    fix is to create the directory in the image and hand it over before ``USER``. Read
    out of the two files rather than named, so the next volume this image mounts is
    held to the rule the day it is written.

    Asked of every image this repository builds, not only the Python one: the
    frontend image mounts nothing today (its writable paths are tmpfs, which Docker
    owns and seeds), and running the guard over it anyway is what makes the *next*
    volume a compose file gains arrive with an owner.
    """
    services = _services_built_from(dockerfile)
    assert services, (
        f"no service in docker-compose.yml is built from {dockerfile}: either the "
        "compose file changed shape or this parser no longer reads it"
    )

    mounts = _named_volume_mounts(dockerfile)
    assert any(_named_volume_mounts(name) for name in DOCKERFILES), (
        "no service built from any image here mounts a named volume: the parse below "
        "would be asserting over an empty set"
    )

    account, as_root = _privileged_lines(dockerfile)
    assert account, (
        f"{dockerfile} never drops privileges with USER: the image would run as root, "
        "and every ownership question below is about a non-root runtime account"
    )

    privileged = "\n".join(as_root)
    entire = DOCKERFILES[dockerfile].read_text(encoding="utf-8")
    owners = {path: owner for line in as_root for owner, path in CHOWN.findall(line)}

    missing: dict[str, str] = {}
    for path, mounting in mounts.items():
        created = f"mkdir -p {path}"
        if created not in privileged:
            below_user = created in entire
            only_below = "only after `USER` gave root up" if below_user else "nowhere in the file"
            missing[path] = (
                f"is mounted by {sorted(mounting)} but is created {only_below}; a "
                "`chown` needs root, so the directory is made and handed over above "
                "`USER` or Docker seeds the volume root:root and the runtime account "
                "cannot write in it"
            )
        elif not _owns(owners, path, account):
            missing[path] = (
                f"is mounted by {sorted(mounting)} but no chown above `USER {account}` "
                f"gives it to {account}: {sorted(owners.items()) or 'nothing is owned'}"
            )

    assert not missing, (
        f"{dockerfile} does not prepare {sorted(missing)}: {missing}. A named volume is "
        "seeded from the image path it covers, so `mkdir -p` plus `chown` before `USER` "
        "is the only place this can be fixed"
    )


# --- the port the container opens, not just the port published --------------

#: ``EXPOSE 3000``, or ``EXPOSE 8000/tcp`` — the number is all that matters here.
EXPOSE = re.compile(r"^\s*EXPOSE\s+(\d+)", re.MULTILINE)


def _exposed_ports(dockerfile: str) -> set[str]:
    """The ports ``dockerfile`` declares the image listens on."""
    return set(EXPOSE.findall(DOCKERFILES[dockerfile].read_text(encoding="utf-8")))


def _published_container_ports(block: list[str]) -> list[str]:
    """The container-side port of each ``ports:`` entry: ``HOST:CONTAINER``, last field.

    ``"${API_PORT:-8000}:8000"`` and ``"127.0.0.1:${FRONTEND_PORT:-3000}:3000"`` both
    end in the port inside the container — the only half an image can be wrong about.
    """
    found: list[str] = []
    for layer in _layers(block):
        for entry in _children(layer, "ports:"):
            if not entry.startswith("- "):
                continue
            spec = entry[2:].strip().strip("'\"")
            found.append(spec.rsplit(":", 1)[-1].split("/")[0])
    return found


def test_every_published_port_is_one_the_image_actually_opens() -> None:
    """Publishing a port the process does not listen on is a green stack with no screen.

    ``D10-128`` adds the first service whose whole purpose is a port, and the mismatch
    it invites is invisible to everything else here: ``docker compose up`` reports the
    container healthy because nothing probes that port, and the only symptom is a
    connection refused in a browser. The image is asked which ports it opens; the
    compose file is asked which it publishes; they have to be the same number.

    A worker's ``9101`` is not ``EXPOSE``'d — all six metrics containers listen on the
    port their ``WORKER_METRICS_PORT`` names, so a value the service itself hands the
    process counts as the image answering too.
    """
    wrong: dict[str, str] = {}
    checked: set[str] = set()
    for name, block in _compose_services().items():
        dockerfile = _image_dockerfile(block)
        if dockerfile is None:
            continue  # an image from a registry: this repository does not build it
        exposed = _exposed_ports(dockerfile)
        configured = set(_environment(block).values())
        for port in _published_container_ports(block):
            checked.add(name)
            if port in exposed or port in configured:
                continue
            wrong[name] = (
                f"publishes container port {port}, but {dockerfile} opens "
                f"{sorted(exposed) or 'no port'} and the service configures "
                f"{sorted(configured) or 'nothing'}"
            )

    # Without this the whole test is a loop that may never run: `_image_dockerfile`
    # returning `None` for everything leaves `wrong` empty and the file green, which is
    # the failure mode `D10-127` was — a check that cannot notice it stopped reading.
    assert checked, (
        "no published port in docker-compose.yml belongs to a service built from an "
        "image here: either every service names a registry image or `_image_dockerfile` "
        "has stopped reading the `build:` blocks, and the check above proved nothing"
    )

    assert not wrong, (
        f"{sorted(wrong)}: {wrong}. A published port no process listens on is a "
        "reachable-looking stack that answers nothing, and no probe in this repository "
        "reads it"
    )


# --- the address a service is told to call, not just that it was told ---------

#: ``http://host:port`` — the shape a rewrite destination resolves to.
PROXY_URL = re.compile(r"https?://([A-Za-z0-9_.-]+):(\d+)")


def _proxy_build_arguments() -> dict[str, str]:
    """Service name -> the ``API_PROXY_URL`` that service's *build* is handed."""
    supplied: dict[str, str] = {}
    for name, block in _compose_services().items():
        arguments = _build_args(block)
        if "API_PROXY_URL" in arguments:
            supplied[name] = arguments["API_PROXY_URL"]
    return supplied


def test_every_service_that_proxies_to_the_api_names_a_service_that_exists() -> None:
    """``127.0.0.1`` inside a container is that container, not the one behind it.

    Two ways this value can be wrong, and only one of them is an address:

    * It is handed to the **build**, not to the container. ``D10-128`` measured this: a
      frontend whose ``environment:`` said ``http://api:8000`` proxied to
      ``127.0.0.1:8000`` and logged ``ECONNREFUSED``, because ``next build`` writes the
      destination into ``.next/routes-manifest.json`` and the standalone server replays
      that file. The same line in ``environment:`` therefore reads as configuration and
      is inert, so this reads ``build.args`` and nothing else — a guard aimed at
      ``environment:`` would pass on the stack that was broken.
    * The host has to name a Compose service, and the port has to be one that service
      opens. Otherwise every screen renders and every call answers 502 through a
      container whose healthcheck passes, because the healthcheck asks the Next server
      for a page and gets one — the same shape as ``D10-127``: correct-looking
      configuration, no consumer that can notice.

    Loopback is not special-cased into a clearer message: it is simply not a service
    name, so it fails here by default rather than by a list of strings someone has to
    remember to extend.
    """
    services = _compose_services()
    proxied = _proxy_build_arguments()
    assert proxied, (
        "no service is handed API_PROXY_URL as a build argument: `next build` takes the "
        "fallback in next.config.ts and every screen answers 502. It belongs in "
        "`build.args` and not `environment:` — the destination is resolved when the "
        "image is built, so a runtime copy changes nothing"
    )

    wrong: dict[str, str] = {}
    for name, address in proxied.items():
        match = PROXY_URL.fullmatch(address)
        if match is None:
            wrong[name] = f"API_PROXY_URL {address!r} is not an http(s) host and port"
            continue
        host, port = match.groups()
        if host not in services:
            loopback = (
                " (inside this container, that is this container)"
                if host
                in {
                    "127.0.0.1",
                    "localhost",
                    "::1",
                }
                else ""
            )
            wrong[name] = f"API_PROXY_URL names {host!r}, which is not a service here{loopback}"
            continue
        target = _image_dockerfile(services[host])
        if target is not None and port not in _exposed_ports(target):
            wrong[name] = (
                f"API_PROXY_URL names port {port} on {host!r}, but {target} opens "
                f"{sorted(_exposed_ports(target)) or 'no port'}"
            )

    assert not wrong, (
        f"{sorted(wrong)}: {wrong}. Docker resolves this name per request, so a wrong "
        "address is a 502 behind a healthy container rather than a failure to start"
    )


# --- the argument the image has to declare to be given ----------------------

#: ``ARG API_PROXY_URL`` — the only thing that lets ``--build-arg`` reach a build.
PROXY_ARGUMENT = re.compile(r"^ARG\s+API_PROXY_URL\b", re.MULTILINE)

#: The command that fixes the rewrite destination: ``next build`` reads the config.
NEXT_BUILD = re.compile(r"^RUN\s+npm run build\s*$", re.MULTILINE)


def test_the_frontend_image_accepts_the_proxy_argument_before_it_builds() -> None:
    """A ``build.args`` entry the image never declares is dropped without a word.

    Docker passes a build argument to the build only for names the ``Dockerfile``
    declares, so the two halves have to be checked together: Compose naming the API and
    the image never accepting the value leaves ``next build`` with the fallback and the
    Compose file reading as correct. That is the ``D10-127`` shape exactly — a value
    written down in the right place and read by nothing.

    The ``ENV`` is required as well. A declared ``ARG`` *is* visible to a later ``RUN``,
    but that is a Docker rule to remember rather than a line in this file to read, and
    the build reads the value as an environment variable.
    """
    frontend = DOCKERFILES["Dockerfile.frontend"].read_text(encoding="utf-8")

    argument = PROXY_ARGUMENT.search(frontend)
    assert argument is not None, (
        "Dockerfile.frontend never declares `ARG API_PROXY_URL`: the value Compose hands "
        "it in `build.args` is silently discarded and `next build` bakes in the fallback"
    )
    build = NEXT_BUILD.search(frontend)
    assert build is not None, (
        "Dockerfile.frontend no longer runs `RUN npm run build` on a line of its own: "
        "this guard cannot tell whether the argument arrives before the build that reads "
        "it, so it stops asserting rather than passing"
    )
    assert argument.start() < build.start(), (
        "Dockerfile.frontend declares `ARG API_PROXY_URL` after `RUN npm run build`: the "
        "destination is fixed by that command, so the argument arrives too late and the "
        "fallback is what gets baked in"
    )
    assert re.search(r"^ENV\s+API_PROXY_URL=", frontend, re.MULTILINE), (
        "Dockerfile.frontend never promotes `ARG API_PROXY_URL` to an `ENV` before the "
        "build, so nothing in the file shows the value reaching `process.env`"
    )


# --- the address the image binds, and the files a page needs to hydrate ------

#: ``HOSTNAME=<address>`` as `Dockerfile.frontend` spells it: one word of an `ENV`.
HOSTNAME = re.compile(r"^\s*HOSTNAME=(\S+)\s*$", re.MULTILINE)

#: The loopback spellings Node's `--host` accepts, none of which another container
#: can reach. Compared literally: this asks "could the Compose network dial it", and
#: the only defence against adding a fourth spelling is that it is not on this list.
LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def test_the_frontend_image_is_built_to_be_reachable_and_hydrated() -> None:
    """Two ways this image builds cleanly and serves a broken screen.

    Neither is a build failure, and the second is the one that looks fine:

    * The standalone server binds ``localhost`` unless ``HOSTNAME`` says otherwise, so
      an image that forgets it answers on its own loopback and nowhere else — 502 from
      every other container while the process reports itself healthy and its probe
      (which runs *inside* that container) passes.
    * ``next build`` leaves the client bundles in ``.next/static``, which is **not**
      inside the ``standalone`` tree it also writes. Copy only ``standalone`` and every
      page returns HTML whose every script is a 404: it renders, it never hydrates, and
      no server-side check notices because the HTML itself was served correctly.

    Both are read off the files rather than restated here, so the guard follows the
    Dockerfile if either line moves.
    """
    frontend = DOCKERFILES["Dockerfile.frontend"].read_text(encoding="utf-8")

    found = HOSTNAME.findall(frontend)
    assert found, (
        "Dockerfile.frontend never sets HOSTNAME: the standalone server binds "
        "localhost and no other container can reach the screen"
    )
    bindable = [address for address in found if address not in LOOPBACK]
    assert bindable, (
        f"Dockerfile.frontend binds {found}, which every other container reads as "
        "itself: the frontend has to answer on an address the Compose network reaches"
    )

    config = (ROOT / "frontend" / "next.config.ts").read_text(encoding="utf-8")
    assert re.search(r'^\s*output:\s*"standalone"', config, re.MULTILINE), (
        'frontend/next.config.ts does not set `output: "standalone"`: `next build` '
        "then writes no server.js and Dockerfile.frontend's runtime stage has nothing "
        "to copy"
    )
    static_copy = re.search(r"^COPY\b.*\.next/static\b.*$", frontend, re.MULTILINE)
    assert static_copy, (
        "Dockerfile.frontend never copies `.next/static`: the standalone tree does not "
        "contain it, so every page would serve HTML whose scripts all 404"
    )
