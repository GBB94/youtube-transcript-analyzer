"""Retrieval subsystem (RETRIEVAL_DESIGN.md, R1-R6) — chunk, context, embed,
index (ONE LanceDB store: dense + native FTS), retrieve, answer, eval.

Everything here is DERIVED: a pure, versioned function of the canonical corpus
layer, rebuildable offline. Version constants are the index cache key (§14);
no silent behavioural change without a bump."""
