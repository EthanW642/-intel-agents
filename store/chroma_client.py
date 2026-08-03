"""Local, file-based vector store (spec section 3.A).

One Chroma collection per domain, holding one entry per analyzed event:
embedding of a one-line summary, with metadata pointing back to the full
SQLite event record and briefing file.

Embeddings are computed by the caller and passed in explicitly (the single
shared sentence-transformers instance in pipeline/dedup.py) — Chroma is NOT
given its own embedding function, so only one copy of the model is ever
resident. NOTE: if you are upgrading from a version of this project where
Chroma owned a SentenceTransformerEmbeddingFunction, delete data/chroma/
once and let it rebuild from the next run's write-backs — the stored
collection config may otherwise try to reconstruct the old embedding
function.
"""
from __future__ import annotations

import chromadb

EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # informational; the actual model lives in pipeline/dedup.py


def get_client(persist_dir: str) -> chromadb.ClientAPI:
    return chromadb.PersistentClient(path=persist_dir)


def get_domain_collection(client: chromadb.ClientAPI, domain: str):
    return client.get_or_create_collection(
        name=f"{domain}_events",
        metadata={"hnsw:space": "cosine"},
    )


def add_events(collection, event_ids: list[str], summaries: list[str], embeddings, metadatas: list[dict]) -> None:
    if not event_ids:
        return
    collection.upsert(
        ids=event_ids,
        documents=summaries,
        embeddings=[list(map(float, e)) for e in embeddings],
        metadatas=metadatas,
    )


def query_related(collection, query_embedding, n_results: int) -> list[dict]:
    if collection.count() == 0:
        return []
    n_results = min(n_results, collection.count())
    results = collection.query(
        query_embeddings=[list(map(float, query_embedding))], n_results=n_results
    )
    related = []
    for doc, meta, dist in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        related.append({"summary": doc, "metadata": meta, "distance": dist})
    return related
