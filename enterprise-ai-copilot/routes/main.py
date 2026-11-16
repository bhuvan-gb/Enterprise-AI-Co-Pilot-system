import os
import uuid
from datetime import date, timedelta

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from extensions import db
from models import (Announcement, AuditLog, Department, Document, Notification, Task, User, utcnow)
from services import analytics_service, audit_service
from services.permissions import accessible_documents_query
from services.utils import parse_date, valid_email, validate_password

bp = Blueprint("main", __name__)

_MAGIC = {b"\x89PNG": "png", b"\xff\xd8\xff": "jpg", b"GIF8": "gif", b"RIFF": "webp"}


@bp.route("/")
def index():
    return redirect(url_for("main.dashboard" if current_user.is_authenticated else "auth.login"))


@bp.route("/dashboard")
@login_required
def dashboard():
    announcements = Announcement.query.order_by(Announcement.is_pinned.desc(), Announcement.created_at.desc()).limit(4).all()
    if current_user.is_admin:
        start, end = date.today() - timedelta(days=180), date.today()
        data = analytics_service.get_analytics(start, end)
        recent_logs = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(6).all()
        recent_tasks = Task.query.filter(Task.status != "Completed").order_by(Task.due_date.is_(None), Task.due_date).limit(6).all()
        failed = AuditLog.query.filter(AuditLog.action == "login_failed",
                                       AuditLog.created_at >= utcnow() - timedelta(hours=24)).count()
        return render_template("dashboard/admin.html", data=data, logs=recent_logs, tasks=recent_tasks,
                               announcements=announcements, failed_logins=failed)
    mine = Task.query.filter(Task.assigned_to == current_user.id)
    upcoming = (mine.filter(Task.status != "Completed").order_by(Task.due_date.is_(None), Task.due_date).limit(6).all())
    stats = analytics_service.personal_stats(current_user)
    docs = accessible_documents_query(current_user).order_by(Document.created_at.desc()).limit(5).all()
    return render_template("dashboard/employee.html", upcoming=upcoming, stats=stats, docs=docs,
                           announcements=announcements)


# ------------------------------------------------------------- notifications
@bp.route("/notifications")
@login_required
def notifications():
    flt = request.args.get("filter", "all")
    q = Notification.query.filter_by(user_id=current_user.id)
    if flt == "unread":
        q = q.filter_by(is_read=False)
    page = request.args.get("page", 1, type=int)
    pag = q.order_by(Notification.created_at.desc()).paginate(page=page, per_page=15, error_out=False)
    announcements = Announcement.query.order_by(Announcement.created_at.desc()).limit(10).all()
    return render_template("notifications.html", pag=pag, flt=flt, announcements=announcements)


@bp.route("/notifications/<int:nid>/read", methods=["POST"])
@login_required
def notification_read(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    n.is_read = True
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    return redirect(request.referrer or url_for("main.notifications"))


@bp.route("/notifications/read-all", methods=["POST"])
@login_required
def notifications_read_all():
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True)
    flash("All notifications marked as read.", "success")
    return redirect(url_for("main.notifications"))


@bp.route("/notifications/<int:nid>/delete", methods=["POST"])
@login_required
def notification_delete(nid):
    n = Notification.query.filter_by(id=nid, user_id=current_user.id).first_or_404()
    db.session.delete(n)
    db.session.commit()
    return redirect(request.referrer or url_for("main.notifications"))


@bp.route("/api/notifications/summary")
@login_required
def api_notifications():
    q = Notification.query.filter_by(user_id=current_user.id)
    latest = q.order_by(Notification.created_at.desc()).limit(6).all()
    return jsonify(unread=q.filter_by(is_read=False).count(), items=[
        {"id": n.id, "title": n.title, "message": n.message, "category": n.category, "link": n.link or "",
         "is_read": n.is_read, "time": n.created_at.strftime("%d %b %H:%M")} for n in latest])


# ------------------------------------------------------------- settings
def _save_avatar(file):
    if not file or not file.filename:
        return None, None
    ext = secure_filename(file.filename).rsplit(".", 1)[-1].lower()
    if ext not in current_app.config["ALLOWED_IMAGE_EXTENSIONS"]:
        return None, "Profile picture must be PNG, JPG, GIF or WebP."
    head = file.stream.read(12)
    file.stream.seek(0, os.SEEK_END)
    size = file.stream.tell()
    file.stream.seek(0)
    kind = next((v for k, v in _MAGIC.items() if head.startswith(k)), None)
    if kind is None or (kind == "webp" and b"WEBP" not in head):
        return None, "That file is not a valid image."
    if size > current_app.config["MAX_AVATAR_BYTES"]:
        return None, "Profile picture must be under 2 MB."
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], "avatars")
    os.makedirs(folder, exist_ok=True)
    name = f"{uuid.uuid4().hex}.{kind}"
    file.save(os.path.join(folder, name))
    return name, None


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        section = request.form.get("section")
        u = current_user
        if section == "profile":
            name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            if len(name) < 2 or not valid_email(email):
                flash("Enter your name and a valid email address.", "danger")
                return redirect(url_for("main.settings"))
            other = User.query.filter(User.email == email, User.id != u.id).first()
            if other:
                flash("That email is already used by another account.", "danger")
                return redirect(url_for("main.settings"))
            avatar, err = _save_avatar(request.files.get("avatar"))
            if err:
                flash(err, "danger")
                return redirect(url_for("main.settings"))
            u.full_name, u.email = name[:120], email
            u.phone = request.form.get("phone", "").strip()[:30] or None
            if avatar:
                old = u.avatar
                u.avatar = avatar
                if old:
                    try:
                        os.remove(os.path.join(current_app.config["UPLOAD_FOLDER"], "avatars", old))
                    except OSError:
                        pass
            db.session.commit()
            audit_service.record("profile_updated", "Profile edited")
            flash("Profile updated.", "success")
        elif section == "password":
            cur, new, conf = (request.form.get(k, "") for k in ("current_password", "new_password", "confirm_password"))
            err = validate_password(new)
            if not u.check_password(cur):
                flash("Current password is incorrect.", "danger")
                audit_service.record("password_change_failed", "Wrong current password", "failure")
            elif err:
                flash(err, "danger")
            elif new != conf:
                flash("New passwords do not match.", "danger")
            else:
                u.set_password(new)
                db.session.commit()
                audit_service.record("password_changed", "Password changed")
                flash("Password changed.", "success")
        elif section == "preferences":
            u.notify_tasks = "notify_tasks" in request.form
            u.notify_documents = "notify_documents" in request.form
            u.notify_announcements = "notify_announcements" in request.form
            theme = request.form.get("theme", "light")
            u.theme = theme if theme in ("light", "dark") else "light"
            db.session.commit()
            flash("Preferences saved.", "success")
        return redirect(url_for("main.settings"))
    return render_template("settings.html")


@bp.route("/api/theme", methods=["POST"])
@login_required
def set_theme():
    theme = (request.get_json(silent=True) or {}).get("theme")
    if theme not in ("light", "dark"):
        return jsonify(error="Invalid theme"), 400
    current_user.theme = theme
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/avatar/<filename>")
@login_required
def avatar(filename):
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], "avatars")
    if filename != secure_filename(filename):
        abort(404)
    return send_from_directory(folder, filename, max_age=3600)
