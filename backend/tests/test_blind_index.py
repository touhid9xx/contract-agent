"""Tests for the contact blind index (P2-E).

The index exists to answer one question — "which contract does this phone number
belong to?" — at a moment when the only stored copy of the number is Fernet
ciphertext under a random IV, so a `WHERE` on the value can never match. Everything
here pins a property that, if it changed, would silently stop that lookup:

* the same value under the same key always hashes the same, or an inbound STOP
  finds nothing;
* a different key hashes differently, or the key stops being a key;
* formatting is normalised before hashing, or `+44 20 7946 0958` never matches
  `+442079460958`;
* non-contact fields get no digest, so the index is not a searchable copy of
  values the rest of the system encrypts.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.blind_index import (
    CONTACT_FIELD_NAMES,
    ContactBlindIndex,
    MissingBlindIndexKeyError,
    build_blind_index,
    contact_digest,
)
from app.core.config import MIN_BLIND_INDEX_KEY_LENGTH, Settings
from app.db.enums import FieldName
from app.workers.extraction import normalize_value

#: A key of exactly the documented shape (32+ printable characters). Not a
#: credential: it never leaves the test process.
KEY = "0123456789abcdef0123456789abcdef"
INDEX = ContactBlindIndex(KEY)

PHONE = "+442079460958"


def test_the_same_value_under_the_same_key_always_hashes_the_same() -> None:
    # Not a tautology: a salted or randomly-IV'd digest would also satisfy "digest
    # exists", and would make every stored index unqueryable.
    assert INDEX.digest(PHONE) == INDEX.digest(PHONE)
    assert ContactBlindIndex(KEY).digest(PHONE) == INDEX.digest(PHONE)


def test_the_golden_digest_is_stable() -> None:
    """Pin the construction itself.

    Every ``ExtractedField.blind_index`` already written (and every opt-out found
    through it) depends on this exact byte construction. A change here would not
    raise: it would quietly orphan the indexes of rows extracted before the
    change, so opt-outs recorded before it would stop being findable. Pinning the
    value turns that into a failing test.
    """
    assert INDEX.digest(PHONE) == "c4aa08716e6ee297eb59464b1fd03d909f9c57b7c4a1f389e4a7a4384b143245"


def test_the_digest_is_a_sha256_hex_digest() -> None:
    digest = INDEX.digest(PHONE)
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_a_different_key_produces_a_different_digest() -> None:
    # This is what makes the index a keyed MAC rather than a plain hash: someone
    # holding the database alone cannot build a rainbow table of phone numbers.
    other = ContactBlindIndex("fedcba9876543210fedcba9876543210")
    assert other.digest(PHONE) != INDEX.digest(PHONE)


def test_the_digest_never_contains_the_value() -> None:
    digest = INDEX.digest(PHONE)
    assert PHONE.lstrip("+") not in digest
    assert "2079460958" not in digest


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+44 (20) 7946-0958", "+442079460958"),
        ("+442079460958", "+442079460958"),
    ],
)
def test_formatting_differences_are_normalised_before_hashing(raw: str, expected: str) -> None:
    # The writer and the reader both normalise. If either one stopped, the customer
    # who texts from the same number would never match their own contract.
    assert normalize_value(FieldName.PHONE, raw) == expected
    assert INDEX.digest(normalize_value(FieldName.PHONE, raw)) == INDEX.digest(PHONE)


def test_a_non_contact_field_gets_no_digest() -> None:
    assert contact_digest(INDEX, FieldName.START_DATE, "2026-01-01") is None


def test_the_indexed_fields_are_exactly_the_contact_fields() -> None:
    assert set(CONTACT_FIELD_NAMES) == {FieldName.EMAIL, FieldName.PHONE}


def test_an_email_and_a_phone_are_indexed_the_same_way() -> None:
    address = normalize_value(FieldName.EMAIL, "Ops@Acme.TEST")
    assert contact_digest(INDEX, FieldName.EMAIL, address) == INDEX.digest("ops@acme.test")


def test_building_the_index_without_a_key_is_an_error() -> None:
    # Fail closed: an installation with no key must refuse to answer "which
    # contract is this number?" rather than guess by scanning ciphertext.
    settings = Settings(app_env="test")
    assert settings.contact_blind_index_key is None
    with pytest.raises(MissingBlindIndexKeyError):
        build_blind_index(settings)


def test_the_key_is_not_derived_from_the_encryption_key() -> None:
    """The two keys have deliberately separate lifetimes.

    ``ENCRYPTION_KEY`` has a retired-key ring, so it can be rotated. If the digest
    were derived from it, one rotation would change every digest at once and every
    stored index — every recorded opt-out — would become unreachable in a single
    step. So a deployment with an encryption key and no index key must still fail
    closed rather than borrow one.
    """
    settings = Settings(
        app_env="test",
        encryption_key="Q2hhbmdlTWUtMzJCeXRlc0tleUZvckZlcm5ldE5vdyE=",
    )
    with pytest.raises(MissingBlindIndexKeyError):
        build_blind_index(settings)


def test_a_key_below_the_documented_floor_is_refused() -> None:
    short = "k" * (MIN_BLIND_INDEX_KEY_LENGTH - 1)
    with pytest.raises(ValidationError):
        Settings(app_env="test", contact_blind_index_key=short)


def test_an_empty_key_is_refused_rather_than_treated_as_absent() -> None:
    # "" would otherwise hash every value under the empty key — a digest that
    # anyone can compute, which is worse than no index at all.
    with pytest.raises(ValidationError):
        Settings(app_env="test", contact_blind_index_key="")


def test_a_key_at_the_floor_is_accepted() -> None:
    settings = Settings(app_env="test", contact_blind_index_key="k" * MIN_BLIND_INDEX_KEY_LENGTH)
    assert build_blind_index(settings).digest(PHONE) == ContactBlindIndex(
        "k" * MIN_BLIND_INDEX_KEY_LENGTH
    ).digest(PHONE)
