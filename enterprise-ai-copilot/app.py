"""Enterprise AI Co-Pilot — application factory.

Run locally:   flask --app app run --debug       (or: python app.py)
"""
import logging
import os
from datetime import datetime, time

from flask import Flask, jsonify, render_template, request, session
from flask_login import current_user
from flask_wtf.csrf import CSRFError

from config import Config
from extensions import csrf, db, login_manager, migrate


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    for sub in ("documents", "avatars", "reports"):
        os.makedirs(os.path.join(app.config["UPLOAD_FOLDER"], sub), exist_ok=True)
    os.makedirs(app.config["INSTANCE_DIR"], exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please sign in to continue."
    login_manager.login_message_category = "info"
    login_manager.session_protection = "strong"

    from models import Notification, User

    @login_manager.user_loader
    def load_user(user_id):
        try:
            user = db.session.get(User, int(user_id))
        except (TypeError, ValueError):
            return None
        return user if user is not None and user.active else None

    @login_manager.unauthorized_handler
    def unauthorized():
        from flask import redirect, url_for, flash
        if _wants_json():
            return jsonify(error="Your session has expired. Please sign in again."), 401
        flash(login_manager.login_message, "info")
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?") if request.method == "GET" else None))

    # ---- blueprints
    from routes import (admin, analytics, auth, chat, documents, employees, knowledge, main, reports, tasks)
    for module in (auth, main, chat, documents, knowledge, employees, tasks, analytics, reports, admin):
        app.register_blueprint(module.bp)

    # ---- template helpers
    from services.utils import human_size

    @app.template_filter("filesize")
    def _filesize(v):
        return human_size(v)

    @app.template_filter("dt")
    def _dt(v, fmt="%d %b %Y, %H:%M"):
        return v.strftime(fmt) if v else "—"

    @app.template_filter("d")
    def _d(v, fmt="%d %b %Y"):
        return v.strftime(fmt) if v else "—"

    @app.context_processor
    def inject_globals():
        from services import ai_service
        unread = 0
        if current_user.is_authenticated:
            unread = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        return {"app_name": app.config["APP_NAME"], "ai_live": ai_service.is_configured(), "unread_count": unread}

    @app.before_request
    def refresh_alerts():
        """Create deadline reminders at most every 15 minutes per session."""
        if request.endpoint in (None, "static") or not current_user.is_authenticated:
            return
        last = session.get("alerts_checked")
        now = datetime.utcnow().timestamp()
        if last is None or now - last > 900:
            try:
                from services import notification_service
                notification_service.refresh_deadline_alerts(current_user)
                session["alerts_checked"] = now
            except Exception:
                db.session.rollback()
                app.logger.exception("Deadline alert refresh failed")

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if request.endpoint != "static" and current_user.is_authenticated:
            resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    # ---- errors
    def _wants_json():
        return request.headers.get("X-Requested-With") == "fetch" or request.path.startswith("/api/")

    def _error(code, title, message):
        if _wants_json():
            return jsonify(error=message), code
        return render_template("errors/error.html", code=code, title=title, message=message), code

    @app.errorhandler(400)
    def e400(_e):
        return _error(400, "Bad request", "The request could not be understood.")

    @app.errorhandler(CSRFError)
    def ecsrf(_e):
        return _error(400, "Session expired", "Your form session expired. Refresh the page and try again.")

    @app.errorhandler(403)
    def e403(_e):
        return _error(403, "Access denied", "You do not have permission to view this page.")

    @app.errorhandler(404)
    def e404(_e):
        return _error(404, "Page not found", "We could not find what you were looking for.")

    @app.errorhandler(413)
    def e413(_e):
        mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return _error(413, "File too large", f"The upload exceeds the {mb} MB limit.")

    @app.errorhandler(500)
    def e500(_e):
        db.session.rollback()
        return _error(500, "Something went wrong", "An unexpected error occurred. It has been logged.")

    from cli import register_commands
    register_commands(app)
    return app


app = create_app()

if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")),
            debug=os.getenv("FLASK_DEBUG", "0") == "1")
