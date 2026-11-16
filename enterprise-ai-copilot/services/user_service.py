import smtplib
from email.message import EmailMessage

from flask import current_app

from extensions import db
from models import User


def next_employee_id():
    n = (db.session.query(db.func.max(User.id)).scalar() or 0) + 1
    while True:
        code = f"EMP-{n:04d}"
        if not User.query.filter_by(employee_id=code).first():
            return code
        n += 1


def send_mail(to, subject, body):
    """Send via SMTP if configured. Returns True when sent."""
    cfg = current_app.config
    if not cfg["SMTP_HOST"]:
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg["SMTP_FROM"], to, subject
    msg.set_content(body)
    with smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=15) as s:
        s.starttls()
        if cfg["SMTP_USER"]:
            s.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
        s.send_message(msg)
    return True
