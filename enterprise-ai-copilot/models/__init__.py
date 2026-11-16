"""SQLAlchemy models. Written to work on SQLite and MySQL 8 (utf8mb4, InnoDB)."""
import json
from datetime import date, datetime, timezone

from flask_login import UserMixin
from sqlalchemy import event
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Engine
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db

LongText = db.Text().with_variant(mysql.LONGTEXT(), "mysql")

TASK_STATUSES = ["To Do", "In Progress", "Completed"]  # "Overdue" is derived from the due date
PRIORITIES = ["Low", "Medium", "High", "Critical"]
TASK_CATEGORIES = ["General", "Development", "Operations", "HR", "Finance", "Marketing", "Support", "Compliance"]
DOC_CATEGORIES = ["General", "Policy", "HR", "Technical", "Finance", "Legal", "Report", "Meeting Notes"]
KB_CATEGORIES = ["Company Policy", "HR Document", "Technical Documentation", "Department Information", "FAQ"]
VISIBILITIES = {"private": "Only me", "department": "My department", "company": "Whole company"}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@event.listens_for(Engine, "connect")
def _enable_sqlite_fk(dbapi_conn, _record):
    if dbapi_conn.__class__.__module__.startswith("sqlite3"):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


class Department(db.Model):
    __tablename__ = "departments"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    is_demo = db.Column(db.Boolean, default=False, nullable=False)
    users = db.relationship("User", back_populates="department")


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(30), unique=True, nullable=False)
    full_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="employee")  # admin | employee
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id", ondelete="SET NULL"))
    designation = db.Column(db.String(100))
    phone = db.Column(db.String(30))
    joining_date = db.Column(db.Date)
    avatar = db.Column(db.String(255))
    theme = db.Column(db.String(10), default="light", nullable=False)
    notify_tasks = db.Column(db.Boolean, default=True, nullable=False)
    notify_documents = db.Column(db.Boolean, default=True, nullable=False)
    notify_announcements = db.Column(db.Boolean, default=True, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)
    failed_logins = db.Column(db.Integer, default=0, nullable=False)
    locked_until = db.Column(db.DateTime)
    last_login = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    is_demo = db.Column(db.Boolean, default=False, nullable=False)

    department = db.relationship("Department", back_populates="users")

    @property
    def is_active(self):  # Flask-Login hook
        return bool(self.active)

    @property
    def is_admin(self):
        return self.role == "admin"

    @property
    def initials(self):
        parts = [p for p in (self.full_name or "?").split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"

    def set_password(self, raw):
        self.password_hash = generate_password_hash(raw, method="pbkdf2:sha256", salt_length=16)

    def check_password(self, raw):
        return check_password_hash(self.password_hash, raw)

    def __repr__(self):
        return f"<User {self.email}>"


class Document(db.Model):
    __tablename__ = "documents"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255), nullable=False)
    filename = db.Column(db.String(255), nullable=False)       # original name (display only)
    stored_name = db.Column(db.String(100), nullable=False)    # random name on disk
    file_type = db.Column(db.String(10), nullable=False)
    file_size = db.Column(db.Integer, default=0, nullable=False)
    category = db.Column(db.String(50), default="General", nullable=False)
    description = db.Column(db.String(500))
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id", ondelete="SET NULL"))
    visibility = db.Column(db.String(20), default="private", nullable=False)  # private|department|company
    status = db.Column(db.String(20), default="processing", nullable=False)   # processing|ready|failed
    status_message = db.Column(db.String(500))
    page_count = db.Column(db.Integer)
    summary = db.Column(db.Text)
    key_points = db.Column(db.Text)      # JSON list
    keywords = db.Column(db.Text)        # JSON list
    ai_mode = db.Column(db.String(20))   # gemini | extractive
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    is_demo = db.Column(db.Boolean, default=False, nullable=False)

    owner = db.relationship("User", foreign_keys=[owner_id])
    department = db.relationship("Department")
    chunks = db.relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan",
                             foreign_keys="DocumentChunk.document_id")

    @property
    def keyword_list(self):
        return _load_list(self.keywords)

    @property
    def point_list(self):
        return _load_list(self.key_points)


class KnowledgeBase(db.Model):
    __tablename__ = "knowledge_base"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    category = db.Column(db.String(50), nullable=False)
    content = db.Column(LongText, nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id", ondelete="SET NULL"))
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    is_published = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    is_demo = db.Column(db.Boolean, default=False, nullable=False)

    author = db.relationship("User", foreign_keys=[created_by])
    department = db.relationship("Department")
    chunks = db.relationship("DocumentChunk", back_populates="kb", cascade="all, delete-orphan",
                             foreign_keys="DocumentChunk.kb_id")


class DocumentChunk(db.Model):
    """One retrievable passage. Belongs to a Document OR a KnowledgeBase entry."""
    __tablename__ = "document_chunks"
    id = db.Column(db.Integer, primary_key=True)
    document_id = db.Column(db.Integer, db.ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    kb_id = db.Column(db.Integer, db.ForeignKey("knowledge_base.id", ondelete="CASCADE"), index=True)
    chunk_index = db.Column(db.Integer, default=0, nullable=False)
    page_number = db.Column(db.Integer)
    section = db.Column(db.String(200))
    content = db.Column(db.Text, nullable=False)
    embedding = db.Column(db.LargeBinary)           # float32 vector bytes
    embedding_model = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)

    document = db.relationship("Document", back_populates="chunks", foreign_keys=[document_id])
    kb = db.relationship("KnowledgeBase", back_populates="chunks", foreign_keys=[kb_id])


class Conversation(db.Model):
    __tablename__ = "conversations"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(150), default="New conversation", nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    messages = db.relationship("Message", back_populates="conversation", cascade="all, delete-orphan",
                               order_by="Message.id")


class Message(db.Model):
    __tablename__ = "messages"
    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id", ondelete="CASCADE"),
                                nullable=False, index=True)
    role = db.Column(db.String(12), nullable=False)  # user | assistant
    content = db.Column(LongText, nullable=False)
    sources = db.Column(db.Text)                     # JSON list
    mode = db.Column(db.String(12))                  # live | demo
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    conversation = db.relationship("Conversation", back_populates="messages")

    def to_dict(self):
        return {"id": self.id, "role": self.role, "content": self.content,
                "sources": _load_list(self.sources), "mode": self.mode,
                "time": self.created_at.strftime("%d %b %H:%M")}


class Task(db.Model):
    __tablename__ = "tasks"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text)
    category = db.Column(db.String(50), default="General", nullable=False)
    priority = db.Column(db.String(20), default="Medium", nullable=False)
    status = db.Column(db.String(20), default="To Do", nullable=False)
    due_date = db.Column(db.Date)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    assigned_to = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"))
    department_id = db.Column(db.Integer, db.ForeignKey("departments.id", ondelete="SET NULL"))
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    is_demo = db.Column(db.Boolean, default=False, nullable=False)

    creator = db.relationship("User", foreign_keys=[created_by])
    assignee = db.relationship("User", foreign_keys=[assigned_to])
    department = db.relationship("Department")
    subtasks = db.relationship("Subtask", back_populates="task", cascade="all, delete-orphan",
                               order_by="Subtask.id")

    @property
    def effective_status(self):
        if self.status != "Completed" and self.due_date and self.due_date < date.today():
            return "Overdue"
        return self.status

    @property
    def subtask_progress(self):
        total = len(self.subtasks)
        done = sum(1 for s in self.subtasks if s.is_done)
        return done, total


class Subtask(db.Model):
    __tablename__ = "subtasks"
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    is_done = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    task = db.relationship("Task", back_populates="subtasks")


class Notification(db.Model):
    __tablename__ = "notifications"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.String(500))
    category = db.Column(db.String(30), default="system", nullable=False)  # task|deadline|document|announcement|system
    link = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, default=False, nullable=False)
    dedupe_key = db.Column(db.String(100), index=True)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    is_demo = db.Column(db.Boolean, default=False, nullable=False)


class Report(db.Model):
    __tablename__ = "reports"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    report_type = db.Column(db.String(40), nullable=False)
    generated_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    parameters = db.Column(db.Text)
    summary = db.Column(db.Text)
    summary_mode = db.Column(db.String(12))
    content_json = db.Column(LongText)
    file_name = db.Column(db.String(100))
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    author = db.relationship("User", foreign_keys=[generated_by])

    @property
    def content(self):
        try:
            return json.loads(self.content_json or "{}")
        except ValueError:
            return {}


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), index=True)
    user_email = db.Column(db.String(150))
    action = db.Column(db.String(60), nullable=False, index=True)
    details = db.Column(db.String(500))
    ip_address = db.Column(db.String(45))
    status = db.Column(db.String(10), default="success", nullable=False)  # success | failure | denied
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False, index=True)
    user = db.relationship("User", foreign_keys=[user_id])


class Announcement(db.Model):
    __tablename__ = "announcements"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    body = db.Column(db.Text, nullable=False)
    author_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    is_pinned = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow, nullable=False)
    is_demo = db.Column(db.Boolean, default=False, nullable=False)
    author = db.relationship("User", foreign_keys=[author_id])


def _load_list(raw):
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except ValueError:
        return []
