"""Upload validation, text extraction, chunking, embedding and storage for documents."""
import json
import logging
import os
import threading
import uuid

from flask import current_app
from werkzeug.utils import secure_filename

from extensions import db
from models import Document, DocumentChunk
from services import rag_service
from services.text_utils import extractive_points, extractive_summary, split_text, top_keywords

log = logging.getLogger(__name__)


class UploadError(ValueError):
    pass


def docs_folder():
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "documents")
    os.makedirs(path, exist_ok=True)
    return path


def doc_path(doc):
    return os.path.join(docs_folder(), doc.stored_name)


def validate_and_save(file_storage):
    """Validate extension, size and magic bytes, then store under a random name.
    Returns (stored_name, ext, size, original_name)."""
    original = secure_filename(file_storage.filename or "")
    if not original or "." not in original:
        raise UploadError("The file has no name or extension.")
    ext = original.rsplit(".", 1)[1].lower()
    if ext not in current_app.config["ALLOWED_DOC_EXTENSIONS"]:
        raise UploadError(f"“.{ext}” files are not allowed. Upload PDF, DOCX or TXT.")
    head = file_storage.stream.read(8)
    file_storage.stream.seek(0)
    if ext == "pdf" and not head.startswith(b"%PDF"):
        raise UploadError("This file is not a valid PDF.")
    if ext == "docx" and not head.startswith(b"PK"):
        raise UploadError("This file is not a valid DOCX document.")
    if ext == "txt" and b"\x00" in file_storage.stream.read(2048):
        file_storage.stream.seek(0)
        raise UploadError("This does not look like a plain-text file.")
    file_storage.stream.seek(0)
    stored = f"{uuid.uuid4().hex}.{ext}"
    full = os.path.join(docs_folder(), stored)
    file_storage.save(full)
    size = os.path.getsize(full)
    if size == 0:
        os.remove(full)
        raise UploadError("The file is empty.")
    return stored, ext, size, original


# ---------------------------------------------------------------- extraction
def extract_segments(path, ext):
    """Return a list of (page_number|None, section|None, text) and the page count."""
    if ext == "pdf":
        from pypdf import PdfReader
        reader = PdfReader(path)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                raise ValueError("The PDF is password-protected.")
        segs = [(i, None, (p.extract_text() or "")) for i, p in enumerate(reader.pages, 1)]
        if not any(s[2].strip() for s in segs):
            raise ValueError("No extractable text found (scanned PDF? OCR is not enabled).")
        return segs, len(reader.pages)
    if ext == "docx":
        from docx import Document as Docx
        d = Docx(path)
        segs, heading, buf = [], "Introduction", []
        for p in d.paragraphs:
            txt = p.text.strip()
            if not txt:
                continue
            if (p.style.name or "").lower().startswith(("heading", "title")):
                if buf:
                    segs.append((None, heading, "\n".join(buf)))
                    buf = []
                heading = txt[:180]
            else:
                buf.append(txt)
        if buf:
            segs.append((None, heading, "\n".join(buf)))
        for ti, table in enumerate(d.tables, 1):
            rows = [" | ".join(c.text.strip() for c in r.cells) for r in table.rows]
            if rows:
                segs.append((None, f"Table {ti}", "\n".join(rows)))
        if not segs:
            raise ValueError("The document contains no text.")
        return segs, None
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    if not text.strip():
        raise ValueError("The file contains no text.")
    return [(None, None, text)], None


def build_chunks(segments):
    out, idx = [], 0
    for page, section, text in segments:
        for part in split_text(text, size=900, overlap=150):
            label = section or (f"Section {idx // 4 + 1}" if page is None else None)
            out.append({"content": part, "page": page, "section": label, "index": idx})
            idx += 1
    return out


# ---------------------------------------------------------------- processing
def start_processing(doc_id):
    app = current_app._get_current_object()
    threading.Thread(target=_run, args=(app, doc_id), daemon=True).start()


def _run(app, doc_id):
    with app.app_context():
        process(doc_id)


def process(doc_id):
    doc = db.session.get(Document, doc_id)
    if doc is None:
        return
    try:
        DocumentChunk.query.filter_by(document_id=doc.id).delete()
        db.session.commit()
        segments, pages = extract_segments(doc_path(doc), doc.file_type)
        chunks = build_chunks(segments)
        if not chunks:
            raise ValueError("No usable text could be extracted.")
        vecs = rag_service.embed_texts([c["content"] for c in chunks])
        model = rag_service.get_embedder().name
        db.session.add_all([
            DocumentChunk(document_id=doc.id, chunk_index=c["index"], page_number=c["page"], section=c["section"],
                          content=c["content"], embedding=rag_service.vec_to_bytes(v), embedding_model=model)
            for c, v in zip(chunks, vecs)])
        full = "\n".join(s[2] for s in segments)
        doc.page_count = pages
        doc.summary = extractive_summary(full)
        doc.key_points = json.dumps(extractive_points(full))
        doc.keywords = json.dumps(top_keywords(full))
        doc.ai_mode = "extractive"
        doc.status, doc.status_message = "ready", None
        db.session.commit()
        rag_service.invalidate()
    except Exception as exc:
        db.session.rollback()
        log.exception("Document processing failed for %s", doc_id)
        doc = db.session.get(Document, doc_id)
        if doc is not None:
            doc.status = "failed"
            doc.status_message = str(exc)[:480]
            db.session.commit()


def full_text(doc, limit=24000):
    """Concatenate chunk text (sampled evenly if the document is very long)."""
    rows = (DocumentChunk.query.filter_by(document_id=doc.id).order_by(DocumentChunk.chunk_index)
            .with_entities(DocumentChunk.content).all())
    texts = [r[0] for r in rows]
    joined = "\n".join(texts)
    if len(joined) <= limit:
        return joined
    step = len(texts) / max(1, limit // 900)
    picked = [texts[int(i * step)] for i in range(int(len(texts) / step))]
    return "\n".join(picked)[:limit]


def delete_document(doc):
    path = doc_path(doc)
    db.session.delete(doc)
    db.session.commit()
    rag_service.invalidate()
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        log.warning("Could not remove file %s", path)
