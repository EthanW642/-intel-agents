"""Stage 2a — local embedding-based near-duplicate collapse.

Runs before triage so the LLM triage call (Stage 2b) never scores the same
underlying story twice. Embedding function is injectable so this logic is
unit-testable without downloading a model or touching the network.
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


def _cosine_sim_matrix(embeddings: np.ndarray) -> np.ndarray:
    # embeddings are assumed L2-normalized, so dot product == cosine similarity.
    return embeddings @ embeddings.T


def dedup_items(items: list, similarity_threshold: float = 0.88, embed_fn=None) -> list:
    """Greedy near-duplicate collapse: walk items in order, keep an item
    unless it's above `similarity_threshold` cosine similarity to an
    already-kept item, in which case drop it as a duplicate."""
    if not items:
        return []

    embed_fn = embed_fn or default_embed_fn
    texts = [f"{item.title}. {item.text}" for item in items]
    embeddings = embed_fn(texts)
    sims = _cosine_sim_matrix(np.asarray(embeddings))

    kept_indices: list[int] = []
    for i in range(len(items)):
        is_duplicate = any(sims[i, j] >= similarity_threshold for j in kept_indices)
        if not is_duplicate:
            kept_indices.append(i)

    logger.info(
        "Dedup: %d items -> %d kept (%d duplicates collapsed)",
        len(items),
        len(kept_indices),
        len(items) - len(kept_indices),
    )
    return [items[i] for i in kept_indices]
