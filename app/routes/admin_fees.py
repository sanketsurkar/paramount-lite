"""
app/routes/admin_fees.py
------------------------
Fees (admin only - teachers never see fees) and printable receipts.

    GET  /admin/fees?standard=&status=          overview per student
    POST /admin/fees/instalments                instalment for a whole class
    GET  /admin/fees/student/{id}               one student's fees
    POST /admin/fees/student/{id}/instalments   instalment for one student
    POST /admin/fees/instalments/{id}/delete    delete (only if unpaid)
    POST /admin/fees/instalments/{id}/pay       record payment -> receipt
    GET  /receipts/{payment_id}                 receipt (admin or that student)
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select

from app.config import PAYMENT_METHODS, STREAM_STANDARDS, STREAMS, standard_label, today_ist
from app.deps import AdminUser, CurrentUser, DbSession, verify_csrf
from app.models import FEE_DUE, FEE_OVERDUE, FEE_PAID, FEE_PARTIAL, FeeInstalment, Payment, Student, User
from app.services import (
    ServiceError,
    academy_info,
    active_students_query,
    create_instalments,
    delete_instalment,
    fee_totals,
    normalise_stream,
    parse_standard,
    record_payment,
    standard_options,
)
from app.templating import redirect, render

router = APIRouter(tags=["fees"])
STATUSES = [FEE_OVERDUE, FEE_DUE, FEE_PARTIAL, FEE_PAID]


@router.get("/admin/fees")
def fees_overview(request: Request, user: AdminUser, db: DbSession, standard: str = "", status: str = ""):
    selected = parse_standard(standard) if standard != "" else None
    students = db.scalars(active_students_query(selected)).all()
    rows, grand = [], {"total": 0, "paid": 0, "balance": 0, "overdue": 0}
    for student in students:
        totals = fee_totals(student.fee_instalments)
        if status and totals["status"] != status:
            continue
        rows.append({"student": student, "totals": totals})
        for key in grand:
            grand[key] += totals[key]
    rows.sort(key=lambda r: (r["student"].standard, r["student"].user.full_name))
    return render(request, "admin/fees.html", {
        "rows": rows, "grand": grand, "selected": selected, "status": status, "statuses": STATUSES,
        "class_options": standard_options(None), "streams": STREAMS, "stream_standards": STREAM_STANDARDS,
        "today": today_ist().isoformat(),
    })


@router.post("/admin/fees/instalments", dependencies=[Depends(verify_csrf)])
def fees_add_for_class(
    request: Request, user: AdminUser, db: DbSession,
    standard: Annotated[str, Form()] = "", stream: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "", amount: Annotated[str, Form()] = "",
    due_date: Annotated[str, Form()] = "",
):
    code = parse_standard(standard)
    if code is None:
        return redirect(request, "/admin/fees", "Choose a class.", "error")
    try:
        stream_clean = normalise_stream(stream, code)
        students = [s for s in db.scalars(active_students_query(code)).all()
                    if stream_clean is None or s.stream == stream_clean]
        count = create_instalments(db, user, students, title=title, amount=amount, due_date=due_date)
    except ServiceError as exc:
        db.rollback()
        return redirect(request, f"/admin/fees?standard={code}", str(exc), "error")
    db.commit()
    return redirect(request, f"/admin/fees?standard={code}",
                    f"Instalment added for {count} student{'s' if count != 1 else ''} in {standard_label(code)}.")


def _student(db, student_id: int) -> Student:
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(404)
    return student


@router.get("/admin/fees/student/{student_id}")
def fees_student(request: Request, student_id: int, user: AdminUser, db: DbSession):
    student = _student(db, student_id)
    instalments = sorted(student.fee_instalments, key=lambda i: (i.due_date, i.id))
    payments = sorted((p for i in instalments for p in i.payments), key=lambda p: (p.paid_on, p.id), reverse=True)
    return render(request, "admin/fee_student.html", {
        "student": student, "instalments": instalments, "payments": payments,
        "totals": fee_totals(instalments), "methods": PAYMENT_METHODS, "today": today_ist().isoformat(),
        "unpaid": [i for i in instalments if i.balance_paise > 0],
    })


@router.post("/admin/fees/student/{student_id}/instalments", dependencies=[Depends(verify_csrf)])
def fees_add_for_student(
    request: Request, student_id: int, user: AdminUser, db: DbSession,
    title: Annotated[str, Form()] = "", amount: Annotated[str, Form()] = "",
    due_date: Annotated[str, Form()] = "",
):
    student = _student(db, student_id)
    back = f"/admin/fees/student/{student.id}"
    try:
        create_instalments(db, user, [student], title=title, amount=amount, due_date=due_date)
    except ServiceError as exc:
        db.rollback()
        return redirect(request, back, str(exc), "error")
    db.commit()
    return redirect(request, back, "Instalment added.")


@router.post("/admin/fees/instalments/{instalment_id}/delete", dependencies=[Depends(verify_csrf)])
def fees_delete_instalment(request: Request, instalment_id: int, user: AdminUser, db: DbSession):
    instalment = db.get(FeeInstalment, instalment_id)
    if instalment is None:
        raise HTTPException(404)
    back = f"/admin/fees/student/{instalment.student_id}"
    try:
        delete_instalment(db, user, instalment)
    except ServiceError as exc:
        return redirect(request, back, str(exc), "error")
    db.commit()
    return redirect(request, back, "Instalment deleted.")


@router.post("/admin/fees/instalments/{instalment_id}/pay", dependencies=[Depends(verify_csrf)])
def fees_record_payment(
    request: Request, instalment_id: int, user: AdminUser, db: DbSession,
    amount: Annotated[str, Form()] = "", method: Annotated[str, Form()] = "",
    reference: Annotated[str, Form()] = "", paid_on: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
):
    instalment = db.get(FeeInstalment, instalment_id)
    if instalment is None:
        raise HTTPException(404)
    back = f"/admin/fees/student/{instalment.student_id}"
    try:
        payment = record_payment(db, user, instalment, amount=amount, method=method,
                                 reference=reference, paid_on=paid_on, note=note)
    except ServiceError as exc:
        db.rollback()
        return redirect(request, back, str(exc), "error")
    db.commit()
    return redirect(request, f"/receipts/{payment.id}", f"Payment recorded. Receipt {payment.receipt_no}.")


@router.get("/receipts/{payment_id}")
def receipt(request: Request, payment_id: int, user: CurrentUser, db: DbSession):
    payment = db.get(Payment, payment_id)
    if payment is None:
        raise HTTPException(404)
    if not user.is_admin and not (user.student is not None and user.student.id == payment.student_id):
        raise HTTPException(403, "You can only view your own receipts.")
    student = payment.instalment.student
    recorded_by = db.get(User, payment.recorded_by) if payment.recorded_by else None
    back = f"/admin/fees/student/{student.id}" if user.is_admin else "/student/fees"
    return render(request, "receipt.html", {
        "payment": payment, "student": student, "instalment": payment.instalment,
        "info": academy_info(db), "recorded_by": recorded_by, "layout": "print", "back": back,
    })
