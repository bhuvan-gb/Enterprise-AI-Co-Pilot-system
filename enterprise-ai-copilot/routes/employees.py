from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from extensions import db
from models import Department, Document, Notification, Task, User
from services import audit_service
from services.permissions import admin_required
from services.user_service import next_employee_id
from services.utils import parse_date, valid_email, validate_password

bp = Blueprint("employees", __name__, url_prefix="/employees")


@bp.route("/")
@login_required
def directory():
    q = request.args.get("q", "").strip()
    dept = request.args.get("department", type=int)
    status = request.args.get("status", "active")
    query = User.query
    if status == "active" or not current_user.is_admin:
        query = query.filter(User.active.is_(True))
    elif status == "inactive":
        query = query.filter(User.active.is_(False))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(User.full_name.ilike(like), User.email.ilike(like), User.employee_id.ilike(like),
                                 User.designation.ilike(like)))
    if dept:
        query = query.filter(User.department_id == dept)
    page = request.args.get("page", 1, type=int)
    pag = query.order_by(User.full_name).paginate(page=page, per_page=12, error_out=False)
    return render_template("employees/directory.html", pag=pag, q=q, dept=dept, status=status,
                           departments=Department.query.order_by(Department.name).all())


@bp.route("/<int:uid>")
@login_required
def profile(uid):
    user = db.session.get(User, uid) or abort(404)
    if not user.active and not current_user.is_admin:
        abort(404)
    tasks = Task.query.filter_by(assigned_to=user.id)
    stats = {"assigned": tasks.count(), "completed": tasks.filter_by(status="Completed").count(),
             "open": tasks.filter(Task.status != "Completed").count(),
             "documents": Document.query.filter_by(owner_id=user.id, visibility="company").count()}
    recent = tasks.order_by(Task.created_at.desc()).limit(5).all() if current_user.is_admin else []
    return render_template("employees/profile.html", u=user, stats=stats, recent=recent)


def _read_form(user, creating):
    f = request.form
    name, email = f.get("full_name", "").strip(), f.get("email", "").strip().lower()
    errors = []
    if len(name) < 2:
        errors.append("Full name is required.")
    if not valid_email(email):
        errors.append("Enter a valid email address.")
    elif User.query.filter(User.email == email, User.id != (user.id or 0)).first():
        errors.append("That email is already in use.")
    emp_id = f.get("employee_id", "").strip() or (user.employee_id or next_employee_id())
    if User.query.filter(User.employee_id == emp_id, User.id != (user.id or 0)).first():
        errors.append("That employee ID already exists.")
    role = f.get("role", "employee")
    if role not in ("admin", "employee"):
        errors.append("Invalid role.")
    pw = f.get("password", "")
    if creating or pw:
        err = validate_password(pw)
        if err:
            errors.append(err)
    jd = parse_date(f.get("joining_date")) if f.get("joining_date") else None
    if f.get("joining_date") and jd is None:
        errors.append("Joining date is invalid.")
    if errors:
        for e in errors:
            flash(e, "danger")
        return False
    user.full_name, user.email, user.employee_id, user.role = name[:120], email, emp_id[:30], role
    user.department_id = f.get("department_id", type=int) or None
    user.designation = f.get("designation", "").strip()[:100] or None
    user.phone = f.get("phone", "").strip()[:30] or None
    user.joining_date = jd
    if pw:
        user.set_password(pw)
    return True


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def new():
    if request.method == "POST":
        user = User()
        if _read_form(user, creating=True):
            db.session.add(user)
            db.session.commit()
            audit_service.record("employee_created", f"{user.full_name} ({user.email}), role {user.role}")
            flash(f"Employee {user.full_name} added.", "success")
            return redirect(url_for("employees.profile", uid=user.id))
    return render_template("employees/form.html", u=None, departments=Department.query.order_by(Department.name).all(),
                           suggested_id=next_employee_id())


@bp.route("/<int:uid>/edit", methods=["GET", "POST"])
@admin_required
def edit(uid):
    user = db.session.get(User, uid) or abort(404)
    if request.method == "POST":
        was_admin = user.role == "admin"
        if _read_form(user, creating=False):
            if was_admin and user.role != "admin" and User.query.filter_by(role="admin", active=True).count() <= 1:
                db.session.rollback()
                flash("You cannot remove the last administrator.", "danger")
            else:
                db.session.commit()
                audit_service.record("employee_updated", f"{user.full_name} ({user.email})")
                flash("Employee updated.", "success")
                return redirect(url_for("employees.profile", uid=user.id))
        else:
            db.session.rollback()
    return render_template("employees/form.html", u=user, departments=Department.query.order_by(Department.name).all(),
                           suggested_id=user.employee_id)


@bp.route("/<int:uid>/toggle", methods=["POST"])
@admin_required
def toggle_active(uid):
    user = db.session.get(User, uid) or abort(404)
    if user.id == current_user.id:
        flash("You cannot deactivate your own account.", "danger")
    elif user.active and user.is_admin and User.query.filter_by(role="admin", active=True).count() <= 1:
        flash("You cannot deactivate the last administrator.", "danger")
    else:
        user.active = not user.active
        db.session.commit()
        audit_service.record("employee_activated" if user.active else "employee_deactivated", user.email)
        flash(f"{user.full_name} is now {'active' if user.active else 'deactivated'}.", "success")
    return redirect(request.referrer or url_for("employees.directory"))


@bp.route("/<int:uid>/delete", methods=["POST"])
@admin_required
def delete(uid):
    user = db.session.get(User, uid) or abort(404)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "danger")
    elif user.is_admin and User.query.filter_by(role="admin").count() <= 1:
        flash("You cannot delete the last administrator.", "danger")
    elif Document.query.filter_by(owner_id=user.id).count() or Task.query.filter_by(created_by=user.id).count():
        flash("This employee owns documents or created tasks. Deactivate the account instead of deleting it.", "warning")
    else:
        Task.query.filter_by(assigned_to=user.id).update({"assigned_to": None})
        name, email = user.full_name, user.email
        db.session.delete(user)
        db.session.commit()
        audit_service.record("employee_deleted", f"{name} ({email})")
        flash("Employee deleted.", "success")
        return redirect(url_for("employees.directory"))
    return redirect(url_for("employees.profile", uid=uid))


# ------------------------------------------------------------------ departments
@bp.route("/departments", methods=["GET", "POST"])
@admin_required
def departments():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name or len(name) > 100:
            flash("Department name is required (max 100 characters).", "danger")
        elif Department.query.filter(db.func.lower(Department.name) == name.lower()).first():
            flash("A department with that name already exists.", "danger")
        else:
            db.session.add(Department(name=name, description=request.form.get("description", "").strip()[:255] or None))
            db.session.commit()
            audit_service.record("department_created", name)
            flash("Department created.", "success")
        return redirect(url_for("employees.departments"))
    rows = [(d, User.query.filter_by(department_id=d.id, active=True).count())
            for d in Department.query.order_by(Department.name)]
    return render_template("employees/departments.html", rows=rows)


@bp.route("/departments/<int:did>/edit", methods=["POST"])
@admin_required
def department_edit(did):
    d = db.session.get(Department, did) or abort(404)
    name = request.form.get("name", "").strip()
    clash = Department.query.filter(db.func.lower(Department.name) == name.lower(), Department.id != did).first()
    if not name or clash:
        flash("Enter a unique department name.", "danger")
    else:
        d.name, d.description = name[:100], request.form.get("description", "").strip()[:255] or None
        db.session.commit()
        audit_service.record("department_updated", name)
        flash("Department updated.", "success")
    return redirect(url_for("employees.departments"))


@bp.route("/departments/<int:did>/delete", methods=["POST"])
@admin_required
def department_delete(did):
    d = db.session.get(Department, did) or abort(404)
    if User.query.filter_by(department_id=d.id).count():
        flash("Move or remove the employees in this department before deleting it.", "warning")
    else:
        name = d.name
        db.session.delete(d)
        db.session.commit()
        audit_service.record("department_deleted", name)
        flash("Department deleted.", "success")
    return redirect(url_for("employees.departments"))
