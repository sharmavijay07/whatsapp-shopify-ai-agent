"""Retrieval over the catalogue and shop policies.

Groq does not offer embeddings. Locally, sentence-transformers MiniLM vectors
live in Qdrant. On Render's 512 MB free instance the default is an in-memory
BM25 index, which imports neither Qdrant nor PyTorch.

Qdrant Cloud (QDRANT_URL + QDRANT_API_KEY) is used when configured. Embeddings
then come from a hosted OpenAI-compatible API (Jina's free tier fits the
memory limit) or from fastembed. If that setup fails to load, retrieval falls
back to BM25.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import uuid
from pathlib import Path

from harbor.config import Settings
from harbor.db import Database

logger = logging.getLogger("harbor.rag")

_MODEL = None
_MODEL_NAME = ""
_MODEL_LOCK = threading.Lock()

_STOP = {
    "a", "an", "the", "is", "are", "of", "for", "to", "and", "or", "my", "your",
    "you", "we", "i", "do", "does", "what", "how", "can", "i", "me", "it", "this",
    "that", "with", "on", "in", "if", "please",
}


def _stem(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    raw = re.findall(r"[a-z0-9']+", text.lower())
    return [_stem(token) for token in raw if token not in _STOP and len(token) > 1]


class MemoryIndex:
    """Okapi BM25 over the chunk list. Pure Python, a few hundred KB of RAM."""

    def __init__(self) -> None:
        self.k1 = 1.4
        self.b = 0.75
        self.doc_tokens: list[list[str]] = []
        self.df: dict[str, int] = {}
        self.avgdl = 0.0

    def build(self, chunks: list[dict]) -> None:
        self.doc_tokens = []
        df: dict[str, int] = {}
        for chunk in chunks:
            tokens = tokenize(f"{chunk['title']} {chunk['text']}")
            self.doc_tokens.append(tokens)
            for token in set(tokens):
                df[token] = df.get(token, 0) + 1
        self.df = df
        self.avgdl = (
            sum(len(tokens) for tokens in self.doc_tokens) / len(self.doc_tokens)
            if self.doc_tokens
            else 0.0
        )

    def rank(self, query: str, chunks: list[dict]) -> list[tuple[float, str]]:
        if len(self.doc_tokens) != len(chunks):
            self.build(chunks)
        query_tokens = tokenize(query)
        total = len(chunks)
        scored: list[tuple[float, str]] = []
        for index, tokens in enumerate(self.doc_tokens):
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            length = len(tokens) or 1
            score = 0.0
            for term in query_tokens:
                freq = counts.get(term, 0)
                if not freq:
                    continue
                docs_with_term = self.df.get(term, 0)
                idf = math.log(1.0 + (total - docs_with_term + 0.5) / (docs_with_term + 0.5))
                denom = freq + self.k1 * (1 - self.b + self.b * length / (self.avgdl or 1.0))
                score += idf * (freq * (self.k1 + 1)) / denom
            scored.append((score, chunks[index]["id"]))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored


def get_embedder(model_name: str, device: str):
    global _MODEL, _MODEL_NAME
    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_NAME == model_name:
            return _MODEL
        from sentence_transformers import SentenceTransformer

        _MODEL = SentenceTransformer(model_name, device=device)
        _MODEL_NAME = model_name
        return _MODEL


def _l2(vector: list[float]) -> list[float]:
    norm = sum(value * value for value in vector) ** 0.5
    if norm <= 0:
        return vector
    return [value / norm for value in vector]


def qdrant_client_kwargs(settings: Settings) -> dict:
    kwargs = {
        "url": settings.qdrant_url.strip(),
        "prefer_grpc": False,
        "timeout": 20,
    }
    api_key = settings.qdrant_api_key.strip()
    if api_key:
        kwargs["api_key"] = api_key
    return kwargs


class _SentenceEmbedder:
    def __init__(self, model_name: str, device: str):
        self._model = get_embedder(model_name, device)
        dimension = getattr(self._model, "get_embedding_dimension", None)
        if dimension is None:
            dimension = self._model.get_sentence_embedding_dimension
        self.dimension = int(dimension())

    def encode(self, texts: list[str]) -> list[list[float]]:
        matrix = self._model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [row.tolist() for row in matrix]


class _FastEmbedder:
    def __init__(self, model_name: str):
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model_name)
        probe = next(iter(self._model.embed(["harbor"])))
        self.dimension = len(list(probe))

    def encode(self, texts: list[str]) -> list[list[float]]:
        rows = []
        for vector in self._model.embed(texts):
            rows.append(_l2([float(value) for value in list(vector)]))
        return rows


class _HostedEmbedder:
    """OpenAI-compatible POST {base}/embeddings. Jina's free API speaks this."""

    def __init__(self, settings: Settings, client):
        self.settings = settings
        self.client = client
        self._dimension = 0
        self.dimension = len(self.encode(["harbor"])[0])

    def _url(self) -> str:
        base = self.settings.embedding_api_url.strip().rstrip("/")
        if not base:
            raise RuntimeError("EMBEDDING_API_URL is empty.")
        if base.endswith("/embeddings"):
            return base
        return base + "/embeddings"

    def encode(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        key = self.settings.embedding_api_key.strip()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        response = self.client.post(
            self._url(),
            headers=headers,
            json={"model": self.settings.resolved_embedding_model(), "input": texts},
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") or []
        ordered = sorted(data, key=lambda item: item.get("index", 0))
        vectors = [_l2([float(value) for value in item["embedding"]]) for item in ordered]
        if len(vectors) != len(texts):
            raise RuntimeError("Embedding API returned the wrong number of vectors.")
        return vectors


def build_embedder(settings: Settings, client=None):
    provider = settings.embedding_provider.strip().lower()
    if provider == "fastembed":
        return _FastEmbedder(settings.resolved_embedding_model())
    if provider == "hosted":
        import httpx

        return _HostedEmbedder(settings, client or httpx.Client(timeout=20.0))
    return _SentenceEmbedder(settings.resolved_embedding_model(), settings.embedding_device)


class KnowledgeBase:
    def __init__(self, settings: Settings, db: Database, http_client=None):
        self.settings = settings
        self.db = db
        self.chunks: list[dict] = []
        self.client = None
        self.embedder = None
        self.dim = 0
        self.collection = settings.qdrant_collection
        self.memory = MemoryIndex()
        self._http = http_client
        self._owns_http = False
        if settings.uses_memory_index():
            self.backend = "memory"
            return
        try:
            self._init_qdrant()
            self.backend = "qdrant"
        except Exception as exc:  # noqa: BLE001 — a bad embedder must not take down the free tier
            logger.warning("Qdrant embeddings unavailable (%s). Using in-memory BM25.", exc)
            if self._owns_http and self._http is not None:
                self._http.close()
            self._http = None
            self._owns_http = False
            self.backend = "memory"
            self.client = None
            self.embedder = None
            self.dim = 0

    def _init_qdrant(self) -> None:
        from qdrant_client import QdrantClient

        if (
            self.settings.embedding_provider.strip().lower() == "hosted"
            and self._http is None
        ):
            import httpx

            self._http = httpx.Client(timeout=20.0)
            self._owns_http = True
        self.embedder = build_embedder(self.settings, self._http)
        self.dim = int(self.embedder.dimension)
        if self.settings.qdrant_url.strip():
            self.client = QdrantClient(**qdrant_client_kwargs(self.settings))
        else:
            path = Path(self.settings.qdrant_path)
            path.mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(path))

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
        closer = getattr(self._http, "close", None)
        if closer and getattr(self, "_owns_http", False):
            closer()

    def load_markdown(self, directory: Path) -> list[dict]:
        documents = []
        if not directory.exists():
            return documents
        for path in sorted(directory.glob("*.md")):
            raw = path.read_text(encoding="utf-8").strip()
            if not raw:
                continue
            parts = re.split(r"\n(?=## )", raw)
            for part in parts:
                lines = part.strip().splitlines()
                if not lines:
                    continue
                if lines[0].startswith("#"):
                    title = lines[0].lstrip("#").strip()
                    text = "\n".join(lines[1:]).strip()
                else:
                    title = path.stem.replace("-", " ").title()
                    text = part.strip()
                documents.append(
                    {
                        "id": "doc:" + hashlib.sha256(f"{title}\n{text}".encode()).hexdigest()[:16],
                        "title": title,
                        "text": text,
                        "source": path.name,
                        "kind": "policy",
                    }
                )
        return documents

    def ensure(self, documents: list[dict]) -> None:
        chunks = []
        for document in documents:
            title = document["title"].strip()
            text = document["text"].strip()
            if not text:
                continue
            chunk_id = document.get("id") or hashlib.sha256(f"{title}\n{text}".encode()).hexdigest()[:16]
            chunks.append(
                {
                    "id": chunk_id,
                    "title": title,
                    "text": text,
                    "source": document.get("source") or "knowledge",
                    "kind": document.get("kind") or "policy",
                }
            )
        digest = hashlib.sha256(
            json.dumps(chunks, sort_keys=True).encode()
        ).hexdigest()
        self.chunks = chunks
        if self.backend == "memory":
            self.memory.build(chunks)
            if chunks:
                self.db.replace_chunks(chunks)
                self.db.set_meta("kb_hash", digest)
                self.db.set_meta("kb_model", "bm25")
            return
        model_key = (
            f"{self.settings.embedding_provider}:{self.settings.resolved_embedding_model()}"
        )
        if (
            self.db.get_meta("kb_hash") == digest
            and self.db.get_meta("kb_model") == model_key
            and self.vector_count() == len(chunks)
            and chunks
        ):
            self.chunks = self.db.list_chunks() or chunks
            return
        if not chunks:
            return
        vectors = self._embed([f"{chunk['title']}\n{chunk['text']}" for chunk in chunks])
        self._replace_collection(chunks, vectors)
        self.db.replace_chunks(chunks)
        self.db.set_meta("kb_hash", digest)
        self.db.set_meta("kb_model", model_key)
        self.chunks = chunks

    def _embed(self, texts: list[str]) -> list[list[float]]:
        return self.embedder.encode(texts)

    def _replace_collection(self, chunks: list[dict], vectors: list[list[float]]) -> None:
        from qdrant_client.models import Distance, PointStruct, VectorParams

        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=VectorParams(size=self.dim, distance=Distance.COSINE),
        )
        points = []
        for chunk, vector in zip(chunks, vectors, strict=True):
            points.append(
                PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, chunk["id"])),
                    vector=vector,
                    payload=chunk,
                )
            )
        self.client.upsert(collection_name=self.collection, points=points)

    def vector_count(self) -> int:
        if self.backend == "memory":
            return len(self.chunks)
        if self.client is None or not self.client.collection_exists(self.collection):
            return 0
        info = self.client.get_collection(self.collection)
        count = getattr(info, "points_count", None)
        return int(count or 0)

    def search(self, query: str, k: int = 4) -> list[dict]:
        if not self.chunks:
            self.chunks = self.db.list_chunks()
            if self.backend == "memory":
                self.memory.build(self.chunks)
        if not self.chunks:
            return []
        if self.backend == "memory":
            return self._memory_search(query, k)
        vector_rank = self._vector_rank(query, limit=max(k * 3, 8))
        lexical_rank = self._lexical_rank(query)
        fused: dict[str, float] = {}
        by_id = {chunk["id"]: chunk for chunk in self.chunks}
        for rank, (chunk_id, score) in enumerate(vector_rank):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + (1.0 / (8 + rank)) + max(score, 0) * 0.15
        for rank, (chunk_id, score) in enumerate(lexical_rank):
            if score <= 0:
                continue
            fused[chunk_id] = fused.get(chunk_id, 0.0) + (1.0 / (8 + rank))
        vector_scores = {chunk_id: score for chunk_id, score in vector_rank}
        lexical_scores = {chunk_id: score for chunk_id, score in lexical_rank}
        ordered = sorted(fused, key=lambda chunk_id: fused[chunk_id], reverse=True)[:k]
        hits = []
        for chunk_id in ordered:
            chunk = by_id.get(chunk_id)
            if not chunk:
                continue
            hits.append(
                {
                    "id": chunk["id"],
                    "title": chunk["title"],
                    "text": chunk["text"],
                    "source": chunk["source"],
                    "kind": chunk["kind"],
                    "vector": round(float(vector_scores.get(chunk_id, 0.0)), 4),
                    "lexical": round(float(lexical_scores.get(chunk_id, 0.0)), 4),
                }
            )
        return hits

    def _memory_search(self, query: str, k: int) -> list[dict]:
        lexical_scores = dict(self._lexical_rank(query))
        ranked = [(score, chunk_id) for score, chunk_id in self.memory.rank(query, self.chunks) if score > 0]
        if not ranked:
            ranked = [(lexical_scores.get(chunk["id"], 0.0), chunk["id"]) for chunk in self.chunks]
            ranked = [item for item in ranked if item[0] > 0]
            ranked.sort(key=lambda item: item[0], reverse=True)
        by_id = {chunk["id"]: chunk for chunk in self.chunks}
        hits = []
        for _score, chunk_id in ranked[:k]:
            chunk = by_id.get(chunk_id)
            if not chunk:
                continue
            hits.append(
                {
                    "id": chunk["id"],
                    "title": chunk["title"],
                    "text": chunk["text"],
                    "source": chunk["source"],
                    "kind": chunk["kind"],
                    "vector": 0.0,
                    "lexical": round(float(lexical_scores.get(chunk_id, 0.0)), 4),
                }
            )
        return hits

    def _vector_rank(self, query: str, limit: int) -> list[tuple[str, float]]:
        vector = self._embed([query])[0]
        if hasattr(self.client, "query_points"):
            result = self.client.query_points(
                collection_name=self.collection,
                query=vector,
                limit=limit,
            )
            points = result.points
        else:
            points = self.client.search(
                collection_name=self.collection,
                query_vector=vector,
                limit=limit,
            )
        ranked = []
        for point in points:
            payload = point.payload or {}
            chunk_id = payload.get("id")
            if chunk_id:
                ranked.append((chunk_id, float(point.score or 0)))
        return ranked

    def _lexical_rank(self, query: str) -> list[tuple[str, float]]:
        query_tokens = set(tokenize(query))
        scored = []
        for chunk in self.chunks:
            title_tokens = set(tokenize(chunk["title"]))
            body_tokens = set(tokenize(chunk["text"]))
            if not query_tokens:
                scored.append((chunk["id"], 0.0))
                continue
            overlap = 0.0
            for token in query_tokens:
                if token in title_tokens:
                    overlap += 2.0
                elif token in body_tokens:
                    overlap += 1.0
            scored.append((chunk["id"], overlap / (len(query_tokens) + 1)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored
