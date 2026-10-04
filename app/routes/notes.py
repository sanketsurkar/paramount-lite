"""
app/routes/notes.py
-------------------
Course notes: Subject -> Chapter -> PDF/image files.

Staff (admin + teachers of that class):
    GET  /staff/notes?standard=7                    pick a class and subject
    GET  /staff/notes/subject/{id}                  chapters and files
    POST /staff/notes/subject/{id}/chapters         add chapter
    POST /staff/notes/chapters/{id}/rename | move | delete | upload
    POST /staff/notes/files/{id}/delete

Everyone allowed to see the subject:
    GET  /files/{id}                                the file itself (inline)
"""
from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select

from app import storage
from app.config import settings, standard_label
from app.deps import CurrentUser, DbSession, StaffUser, allowed_standards, ensure_standard_access, verify_csrf
from app.models import Chapter, Content, Subject
from app.services import (
    ServiceError,
    add_chapter,
    add_content,
    delete_chapter,
    delete_content,
    move_chapter,
    parse_standard,
    rename_chapter,
    standard_options,
    subject_applies,
    subjects_for,
)
from app.templating import redirect, render

router = APIRouter(tags=["notes"])


def _subject_for_staff(db, user, subject_id: int) -> Subject:
    subject = db.get(Subject, subject_id)
    if subject is None or subject.archived:
        raise HTTPException(404)
    ensure_standard_access(user, subject.standard)
    return subject


def _chapter_for_staff(db, user, chapter_id: int) -> Chapter:
    chapter = db.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(404)
    ensure_standard_access(user, chapter.subject.standard)
    return chapter


def _subject_url(subject_id: int) -> str:
    return f"/staff/notes/subject/{subject_id}"


@router.get("/staff/notes")
def notes_index(request: Request, user: StaffUser, db: DbSession, standard: str = ""):
    allowed = allowed_standards(user)
    options = standard_options(allowed)
    selected = parse_standard(standard) if standard != "" else (options[0][0] if options else None)
    subjects = []
    if selected is not None:
        ensure_standard_access(user, selected)
        for subject in subjects_for(db, selected):
            files = db.scalar(select(func.count(Content.id)).join(Chapter, Content.chapter_id == Chapter.id)
                              .where(Chapter.subject_id == subject.id)) or 0
            subjects.append({"subject": subject, "chapters": len(subject.chapters), "files": files})
    return render(request, "staff/notes_index.html",
                  {"class_options": options, "selected": selected, "subjects": subjects})


@router.get("/staff/notes/subject/{subject_id}")
def notes_subject(request: Request, subject_id: int, user: StaffUser, db: DbSession):
    subject = _subject_for_staff(db, user, subject_id)
    return render(request, "staff/notes_subject.html", {
        "subject": subject, "max_mb": settings.max_upload_bytes // (1024 * 1024),
    })


@router.post("/staff/notes/subject/{subject_id}/chapters", dependencies=[Depends(verify_csrf)])
def chapter_add(request: Request, subject_id: int, user: StaffUser, db: DbSession,
                title: Annotated[str, Form()] = ""):
    subject = _subject_for_staff(db, user, subject_id)
    try:
        add_chapter(db, user, subject, title)
    except ServiceError as exc:
        return redirect(request, _subject_url(subject.id), str(exc), "error")
    db.commit()
    return redirect(request, _subject_url(subject.id), "Chapter added.")


@router.post("/staff/notes/chapters/{chapter_id}/rename", dependencies=[Depends(verify_csrf)])
def chapter_rename(request: Request, chapter_id: int, user: StaffUser, db: DbSession,
                   title: Annotated[str, Form()] = ""):
    chapter = _chapter_for_staff(db, user, chapter_id)
    try:
        rename_chapter(db, user, chapter, title)
    except ServiceError as exc:
        return redirect(request, _subject_url(chapter.subject_id), str(exc), "error")
    db.commit()
    return redirect(request, _subject_url(chapter.subject_id), "Chapter renamed.")


@router.post("/staff/notes/chapters/{chapter_id}/move", dependencies=[Depends(verify_csrf)])
def chapter_move(request: Request, chapter_id: int, user: StaffUser, db: DbSession,
                 direction: Annotated[str, Form()] = "up"):
    chapter = _chapter_for_staff(db, user, chapter_id)
    move_chapter(db, chapter, direction)
    db.commit()
    return redirect(request, _subject_url(chapter.subject_id))


@router.post("/staff/notes/chapters/{chapter_id}/delete", dependencies=[Depends(verify_csrf)])
def chapter_delete(request: Request, chapter_id: int, user: StaffUser, db: DbSession):
    chapter = _chapter_for_staff(db, user, chapter_id)
    subject_id = chapter.subject_id
    keys = delete_chapter(db, user, chapter)
    db.commit()
    for key in keys:              # remove files only after the database change succeeded
        storage.delete(key)
    return redirect(request, _subject_url(subject_id), "Chapter deleted.")


@router.post("/staff/notes/chapters/{chapter_id}/upload", dependencies=[Depends(verify_csrf)])
def content_upload(request: Request, chapter_id: int, user: StaffUser, db: DbSession,
                   file: Annotated[UploadFile, File()], title: Annotated[str, Form()] = ""):
    chapter = _chapter_for_staff(db, user, chapter_id)
    data = file.file.read(settings.max_upload_bytes + 1)   # read at most limit + 1 byte
    try:
        add_content(db, user, chapter, title, data, file.filename or "file")
    except ServiceError as exc:
        return redirect(request, _subject_url(chapter.subject_id), str(exc), "error")
    db.commit()
    return redirect(request, _subject_url(chapter.subject_id), "File uploaded.")


@router.post("/staff/notes/files/{content_id}/delete", dependencies=[Depends(verify_csrf)])
def content_delete(request: Request, content_id: int, user: StaffUser, db: DbSession):
    content = db.get(Content, content_id)
    if content is None:
        raise HTTPException(404)
    subject_id = content.chapter.subject_id
    ensure_standard_access(user, content.chapter.subject.standard)
    key = delete_content(db, user, content)
    db.commit()
    storage.delete(key)
    return redirect(request, _subject_url(subject_id), "File deleted.")


@router.get("/files/{content_id}")
def serve_file(content_id: int, user: CurrentUser, db: DbSession, download: int = 0):
    """Stream a note file after checking the user may see that subject."""
    content = db.get(Content, content_id)
    if content is None:
        raise HTTPException(404)
    subject = content.chapter.subject
    if user.is_student:
        student = user.student
        if (student is None or subject.archived or subject.standard != student.standard
                or not subject_applies(subject, student)):
            raise HTTPException(403, "This file isn't part of your class.")
    else:
        ensure_standard_access(user, subject.standard)
    try:
        data = storage.load(content.storage_key)
    except storage.StorageError as exc:
        raise HTTPException(404, str(exc)) from exc
    # HTTP headers must be plain ASCII: send an ASCII fallback plus the real
    # (UTF-8) name in filename* so Hindi/emoji file names still work.
    ascii_name = content.original_filename.encode("ascii", "ignore").decode().replace('"', "") or "file"
    disposition = "attachment" if download else "inline"
    return Response(data, media_type=content.mime, headers={
        "Content-Disposition": (f'{disposition}; filename="{ascii_name}"; '
                                f"filename*=UTF-8''{quote(content.original_filename)}"),
        "Cache-Control": "private, max-age=3600",
    })
