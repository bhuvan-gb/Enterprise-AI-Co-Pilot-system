import os
from datetime import date, timedelta

from flask import (Blueprint, Response, abort, current_app, flash, redirect, render_template, request, send_file, url_for)
from flask_login import current_user, login_required

from extensions import db
from models import Department, Report
from services import ai_service, audit_service, report_service
from services.utils import parse_date

bp = Blueprint("reports", __name__, url_prefix="/reports")


def _get(rid):
    r = db.session.get(Report, rid) or abort(404)
    if not current_user.is_admin and r.generated_by != current_user.id:
        audit_service.record("report_access_denied", f"Report {rid}", "denied")
        abort(403)
    return r


@bp.route("/")
@login_required
def index():
    q = Report.query
    if not current_user.is_admin:
        q = q.filter_by(generated_by=current_user.id)
    pag = q.order_by(Report.created_at.desc()).paginate(page=request.args.get("page", 1, type=int), per_page=10,
                                                         error_out=False)
    return render_template("reports/index.html", pag=pag, types=report_service.available_types(current_user),
                           departments=Department.query.order_by(Department.name).all(),
                           today=date.today(), month_ago=date.today() - timedelta(days=30), live=ai_service.is_configured())


@bp.route("/generate", methods=["POST"])
@login_required
def generate():
    rtype = request.form.get("report_type")
    if rtype not in report_service.available_types(current_user):
        audit_service.record("report_denied", str(rtype), "denied")
        abort(403)
    start = parse_date(request.form.get("start")) or date.today() - timedelta(days=30)
    end = parse_date(request.form.get("end")) or date.today()
    if start > end:
        start, end = end, start
    dept = request.form.get("department", type=int) if current_user.is_admin else None
    try:
        report = report_service.create_report(rtype, current_user, start, end, dept)
    except Exception as exc:  # report building should never show a stack trace to the user
        db.session.rollback()
        current_app.logger.exception("Report generation failed")
        flash(f"The report could not be generated: {exc}", "danger")
        return redirect(url_for("reports.index"))
    audit_service.record("report_generated", f"{report.title} (#{report.id})")
    flash("Report generated.", "success")
    return redirect(url_for("reports.view", rid=report.id))


@bp.route("/<int:rid>")
@login_required
def view(rid):
    r = _get(rid)
    return render_template("reports/view.html", r=r, c=r.content)


@bp.route("/<int:rid>/download")
@login_required
def download(rid):
    r = _get(rid)
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "reports", r.file_name or "")
    if not r.file_name or not os.path.exists(path):
        flash("The PDF file is missing. Generate the report again.", "danger")
        return redirect(url_for("reports.view", rid=rid))
    audit_service.record("report_downloaded", f"#{r.id}")
    return send_file(path, mimetype="application/pdf", as_attachment=True,
                     download_name=f"report-{r.id}-{r.report_type}.pdf")


@bp.route("/<int:rid>/csv")
@login_required
def csv_export(rid):
    r = _get(rid)
    audit_service.record("report_exported_csv", f"#{r.id}")
    return Response(report_service.to_csv(r.content), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="report-{r.id}.csv"'})


@bp.route("/<int:rid>/delete", methods=["POST"])
@login_required
def delete(rid):
    r = _get(rid)
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], "reports", r.file_name or "")
    db.session.delete(r)
    db.session.commit()
    if r.file_name and os.path.exists(path):
        os.remove(path)
    flash("Report deleted.", "success")
    return redirect(url_for("reports.index"))
