"""RAG retrieval: embeddings + FAISS vector search, filtered by the caller's permissions.

Embedding backends:
  * sentence-transformers (semantic, recommended)  -> EMBEDDING_BACKEND=auto|sentence-transformers
  * hashing (lightweight keyword-style fallback)    -> used automatically if the model cannot be loaded
Chunks remember which model produced their vector; stale vectors are re-embedded automatically.
"""
import hashlib
import logging
import re
import threading

import numpy as np
from flask import current_app, url_for
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from extensions import db
from models import DocumentChunk, KnowledgeBase
from services.permissions import can_access_document, can_access_kb
from services.text_utils import STOPWORDS, split_text

log = logging.getLogger(__name__)
_lock = threading.RLock()
_embedder = None
_state = {"sig": None, "index": None}


# ---------------------------------------------------------------- embedders
class HashingEmbedder:
    name = "hashing-384"
    dim = 384
    semantic = False

    def encode(self, texts):
        out = np.zeros((len(texts), self.dim), dtype="float32")
        for i, text in enumerate(texts):
            toks = [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOPWORDS]
            feats = toks + [a + "_" + b for a, b in zip(toks, toks[1:])]
            for f in feats:
                h = int(hashlib.md5(f.encode("utf-8")).hexdigest(), 16)
                out[i, h % self.dim] += 1.0 if (h >> 64) & 1 else -1.0
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1
        return out / norms


class STEmbedder:
    semantic = True

    def __init__(self, model_name):
        from sentence_transformers import SentenceTransformer  # heavy import, done lazily
        self.model = SentenceTransformer(model_name)
        self.name = f"st:{model_name}"
        self.dim = self.model.get_sentence_embedding_dimension()

    def encode(self, texts):
        vecs = self.model.encode(list(texts), normalize_embeddings=True, batch_size=32, show_progress_bar=False)
        return np.asarray(vecs, dtype="float32")


def get_embedder():
    global _embedder
    with _lock:
        if _embedder is not None:
            return _embedder
        backend = str(current_app.config.get("EMBEDDING_BACKEND", "auto")).lower()
        if backend != "hashing":
            try:
                _embedder = STEmbedder(current_app.config["EMBEDDING_MODEL"])
                log.info("Embedding backend: %s", _embedder.name)
            except Exception as exc:
                log.warning("sentence-transformers unavailable (%s). Falling back to hashing embeddings.", exc)
        if _embedder is None:
            _embedder = HashingEmbedder()
        return _embedder


def backend_info():
    emb = get_embedder()
    return {"name": emb.name, "semantic": emb.semantic}


def embed_texts(texts):
    return get_embedder().encode(texts)


def vec_to_bytes(vec):
    return np.asarray(vec, dtype="float32").tobytes()


def bytes_to_vec(raw):
    return np.frombuffer(raw, dtype="float32")


# ---------------------------------------------------------------- vector index
class VectorIndex:
    """FAISS IndexFlatIP when available (vectors are L2-normalised => cosine); numpy otherwise."""

    def __init__(self, matrix, ids):
        self.ids = ids
        self.matrix = np.ascontiguousarray(matrix, dtype="float32")
        self.faiss = None
        try:
            import faiss
            self.faiss = faiss.IndexFlatIP(self.matrix.shape[1])
            self.faiss.add(self.matrix)
        except Exception:
            self.faiss = None

    @property
    def engine(self):
        return "FAISS" if self.faiss is not None else "NumPy"

    def search(self, qvec, k):
        k = min(k, len(self.ids))
        q = np.asarray(qvec, dtype="float32").reshape(1, -1)
        if self.faiss is not None:
            scores, idxs = self.faiss.search(q, k)
            return [(self.ids[i], float(s)) for s, i in zip(scores[0], idxs[0]) if i >= 0]
        scores = (self.matrix @ q[0])
        top = np.argsort(-scores)[:k]
        return [(self.ids[i], float(scores[i])) for i in top]


def invalidate():
    with _lock:
        _state["sig"] = None
        _state["index"] = None


def _reembed_stale(emb):
    stale = DocumentChunk.query.filter(
        (DocumentChunk.embedding.is_(None)) | (DocumentChunk.embedding_model != emb.name)
        | (DocumentChunk.embedding_model.is_(None))).all()
    for start in range(0, len(stale), 64):
        batch = stale[start:start + 64]
        vecs = emb.encode([c.content for c in batch])
        for chunk, vec in zip(batch, vecs):
            chunk.embedding = vec_to_bytes(vec)
            chunk.embedding_model = emb.name
    if stale:
        db.session.commit()


def _get_index():
    emb = get_embedder()
    count, max_id = db.session.query(func.count(DocumentChunk.id), func.max(DocumentChunk.id)).one()
    sig = (count, max_id, emb.name)
    with _lock:
        if _state["sig"] == sig and _state["index"] is not None:
            return _state["index"]
        if not count:
            _state["sig"], _state["index"] = sig, None
            return None
        _reembed_stale(emb)
        rows = db.session.query(DocumentChunk.id, DocumentChunk.embedding).order_by(DocumentChunk.id).all()
        ids = [r[0] for r in rows if r[1]]
        matrix = np.vstack([bytes_to_vec(r[1]) for r in rows if r[1]]) if ids else None
        _state["index"] = VectorIndex(matrix, ids) if ids else None
        _state["sig"] = sig
        return _state["index"]


def index_engine():
    idx = _get_index()
    return idx.engine if idx else "FAISS/NumPy (no data yet)"


# ---------------------------------------------------------------- knowledge base indexing
def index_knowledge(entry):
    """(Re)build chunks for a knowledge-base entry so the AI assistant can retrieve it."""
    DocumentChunk.query.filter_by(kb_id=entry.id).delete()
    text = f"{entry.title}\n{entry.content}"
    parts = split_text(text, size=900, overlap=120)
    if parts and entry.is_published:
        vecs = embed_texts(parts)
        emb = get_embedder()
        for i, (part, vec) in enumerate(zip(parts, vecs)):
            db.session.add(DocumentChunk(kb_id=entry.id, chunk_index=i, section=entry.category, content=part,
                                         embedding=vec_to_bytes(vec), embedding_model=emb.name))
    db.session.commit()
    invalidate()


# ---------------------------------------------------------------- search
def _min_score(emb):
    return 0.22 if emb.semantic else 0.08


def _to_result(chunk, score):
    if chunk.document is not None:
        d = chunk.document
        return {"chunk_id": chunk.id, "text": chunk.content, "score": round(score, 3), "title": d.title,
                "type": "document", "page": chunk.page_number, "section": chunk.section,
                "url": url_for("documents.view", doc_id=d.id)}
    k = chunk.kb
    return {"chunk_id": chunk.id, "text": chunk.content, "score": round(score, 3), "title": k.title,
            "type": "knowledge", "page": None, "section": k.category,
            "url": url_for("knowledge.view", kid=k.id)}


def search(user, query, k=None, document_id=None):
    """Return the top-k passages the *user is allowed to see*, most relevant first."""
    k = k or current_app.config["RAG_TOP_K"]
    emb = get_embedder()
    index = _get_index()
    if index is None or not (query or "").strip():
        return []
    qvec = emb.encode([query])[0]
    results = []

    if document_id:  # scoped search inside one document
        rows = (DocumentChunk.query.options(joinedload(DocumentChunk.document))
                .filter_by(document_id=document_id).all())
        scored = []
        for r in rows:
            if r.embedding and r.document is not None and can_access_document(user, r.document):
                scored.append((float(bytes_to_vec(r.embedding) @ qvec), r))
        scored.sort(key=lambda t: t[0], reverse=True)
        return [_to_result(r, s) for s, r in scored[:k]]

    hits = index.search(qvec, 200)
    score_by_id = dict(hits)
    rows = (DocumentChunk.query.options(joinedload(DocumentChunk.document), joinedload(DocumentChunk.kb))
            .filter(DocumentChunk.id.in_(list(score_by_id))).all())
    for r in rows:
        allowed = (r.document is not None and can_access_document(user, r.document)) or \
                  (r.kb is not None and can_access_kb(user, r.kb))
        if allowed and score_by_id[r.id] >= _min_score(emb):
            results.append(_to_result(r, score_by_id[r.id]))
    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:k]


def build_context(results):
    blocks = []
    for i, r in enumerate(results, 1):
        where = f", page {r['page']}" if r.get("page") else (f", {r['section']}" if r.get("section") else "")
        blocks.append(f"[{i}] Source: {r['title']}{where}\n{r['text']}")
    return "\n\n".join(blocks)


def source_list(results):
    seen, out = set(), []
    for i, r in enumerate(results, 1):
        key = (r["title"], r.get("page"), r.get("section"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"n": i, "title": r["title"], "page": r.get("page"), "section": r.get("section"),
                    "url": r["url"], "type": r["type"], "score": r["score"]})
    return out
