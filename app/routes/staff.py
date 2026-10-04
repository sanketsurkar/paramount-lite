"""
app/routes/staff.py
-------------------
Pages shared by teachers and the admin ("staff").

    GET  /staff                         teacher home (admin goes to /admin)
    GET  /staff/students                student list (teachers: own classes only)
    GET  /staff/students/{id}           student profile + results
    GET  /staff/notices                 notices list + create form
    POST /staff/notices                 create notice
    POST /staff/notices/{id}/delete     delete notice
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, or_, select

from app.config import STANDARDS, STREAM_STANDARDS, STREAMS, standard_label, today_ist
from app.deps import DbSession, StaffUser, allowed_standards, ensure_standard_access, verify_csrf
from app.models import STUDENT_ACTIVE, STUDENT_PASSED_OUT, Exam, Notice, Student, User
from app.services import (
    ServiceError,
    active_students_query,
    create_notice,
    fee_totals,
    parse_standard,
    standard_options,
    student_exam_history,
    trend_chart,
)
from app.templating import redirect, render

router = APIRouter(prefix="/staff", tags=["staff"])


@router.get("")
def staff_home(request: Request, user: StaffUser, db: DbSession):
    if user.is_admin:
        return RedirectResponse("/admin", status_code=303)
    codes = allowed_standards(user) or []
    classes = []
    for code in codes:
        count = db.scalar(select(func.count()).select_from(active_students_query(code).subquery())) or 0
        classes.append({"code": code, "label": standard_label(code), "students": count})
    exams = db.scalars(
        select(Exam).where(Exam.standard.in_(codes or [-99])).order_by(Exam.created_at.desc()).limit(6)
    ).all()
    notices = db.scalars(
        select(Notice).where(or_(Notice.standard.is_(None), Notice.standard.in_(codes or [-99])),
                             or_(Notice.expires_on.is_(None), Notice.expires_on >= today_ist()))
        .order_by(Notice.created_at.desc()).limit(5)
    ).all()
    return render(request, "staff/home.html", {"classes": classes, "exams": exams, "notices": notices})


@router.get("/students")
def students_list(
    request: Request, user: StaffUser, db: DbSession,
    standard: str = "", status: str = "active", q: str = "",
):
    allowed = allowed_standards(user)
    selected = parse_standard(standard) if standard != "" else None
    query = select(Student).join(User, Student.user_id == User.id)
    if allowed is not None:
        query = query.where(Student.standard.in_(allowed or [-99]))
    if selected is not None:
        ensure_standard_access(user, selected)
        query = query.where(Student.standard == selected)
    if status == "active":
        query = query.where(Student.status == STUDENT_ACTIVE, User.is_active.is_(True))
    elif status == "inactive":
        query = query.where(User.is_active.is_(False))
    elif status == "passed_out":
        query = query.where(Student.status == STUDENT_PASSED_OUT)
    search = q.strip()
    if search:
        like = f"%{search}%"
        query = query.where(or_(User.full_name.ilike(like), User.login_id.ilike(like),
                                Student.phone.ilike(like)))
    students = db.scalars(query.order_by(Student.standard, User.full_name).limit(500)).all()
    return render(request, "staff/students.html", {
        "students": students, "selected": selected, "status": status, "q": search,
        "class_options": standard_options(allowed), "streams": STREAMS,
        "stream_standards": STREAM_STANDARDS,
    })


@router.get("/students/{student_id}")
def student_detail(request: Request, student_id: int, user: StaffUser, db: DbSession):
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(404)
    ensure_standard_access(user, student.standard)
    history = student_exam_history(db, student)
    return render(request, "staff/student_detail.html", {
        "student": student,
        "history": list(reversed(history)),
        "chart": trend_chart(history),
        "fees": fee_totals(student.fee_instalments) if user.is_admin else None,
        "class_options": standard_options(None), "streams": STREAMS,
        "stream_standards": STREAM_STANDARDS,
    })


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------
@router.get("/notices")
def notices_page(request: Request, user: StaffUser, db: DbSession):
    allowed = allowed_standards(user)
    query = select(Notice).order_by(Notice.created_at.desc()).limit(200)
    if allowed is not None:
        query = query.where(or_(Notice.standard.is_(None), Notice.standard.in_(allowed or [-99])))
    notices = db.scalars(query).all()
    return render(request, "staff/notices.html", {
        "notices": notices, "today": today_ist(), "class_options": standard_options(allowed),
    })


@router.post("/notices", dependencies=[Depends(verify_csrf)])
def notices_create(
    request: Request, user: StaffUser, db: DbSession,
    title: Annotated[str, Form()] = "", body: Annotated[str, Form()] = "",
    standard: Annotated[str, Form()] = "", expires_on: Annotated[str, Form()] = "",
):
    target = None if standard in ("", "all") else parse_standard(standard)
    if target is None and standard not in ("", "all"):
        return redirect(request, "/staff/notices", "Choose a valid class.", "error")
    if target is None and not user.is_admin:
        return redirect(request, "/staff/notices", "Teachers can post notices to their own classes only.", "error")
    if target is not None:
        ensure_standard_access(user, target)
    try:
        create_notice(db, user, title=title, body=body, standard=target, expires_on=expires_on)
    except ServiceError as exc:
        return redirect(request, "/staff/notices", str(exc), "error")
    db.commit()
    return redirect(request, "/staff/notices", "Notice posted.")


@router.post("/notices/{notice_id}/delete", dependencies=[Depends(verify_csrf)])
def notices_delete(request: Request, notice_id: int, user: StaffUser, db: DbSession):
    notice = db.get(Notice, notice_id)
    if notice is None:
        raise HTTPException(404)
    if not user.is_admin and notice.created_by != user.id:
        raise HTTPException(403, "You can delete only notices you posted.")
    db.delete(notice)
    db.commit()
    return redirect(request, "/staff/notices", "Notice deleted.")
