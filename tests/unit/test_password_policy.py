"""Password policy validator tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contract_agent.schemas.user import UserRegister


def _payload(password: str) -> dict[str, str]:
    return {"email": "a@b.com", "password": password}


def test_valid_password_passes() -> None:
    UserRegister(**_payload("Sup3rSecret!Pass"))


@pytest.mark.parametrize(
    "bad",
    [
        "Short1!",  # <12
        "alllowercase123!",  # no upper
        "ALLUPPERCASE123!",  # no lower
        "NoDigitsHere!!!!",  # no digit
        "NoSymbols12345",  # no symbol
    ],
)
def test_invalid_passwords_rejected(bad: str) -> None:
    with pytest.raises(ValidationError):
        UserRegister(**_payload(bad))


def test_max_length_enforced() -> None:
    with pytest.raises(ValidationError):
        UserRegister(**_payload("A1!" + "x" * 200))
