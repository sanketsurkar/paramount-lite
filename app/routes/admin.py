"""
app/routes/admin.py
-------------------
Admin dashboard (admin only).

    GET  /admin            overview: people, subjects, exams, fees, notices, recent activity
    GET  /admin/settings   academy details, UPI ID, leaderboard switches
    POST /admin/settings
    GET  /admin/audit      audit log (filter + pages)

Later phases add more admin pages in their own files (admin_people.py,
admin_fees.py, ...) - main.py picks every route file up automatically.
"""
from __future__ import annotations

import json

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from sqlalchemy import func, or_, select

from app.config import STANDARDS, current_academic_year, today_ist
from app.deps import AdminUser, DbSession, verify_csrf
from app.models import (
    STUDENT_ACTIVE,
    AuditLog,
    Exam,
    EXAM_PUBLISHED,
    FeeInstalment,
    Notice,
    Student,
    Subject,
    Teacher,
    User,
)
from app.services import (
    ACADEMY_KEYS,
    ServiceError,
    academy_info,
    audit,
    clean_text,
    leaderboard_enabled,
    set_setting,
)
from app.templating import redirect, render

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("")
def dashboard(request: Request, user: AdminUser, db: DbSession):
    year = current_academic_year()
    today = today_ist()

    # select(func.count()) ... is like  context.Students.Count(...)  in EF.
    students = db.scalar(
        select(func.count())
        .select_from(Student)
        .join(User, Student.user_id == User.id)
        .where(Student.status == STUDENT_ACTIVE, User.is_active.is_(True))
    ) or 0

    teachers = db.scalar(
        select(func.count())
        .select_from(Teacher)
        .join(User, Teacher.user_id == User.id)
        .where(User.is_active.is_(True))
    ) or 0

    subjects = db.scalar(
        select(func.count()).select_from(Subject).where(Subject.archived.is_(False))
    ) or 0

    exams_total = db.scalar(
        select(func.count()).select_from(Exam).where(Exam.academic_year == year)
    ) or 0
    exams_published = db.scalar(
        select(func.count())
        .select_from(Exam)
        .where(Exam.academic_year == year, Exam.status == EXAM_PUBLISHED)
    ) or 0

    # Fees for the current academic year (amounts are in paise).
    fee_total, fee_paid = db.execute(
        select(
            func.coalesce(func.sum(FeeInstalment.amount_paise), 0),
            func.coalesce(func.sum(FeeInstalment.paid_amount_paise), 0),
        ).where(FeeInstalment.academic_year == year)
    ).one()
    overdue = db.scalar(
        select(func.count())
        .select_from(FeeInstalment)
        .where(
            FeeInstalment.academic_year == year,
            FeeInstalment.due_date < today,
            FeeInstalment.paid_amount_paise < FeeInstalment.amount_paise,
        )
    ) or 0
    collected_percent = (fee_paid / fee_total * 100) if fee_total else 0.0

    notices_active = db.scalar(
        select(func.count())
        .select_from(Notice)
        .where(or_(Notice.expires_on.is_(None), Notice.expires_on >= today))
    ) or 0

    recent_activity = db.scalars(
        select(AuditLog).order_by(AuditLog.at.desc()).limit(6)
    ).all()

    return render(
        request,
        "admin/dashboard.html",
        {
            "year": year,
            "stats": {
                "students": students,
                "teachers": teachers,
                "subjects": subjects,
                "exams_total": exams_total,
                "exams_published": exams_published,
                "fee_total": fee_total,
                "fee_paid": fee_paid,
                "fee_outstanding": fee_total - fee_paid,
                "overdue": overdue,
                "collected_percent": collected_percent,
                "notices_active": notices_active,
            },
            "recent_activity": recent_activity,
        },
    )


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
@router.get("/settings")
def settings_page(request: Request, user: AdminUser, db: DbSession):
    return render(request, "admin/settings.html", {
        "info": academy_info(db),
        "boards": [(code, label, leaderboard_enabled(db, code)) for code, label in STANDARDS],
        "year": current_academic_year(),
    })


@router.post("/settings", dependencies=[Depends(verify_csrf)])
async def settings_save(request: Request, user: AdminUser, db: DbSession):
    form = await request.form()
    limits = {"name": 120, "tagline": 120, "address": 250, "phone": 40, "email": 120,
              "upi_id": 80, "upi_payee": 80}
    try:
        values = {}
        for short, full in ACADEMY_KEYS.items():
            required = short in ("name",)
            values[full] = clean_text(form.get(short), short.replace("_", " ").capitalize(),
                                      max_len=limits[short], required=required) or ""
        upi_id = values["upi.id"]
        if upi_id and "@" not in upi_id:
            raise ServiceError("A UPI ID looks like name@bank.")
    except ServiceError as exc:
        return redirect(request, "/admin/settings", str(exc), "error")
    for key, value in values.items():
        set_setting(db, key, value)
    enabled = set(form.getlist("leaderboard"))
    for code, _label in STANDARDS:
        set_setting(db, f"leaderboard.{code}", "on" if str(code) in enabled else "off")
    audit(db, user, "settings.update", "settings", None, upi_id=values["upi.id"],
          leaderboards_on=sorted(int(c) for c in enabled if c.lstrip("-").isdigit()))
    db.commit()
    return redirect(request, "/admin/settings", "Settings saved.")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
AUDIT_FILTERS = [("", "Everything"), ("marks", "Marks"), ("fees", "Fees"), ("password", "Passwords"),
                 ("subject", "Subjects"), ("students", "Promotion"), ("user", "People"),
                 ("exam", "Exams"), ("notes", "Notes"), ("notice", "Notices"), ("settings", "Settings")]
PAGE_SIZE = 50


@router.get("/audit")
def audit_page(request: Request, user: AdminUser, db: DbSession, kind: str = "", page: int = 1):
    page = max(1, page)
    query = select(AuditLog)
    count_query = select(func.count(AuditLog.id))
    if kind:
        query = query.where(AuditLog.action.like(kind + ".%"))
        count_query = count_query.where(AuditLog.action.like(kind + ".%"))
    total = db.scalar(count_query) or 0
    entries = db.scalars(query.order_by(AuditLog.at.desc(), AuditLog.id.desc())
                         .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    rows = []
    for entry in entries:
        try:
            details = json.loads(entry.details) if entry.details else {}
        except ValueError:
            details = {"raw": entry.details}
        rows.append({"entry": entry, "details": details})
    return render(request, "admin/audit.html", {
        "rows": rows, "kind": kind, "filters": AUDIT_FILTERS, "page": page,
        "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE), "total": total,
    })
