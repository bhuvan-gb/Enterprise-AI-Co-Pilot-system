"""End-to-end smoke test (uses a throw-away SQLite DB + hashing embeddings, no API calls).

Run from the project root, after `pip install -r requirements.txt`:
    python scripts/smoke_test.py
"""
import io
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp()
os.environ.update(DATABASE_URL=f"sqlite:///{TMP}/smoke.db", EMBEDDING_BACKEND="hashing", GEMINI_API_KEY="",
                  SECRET_KEY="smoke-test-key", DB_ENGINE="sqlite")

from config import Config  # noqa: E402


class TestConfig(Config):
    TESTING = True
    WTF_CSRF_ENABLED = False
    UPLOAD_FOLDER = os.path.join(TMP, "uploads")
    SQLALCHEMY_DATABASE_URI = os.environ["DATABASE_URL"]


from app import create_app  # noqa: E402
from extensions import db  # noqa: E402
from models import Department, Document, Task, User  # noqa: E402

app = create_app(TestConfig)
FAILS = []
JSON = {"X-Requested-With": "fetch"}


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  [{extra}]" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def login(client, email, pw):
    return client.post("/login", data={"email": email, "password": pw}, follow_redirects=False)


with app.app_context():
    db.create_all()
    dept = Department(name="Engineering")
    db.session.add(dept)
    db.session.flush()
    admin = User(employee_id="EMP-9001", full_name="Test Admin", email="admin@test.local", role="admin")
    admin.set_password("Admin1234")
    emp = User(employee_id="EMP-9002", full_name="Test Employee", email="emp@test.local", role="employee",
               department_id=dept.id)
    emp.set_password("Employee1234")
    db.session.add_all([admin, emp])
    db.session.commit()
    ADMIN_ID, EMP_ID = admin.id, emp.id

print("\n== Authentication")
c = app.test_client()
check("login page loads", c.get("/login").status_code == 200)
check("wrong password rejected", login(c, "admin@test.local", "nope").status_code == 200)
r = login(c, "admin@test.local", "Admin1234")
check("admin login redirects", r.status_code == 302)
check("unauthenticated user is redirected", app.test_client().get("/dashboard").status_code == 302)

print("\n== Every admin page opens")
for url in ["/dashboard", "/copilot", "/documents/", "/knowledge/", "/employees/", "/employees/departments", "/tasks/",
            "/tasks/new", "/tasks/?view=board", "/analytics/", "/reports/", "/notifications", "/admin/audit",
            "/settings", "/employees/new", "/knowledge/new", "/documents/search?q=leave", f"/employees/{EMP_ID}"]:
    resp = c.get(url)
    check(f"GET {url}", resp.status_code == 200, str(resp.status_code))

print("\n== CRUD: departments, employees, tasks, knowledge")
check("create department", c.post("/employees/departments", data={"name": "Finance"}, follow_redirects=True).status_code == 200)
r = c.post("/employees/new", data={"full_name": "New Person", "email": "new@test.local", "password": "Passw0rd99",
                                   "role": "employee", "department_id": dept.id, "employee_id": ""})
check("create employee", r.status_code == 302, str(r.status_code))
r = c.post("/tasks/new", data={"title": "Prepare budget", "description": "Q3", "category": "Finance",
                               "priority": "High", "due_date": "2030-01-01", "assigned_to": EMP_ID,
                               "subtasks": ["Gather data", "Review"]})
check("create task", r.status_code == 302, str(r.status_code))
with app.app_context():
    task = Task.query.filter_by(title="Prepare budget").first()
    TASK_ID = task.id if task else None
check("task saved with subtasks", TASK_ID is not None)
check("change task status", c.post(f"/tasks/{TASK_ID}/status", data={"status": "In Progress"}).status_code == 302)
check("AI priority (demo mode)", c.post("/tasks/ai/priority", json={"title": "urgent outage"}, headers=JSON).get_json().get("mode") == "demo")
r = c.post("/knowledge/new", data={"title": "Remote work policy", "category": "Company Policy", "is_published": "on",
                                   "content": "Employees may work remotely two days per week with manager approval."})
check("create knowledge entry", r.status_code == 302, str(r.status_code))

print("\n== Documents + RAG")
txt = b"Annual leave policy. Employees accrue 1.5 days of paid leave every month. Carry forward is limited to 10 days."
r = c.post("/documents/upload", data={"files": (io.BytesIO(txt), "leave.txt"), "category": "HR", "visibility": "company"},
           headers=JSON, content_type="multipart/form-data")
check("upload txt", r.status_code == 200, r.get_data(as_text=True)[:100])
r = c.post("/documents/upload", data={"files": (io.BytesIO(b"MZ..."), "virus.exe")}, headers=JSON,
           content_type="multipart/form-data")
check("exe upload rejected", r.status_code == 400)
priv = b"CONFIDENTIAL salary revision plan for the executive team: bonus pool is 5 million."
c.post("/documents/upload", data={"files": (io.BytesIO(priv), "secret.txt"), "visibility": "private"},
       headers=JSON, content_type="multipart/form-data")
with app.app_context():
    docs = {d.filename: d.id for d in Document.query.all()}
for _ in range(40):
    with app.app_context():
        if all(d.status != "processing" for d in Document.query.all()):
            break
    time.sleep(0.5)
with app.app_context():
    states = {d.filename: d.status for d in Document.query.all()}
check("documents processed", all(s == "ready" for s in states.values()), str(states))
r = c.post(f"/documents/{docs['leave.txt']}/ask", json={"question": "How many days of leave accrue per month?"}, headers=JSON)
body = r.get_json() or {}
check("document Q&A returns sources", r.status_code == 200 and bool(body.get("sources")), str(body)[:120])
r = c.post("/api/chat/conversations", headers=JSON)
cid = r.get_json()["id"]
r = c.post(f"/api/chat/conversations/{cid}/send", json={"message": "carry forward leave"}, headers=JSON)
check("chat message answered", r.status_code == 200 and r.get_json()["message"]["mode"] == "demo")
check("chat export", c.get(f"/copilot/{cid}/export").status_code == 200)

print("\n== Reports")
for rtype in ["task_completion", "employee_activity", "department", "document_summary", "monthly_business"]:
    r = c.post("/reports/generate", data={"report_type": rtype, "start": "2020-01-01", "end": "2031-01-01"})
    check(f"generate {rtype}", r.status_code == 302, str(r.status_code))
d = c.get("/reports/1/download")
check("PDF download", d.status_code == 200 and d.data[:4] == b"%PDF")
check("CSV export", c.get("/reports/1/csv").status_code == 200)

print("\n== Permissions (employee)")
e = app.test_client()
check("employee login", login(e, "emp@test.local", "Employee1234").status_code == 302)
for url in ["/analytics/", "/admin/audit", "/employees/new", "/knowledge/new", f"/employees/{ADMIN_ID}/edit",
            "/employees/departments"]:
    check(f"employee blocked from {url}", e.get(url).status_code == 403)
check("employee cannot open private doc", e.get(f"/documents/{docs['secret.txt']}").status_code == 403)
check("employee cannot download private doc", e.get(f"/documents/{docs['secret.txt']}/file").status_code == 403)
check("employee can open shared doc", e.get(f"/documents/{docs['leave.txt']}").status_code == 200)
r = e.post("/api/chat/conversations", headers=JSON)
ecid = r.get_json()["id"]
r = e.post(f"/api/chat/conversations/{ecid}/send", json={"message": "bonus pool executive salary revision"}, headers=JSON)
check("RAG never leaks private document", "5 million" not in r.get_data(as_text=True))
check("employee sees assigned task", e.get(f"/tasks/{TASK_ID}").status_code == 200)
check("employee cannot read admin's conversation", e.get(f"/copilot/{cid}").status_code == 404)
check("employee cannot delete admin's document", e.post(f"/documents/{docs['leave.txt']}/delete").status_code == 403)
check("employee cannot post announcement", e.post("/admin/announcements", data={"title": "x", "body": "y"}).status_code == 403)
check("employee dashboard", e.get("/dashboard").status_code == 200)

print("\n== Account lockout")
f = app.test_client()
for _ in range(5):
    login(f, "emp@test.local", "wrong")
check("account locks after repeated failures", b"Too many" in login(f, "emp@test.local", "Employee1234").data)

print("\n" + ("ALL CHECKS PASSED" if not FAILS else f"{len(FAILS)} CHECK(S) FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
