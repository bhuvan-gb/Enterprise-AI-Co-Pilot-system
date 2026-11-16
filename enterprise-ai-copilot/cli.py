"""Flask CLI commands: init-db, test-db, create-admin, seed-demo, clear-demo, export-schema."""
import os
import random
import secrets
import uuid
from datetime import date, datetime, timedelta

import click
from sqlalchemy import text

from extensions import db


def register_commands(app):

    @app.cli.command("init-db")
    def init_db():
        """Create all tables (works for SQLite and MySQL)."""
        import models  # noqa: F401  (registers tables)
        db.create_all()
        click.echo(f"Tables ready on {db.engine.dialect.name}: {db.engine.url.render_as_string(hide_password=True)}")

    @app.cli.command("test-db")
    def test_db():
        """Check the database connection and list the tables."""
        try:
            with db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            tables = sorted(db.inspect(db.engine).get_table_names())
            click.secho(f"Connected to {db.engine.dialect.name}. Tables ({len(tables)}): {', '.join(tables) or 'none yet — run flask init-db'}", fg="green")
        except Exception as exc:
            click.secho(f"Database connection FAILED: {exc}", fg="red")
            raise SystemExit(1)

    @app.cli.command("export-schema")
    def export_schema():
        """Print the MySQL DDL generated from the models."""
        from sqlalchemy.dialects import mysql
        from sqlalchemy.schema import CreateIndex, CreateTable
        import models  # noqa: F401
        dialect = mysql.dialect()
        for table in db.metadata.sorted_tables:
            click.echo(str(CreateTable(table).compile(dialect=dialect)).strip() + ";\n")
            for idx in table.indexes:
                click.echo(str(CreateIndex(idx).compile(dialect=dialect)).strip() + ";")
            click.echo()

    @app.cli.command("create-admin")
    @click.option("--name", default=lambda: os.getenv("ADMIN_NAME") or None)
    @click.option("--email", default=lambda: os.getenv("ADMIN_EMAIL") or None)
    @click.option("--password", default=lambda: os.getenv("ADMIN_PASSWORD") or None)
    def create_admin(name, email, password):
        """Create an administrator (prompts for anything not supplied)."""
        from models import User
        from services.user_service import next_employee_id
        from services.utils import valid_email, validate_password
        name = name or click.prompt("Full name")
        email = (email or click.prompt("Email")).strip().lower()
        if not valid_email(email):
            raise click.ClickException("Invalid email address.")
        if User.query.filter_by(email=email).first():
            raise click.ClickException("A user with that email already exists.")
        password = password or click.prompt("Password", hide_input=True, confirmation_prompt=True)
        err = validate_password(password)
        if err:
            raise click.ClickException(err)
        u = User(employee_id=next_employee_id(), full_name=name, email=email, role="admin",
                 designation="Administrator", joining_date=date.today())
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        click.secho(f"Administrator created: {email}", fg="green")

    @app.cli.command("seed-demo")
    @click.option("--password", default=None, help="Password for demo users (random if omitted).")
    def seed_demo(password):
        """Insert clearly-flagged demo data (employees, tasks, documents, notifications...)."""
        from models import (Announcement, Department, Document, KnowledgeBase, Notification, Subtask, Task, User)
        from services import document_service, rag_service
        if User.query.filter_by(is_demo=True).first():
            raise click.ClickException("Demo data already exists. Run `flask clear-demo` first.")
        rng = random.Random(7)
        password = password or (secrets.token_urlsafe(8) + "a1")
        admin = User.query.filter_by(role="admin").first()
        if admin is None:
            raise click.ClickException("Create an administrator first: flask create-admin")

        depts = {}
        for name, desc in [("Engineering", "Product and platform development"), ("Human Resources", "People operations"),
                           ("Finance", "Accounting and budgeting"), ("Marketing", "Brand and growth"),
                           ("Operations", "Business operations and support")]:
            d = Department.query.filter_by(name=name).first()
            if d is None:
                d = Department(name=name, description=desc, is_demo=True)
                db.session.add(d)
            depts[name] = d
        db.session.flush()

        people = [("Aarav Sharma", "Engineering", "Senior Developer"), ("Diya Patel", "Engineering", "QA Engineer"),
                  ("Rohan Mehta", "Human Resources", "HR Manager"), ("Ananya Reddy", "Finance", "Financial Analyst"),
                  ("Kabir Singh", "Marketing", "Marketing Lead"), ("Isha Nair", "Operations", "Operations Executive"),
                  ("Vikram Rao", "Engineering", "DevOps Engineer"), ("Meera Iyer", "Finance", "Accountant")]
        users = []
        for i, (name, dept, role) in enumerate(people, 1):
            u = User(employee_id=f"DEMO-{i:03d}", full_name=name, email=f"{name.split()[0].lower()}.{i}@demo.example",
                     role="employee", department_id=depts[dept].id, designation=role,
                     joining_date=date.today() - timedelta(days=rng.randint(60, 900)), is_demo=True,
                     last_login=datetime.utcnow() - timedelta(days=rng.randint(0, 14)))
            u.set_password(password)
            db.session.add(u)
            users.append(u)
        db.session.flush()

        titles = ["Update onboarding checklist", "Fix login timeout bug", "Prepare Q3 budget review", "Launch email campaign",
                  "Audit vendor contracts", "Migrate staging database", "Write API documentation", "Plan team offsite",
                  "Reconcile monthly invoices", "Refresh brand guidelines", "Security patch rollout", "Customer survey analysis",
                  "Review leave policy draft", "Set up monitoring alerts", "Train new hires on tools"]
        cats = ["Development", "HR", "Finance", "Marketing", "Compliance", "Operations", "General"]
        now = datetime.utcnow()
        for i in range(32):
            assignee = rng.choice(users)
            created = now - timedelta(days=rng.randint(2, 150))
            due = (created + timedelta(days=rng.randint(5, 40))).date()
            status = rng.choices(["To Do", "In Progress", "Completed"], [3, 3, 5])[0]
            t = Task(title=f"{rng.choice(titles)} #{i + 1}", description="Demo task generated for illustration.",
                     category=rng.choice(cats), priority=rng.choice(["Low", "Medium", "High", "Critical"]), status=status,
                     due_date=due, created_by=admin.id, assigned_to=assignee.id, department_id=assignee.department_id,
                     created_at=created, is_demo=True,
                     completed_at=(created + timedelta(days=rng.randint(1, 30))) if status == "Completed" else None)
            if t.completed_at and t.completed_at > now:
                t.completed_at = now
            db.session.add(t)
            db.session.flush()
            if i % 4 == 0:
                for s in ("Gather requirements", "Do the work", "Review"):
                    db.session.add(Subtask(task_id=t.id, title=s, is_done=status == "Completed"))
        db.session.commit()

        samples = {
            "Leave Policy (demo)": ("HR", "Employees accrue 1.5 days of paid annual leave per month, up to 18 days a year. "
                "Leave requests must be submitted through the HR portal at least five working days in advance. Managers approve "
                "or reject requests within two business days. Unused leave up to 10 days may be carried forward to the next "
                "calendar year. Sick leave is 12 days per year and requires a medical certificate after three consecutive days. "
                "Maternity leave is 26 weeks and paternity leave is 10 working days."),
            "IT Security Guidelines (demo)": ("Technical", "Passwords must contain at least 12 characters and be changed every "
                "90 days. Multi-factor authentication is mandatory for email, VPN and the admin console. Company laptops must "
                "use full-disk encryption and lock automatically after five minutes of inactivity. Report phishing e-mails to "
                "the security desk immediately. Never share credentials, and never install unapproved software on company devices. "
                "Security incidents must be reported within one hour of discovery."),
            "Q3 Sales Brief (demo)": ("Report", "In the third quarter, revenue grew 14 percent compared with the previous quarter, "
                "driven mainly by the enterprise segment. The sales team closed 42 new accounts, and customer churn fell to 2.1 "
                "percent. The marketing team's webinar series generated 1,200 qualified leads. Key risks include longer sales "
                "cycles in the public sector and rising cloud costs. The fourth-quarter plan focuses on renewals and expanding "
                "into two new regions.")}
        folder = document_service.docs_folder()
        for title, (cat, body) in samples.items():
            stored = f"{uuid.uuid4().hex}.txt"
            with open(os.path.join(folder, stored), "w", encoding="utf-8") as fh:
                fh.write(f"{title}\n\n{body}\n")
            doc = Document(title=title, filename=title.replace(" ", "_") + ".txt", stored_name=stored, file_type="txt",
                           file_size=os.path.getsize(os.path.join(folder, stored)), category=cat, owner_id=admin.id,
                           visibility="company", status="processing", is_demo=True)
            db.session.add(doc)
            db.session.commit()
            document_service.process(doc.id)

        for title, cat, body in [("Work-from-home policy (demo)", "Company Policy",
                                  "Employees may work from home up to two days per week with manager approval. Core hours are 11:00 to 16:00 local time."),
                                 ("How do I reset my VPN token? (demo)", "FAQ",
                                  "Open the IT service desk portal, choose 'VPN token reset' and approve the request on your phone. Resets complete within 15 minutes."),
                                 ("Expense reimbursement (demo)", "HR Document",
                                  "Submit expense claims within 30 days with receipts. Claims above 5,000 INR need director approval. Payment is made in the next payroll cycle.")]:
            kb = KnowledgeBase(title=title, category=cat, content=body, created_by=admin.id, is_demo=True)
            db.session.add(kb)
            db.session.commit()
            rag_service.index_knowledge(kb)

        db.session.add_all([Announcement(title="Welcome to the platform (demo)", body="This is a demo announcement.",
                                         author_id=admin.id, is_pinned=True, is_demo=True),
                            Announcement(title="Quarterly town-hall on Friday (demo)", body="Join us at 4 pm in the main hall.",
                                         author_id=admin.id, is_demo=True)])
        for u in users[:4]:
            db.session.add(Notification(user_id=u.id, title="Welcome (demo)", message="Demo notification.",
                                        category="system", is_demo=True))
        db.session.commit()
        click.secho("Demo data created. All demo records are flagged and can be removed with `flask clear-demo`.", fg="green")
        click.echo(f"Demo employee password (shown once): {password}")
        click.echo("Demo logins: " + ", ".join(u.email for u in users[:3]) + " ...")

    @app.cli.command("clear-demo")
    def clear_demo():
        """Remove every record flagged as demo data."""
        from models import (Announcement, Department, Document, KnowledgeBase, Notification, Task, User)
        from services import document_service, rag_service
        admin = User.query.filter_by(role="admin").first()
        demo_ids = [u.id for u in User.query.filter_by(is_demo=True)]
        for d in Document.query.filter_by(is_demo=True).all():
            document_service.delete_document(d)
        if demo_ids and admin:
            Document.query.filter(Document.owner_id.in_(demo_ids)).update({"owner_id": admin.id}, synchronize_session=False)
            Task.query.filter(Task.created_by.in_(demo_ids), Task.is_demo.is_(False)).update({"created_by": admin.id}, synchronize_session=False)
            KnowledgeBase.query.filter(KnowledgeBase.created_by.in_(demo_ids)).update({"created_by": admin.id}, synchronize_session=False)
        for model in (Task, Notification, Announcement, KnowledgeBase):
            for row in model.query.filter_by(is_demo=True).all():
                db.session.delete(row)
        db.session.commit()
        if demo_ids:
            Task.query.filter(Task.assigned_to.in_(demo_ids)).update({"assigned_to": None}, synchronize_session=False)
            db.session.commit()
            for u in User.query.filter(User.id.in_(demo_ids)).all():
                db.session.delete(u)
            db.session.commit()
        for d in Department.query.filter_by(is_demo=True).all():
            if not User.query.filter_by(department_id=d.id).count():
                db.session.delete(d)
        db.session.commit()
        rag_service.invalidate()
        click.secho("Demo data removed.", fg="green")
