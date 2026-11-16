"""Small shared helpers: validation, parsing, formatting."""
import re
from datetime import date, datetime
from urllib.parse import urlparse

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def valid_email(value):
    return bool(value) and len(value) <= 150 and bool(EMAIL_RE.match(value))


def validate_password(pw):
    """Return an error message, or None when the password is acceptable."""
    if len(pw or "") < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return "Password must contain at least one letter and one number."
    if len(pw) > 128:
        return "Password is too long."
    return None


def parse_date(value):
    if not value:
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None


def safe_next_url(target):
    """Only allow same-site relative redirects (prevents open redirects)."""
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def human_size(num):
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024


def clamp(text, limit):
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def client_ip(request):
    return (request.headers.get("X-Forwarded-For", request.remote_addr or "") or "").split(",")[0].strip()[:45]
