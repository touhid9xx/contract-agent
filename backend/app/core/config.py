"""Typed, fail-fast application settings.

Configuration is read once from the process environment (plus an optional local
``.env``) and validated eagerly. Unsafe or contradictory configuration raises at
start-up rather than surfacing later as a half-working service — the spec's
fail-fast requirement. Nothing here ever logs a secret value.
"""

from __future__ import annotations

import base64
import ssl
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["development", "test", "staging", "production"]
#: Transport the outbox relay publishes through. ``none`` keeps the refusing
#: stand-in, so events retry and then park as failed instead of being dropped.
EventTransport = Literal["none", "kafka"]
#: Which backend reads field values out of a contract document. ``pattern`` is
#: deterministic and offline; ``ollama`` adds a local LLM for the fields a regex
#: cannot reach. There is deliberately no cloud member: "no cloud LLM" is a
#: non-negotiable, so it is unrepresentable rather than merely discouraged.
ExtractionBackend = Literal["pattern", "ollama"]
#: Transport a notification is sent through. ``none`` keeps the refusing
#: stand-in, so a dispatch records an attempt and fails visibly instead of
#: pretending a message left the building. ``smtp`` selects
#: :class:`~app.workers.senders.SmtpSender`, and it exists only because that
#: adapter does: a transport is never named here ahead of the code that
#: implements it, which is why there is still no ``sms`` or ``voice`` member.
NotificationTransport = Literal["none", "smtp"]

#: The value shipped in .env.example. Never valid in staging/production.
#: Not a credential: production validation below refuses to start when it is
#: still set, which is why the hardcoded-password warning is suppressed.
DEV_SECRET_PLACEHOLDER = "dev-only-insecure-secret-key-change-me"  # noqa: S105  # nosec B105

MIN_PRODUCTION_SECRET_LENGTH = 32
#: How many retired encryption keys may be carried at once. A rotation that is
#: finished carries none, so this only bounds the window between "new key in
#: place" and "`reseal-fields` has rewritten every row" — and every decrypt walks
#: the list, so an unbounded one would be an unbounded cost on the send path.
MAX_RETIRED_ENCRYPTION_KEYS = 4
_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
#: Hosts considered non-public. Used to *detect* a development origin, never to bind.
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")  # nosec B104


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- runtime ---------------------------------------------------------
    app_env: AppEnv = "development"
    app_name: str = "contract-expiry-notification-agent"
    app_version: str = "0.1.0"
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    #: ``None`` means "decide from app_env": JSON in staging/production.
    log_json: bool | None = None

    # --- security --------------------------------------------------------
    secret_key: str = Field(default=DEV_SECRET_PLACEHOLDER, min_length=16)
    access_token_expire_minutes: int = Field(default=15, gt=0, le=120)
    refresh_token_expire_days: int = Field(default=7, gt=0, le=30)
    encryption_key: str = ""
    #: Keys this installation used *before* the current one, comma-separated, in
    #: the order they were retired. They are decrypt-only: nothing is ever sealed
    #: with one, so rotation is "add the old key here, reseal, then clear this".
    #: Empty is the steady state, not a degraded one.
    encryption_key_previous: str = ""

    # --- password hashing (Argon2id) -------------------------------------
    #: The defaults are argon2-cffi's recommendations and comfortably exceed the
    #: OWASP Argon2id minimum (19 MiB, t=2, p=1). The lower bounds are enforced
    #: here so a weakened production configuration fails at start-up rather than
    #: quietly becoming cheap to brute-force. Tests lower them deliberately.
    argon2_time_cost: int = Field(default=3, ge=1, le=16)
    argon2_memory_cost: int = Field(default=65536, ge=8192, le=4_194_304)
    argon2_parallelism: int = Field(default=4, ge=1, le=16)

    # --- infrastructure --------------------------------------------------
    database_url: str = "mysql+pymysql://app:app@localhost:3306/contracts"
    db_pool_size: int = Field(default=20, gt=0, le=200)
    db_max_overflow: int = Field(default=10, ge=0, le=200)
    db_pool_recycle_seconds: int = Field(default=1800, gt=0)
    db_echo: bool = False
    redis_url: str = "redis://localhost:6379/0"
    kafka_bootstrap_servers: str = "localhost:9092"
    ollama_base_url: str = "http://localhost:11434"

    # --- event transport (F4) --------------------------------------------
    #: Read by the relay process only. The API shares this settings object but
    #: never publishes, so selecting a transport is not required for it — which
    #: is why this is not enforced in staging/production (docker-compose.yml
    #: runs both processes from one image with one environment).
    event_transport: EventTransport = "none"
    kafka_client_id: str = "contract-agent-outbox-relay"
    #: Upper bound on a single publish. Delivery is acknowledged per event, so
    #: this must stay below ``outbox_lease_seconds``: a slow broker would
    #: otherwise let the lease expire mid-batch and a second relay republishes.
    kafka_publish_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    kafka_produce_retries: int = Field(default=3, ge=0, le=20)

    # --- outbox relay ----------------------------------------------------
    #: Rows claimed per relay tick. Bounded so one tick cannot starve the loop.
    outbox_batch_size: int = Field(default=50, gt=0, le=1000)
    #: How long a claimed batch stays owned by one relay before another may
    #: reclaim it. Must exceed the worst-case publish latency of a batch.
    outbox_lease_seconds: int = Field(default=120, gt=0, le=3600)
    #: Retries before a row is parked in the dead-letter state for an operator.
    outbox_max_attempts: int = Field(default=10, gt=0, le=100)
    outbox_poll_interval_seconds: float = Field(default=0.5, gt=0, le=60)

    # --- readiness (F5) --------------------------------------------------
    #: Upper bound on one dependency check in ``/health/ready``. Readiness has to
    #: answer or fail quickly: an orchestrator that receives no answer cannot
    #: distinguish "overloaded" from "dead", and keeps routing traffic to a
    #: wedged process. The same value bounds the Kafka admin client's request
    #: timeout, since a worker thread cannot be cancelled from outside.
    readiness_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    # --- extraction (F7) -------------------------------------------------
    extraction_backend: ExtractionBackend = "pattern"
    #: Local model name, read only by the Ollama backend.
    extraction_model: str = "llama3.1:8b"
    extraction_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    #: Confidence at or above which a field may skip human review. Below it the
    #: field is written ``pending_review`` and blocks the notification pipeline.
    #: 0.8 matches the threshold the Phase-0 review UI already renders against.
    extraction_review_threshold: float = Field(default=0.8, gt=0, le=1)
    #: Upper bound on the document text handed to a backend, in characters.
    extraction_max_document_chars: int = Field(default=20_000, gt=0, le=200_000)

    # --- notification dispatch (F10) -------------------------------------
    notification_transport: NotificationTransport = "none"
    #: Quiet hours, as **local wall-clock hours in the contract's own time zone**
    #: (``Contract.timezone``), never UTC: 08:00 means 08:00 where the customer
    #: is, which is the only reading that protects them from a call at 03:00.
    #:
    #: The window is half-open — ``[start, end)`` — and wraps past midnight when
    #: ``start > end`` (the normal case: 20:00 up to, not including, 08:00).
    #: ``end`` equal to ``start`` is refused below, because a zero-length window
    #: would switch quiet hours off entirely while looking configured, and the
    #: contact policy treats quiet hours as a rule rather than an option.
    quiet_hours_start: int = Field(default=20, ge=0, le=23)
    quiet_hours_end: int = Field(default=8, ge=0, le=23)
    #: Dispatch attempts before a row is parked ``failed`` for an operator. The
    #: same role ``outbox_max_attempts`` plays for a parked event row: a retry
    #: loop has to be bounded, and a bound has to be visible.
    #:
    #: The floor is 1, not 0. ``dispatch_notification`` parks the row when
    #: ``attempts >= notification_retry_max`` *before* the transport is reached, so
    #: 0 would park every row on its first attempt and make a send impossible — a
    #: configuration that looks like "no retries" and behaves like "never".
    notification_retry_max: int = Field(default=3, ge=1, le=20)
    #: Rows the scheduler claims per tick. Each row is its own transaction, so this
    #: bounds the work of one tick, not the size of a lock.
    notification_batch_size: int = Field(default=50, gt=0, le=1000)
    #: How long the scheduler waits after draining the due queue. Kept well under a
    #: minute so a quiet-hours deferral resumes promptly once its window opens; a
    #: row deferred to 08:00 customer time would otherwise wait for an arbitrary
    #: sweep interval to be noticed.
    notification_poll_interval_seconds: float = Field(default=5.0, gt=0, le=300)

    # --- smtp transport --------------------------------------------------
    # Read only when ``notification_transport == "smtp"``. ``smtp_host`` is the
    # one field with no usable default: every other setting below has a sane
    # value for a submission relay, and a relay with no host is not a transport.
    smtp_host: str = ""
    #: 587 is the submission port — the one that expects STARTTLS. 465 (implicit
    #: TLS) is deliberately not supported: the client has to pick ``SMTP_SSL``
    #: before the first byte, so offering both would mean two connection shapes
    #: behind one transport name, which is a setting most deployments would get
    #: wrong exactly once and only in production.
    smtp_port: int = Field(default=587, gt=0, le=65535)
    #: The envelope sender (``MAIL FROM``, and the ``From:`` header). Required
    #: for ``smtp``: a relay rejects a session with no sender, and without this
    #: check that rejection would arrive at the first real send instead of at
    #: start-up.
    smtp_from: str = ""
    smtp_username: str = ""
    #: Never logged and never returned. A failed login is reported from the
    #: relay's own response text, which the adapter scrubs before it is stored.
    smtp_password: str = ""
    #: STARTTLS. On by default and refused as ``False`` in staging/production —
    #: without it the credentials below cross the network in clear text.
    smtp_starttls: bool = True
    #: Upper bound on ONE SMTP conversation: connect, TLS, auth and ``DATA``.
    #:
    #: This is a correctness bound rather than a tuning knob. ``smtplib`` applies
    #: it as the socket timeout, so a relay that accepts the connection and then
    #: says nothing cannot hold the caller open. That matters because the
    #: scheduler sends *while it holds the row lock*: ``claim_next_due`` takes
    #: ``FOR UPDATE`` and the transaction commits only after ``send`` returns, so
    #: an unbounded send would pin the locked row — and the serial tick loop
    #: behind it — for as long as the provider felt like it.
    smtp_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    #: PEM bundle of the CAs that may vouch for the relay. Empty means the
    #: operating system's trust store, which is right for a public relay and for
    #: a relay whose certificate chains to a public CA.
    #:
    #: It exists because ``smtplib``'s own default is not safe. ``starttls()``
    #: called without a ``context`` builds ``ssl._create_stdlib_context()``, whose
    #: ``verify_mode`` is ``CERT_NONE`` and whose ``check_hostname`` is ``False``:
    #: the session is encrypted, but a machine in the middle of the connection is
    #: accepted without a word, and the credentials below are handed to it. The
    #: adapter therefore always passes an explicit *verifying* context, and this
    #: setting is how an internal relay with a private CA is trusted instead of
    #: switching verification off.
    smtp_ca_bundle: str = ""

    # --- http ------------------------------------------------------------
    cors_origins: str = "http://localhost:3000"

    # --- validators ------------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in _VALID_LOG_LEVELS:
            allowed = ", ".join(sorted(_VALID_LOG_LEVELS))
            raise ValueError(f"LOG_LEVEL must be one of {allowed}; got {value!r}")
        return level

    @field_validator("api_v1_prefix")
    @classmethod
    def _require_leading_slash(cls, value: str) -> str:
        # Deliberately does not normalise: silently rewriting a misconfigured
        # prefix would hide the mistake. The trailing-slash rule lives in the
        # model validator below so both faults are reported the same way.
        if not value.startswith("/"):
            raise ValueError(f"API_V1_PREFIX must start with '/'; got {value!r}")
        return value

    @field_validator("cors_origins")
    @classmethod
    def _reject_wildcard_cors(cls, value: str) -> str:
        # A wildcard origin combined with credentials is silently ignored by
        # browsers and hides a genuine misconfiguration. Refuse it outright.
        if "*" in value:
            raise ValueError(
                "CORS_ORIGINS must list explicit origins; '*' is not permitted "
                "because credentialed requests are enabled"
            )
        return value

    @model_validator(mode="after")
    def _validate_consistency(self) -> Settings:
        if self.api_v1_prefix.endswith("/"):
            raise ValueError("API_V1_PREFIX must not end with '/'")

        # "access token must expire before the refresh token" is guaranteed
        # structurally by the Field bounds (<=120 minutes vs >=1 day), so no
        # cross-field check is needed; adding one would be unreachable code.

        if self.event_transport == "kafka":
            if not self.kafka_servers:
                raise ValueError(
                    "KAFKA_BOOTSTRAP_SERVERS must list at least one host when EVENT_TRANSPORT=kafka"
                )
            if self.kafka_publish_timeout_seconds >= self.outbox_lease_seconds:
                raise ValueError(
                    "KAFKA_PUBLISH_TIMEOUT_SECONDS must be below OUTBOX_LEASE_SECONDS; "
                    f"got {self.kafka_publish_timeout_seconds} vs "
                    f"{self.outbox_lease_seconds}. A publish that outlives the lease "
                    "lets another relay reclaim the row and publish it twice."
                )

        # A zero-length window would read as "quiet hours configured" while
        # switching them off, so the contact policy refuses the shape rather
        # than silently sending at 03:00. ``start > end`` is the legal way to
        # describe a window that wraps midnight.
        if self.quiet_hours_start == self.quiet_hours_end:
            raise ValueError(
                "QUIET_HOURS_START and QUIET_HOURS_END must differ; use "
                "start > end (e.g. 20 and 8) for a window that wraps midnight"
            )

        # An SMTP relay missing any of these three would read as "configured"
        # while failing at the first real send — which, in the scheduler, means
        # failing *inside* the row lock rather than at start-up. Each is a boot
        # error instead.
        if self.notification_transport == "smtp":
            if not self.smtp_host.strip():
                raise ValueError("SMTP_HOST must be set when NOTIFICATION_TRANSPORT=smtp")
            if not _is_plausible_address(self.smtp_from):
                raise ValueError(
                    "SMTP_FROM must be an e-mail address when "
                    f"NOTIFICATION_TRANSPORT=smtp; got {self.smtp_from!r}"
                )
            if bool(self.smtp_username) != bool(self.smtp_password):
                raise ValueError(
                    "SMTP_USERNAME and SMTP_PASSWORD must be set together; a relay "
                    "that wants credentials needs both, and one half would only "
                    "surface as a failed login at the first send"
                )
            # Trusting a CA that is never consulted reads as "TLS configured"
            # while the session is not even encrypted, so the pair is refused
            # rather than silently half-applied. Checked before the bundle is
            # parsed: when both are wrong, "this setting does nothing" is the
            # fix the operator needs, not a complaint about the file it names.
            if self.smtp_ca_bundle and not self.smtp_starttls:
                raise ValueError(
                    "SMTP_CA_BUNDLE has no effect with SMTP_STARTTLS=false: the "
                    "session is not encrypted, so no certificate is verified"
                )
            # A CA bundle that cannot be loaded would otherwise be discovered when
            # the first context is built — inside the row lock, on the send path.
            # Checked here so a mistyped path, or an empty or truncated file, is a
            # boot error like the rest.
            if self.smtp_ca_bundle:
                bundle = Path(self.smtp_ca_bundle)
                if not bundle.is_file():
                    raise ValueError(
                        "SMTP_CA_BUNDLE must name a readable PEM bundle when set; "
                        f"got {self.smtp_ca_bundle!r}. Leave it empty to use the "
                        "system trust store."
                    )
                # Existing is not the same as loadable: an empty or non-PEM file
                # passes ``is_file`` and then makes ``ssl`` raise when the context
                # is built. Parsed here so the failure lands at start-up rather
                # than at the first send, which would be under the row lock and
                # look like a relay fault.
                try:
                    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_verify_locations(
                        cafile=str(bundle)
                    )
                except (ssl.SSLError, OSError) as exc:
                    raise ValueError(
                        "SMTP_CA_BUNDLE must be a PEM file ssl can load; "
                        f"got {self.smtp_ca_bundle!r}: {exc}"
                    ) from exc

        # A malformed key is worse than a missing one: it fails at the first
        # encrypt rather than at start-up, which is a write path, not a boot.
        # Empty stays legal here because the API process never stores field
        # values; ``build_cipher`` refuses at the point of use instead.
        if self.encryption_key and not self.fernet_key_is_valid:
            raise ValueError(
                "ENCRYPTION_KEY must be a 32-byte URL-safe base64 value; generate "
                'one with: python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )

        # Retired keys are decrypt-only, so a bad one does not corrupt a write --
        # it turns a *readable* row into a 503 or a suppressed notification, which
        # is the failure this whole setting exists to prevent. Validated at boot
        # for that reason, and every complaint names the offending position rather
        # than the value: a key is a secret even when it is the old one.
        retired = self.retired_encryption_keys
        if len(retired) > MAX_RETIRED_ENCRYPTION_KEYS:
            raise ValueError(
                "ENCRYPTION_KEY_PREVIOUS lists "
                f"{len(retired)} keys; at most {MAX_RETIRED_ENCRYPTION_KEYS} may be "
                "carried at once. A finished rotation carries none -- run "
                "'python -m app.cli reseal-fields' and then clear it."
            )
        for position, retired_key in enumerate(retired, start=1):
            if not _is_fernet_key(retired_key):
                raise ValueError(
                    "every ENCRYPTION_KEY_PREVIOUS entry must be a 32-byte URL-safe "
                    f"base64 value; entry {position} is not. A malformed entry "
                    "cannot decrypt anything, so the rows it was meant to unseal "
                    "would stay unreadable while the setting looked configured."
                )
        if len(set(retired)) != len(retired):
            raise ValueError(
                "ENCRYPTION_KEY_PREVIOUS lists the same key twice; each retired key may appear once"
            )
        if self.encryption_key and self.encryption_key in retired:
            # A rotation to the key you already have is not a rotation, and carrying
            # it twice hides that the new key was never actually put in place.
            raise ValueError(
                "ENCRYPTION_KEY also appears in ENCRYPTION_KEY_PREVIOUS; if the "
                "values are equal there has been no rotation, and if they are "
                "meant to differ one of the two variables holds the wrong key"
            )
        if retired and not self.encryption_key:
            raise ValueError(
                "ENCRYPTION_KEY_PREVIOUS is set but ENCRYPTION_KEY is empty. "
                "Retired keys only decrypt; something has to be able to seal the "
                "resealed rows, so the current key is still required."
            )

        # Argon2 requires ``memory_cost >= 8 * parallelism``. The Field bounds
        # already make a violation unrepresentable -- memory is at least 8192 KiB
        # and parallelism at most 16, so even the strictest permitted shape gives
        # 512 KiB per lane -- and a model validator for it would be unreachable
        # code (see ``test_security`` for the guard on that arithmetic).

        if self.app_env in {"staging", "production"}:
            self._reject_insecure_production_defaults()

        return self

    def _reject_insecure_production_defaults(self) -> None:
        if self.secret_key == DEV_SECRET_PLACEHOLDER:
            raise ValueError(
                "SECRET_KEY still holds the development placeholder; set a real "
                f"secret in {self.app_env}"
            )
        if len(self.secret_key) < MIN_PRODUCTION_SECRET_LENGTH:
            raise ValueError(
                f"SECRET_KEY must be at least {MIN_PRODUCTION_SECRET_LENGTH} "
                f"characters in {self.app_env}; got {len(self.secret_key)}"
            )
        if self.log_json is False:
            raise ValueError("LOG_JSON must not be disabled in staging/production")
        # Loopback origins are refused above for the same reason: a production
        # shape that only works because nothing is listening should not boot.
        # Here the exposure is worse than a misconfiguration — the SMTP
        # credentials and the recipient's address would be sent in clear text.
        if self.notification_transport == "smtp" and not self.smtp_starttls:
            raise ValueError(
                "SMTP_STARTTLS must not be disabled in "
                f"{self.app_env}: the credentials and the recipient address would "
                "cross the network unencrypted"
            )
        insecure_origins = [origin for origin in self.cors_origin_list if _is_loopback(origin)]
        if insecure_origins:
            raise ValueError(
                "CORS_ORIGINS must not contain loopback development origins in "
                f"{self.app_env}: {insecure_origins}"
            )

    # --- derived values --------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def is_test(self) -> bool:
        return self.app_env == "test"

    @property
    def json_logs(self) -> bool:
        """Whether to emit JSON logs. Defaults on for staging/production."""
        if self.log_json is not None:
            return self.log_json
        return self.app_env in {"staging", "production"}

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def kafka_servers(self) -> list[str]:
        return [s.strip() for s in self.kafka_bootstrap_servers.split(",") if s.strip()]

    @property
    def has_encryption_key(self) -> bool:
        return bool(self.encryption_key)

    @property
    def fernet_key_is_valid(self) -> bool:
        """Check ``ENCRYPTION_KEY`` is a 32-byte URL-safe base64 value.

        Fernet itself is only imported where it is used (PII columns), so the
        shape is validated here without pulling ``cryptography`` into every
        process that merely loads settings.
        """
        return _is_fernet_key(self.encryption_key)

    @property
    def retired_encryption_keys(self) -> list[str]:
        """``ENCRYPTION_KEY_PREVIOUS`` as a list, oldest entry first.

        Comma-separated rather than repeated, matching ``CORS_ORIGINS`` and
        ``KAFKA_BOOTSTRAP_SERVERS``: one variable, one line in ``.env``, and no
        ordering convention an operator has to guess at. Order does not change
        the result (every entry is tried until one works), but keeping the
        retirement order makes "which key is this?" answerable by eye.
        """
        return [key.strip() for key in self.encryption_key_previous.split(",") if key.strip()]


def _is_fernet_key(value: str) -> bool:
    """Whether ``value`` has the shape of a Fernet key (32 bytes, URL-safe base64).

    A shape check, not an import of ``cryptography``: settings are loaded by
    processes that never touch a PII column, and the point here is only to catch
    a typo at boot instead of at the first encrypt or decrypt.
    """
    if not value:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return False
    return len(decoded) == 32


def _is_plausible_address(value: str) -> bool:
    """A shape check on ``SMTP_FROM``, not an RFC 5322 parse.

    Start-up validation is here to catch "nobody set this" and "somebody pasted a
    display name", not to re-implement ``email.headerregistry``. A domain with no
    dot is refused because it cannot be a deliverable public sender, and a value
    containing whitespace is refused because that is the display-name mistake
    (``Ops Team <ops@example.com>``) which the envelope sender cannot hold.
    """
    candidate = value.strip()
    if not candidate or any(char.isspace() for char in candidate):
        return False
    local, _, domain = candidate.partition("@")
    return bool(local) and "." in domain and not domain.startswith(".") and not domain.endswith(".")


def _is_loopback(origin: str) -> bool:
    host = origin.split("://", 1)[-1].split("/", 1)[0].rsplit(":", 1)[0]
    return host in _LOOPBACK_HOSTS


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton (validated on first call)."""
    return Settings()
