from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from extensions import db
from models import KB_CATEGORIES, Announcement, Department, KnowledgeBase
from services import audit_service, rag_service
from services.permissions import admin_required, can_access_kb

bp = Blueprint("knowledge", __name__, url_prefix="/knowledge")


def _visible_query():
    q = KnowledgeBase.query
    if not current_user.is_admin:
        q = q.filter(KnowledgeBase.is_published.is_(True))
        conds = [KnowledgeBase.department_id.is_(None)]
        if current_user.department_id:
            conds.append(KnowledgeBase.department_id == current_user.department_id)
        q = q.filter(or_(*conds))
    return q


@bp.route("/")
@login_required
def index():
    cat = request.args.get("category", "")
    term = request.args.get("q", "").strip()
    q = _visible_query()
    if cat:
        q = q.filter(KnowledgeBase.category == cat)
    if term:
        like = f"%{term}%"
        q = q.filter(or_(KnowledgeBase.title.ilike(like), KnowledgeBase.content.ilike(like)))
    entries = q.order_by(KnowledgeBase.category, KnowledgeBase.title).all()
    announcements = Announcement.query.order_by(Announcement.created_at.desc()).limit(5).all()
    return render_template("knowledge/index.html", entries=entries, cat=cat, q=term, categories=KB_CATEGORIES,
                           announcements=announcements)


@bp.route("/<int:kid>")
@login_required
def view(kid):
    entry = db.session.get(KnowledgeBase, kid) or abort(404)
    if not can_access_kb(current_user, entry):
        abort(403)
    return render_template("knowledge/view.html", entry=entry)


def _save(entry):
    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    cat = request.form.get("category")
    if not title or len(title) > 200 or not content or cat not in KB_CATEGORIES:
        flash("Title, category and content are required.", "danger")
        return False
    entry.title, entry.content, entry.category = title, content, cat
    entry.department_id = request.form.get("department_id", type=int) or None
    entry.is_published = "is_published" in request.form
    return True


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new():
    if request.method == "POST":
        entry = KnowledgeBase(created_by=current_user.id)
        if _save(entry):
            db.session.add(entry)
            db.session.commit()
            rag_service.index_knowledge(entry)
            audit_service.record("knowledge_created", entry.title)
            flash("Knowledge base entry created and indexed for the AI assistant.", "success")
            return redirect(url_for("knowledge.view", kid=entry.id))
    return render_template("knowledge/form.html", entry=None, categories=KB_CATEGORIES,
                           departments=Department.query.order_by(Department.name).all())


@bp.route("/<int:kid>/edit", methods=["GET", "POST"])
@admin_required
def edit(kid):
    entry = db.session.get(KnowledgeBase, kid) or abort(404)
    if request.method == "POST" and _save(entry):
        db.session.commit()
        rag_service.index_knowledge(entry)
        audit_service.record("knowledge_updated", entry.title)
        flash("Entry updated.", "success")
        return redirect(url_for("knowledge.view", kid=entry.id))
    return render_template("knowledge/form.html", entry=entry, categories=KB_CATEGORIES,
                           departments=Department.query.order_by(Department.name).all())


@bp.route("/<int:kid>/delete", methods=["POST"])
@admin_required
def delete(kid):
    entry = db.session.get(KnowledgeBase, kid) or abort(404)
    title = entry.title
    db.session.delete(entry)
    db.session.commit()
    rag_service.invalidate()
    audit_service.record("knowledge_deleted", title)
    flash("Entry deleted.", "success")
    return redirect(url_for("knowledge.index"))
