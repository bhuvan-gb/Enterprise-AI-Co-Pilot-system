from flask import Blueprint, Response, abort, jsonify, render_template, request, url_for
from flask_login import current_user, login_required

from extensions import db
from models import Conversation, Document, Message, utcnow
from services import ai_service, audit_service
from services.permissions import accessible_documents_query
import json

bp = Blueprint("chat", __name__)

SUGGESTIONS_ALL = ["What tasks are due this week?", "Summarise our leave policy", "Draft a polite follow-up email to a client",
                   "What does our IT security guideline say about passwords?", "Give me tips to prioritise my workload"]
SUGGESTIONS_ADMIN = ["Which departments have the most overdue tasks?", "Write an announcement about a policy update"]


def _owned(cid):
    c = db.session.get(Conversation, cid)
    if c is None or c.user_id != current_user.id:
        abort(404)
    return c


@bp.route("/copilot")
@bp.route("/copilot/<int:cid>")
@login_required
def copilot(cid=None):
    convs = Conversation.query.filter_by(user_id=current_user.id).order_by(Conversation.updated_at.desc()).all()
    active = _owned(cid) if cid else None
    docs = accessible_documents_query(current_user).filter_by(status="ready").order_by(Document.title).all()
    suggestions = SUGGESTIONS_ALL + (SUGGESTIONS_ADMIN if current_user.is_admin else [])
    return render_template("chat/copilot.html", conversations=convs, active=active, docs=docs,
                           messages=[m.to_dict() for m in active.messages] if active else [],
                           suggestions=suggestions, live=ai_service.is_configured(),
                           auto_q=request.args.get("q", "")[:1000])


@bp.route("/api/chat/conversations", methods=["POST"])
@login_required
def api_new():
    c = Conversation(user_id=current_user.id)
    db.session.add(c)
    db.session.commit()
    return jsonify(id=c.id, title=c.title, url=url_for("chat.copilot", cid=c.id))


@bp.route("/api/chat/conversations/<int:cid>/rename", methods=["POST"])
@login_required
def api_rename(cid):
    c = _owned(cid)
    title = ((request.get_json(silent=True) or {}).get("title") or "").strip()[:150]
    if not title:
        return jsonify(error="Title cannot be empty."), 400
    c.title = title
    db.session.commit()
    return jsonify(ok=True, title=c.title)


@bp.route("/api/chat/conversations/<int:cid>/delete", methods=["POST"])
@login_required
def api_delete(cid):
    db.session.delete(_owned(cid))
    db.session.commit()
    return jsonify(ok=True, url=url_for("chat.copilot"))


@bp.route("/api/chat/conversations/<int:cid>/clear", methods=["POST"])
@login_required
def api_clear(cid):
    c = _owned(cid)
    Message.query.filter_by(conversation_id=c.id).delete()
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/api/chat/conversations/<int:cid>/send", methods=["POST"])
@login_required
def api_send(cid):
    c = _owned(cid)
    payload = request.get_json(silent=True) or {}
    text = (payload.get("message") or "").strip()
    if not text:
        return jsonify(error="Type a message first."), 400
    if len(text) > 4000:
        return jsonify(error="Message is too long (max 4000 characters)."), 400
    doc_id = payload.get("document_id")
    history = [{"role": m.role, "content": m.content} for m in c.messages[-10:]]
    user_msg = Message(conversation_id=c.id, role="user", content=text)
    db.session.add(user_msg)
    if c.title == "New conversation":
        c.title = text[:60]
    db.session.commit()
    try:
        res = ai_service.answer(current_user, text, history, document_id=int(doc_id) if doc_id else None)
    except ai_service.AIError as exc:
        return jsonify(error=str(exc), title=c.title), 502
    reply = Message(conversation_id=c.id, role="assistant", content=res["text"], mode=res["mode"],
                    sources=json.dumps(res["sources"]))
    c.updated_at = utcnow()
    db.session.add(reply)
    db.session.commit()
    return jsonify(message=reply.to_dict(), title=c.title)


@bp.route("/copilot/<int:cid>/export")
@login_required
def export(cid):
    c = _owned(cid)
    fmt = "txt" if request.args.get("fmt") == "txt" else "md"
    lines = [f"# {c.title}" if fmt == "md" else c.title, f"Exported {utcnow():%d %b %Y %H:%M} UTC", ""]
    for m in c.messages:
        who = "You" if m.role == "user" else "Co-Pilot"
        lines.append(f"**{who}** ({m.created_at:%d %b %H:%M})" if fmt == "md" else f"{who} ({m.created_at:%d %b %H:%M}):")
        lines += [m.content, ""]
    audit_service.record("conversation_exported", f"Conversation {c.id}")
    safe = "".join(ch if ch.isalnum() else "_" for ch in c.title)[:40] or "conversation"
    return Response("\n".join(lines), mimetype="text/plain; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.{fmt}"'})
