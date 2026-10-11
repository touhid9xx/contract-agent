"""Extended password policy tests — edge cases."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from contract_agent.schemas.user import PasswordChange, UserRegister


def _reg(pw: str) -> dict[str, str]:
    return {"email": "a@b.com", "password": pw}


def test_exactly_12_chars_accepted() -> None:
    UserRegister(**_reg("Aa1!56789012"))


def test_11_chars_rejected() -> None:
    with pytest.raises(ValidationError):
        UserRegister(**_reg("Aa1!5678901"))


def test_unicode_password_accepted() -> None:
    # Bengali char + upper + lower + digit + symbol
    UserRegister(**_reg("Aবc1!5678901"))


def test_whitespace_only_no_digit_rejected() -> None:
    with pytest.raises(ValidationError):
        UserRegister(**_reg("Aaaaaa!bbbbb"))


def test_128_char_boundary() -> None:
    UserRegister(**_reg("Aa1!" + "x" * 124))


def test_129_char_rejected() -> None:
    with pytest.raises(ValidationError):
        UserRegister(**_reg("Aa1!" + "x" * 125))


# ---------- PasswordChange ----------
def test_password_change_valid() -> None:
    PasswordChange(current_password="Old!Pass1234", new_password="New!Pass5678")


def test_password_change_enforces_new_policy() -> None:
    with pytest.raises(ValidationError):
        PasswordChange(current_password="Old!Pass1234", new_password="weak")


def test_password_change_does_not_enforce_current_policy() -> None:
    """Current password might predate the policy — shouldn't block change."""
    PasswordChange(current_password="old", new_password="New!Pass5678")
