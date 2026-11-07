"""Encrypted document storage for contract ingestion (gap 12).

Split along the two halves of the guarantee, because neither half proves the other:

* **the store** is exercised directly against a temporary root — round-trip,
  idempotent re-save, the size cap, tamper detection, and the two confinement
  rules that stop a database row naming a path outside the root;
* **the route** is exercised through the real ASGI app, because "the bytes are on
  disk *and* a committed row names them, written in one transaction" is not a
  property a unit test of either half can establish.

Two negative assertions recur: the plaintext never reaches the disk, and a process
without ``ENCRYPTION_KEY`` cannot accept a document at all.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.core.crypto import DecryptionError, FieldCipher, MissingEncryptionKeyError
from app.core.documents import (
    DocumentMissingError,
    DocumentStore,
    DocumentTooLargeError,
    EmptyDocumentError,
    InvalidDocumentKeyError,
    build_document_store,
    document_digest,
)
from app.db.enums import UserRole
from app.db.models import Contract, OutboxEvent, Tenant, User
from app.db.session import session_scope
from app.main import create_app

DOCUMENT_PATH = "/api/v1/contracts/document"
LOGIN_PATH = "/api/v1/auth/login"

#: The media type the route declares. Written out rather than imported from the
#: route: if somebody changes the constant, the upload below should stop matching
#: the client's request, which is the behaviour this suite is meant to notice.
DOCUMENT_MEDIA_TYPE = "application/octet-stream"

#: Matches ``conftest.DEFAULT_PASSWORD``; protects in-memory databases only.
PASSWORD = "correct-horse-battery-staple"  # nosec B105
EMAIL = "ops@acme.test"

#: A plausible-sized document with byte-distinct content, so a partial write or an
#: off-by-one truncation shows up as a mismatch instead of passing by luck.
PAYLOAD = b"%PDF-1.7\n" + bytes(range(256)) * 4

#: One mebibyte: big enough that no test payload is accidentally over it, and used
#: as the default cap for every app built below.
ONE_MIB = 1024 * 1024


# --- fixtures -------------------------------------------------------------
@pytest.fixture
def encryption_key() -> str:
    return Fernet.generate_key().decode("ascii")


@pytest.fixture
def cipher(encryption_key: str) -> FieldCipher:
    return FieldCipher(encryption_key)


@pytest.fixture
def store(tmp_path: Path, cipher: FieldCipher) -> DocumentStore:
    return DocumentStore(tmp_path / "documents", cipher, max_bytes=ONE_MIB)


# --- helpers --------------------------------------------------------------
def make_client(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    encryption_key: str,
    store_root: Path,
    max_bytes: int = ONE_MIB,
) -> TestClient:
    """Build the app with a document store rooted in the test's temporary directory.

    Built per test rather than shared as a fixture because two tests in this module
    need a *different* configuration — no key at all, and a cap small enough to
    trip — and a fixture with knobs would hide which configuration is under test.
    """
    configured = settings.model_copy(
        update={
            "encryption_key": encryption_key,
            "document_store_path": str(store_root),
            "document_max_bytes": max_bytes,
        }
    )
    return TestClient(create_app(configured, session_factory=session_factory))


def login(client: TestClient, tenant: Tenant, email: str = EMAIL) -> dict[str, str]:
    response = client.post(
        LOGIN_PATH,
        json={"tenant": tenant.slug, "email": email, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def auth(
    client: TestClient,
    tenant: Tenant,
    make_user: Callable[..., User],
    *,
    role: UserRole = UserRole.OPERATOR,
    email: str = EMAIL,
) -> dict[str, str]:
    make_user(tenant, email=email, role=role)
    return login(client, tenant, email)


def upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    payload: bytes = PAYLOAD,
    customer_ref: str = "A. Rahman",
    **params: str,
) -> Response:
    """POST a document, with the one media type the route expects."""
    return client.post(
        DOCUMENT_PATH,
        params={"customer_ref": customer_ref, **params},
        content=payload,
        headers={**headers, "Content-Type": DOCUMENT_MEDIA_TYPE},
    )


def load_contract(session_factory: sessionmaker[Session], contract_id: int) -> Contract:
    """Re-read a contract through a fresh session, so assertions see stored state."""
    with session_scope(session_factory) as session:
        row = session.get(Contract, contract_id)
        assert row is not None
        return row


def outbox_rows(session_factory: sessionmaker[Session]) -> list[OutboxEvent]:
    with session_scope(session_factory) as session:
        return list(session.execute(select(OutboxEvent).order_by(OutboxEvent.id)).scalars())


def blobs(root: Path) -> list[Path]:
    """Every file under the store root, so "one blob" can be asserted, not assumed."""
    return [path for path in root.rglob("*") if path.is_file()]


# --- the store ------------------------------------------------------------
def test_a_document_round_trips_through_the_store(store: DocumentStore) -> None:
    stored = store.save(PAYLOAD)

    assert stored.size == len(PAYLOAD)
    assert store.has(stored.key) is True
    assert store.load(stored.key) == PAYLOAD


def test_the_key_is_the_hash_of_the_plaintext(store: DocumentStore) -> None:
    stored = store.save(PAYLOAD)
    digest = document_digest(PAYLOAD)

    # The key is derived from the content, which is what makes a re-upload
    # idempotent and lets the same hash serve as the ``contracts.sha256`` value.
    assert stored.sha256 == digest
    assert stored.key == f"{digest[:2]}/{digest}"


def test_the_blob_on_disk_holds_no_plaintext(store: DocumentStore) -> None:
    stored = store.save(PAYLOAD)
    on_disk = (store.root / stored.key).read_bytes()

    assert on_disk != PAYLOAD
    assert PAYLOAD not in on_disk


def test_saving_the_same_document_twice_writes_one_blob(store: DocumentStore) -> None:
    first = store.save(PAYLOAD)
    second = store.save(PAYLOAD)

    assert first.key == second.key
    assert blobs(store.root) == [store.root / first.key]


def test_a_document_sealed_before_a_rotation_is_still_readable(
    tmp_path: Path, encryption_key: str
) -> None:
    """A document is sealed by the same key ring as a field value, so it rotates too."""
    root = tmp_path / "documents"
    stored = DocumentStore(root, FieldCipher(encryption_key), max_bytes=ONE_MIB).save(PAYLOAD)

    rotated_key = Fernet.generate_key().decode("ascii")
    rotated = FieldCipher(rotated_key, retired_keys=[encryption_key])

    assert DocumentStore(root, rotated, max_bytes=ONE_MIB).load(stored.key) == PAYLOAD


def test_an_empty_document_is_refused(store: DocumentStore) -> None:
    with pytest.raises(EmptyDocumentError, match="at least one byte"):
        store.save(b"")

    # Refused before the root was created: there is nothing to clean up.
    assert not store.root.exists()


def test_an_oversized_document_is_refused_before_anything_is_written(
    tmp_path: Path, cipher: FieldCipher
) -> None:
    store = DocumentStore(tmp_path / "documents", cipher, max_bytes=16)

    with pytest.raises(DocumentTooLargeError, match="17 bytes, over the 16-byte limit") as raised:
        store.save(b"x" * 17)

    assert (raised.value.size, raised.value.limit) == (17, 16)
    assert not store.root.exists()


def test_a_tampered_blob_fails_to_decrypt_instead_of_returning_garbage(
    store: DocumentStore,
) -> None:
    stored = store.save(PAYLOAD)
    (store.root / stored.key).write_bytes(b"not the document this key names")

    with pytest.raises(DecryptionError, match="could not be decrypted"):
        store.load(stored.key)


def test_a_key_shape_the_store_could_not_have_produced_is_refused(
    store: DocumentStore,
) -> None:
    for key in ("../../etc/passwd", "/etc/passwd", "aa/short", "zz/" + "0" * 64, ""):
        with pytest.raises(InvalidDocumentKeyError, match="not in the form"):
            store.load(key)


def test_a_symlink_inside_a_shard_cannot_escape_the_store_root(
    tmp_path: Path, cipher: FieldCipher
) -> None:
    root = tmp_path / "documents"
    store = DocumentStore(root, cipher, max_bytes=ONE_MIB)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"not yours")
    shard = root / "ab"
    shard.mkdir(parents=True)
    link = shard / ("ab" + "0" * 62)
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating symlinks without privileges")

    # The shape rule cannot catch this one: the name is a perfectly valid key. Only
    # resolving it and re-checking containment does.
    with pytest.raises(InvalidDocumentKeyError, match="outside the store root"):
        store.load(f"ab/{link.name}")


def test_a_missing_document_names_the_store_root(store: DocumentStore) -> None:
    key = f"ab/{'0' * 64}"

    assert store.has(key) is False
    with pytest.raises(DocumentMissingError, match="no document stored for key"):
        store.load(key)


def test_the_store_refuses_to_build_without_an_encryption_key(
    settings: Settings, tmp_path: Path
) -> None:
    configured = settings.model_copy(update={"document_store_path": str(tmp_path / "documents")})

    with pytest.raises(MissingEncryptionKeyError, match="ENCRYPTION_KEY is required"):
        build_document_store(configured)


# --- the upload route -----------------------------------------------------
def test_uploading_a_document_stores_it_and_names_the_row(
    settings: Settings,
    encryption_key: str,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    root = tmp_path / "documents"
    with make_client(
        settings, session_factory, encryption_key=encryption_key, store_root=root
    ) as client:
        headers = auth(client, tenant, make_user)
        response = upload(client, headers, customer_ref="A. Rahman", end_date="2026-01-01")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["customer_ref"] == "A. Rahman"
    assert body["source"] == "pdf"
    assert body["sha256"] == document_digest(PAYLOAD)

    contract = load_contract(session_factory, body["id"])
    assert contract.storage_path == f"{body['sha256'][:2]}/{body['sha256']}"
    assert contract.sha256 == document_digest(PAYLOAD)

    # The pair has to agree: the row names a key, and that key holds this document.
    storage_key = contract.storage_path
    assert storage_key is not None
    store = DocumentStore(root, FieldCipher(encryption_key), max_bytes=ONE_MIB)
    assert store.load(storage_key) == PAYLOAD


def test_the_upload_announces_the_same_event_shape_as_metadata_ingestion(
    settings: Settings,
    encryption_key: str,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    with make_client(
        settings, session_factory, encryption_key=encryption_key, store_root=tmp_path / "documents"
    ) as client:
        headers = auth(client, tenant, make_user)
        response = upload(client, headers)

    assert response.status_code == 201, response.text
    events = outbox_rows(session_factory)
    assert [event.event_type for event in events] == ["contract.ingested"]
    # A consumer must not be able to tell which route produced the fact; only
    # ``source`` differs, and it is a field the consumer already reads.
    payload = events[0].payload
    assert set(payload) == {"contract_id", "end_date", "sha256", "source", "status"}
    assert payload["contract_id"] == response.json()["id"]
    assert payload["source"] == "pdf"
    assert payload["status"] == "active"


def test_the_same_document_uploaded_twice_is_a_conflict(
    settings: Settings,
    encryption_key: str,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    root = tmp_path / "documents"
    with make_client(
        settings, session_factory, encryption_key=encryption_key, store_root=root
    ) as client:
        headers = auth(client, tenant, make_user)
        first = upload(client, headers)
        second = upload(client, headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 409
    # The conflict names the row that already holds the content, and no second
    # event was created for the retry.
    assert str(first.json()["id"]) in second.json()["error"]["message"]
    assert len(outbox_rows(session_factory)) == 1
    assert len(blobs(root)) == 1


def test_two_tenants_sharing_a_document_share_the_blob_but_not_the_row(
    settings: Settings,
    encryption_key: str,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    other_tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """Uniqueness is ``(tenant_id, sha256)``; the bytes are immutable and shared."""
    root = tmp_path / "documents"
    with make_client(
        settings, session_factory, encryption_key=encryption_key, store_root=root
    ) as client:
        mine = upload(client, auth(client, tenant, make_user))
        theirs = upload(client, auth(client, other_tenant, make_user))

    assert mine.status_code == 201, mine.text
    assert theirs.status_code == 201, theirs.text
    assert mine.json()["id"] != theirs.json()["id"]
    assert mine.json()["sha256"] == theirs.json()["sha256"]
    assert len(blobs(root)) == 1


def test_an_oversized_upload_is_refused_with_413(
    settings: Settings,
    encryption_key: str,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    root = tmp_path / "documents"
    with make_client(
        settings, session_factory, encryption_key=encryption_key, store_root=root, max_bytes=16
    ) as client:
        headers = auth(client, tenant, make_user)
        response = upload(client, headers, payload=b"x" * 64)

    assert response.status_code == 413
    error = response.json()["error"]
    assert error["code"] == "DOCUMENT_TOO_LARGE"
    assert error["details"] == {"size": 64, "limit": 16}
    assert not root.exists()


def test_a_blank_customer_ref_is_refused(
    settings: Settings,
    encryption_key: str,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """The same rule the JSON route applies to its body, applied to a query param."""
    with make_client(
        settings, session_factory, encryption_key=encryption_key, store_root=tmp_path / "documents"
    ) as client:
        response = upload(client, auth(client, tenant, make_user), customer_ref="   ")

    assert response.status_code == 422
    assert "blank" in response.json()["error"]["message"]


def test_a_viewer_cannot_upload(
    settings: Settings,
    encryption_key: str,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    root = tmp_path / "documents"
    with make_client(
        settings, session_factory, encryption_key=encryption_key, store_root=root
    ) as client:
        response = upload(client, auth(client, tenant, make_user, role=UserRole.VIEWER))

    assert response.status_code == 403
    assert not root.exists()


def test_a_document_cannot_be_accepted_without_an_encryption_key(
    settings: Settings,
    tmp_path: Path,
    session_factory: sessionmaker[Session],
    tenant: Tenant,
    make_user: Callable[..., User],
) -> None:
    """No key means no store, and that is a 503 rather than a plaintext write."""
    root = tmp_path / "documents"
    app = create_app(
        settings.model_copy(update={"document_store_path": str(root)}),
        session_factory=session_factory,
    )
    with TestClient(app) as client:
        response = upload(client, auth(client, tenant, make_user))

    assert response.status_code == 503
    assert "ENCRYPTION_KEY" in response.json()["error"]["message"]
    assert not root.exists()
