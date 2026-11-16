from datetime import date, timedelta

from extensions import db
from models import Notification, Task, User

_PREF = {"task": "notify_tasks", "deadline": "notify_tasks", "document": "notify_documents",
         "announcement": "notify_announcements"}


def notify(user, title, message="", category="system", link=None, dedupe_key=None, is_demo=False, commit=True):
    """Create a notification honouring the recipient's preferences. Returns the row or None."""
    if user is None or not user.active:
        return None
    pref = _PREF.get(category)
    if pref and not getattr(user, pref, True):
        return None
    if dedupe_key and Notification.query.filter_by(user_id=user.id, dedupe_key=dedupe_key).first():
        return None
    n = Notification(user_id=user.id, title=title[:200], message=(message or "")[:500], category=category,
                     link=link, dedupe_key=dedupe_key, is_demo=is_demo)
    db.session.add(n)
    if commit:
        db.session.commit()
    return n


def notify_users(users, *args, **kwargs):
    kwargs["commit"] = False
    for u in users:
        notify(u, *args, **kwargs)
    db.session.commit()


def refresh_deadline_alerts(user):
    """Create (once per task) 'due soon' and 'overdue' alerts for the user's open tasks."""
    today = date.today()
    soon = today + timedelta(days=2)
    tasks = Task.query.filter(Task.assigned_to == user.id, Task.status != "Completed",
                              Task.due_date.isnot(None), Task.due_date <= soon).all()
    for t in tasks:
        link = f"/tasks/{t.id}"
        if t.due_date < today:
            notify(user, "Task overdue", f"“{t.title}” was due on {t.due_date:%d %b %Y}.", "deadline", link,
                   dedupe_key=f"overdue-{t.id}", commit=False)
        else:
            notify(user, "Deadline approaching", f"“{t.title}” is due on {t.due_date:%d %b %Y}.", "deadline",
                   link, dedupe_key=f"due-{t.id}-{t.due_date.isoformat()}", commit=False)
    db.session.commit()


def admins():
    return User.query.filter_by(role="admin", active=True).all()
