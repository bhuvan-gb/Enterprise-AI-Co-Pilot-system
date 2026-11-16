"""Authorization rules. Every route enforces these on the server side."""
from functools import wraps

from flask import abort
from flask_login import current_user, login_required
from sqlalchemy import and_, or_

from models import Document


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        if not current_user.is_admin:
            abort(403)
        return view(*args, **kwargs)
    return wrapper


# ---- documents ---------------------------------------------------------------
def can_access_document(user, doc):
    if user.is_admin or doc.owner_id == user.id or doc.visibility == "company":
        return True
    return (doc.visibility == "department" and user.department_id is not None
            and doc.department_id == user.department_id)


def can_manage_document(user, doc):
    return user.is_admin or doc.owner_id == user.id


def accessible_documents_query(user):
    query = Document.query
    if user.is_admin:
        return query
    conds = [Document.owner_id == user.id, Document.visibility == "company"]
    if user.department_id:
        conds.append(and_(Document.visibility == "department", Document.department_id == user.department_id))
    return query.filter(or_(*conds))


# ---- knowledge base ----------------------------------------------------------
def can_access_kb(user, entry):
    if user.is_admin:
        return True
    if not entry.is_published:
        return False
    return entry.department_id is None or entry.department_id == user.department_id


# ---- tasks -------------------------------------------------------------------
def can_view_task(user, task):
    return user.is_admin or task.assigned_to == user.id or task.created_by == user.id


def can_edit_task(user, task):
    return user.is_admin or task.created_by == user.id


def can_update_task_status(user, task):
    return can_view_task(user, task)
