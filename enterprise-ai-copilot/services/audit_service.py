import logging

from flask import has_request_context, request
from flask_login import current_user

from extensions import db
from models import AuditLog
from services.utils import client_ip, clamp

log = logging.getLogger(__name__)


def record(action, details="", status="success", user=None, email=None):
    """Write an audit row. Never raises: auditing must not break the user's request."""
    try:
        if user is None and has_request_context() and current_user.is_authenticated:
            user = current_user
        entry = AuditLog(
            user_id=user.id if user is not None else None,
            user_email=(user.email if user is not None else email),
            action=action, details=clamp(details, 500), status=status,
            ip_address=client_ip(request) if has_request_context() else None,
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:  # pragma: no cover
        db.session.rollback()
        log.exception("Could not write audit log")
