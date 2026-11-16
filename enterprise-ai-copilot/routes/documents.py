import json
import os

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request,
                   send_file, url_for)
from flask_login import current_user, login_required
from sqlalchemy import or_

from extensions import db
from models import DOC_CATEGORIES, VISIBILITIES, Document, DocumentChunk, User
from services import ai_service, audit_service, document_service, notification_service, rag_service
from services.permissions import (accessible_documents_query, can_access_document, can_manage_document)
from services.utils import clamp

bp = Blueprint("documents", __name__, url_prefix="/documents")


def _get(doc_id, manage=False):
    doc = db.session.get(Document, doc_id)
    if doc is None:
        abort(404)
    allowed = can_manage_document(current_user, doc) if manage else can_access_document(current_user, doc)
    if not allowed:
        audit_service.record("document_access_denied", f"Document {doc_id}", "denied")
        abort(403)
    return doc


def _wants_json():
    return request.headers.get("X-Requested-With") == "fetch"


@bp.route("/")
@login_required
def library():
    q = request.args.get("q", "").strip()
    cat = request.args.get("category", "")
    query = accessible_documents_query(current_user)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Document.title.ilike(like), Document.filename.ilike(like),
                                 Document.keywords.ilike(like), Document.description.ilike(like)))
    if cat:
        query = query.filter(Document.category == cat)
    page = request.args.get("page", 1, type=int)
    pag = query.order_by(Document.created_at.desc()).paginate(page=page, per_page=12, error_out=False)
    processing = any(d.status == "processing" for d in pag.items)
    return render_template("documents/library.html", pag=pag, q=q, cat=cat, categories=DOC_CATEGORIES,
                           visibilities=VISIBILITIES, processing=processing, backend=rag_service.backend_info())


@bp.route("/upload", methods=["POST"])
@login_required
def upload():
    files = [f for f in request.files.getlist("files") if f and f.filename]
    if not files:
        msg = "Choose at least one PDF, DOCX or TXT file."
        return (jsonify(error=msg), 400) if _wants_json() else (flash(msg, "danger") or redirect(url_for("documents.library")))
    category = request.form.get("category", "General")
    category = category if category in DOC_CATEGORIES else "General"
    visibility = request.form.get("visibility", "private")
    if visibility not in VISIBILITIES or (visibility == "department" and not current_user.department_id):
        visibility = "private"
    ok, errors = [], []
    for f in files:
        try:
            stored, ext, size, original = document_service.validate_and_save(f)
        except document_service.UploadError as exc:
            errors.append(f"{f.filename}: {exc}")
            continue
        doc = Document(title=clamp(request.form.get("title") if len(files) == 1 and request.form.get("title")
                                   else original.rsplit(".", 1)[0].replace("_", " "), 250),
                       filename=original, stored_name=stored, file_type=ext, file_size=size, category=category,
                       description=clamp(request.form.get("description"), 490) or None, owner_id=current_user.id,
                       department_id=current_user.department_id, visibility=visibility, status="processing")
        db.session.add(doc)
        db.session.commit()
        audit_service.record("document_uploaded", f"{doc.title} (#{doc.id}, {ext}, {size} bytes)")
        document_service.start_processing(doc.id)
        recipients = []
        if visibility == "company":
            recipients = User.query.filter(User.active.is_(True), User.id != current_user.id).all()
        elif visibility == "department":
            recipients = User.query.filter(User.active.is_(True), User.id != current_user.id,
                                           User.department_id == current_user.department_id).all()
        notification_service.notify_users(recipients, "New document shared", f"{current_user.full_name} uploaded “{doc.title}”.",
                                          "document", url_for("documents.view", doc_id=doc.id))
        ok.append(doc.id)
    if _wants_json():
        return jsonify(uploaded=len(ok), errors=errors), (200 if ok else 400)
    for e in errors:
        flash(e, "danger")
    if ok:
        flash(f"{len(ok)} document(s) uploaded. Processing has started.", "success")
    return redirect(url_for("documents.library"))


@bp.route("/<int:doc_id>")
@login_required
def view(doc_id):
    doc = _get(doc_id)
    preview = document_service.full_text(doc, 3500) if doc.status == "ready" else ""
    chunk_count = DocumentChunk.query.filter_by(document_id=doc.id).count()
    return render_template("documents/view.html", doc=doc, preview=preview, chunk_count=chunk_count, categories=DOC_CATEGORIES,
                           visibilities=VISIBILITIES, can_manage=can_manage_document(current_user, doc),
                           live=ai_service.is_configured())


@bp.route("/<int:doc_id>/file")
@login_required
def file(doc_id):
    doc = _get(doc_id)
    path = document_service.doc_path(doc)
    if not os.path.exists(path):
        abort(404)
    download = request.args.get("download") == "1"
    mimes = {"pdf": "application/pdf", "txt": "text/plain",
             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
    if download:
        audit_service.record("document_downloaded", f"{doc.title} (#{doc.id})")
    return send_file(path, mimetype=mimes[doc.file_type], as_attachment=download or doc.file_type != "pdf",
                     download_name=doc.filename)


@bp.route("/<int:doc_id>/status")
@login_required
def status(doc_id):
    doc = _get(doc_id)
    return jsonify(status=doc.status, message=doc.status_message)


@bp.route("/<int:doc_id>/summarize", methods=["POST"])
@login_required
def summarize(doc_id):
    doc = _get(doc_id)
    if doc.status != "ready":
        return jsonify(error="The document is not processed yet."), 409
    try:
        res = ai_service.summarize_document(doc, document_service.full_text(doc))
    except ai_service.AIError as exc:
        return jsonify(error=str(exc)), 502
    if can_manage_document(current_user, doc):
        doc.summary, doc.key_points = res["summary"], json.dumps(res["key_points"])
        doc.keywords, doc.ai_mode = json.dumps(res["keywords"]), res["mode"]
        db.session.commit()
    return jsonify(res)


@bp.route("/<int:doc_id>/ask", methods=["POST"])
@login_required
def ask(doc_id):
    doc = _get(doc_id)
    question = ((request.get_json(silent=True) or {}).get("question") or "").strip()
    if not question:
        return jsonify(error="Type a question first."), 400
    if doc.status != "ready":
        return jsonify(error="The document is still being processed."), 409
    try:
        res = ai_service.answer(current_user, question, document_id=doc.id)
    except ai_service.AIError as exc:
        return jsonify(error=str(exc)), 502
    return jsonify(res)


@bp.route("/<int:doc_id>/update", methods=["POST"])
@login_required
def update(doc_id):
    doc = _get(doc_id, manage=True)
    title = request.form.get("title", "").strip()
    if title:
        doc.title = clamp(title, 250)
    if request.form.get("category") in DOC_CATEGORIES:
        doc.category = request.form["category"]
    vis = request.form.get("visibility")
    if vis in VISIBILITIES and not (vis == "department" and not doc.owner.department_id):
        doc.visibility = vis
        doc.department_id = doc.owner.department_id
    doc.description = clamp(request.form.get("description"), 490) or None
    db.session.commit()
    rag_service.invalidate()
    flash("Document details updated.", "success")
    return redirect(url_for("documents.view", doc_id=doc.id))


@bp.route("/<int:doc_id>/reprocess", methods=["POST"])
@login_required
def reprocess(doc_id):
    doc = _get(doc_id, manage=True)
    doc.status, doc.status_message = "processing", None
    db.session.commit()
    document_service.start_processing(doc.id)
    flash("Reprocessing started.", "info")
    return redirect(url_for("documents.view", doc_id=doc.id))


@bp.route("/<int:doc_id>/delete", methods=["POST"])
@login_required
def delete(doc_id):
    doc = _get(doc_id, manage=True)
    title, did = doc.title, doc.id
    document_service.delete_document(doc)
    audit_service.record("document_deleted", f"{title} (#{did})")
    flash(f"“{title}” was deleted.", "success")
    return redirect(url_for("documents.library"))


@bp.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()
    results = rag_service.search(current_user, q, k=10) if q else []
    return render_template("documents/search.html", q=q, results=results, backend=rag_service.backend_info())
