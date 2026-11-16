"""Central configuration. All secrets come from environment variables (.env)."""
import os
import secrets
from datetime import timedelta
from urllib.parse import quote_plus

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
load_dotenv(os.path.join(BASE_DIR, ".env"))


def _bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _secret_key():
    """Use SECRET_KEY from env; otherwise generate one once and keep it in instance/."""
    key = (os.getenv("SECRET_KEY") or "").strip()
    if key and key.lower() not in ("change-me", "changeme"):
        return key
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    path = os.path.join(INSTANCE_DIR, ".secret_key")
    if os.path.exists(path):
        with open(path) as fh:
            return fh.read().strip()
    key = secrets.token_hex(32)
    with open(path, "w") as fh:
        fh.write(key)
    return key


def _database_uri():
    """DATABASE_URL wins. Otherwise DB_ENGINE=mysql builds a PyMySQL URI, else SQLite."""
    url = (os.getenv("DATABASE_URL") or "").strip()
    if url:
        return url
    if os.getenv("DB_ENGINE", "sqlite").strip().lower() == "mysql":
        user = quote_plus(os.getenv("MYSQL_USER", "copilot_user"))
        pwd = quote_plus(os.getenv("MYSQL_PASSWORD", ""))
        host = os.getenv("MYSQL_HOST", "127.0.0.1")
        port = os.getenv("MYSQL_PORT", "3306")
        name = os.getenv("MYSQL_DATABASE", "enterprise_copilot")
        return f"mysql+pymysql://{user}:{pwd}@{host}:{port}/{name}?charset=utf8mb4"
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    return "sqlite:///" + os.path.join(INSTANCE_DIR, "copilot.db").replace("\\", "/")


class Config:
    APP_NAME = "Enterprise AI Co-Pilot"
    INSTANCE_DIR = INSTANCE_DIR
    SECRET_KEY = _secret_key()
    SQLALCHEMY_DATABASE_URI = _database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 280}

    # Sessions / cookies
    PERMANENT_SESSION_LIFETIME = timedelta(hours=int(os.getenv("SESSION_HOURS", "8")))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)  # set True behind HTTPS
    WTF_CSRF_TIME_LIMIT = None

    # Uploads
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_UPLOAD_MB", "20")) * 1024 * 1024
    ALLOWED_DOC_EXTENSIONS = {"pdf", "docx", "txt"}
    ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
    MAX_AVATAR_BYTES = 2 * 1024 * 1024

    # AI
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    AI_TIMEOUT = int(os.getenv("AI_TIMEOUT_SECONDS", "60"))
    EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "auto")  # auto | sentence-transformers | hashing
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
    RAG_TOP_K = int(os.getenv("RAG_TOP_K", "5"))

    # Auth
    ALLOW_REGISTRATION = _bool("ALLOW_REGISTRATION", True)
    SHOW_RESET_LINK = _bool("SHOW_RESET_LINK", False)  # dev only: show reset link on screen
    MAX_FAILED_LOGINS = int(os.getenv("MAX_FAILED_LOGINS", "5"))
    LOCKOUT_MINUTES = int(os.getenv("LOCKOUT_MINUTES", "15"))
    RESET_TOKEN_MINUTES = 30

    # Optional SMTP for password-reset e-mails
    SMTP_HOST = os.getenv("SMTP_HOST", "")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    SMTP_FROM = os.getenv("SMTP_FROM", "no-reply@example.com")
