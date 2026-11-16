from datetime import timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from extensions import db
from models import Announcement, AuditLog, User, utcnow
from flask_login import current_user
from services import audit_service, notification_service
from services.permissions import admin_required
from services.utils import parse_date
from datetime import datetime

bp = Blueprint("admin", __name__, url_prefix="/admin")


@bp.route("/audit")
@admin_required
def audit():
    q = AuditLog.query
    action = request.args.get("action", "").strip()
    status = request.args.get("status", "")
    term = request.args.get("q", "").strip()
    start, end = parse_date(request.args.get("start")), parse_date(request.args.get("end"))
    if action:
        q = q.filter(AuditLog.action == action)
    if status in ("success", "failure", "denied"):
        q = q.filter(AuditLog.status == status)
    if term:
        like = f"%{term}%"
        q = q.filter(AuditLog.user_email.ilike(like) | AuditLog.details.ilike(like) | AuditLog.ip_address.ilike(like))
    if start:
        q = q.filter(AuditLog.created_at >= datetime.combine(start, datetime.min.time()))
    if end:
        q = q.filter(AuditLog.created_at <= datetime.combine(end, datetime.max.time()))
    pag = q.order_by(AuditLog.created_at.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=25,
                                                           error_out=False)
    day = utcnow() - timedelta(hours=24)
    summary = {"events": AuditLog.query.filter(AuditLog.created_at >= day).count(),
               "failed_logins": AuditLog.query.filter(AuditLog.action == "login_failed", AuditLog.created_at >= day).count(),
               "denied": AuditLog.query.filter(AuditLog.status == "denied", AuditLog.created_at >= day).count(),
               "locked": User.query.filter(User.locked_until > utcnow()).count()}
    actions = [a[0] for a in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action)]
    return render_template("admin/audit.html", pag=pag, actions=actions, summary=summary, action=action, status=status,
                           q=term, start=request.args.get("start", ""), end=request.args.get("end", ""))


@bp.route("/announcements", methods=["POST"])
@admin_required
def announcement_create():
    title, body = request.form.get("title", "").strip(), request.form.get("body", "").strip()
    if not title or len(title) > 200 or not body:
        flash("An announcement needs a title and a message.", "danger")
        return redirect(url_for("main.notifications"))
    a = Announcement(title=title, body=body, author_id=current_user.id, is_pinned="is_pinned" in request.form)
    db.session.add(a)
    db.session.commit()
    users = User.query.filter(User.active.is_(True), User.id != current_user.id).all()
    notification_service.notify_users(users, f"Announcement: {title}", body[:300], "announcement",
                                      url_for("main.notifications"))
    audit_service.record("announcement_posted", title)
    flash(f"Announcement sent to {len(users)} people.", "success")
    return redirect(url_for("main.notifications"))


@bp.route("/announcements/<int:aid>/delete", methods=["POST"])
@admin_required
def announcement_delete(aid):
    a = db.session.get(Announcement, aid) or abort(404)
    db.session.delete(a)
    db.session.commit()
    audit_service.record("announcement_deleted", a.title)
    flash("Announcement removed.", "success")
    return redirect(request.referrer or url_for("main.notifications"))
