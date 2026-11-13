"""Hybrid retrieval: local sentence-transformers vectors in Qdrant, plus keywords.

Groq does not offer an embeddings API, so the default embedder is
sentence-transformers/all-MiniLM-L6-v2 running on CPU. No embedding key is read.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from pathlib import Path

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from harbor.config import Settings
from harbor.db import Database

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


def get_embedder(model_name: str, device: str):
    global _MODEL, _MODEL_NAME
    with _MODEL_LOCK:
        if _MODEL is not None and _MODEL_NAME == model_name:
            return _MODEL
        from sentence_transformers import SentenceTransformer

        _MODEL = SentenceTransformer(model_name, device=device)
        _MODEL_NAME = model_name
        return _MODEL


class KnowledgeBase:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self.embedder = get_embedder(settings.embedding_model, settings.embedding_device)
        dimension = getattr(self.embedder, "get_embedding_dimension", None)
        if dimension is None:
            dimension = self.embedder.get_sentence_embedding_dimension
        self.dim = int(dimension())
        if settings.qdrant_url.strip():
            self.client = QdrantClient(url=settings.qdrant_url.strip(), prefer_grpc=False)
        else:
            path = Path(settings.qdrant_path)
            path.mkdir(parents=True, exist_ok=True)
            self.client = QdrantClient(path=str(path))
        self.collection = settings.qdrant_collection
        self.chunks: list[dict] = []

    def close(self) -> None:
        self.client.close()

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
        if (
            self.db.get_meta("kb_hash") == digest
            and self.db.get_meta("kb_model") == self.settings.embedding_model
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
        self.db.set_meta("kb_model", self.settings.embedding_model)
        self.chunks = chunks

    def _embed(self, texts: list[str]) -> list[list[float]]:
        matrix = self.embedder.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [row.tolist() for row in matrix]

    def _replace_collection(self, chunks: list[dict], vectors: list[list[float]]) -> None:
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
        if not self.client.collection_exists(self.collection):
            return 0
        info = self.client.get_collection(self.collection)
        count = getattr(info, "points_count", None)
        return int(count or 0)

    def search(self, query: str, k: int = 4) -> list[dict]:
        if not self.chunks:
            self.chunks = self.db.list_chunks()
        if not self.chunks:
            return []
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
