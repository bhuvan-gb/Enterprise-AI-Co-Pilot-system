"""Analytics computed from real database rows (demo rows are flagged and can be excluded)."""
from collections import OrderedDict
from datetime import date, datetime, timedelta

from extensions import db
from models import Department, Document, Task, User, utcnow


def month_keys(start, end):
    keys, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return keys[-24:]


def default_range():
    end = date.today()
    return end - timedelta(days=180), end


def get_analytics(start, end, dept_id=None, include_demo=True):
    start_dt = datetime.combine(start, datetime.min.time())
    end_dt = datetime.combine(end, datetime.max.time())

    users_q = User.query.filter_by(active=True)
    tasks_q = Task.query.filter(Task.created_at.between(start_dt, end_dt))
    docs_q = Document.query.filter(Document.created_at.between(start_dt, end_dt))
    if dept_id:
        users_q = users_q.filter(User.department_id == dept_id)
        tasks_q = tasks_q.filter(Task.department_id == dept_id)
        docs_q = docs_q.filter(Document.department_id == dept_id)
    if not include_demo:
        users_q = users_q.filter(User.is_demo.is_(False))
        tasks_q = tasks_q.filter(Task.is_demo.is_(False))
        docs_q = docs_q.filter(Document.is_demo.is_(False))

    users, tasks, docs = users_q.all(), tasks_q.all(), docs_q.all()
    week_ago = utcnow() - timedelta(days=7)
    completed = [t for t in tasks if t.status == "Completed"]
    overdue = [t for t in tasks if t.effective_status == "Overdue"]
    pending = [t for t in tasks if t.status != "Completed"]

    status_counts = OrderedDict((s, 0) for s in ("To Do", "In Progress", "Completed", "Overdue"))
    for t in tasks:
        status_counts[t.effective_status] += 1
    prio_counts = OrderedDict((p, 0) for p in ("Low", "Medium", "High", "Critical"))
    for t in tasks:
        prio_counts[t.priority] = prio_counts.get(t.priority, 0) + 1

    depts = Department.query.order_by(Department.name).all()
    if dept_id:
        depts = [d for d in depts if d.id == dept_id]
    d_labels, d_emp, d_tasks, d_done, d_rate = [], [], [], [], []
    for d in depts:
        dt = [t for t in tasks if t.department_id == d.id]
        done = sum(1 for t in dt if t.status == "Completed")
        d_labels.append(d.name)
        d_emp.append(sum(1 for u in users if u.department_id == d.id))
        d_tasks.append(len(dt))
        d_done.append(done)
        d_rate.append(round(done * 100 / len(dt), 1) if dt else 0)

    keys = month_keys(start, end)
    created = OrderedDict((k, 0) for k in keys)
    done_m = OrderedDict((k, 0) for k in keys)
    docs_m = OrderedDict((k, 0) for k in keys)
    for t in tasks:
        k = t.created_at.strftime("%Y-%m")
        if k in created:
            created[k] += 1
    for t in completed:
        if t.completed_at:
            k = t.completed_at.strftime("%Y-%m")
            if k in done_m:
                done_m[k] += 1
    for d in docs:
        k = d.created_at.strftime("%Y-%m")
        if k in docs_m:
            docs_m[k] += 1
    pretty = [datetime.strptime(k, "%Y-%m").strftime("%b %Y") for k in keys]

    demo_count = (sum(1 for u in users if u.is_demo) + sum(1 for t in tasks if t.is_demo)
                  + sum(1 for d in docs if d.is_demo))
    return {
        "kpis": {"employees": len(users),
                 "active_users": sum(1 for u in users if u.last_login and u.last_login >= week_ago),
                 "documents": len(docs), "tasks_total": len(tasks), "tasks_completed": len(completed),
                 "tasks_pending": len(pending), "tasks_overdue": len(overdue),
                 "completion_rate": round(len(completed) * 100 / len(tasks), 1) if tasks else 0},
        "status": {"labels": list(status_counts), "values": list(status_counts.values())},
        "priority": {"labels": list(prio_counts), "values": list(prio_counts.values())},
        "departments": {"labels": d_labels, "employees": d_emp, "tasks": d_tasks, "completed": d_done, "rate": d_rate},
        "monthly": {"labels": pretty, "created": list(created.values()), "completed": list(done_m.values()),
                    "documents": list(docs_m.values())},
        "demo_records": demo_count,
    }


def stats_as_text(data, start, end, dept_name=None):
    k = data["kpis"]
    lines = [f"Period: {start} to {end}" + (f"; department: {dept_name}" if dept_name else "; all departments"),
             f"Active employees: {k['employees']} (logged in during last 7 days: {k['active_users']})",
             f"Documents uploaded: {k['documents']}",
             f"Tasks created: {k['tasks_total']}; completed: {k['tasks_completed']}; pending: {k['tasks_pending']}; "
             f"overdue: {k['tasks_overdue']}; completion rate: {k['completion_rate']}%"]
    d = data["departments"]
    for i, name in enumerate(d["labels"]):
        lines.append(f"Department {name}: {d['employees'][i]} employees, {d['tasks'][i]} tasks, "
                     f"{d['completed'][i]} completed ({d['rate'][i]}%)")
    return "\n".join(lines)


def personal_stats(user):
    tasks = Task.query.filter_by(assigned_to=user.id).all()
    counts = OrderedDict((s, 0) for s in ("To Do", "In Progress", "Completed", "Overdue"))
    for t in tasks:
        counts[t.effective_status] += 1
    return {"status": {"labels": list(counts), "values": list(counts.values())}, "total": len(tasks)}
