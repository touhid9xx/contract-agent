"""Turning a stored document into the text extraction reads (gap 12).

:mod:`app.core.documents` gave ``Contract.storage_path`` a writer and an
existence reader. It did not give it a *content* reader: nothing turned the
sealed bytes back into text, so ``extract_contract`` — which has taken a
``document_text`` since F7 — still had no producer in the product. This module is
that reader, and it is deliberately the smallest one that can be honest.

Four decisions, each one a refusal rather than a feature:

**The bytes decide the format, not a filename or a content type.** The media type
is not stored: ``POST /contracts/document`` accepts one and discards it, because a
column that records what a client *claimed* is worth less than the two magic
bytes that record what the file *is*. A PDF starts with ``%PDF-``; anything else
is read as UTF-8 text, which is exactly what ``cli extract --text-file`` already
did — so the pre-existing path keeps working and there is no second rule.

**A scanned document is not an empty document.** A PDF page with no text layer
yields ``""`` from the parser. This module returns that as text and reports the
page count, so the caller can say "4 pages, no text" instead of "nothing found" —
the difference between "the contract never stated an email address" and "we never
read the contract" is the whole reason the review queue exists.

**An encrypted PDF is refused, not guessed at.** ``No text password`` is not a
password: there is no configured credential, and inventing an empty-password
attempt would silently return partial text for a document whose owner expected it
to stay closed. Refuse, name the fix, let an operator decrypt and re-upload.

**A malformed PDF is refused, not crashed on.** These bytes come from an upload,
so they are untrusted input; the parser is a parser, and a PDF built to break one
must produce a refusal this module owns rather than a traceback.

``pypdf`` is imported lazily, like ``kafka-python`` in ``app/workers/publishers.py``
and for the same reason: a text-only deployment that never parses a PDF should not
pay for the import, and a process without the wheel installed must fail with a
sentence naming the fix rather than an ``ImportError`` at start-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Literal

__all__ = [
    "DocumentKind",
    "DocumentTextError",
    "DocumentTextUnavailableError",
    "ExtractedText",
    "UnsupportedDocumentError",
    "document_kind",
    "extract_document_text",
]

DocumentKind = Literal["pdf", "text"]

#: The PDF signature. ISO 32000 permits it anywhere in the first 1024 bytes, which
#: is the window searched: a whole-file search would let a *text* file that merely
#: mentions ``%PDF-`` be handed to the PDF parser, and a zero-window check would
#: reject the producers that emit a small preamble before the header.
_PDF_MAGIC = b"%PDF-"
_PDF_MAGIC_WINDOW = 1024


class DocumentTextError(RuntimeError):
    """Base for every refusal this module raises."""


class UnsupportedDocumentError(DocumentTextError):
    """Raised when the bytes are neither a PDF nor UTF-8 text.

    Deliberately carries no part of the document: a refusal is logged, and the
    bytes are PII by definition — that is why they were sealed in the first place.
    """


class DocumentTextUnavailableError(DocumentTextError):
    """Raised when the document is recognised but its text cannot be produced.

    Three causes, and the sentence differs because the fix does: the parsed form
    is encrypted, the parser could not read the file, or the parser is not
    installed. "I cannot read this" and "there is nothing in this" must never look
    alike to an operator.
    """


@dataclass(frozen=True, slots=True)
class ExtractedText:
    """What :func:`extract_document_text` reports about a document."""

    #: The text extraction will be run over. May be empty — a scanned PDF legitimately
    #: produces nothing — and the caller decides what an empty page means.
    text: str
    #: Which reader produced it. Logged, so an operator can say what a document was
    #: without recording anything about its contents.
    kind: DocumentKind
    #: Pages parsed; ``1`` for a plain-text document, which is one document rather
    #: than one page.
    pages: int


def document_kind(payload: bytes) -> DocumentKind:
    """Whether ``payload`` is a PDF or UTF-8 text, decided by its own bytes."""
    if _PDF_MAGIC in payload[:_PDF_MAGIC_WINDOW]:
        return "pdf"
    return "text"


def extract_document_text(payload: bytes) -> ExtractedText:
    """Turn a stored document's plaintext bytes into text.

    Takes plaintext, not a store key: :class:`~app.core.documents.DocumentStore`
    owns decryption and this module owns parsing, so neither has to know the other's
    failure modes. The caller loads, this reads.
    """
    if not payload:
        raise UnsupportedDocumentError(
            "the document has no bytes; an empty upload is a malformed request "
            "rather than an empty contract"
        )
    if document_kind(payload) == "pdf":
        return _pdf_text(payload)
    return _plain_text(payload)


def _plain_text(payload: bytes) -> ExtractedText:
    """The pre-existing behaviour of ``cli extract --text-file``, unchanged.

    ``cli extract`` has always read UTF-8 text, so a document that is not a PDF is
    read the same way it always was. Decoding strictly rather than with
    ``errors="replace"`` is the difference between refusing a binary file and
    handing extractor regexes a document full of replacement characters.
    """
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedDocumentError(
            "the document is neither a PDF nor UTF-8 text, so it cannot be read; "
            "convert it to PDF or to UTF-8 text and store it again"
        ) from exc
    return ExtractedText(text=text, kind="text", pages=1)


def _pdf_text(payload: bytes) -> ExtractedText:
    """Every page's text layer, concatenated in page order.

    Pages are joined with a newline rather than a blank line so that
    ``FieldCandidate.source_line`` keeps meaning "a line of the document": the
    pattern extractor measures label windows in characters and lines, and a page
    break is one line boundary, not two.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - the wheel is pinned in requirements
        raise DocumentTextUnavailableError(
            "reading a PDF needs the 'pypdf' package, which is not installed; "
            "install backend/requirements.txt in the environment running this command"
        ) from exc

    # Reader construction already parses the cross-reference table, and `is_encrypted`
    # reads the trailer, so both are inside the one refusal boundary: a PDF may fail
    # either step, and both failures mean the same thing to an operator.
    try:
        reader = PdfReader(BytesIO(payload))
        encrypted = reader.is_encrypted
        pages = list(reader.pages)
    except Exception as exc:
        raise DocumentTextUnavailableError(
            "the file begins like a PDF but could not be parsed as one; it may be "
            "truncated or corrupt"
        ) from exc

    if encrypted:
        raise DocumentTextUnavailableError(
            "the PDF is encrypted, so its text cannot be read without a password; "
            "decrypt it and store the unencrypted document instead"
        )

    chunks: list[str] = []
    for page in pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception as exc:
            raise DocumentTextUnavailableError(
                "a page of the PDF could not be read; its fonts or content streams may be malformed"
            ) from exc

    return ExtractedText(text="\n".join(chunks), kind="pdf", pages=len(pages))
