"""Builds report data from the database, adds an AI/automatic summary and renders PDFs with ReportLab."""
import json
import os
import uuid
from datetime import date, datetime, timedelta
from xml.sax.saxutils import escape

from flask import current_app
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from extensions import db
from models import Department, Document, Report, Task, User, utcnow
from services import ai_service
from services.analytics_service import month_keys
from services.permissions import accessible_documents_query
from services.utils import human_size

REPORT_TYPES = {
    "task_completion": ("Task completion report", False),
    "document_summary": ("Document summary report", False),
    "employee_activity": ("Employee activity report", True),   # (label, admin_only)
    "department": ("Department report", True),
    "monthly_business": ("Monthly business report", True),
}
NAVY = colors.HexColor("#0B1B3F")
VIOLET = colors.HexColor("#6D5BD0")


def available_types(user):
    return {k: v[0] for k, v in REPORT_TYPES.items() if user.is_admin or not v[1]}


def _rate(done, total):
    return f"{round(done * 100 / total)}%" if total else "—"


def build(report_type, user, start, end, dept_id=None):
    """Return a JSON-serialisable dict: title, period, stats[[label,value]], tables[{title,columns,rows}]."""
    label = REPORT_TYPES[report_type][0]
    s_dt = datetime.combine(start, datetime.min.time())
    e_dt = datetime.combine(end, datetime.max.time())
    dept = db.session.get(Department, dept_id) if dept_id else None
    data = {"title": label, "period": f"{start:%d %b %Y} – {end:%d %b %Y}",
            "scope": dept.name if dept else ("All departments" if user.is_admin else user.full_name),
            "stats": [], "tables": []}

    if report_type == "task_completion":
        q = Task.query.filter(Task.created_at.between(s_dt, e_dt))
        if not user.is_admin:
            q = q.filter(Task.assigned_to == user.id)
        elif dept_id:
            q = q.filter(Task.department_id == dept_id)
        tasks = q.order_by(Task.due_date).all()
        done = sum(t.status == "Completed" for t in tasks)
        data["stats"] = [["Total tasks", len(tasks)], ["Completed", done],
                         ["In progress", sum(t.status == "In Progress" for t in tasks)],
                         ["To do", sum(t.status == "To Do" for t in tasks)],
                         ["Overdue", sum(t.effective_status == "Overdue" for t in tasks)],
                         ["Completion rate", _rate(done, len(tasks))]]
        data["tables"].append({"title": "Tasks", "columns": ["Task", "Assignee", "Priority", "Status", "Due"],
                               "rows": [[t.title, t.assignee.full_name if t.assignee else "—", t.priority,
                                         t.effective_status, f"{t.due_date:%d %b %Y}" if t.due_date else "—"]
                                        for t in tasks]})

    elif report_type == "employee_activity":
        uq = User.query.filter_by(active=True)
        if dept_id:
            uq = uq.filter_by(department_id=dept_id)
        rows = []
        for u in uq.order_by(User.full_name).all():
            tasks = Task.query.filter(Task.assigned_to == u.id, Task.created_at.between(s_dt, e_dt)).all()
            docs = Document.query.filter(Document.owner_id == u.id, Document.created_at.between(s_dt, e_dt)).count()
            rows.append([u.full_name, u.department.name if u.department else "—", len(tasks),
                         sum(t.status == "Completed" for t in tasks),
                         sum(t.effective_status == "Overdue" for t in tasks), docs,
                         f"{u.last_login:%d %b %Y}" if u.last_login else "Never"])
        data["stats"] = [["Employees", len(rows)], ["Tasks assigned", sum(r[2] for r in rows)],
                         ["Tasks completed", sum(r[3] for r in rows)], ["Documents uploaded", sum(r[5] for r in rows)]]
        data["tables"].append({"title": "Activity by employee",
                               "columns": ["Employee", "Department", "Tasks", "Done", "Overdue", "Docs", "Last login"],
                               "rows": rows})

    elif report_type == "department":
        rows = []
        for d in Department.query.order_by(Department.name).all():
            if dept_id and d.id != dept_id:
                continue
            tasks = Task.query.filter(Task.department_id == d.id, Task.created_at.between(s_dt, e_dt)).all()
            done = sum(t.status == "Completed" for t in tasks)
            rows.append([d.name, User.query.filter_by(department_id=d.id, active=True).count(), len(tasks), done,
                         sum(t.effective_status == "Overdue" for t in tasks), _rate(done, len(tasks)),
                         Document.query.filter(Document.department_id == d.id,
                                               Document.created_at.between(s_dt, e_dt)).count()])
        data["stats"] = [["Departments", len(rows)], ["Employees", sum(r[1] for r in rows)],
                         ["Tasks", sum(r[2] for r in rows)], ["Completed", sum(r[3] for r in rows)]]
        data["tables"].append({"title": "Department performance",
                               "columns": ["Department", "Staff", "Tasks", "Done", "Overdue", "Rate", "Docs"],
                               "rows": rows})

    elif report_type == "document_summary":
        docs = (accessible_documents_query(user).filter(Document.created_at.between(s_dt, e_dt))
                .order_by(Document.created_at.desc()).all())
        cats = {}
        for d in docs:
            cats[d.category] = cats.get(d.category, 0) + 1
        data["stats"] = [["Documents", len(docs)], ["Ready", sum(d.status == "ready" for d in docs)],
                         ["Total size", human_size(sum(d.file_size for d in docs))], ["Categories", len(cats)]]
        data["tables"].append({"title": "Documents", "columns": ["Title", "Category", "Owner", "Uploaded", "Summary"],
                               "rows": [[d.title, d.category, d.owner.full_name, f"{d.created_at:%d %b %Y}",
                                         (d.summary or "—")[:160]] for d in docs]})

    elif report_type == "monthly_business":
        keys = month_keys(start, end)
        rows = []
        for k in keys:
            y, m = int(k[:4]), int(k[5:])
            a = datetime(y, m, 1)
            b = datetime(y + (m == 12), (m % 12) + 1, 1)
            tq = Task.query.filter(Task.created_at >= a, Task.created_at < b)
            if dept_id:
                tq = tq.filter(Task.department_id == dept_id)
            created = tq.count()
            comp = Task.query.filter(Task.completed_at >= a, Task.completed_at < b)
            if dept_id:
                comp = comp.filter(Task.department_id == dept_id)
            rows.append([a.strftime("%b %Y"), created, comp.count(),
                         Document.query.filter(Document.created_at >= a, Document.created_at < b).count(),
                         User.query.filter(User.created_at >= a, User.created_at < b).count()])
        data["stats"] = [["Months covered", len(rows)], ["Tasks created", sum(r[1] for r in rows)],
                         ["Tasks completed", sum(r[2] for r in rows)], ["Documents", sum(r[3] for r in rows)]]
        data["tables"].append({"title": "Month by month",
                               "columns": ["Month", "Tasks created", "Tasks completed", "Documents", "New users"],
                               "rows": rows})
    return data


def stats_text(data):
    lines = [f"{data['title']} — {data['period']} — scope: {data['scope']}"]
    lines += [f"{k}: {v}" for k, v in data["stats"]]
    return "\n".join(lines)


def create_report(report_type, user, start, end, dept_id=None):
    data = build(report_type, user, start, end, dept_id)
    mode, summary = "demo", ""
    try:
        res = ai_service.report_summary(data["title"], stats_text(data))
        summary, mode = res["text"], res["mode"]
    except ai_service.AIError as exc:
        summary = f"AI summary unavailable: {exc}"
    data["summary"], data["summary_mode"] = summary, mode
    report = Report(title=f"{data['title']} ({data['period']})"[:200], report_type=report_type, generated_by=user.id,
                    parameters=json.dumps({"start": start.isoformat(), "end": end.isoformat(), "department_id": dept_id}),
                    summary=summary, summary_mode=mode, content_json=json.dumps(data))
    db.session.add(report)
    db.session.flush()
    folder = os.path.join(current_app.config["UPLOAD_FOLDER"], "reports")
    os.makedirs(folder, exist_ok=True)
    name = f"{uuid.uuid4().hex}.pdf"
    render_pdf(data, os.path.join(folder, name), user.full_name)
    report.file_name = name
    db.session.commit()
    return report


def render_pdf(data, path, generated_by):
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Title"], textColor=NAVY, fontSize=20, alignment=0, spaceAfter=4)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], textColor=NAVY, fontSize=13, spaceBefore=14, spaceAfter=6)
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9.5, leading=14)
    cell = ParagraphStyle("c", parent=ss["BodyText"], fontSize=8, leading=10)
    head = ParagraphStyle("hc", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    muted = ParagraphStyle("m", parent=body, textColor=colors.HexColor("#5B6478"), fontSize=8.5)

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#5B6478"))
        canvas.drawString(18 * mm, 10 * mm, "Enterprise AI Co-Pilot — confidential")
        canvas.drawRightString(A4[0] - 18 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm,
                            bottomMargin=18 * mm, title=data["title"], author="Enterprise AI Co-Pilot")
    width = A4[0] - 36 * mm
    story = [Paragraph(escape(data["title"]), h1),
             Paragraph(escape(f"Period: {data['period']}   |   Scope: {data['scope']}   |   "
                              f"Generated {utcnow():%d %b %Y %H:%M} UTC by {generated_by}"), muted),
             Spacer(1, 8)]
    label = "AI-generated summary (Gemini)" if data.get("summary_mode") == "live" else "Automatic summary (not AI-generated)"
    story += [Paragraph(label, h2), Paragraph(escape(data.get("summary", "")).replace("\n", "<br/>"), body)]
    if data["stats"]:
        story.append(Paragraph("Key figures", h2))
        rows = [[Paragraph(escape(str(k)), cell), Paragraph(f"<b>{escape(str(v))}</b>", cell)] for k, v in data["stats"]]
        t = Table(rows, colWidths=[width * 0.6, width * 0.4])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#D9DDE8")),
                               ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F1F2FA")),
                               ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
        story.append(t)
    for tbl in data["tables"]:
        story.append(Paragraph(escape(tbl["title"]), h2))
        if not tbl["rows"]:
            story.append(Paragraph("No records for the selected period.", body))
            continue
        cols = tbl["columns"]
        rows = [[Paragraph(escape(c), head) for c in cols]]
        rows += [[Paragraph(escape(str(v)), cell) for v in r] for r in tbl["rows"]]
        t = Table(rows, colWidths=[width / len(cols)] * len(cols), repeatRows=1)
        t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), NAVY),
                               ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F6FB")]),
                               ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#D9DDE8")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]))
        story.append(t)
    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def to_csv(content):
    """Export every table of a report as one CSV (uses pandas)."""
    import pandas as pd
    frames = []
    for tbl in content.get("tables", []):
        df = pd.DataFrame(tbl["rows"], columns=tbl["columns"])
        df.insert(0, "section", tbl["title"])
        frames.append(df)
    if not frames:
        return "section\n"
    return pd.concat(frames, ignore_index=True).to_csv(index=False)
