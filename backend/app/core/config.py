"""Typed, fail-fast application settings.

Configuration is read once from the process environment (plus an optional local
``.env``) and validated eagerly. Unsafe or contradictory configuration raises at
start-up rather than surfacing later as a half-working service — the spec's
fail-fast requirement. Nothing here ever logs a secret value.
"""

from __future__ import annotations

import base64
import ipaddress
import ssl
from datetime import timedelta
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.db.enums import Channel

AppEnv = Literal["development", "test", "staging", "production"]
#: Transport the outbox relay publishes through. ``none`` keeps the refusing
#: stand-in, so events retry and then park as failed instead of being dropped.
EventTransport = Literal["none", "kafka"]
#: Which backend reads field values out of a contract document. ``pattern`` is
#: deterministic and offline; ``ollama`` adds a local LLM for the fields a regex
#: cannot reach. There is deliberately no cloud member: "no cloud LLM" is a
#: non-negotiable, so it is unrepresentable rather than merely discouraged.
ExtractionBackend = Literal["pattern", "ollama"]
#: Transport e-mail is sent through. ``none`` keeps the refusing stand-in, so a
#: dispatch records an attempt and fails visibly instead of pretending a message
#: left the building. ``smtp`` selects :class:`~app.workers.senders.SmtpSender`,
#: and it exists only because that adapter does: a transport is never named here
#: ahead of the code that implements it. Each channel gets its own switch below
#: rather than a member here, because the channels are independently deployable.
NotificationTransport = Literal["none", "smtp"]
#: Transport SMS is sent through, and a **second switch rather than a third
#: ``NotificationTransport`` member**. The two channels are independently
#: deployable — a relay with no gateway, a gateway with no relay — so one setting
#: could not describe both without inventing a precedence rule nobody asked for.
#: ``textbee`` selects :class:`~app.workers.senders.TextbeeSender`; as above, the
#: name exists only because the adapter does.
SmsTransport = Literal["none", "textbee"]
#: Transport a voice reminder is *placed* through, and a **third switch rather than
#: a ``NotificationTransport`` member**, for the same reason as :data:`SmsTransport`.
#: ``ari`` selects :class:`~app.workers.senders.AriVoiceSender`, which dials through
#: the Asterisk REST Interface; as above, the name exists only because the adapter
#: does. There is deliberately no ``twilio``-style member: the media side (what the
#: call says, and whether a human or a machine answered) belongs to the Stasis
#: application the call is handed to, not to this transport.
VoiceTransport = Literal["none", "ari"]
#: Which selector answers "is there a route that *reads* a customer's reply".
#: A third independent switch, because sending and hearing are separate capabilities:
#: ``SMS_TRANSPORT=textbee`` with ``INBOUND_TRANSPORT=none`` is a deployment that talks
#: and cannot listen. The name exists only because :mod:`app.api.v1.inbound` does.
InboundTransport = Literal["none", "textbee"]
#: Which selector answers "was this uploaded document examined for malware before it
#: was stored". A fourth independent switch, and the only one here that is not a
#: channel: a deployment can serve contracts without a scanner, exactly as it can send
#: without a gateway, and refusing to start over one would be a worse answer than the
#: behaviour it already has. ``clamd`` selects
#: :class:`~app.core.antivirus.ClamdScanner` and, as everywhere in this file, the name
#: exists only because the adapter does.
VirusScanTransport = Literal["none", "clamd"]
#: Which selector answers "which channel should we contact this customer on".
#: ``deterministic`` is the F9 contact policy and the default: it decides, and the
#: row records that it did. ``ollama`` adds the local model *on top of* it, as an
#: advisor that may only choose among the channels the policy already permitted
#: (P2-D). A hosted model is not a member — "no cloud LLM" is a non-negotiable, so
#: ``AGENT_BACKEND=openai`` is a start-up ``ValidationError``, not a code path.
AgentBackend = Literal["deterministic", "ollama"]

#: The value shipped in .env.example. Never valid in staging/production.
#: Not a credential: production validation below refuses to start when it is
#: still set, which is why the hardcoded-password warning is suppressed.
DEV_SECRET_PLACEHOLDER = "dev-only-insecure-secret-key-change-me"  # noqa: S105  # nosec B105

#: The two field keys `.env.example` ships, for the same reason and under the same
#: rule as the secret above: never valid in staging/production, where start-up
#: refuses them. They are here, and not only in the example, because the example is
#: a file a deployer copies — the refusal is what makes shipping them safe.
#:
#: The encryption key is *deliberately* not random: it is 32 zero bytes, url-safe
#: base64-encoded. A readable sentence encoded the same way is not enough — base64
#: spreads the bits, so ``base64url(b"dev-only-not-a-credential...")`` measures 4.77
#: bits of entropy per character and `generic-api-key` flags it in history, which is
#: how the first version of this placeholder was caught (CI run 43764587165). All
#: zero bytes measures 0.16: it is unmistakably filler, it is the value
#: ``test_encryption_key_shape_detection`` already uses to prove the shape check, and
#: a key generator that somehow produced zeros is refused in staging/production
#: rather than sealing customer data under it.
#:
#: They exist because ``ExtractionWorker`` builds its cipher and its blind index in
#: ``__init__`` — deliberately, so a deployment missing a key fails at start-up
#: instead of once per contract — which means a copy of the example with either one
#: empty crash-loops the ``extractor`` service before it reads a single document.
DEV_ENCRYPTION_KEY_PLACEHOLDER = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
DEV_BLIND_INDEX_KEY_PLACEHOLDER = "not-a-credential-dev-blind-index-key"

MIN_PRODUCTION_SECRET_LENGTH = 32
#: How many retired encryption keys may be carried at once. A rotation that is
#: finished carries none, so this only bounds the window between "new key in
#: place" and "`reseal-fields` has rewritten every row" — and every decrypt walks
#: the list, so an unbounded one would be an unbounded cost on the send path.
MAX_RETIRED_ENCRYPTION_KEYS = 4
#: Shortest ``CONTACT_BLIND_INDEX_KEY`` accepted. It lives here rather than in
#: :mod:`app.core.blind_index` because that module imports ``Settings`` from this one,
#: so the dependency only runs downhill: the floor is validated at boot and enforced
#: again (independently) by ``ContactBlindIndex.__init__`` at the point of use.
MIN_BLIND_INDEX_KEY_LENGTH = 32
#: Shortest ``INBOUND_WEBHOOK_SECRET`` accepted. The secret is the only thing between
#: an internet-facing endpoint and anyone who can guess a phone number, and a wrong
#: guess is answered by the same 202 a right one gets — so an attacker learns nothing
#: per attempt and the only cost of guessing is the length of this value.
#:
#: Reused verbatim as the floor for ``VOICE_OUTCOME_SECRET`` (``D10-154``). The name
#: predates the second signed webhook and is kept rather than generalised because the
#: ledgers already cite it by that spelling; the *number* is the claim that matters, and
#: it is the same claim: a signing key is the whole of the authentication.
MIN_INBOUND_SECRET_LENGTH = 32
#: The channels this build has a transport *factory* for at all — the ceiling that no
#: setting can raise.
#:
#: ``Channel.VOICE`` is a member, and that is the whole of what ``D10-144`` changed: a
#: dialler now exists (:class:`~app.workers.senders.AriVoiceSender`), so a queued voice
#: notification is no longer unsendable *by construction*. Whether it is deliverable is
#: then an ordinary configuration question answered by
#: :attr:`Settings.deliverable_notification_channels` — ``VOICE_TRANSPORT=none``, the
#: default, keeps it out of that set.
#:
#: The two facts stay separate because they are an operator's two different next moves —
#: one is fixed by an environment variable, the other by shipping code, or by approving a
#: value a channel can use. :func:`app.core.attention.attention_report` publishes them as
#: separate series for exactly that reason (``D10-129``).
#:
#: It is the same set :func:`app.workers.senders.build_sender` builds factories for, and
#: that function raises rather than accept a deliverable channel it cannot build, so a
#: channel added here without a sender fails at start-up instead of queueing silently.
NOTIFIABLE_CHANNELS = frozenset({Channel.EMAIL, Channel.SMS, Channel.VOICE})
_VALID_LOG_LEVELS = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"})
#: Hosts considered non-public. Used to *detect* a development origin, never to bind.
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")  # nosec B104
#: The token an ARI dial string must contain, formatted with the recipient. Named
#: rather than inlined so the setting's documentation, the validator below and
#: :class:`~app.workers.senders.AriVoiceSender` cannot disagree about its spelling.
ARI_NUMBER_PLACEHOLDER = "{number}"


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

    # --- login throttle (gap 9) ------------------------------------------
    #: Failed ``POST /auth/login`` attempts allowed against one
    #: ``(tenant slug, email)`` pair inside ``login_window_seconds`` before the
    #: next attempt is refused with ``429`` and a ``Retry-After``.
    #:
    #: This is *admission control*, not password policy: Argon2id makes each
    #: attempt expensive for the server, which is exactly why an unbounded number
    #: of them is a denial-of-service lever as much as a guessing lever.
    #:
    #: Five is the OWASP-style starting point for a lockout counter. The floor is
    #: 1, not 0, for the same reason ``notification_retry_max`` has a floor of 1:
    #: 0 would refuse the *first* attempt and make login impossible, a setting
    #: that reads as "strictest" and behaves as "broken".
    login_max_attempts: int = Field(default=5, ge=1, le=100)
    #: The window failures are counted over, and therefore also how long a
    #: throttled caller waits: the block lifts by itself once the oldest counted
    #: failure ages out, so there is no separate lockout timer to get stuck.
    #: 15 minutes is long enough to make guessing pointless and short enough that
    #: a user who fat-fingered their password is not locked out for the afternoon.
    login_window_seconds: int = Field(default=900, ge=1, le=86_400)

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

    # --- agent advisory (P2-D) -------------------------------------------
    #: Selects the channel chooser. See :data:`AgentBackend`: the deterministic
    #: policy always runs first, and it alone decides *whether* a customer may be
    #: contacted at all. The model can only rank what the policy permitted.
    agent_backend: AgentBackend = "deterministic"
    #: Local model the advisor asks. Pinned by tag, not by digest: the tag is what
    #: ``ollama pull`` in ``docker-compose.yml`` produces, and an unpinned name
    #: would let a re-pull change a decision nobody reviewed.
    agent_model: str = "qwen2:7b"
    #: Second model, tried **once** and only after the primary fails to answer at
    #: all (refused connection, timeout, non-JSON body). A shape failure is
    #: repaired against the same model instead: the small model is not better at
    #: reading the schema, and switching models mid-repair would make
    #: ``schema_repair_attempts`` describe two experiments at once.
    #: ``None`` disables the second attempt.
    agent_fallback_model: str | None = "qwen2:1.5b"
    #: Ceiling on one completion. Generous because ``qwen2:7b`` on CPU is slow and
    #: a timeout is a review item; the API request this runs inside is already
    #: bounded by the client. Alerting on p95 latency is P2-J's job, not this
    #: bound's, which only has to stop a wedged socket from holding a worker.
    agent_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    #: Shape repairs allowed before the answer is abandoned. Three matches the
    #: spec's repair loop; each attempt is counted in
    #: ``agent_decisions.schema_repair_attempts`` so a model that always needs
    #: three is visible in the data rather than in a log.
    agent_repair_max: int = Field(default=3, ge=0, le=10)
    #: Floor on the model's **self-reported** confidence. Below it the answer is
    #: still recorded — discarding it would leave no evidence — but the decision
    #: is flagged ``needs_review``, which blocks dispatch until a person clears it.
    #: 0.6 is deliberately not the extraction threshold (0.8): the two numbers
    #: bound different quantities, and reusing one would imply they are comparable.
    agent_min_confidence: float = Field(default=0.6, gt=0, le=1)

    # --- contract documents (gap 12) -------------------------------------
    #: Where ingested document bytes are kept.
    #:
    #: Configurable because both things that can go wrong here are local: the API
    #: container's root filesystem is read-only, so the store has to be a mounted
    #: volume, and a development checkout wants the bytes beside it rather than
    #: under ``/var/lib``. Relative in development, **absolute** in
    #: staging/production — enforced below, because the API, the workers and an
    #: operator's CLI do not share a working directory and a disagreement there
    #: presents as a stored document that cannot be found.
    document_store_path: str = Field(default="var/documents", min_length=1)
    #: Largest document accepted, in bytes, checked before anything is written.
    #:
    #: The cap is a correctness bound rather than a tuning knob: the store holds
    #: the file in memory whole (plaintext, then sealed), and an unbounded upload
    #: is a denial-of-service lever against a process that is also serving the
    #: API. 10 MiB clears a scanned contract with room to spare and stays far
    #: below anything that would trouble the host.
    document_max_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=100 * 1024 * 1024)

    # --- document scanning (P2-A) ----------------------------------------
    # P2-A described an upload as "magic-byte + ClamAV + sha256 dedup". Two of the
    # three shipped with gap 12 — the content-addressed key in
    # :mod:`app.core.documents` and the magic-byte reader in
    # :mod:`app.core.document_text`. This is the third. Unlike the other two it is a
    # *switch* rather than a rule, because it needs a daemon running beside the app,
    # and no setting here may ever claim a scan that did not happen.
    #: Whether an uploaded document is examined for malware before it is sealed.
    #:
    #: ``none`` is the default and it is the honest one: it describes exactly what this
    #: build did before the scanner existed, so an installation without a clamd keeps
    #: accepting the documents it always accepted rather than losing the feature. What
    #: ``none`` must never do is stand in for a verdict — an unscanned document is
    #: *unexamined*, not clean, and the module that owns the scan says so in one place
    #: rather than letting every caller invent its own wording.
    virus_scan_transport: VirusScanTransport = "none"
    #: Where clamd listens. TCP rather than the unix socket clamd also offers, because
    #: the two processes normally live in different containers, where a server's socket
    #: path names a file this container cannot open.
    #:
    #: The protocol is plain and unauthenticated — there is no TLS to enable — so the
    #: host is refused at start-up when it is a *public* address: that would send a
    #: customer's contract in clear text to a daemon with no password. A hostname is
    #: not resolved here (DNS at boot is its own failure mode); the rule applies to
    #: addresses written out literally, which is what a deployment pins.
    clamd_host: str = "127.0.0.1"
    clamd_port: int = Field(default=3310, ge=1, le=65535)
    #: Upper bound on one socket operation against clamd, not on the whole scan: a large
    #: but healthy scan keeps making progress and never trips it, while a daemon that
    #: accepts the connection and then says nothing is cut off here.
    #:
    #: This is also the worst case added to one upload's latency, since the scan runs
    #: inside the request (see :func:`app.api.v1.contracts.ingest_contract_document`).
    clamd_timeout_seconds: float = Field(default=30.0, gt=0, le=300)

    # --- pull-mode ingestion (P2-H) --------------------------------------
    # The upload routes ask a client to send a contract; the pull route names a URL
    # and lets *this* process open the connection instead. Every knob below is a
    # bound on that one outbound request, because a caller-chosen URL is a
    # caller-chosen destination for a socket this deployment owns.
    #: Upper bound on the whole conversation with a source: connect, TLS, headers,
    #: body. A pull runs inside a request, so an unbounded one would let a source
    #: that accepts the connection and then says nothing hold a worker open.
    pull_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    #: Largest body accepted, in bytes. The read stops one byte past this, so a
    #: source that lies about ``Content-Length`` still cannot stream unbounded
    #: bytes into the API process. 1 MiB is generous for contract metadata and far
    #: below anything that would trouble a host also serving other requests.
    pull_max_bytes: int = Field(default=1024 * 1024, gt=0, le=20 * 1024 * 1024)
    #: Contracts one pull may carry. Bounds the transaction, not a lock: every row
    #: in a pull commits together (see the route), so this is what keeps one call
    #: from writing an unbounded batch in a single transaction.
    pull_max_contracts: int = Field(default=100, gt=0, le=1000)
    #: Permit ``http`` and skip the "public address only" check.
    #:
    #: **Development and test only**, and refused outside them below. Its whole
    #: purpose is a local double — a fake company API on ``127.0.0.1`` — so the
    #: real transport can be exercised end to end without a certificate or a public
    #: hostname. A deployment that can be told to fetch ``http://10.0.0.7/`` is a
    #: deployment whose internal network is reachable by anyone who can reach the
    #: API, which is the request-forgery lever this feature would otherwise add.
    pull_allow_private_hosts: bool = False

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
    #: How long a notification may be due and still ``pending`` before the attention
    #: check calls it stuck (``D10-129``).
    #:
    #: A grace is not politeness, it is what makes the count mean anything. A row is
    #: ``pending`` from the moment ``scheduled_at`` passes until the scheduler's next
    #: claim, which is at most :attr:`notification_poll_interval_seconds`; counting
    #: that window would make a deployment doing its job look exactly like one whose
    #: scheduler had died, and the gauge would stand at one for ever on a quiet
    #: install. Fifteen minutes is three orders of magnitude more than the loop
    #: needs — the retry path parks a row as ``failed`` after
    #: :attr:`notification_retry_max` attempts at five-second intervals — and short
    #: enough that an operator learns about a stuck queue in the same working session.
    notification_overdue_grace_seconds: float = Field(default=900.0, gt=0, le=86_400)
    #: Seconds between attention checks (`python -m app.workers.attention_watch`).
    #: Five minutes, not five seconds: every tick reads three tables and the states
    #: it reports — a parked event, a suppressed notification, a plaintext field
    #: value — do not become more urgent between ticks. The ceiling is an hour
    #: because a longer interval makes the log line arrive after the operator who
    #: needed it has gone home.
    attention_poll_interval_seconds: float = Field(default=300.0, gt=0, le=3600)
    #: Extracted-field rows one attention check scans for unsealed values. The same
    #: bound ``doctor --limit`` takes, and the same stated consequence: a full page
    #: makes the reported count a floor rather than the table's total, which the
    #: report records as ``field_scan_truncated`` instead of rounding away. The
    #: default matches ``DEFAULT_RESEAL_LIMIT`` so the scheduled check and a hand
    #: run of ``doctor`` agree on what they looked at.
    attention_field_scan_limit: int = Field(default=500, gt=0, le=100_000)
    #: Where the attention watch mails its verdict when that verdict *changes*.
    #:
    #: Empty — the default — means "log only", which is exactly what the worker did
    #: before this existed. An address here puts a person on the other end: one
    #: message when a finding appears, one when the board is clear again, and
    #: nothing in between however long the state stands.
    #:
    #: It is **deployment configuration, not tenant data**, and that is the whole
    #: reason gap 22 was finished this way rather than with an HTTP route.
    #: :func:`~app.core.attention.attention_report` reads parked events, undelivered
    #: notifications and unsealed fields **across** tenants on purpose — each of
    #: those queries says tenant-scoping it would be misleading — while a route
    #: answers a *tenant* principal. This recipient is the person who runs the
    #: deployment, so no principal is impersonated and no count has to be scoped.
    #:
    #: The transport is the one ``NOTIFICATION_TRANSPORT`` already names: one relay,
    #: one set of credentials, one already-tested socket path. Setting this with no
    #: SMTP transport is refused at start-up rather than at the first finding.
    attention_alert_to: str = ""

    # --- escalation (gap 15) ---------------------------------------------
    #: Hours between an escalation being raised and its deadline. ``Q2`` in
    #: ``DECISIONS.md`` fixes the default at 48 h, counted from the moment the
    #: escalation is raised — **not** from the reminder that preceded it. The two
    #: clocks are different, and the Phase-0 demo splits them the same way (a
    #: two-week no-response window raises the escalation, then ``sla_hours: 48``).
    #:
    #: The floor is 1 because 0 would mark every escalation late the instant it
    #: opened, which looks like "no grace period" and behaves like "already
    #: failed". The ceiling is deliberately loose: this is a business clock an
    #: operator may legitimately stretch, not a security parameter.
    escalation_sla_hours: int = Field(default=48, ge=1, le=720)
    #: Days of silence after the last accepted contact before the scanner hands a
    #: contract to a human. This is the *other* clock: the window that decides
    #: whether an escalation is raised at all, as distinct from
    #: :attr:`escalation_sla_hours`, which starts counting once one is.
    #:
    #: Fourteen days matches the Phase-0 demo's two-week no-response queue and the
    #: ``T-14`` reminder milestone. The floor is 1 because 0 would escalate a
    #: contract in the same tick that contacted it.
    escalation_no_response_days: int = Field(default=14, ge=1, le=90)
    #: Contracts the scanner claims per tick. Unlike the notification scheduler this
    #: bounds one *transaction* (the claim and the writes it authorises must share a
    #: lock), so it also bounds how long the scanner holds row locks.
    escalation_scan_batch_size: int = Field(default=50, gt=0, le=1000)
    #: How long the scanner waits between passes. Five minutes, not five seconds:
    #: the window it measures is fourteen *days*, so a finer sweep would only add
    #: load without finding anything sooner.
    escalation_scan_poll_interval_seconds: float = Field(default=300.0, gt=0, le=3600)
    #: Where a *newly raised* escalation is mailed. Empty means log-only, which stays
    #: the default for the same reason :attr:`attention_alert_to` is empty by default:
    #: this is deployment configuration, not customer data, and a deployment that has
    #: not named a person to receive it should not silently believe one exists.
    #:
    #: The recipient is the person who runs the deployment, never the customer and
    #: never a tenant principal. What raises an escalation is precisely that the
    #: *customer* could not be reached, so the party who can act is the operator.
    #: The alert therefore reads none of the contact policy — no consent, no quiet
    #: hours, no template — because none of those gates govern mailing ourselves.
    #:
    #: It travels the one transport ``NOTIFICATION_TRANSPORT`` already names, so a
    #: deployment has one relay to configure and one credential to rotate.
    escalation_alert_to: str = ""

    # --- reminder planning ----------------------------------------------
    # The daily scan. Everything above *consumes* a notification row: the
    # scheduler claims due rows, the dispatcher sends one, the escalation scanner
    # measures silence after one. Until this worker existed nothing wrote those
    # rows except a client calling ``POST /api/v1/notifications`` per contract, so
    # "remind 60/30/14 days before expiry" was a promise the deployment had to keep
    # by hand. These two settings are the whole knob surface of the clock.
    #: Contracts the planner leases per query. Unlike the escalation scanner this
    #: bounds one *query*, not one transaction — see
    #: :meth:`app.workers.reminder_planner.ReminderPlanner.run_once` for why the
    #: planner's lease is over the contract rather than over the row it writes.
    reminder_scan_batch_size: int = Field(default=50, gt=0, le=1000)
    #: How long the planner waits between passes. Five minutes, matching the other
    #: two scanners: the milestones it measures are measured in *days*, so a finer
    #: sweep would add load without finding anything sooner. It is deliberately not
    #: a "once a day at 09:00" setting — a deployment that starts at noon still gets
    #: its reminders, and a tick missed to a restart is retried a few minutes later
    #: rather than a day later.
    reminder_scan_poll_interval_seconds: float = Field(default=300.0, gt=0, le=3600)

    # --- contract expiry --------------------------------------------------
    # The other half of the same clock. The planner stops *planning* once a contract
    # drops out of its window, but nothing moved the contract's status when its term
    # ran out, so an expired file stayed ``active`` for ever: still countable on a
    # dashboard about to be renewed, still scannable for escalation, still not
    # contactable-but-not-refused. D10-98 closes that. These are its only knobs.
    #: Contracts the sweep leases per query, the same size as the planner's. The work
    #: per leased row is heavier here — every genuinely expired row costs a `status`
    #: write and an audit row, where the planner's usually costs a read and a refusal —
    #: but this still bounds one statement, not how long a lock is held.
    expiry_sweep_batch_size: int = Field(default=50, gt=0, le=1000)
    #: How long the sweep waits between passes. Hourly, not five-minutely: the event
    #: it looks for happens once per contract, on a calendar day boundary, so a finer
    #: interval would add a query per five minutes to notice something that can never
    #: be more than an hour late. An hour also bounds the worst-case lag between a
    #: contract's term ending and its status saying so, which is the number an
    #: operator will ask about.
    expiry_sweep_poll_interval_seconds: float = Field(default=3600.0, gt=0, le=86400)

    # --- document extraction ----------------------------------------------
    # `extract_contract` has been complete since F7 and, until D10-120, reachable only
    # through ``python -m app.cli extract`` one contract at a time. These two settings
    # are the knob surface of the clock that replaced that: nothing else about the
    # extraction pipeline is configurable, because nothing else about it was a choice.
    #: Contracts the worker leases per query. Ten, where the two scanners use fifty: the
    #: per-row cost here is a document load off the volume, a decrypt and a parse of up
    #: to ``extraction_max_document_chars`` — the most expensive per-row work any worker
    #: in this codebase does — and a batch that takes a minute is indistinguishable from
    #: one that takes ten when the poll interval is a quarter of an hour. It bounds a
    #: statement, not a lock: the worker commits each contract as it finishes it.
    extraction_scan_batch_size: int = Field(default=10, gt=0, le=1000)
    #: How long the worker waits between passes. Fifteen minutes — longer than any other
    #: scanner here — because nothing downstream is waiting on it within the hour: a
    #: contract is extracted once, ever, and the reminders that read the result are
    #: planned days before its end date. The cost of being slow is a contract that sits
    #: with unextracted fields for a few minutes; the cost of being fast is re-reading
    #: the volume and re-parsing every document the deployment has just ingested. It is
    #: still a poll rather than a cron for the reason the planner gives: a deployment
    #: started at an arbitrary time must still do its work, and a tick lost to a restart
    #: must be retried in minutes rather than at the same hour tomorrow.
    extraction_scan_poll_interval_seconds: float = Field(default=900.0, gt=0, le=86400)

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

    # --- sms -------------------------------------------------------------
    #: Read only when ``sms_transport == "textbee"``. Its own switch rather than a
    #: ``NotificationTransport`` member; see the type alias above.
    sms_transport: SmsTransport = "none"
    #: The gateway credential. Never logged and never returned, and kept out of
    #: ``TextbeeSender.__repr__`` for the same reason ``smtp_password`` is.
    textbee_api_key: str = ""
    #: Which gateway device sends. It is a path segment of the request URL, so a
    #: wrong value is a 404 at the first send unless start-up refuses it.
    textbee_device_id: str = ""
    #: The gateway origin. Checked below to be https unless loopback: the request
    #: body carries the customer's phone number and the header carries the key.
    textbee_base_url: str = "https://api.textbee.dev"
    #: Upper bound on ONE gateway call, for the same correctness reason as
    #: ``smtp_timeout_seconds``: the scheduler sends while holding the row lock.
    sms_timeout_seconds: float = Field(default=10.0, gt=0, le=60)

    # --- voice -----------------------------------------------------------
    #: Read only when ``voice_transport == "ari"``. Its own switch for the same
    #: reason ``sms_transport`` has one.
    #:
    #: ``ari`` is a real dialler or nothing at all. It does **not** make the call
    #: audible or intelligent: what the customer hears, and whether a human or an
    #: answering machine picked up, is decided by the Stasis application named in
    #: ``ari_app``, which lives outside this build. What this switch buys is a
    #: channel that is actually created — a phone that rings — and a refusal that
    #: names the reason when it is not.
    voice_transport: VoiceTransport = "none"
    #: The Asterisk REST Interface origin, e.g. ``http://asterisk:8088``. Checked
    #: below to be https unless loopback: the request carries the customer's number
    #: in a query parameter and the credentials in a header. Loopback stays legal in
    #: every environment because that is how ARI is normally reached — the dialler
    #: runs as a sidecar to Asterisk, not across a network.
    ari_base_url: str = ""
    #: HTTP Basic credentials for ARI. ARI has no other authentication, so both are
    #: required whenever the transport is on, and neither is ever logged or
    #: returned — see ``AriVoiceSender.__repr__``.
    ari_username: str = ""
    ari_password: str = ""
    #: The dial string the call is originated to: the *tech/data* half of an ARI
    #: endpoint, with the recipient written as ``{number}`` —
    #: ``PJSIP/{number}@trunk``, not a URL. The placeholder is required. A fixed
    #: endpoint would place every reminder of the tenant to the same phone, which is
    #: not a smaller bug than not dialling at all, so start-up refuses a template
    #: without it. The rendered string carries the customer's number, which is why it
    #: is treated as credential-shaped: never logged, never in an error.
    ari_endpoint: str = ""
    #: The Stasis application the answered channel is handed to. It is what the call
    #: *becomes* after this transport's job ends.
    ari_app: str = "contract-reminders"
    #: What the customer sees as caller ID. Optional: an empty value lets Asterisk
    #: use the endpoint's own number, which is the right default for a trunk that
    #: only permits one.
    ari_caller_id: str = ""
    #: Upper bound on how long the far end may ring before Asterisk gives up, and
    #: simultaneously the bound on this dialler's HTTP call — for the same
    #: correctness reason as ``sms_timeout_seconds``: the scheduler sends while
    #: holding the row lock.
    voice_ring_seconds: int = Field(default=25, gt=0, le=120)
    #: The shared secret Asterisk signs call-outcome callbacks with — the route that
    #: finally answers what ``voice_transport`` alone cannot: whether a human or an
    #: answering machine picked up (``D10-154``).
    #:
    #: This value *is* the switch. Unlike ``inbound_transport`` there is no separate
    #: on/off variable, because the callback has exactly one possible author — our own
    #: Asterisk — so "can we be told?" and "how do we authenticate the teller?" are the
    #: same question. Empty means no route is mounted and the path answers 404, which is
    #: the honest default for a build that never dials.
    #:
    #: Separate from ``inbound_webhook_secret`` on purpose: they authenticate different
    #: callers with different lifetimes, and one leaked SMS gateway secret must not also
    #: let its holder invent call outcomes.
    voice_outcome_secret: str = ""

    # --- inbound ---------------------------------------------------------
    #: Whether this deployment can read a reply. ``none`` is the default and the only
    #: honest one for a build that has never been registered with a gateway.
    inbound_transport: InboundTransport = "none"
    #: The shared secret the gateway signs webhook bodies with. This is the *only*
    #: thing standing between an internet-facing endpoint and anyone who can guess a
    #: phone number, so it is required whenever inbound is enabled and never defaulted.
    inbound_webhook_secret: str = ""
    #: The key behind :mod:`app.core.blind_index`, which turns a normalized contact
    #: value into a searchable digest. Deliberately **not** derived from
    #: ``encryption_key``: that one is rotated (it has a retired-key ring) and a
    #: rotation would silently change every digest, which would make every stored
    #: opt-out unreachable at once. Separate key, separate lifetime.
    #:
    #: ``None`` and ``""`` are deliberately *different*: unset means "this deployment
    #: has no inbound lookup", which is honest and legal, while an empty string is
    #: refused by the validator below because it almost always means an environment
    #: variable the operator believes holds a key.
    contact_blind_index_key: str | None = None

    # --- http ------------------------------------------------------------
    cors_origins: str = "http://localhost:3000"

    # --- worker metrics --------------------------------------------------
    #: Where a worker's own ``/metrics`` listener binds. The API always serves
    #: ``/metrics``; every other process holds a private copy of the same registry
    #: (see :mod:`app.workers.metrics_server`), and this is the address it
    #: publishes that copy on.
    #:
    #: Loopback by default, because a listener nobody asked for should not be
    #: reachable from off the host. The Compose file sets ``0.0.0.0`` for the
    #: workers: inside a container the container is the network boundary, and a
    #: scraper on the Compose network cannot reach another container's loopback.
    worker_metrics_host: str = "127.0.0.1"
    #: ``0`` switches the listener off — a deployment that scrapes nothing should
    #: not open a port, and that was the state of *every* worker before D10-107.
    #: Any other value is the port to bind. Workers sharing one host need distinct
    #: values; in Compose each worker has its own container, so the single default
    #: serves the whole stack and only the published host ports differ.
    worker_metrics_port: int = Field(default=9101, ge=0, le=65535)

    # --- validators ------------------------------------------------------
    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in _VALID_LOG_LEVELS:
            allowed = ", ".join(sorted(_VALID_LOG_LEVELS))
            raise ValueError(f"LOG_LEVEL must be one of {allowed}; got {value!r}")
        return level

    @field_validator("worker_metrics_host")
    @classmethod
    def _require_a_bind_host(cls, value: str) -> str:
        """Refuse a blank bind address; absence is expressed by the port being 0.

        ``WORKER_METRICS_HOST`` set to nothing almost always means an environment
        variable the operator believes holds an address. Binding the empty string
        fails at the socket instead, inside a worker, after it has logged its
        start-up lines — so the refusal is here, where it can name the variable.
        """
        host = value.strip()
        if not host:
            raise ValueError(
                "WORKER_METRICS_HOST is set but empty; give an address to bind "
                "(127.0.0.1, or 0.0.0.0 inside a container), or set "
                "WORKER_METRICS_PORT=0 to switch the worker metrics listener off."
            )
        return host

    @field_validator("contact_blind_index_key")
    @classmethod
    def _check_blind_index_key(cls, value: str | None) -> str | None:
        """Refuse a configured-but-unusable key; leave the default alone.

        Because this runs only for values that were actually supplied, absence keeps
        meaning "no inbound lookup" while a blank value is an error. Collapsing the two
        would let a deployment whose environment variable is empty look configured and
        then store no index at all — the exact silent state this module refuses.

        The floor lives here as well as in ``ContactBlindIndex.__init__``: refusing at
        boot names the variable, whereas refusing at construction happens inside a
        request or a worker tick and reaches the operator as a 503.
        """
        if value is None:
            return None
        key = value.strip()
        if not key:
            raise ValueError(
                "CONTACT_BLIND_INDEX_KEY is set but empty; unset it to run without an "
                "inbound lookup, or give it real key material"
            )
        if len(key) < MIN_BLIND_INDEX_KEY_LENGTH:
            raise ValueError(
                "CONTACT_BLIND_INDEX_KEY must be at least "
                f"{MIN_BLIND_INDEX_KEY_LENGTH} characters; it is key material for a "
                "deterministic index, not a password, and a short key makes the "
                "stored digests reversible by enumerating phone numbers offline"
            )
        return key

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

        # An alert address with nothing to send it through would read as "the
        # operator is being told" while the first finding went nowhere — and the
        # first finding is precisely when it matters. Two ways to be wrong, both
        # refused here: a value that is not an address, and an address with no
        # transport behind it. Checked after the SMTP block above so a deployment
        # with both mistakes is told about the transport first: "you have no relay"
        # is the fix that also makes the address work.
        if self.attention_alert_to:
            if not _is_plausible_address(self.attention_alert_to):
                raise ValueError(
                    "ATTENTION_ALERT_TO must be an e-mail address when set; got "
                    f"{self.attention_alert_to!r}. Leave it empty to keep the "
                    "attention verdict in the log only."
                )
            if self.notification_transport != "smtp":
                raise ValueError(
                    "ATTENTION_ALERT_TO is set but NOTIFICATION_TRANSPORT is "
                    f"{self.notification_transport!r}, so there is no transport to "
                    "send the alert with. Set NOTIFICATION_TRANSPORT=smtp and "
                    "configure the SMTP block, or unset ATTENTION_ALERT_TO."
                )

        # The same two ways to be wrong as the attention address above, refused for
        # the same reason: an address with no relay behind it reads to an operator as
        # "a person is being told" while the first escalation goes nowhere — and the
        # first escalation is exactly when that belief costs a customer.
        if self.escalation_alert_to:
            if not _is_plausible_address(self.escalation_alert_to):
                raise ValueError(
                    "ESCALATION_ALERT_TO must be an e-mail address when set; got "
                    f"{self.escalation_alert_to!r}. Leave it empty to keep raised "
                    "escalations in the queue and the log only."
                )
            if self.notification_transport != "smtp":
                raise ValueError(
                    "ESCALATION_ALERT_TO is set but NOTIFICATION_TRANSPORT is "
                    f"{self.notification_transport!r}, so there is no transport to "
                    "send the alert with. Set NOTIFICATION_TRANSPORT=smtp and "
                    "configure the SMTP block, or unset ESCALATION_ALERT_TO."
                )

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

        # The SMS transport's requirements, checked at boot for the same reason
        # SMTP's are: a missing key or device would otherwise surface at the first
        # send, which the scheduler performs *inside* the row lock.
        if self.sms_transport == "textbee":
            if not self.textbee_api_key.strip():
                raise ValueError("TEXTBEE_API_KEY must be set when SMS_TRANSPORT=textbee")
            if not self.textbee_device_id.strip():
                raise ValueError(
                    "TEXTBEE_DEVICE_ID must be set when SMS_TRANSPORT=textbee; it is a "
                    "segment of the gateway URL, so a wrong value is a 404 at the first "
                    "send rather than an error here"
                )
            # Stricter than the OLLAMA_BASE_URL rule on purpose. That one guards a
            # feature vector; this request carries a customer's phone number in the
            # body *and* a credential in a header, so a plaintext hop is refused in
            # every environment. Loopback stays allowed so a local double can be
            # pointed at without a certificate.
            url = urlsplit(self.textbee_base_url)
            host = url.hostname or ""
            if not host:
                raise ValueError(
                    "TEXTBEE_BASE_URL must be an absolute URL when "
                    f"SMS_TRANSPORT=textbee; got {self.textbee_base_url!r}"
                )
            if host not in _LOOPBACK_HOSTS and url.scheme != "https":
                raise ValueError(
                    "TEXTBEE_BASE_URL must be https (or loopback) when "
                    f"SMS_TRANSPORT=textbee; got {self.textbee_base_url!r}. The "
                    "recipient number and the API key would otherwise cross the "
                    "network unencrypted."
                )

        # The dialler's requirements, checked at boot for the same reason the SMS ones
        # are: a half-configured transport would otherwise surface at the first ring,
        # which the scheduler performs *inside* the row lock.
        if self.voice_transport == "ari":
            if not self.ari_username.strip() or not self.ari_password:
                raise ValueError(
                    "ARI_USERNAME and ARI_PASSWORD must both be set when "
                    "VOICE_TRANSPORT=ari; ARI offers no other authentication, so an "
                    "empty pair is a 401 at the first call rather than an error here"
                )
            url = urlsplit(self.ari_base_url)
            host = url.hostname or ""
            if not host:
                raise ValueError(
                    "ARI_BASE_URL must be an absolute URL when VOICE_TRANSPORT=ari; "
                    f"got {self.ari_base_url!r}"
                )
            if host not in _LOOPBACK_HOSTS and url.scheme != "https":
                raise ValueError(
                    "ARI_BASE_URL must be https (or loopback) when VOICE_TRANSPORT=ari; "
                    f"got {self.ari_base_url!r}. The dialled number and the ARI "
                    "credentials would otherwise cross the network unencrypted."
                )
            # The placeholder is what makes the endpoint a *template* rather than an
            # address. Without it every reminder the tenant queues would ring the same
            # phone — the deployment's own, most likely — and nothing would say so:
            # ARI would happily create the channel and this build would record a send.
            if ARI_NUMBER_PLACEHOLDER not in self.ari_endpoint:
                raise ValueError(
                    "ARI_ENDPOINT must contain "
                    f"{ARI_NUMBER_PLACEHOLDER!r} when VOICE_TRANSPORT=ari; got "
                    f"{self.ari_endpoint!r}. It is formatted with the customer's number "
                    "for every call, and a fixed endpoint would dial one phone for every "
                    "reminder without reporting anything wrong."
                )

        # ``CONTACT_BLIND_INDEX_KEY``'s shape is checked by
        # :meth:`_check_blind_index_key` above, which runs only when the variable is
        # actually present. Checking it again here would have to answer "absent" and
        # "blank" the same way, and they are not the same deployment.

        # A call-outcome secret with no dialler is a callback nobody can place. This
        # build writes a provider channel id from exactly one place — ``AriVoiceSender``
        # — so with ``VOICE_TRANSPORT=none`` no notification row can ever carry the id a
        # callback names and the route would accept signed traffic and match nothing,
        # for ever. Refused in every environment, because it is a contradiction in the
        # deployment rather than a production-only risk.
        #
        # Blank is *not* a half-configured switch the way an empty
        # ``INBOUND_WEBHOOK_SECRET`` is: absent means "this deployment cannot hear call
        # outcomes", which is honest and is the default. A value that is present but
        # short is the operator believing they configured something.
        outcome_secret = self.voice_outcome_secret.strip()
        if outcome_secret:
            if self.voice_transport != "ari":
                raise ValueError(
                    "VOICE_OUTCOME_SECRET must not be set when VOICE_TRANSPORT is not "
                    "ari: only AriVoiceSender records the channel id a call-outcome "
                    "callback names, so nothing could ever match and the route would "
                    "answer 202 to every signed request for ever. Set "
                    "VOICE_TRANSPORT=ari, or leave the secret empty."
                )
            if len(outcome_secret) < MIN_INBOUND_SECRET_LENGTH:
                raise ValueError(
                    "VOICE_OUTCOME_SECRET must be at least "
                    f"{MIN_INBOUND_SECRET_LENGTH} characters; it is the signing key for "
                    "the call-outcome callback, and a caller that can forge one can "
                    "claim any call was answered by a person"
                )

        # Inbound is the only feature that makes a reminder SMS stoppable, so enabling
        # it requires both halves. Refusing a half-configured gateway is what stops a
        # deployment that looks like it listens while every STOP lands nowhere.
        if self.inbound_transport == "textbee":
            secret = self.inbound_webhook_secret.strip()
            if not secret:
                raise ValueError(
                    "INBOUND_WEBHOOK_SECRET must be set when INBOUND_TRANSPORT=textbee: "
                    "the endpoint is reachable by anyone who can reach the app, and the "
                    "signature is the only thing that distinguishes the gateway from a "
                    "stranger who guessed a phone number"
                )
            if len(secret) < MIN_INBOUND_SECRET_LENGTH:
                raise ValueError(
                    "INBOUND_WEBHOOK_SECRET must be at least "
                    f"{MIN_INBOUND_SECRET_LENGTH} characters; it is the signing key for "
                    "every inbound webhook"
                )
            if not self.contact_blind_index_key:
                raise ValueError(
                    "CONTACT_BLIND_INDEX_KEY must be set when INBOUND_TRANSPORT=textbee: "
                    "an inbound reply carries a phone number and nothing else, and "
                    "contact values are stored as ciphertext under a random IV, so "
                    "without the index there is no way to find the contract it means. "
                    "The route would accept every STOP and honour none of them."
                )

        # A scanner sits on the far side of a plain, unauthenticated socket, and what
        # crosses it is a customer's contract in the clear. A clamd on a *public*
        # address would therefore be a document leak with a malware check attached,
        # so a literal public address is refused here. Checked in every environment,
        # not only production: development is where the value is first typed.
        #
        # A hostname is deliberately not resolved. DNS at boot is its own failure
        # mode — it makes start-up depend on a resolver, and a name that resolves
        # publicly is an operator decision this file cannot see. The rule applies to
        # addresses written out literally, which is what a deployment pins.
        if self.virus_scan_transport == "clamd":
            host = self.clamd_host.strip()
            if not host:
                raise ValueError(
                    "CLAMD_HOST must be set when VIRUS_SCAN_TRANSPORT=clamd; it is "
                    "empty, which would fail at the first upload rather than here. "
                    "Unset VIRUS_SCAN_TRANSPORT to accept documents unexamined."
                )
            if _is_public_address(host):
                raise ValueError(
                    "CLAMD_HOST must not be a public address when "
                    f"VIRUS_SCAN_TRANSPORT=clamd; got {host!r}. The clamd protocol "
                    "has no TLS and no authentication, so every document would cross "
                    "the network in clear text to a daemon that anything reaching "
                    "that port could answer for. Use a loopback or private address."
                )

        if self.app_env in {"staging", "production"}:
            # Refused rather than merely documented, because the cost of being wrong
            # is a message to a real person that they cannot stop. An SMS with no
            # opt-out route is not a smaller notification, it is an unsolicited one:
            # the recipient cannot withdraw consent and this build cannot hear them
            # if they try. Development stays open so the adapter can be exercised
            # against a local double.
            #
            # Narrowed, not removed: the refusal used to be unconditional outside
            # development because no inbound route existed at all. One does now, so the
            # condition is the *actual* precondition — that this deployment can read a
            # reply. Sending without listening is still refused, which is what the
            # original rule was protecting.
            if self.sms_transport == "textbee" and self.inbound_transport != "textbee":
                raise ValueError(
                    "SMS_TRANSPORT=textbee is not available in "
                    f"{self.app_env} without INBOUND_TRANSPORT=textbee: a customer "
                    "could not withdraw consent by asking and nothing would honour "
                    "it, which makes the reminder an unsolicited message rather than "
                    "a smaller one. Configure inbound, or use development."
                )
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
        # A relative store resolves against each process's working directory, so
        # the API (``/app``), a worker under systemd and an operator's CLI would
        # each write or look for documents somewhere different. That failure is
        # silent until a document is needed and then presents as a missing file,
        # so it is refused at boot instead. Development keeps the default
        # relative path; staging/production must name an absolute one.
        if not _is_absolute_path(self.document_store_path):
            raise ValueError(
                "DOCUMENT_STORE_PATH must be an absolute path in "
                f"{self.app_env}; got {self.document_store_path!r}. A relative path "
                "resolves against each process's working directory, so the API, the "
                "workers and the CLI would disagree about where documents live."
            )
        # The product LLM is local by rule, and the feature vector it receives is
        # personal data even when no identifier is in it: a model reached over a
        # plaintext hop hands a third party the ability to observe who is being
        # contacted about which contract. Loopback is fine (a sidecar on the same
        # host); anything else must be TLS. This refuses a *shape* that would send
        # it in the clear rather than trusting the deployer to remember.
        if self.agent_backend == "ollama":
            host = urlsplit(self.ollama_base_url).hostname or ""
            is_local = host in _LOOPBACK_HOSTS
            if not is_local and urlsplit(self.ollama_base_url).scheme != "https":
                raise ValueError(
                    "OLLAMA_BASE_URL must be https (or loopback) in "
                    f"{self.app_env} when AGENT_BACKEND=ollama; got "
                    f"{self.ollama_base_url!r}. The feature vector and every "
                    "completion would otherwise cross the network unencrypted."
                )
        # Pull-mode ingestion adds an outbound request whose destination a caller
        # chooses, so the development allowance for a loopback double is refused
        # here: with it on, any operator could point a pull at this deployment's
        # own network — a metadata service, an admin port on a sidecar, another
        # tenant's internal host — and read the answer back in an error message.
        # The flag exists to exercise the transport against a local double, and a
        # local double is never what staging or production is talking to.
        if self.pull_allow_private_hosts:
            raise ValueError(
                "PULL_ALLOW_PRIVATE_HOSTS must not be enabled in "
                f"{self.app_env}: it lifts the public-address rule on outbound pulls, "
                "so anyone who can call the API could make it reach its internal "
                "network. It exists for a local double in development and test."
            )
        # The example's two field keys, refused on the same rule as the secret above
        # and for a sharper version of the same reason: `ENCRYPTION_KEY` seals every
        # stored contact value and `CONTACT_BLIND_INDEX_KEY` is what makes a digest
        # match an inbound STOP. A deployment that copied the example and kept these
        # would be sealing customer contact data under a key printed in the repo, and
        # searching it with a second one. Neither is a placeholder that half-works.
        if self.encryption_key == DEV_ENCRYPTION_KEY_PLACEHOLDER:
            raise ValueError(
                "ENCRYPTION_KEY still holds the value `.env.example` ships; set a real "
                f"key in {self.app_env}"
            )
        if self.contact_blind_index_key == DEV_BLIND_INDEX_KEY_PLACEHOLDER:
            raise ValueError(
                "CONTACT_BLIND_INDEX_KEY still holds the value `.env.example` ships; "
                f"set a real key in {self.app_env}"
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

    @property
    def notification_overdue_grace(self) -> timedelta:
        """``NOTIFICATION_OVERDUE_GRACE_SECONDS`` as the interval the queries compare.

        Two readers count overdue-pending rows — the operator's attention check and a
        tenant's own board — and both hand the number to the same repository predicate.
        Converting in one place keeps them from disagreeing about the window by a
        rounding difference, which would show up as two boards on the same database
        reporting different counts.
        """
        return timedelta(seconds=self.notification_overdue_grace_seconds)

    @property
    def deliverable_notification_channels(self) -> frozenset[Channel]:
        """The channels this deployment has a transport for, derived from the switches.

        The single answer to "can anything ever send this?", asked by two callers that
        must not disagree: :func:`~app.workers.senders.build_sender` decides which
        senders to wire from it, and :func:`~app.core.attention.attention_report` decides
        from it whether a reminder stuck ``pending`` follows from configuration or from a
        fault. Written once, here, because the two-answer version is how a background
        send starts disagreeing with the attention check about whether a channel exists
        at all — the same failure ``D10-129`` found in the alerting.

        ``voice`` joins the other two on exactly the same terms: it is present when
        ``VOICE_TRANSPORT`` names a dialler and absent when it does not. Before
        ``D10-144`` the answer was hardcoded — no transport in this build placed calls,
        so a queued voice notification was unsendable on *every* configuration — and
        that fact now lives where it belongs, in the switch, rather than in this method.
        What the switch does **not** claim is that anyone answers: ``voice`` means the
        call is placed, and the media side belongs to the Stasis application.

        A frozenset of :attr:`Channel` rather than of :class:`str` so a caller cannot
        compare it against a raw column value without going through the enum, which is
        where the two spellings would drift.
        """
        channels: set[Channel] = set()
        if self.notification_transport != "none":
            channels.add(Channel.EMAIL)
        if self.sms_transport != "none":
            channels.add(Channel.SMS)
        if self.voice_transport != "none":
            channels.add(Channel.VOICE)
        return frozenset(channels)


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


def _is_public_address(host: str) -> bool:
    """Whether ``host`` is written out as an address on the public internet.

    Only a literal address is judged. A name is not resolved, both because a
    resolver dependency at start-up is its own failure mode and because what a name
    points at is a decision this process cannot observe. ``is_global`` is the exact
    question being asked — loopback, link-local, private and unspecified ranges all
    answer ``False`` — so a Compose service name or a ``10.x`` address stays legal
    while a bare public IP does not.
    """
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def _is_absolute_path(value: str) -> bool:
    """Whether ``value`` is anchored, in *either* path notation.

    ``pathlib.Path`` is the wrong tool here: it is flavoured by the host OS, so
    ``Path("/var/lib/app/documents").is_absolute()`` is ``False`` on Windows. A
    developer running the tests on Windows would then be told the deployment's
    absolute POSIX path is relative — the same host-flavour trap ``D6-03``
    recorded for ``ZoneInfo``. Both pure flavours are asked instead, so the
    answer depends on the string, not on the machine reading it.
    """
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton (validated on first call)."""
    return Settings()
