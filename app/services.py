"""
app/services.py
---------------
Business logic shared by the routes - the "service layer" you would have in
a .NET app. Routes stay thin: read the form -> call a service -> render or
redirect.

Rules followed by every function here:
* Raise ServiceError("message") for problems the user can fix; routes show
  the message as a toast.
* Never commit. The route commits once at the end (one unit of work), so a
  failure half-way leaves the database unchanged.
* Anything that changes marks, fees, passwords, subjects or classes writes
  an audit_log row via audit().
"""
from __future__ import annotations

import csv
import io
import json
import re
import secrets
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import storage
from app.config import (
    DEFAULT_SETTINGS,
    MAX_STANDARD,
    MIN_STANDARD,
    PAYMENT_METHODS,
    RECEIPT_PREFIX,
    ROLE_ADMIN,
    ROLE_STUDENT,
    ROLE_TEACHER,
    STANDARDS,
    STREAM_STANDARDS,
    STREAMS,
    STUDENT_ID_PREFIX,
    TEACHER_ID_PREFIX,
    current_academic_year,
    settings,
    standard_label,
    today_ist,
    utcnow,
)
from app.models import (
    EXAM_DRAFT,
    EXAM_PUBLISHED,
    FEE_DUE,
    FEE_OVERDUE,
    FEE_PAID,
    FEE_PARTIAL,
    STUDENT_ACTIVE,
    STUDENT_PASSED_OUT,
    AuditLog,
    Chapter,
    Content,
    Exam,
    ExamRemark,
    ExamSubject,
    FeeInstalment,
    Notice,
    Payment,
    Result,
    Setting,
    Student,
    Subject,
    Teacher,
    TeacherStandard,
    User,
)
from app.security import generate_otp, hash_password


class ServiceError(Exception):
    """A problem the user can fix. The message is shown to them as-is."""


# ===========================================================================
# Audit log
# ===========================================================================
def audit(
    db: Session,
    actor: User | None,
    action: str,
    entity: str,
    entity_id: Any = None,
    **details: Any,
) -> None:
    """Record who did what. `details` is stored as JSON text."""
    db.add(
        AuditLog(
            user_id=actor.id if actor else None,
            actor_login_id=actor.login_id if actor else "SYSTEM",
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            details=json.dumps(details, ensure_ascii=False, default=str) if details else None,
        )
    )


# ===========================================================================
# Settings (key/value table)
# ===========================================================================
def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row is not None else default


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


ACADEMY_KEYS = {
    "name": "academy.name",
    "tagline": "academy.tagline",
    "address": "academy.address",
    "phone": "academy.phone",
    "email": "academy.email",
    "upi_id": "upi.id",
    "upi_payee": "upi.payee",
}


def academy_info(db: Session) -> dict[str, str]:
    """Academy details for receipts and the fees page."""
    rows = {
        s.key: s.value
        for s in db.scalars(select(Setting).where(Setting.key.in_(ACADEMY_KEYS.values())))
    }
    return {short: rows.get(full, DEFAULT_SETTINGS.get(full, "")) for short, full in ACADEMY_KEYS.items()}


def leaderboard_enabled(db: Session, standard: int) -> bool:
    return get_setting(db, f"leaderboard.{standard}", "on") != "off"


# ===========================================================================
# Input cleaning (used by forms AND Excel import, so rules are identical)
# ===========================================================================
_STANDARD_WORDS = {"nursery": -2, "nur": -2, "lkg": -1, "ukg": 0}


def parse_standard(value: Any) -> int | None:
    """'Nursery' -> -2, 'UKG' -> 0, 'Class 7' / '7th' / 7 -> 7. None if invalid."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
    else:
        text = str(value).strip().lower()
        if not text:
            return None
        if text in _STANDARD_WORDS:
            return _STANDARD_WORDS[text]
        match = re.search(r"-?\d+", text)
        if not match:
            return None
        number = int(match.group())
    return number if MIN_STANDARD <= number <= MAX_STANDARD else None


def require_standard(value: Any) -> int:
    number = parse_standard(value)
    if number is None:
        raise ServiceError("Choose a valid class (Nursery, LKG, UKG or 1 to 12).")
    return number


def clean_text(value: Any, label: str, *, min_len: int = 1, max_len: int = 120, required: bool = True) -> str | None:
    """Trim and collapse spaces; enforce length."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        if required:
            raise ServiceError(f"{label} is required.")
        return None
    if len(text) < min_len:
        raise ServiceError(f"{label} must be at least {min_len} characters.")
    if len(text) > max_len:
        raise ServiceError(f"{label} must be at most {max_len} characters.")
    return text


def clean_name(value: Any, label: str = "Name", max_len: int = 120) -> str:
    return clean_text(value, label, min_len=2, max_len=max_len)  # type: ignore[return-value]


def clean_phone(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(int(value)) if isinstance(value, float) else str(value).strip()
    cleaned = re.sub(r"[^\d+]", "", text)
    digits = re.sub(r"\D", "", cleaned)
    if len(digits) < 10 or len(cleaned) > 15:
        raise ServiceError(f"'{text}' is not a valid phone number (10 digits or more).")
    return cleaned


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def clean_email(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if not _EMAIL_RE.match(text) or len(text) > 120:
        raise ServiceError(f"'{text}' is not a valid email address.")
    return text.lower()


def parse_date(value: Any, label: str = "Date") -> date | None:
    """Accepts date objects (Excel) or 'YYYY-MM-DD', 'DD-MM-YYYY', 'DD/MM/YYYY'."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ServiceError(f"{label} '{text}' is not a valid date (use YYYY-MM-DD or DD-MM-YYYY).")


def parse_rupees(value: Any) -> int:
    """'2,500.50' -> 250050 paise. Raises ServiceError when invalid."""
    text = str(value or "").replace(",", "").replace("₹", "").strip()
    try:
        amount = Decimal(text)
        if not amount.is_finite() or amount <= 0:
            raise ServiceError("The amount must be more than zero.")
    except InvalidOperation:
        raise ServiceError("Enter the amount in rupees, for example 2500 or 2500.50.") from None
    if amount != amount.quantize(Decimal("0.01")):
        raise ServiceError("Use at most 2 decimal places for the amount.")
    if amount > Decimal("10000000"):
        raise ServiceError("That amount is too large.")
    return int(amount * 100)


def normalise_stream(value: Any, standard: int) -> str | None:
    """Streams only exist for Class 11 and 12."""
    if standard not in STREAM_STANDARDS:
        return None
    text = str(value or "").strip().lower()
    if not text:
        return None
    for stream in STREAMS:
        if text == stream.lower():
            return stream
    raise ServiceError(f"Stream must be one of: {', '.join(STREAMS)}.")


def parse_standards_list(value: Any) -> list[int]:
    """['9','10'] or '9, 10, Nursery' -> [9, 10, -2]."""
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;/]", str(value))
    result: list[int] = []
    for item in items:
        if str(item).strip() == "":
            continue
        number = parse_standard(item)
        if number is None:
            raise ServiceError(f"'{item}' is not a valid class.")
        if number not in result:
            result.append(number)
    return sorted(result)


def standard_options(allowed: list[int] | None) -> list[tuple[int, str]]:
    """Class choices for drop-downs; None means all classes (admin)."""
    return [(code, label) for code, label in STANDARDS if allowed is None or code in allowed]


# ===========================================================================
# Login IDs
# ===========================================================================
def _next_number(db: Session, prefix: str) -> int:
    ids = db.scalars(select(User.login_id).where(User.login_id.like(prefix + "%"))).all()
    numbers = [int(i[len(prefix):]) for i in ids if i[len(prefix):].isdigit()]
    return max(numbers, default=0) + 1


def next_student_login_id(db: Session, admission_year: int) -> str:
    prefix = f"{STUDENT_ID_PREFIX}{admission_year % 100:02d}-"   # TPA26-
    return f"{prefix}{_next_number(db, prefix):04d}"


def next_teacher_login_id(db: Session) -> str:
    return f"{TEACHER_ID_PREFIX}{_next_number(db, TEACHER_ID_PREFIX):03d}"


# ===========================================================================
# One-time credentials shown to the admin after create/reset/import.
# Kept in server memory for 15 minutes (never in the cookie, never in the DB),
# so the page can be refreshed or printed without creating users twice.
# ===========================================================================
_CREDENTIALS: dict[str, tuple[float, dict]] = {}
CREDENTIALS_TTL_SECONDS = 15 * 60


def stash_credentials(payload: dict) -> str:
    now = time.monotonic()
    for key in [k for k, (expires, _) in _CREDENTIALS.items() if expires < now]:
        _CREDENTIALS.pop(key, None)
    token = secrets.token_urlsafe(16)
    _CREDENTIALS[token] = (now + CREDENTIALS_TTL_SECONDS, payload)
    return token


def get_credentials(token: str) -> dict | None:
    item = _CREDENTIALS.get(token)
    if item is None or item[0] < time.monotonic():
        _CREDENTIALS.pop(token, None)
        return None
    return item[1]


# ===========================================================================
# Students
# ===========================================================================
@dataclass
class StudentForm:
    full_name: str
    standard: int
    stream: str | None = None
    father_name: str | None = None
    phone: str | None = None
    dob: date | None = None
    admission_year: int = 0


def build_student_form(
    *,
    full_name: Any,
    standard: Any,
    stream: Any = None,
    father_name: Any = None,
    phone: Any = None,
    dob: Any = None,
    admission_year: Any = None,
) -> StudentForm:
    """Validate raw values (from a form or an Excel row). Raises ServiceError."""
    std = require_standard(standard)
    this_year = today_ist().year
    if admission_year in (None, ""):
        year = this_year
    else:
        try:
            year = int(float(str(admission_year).strip()))
        except ValueError:
            raise ServiceError(f"Admission year '{admission_year}' is not a year.") from None
        if not 2000 <= year <= this_year + 1:
            raise ServiceError(f"Admission year must be between 2000 and {this_year + 1}.")
    birth = parse_date(dob, "Date of birth")
    if birth and birth > today_ist():
        raise ServiceError("Date of birth can't be in the future.")
    return StudentForm(
        full_name=clean_name(full_name, "Student name"),
        standard=std,
        stream=normalise_stream(stream, std),
        father_name=clean_text(father_name, "Father's name", required=False),
        phone=clean_phone(phone),
        dob=birth,
        admission_year=year,
    )


def create_student(db: Session, actor: User, form: StudentForm) -> tuple[Student, str]:
    """Create user + student. Returns (student, one-time password)."""
    login_id = next_student_login_id(db, form.admission_year)
    otp = generate_otp()
    user = User(
        login_id=login_id,
        full_name=form.full_name,
        role=ROLE_STUDENT,
        password_hash=hash_password(otp),
        must_change_password=True,
    )
    student = Student(
        user=user,
        standard=form.standard,
        stream=form.stream,
        admission_year=form.admission_year,
        father_name=form.father_name,
        phone=form.phone,
        dob=form.dob,
    )
    db.add_all([user, student])
    db.flush()      # assigns IDs so the next login ID calculation sees this one
    audit(db, actor, "user.create", "student", login_id, name=form.full_name,
          standard=standard_label(form.standard))
    return student, otp


def update_student(db: Session, actor: User, student: Student, form: StudentForm) -> None:
    changes = {}
    for attr, new in (
        ("standard", form.standard), ("stream", form.stream), ("father_name", form.father_name),
        ("phone", form.phone), ("dob", form.dob),
    ):
        if getattr(student, attr) != new:
            changes[attr] = [getattr(student, attr), new]
            setattr(student, attr, new)
    if student.user.full_name != form.full_name:
        changes["full_name"] = [student.user.full_name, form.full_name]
        student.user.full_name = form.full_name
    if changes:
        audit(db, actor, "user.update", "student", student.user.login_id, changes=changes)


def active_students_query(standard: int | None = None):
    """SELECT active students (optionally one class), ordered by name."""
    query = (
        select(Student)
        .join(User, Student.user_id == User.id)
        .where(Student.status == STUDENT_ACTIVE, User.is_active.is_(True))
    )
    if standard is not None:
        query = query.where(Student.standard == standard)
    return query.order_by(User.full_name)


# ===========================================================================
# Teachers
# ===========================================================================
def create_teacher(
    db: Session, actor: User, *, full_name: Any, phone: Any, email: Any, standards: list[int]
) -> tuple[Teacher, str]:
    name = clean_name(full_name, "Teacher name")
    phone_clean, email_clean = clean_phone(phone), clean_email(email)
    login_id = next_teacher_login_id(db)
    otp = generate_otp()
    user = User(login_id=login_id, full_name=name, role=ROLE_TEACHER,
                password_hash=hash_password(otp), must_change_password=True)
    teacher = Teacher(user=user, phone=phone_clean, email=email_clean)
    teacher.standards = [TeacherStandard(standard=s) for s in sorted(set(standards))]
    db.add_all([user, teacher])
    db.flush()
    audit(db, actor, "user.create", "teacher", login_id, name=name,
          classes=[standard_label(s) for s in standards])
    return teacher, otp


def update_teacher(
    db: Session, actor: User, teacher: Teacher, *, full_name: Any, phone: Any, email: Any,
    standards: list[int],
) -> None:
    teacher.user.full_name = clean_name(full_name, "Teacher name")
    teacher.phone = clean_phone(phone)
    teacher.email = clean_email(email)
    # Change class assignments by difference (avoids unique-constraint clashes).
    wanted = set(standards)
    for link in list(teacher.standards):
        if link.standard not in wanted:
            teacher.standards.remove(link)          # delete-orphan removes the row
    have = {link.standard for link in teacher.standards}
    for code in sorted(wanted - have):
        teacher.standards.append(TeacherStandard(standard=code))
    audit(db, actor, "user.update", "teacher", teacher.user.login_id,
          classes=[standard_label(s) for s in sorted(wanted)])


# ===========================================================================
# Passwords and account status (admin only)
# ===========================================================================
def reset_password(db: Session, actor: User, user: User) -> str:
    otp = generate_otp()
    user.password_hash = hash_password(otp)
    user.must_change_password = True
    user.session_version += 1        # logs the user out everywhere
    audit(db, actor, "password.reset", user.role, user.login_id)
    return otp


def set_user_active(db: Session, actor: User, user: User, active: bool) -> None:
    if user.role == ROLE_ADMIN:
        raise ServiceError("The admin account can't be deactivated.")
    user.is_active = active
    if not active:
        user.session_version += 1
    audit(db, actor, "user.activate" if active else "user.deactivate", user.role, user.login_id)


# ===========================================================================
# Subjects
# ===========================================================================
_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _clean_subject_fields(name: Any, colour: Any, icon: Any) -> tuple[str, str, str]:
    name_clean = clean_name(name, "Subject name", max_len=60)
    colour_clean = str(colour or "#1e3a8a").strip()
    if not _HEX_RE.match(colour_clean):
        raise ServiceError("Pick a colour.")
    icon_clean = str(icon or "📘").strip() or "📘"
    if len(icon_clean) > 8:
        raise ServiceError("Use a single emoji as the icon.")
    return name_clean, colour_clean, icon_clean


def subjects_for(db: Session, standard: int, include_archived: bool = False) -> list[Subject]:
    query = select(Subject).where(Subject.standard == standard)
    if not include_archived:
        query = query.where(Subject.archived.is_(False))
    return list(db.scalars(query.order_by(Subject.archived, Subject.position, Subject.id)))


def subject_applies(subject: Subject, student: Student) -> bool:
    """Stream subjects (11-12) only apply to students of that stream."""
    return subject.stream is None or subject.stream == student.stream


def subjects_for_student(db: Session, student: Student) -> list[Subject]:
    return [s for s in subjects_for(db, student.standard) if subject_applies(s, student)]


def _find_subject(db: Session, standard: int, name: str, exclude_id: int | None = None) -> Subject | None:
    query = select(Subject).where(Subject.standard == standard, func.lower(Subject.name) == name.lower())
    if exclude_id is not None:
        query = query.where(Subject.id != exclude_id)
    return db.scalar(query)


def _next_subject_position(db: Session, standard: int) -> int:
    current = db.scalar(select(func.max(Subject.position)).where(Subject.standard == standard))
    return (current if current is not None else -1) + 1


def add_subject(
    db: Session, actor: User, *, name: Any, colour: Any, icon: Any, standards: list[int], stream: Any
) -> tuple[list[str], list[str]]:
    """Add one subject to several classes. Returns (added class labels, skipped labels)."""
    name_clean, colour_clean, icon_clean = _clean_subject_fields(name, colour, icon)
    if not standards:
        raise ServiceError("Choose at least one class.")
    added, skipped = [], []
    for std in standards:
        if _find_subject(db, std, name_clean):
            skipped.append(standard_label(std))
            continue
        stream_clean = normalise_stream(stream, std) if std in STREAM_STANDARDS else None
        db.add(Subject(standard=std, name=name_clean, colour=colour_clean, icon=icon_clean,
                       stream=stream_clean, position=_next_subject_position(db, std)))
        db.flush()
        added.append(standard_label(std))
    if added:
        audit(db, actor, "subject.add", "subject", name_clean, classes=added)
    return added, skipped


def update_subject(db: Session, actor: User, subject: Subject, *, name: Any, colour: Any, icon: Any,
                   stream: Any) -> None:
    name_clean, colour_clean, icon_clean = _clean_subject_fields(name, colour, icon)
    if _find_subject(db, subject.standard, name_clean, exclude_id=subject.id):
        raise ServiceError(f"{standard_label(subject.standard)} already has a subject called {name_clean}.")
    old = subject.name
    subject.name, subject.colour, subject.icon = name_clean, colour_clean, icon_clean
    subject.stream = normalise_stream(stream, subject.standard)
    audit(db, actor, "subject.update", "subject", subject.id, old_name=old, name=name_clean,
          standard=standard_label(subject.standard))


def subject_usage(db: Session, subject: Subject) -> dict[str, int]:
    notes = db.scalar(
        select(func.count(Content.id)).join(Chapter, Content.chapter_id == Chapter.id)
        .where(Chapter.subject_id == subject.id)
    ) or 0
    exams = db.scalar(select(func.count(ExamSubject.id)).where(ExamSubject.subject_id == subject.id)) or 0
    marks = db.scalar(select(func.count(Result.id)).where(Result.subject_id == subject.id)) or 0
    return {"notes": notes, "exams": exams, "marks": marks}


def remove_subject(db: Session, actor: User, subject: Subject) -> str:
    """Delete if unused; archive if it has notes or marks. Returns 'deleted' or 'archived'."""
    usage = subject_usage(db, subject)
    label = standard_label(subject.standard)
    if any(usage.values()):
        subject.archived = True
        subject.archived_at = utcnow()
        audit(db, actor, "subject.archive", "subject", subject.id, name=subject.name, standard=label)
        return "archived"
    audit(db, actor, "subject.delete", "subject", subject.id, name=subject.name, standard=label)
    db.delete(subject)
    return "deleted"


def restore_subject(db: Session, actor: User, subject: Subject) -> None:
    subject.archived = False
    subject.archived_at = None
    subject.position = _next_subject_position(db, subject.standard)
    audit(db, actor, "subject.restore", "subject", subject.id, name=subject.name,
          standard=standard_label(subject.standard))


def move_subject(db: Session, actor: User, subject: Subject, direction: str) -> None:
    siblings = subjects_for(db, subject.standard)
    index = next(i for i, s in enumerate(siblings) if s.id == subject.id)
    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(siblings):
        siblings[index], siblings[target] = siblings[target], siblings[index]
        for position, item in enumerate(siblings):
            item.position = position
        audit(db, actor, "subject.reorder", "subject", subject.id, name=subject.name,
              standard=standard_label(subject.standard))


def copy_subject(db: Session, actor: User, subject: Subject, standards: list[int]) -> tuple[list[str], list[str]]:
    targets = [s for s in standards if s != subject.standard]
    if not targets:
        raise ServiceError("Choose at least one other class.")
    return add_subject(db, actor, name=subject.name, colour=subject.colour, icon=subject.icon,
                       standards=targets, stream=subject.stream)


# ===========================================================================
# Course notes (chapters and files)
# ===========================================================================
def add_chapter(db: Session, actor: User, subject: Subject, title: Any) -> Chapter:
    title_clean = clean_text(title, "Chapter title", min_len=2, max_len=150)
    position = len(subject.chapters)
    chapter = Chapter(subject=subject, title=title_clean, position=position)
    db.add(chapter)
    audit(db, actor, "notes.chapter_add", "subject", subject.id, title=title_clean)
    return chapter


def rename_chapter(db: Session, actor: User, chapter: Chapter, title: Any) -> None:
    chapter.title = clean_text(title, "Chapter title", min_len=2, max_len=150)  # type: ignore[assignment]
    audit(db, actor, "notes.chapter_rename", "chapter", chapter.id, title=chapter.title)


def move_chapter(db: Session, chapter: Chapter, direction: str) -> None:
    siblings = list(chapter.subject.chapters)
    index = siblings.index(chapter)
    target = index - 1 if direction == "up" else index + 1
    if 0 <= target < len(siblings):
        siblings[index], siblings[target] = siblings[target], siblings[index]
        for position, item in enumerate(siblings):
            item.position = position


def delete_chapter(db: Session, actor: User, chapter: Chapter) -> list[str]:
    """Delete a chapter and its files. Returns storage keys to remove after commit."""
    keys = [c.storage_key for c in chapter.contents]
    audit(db, actor, "notes.chapter_delete", "chapter", chapter.id, title=chapter.title, files=len(keys))
    db.delete(chapter)
    return keys


def add_content(db: Session, actor: User, chapter: Chapter, title: Any, data: bytes, filename: str) -> Content:
    """Validate (size + real file signature) and store an uploaded PDF/image."""
    if not data:
        raise ServiceError("Choose a file to upload.")
    if len(data) > settings.max_upload_bytes:
        raise ServiceError(f"The file is larger than {settings.max_upload_bytes // (1024 * 1024)} MB.")
    detected = storage.detect_file_type(data[:16])
    if detected is None:
        raise ServiceError("Only PDF, JPG, PNG or WEBP files are allowed.")
    kind, mime, ext = detected
    safe_name = re.sub(r"[^\w.\- ]", "_", filename or "file")[:200]
    title_clean = clean_text(title, "Title", min_len=2, max_len=150, required=False) or \
        re.sub(r"\.[A-Za-z0-9]+$", "", safe_name)[:150] or "Notes"
    key = storage.new_key(ext)
    try:
        storage.save(key, data, mime)
    except storage.StorageError as exc:
        raise ServiceError(str(exc)) from exc
    content = Content(chapter=chapter, title=title_clean, kind=kind, mime=mime, size_bytes=len(data),
                      storage_key=key, original_filename=safe_name, uploaded_by=actor.id)
    db.add(content)
    audit(db, actor, "notes.upload", "chapter", chapter.id, title=title_clean, size=len(data))
    return content


def delete_content(db: Session, actor: User, content: Content) -> str:
    key = content.storage_key
    audit(db, actor, "notes.delete", "content", content.id, title=content.title)
    db.delete(content)
    return key


# ===========================================================================
# Exams, marks and results
# ===========================================================================
_MARK_RE = re.compile(r"^\d+(\.\d)?$")


def parse_mark(raw: Any, max_marks: float) -> tuple[float | None, bool]:
    """'' -> (None, False) clear | 'AB' -> (None, True) absent | '42.5' -> (42.5, False)."""
    text = str(raw or "").strip().upper()
    if text == "":
        return None, False
    if text == "AB":
        return None, True
    if not _MARK_RE.match(text):
        raise ServiceError("Enter a number (at most one decimal place) or AB for absent.")
    value = float(text)
    if value > max_marks:
        raise ServiceError(f"Marks can't be more than {max_marks:g}.")
    return value, False


@dataclass
class StudentResult:
    """One student's line in an exam: marks per subject, totals and rank."""
    student: Student
    marks: dict[int, Result] = field(default_factory=dict)
    obtained: float = 0.0
    maximum: float = 0.0
    percent: float | None = None
    rank: int | None = None
    remark: str | None = None


@dataclass
class ExamSummary:
    exam: Exam
    subjects: list[ExamSubject]
    rows: list[StudentResult]
    class_average: float | None
    ranked_count: int

    def row_for(self, student_id: int) -> StudentResult | None:
        return next((r for r in self.rows if r.student.id == student_id), None)


def exam_students(db: Session, exam: Exam) -> list[Student]:
    """Active students of the exam's class + anyone who already has marks in it."""
    in_class = db.scalars(active_students_query(exam.standard)).all()
    with_marks = db.scalars(
        select(Student).where(Student.id.in_(select(Result.student_id).where(Result.exam_id == exam.id)))
    ).all()
    merged = {s.id: s for s in in_class} | {s.id: s for s in with_marks}
    return sorted(merged.values(), key=lambda s: s.user.full_name.lower())


def summarise_exam(db: Session, exam: Exam) -> ExamSummary:
    """Totals, percentages and ranks (ties share a rank: 1, 1, 3)."""
    subjects = list(exam.subjects)
    results = db.scalars(select(Result).where(Result.exam_id == exam.id)).all()
    by_student: dict[int, dict[int, Result]] = defaultdict(dict)
    for result in results:
        by_student[result.student_id][result.subject_id] = result
    remarks = {r.student_id: r.remark for r in exam.remarks}

    rows: list[StudentResult] = []
    for student in exam_students(db, exam):
        row = StudentResult(student=student, marks=by_student.get(student.id, {}),
                            remark=remarks.get(student.id))
        entered = False
        for es in subjects:
            if not subject_applies(es.subject, student):
                continue
            row.maximum += es.max_marks
            result = row.marks.get(es.subject_id)
            if result is not None:
                entered = True
                row.obtained += 0 if result.is_absent else (result.marks or 0)
        if entered and row.maximum > 0:
            row.percent = round(row.obtained / row.maximum * 100, 2)
        rows.append(row)

    ranked = [r for r in rows if r.percent is not None]
    for row in ranked:
        row.rank = 1 + sum(1 for other in ranked if other.percent > row.percent)  # type: ignore[operator]
    average = round(sum(r.percent for r in ranked) / len(ranked), 2) if ranked else None  # type: ignore[misc]
    return ExamSummary(exam=exam, subjects=subjects, rows=rows, class_average=average,
                       ranked_count=len(ranked))


def _subject_max_from_form(db: Session, standard: int, subject_max: dict[int, Any]) -> list[tuple[Subject, float]]:
    if not subject_max:
        raise ServiceError("Choose at least one subject.")
    available = {s.id: s for s in subjects_for(db, standard)}
    chosen: list[tuple[Subject, float]] = []
    for subject_id, raw_max in subject_max.items():
        subject = available.get(subject_id)
        if subject is None:
            raise ServiceError("One of the chosen subjects doesn't belong to this class.")
        try:
            max_marks = float(raw_max)
        except (TypeError, ValueError):
            raise ServiceError(f"Enter maximum marks for {subject.name}.") from None
        if not 0 < max_marks <= 1000:
            raise ServiceError(f"Maximum marks for {subject.name} must be between 1 and 1000.")
        chosen.append((subject, max_marks))
    chosen.sort(key=lambda pair: pair[0].position)
    return chosen


def create_exam(db: Session, actor: User, *, standard: int, name: Any, exam_date: Any,
                subject_max: dict[int, Any]) -> Exam:
    when = parse_date(exam_date, "Exam date")
    if when is None:
        raise ServiceError("Exam date is required.")
    exam = Exam(standard=standard, academic_year=current_academic_year(),
                name=clean_name(name, "Exam name", max_len=100), exam_date=when,
                status=EXAM_DRAFT, created_by=actor.id)
    for position, (subject, max_marks) in enumerate(_subject_max_from_form(db, standard, subject_max)):
        exam.subjects.append(ExamSubject(subject_id=subject.id, max_marks=max_marks, position=position))
    db.add(exam)
    db.flush()
    audit(db, actor, "exam.create", "exam", exam.id, name=exam.name, standard=standard_label(standard))
    return exam


def update_exam(db: Session, actor: User, exam: Exam, *, name: Any, exam_date: Any,
                subject_max: dict[int, Any]) -> None:
    if exam.is_published:
        raise ServiceError("Unpublish the exam before changing it.")
    when = parse_date(exam_date, "Exam date")
    if when is None:
        raise ServiceError("Exam date is required.")
    chosen = _subject_max_from_form(db, exam.standard, subject_max)
    chosen_ids = {s.id for s, _ in chosen}
    highest = dict(db.execute(
        select(Result.subject_id, func.max(Result.marks)).where(Result.exam_id == exam.id)
        .group_by(Result.subject_id)
    ).all())
    for es in exam.subjects:
        if es.subject_id not in chosen_ids and es.subject_id in highest:
            raise ServiceError(f"{es.subject.name} already has marks, so it can't be removed.")
    for subject, max_marks in chosen:
        if (highest.get(subject.id) or 0) > max_marks:
            raise ServiceError(f"Some {subject.name} marks are above {max_marks:g}.")
    exam.name = clean_name(name, "Exam name", max_len=100)
    exam.exam_date = when
    current = {es.subject_id: es for es in exam.subjects}
    for es in list(exam.subjects):
        if es.subject_id not in chosen_ids:
            exam.subjects.remove(es)
    for position, (subject, max_marks) in enumerate(chosen):
        es = current.get(subject.id)
        if es is None:
            exam.subjects.append(ExamSubject(subject_id=subject.id, max_marks=max_marks, position=position))
        else:
            es.max_marks, es.position = max_marks, position
    audit(db, actor, "exam.update", "exam", exam.id, name=exam.name)


def delete_exam(db: Session, actor: User, exam: Exam) -> None:
    if exam.is_published:
        raise ServiceError("Published exams can't be deleted. Unpublish it first.")
    audit(db, actor, "exam.delete", "exam", exam.id, name=exam.name, standard=standard_label(exam.standard))
    db.delete(exam)


def set_exam_published(db: Session, actor: User, exam: Exam, publish: bool) -> None:
    exam.status = EXAM_PUBLISHED if publish else EXAM_DRAFT
    exam.published_at = utcnow() if publish else None
    audit(db, actor, "exam.publish" if publish else "exam.unpublish", "exam", exam.id, name=exam.name)


def save_marks(db: Session, actor: User, exam: Exam, cells: list[dict], remarks: list[dict]) -> dict:
    """Autosave from the marks grid. Invalid cells are reported, valid ones saved."""
    if exam.is_published:
        raise ServiceError("This exam is published. Unpublish it to change marks.")
    students = {s.id: s for s in exam_students(db, exam)}
    exam_subjects = {es.subject_id: es for es in exam.subjects}
    existing = {(r.student_id, r.subject_id): r
                for r in db.scalars(select(Result).where(Result.exam_id == exam.id))}
    saved, errors, changes = 0, [], []

    for cell in cells[:2000]:
        try:
            student_id, subject_id = int(cell["student_id"]), int(cell["subject_id"])
        except (KeyError, TypeError, ValueError):
            errors.append({"error": "Bad cell."})
            continue
        student, es = students.get(student_id), exam_subjects.get(subject_id)
        if student is None or es is None or not subject_applies(es.subject, student):
            errors.append({"student_id": student_id, "subject_id": subject_id, "error": "Not part of this exam."})
            continue
        try:
            value, absent = parse_mark(cell.get("value"), es.max_marks)
        except ServiceError as exc:
            errors.append({"student_id": student_id, "subject_id": subject_id, "error": str(exc)})
            continue
        current = existing.get((student_id, subject_id))
        before = None if current is None else ("AB" if current.is_absent else current.marks)
        if value is None and not absent:
            if current is not None:
                db.delete(current)
                existing.pop((student_id, subject_id))
        else:
            if current is None:
                current = Result(exam_id=exam.id, student_id=student_id, subject_id=subject_id)
                db.add(current)
                existing[(student_id, subject_id)] = current
            current.marks = None if absent else value
            current.is_absent = absent
            current.updated_by = actor.id
        after = "AB" if absent else value
        if before != after:
            changes.append([student.user.login_id, es.subject.name, before, after])
        saved += 1

    current_remarks = {r.student_id: r for r in exam.remarks}
    for item in remarks[:500]:
        try:
            student_id = int(item["student_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if student_id not in students:
            continue
        text = re.sub(r"\s+", " ", str(item.get("remark") or "")).strip()[:300]
        row = current_remarks.get(student_id)
        if not text and row is not None:
            db.delete(row)
        elif text and row is None:
            db.add(ExamRemark(exam_id=exam.id, student_id=student_id, remark=text, updated_by=actor.id))
        elif text and row is not None and row.remark != text:
            row.remark, row.updated_by = text, actor.id
        saved += 1

    if changes:
        audit(db, actor, "marks.update", "exam", exam.id, exam=exam.name, cells=len(changes),
              changes=changes[:25])
    return {"saved": saved, "errors": errors}


def student_exam_history(db: Session, student: Student) -> list[tuple[Exam, StudentResult, ExamSummary]]:
    """Published exams the student has marks in, oldest first."""
    exam_ids = select(Result.exam_id).where(Result.student_id == student.id).distinct()
    exams = db.scalars(
        select(Exam).where(Exam.id.in_(exam_ids), Exam.status == EXAM_PUBLISHED)
        .order_by(Exam.exam_date, Exam.id)
    ).all()
    history = []
    for exam in exams:
        summary = summarise_exam(db, exam)
        row = summary.row_for(student.id)
        if row is not None and row.percent is not None:
            history.append((exam, row, summary))
    return history


def trend_chart(history: list[tuple[Exam, StudentResult, ExamSummary]],
                width: int = 600, height: int = 220) -> dict | None:
    """Coordinates for the SVG progress chart (x = exams in order, y = %)."""
    if not history:
        return None
    left, right, top, bottom = 40, 16, 16, 34
    inner_w, inner_h = width - left - right, height - top - bottom
    count = len(history)
    dots = []
    for i, (exam, row, _summary) in enumerate(history):
        x = left + (inner_w / 2 if count == 1 else inner_w * i / (count - 1))
        y = top + inner_h * (1 - (row.percent or 0) / 100)
        dots.append({"x": round(x, 1), "y": round(y, 1), "percent": row.percent,
                     "label": exam.name, "short": exam.name[:12]})
    grid = [{"y": round(top + inner_h * (1 - p / 100), 1), "label": p} for p in (0, 25, 50, 75, 100)]
    return {"width": width, "height": height, "dots": dots, "grid": grid, "left": left,
            "right": width - right, "bottom": height - bottom,
            "points": " ".join(f"{d['x']},{d['y']}" for d in dots)}


def latest_published_exam(db: Session, standard: int) -> Exam | None:
    return db.scalar(
        select(Exam).where(Exam.standard == standard, Exam.status == EXAM_PUBLISHED)
        .order_by(Exam.exam_date.desc(), Exam.id.desc()).limit(1)
    )


def leaderboard(db: Session, standard: int) -> dict | None:
    """Ranking from the latest published exam of a class."""
    exam = latest_published_exam(db, standard)
    if exam is None:
        return None
    summary = summarise_exam(db, exam)
    ranked = sorted((r for r in summary.rows if r.rank is not None),
                    key=lambda r: (r.rank, r.student.user.full_name.lower()))
    return {"exam": exam, "ranked": ranked, "average": summary.class_average}


# ===========================================================================
# Fees
# ===========================================================================
def create_instalments(db: Session, actor: User, students: list[Student], *, title: Any,
                       amount: Any, due_date: Any) -> int:
    title_clean = clean_text(title, "Instalment title", min_len=2, max_len=80)
    amount_paise = parse_rupees(amount)
    due = parse_date(due_date, "Due date")
    if due is None:
        raise ServiceError("Due date is required.")
    if not students:
        raise ServiceError("No active students match - nothing was added.")
    year = current_academic_year()
    for student in students:
        db.add(FeeInstalment(student_id=student.id, academic_year=year, title=title_clean,
                             amount_paise=amount_paise, due_date=due, created_by=actor.id))
    audit(db, actor, "fees.instalment_add", "fees", None, title=title_clean,
          amount_paise=amount_paise, due=due, students=len(students))
    return len(students)


def fee_totals(instalments: list[FeeInstalment]) -> dict[str, Any]:
    total = sum(i.amount_paise for i in instalments)
    paid = sum(i.paid_amount_paise for i in instalments)
    statuses = [i.status for i in instalments]
    if not instalments:
        overall = None
    elif FEE_OVERDUE in statuses:
        overall = FEE_OVERDUE
    elif all(s == FEE_PAID for s in statuses):
        overall = FEE_PAID
    elif FEE_PARTIAL in statuses or paid > 0:
        overall = FEE_PARTIAL
    else:
        overall = FEE_DUE
    return {"total": total, "paid": paid, "balance": total - paid,
            "overdue": statuses.count(FEE_OVERDUE), "status": overall}


def next_receipt_no(db: Session, year: int) -> str:
    prefix = f"{RECEIPT_PREFIX}-{year}-"
    numbers = [int(n[len(prefix):]) for n in
               db.scalars(select(Payment.receipt_no).where(Payment.receipt_no.like(prefix + "%")))
               if n[len(prefix):].isdigit()]
    return f"{prefix}{max(numbers, default=0) + 1:04d}"


def record_payment(db: Session, actor: User, instalment: FeeInstalment, *, amount: Any, method: Any,
                   reference: Any, paid_on: Any, note: Any) -> Payment:
    amount_paise = parse_rupees(amount)
    if amount_paise > instalment.balance_paise:
        raise ServiceError(f"The amount is more than the balance due ({instalment.balance_paise / 100:,.2f}).")
    method_clean = str(method or "").strip()
    if method_clean not in PAYMENT_METHODS:
        raise ServiceError("Choose how the payment was made.")
    reference_clean = clean_text(reference, "Reference", max_len=60, required=False)
    if method_clean == "UPI" and not reference_clean:
        raise ServiceError("UPI payments need the UPI reference (UTR) number.")
    when = parse_date(paid_on, "Payment date") or today_ist()
    if when > today_ist():
        raise ServiceError("The payment date can't be in the future.")
    payment = Payment(instalment_id=instalment.id, student_id=instalment.student_id,
                      amount_paise=amount_paise, method=method_clean, reference=reference_clean,
                      paid_on=when, receipt_no=next_receipt_no(db, when.year),
                      note=clean_text(note, "Note", max_len=200, required=False), recorded_by=actor.id)
    instalment.paid_amount_paise += amount_paise
    db.add(payment)
    db.flush()
    audit(db, actor, "fees.payment", "student", instalment.student.user.login_id,
          receipt=payment.receipt_no, amount_paise=amount_paise, method=method_clean,
          reference=reference_clean, instalment=instalment.title)
    return payment


def delete_instalment(db: Session, actor: User, instalment: FeeInstalment) -> None:
    if instalment.payments:
        raise ServiceError("This instalment has payments, so it can't be deleted.")
    audit(db, actor, "fees.instalment_delete", "student", instalment.student.user.login_id,
          title=instalment.title, amount_paise=instalment.amount_paise)
    db.delete(instalment)


def upi_link(info: dict[str, str], amount_paise: int, note: str) -> str | None:
    """upi://pay link understood by GPay, PhonePe, Paytm, BHIM etc."""
    if not info.get("upi_id"):
        return None
    params = [("pa", info["upi_id"]), ("pn", info.get("upi_payee") or info.get("name") or "Academy")]
    if amount_paise > 0:
        params.append(("am", f"{amount_paise / 100:.2f}"))
    params.append(("cu", "INR"))
    if note:
        params.append(("tn", note[:60]))
    return "upi://pay?" + "&".join(f"{k}={quote(v, safe='@.-_')}" for k, v in params)


# ===========================================================================
# Year-end promotion
# ===========================================================================
def promotion_preview(db: Session) -> list[dict[str, Any]]:
    counts = dict(db.execute(
        select(Student.standard, func.count(Student.id)).join(User, Student.user_id == User.id)
        .where(Student.status == STUDENT_ACTIVE, User.is_active.is_(True)).group_by(Student.standard)
    ).all())
    rows = []
    for code, label in STANDARDS:
        if counts.get(code):
            rows.append({"from": label, "to": "Passed out" if code == MAX_STANDARD else standard_label(code + 1),
                         "count": counts[code]})
    return rows


def promote_all(db: Session, actor: User) -> dict[str, int]:
    students = db.scalars(
        select(Student).join(User, Student.user_id == User.id)
        .where(Student.status == STUDENT_ACTIVE, User.is_active.is_(True))
    ).all()
    promoted = passed_out = 0
    for student in students:
        if student.standard >= MAX_STANDARD:
            student.status = STUDENT_PASSED_OUT
            passed_out += 1
        else:
            student.standard += 1
            if student.standard not in STREAM_STANDARDS:
                student.stream = None
            promoted += 1
    audit(db, actor, "students.promote", "students", None, promoted=promoted, passed_out=passed_out)
    return {"promoted": promoted, "passed_out": passed_out}


# ===========================================================================
# Notices
# ===========================================================================
def create_notice(db: Session, actor: User, *, title: Any, body: Any, standard: int | None,
                  expires_on: Any) -> Notice:
    expiry = parse_date(expires_on, "Expiry date")
    if expiry and expiry < today_ist():
        raise ServiceError("The expiry date is in the past.")
    notice = Notice(title=clean_text(title, "Title", min_len=2, max_len=150),
                    body=clean_text(body, "Message", min_len=2, max_len=4000), standard=standard,
                    expires_on=expiry, created_by=actor.id)
    db.add(notice)
    audit(db, actor, "notice.create", "notice", None, title=notice.title, to=standard_label(standard))
    return notice


def visible_notices(db: Session, standard: int | None, limit: int | None = None) -> list[Notice]:
    """Live notices for a student's class (plus notices for everyone)."""
    query = select(Notice).where(
        or_(Notice.standard.is_(None), Notice.standard == standard),
        or_(Notice.expires_on.is_(None), Notice.expires_on >= today_ist()),
    ).order_by(Notice.created_at.desc())
    if limit:
        query = query.limit(limit)
    return list(db.scalars(query))


# ===========================================================================
# Excel / CSV import
# ===========================================================================
MAX_IMPORT_ROWS = 1000

STUDENT_COLUMNS = {
    "full_name": ("name", "studentname", "fullname"),
    "standard": ("class", "standard", "std", "grade"),
    "stream": ("stream",),
    "father_name": ("fathersname", "fathername", "father", "parentname", "guardianname"),
    "phone": ("phone", "mobile", "phoneno", "mobileno", "contact", "contactno"),
    "dob": ("dob", "dateofbirth", "birthdate"),
    "admission_year": ("admissionyear", "admyear", "yearofadmission"),
}
TEACHER_COLUMNS = {
    "full_name": ("name", "teachername", "fullname"),
    "phone": ("phone", "mobile", "phoneno", "mobileno", "contact"),
    "email": ("email", "emailid", "mail"),
    "standards": ("classes", "class", "standards", "assignedclasses"),
}


def _normalise_header(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def read_table(filename: str, data: bytes) -> list[dict[str, Any]]:
    """Read the first sheet of an .xlsx file or a .csv file into dicts keyed by normalised header."""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook   # imported here: only needed for imports
        try:
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception:
            raise ServiceError("That file could not be read as an Excel (.xlsx) workbook.") from None
        rows = [list(r) for r in workbook.worksheets[0].iter_rows(values_only=True)]
        workbook.close()
    elif name.endswith(".csv"):
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
    else:
        raise ServiceError("Upload an Excel (.xlsx) or CSV (.csv) file.")
    rows = [r for r in rows if any(str(c or "").strip() for c in r)]
    if not rows:
        raise ServiceError("The file is empty.")
    headers = [_normalise_header(h) for h in rows[0]]
    records = [dict(zip(headers, r)) for r in rows[1:]]
    if len(records) > MAX_IMPORT_ROWS:
        raise ServiceError(f"Import at most {MAX_IMPORT_ROWS} rows at a time.")
    return records


def _pick(record: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in record and record[alias] not in (None, ""):
            return record[alias]
    return None


def import_people(db: Session, actor: User, kind: str, records: list[dict[str, Any]]) -> dict:
    """Create students or teachers row by row. Bad rows are reported, good rows created."""
    columns = STUDENT_COLUMNS if kind == "students" else TEACHER_COLUMNS
    if records and _pick(records[0], columns["full_name"]) is None and \
            not any(a in records[0] for a in columns["full_name"]):
        raise ServiceError("No 'Name' column found. Download the template to see the expected columns.")
    created, errors = [], []
    for row_number, record in enumerate(records, start=2):
        values = {key: _pick(record, aliases) for key, aliases in columns.items()}
        try:
            if kind == "students":
                form = build_student_form(**values)
                student, otp = create_student(db, actor, form)
                created.append({"name": form.full_name, "login_id": student.user.login_id, "otp": otp,
                                "detail": standard_label(form.standard)})
            else:
                standards = parse_standards_list(values["standards"])
                teacher, otp = create_teacher(db, actor, full_name=values["full_name"], phone=values["phone"],
                                              email=values["email"], standards=standards)
                created.append({"name": teacher.user.full_name, "login_id": teacher.user.login_id, "otp": otp,
                                "detail": ", ".join(standard_label(s) for s in standards) or "No classes"})
        except ServiceError as exc:
            errors.append({"row": row_number, "error": str(exc)})
    return {"created": created, "errors": errors}


def import_template(kind: str) -> bytes:
    """A ready-to-fill Excel file with the right headers and one example row."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    if kind == "students":
        sheet.title = "Students"
        sheet.append(["Name", "Class", "Stream", "Father's name", "Phone", "DOB", "Admission year"])
        sheet.append(["Aarav Sharma", "7", "", "Rakesh Sharma", "9876543210", "2014-05-21", today_ist().year])
        sheet.append(["Diya Patel", "11", "Science", "Mehul Patel", "9812345678", "21-08-2010", ""])
    else:
        sheet.title = "Teachers"
        sheet.append(["Name", "Phone", "Email", "Classes"])
        sheet.append(["Meera Iyer", "9876500001", "meera@example.com", "9, 10"])
        sheet.append(["Rahul Verma", "9876500002", "", "Nursery, LKG, UKG"])
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1E3A8A")
    for column in sheet.columns:
        sheet.column_dimensions[column[0].column_letter].width = 20
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
