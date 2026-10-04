"""
app/routes/student.py
---------------------
Everything a student (or parent) sees. Read-only.

    GET /student                    home: latest result, notices, fees
    GET /student/courses            subjects
    GET /student/courses/{id}       chapters + files (open / save offline)
    GET /student/saved              files saved on this phone
    GET /student/results            all published results + trend chart
    GET /student/results/{exam_id}  one exam in detail
    GET /student/ranks              class leaderboard
    GET /student/fees               instalments, UPI QR, receipts
    GET /student/notices            all live notices
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.config import standard_label
from app.deps import CurrentStudent, DbSession
from app.models import STUDENT_PASSED_OUT, Exam, Payment, Subject
from app.qr import QrError, qr_svg
from app.services import (
    academy_info,
    fee_totals,
    leaderboard,
    leaderboard_enabled,
    student_exam_history,
    subject_applies,
    subjects_for_student,
    summarise_exam,
    trend_chart,
    upi_link,
    visible_notices,
)
from app.templating import render

router = APIRouter(prefix="/student", tags=["student"])


@router.get("")
def home(request: Request, student: CurrentStudent, db: DbSession):
    history = student_exam_history(db, student)
    latest = history[-1] if history else None
    return render(request, "student/home.html", {
        "student": student,
        "class_label": standard_label(student.standard),
        "passed_out": student.status == STUDENT_PASSED_OUT,
        "latest": latest,
        "notices": visible_notices(db, student.standard, limit=5),
        "fees": fee_totals(student.fee_instalments),
        "subjects": subjects_for_student(db, student)[:6],
    })


@router.get("/courses")
def courses(request: Request, student: CurrentStudent, db: DbSession):
    subjects = subjects_for_student(db, student)
    cards = [{"subject": s, "chapters": len(s.chapters),
              "files": sum(len(c.contents) for c in s.chapters)} for s in subjects]
    return render(request, "student/courses.html", {"cards": cards})


@router.get("/courses/{subject_id}")
def course_subject(request: Request, subject_id: int, student: CurrentStudent, db: DbSession):
    subject = db.get(Subject, subject_id)
    if (subject is None or subject.archived or subject.standard != student.standard
            or not subject_applies(subject, student)):
        raise HTTPException(404)
    return render(request, "student/subject.html", {"subject": subject})


@router.get("/saved")
def saved(request: Request, student: CurrentStudent):
    return render(request, "student/saved.html", {})


@router.get("/results")
def results(request: Request, student: CurrentStudent, db: DbSession):
    history = student_exam_history(db, student)
    return render(request, "student/results.html", {
        "history": list(reversed(history)), "chart": trend_chart(history),
    })


@router.get("/results/{exam_id}")
def result_detail(request: Request, exam_id: int, student: CurrentStudent, db: DbSession):
    exam = db.get(Exam, exam_id)
    if exam is None or not exam.is_published:
        raise HTTPException(404)
    summary = summarise_exam(db, exam)
    row = summary.row_for(student.id)
    if row is None or row.percent is None:
        raise HTTPException(404)
    subjects = [es for es in summary.subjects if subject_applies(es.subject, student)]
    return render(request, "student/result_detail.html", {
        "exam": exam, "row": row, "subjects": subjects, "summary": summary,
    })


@router.get("/ranks")
def ranks(request: Request, student: CurrentStudent, db: DbSession):
    enabled = leaderboard_enabled(db, student.standard)
    board = leaderboard(db, student.standard) if enabled else None
    mine = None
    if board:
        mine = next((r for r in board["ranked"] if r.student.id == student.id), None)
    return render(request, "student/ranks.html", {
        "enabled": enabled, "board": board, "mine": mine,
        "top": board["ranked"][:10] if board else [], "class_label": standard_label(student.standard),
    })


@router.get("/fees")
def fees(request: Request, student: CurrentStudent, db: DbSession):
    instalments = sorted(student.fee_instalments, key=lambda i: (i.balance_paise == 0, i.due_date))
    totals = fee_totals(instalments)
    info = academy_info(db)
    link = upi_link(info, totals["balance"], f"Fees {student.user.login_id}") if totals["balance"] > 0 else None
    qr = None
    if link:
        try:
            qr = qr_svg(link)
        except QrError:
            qr = None
    payments = sorted((p for i in student.fee_instalments for p in i.payments),
                      key=lambda p: (p.paid_on, p.id), reverse=True)
    return render(request, "student/fees.html", {
        "instalments": instalments, "totals": totals, "info": info, "upi_link": link, "qr": qr,
        "payments": payments,
    })


@router.get("/notices")
def notices(request: Request, student: CurrentStudent, db: DbSession):
    return render(request, "student/notices.html", {"notices": visible_notices(db, student.standard)})
