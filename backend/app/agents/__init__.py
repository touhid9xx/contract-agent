"""The local-model advisory boundary (P2-D).

Nothing in this package decides anything. It carries the prompt text that is
sent to Ollama and the registry that versions it; the selection logic that wraps
it in policy lives in :mod:`app.workers.advisory`. The split is deliberate: the
prompt is data an operator may edit, while the rules that bound what a model may
influence are code that must be reviewed.
"""

from __future__ import annotations
