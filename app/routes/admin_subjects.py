"""
app/routes/admin_subjects.py
----------------------------
Admin: subjects per class.

    GET  /admin/subjects?standard=7
    POST /admin/subjects                       add (to several classes at once)
    POST /admin/subjects/{id}/edit             rename / colour / icon / stream
    POST /admin/subjects/{id}/move             up / down
    POST /admin/subjects/{id}/copy             copy to other classes
    POST /admin/subjects/{id}/remove           delete if unused, otherwise archive
    POST /admin/subjects/{id}/restore          un-archive
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from app.config import STREAM_STANDARDS, STREAMS, standard_label
from app.deps import AdminUser, DbSession, verify_csrf
from app.models import Subject
from app.services import (
    ServiceError,
    add_subject,
    copy_subject,
    move_subject,
    parse_standard,
    parse_standards_list,
    remove_subject,
    restore_subject,
    standard_options,
    subject_usage,
    subjects_for,
    update_subject,
)
from app.templating import redirect, render

router = APIRouter(prefix="/admin/subjects", tags=["admin-subjects"])

SUGGESTED_ICONS = ["📘", "➗", "🔬", "🌍", "🏛️", "💻", "🎨", "🎵", "⚽", "🧪", "🧬", "📐",
                   "📊", "💼", "🗣️", "✍️", "🕉️", "📖"]


def _page(standard: int) -> str:
    return f"/admin/subjects?standard={standard}"


def _get(db, subject_id: int) -> Subject:
    subject = db.get(Subject, subject_id)
    if subject is None:
        raise HTTPException(404)
    return subject


@router.get("")
def subjects_page(request: Request, user: AdminUser, db: DbSession, standard: str = "1"):
    selected = parse_standard(standard)
    if selected is None:
        selected = 1
    subjects = subjects_for(db, selected, include_archived=True)
    rows = [{"subject": s, "usage": subject_usage(db, s)} for s in subjects]
    return render(request, "admin/subjects.html", {
        "selected": selected, "rows": rows, "class_options": standard_options(None),
        "streams": STREAMS, "has_streams": selected in STREAM_STANDARDS, "icons": SUGGESTED_ICONS,
        "stream_standards": STREAM_STANDARDS,
    })


@router.post("", dependencies=[Depends(verify_csrf)])
def subject_add(
    request: Request, user: AdminUser, db: DbSession,
    name: Annotated[str, Form()] = "", colour: Annotated[str, Form()] = "#1e3a8a",
    icon: Annotated[str, Form()] = "📘", stream: Annotated[str, Form()] = "",
    standards: Annotated[list[str] | None, Form()] = None, back: Annotated[str, Form()] = "1",
):
    back_std = parse_standard(back) if parse_standard(back) is not None else 1
    try:
        added, skipped = add_subject(db, user, name=name, colour=colour, icon=icon,
                                     standards=parse_standards_list(standards or []), stream=stream)
    except ServiceError as exc:
        db.rollback()
        return redirect(request, _page(back_std), str(exc), "error")
    db.commit()
    message = f"Added to {len(added)} class{'es' if len(added) != 1 else ''}."
    if skipped:
        message += f" Already existed in: {', '.join(skipped)}."
    return redirect(request, _page(back_std), message, "success" if added else "info")


@router.post("/{subject_id}/edit", dependencies=[Depends(verify_csrf)])
def subject_edit(
    request: Request, subject_id: int, user: AdminUser, db: DbSession,
    name: Annotated[str, Form()] = "", colour: Annotated[str, Form()] = "#1e3a8a",
    icon: Annotated[str, Form()] = "📘", stream: Annotated[str, Form()] = "",
):
    subject = _get(db, subject_id)
    try:
        update_subject(db, user, subject, name=name, colour=colour, icon=icon, stream=stream)
    except ServiceError as exc:
        db.rollback()
        return redirect(request, _page(subject.standard), str(exc), "error")
    db.commit()
    return redirect(request, _page(subject.standard), "Subject saved.")


@router.post("/{subject_id}/move", dependencies=[Depends(verify_csrf)])
def subject_move(request: Request, subject_id: int, user: AdminUser, db: DbSession,
                 direction: Annotated[str, Form()] = "up"):
    subject = _get(db, subject_id)
    move_subject(db, user, subject, direction)
    db.commit()
    return redirect(request, _page(subject.standard))


@router.post("/{subject_id}/copy", dependencies=[Depends(verify_csrf)])
def subject_copy(request: Request, subject_id: int, user: AdminUser, db: DbSession,
                 standards: Annotated[list[str] | None, Form()] = None):
    subject = _get(db, subject_id)
    try:
        added, skipped = copy_subject(db, user, subject, parse_standards_list(standards or []))
    except ServiceError as exc:
        db.rollback()
        return redirect(request, _page(subject.standard), str(exc), "error")
    db.commit()
    message = f"Copied to: {', '.join(added)}." if added else "Nothing copied."
    if skipped:
        message += f" Already existed in: {', '.join(skipped)}."
    return redirect(request, _page(subject.standard), message, "success" if added else "info")


@router.post("/{subject_id}/remove", dependencies=[Depends(verify_csrf)])
def subject_remove(request: Request, subject_id: int, user: AdminUser, db: DbSession):
    subject = _get(db, subject_id)
    standard, name = subject.standard, subject.name
    outcome = remove_subject(db, user, subject)
    db.commit()
    if outcome == "archived":
        return redirect(request, _page(standard),
                        f"{name} has notes or marks, so it was archived (hidden, old results kept).", "info")
    return redirect(request, _page(standard), f"{name} deleted.")


@router.post("/{subject_id}/restore", dependencies=[Depends(verify_csrf)])
def subject_restore(request: Request, subject_id: int, user: AdminUser, db: DbSession):
    subject = _get(db, subject_id)
    if subject.archived:
        restore_subject(db, user, subject)
        db.commit()
    return redirect(request, _page(subject.standard), f"{subject.name} restored.")
