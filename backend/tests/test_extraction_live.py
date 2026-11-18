"""The model backend against something that is not a mock (gap 17).

``OllamaExtractor`` is the one backend in this tree written for a service the
repository does not ship: ``docker-compose.yml`` defines no Ollama service
(``D10-88``), so every existing test of it — in ``test_extraction.py`` — answers
with ``urllib.request.urlopen`` patched. A patched ``urlopen`` proves the *parsing*
of an answer; it says nothing about the transport that would carry one. So gap 17's
standing sentence, *"has never been run against a live model"*, was true and also
had no command attached to it: there was no way for anyone to check it.

Two tests, deliberately of different kinds, because they make different claims.

* The first needs no model. It aims the extractor at a loopback port that is
  provably closed and asserts the real, unmocked failure path refuses rather than
  guessing — the one promise ``D7-03`` made about a silent model. Deterministic and
  offline, so it runs in CI like any other test.
* The second needs a model. It is ``live``-marked and opt-in behind ``OLLAMA_LIVE=1``,
  the same shape as ``KAFKA_LIVE`` in ``test_publishers.py``. CI skips it by design;
  an operator with a model can run gap 17's other half in one command.

Running the live half, once::

    ollama serve & ollama pull llama3.1:8b
    OLLAMA_LIVE=1 python -m pytest backend/tests/test_extraction_live.py -v

**A skip is not a pass.** Until somebody runs that, "no live model has answered"
stays the honest record. All this file guarantees is that the claim became
checkable, which is the half a test can own.
"""

from __future__ import annotations

import os
import socket

import pytest

from app.core.config import Settings
from app.db.enums import FieldName
from app.workers.extraction import ExtractionUnavailableError
from app.workers.extractors import OllamaExtractor, build_extractor

#: The smallest document that still holds a labelled value, so a model that answers
#: at all has something specific to answer about. A label at the start of a line is
#: the shape ``D10-106`` proved the *pattern* backend reads, so a model that misses
#: it is a real reading about the model rather than about this fixture.
DOCUMENT = "Support agreement\nContact email: priya.raman@example.test\n"

#: The value the document names, kept apart from the text so an assertion about
#: leakage can name what must not leak.
EMAIL = "priya.raman@example.test"

#: The declared field names, so a model cannot pass by inventing one: a candidate
#: whose name is not a member here could not be stored even if it looked plausible.
DECLARED_FIELDS = frozenset(FieldName)


def _a_closed_loopback_port() -> int:
    """A port on ``127.0.0.1`` that nothing is listening on.

    Bound and then released, so the number is one the kernel has just confirmed was
    free. Picking a memorable constant like ``11434`` instead would make this test
    pass or fail depending on whether the developer happened to have Ollama running
    — the opposite of what the assertion is for.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_a_model_that_does_not_answer_raises_rather_than_guessing() -> None:
    """The unmocked refusal path: a refused connection is a failure, not a result.

    ``test_extraction.py`` already proves this with ``urlopen`` patched. This proves
    it through the real socket stack, where the exception that arrives is one
    ``urllib`` actually raised rather than one the test constructed — the difference
    between testing a handler and testing the thing that reaches it.
    """
    endpoint = f"http://127.0.0.1:{_a_closed_loopback_port()}"
    extractor = OllamaExtractor(
        base_url=endpoint,
        model="llama3.1:8b",
        timeout_seconds=1.0,
    )

    with pytest.raises(ExtractionUnavailableError) as refusal:
        extractor.extract(DOCUMENT)

    message = str(refusal.value)
    # It refuses. There is no fallback that invents candidates to keep a caller happy,
    # which is the whole reason this backend raises instead of returning an empty list.
    assert "did not answer" in message
    # And the refusal is safe to log: the message is the only part of this path that
    # leaves the process, so it carries neither the infrastructure nor the customer.
    assert endpoint not in message
    assert "127.0.0.1" not in message
    assert EMAIL not in message
    assert DOCUMENT not in message


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("OLLAMA_LIVE") != "1",
    reason="set OLLAMA_LIVE=1 with a reachable model to run this",
)
def test_a_live_model_reads_a_labelled_value() -> None:
    """Opt-in: needs a real model. Proves a served model can be reached at all.

    Built through ``build_extractor`` rather than ``OllamaExtractor(...)`` directly,
    so the selection path is exercised too: this is the same object the extraction
    worker gets when ``EXTRACTION_BACKEND=ollama``.

    The endpoint is not passed here. ``Settings`` reads ``OLLAMA_BASE_URL`` from the
    environment and otherwise falls back to its shipped loopback default, so this
    asks for exactly the endpoint a deployment would use.

    A failure in this test is a finding, not flake: it means the model answered and
    what it answered was wrong, which is information about the model that no patched
    ``urlopen`` could have produced.
    """
    settings = Settings(
        app_env="test",
        log_json=False,
        extraction_backend="ollama",
        extraction_model=os.environ.get("OLLAMA_LIVE_MODEL", "llama3.1:8b"),
    )

    backend = build_extractor(settings)
    assert isinstance(backend, OllamaExtractor)

    found = backend.extract(DOCUMENT)

    # It has to produce something: an empty answer from a reachable model is the
    # "answers anything" result that made this backend unmeasurable in the first place.
    assert found, "the model was reachable but extracted nothing from a labelled document"
    # Nothing it returns can be a field the schema has no column for.
    assert {candidate.field_name for candidate in found} <= DECLARED_FIELDS
    # Confidence is a plain float in 0.0-1.0 because the column quantises to
    # NUMERIC(4,3); a backend handing over 95 would be silently rounded into 100.0.
    assert all(0.0 <= candidate.confidence <= 1.0 for candidate in found)
    # The value this document actually names is the one thing a model reading a
    # labelled line should not miss, and it is what gap 17 says was never observed.
    assert EMAIL in {candidate.value for candidate in found}
