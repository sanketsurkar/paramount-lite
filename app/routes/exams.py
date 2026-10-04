"""
app/routes/exams.py
-------------------
Exams and the spreadsheet-style marks grid (admin + teachers of that class).

    GET  /staff/exams?standard=7          list + "New exam" form for the class
    POST /staff/exams                     create (draft)
    GET  /staff/exams/{id}                marks grid
    POST /staff/exams/{id}/marks          JSON autosave from the grid
    POST /staff/exams/{id}/edit           change name/date/subjects (draft only)
    POST /staff/exams/{id}/delete         delete (draft only)
    POST /staff/exams/{id}/publish        admin only
    POST /staff/exams/{id}/unpublish      admin only
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import today_ist
from app.deps import DbSession, StaffUser, allowed_standards, ensure_standard_access, verify_csrf
from app.models import Exam, Result
from app.services import (
    ServiceError,
    create_exam,
    delete_exam,
    parse_standard,
    save_marks,
    set_exam_published,
    standard_options,
    subjects_for,
    summarise_exam,
    update_exam,
)
from app.templating import redirect, render

router = APIRouter(prefix="/staff/exams", tags=["exams"])


def _exam_for_staff(db, user, exam_id: int) -> Exam:
    exam = db.get(Exam, exam_id)
    if exam is None:
        raise HTTPException(404)
    ensure_standard_access(user, exam.standard)
    return exam


async def _subject_max(request: Request) -> tuple[dict[str, Any], dict[int, Any]]:
    """Read the exam form: checked subject_ids plus their max_<id> fields."""
    form = await request.form()
    subject_max: dict[int, Any] = {}
    for raw_id in form.getlist("subject_ids"):
        try:
            subject_id = int(raw_id)
        except ValueError:
            continue
        subject_max[subject_id] = form.get(f"max_{subject_id}") or "100"
    return dict(form), subject_max


@router.get("")
def exams_list(request: Request, user: StaffUser, db: DbSession, standard: str = ""):
    allowed = allowed_standards(user)
    options = standard_options(allowed)
    selected = parse_standard(standard) if standard != "" else None
    query = select(Exam).order_by(Exam.exam_date.desc(), Exam.id.desc())
    if selected is not None:
        ensure_standard_access(user, selected)
        query = query.where(Exam.standard == selected)
    elif allowed is not None:
        query = query.where(Exam.standard.in_(allowed or [-99]))
    exams = db.scalars(query.limit(200)).all()
    return render(request, "staff/exams.html", {
        "exams": exams, "class_options": options, "selected": selected,
        "class_subjects": subjects_for(db, selected) if selected is not None else [],
        "today": today_ist().isoformat(),
    })


@router.post("", dependencies=[Depends(verify_csrf)])
async def exams_create(request: Request, user: StaffUser, db: DbSession):
    form, subject_max = await _subject_max(request)
    standard = parse_standard(form.get("standard"))
    if standard is None:
        return redirect(request, "/staff/exams", "Choose a class first.", "error")
    ensure_standard_access(user, standard)
    try:
        exam = create_exam(db, user, standard=standard, name=form.get("name"),
                           exam_date=form.get("exam_date"), subject_max=subject_max)
    except ServiceError as exc:
        return redirect(request, f"/staff/exams?standard={standard}", str(exc), "error")
    db.commit()
    return redirect(request, f"/staff/exams/{exam.id}", "Exam created. Enter marks below - they save automatically.")


@router.get("/{exam_id}")
def exam_detail(request: Request, exam_id: int, user: StaffUser, db: DbSession):
    exam = _exam_for_staff(db, user, exam_id)
    summary = summarise_exam(db, exam)
    used_subjects = set(db.scalars(select(Result.subject_id).where(Result.exam_id == exam.id).distinct()))
    current_max = {es.subject_id: es.max_marks for es in exam.subjects}
    return render(request, "staff/exam_detail.html", {
        "exam": exam, "summary": summary, "class_subjects": subjects_for(db, exam.standard),
        "current_max": current_max, "used_subjects": used_subjects,
        "can_delete": not exam.is_published and (user.is_admin or exam.created_by == user.id),
    })


@router.post("/{exam_id}/marks", dependencies=[Depends(verify_csrf)])
def exam_save_marks(exam_id: int, user: StaffUser, db: DbSession,
                    payload: Annotated[dict, Body()]):
    exam = _exam_for_staff(db, user, exam_id)
    cells = payload.get("cells") or []
    remarks = payload.get("remarks") or []
    if not isinstance(cells, list) or not isinstance(remarks, list):
        return JSONResponse({"error": "Bad data."}, status_code=400)
    try:
        outcome = save_marks(db, user, exam, cells, remarks)
    except ServiceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)
    db.commit()
    return outcome


@router.post("/{exam_id}/edit", dependencies=[Depends(verify_csrf)])
async def exam_edit(request: Request, exam_id: int, user: StaffUser, db: DbSession):
    exam = _exam_for_staff(db, user, exam_id)
    form, subject_max = await _subject_max(request)
    try:
        update_exam(db, user, exam, name=form.get("name"), exam_date=form.get("exam_date"),
                    subject_max=subject_max)
    except ServiceError as exc:
        db.rollback()
        return redirect(request, f"/staff/exams/{exam.id}", str(exc), "error")
    db.commit()
    return redirect(request, f"/staff/exams/{exam.id}", "Exam updated.")


@router.post("/{exam_id}/delete", dependencies=[Depends(verify_csrf)])
def exam_delete(request: Request, exam_id: int, user: StaffUser, db: DbSession):
    exam = _exam_for_staff(db, user, exam_id)
    if not user.is_admin and exam.created_by != user.id:
        raise HTTPException(403, "Only the admin or the teacher who created this exam can delete it.")
    standard = exam.standard
    try:
        delete_exam(db, user, exam)
    except ServiceError as exc:
        return redirect(request, f"/staff/exams/{exam_id}", str(exc), "error")
    db.commit()
    return redirect(request, f"/staff/exams?standard={standard}", "Exam deleted.")


@router.post("/{exam_id}/{action}", dependencies=[Depends(verify_csrf)])
def exam_publish(request: Request, exam_id: int, action: str, user: StaffUser, db: DbSession):
    if action not in ("publish", "unpublish"):
        raise HTTPException(404)
    if not user.is_admin:
        raise HTTPException(403, "Only the admin can publish or unpublish results.")
    exam = _exam_for_staff(db, user, exam_id)
    set_exam_published(db, user, exam, action == "publish")
    db.commit()
    message = "Results published - students can see them now." if action == "publish" \
        else "Exam unpublished - students can no longer see it."
    return redirect(request, f"/staff/exams/{exam.id}", message)
