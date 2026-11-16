import logging
from datetime import timedelta

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf import FlaskForm
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from wtforms import BooleanField, PasswordField, SelectField, StringField
from wtforms.validators import DataRequired, EqualTo, Length

from extensions import db
from models import Department, User, utcnow
from services import audit_service
from services.user_service import next_employee_id, send_mail
from services.utils import safe_next_url, validate_password, valid_email

bp = Blueprint("auth", __name__)
log = logging.getLogger(__name__)


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Length(max=150)])
    password = PasswordField("Password", validators=[DataRequired()])
    remember = BooleanField("Keep me signed in")


class RegisterForm(FlaskForm):
    full_name = StringField("Full name", validators=[DataRequired(), Length(min=2, max=120)])
    email = StringField("Work email", validators=[DataRequired(), Length(max=150)])
    department_id = SelectField("Department", coerce=int, choices=[])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=128)])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", "Passwords do not match.")])


class ForgotForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Length(max=150)])


class ResetForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired(), Length(max=128)])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", "Passwords do not match.")])


def _serializer():
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="password-reset")


def _dept_choices():
    return [(0, "— Select department —")] + [(d.id, d.name) for d in Department.query.order_by(Department.name)]


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email).first()
        now = utcnow()
        if user and user.locked_until and user.locked_until > now:
            mins = int((user.locked_until - now).total_seconds() // 60) + 1
            audit_service.record("login_blocked", "Account temporarily locked", "failure", user=user)
            flash(f"Too many failed attempts. Try again in about {mins} minute(s).", "danger")
        elif user and user.check_password(form.password.data):
            if not user.active:
                audit_service.record("login_denied", "Deactivated account", "denied", user=user)
                flash("This account has been deactivated. Contact your administrator.", "danger")
            else:
                user.failed_logins, user.locked_until, user.last_login = 0, None, now
                db.session.commit()
                session.clear()
                session.permanent = True
                login_user(user, remember=form.remember.data, duration=timedelta(days=7))
                audit_service.record("login", "Signed in", user=user)
                return redirect(safe_next_url(request.args.get("next")) or url_for("main.dashboard"))
        else:
            if user:
                user.failed_logins += 1
                if user.failed_logins >= current_app.config["MAX_FAILED_LOGINS"]:
                    user.locked_until = now + timedelta(minutes=current_app.config["LOCKOUT_MINUTES"])
                    user.failed_logins = 0
                db.session.commit()
            audit_service.record("login_failed", "Invalid credentials", "failure", user=user, email=email)
            flash("Incorrect email or password.", "danger")
    return render_template("auth/login.html", form=form, show_register=current_app.config["ALLOW_REGISTRATION"],
                           needs_setup=not User.query.filter_by(role="admin").first())


@bp.route("/register", methods=["GET", "POST"])
def register():
    if not current_app.config["ALLOW_REGISTRATION"]:
        flash("Self-registration is disabled. Ask an administrator to create your account.", "warning")
        return redirect(url_for("auth.login"))
    if current_user.is_authenticated:
        return redirect(url_for("main.dashboard"))
    form = RegisterForm()
    form.department_id.choices = _dept_choices()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        err = validate_password(form.password.data)
        if not valid_email(email):
            flash("Enter a valid email address.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("An account with this email already exists.", "danger")
        elif err:
            flash(err, "danger")
        else:
            user = User(employee_id=next_employee_id(), full_name=form.full_name.data.strip(), email=email,
                        role="employee", department_id=form.department_id.data or None, joining_date=utcnow().date())
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()
            audit_service.record("register", f"New account {email}", user=user)
            flash("Account created. You can sign in now.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/register.html", form=form)


@bp.route("/setup", methods=["GET", "POST"])
def setup():
    """One-time creation of the first administrator. Disabled once any admin exists."""
    if User.query.filter_by(role="admin").first():
        flash("Setup is already complete.", "info")
        return redirect(url_for("auth.login"))
    form = RegisterForm()
    form.department_id.choices = [(0, "—")]
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        err = validate_password(form.password.data)
        if not valid_email(email):
            flash("Enter a valid email address.", "danger")
        elif err:
            flash(err, "danger")
        else:
            admin = User(employee_id=next_employee_id(), full_name=form.full_name.data.strip(), email=email,
                         role="admin", designation="Administrator", joining_date=utcnow().date())
            admin.set_password(form.password.data)
            db.session.add(admin)
            db.session.commit()
            audit_service.record("admin_created", "Initial administrator created", user=admin)
            flash("Administrator created. Sign in to continue.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/setup.html", form=form)


@bp.route("/logout", methods=["POST"])
@login_required
def logout():
    audit_service.record("logout", "Signed out")
    logout_user()
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


@bp.route("/forgot-password", methods=["GET", "POST"])
def forgot():
    form = ForgotForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        user = User.query.filter_by(email=email, active=True).first()
        if user:
            token = _serializer().dumps(user.id)
            link = url_for("auth.reset", token=token, _external=True)
            sent = False
            try:
                sent = send_mail(user.email, "Reset your password",
                                 f"Hello {user.full_name},\n\nUse this link within "
                                 f"{current_app.config['RESET_TOKEN_MINUTES']} minutes to reset your password:\n{link}\n")
            except Exception:
                log.exception("Could not send reset e-mail")
            if not sent:
                log.warning("PASSWORD RESET LINK for %s (SMTP not configured): %s", email, link)
                if current_app.config["SHOW_RESET_LINK"]:
                    flash(f"Development mode — reset link: {link}", "warning")
            audit_service.record("password_reset_requested", email, user=user)
        flash("If an account exists for that email, a reset link has been sent.", "info")
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot.html", form=form)


@bp.route("/reset-password/<token>", methods=["GET", "POST"])
def reset(token):
    try:
        uid = _serializer().loads(token, max_age=current_app.config["RESET_TOKEN_MINUTES"] * 60)
    except (SignatureExpired, BadSignature):
        flash("This reset link is invalid or has expired. Request a new one.", "danger")
        return redirect(url_for("auth.forgot"))
    user = db.session.get(User, uid)
    form = ResetForm()
    if user and form.validate_on_submit():
        err = validate_password(form.password.data)
        if err:
            flash(err, "danger")
        else:
            user.set_password(form.password.data)
            user.failed_logins, user.locked_until = 0, None
            db.session.commit()
            audit_service.record("password_reset", "Password reset via link", user=user)
            flash("Password updated. Please sign in.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", form=form)
