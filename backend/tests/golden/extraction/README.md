# Golden corpus: extraction

Eight documents and the field values a careful reader finds in each. The pattern
backend is scored against them by `tests/test_extraction_eval.py`, and anyone can
re-run the measurement:

    python -m app.cli evaluate-extraction --golden tests/golden/extraction/manifest.json

## What `expected` means

It is the value the pipeline should **store**, not every string of that shape that
appears in the document. `06-utility-notice.txt` contains
`billing@wessexwater.example.test`; that is the water company's address, not the
customer's contact, so the document states no `email` and predicting one is counted
as a false positive. Getting this wrong in the other direction — writing down a
value the document does not contain — would inflate the score, so every entry was
read off the document itself.

A field a document does not mention is left out of `expected`. Predicting it anyway
is also a false positive.

## What this corpus does not prove

It was written by the same person who wrote the regexes it scores, so it is a
regression net: it fails when extraction gets worse. It is **not** an accuracy claim
about customers' contracts. For that, label your own documents in this format and
point `--golden` at the manifest; the number you get is the one worth quoting.

`address` and `dob` are stated on purpose even though the pattern backend cannot
reach them — a corpus that only asks for what the code already does measures
nothing. They show up in the report as unreachable fields with recall 0, which is
gap 17 as a number rather than as a note.
