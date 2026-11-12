"""Versioned prompt registry (P2-D).

Prompts live in files next to this module rather than in string literals because
a prompt version is *provenance*: ``agent_decisions.prompt_version`` names the
exact text that produced a channel, so an operator auditing a row must be able to
read the text that row refers to. Keeping the text in a file also makes the
spec's "rollback by flipping the registry, no deploy" claim real — editing
``registry.json`` and restarting points every *new* decision at another version,
while rows already written keep naming the version that actually ran.

``registry.json`` maps a prompt name to its active version::

    {"advisory_channel": 1}

The body lives in ``{name}_v{version}.txt``. A missing file, a malformed
registry, an unknown name or a version that is not a positive integer raises
:class:`PromptRegistryError`. Callers turn that into "no advisory this time"
(a review flag), never into a silently substituted default prompt: a decision
whose provenance names text we could not load is a decision we cannot audit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = [
    "PROMPT_DIR",
    "Prompt",
    "PromptRegistryError",
    "load_prompt",
    "prompt_version",
]

#: Directory the prompt bodies and ``registry.json`` live in. Read through the
#: module attribute (not the imported name) so tests can point it at a temporary
#: directory.
PROMPT_DIR = Path(__file__).resolve().parent

REGISTRY_FILE = "registry.json"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
MAX_PROMPT_VERSION = 9_999


class PromptRegistryError(RuntimeError):
    """The registry or the prompt body it names is missing or malformed."""


@dataclass(frozen=True, slots=True)
class Prompt:
    """One versioned prompt body.

    ``label`` is what goes into ``agent_decisions.prompt_version`` — the same
    string for every decision produced by this file, and different for every
    edit, which is what makes an A/B split or a rollback attributable later.
    """

    name: str
    version: int
    text: str

    @property
    def label(self) -> str:
        return f"{self.name}_v{self.version}"


def _checked_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise PromptRegistryError(
            f"prompt name {name!r} must match {_NAME_RE.pattern} — a name is also "
            "a path component, so anything else could escape the prompt directory"
        )
    return name


@lru_cache(maxsize=8)
def _read_registry(directory: str) -> dict[str, int]:
    path = Path(directory) / REGISTRY_FILE
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PromptRegistryError(f"cannot read {path}: {exc.strerror}") from exc
    except ValueError as exc:
        raise PromptRegistryError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise PromptRegistryError(f"{path} must be a JSON object of name -> version")

    registry: dict[str, int] = {}
    for name, version in raw.items():
        if not isinstance(name, str) or not isinstance(version, int) or isinstance(version, bool):
            raise PromptRegistryError(
                f"{path}: entry {name!r} must be an integer version, got {version!r}"
            )
        if not 0 < version <= MAX_PROMPT_VERSION:
            raise PromptRegistryError(
                f"{path}: entry {name!r} version {version} is outside 1..{MAX_PROMPT_VERSION}"
            )
        registry[_checked_name(name)] = version
    return registry


def prompt_version(name: str) -> int:
    """The version ``registry.json`` currently activates for ``name``."""
    registry = _read_registry(str(PROMPT_DIR))
    try:
        return registry[_checked_name(name)]
    except KeyError as exc:
        raise PromptRegistryError(
            f"no active version for prompt {name!r} in {PROMPT_DIR / REGISTRY_FILE}"
        ) from exc


def load_prompt(name: str, version: int | None = None) -> Prompt:
    """Read the active (or an explicitly pinned) version of ``name``.

    Pinning exists for tests and for reproducing a historical decision; normal
    callers pass only ``name`` and get whatever the registry activates.
    """
    resolved = prompt_version(name) if version is None else version
    if not 0 < resolved <= MAX_PROMPT_VERSION:
        raise PromptRegistryError(f"version {resolved} is outside 1..{MAX_PROMPT_VERSION}")
    path = PROMPT_DIR / f"{_checked_name(name)}_v{resolved}.txt"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromptRegistryError(f"cannot read {path}: {exc.strerror}") from exc
    if not text.strip():
        raise PromptRegistryError(f"{path} is empty")
    return Prompt(name=name, version=resolved, text=text)
