from datetime import date

from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import or_

from extensions import db
from models import PRIORITIES, TASK_CATEGORIES, TASK_STATUSES, Subtask, Task, User, utcnow
from services import ai_service, audit_service, notification_service
from services.permissions import admin_required, can_edit_task, can_update_task_status, can_view_task
from services.utils import clamp, parse_date

bp = Blueprint("tasks", __name__, url_prefix="/tasks")


def _task(tid, check):
    t = db.session.get(Task, tid) or abort(404)
    if not check(current_user, t):
        audit_service.record("task_access_denied", f"Task {tid}", "denied")
        abort(403)
    return t


def _visible_query():
    q = Task.query
    if not current_user.is_admin:
        q = q.filter(or_(Task.assigned_to == current_user.id, Task.created_by == current_user.id))
    return q


def _assignable_users():
    if current_user.is_admin:
        return User.query.filter_by(active=True).order_by(User.full_name).all()
    return [current_user]


@bp.route("/")
@login_required
def list_tasks():
    q = _visible_query()
    status, prio = request.args.get("status", ""), request.args.get("priority", "")
    cat, term = request.args.get("category", ""), request.args.get("q", "").strip()
    assignee = request.args.get("assignee", type=int)
    today = date.today()
    if status == "Overdue":
        q = q.filter(Task.status != "Completed", Task.due_date < today)
    elif status in TASK_STATUSES:
        q = q.filter(Task.status == status)
        if status != "Completed":
            q = q.filter(or_(Task.due_date.is_(None), Task.due_date >= today))
    if prio in PRIORITIES:
        q = q.filter(Task.priority == prio)
    if cat in TASK_CATEGORIES:
        q = q.filter(Task.category == cat)
    if term:
        q = q.filter(Task.title.ilike(f"%{term}%"))
    if assignee and current_user.is_admin:
        q = q.filter(Task.assigned_to == assignee)
    view = request.args.get("view", "list")
    q = q.order_by(Task.status == "Completed", Task.due_date.is_(None), Task.due_date, Task.id.desc())
    if view == "board":
        tasks = q.limit(200).all()
        cols = {"To Do": [], "In Progress": [], "Overdue": [], "Completed": []}
        for t in tasks:
            cols[t.effective_status].append(t)
        return render_template("tasks/list.html", pag=None, cols=cols, view=view, **_ctx(status, prio, cat, term, assignee))
    pag = q.paginate(page=request.args.get("page", 1, type=int), per_page=12, error_out=False)
    return render_template("tasks/list.html", pag=pag, cols=None, view=view, **_ctx(status, prio, cat, term, assignee))


def _ctx(status, prio, cat, term, assignee):
    return dict(status=status, prio=prio, cat=cat, term=term, assignee=assignee, statuses=TASK_STATUSES + ["Overdue"],
                priorities=PRIORITIES, categories=TASK_CATEGORIES, users=_assignable_users(), live=ai_service.is_configured())


def _apply_form(task):
    f = request.form
    title = f.get("title", "").strip()
    due_raw = f.get("due_date", "").strip()
    due = parse_date(due_raw)
    errors = []
    if not title or len(title) > 200:
        errors.append("Title is required (max 200 characters).")
    if due_raw and due is None:
        errors.append("Deadline is not a valid date.")
    if f.get("priority") not in PRIORITIES:
        errors.append("Choose a priority.")
    if f.get("category") not in TASK_CATEGORIES:
        errors.append("Choose a category.")
    assignee_id = f.get("assigned_to", type=int) if current_user.is_admin else current_user.id
    assignee = db.session.get(User, assignee_id) if assignee_id else None
    if assignee_id and (assignee is None or not assignee.active):
        errors.append("Choose a valid assignee.")
    if errors:
        for e in errors:
            flash(e, "danger")
        return False
    task.title, task.description = title, f.get("description", "").strip() or None
    task.priority, task.category, task.due_date = f["priority"], f["category"], due
    task.assigned_to = assignee.id if assignee else None
    task.department_id = assignee.department_id if assignee else None
    return True


@bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        task = Task(created_by=current_user.id, status="To Do")
        if _apply_form(task):
            db.session.add(task)
            db.session.flush()
            for s in request.form.getlist("subtasks"):
                if s.strip():
                    db.session.add(Subtask(task_id=task.id, title=clamp(s, 190)))
            db.session.commit()
            audit_service.record("task_created", f"{task.title} (#{task.id}) → {task.assignee.full_name if task.assignee else 'unassigned'}")
            _notify_assignment(task)
            flash("Task created.", "success")
            return redirect(url_for("tasks.detail", tid=task.id))
    return render_template("tasks/form.html", task=None, categories=TASK_CATEGORIES, priorities=PRIORITIES,
                           users=_assignable_users(), live=ai_service.is_configured(), form=request.form)


def _notify_assignment(task):
    if task.assignee and task.assignee.id != current_user.id:
        notification_service.notify(task.assignee, "New task assigned",
                                    f"{current_user.full_name} assigned you “{task.title}”"
                                    + (f" (due {task.due_date:%d %b %Y})." if task.due_date else "."),
                                    "task", url_for("tasks.detail", tid=task.id))


@bp.route("/<int:tid>")
@login_required
def detail(tid):
    task = _task(tid, can_view_task)
    return render_template("tasks/detail.html", task=task, statuses=TASK_STATUSES,
                           can_edit=can_edit_task(current_user, task), live=ai_service.is_configured())


@bp.route("/<int:tid>/edit", methods=["GET", "POST"])
@login_required
def edit(tid):
    task = _task(tid, can_edit_task)
    if request.method == "POST":
        old = task.assigned_to
        if _apply_form(task):
            db.session.commit()
            audit_service.record("task_updated", f"{task.title} (#{task.id})")
            if task.assigned_to != old:
                _notify_assignment(task)
            flash("Task updated.", "success")
            return redirect(url_for("tasks.detail", tid=task.id))
        db.session.rollback()
    return render_template("tasks/form.html", task=task, categories=TASK_CATEGORIES, priorities=PRIORITIES,
                           users=_assignable_users(), live=ai_service.is_configured(), form=request.form)


@bp.route("/<int:tid>/delete", methods=["POST"])
@login_required
def delete(tid):
    task = _task(tid, can_edit_task)
    title = task.title
    db.session.delete(task)
    db.session.commit()
    audit_service.record("task_deleted", f"{title} (#{tid})")
    flash("Task deleted.", "success")
    return redirect(url_for("tasks.list_tasks"))


@bp.route("/<int:tid>/status", methods=["POST"])
@login_required
def set_status(tid):
    task = _task(tid, can_update_task_status)
    status = request.form.get("status") or (request.get_json(silent=True) or {}).get("status")
    if status not in TASK_STATUSES:
        abort(400)
    task.status = status
    task.completed_at = utcnow() if status == "Completed" else None
    db.session.commit()
    if status == "Completed" and task.created_by != current_user.id:
        notification_service.notify(task.creator, "Task completed", f"{current_user.full_name} completed “{task.title}”.",
                                    "task", url_for("tasks.detail", tid=task.id))
    audit_service.record("task_status_changed", f"#{task.id} → {status}")
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, status=task.effective_status)
    flash(f"Status changed to {status}.", "success")
    return redirect(request.referrer or url_for("tasks.detail", tid=tid))


# -------------------------------------------------------------- subtasks
@bp.route("/<int:tid>/subtasks", methods=["POST"])
@login_required
def subtask_add(tid):
    task = _task(tid, can_update_task_status)
    title = request.form.get("title", "").strip()
    if title:
        db.session.add(Subtask(task_id=task.id, title=clamp(title, 190)))
        db.session.commit()
    return redirect(url_for("tasks.detail", tid=tid))


@bp.route("/<int:tid>/subtasks/<int:sid>/toggle", methods=["POST"])
@login_required
def subtask_toggle(tid, sid):
    task = _task(tid, can_update_task_status)
    s = Subtask.query.filter_by(id=sid, task_id=task.id).first_or_404()
    s.is_done = not s.is_done
    db.session.commit()
    return redirect(url_for("tasks.detail", tid=tid))


@bp.route("/<int:tid>/subtasks/<int:sid>/delete", methods=["POST"])
@login_required
def subtask_delete(tid, sid):
    task = _task(tid, can_update_task_status)
    s = Subtask.query.filter_by(id=sid, task_id=task.id).first_or_404()
    db.session.delete(s)
    db.session.commit()
    return redirect(url_for("tasks.detail", tid=tid))


# -------------------------------------------------------------- AI endpoints
def _ai(fn):
    try:
        return jsonify(fn())
    except ai_service.AIError as exc:
        return jsonify(error=str(exc)), 502


@bp.route("/ai/priority", methods=["POST"])
@login_required
def ai_priority():
    d = request.get_json(silent=True) or {}
    return _ai(lambda: ai_service.suggest_priority(d.get("title", ""), d.get("description", ""), parse_date(d.get("due_date"))))


@bp.route("/ai/description", methods=["POST"])
@login_required
def ai_description():
    d = request.get_json(silent=True) or {}
    if not (d.get("title") or "").strip():
        return jsonify(error="Enter a task title first."), 400
    return _ai(lambda: ai_service.generate_description(d["title"], d.get("category", "General")))


@bp.route("/ai/subtasks", methods=["POST"])
@login_required
def ai_subtasks():
    d = request.get_json(silent=True) or {}
    if not (d.get("title") or "").strip():
        return jsonify(error="Enter a task title first."), 400
    return _ai(lambda: ai_service.suggest_subtasks(d["title"], d.get("description", "")))


@bp.route("/<int:tid>/ai-breakdown", methods=["POST"])
@login_required
def ai_breakdown(tid):
    task = _task(tid, can_update_task_status)
    try:
        res = ai_service.suggest_subtasks(task.title, task.description or "")
    except ai_service.AIError as exc:
        flash(str(exc), "danger")
        return redirect(url_for("tasks.detail", tid=tid))
    for s in res["subtasks"]:
        db.session.add(Subtask(task_id=task.id, title=clamp(s, 190)))
    db.session.commit()
    flash(f"{len(res['subtasks'])} subtasks added" + ("" if res["mode"] == "live" else " (template — demo mode, not AI)") + ".",
          "success" if res["mode"] == "live" else "warning")
    return redirect(url_for("tasks.detail", tid=tid))


@bp.route("/ai/summary", methods=["POST"])
@login_required
def ai_summary():
    tasks = _visible_query().filter(Task.status != "Completed").order_by(Task.due_date).all()
    return _ai(lambda: ai_service.summarize_tasks(tasks))


@bp.route("/ai/workload", methods=["POST"])
@admin_required
def ai_workload():
    rows = []
    for u in User.query.filter_by(active=True).all():
        ts = Task.query.filter(Task.assigned_to == u.id, Task.status != "Completed").all()
        if ts:
            rows.append({"name": u.full_name, "open": len(ts),
                         "overdue": sum(t.effective_status == "Overdue" for t in ts),
                         "high": sum(t.priority in ("High", "Critical") for t in ts)})
    return _ai(lambda: ai_service.workload_suggestions(rows))
