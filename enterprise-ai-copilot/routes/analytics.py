from datetime import date

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required

from extensions import db
from models import Department
from services import ai_service, analytics_service, audit_service
from services.permissions import admin_required
from services.utils import parse_date

bp = Blueprint("analytics", __name__, url_prefix="/analytics")


def _filters():
    d_start, d_end = analytics_service.default_range()
    start = parse_date(request.values.get("start")) or d_start
    end = parse_date(request.values.get("end")) or d_end
    if start > end:
        start, end = end, start
    dept = request.values.get("department", type=int)
    include_demo = request.values.get("demo", "1") != "0"
    return start, end, dept, include_demo


@bp.route("/")
@admin_required
def dashboard():
    start, end, dept, include_demo = _filters()
    data = analytics_service.get_analytics(start, end, dept, include_demo)
    return render_template("analytics/dashboard.html", data=data, start=start, end=end, dept=dept,
                           include_demo=include_demo, departments=Department.query.order_by(Department.name).all(),
                           live=ai_service.is_configured())


@bp.route("/ai-summary", methods=["POST"])
@admin_required
def ai_summary():
    start, end, dept, include_demo = _filters()
    data = analytics_service.get_analytics(start, end, dept, include_demo)
    d = db.session.get(Department, dept) if dept else None
    text = analytics_service.stats_as_text(data, start, end, d.name if d else None)
    try:
        res = ai_service.business_summary(text)
    except ai_service.AIError as exc:
        return jsonify(error=str(exc)), 502
    audit_service.record("analytics_summary_generated", f"{start} to {end}")
    return jsonify(res)
