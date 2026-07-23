"""Local, file-based vector store (spec section 3.A).

One Chroma collection per domain, holding one entry per analyzed event:
embedding of a one-line summary, with metadata pointing back to the full
SQLite event record and briefing file.
"""
from __future__ import annotations

import chromadb
from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction

EMBEDDING_MODEL = "all-MiniLM-L6-v2"


def get_client(persist_dir: str) -> chromadb.ClientAPI:
    return chromadb.PersistentClient(path=persist_dir)


def get_domain_collection(client: chromadb.ClientAPI, domain: str):
    embed_fn = SentenceTransformerEmbeddingFunction(model_name=EMBEDDING_MODEL)
    return client.get_or_create_collection(
        name=f"{domain}_events",
        embedding_function=embed_fn,
        metadata={"hnsw:space": "cosine"},
    )


def add_event(collection, event_id: str, summary: str, metadata: dict) -> None:
    collection.upsert(ids=[event_id], documents=[summary], metadatas=[metadata])


def query_related(collection, query_text: str, n_results: int) -> list[dict]:
    if collection.count() == 0:
        return []
    n_results = min(n_results, collection.count())
    results = collection.query(query_texts=[query_text], n_results=n_results)
    related = []
    for doc, meta, dist in zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    ):
        related.append({"summary": doc, "metadata": meta, "distance": dist})
    return related
