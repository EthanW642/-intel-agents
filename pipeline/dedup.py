"""Stage 2a — duplicate collapse, cheapest check first.

1. `dedup_exact`: free hash-based collapse on URL (GDELT and the Google
   News feeds routinely surface the same article more than once) — runs
   before any model work.
2. `dedup_items`: local embedding-based near-duplicate collapse. Runs
   before triage so the LLM triage call (Stage 2b) never scores the same
   underlying story twice.

The sentence-transformers model loaded here (`default_embed_fn`) is the
ONLY embedding model in the whole pipeline — the memory query and Chroma
write-back reuse it instead of loading their own copy (store/chroma_client
used to hold a second SentenceTransformer instance). The embedding function
is injectable so this logic stays unit-testable without downloading a model
or touching the network.
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = "all-MiniLM-L6-v2"
_model = None


def default_embed_fn(texts: list[str]) -> np.ndarray:
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        _model = SentenceTransformer(EMBEDDING_MODEL)
    return np.asarray(_model.encode(texts, normalize_embeddings=True))


def dedup_exact(items: list) -> list:
    """Collapse items that share a URL. Free — no model involved. Keeps the
    item with the longest text so the richest representative survives."""
    by_url: dict[str, object] = {}
    for item in items:
        key = item.url.strip()
        existing = by_url.get(key)
        if existing is None or len(item.text) > len(existing.text):
            by_url[key] = item
    kept = list(by_url.values())
    if len(kept) < len(items):
        logger.info(
            "Exact dedup: %d items -> %d kept (%d URL duplicates collapsed)",
            len(items),
            len(kept),
            len(items) - len(kept),
        )
    return kept


def dedup_items(items: list, similarity_threshold: float = 0.88, embed_fn=None) -> list:
    """Greedy near-duplicate collapse: walk items in order, keep an item
    unless it's above `similarity_threshold` cosine similarity to an
    already-kept item, in which case drop it as a duplicate.

    Compares each candidate only against the kept set (O(n*k) with a small
    k-row matrix) instead of materializing the full n x n similarity
    matrix — at the ~10k-item raw volumes real runs have hit, the full
    matrix costs ~800MB of RAM and a ~100M-iteration Python loop for no
    benefit."""
    if not items:
        return []

    embed_fn = embed_fn or default_embed_fn
    texts = [f"{item.title}. {item.text}" for item in items]
    embeddings = np.asarray(embed_fn(texts))

    kept_indices: list[int] = []
    kept_vectors: list[np.ndarray] = []
    for i in range(len(items)):
        # embeddings are L2-normalized, so dot product == cosine similarity
        if kept_vectors:
            sims = np.stack(kept_vectors) @ embeddings[i]
            if float(sims.max()) >= similarity_threshold:
                continue
        kept_indices.append(i)
        kept_vectors.append(embeddings[i])

    logger.info(
        "Dedup: %d items -> %d kept (%d duplicates collapsed)",
        len(items),
        len(kept_indices),
        len(items) - len(kept_indices),
    )
    return [items[i] for i in kept_indices]
