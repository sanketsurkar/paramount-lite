"""
app/routes/admin_people.py
--------------------------
Admin: students, teachers, passwords, Excel/CSV import and year-end Promote.

    POST /admin/students                         add a student
    POST /admin/students/{id}/edit               edit a student
    POST /admin/users/{id}/reset-password        new one-time password
    POST /admin/users/{id}/toggle-active         deactivate / reactivate
    GET  /admin/teachers                         teacher list
    POST /admin/teachers                         add a teacher
    POST /admin/teachers/{id}/edit               edit a teacher
    GET  /admin/import                           import page
    POST /admin/import                           upload .xlsx/.csv
    GET  /admin/import/template/{kind}.xlsx      example file
    GET  /admin/credentials/{token}              one-time passwords to hand out
    GET  /admin/promote, POST /admin/promote     year-end promotion
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from app.config import STREAM_STANDARDS, STREAMS, settings, standard_label
from app.deps import AdminUser, DbSession, verify_csrf
from app.models import Student, Teacher, User
from app.services import (
    ServiceError,
    build_student_form,
    create_student,
    create_teacher,
    get_credentials,
    import_people,
    import_template,
    parse_standards_list,
    promote_all,
    promotion_preview,
    read_table,
    reset_password,
    set_user_active,
    standard_options,
    stash_credentials,
    update_student,
    update_teacher,
)
from app.templating import redirect, render

router = APIRouter(prefix="/admin", tags=["admin-people"])


def _show_credentials(request: Request, title: str, created: list[dict], errors: list[dict] | None = None,
                      back: str = "/staff/students"):
    token = stash_credentials({"title": title, "created": created, "errors": errors or [], "back": back})
    return redirect(request, f"/admin/credentials/{token}")


@router.get("/credentials/{token}")
def credentials(request: Request, token: str, user: AdminUser):
    payload = get_credentials(token)
    if payload is None:
        return redirect(request, "/admin", "That list has expired (it's kept for 15 minutes). "
                                           "Reset the password again if needed.", "info")
    return render(request, "admin/credentials.html", payload,
                  headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Students
# ---------------------------------------------------------------------------
@router.post("/students", dependencies=[Depends(verify_csrf)])
def student_create(
    request: Request, user: AdminUser, db: DbSession,
    full_name: Annotated[str, Form()] = "", standard: Annotated[str, Form()] = "",
    stream: Annotated[str, Form()] = "", father_name: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "", dob: Annotated[str, Form()] = "",
    admission_year: Annotated[str, Form()] = "",
):
    try:
        form = build_student_form(full_name=full_name, standard=standard, stream=stream,
                                  father_name=father_name, phone=phone, dob=dob,
                                  admission_year=admission_year)
        student, otp = create_student(db, user, form)
    except ServiceError as exc:
        return redirect(request, "/staff/students", str(exc), "error")
    db.commit()
    return _show_credentials(request, "Student added", [{
        "name": form.full_name, "login_id": student.user.login_id, "otp": otp,
        "detail": standard_label(form.standard)}], back=f"/staff/students/{student.id}")


@router.post("/students/{student_id}/edit", dependencies=[Depends(verify_csrf)])
def student_edit(
    request: Request, student_id: int, user: AdminUser, db: DbSession,
    full_name: Annotated[str, Form()] = "", standard: Annotated[str, Form()] = "",
    stream: Annotated[str, Form()] = "", father_name: Annotated[str, Form()] = "",
    phone: Annotated[str, Form()] = "", dob: Annotated[str, Form()] = "",
):
    student = db.get(Student, student_id)
    if student is None:
        raise HTTPException(404)
    back = f"/staff/students/{student.id}"
    try:
        form = build_student_form(full_name=full_name, standard=standard, stream=stream,
                                  father_name=father_name, phone=phone, dob=dob,
                                  admission_year=student.admission_year)
        update_student(db, user, student, form)
    except ServiceError as exc:
        return redirect(request, back, str(exc), "error")
    db.commit()
    return redirect(request, back, "Student details saved.")


# ---------------------------------------------------------------------------
# Passwords and status (students and teachers)
# ---------------------------------------------------------------------------
def _person_page(user: User) -> str:
    if user.student is not None:
        return f"/staff/students/{user.student.id}"
    return "/admin/teachers"


@router.post("/users/{user_id}/reset-password", dependencies=[Depends(verify_csrf)])
def user_reset_password(request: Request, user_id: int, user: AdminUser, db: DbSession):
    person = db.get(User, user_id)
    if person is None or person.id == user.id:
        raise HTTPException(404)
    otp = reset_password(db, user, person)
    db.commit()
    detail = standard_label(person.student.standard) if person.student else person.role.title()
    return _show_credentials(request, "Password reset", [{
        "name": person.full_name, "login_id": person.login_id, "otp": otp, "detail": detail}],
        back=_person_page(person))


@router.post("/users/{user_id}/toggle-active", dependencies=[Depends(verify_csrf)])
def user_toggle_active(request: Request, user_id: int, user: AdminUser, db: DbSession):
    person = db.get(User, user_id)
    if person is None:
        raise HTTPException(404)
    try:
        set_user_active(db, user, person, not person.is_active)
    except ServiceError as exc:
        return redirect(request, _person_page(person), str(exc), "error")
    db.commit()
    state = "reactivated" if person.is_active else "deactivated"
    return redirect(request, _person_page(person), f"{person.full_name} {state}.")


# ---------------------------------------------------------------------------
# Teachers
# ---------------------------------------------------------------------------
@router.get("/teachers")
def teachers_page(request: Request, user: AdminUser, db: DbSession):
    teachers = db.scalars(select(Teacher).join(User, Teacher.user_id == User.id)
                          .order_by(User.is_active.desc(), User.full_name)).all()
    return render(request, "admin/teachers.html", {
        "teachers": teachers, "class_options": standard_options(None)})


@router.post("/teachers", dependencies=[Depends(verify_csrf)])
def teacher_create(
    request: Request, user: AdminUser, db: DbSession,
    full_name: Annotated[str, Form()] = "", phone: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "", standards: Annotated[list[str] | None, Form()] = None,
):
    try:
        codes = parse_standards_list(standards or [])
        teacher, otp = create_teacher(db, user, full_name=full_name, phone=phone, email=email, standards=codes)
    except ServiceError as exc:
        return redirect(request, "/admin/teachers", str(exc), "error")
    db.commit()
    return _show_credentials(request, "Teacher added", [{
        "name": teacher.user.full_name, "login_id": teacher.user.login_id, "otp": otp,
        "detail": ", ".join(standard_label(c) for c in codes) or "No classes yet"}], back="/admin/teachers")


@router.post("/teachers/{teacher_id}/edit", dependencies=[Depends(verify_csrf)])
def teacher_edit(
    request: Request, teacher_id: int, user: AdminUser, db: DbSession,
    full_name: Annotated[str, Form()] = "", phone: Annotated[str, Form()] = "",
    email: Annotated[str, Form()] = "", standards: Annotated[list[str] | None, Form()] = None,
):
    teacher = db.get(Teacher, teacher_id)
    if teacher is None:
        raise HTTPException(404)
    try:
        update_teacher(db, user, teacher, full_name=full_name, phone=phone, email=email,
                       standards=parse_standards_list(standards or []))
    except ServiceError as exc:
        db.rollback()
        return redirect(request, "/admin/teachers", str(exc), "error")
    db.commit()
    return redirect(request, "/admin/teachers", "Teacher details saved.")


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
@router.get("/import")
def import_page(request: Request, user: AdminUser, kind: str = "students"):
    kind = "teachers" if kind == "teachers" else "students"
    return render(request, "admin/import.html", {"kind": kind})


@router.post("/import", dependencies=[Depends(verify_csrf)])
def import_upload(request: Request, user: AdminUser, db: DbSession,
                  file: Annotated[UploadFile, File()], kind: Annotated[str, Form()] = "students"):
    kind = "teachers" if kind == "teachers" else "students"
    data = file.file.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        return redirect(request, f"/admin/import?kind={kind}", "The file is too large.", "error")
    try:
        outcome = import_people(db, user, kind, read_table(file.filename or "", data))
    except ServiceError as exc:
        db.rollback()
        return redirect(request, f"/admin/import?kind={kind}", str(exc), "error")
    db.commit()
    title = f"Imported {len(outcome['created'])} {kind}"
    back = "/staff/students" if kind == "students" else "/admin/teachers"
    return _show_credentials(request, title, outcome["created"], outcome["errors"], back=back)


@router.get("/import/template/{kind}.xlsx")
def import_template_download(kind: str, user: AdminUser):
    if kind not in ("students", "teachers"):
        raise HTTPException(404)
    return Response(
        import_template(kind),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="paramount-{kind}-template.xlsx"'},
    )


# ---------------------------------------------------------------------------
# Promote
# ---------------------------------------------------------------------------
@router.get("/promote")
def promote_page(request: Request, user: AdminUser, db: DbSession):
    return render(request, "admin/promote.html", {"preview": promotion_preview(db)})


@router.post("/promote", dependencies=[Depends(verify_csrf)])
def promote_submit(request: Request, user: AdminUser, db: DbSession,
                   confirm: Annotated[str, Form()] = ""):
    if confirm.strip() != "PROMOTE":
        return redirect(request, "/admin/promote", "Type PROMOTE in capital letters to confirm.", "error")
    outcome = promote_all(db, user)
    db.commit()
    return redirect(request, "/admin/promote",
                    f"Done: {outcome['promoted']} students moved up, {outcome['passed_out']} marked passed out.")
