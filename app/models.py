"""
app/models.py
-------------
All database tables as SQLAlchemy 2.0 classes (= EF Core entity classes).

    Mapped[int]          -> NOT NULL column
    Mapped[int | None]   -> NULL-able column
    relationship(...)    -> navigation property

Conventions:
* Timestamps are naive UTC (config.utcnow); show them in IST.
* Money is INTEGER PAISE: Rs 1,250.50 is stored as 125050 (no float errors).
* A class is an integer `standard`: Nursery=-2, LKG=-1, UKG=0, 1..12.
* Users/students are never hard-deleted - they are deactivated, so history
  (marks, receipts, audit log) always stays intact.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import ROLE_ADMIN, ROLE_STUDENT, ROLE_TEACHER, today_ist, utcnow
from app.db import Base

# ---------------------------------------------------------------------------
# Status values (plain strings keep the DB readable in any SQL tool)
# ---------------------------------------------------------------------------
STUDENT_ACTIVE = "active"
STUDENT_PASSED_OUT = "passed_out"      # set when Class 12 is promoted

EXAM_DRAFT = "draft"
EXAM_PUBLISHED = "published"

CONTENT_PDF = "pdf"
CONTENT_IMAGE = "image"

FEE_PAID = "Paid"
FEE_PARTIAL = "Partial"
FEE_DUE = "Due"
FEE_OVERDUE = "Overdue"


# ===========================================================================
# People
# ===========================================================================
class User(Base):
    """One row per person who can log in: admin, teacher, or student/parent."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin','teacher','student')", name="ck_users_role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # ADM-001 / TCH-001 / TPA26-0001 - what people type on the login page
    login_id: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(10))
    password_hash: Mapped[str] = mapped_column(String(255))
    # True for new users and after an admin reset -> forced to change at login
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    # Increased on every password change/reset. The session cookie stores it,
    # so sessions on other devices stop working (like ASP.NET's security stamp).
    session_version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime)

    student: Mapped[Student | None] = relationship(back_populates="user")
    teacher: Mapped[Teacher | None] = relationship(back_populates="user")

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN

    @property
    def is_teacher(self) -> bool:
        return self.role == ROLE_TEACHER

    @property
    def is_student(self) -> bool:
        return self.role == ROLE_STUDENT

    @property
    def short_name(self) -> str:
        """'Aarav Sharma' -> 'Aarav S.' (used on the leaderboard for privacy)."""
        parts = self.full_name.split()
        if len(parts) < 2:
            return self.full_name
        return f"{parts[0]} {parts[-1][0].upper()}."


class Student(Base):
    """Extra details for users with role 'student'."""

    __tablename__ = "students"
    __table_args__ = (
        CheckConstraint("standard BETWEEN -2 AND 12", name="ck_students_standard"),
        Index("ix_students_standard_status", "standard", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    standard: Mapped[int] = mapped_column(Integer)
    stream: Mapped[str | None] = mapped_column(String(20))   # only for Class 11-12
    admission_year: Mapped[int] = mapped_column(Integer)      # e.g. 2026
    father_name: Mapped[str | None] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(20))
    dob: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(12), default=STUDENT_ACTIVE)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="student")
    fee_instalments: Mapped[list[FeeInstalment]] = relationship(
        back_populates="student",
        cascade="all, delete-orphan",
        order_by="FeeInstalment.due_date",
    )


class Teacher(Base):
    """Extra details for users with role 'teacher'."""

    __tablename__ = "teachers"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    phone: Mapped[str | None] = mapped_column(String(20))
    email: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="teacher")
    standards: Mapped[list[TeacherStandard]] = relationship(
        back_populates="teacher",
        cascade="all, delete-orphan",
        order_by="TeacherStandard.standard",
    )

    @property
    def standard_codes(self) -> list[int]:
        """Classes this teacher is allowed to see, e.g. [9, 10]."""
        return [ts.standard for ts in self.standards]


class TeacherStandard(Base):
    """Which classes a teacher is assigned to (many-to-many link table)."""

    __tablename__ = "teacher_standards"
    __table_args__ = (
        UniqueConstraint("teacher_id", "standard", name="uq_teacher_standard"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    teacher_id: Mapped[int] = mapped_column(ForeignKey("teachers.id", ondelete="CASCADE"))
    standard: Mapped[int] = mapped_column(Integer)

    teacher: Mapped[Teacher] = relationship(back_populates="standards")


# ===========================================================================
# Subjects and course notes
# ===========================================================================
class Subject(Base):
    """A subject in one class. Archived subjects are hidden but keep old results."""

    __tablename__ = "subjects"
    __table_args__ = (
        UniqueConstraint("standard", "name", name="uq_subjects_standard_name"),
        Index("ix_subjects_standard_position", "standard", "position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    standard: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String(60))
    stream: Mapped[str | None] = mapped_column(String(20))   # None = all streams
    colour: Mapped[str] = mapped_column(String(7), default="#1e3a8a")  # hex
    icon: Mapped[str] = mapped_column(String(16), default="📘")        # emoji
    position: Mapped[int] = mapped_column(Integer, default=0)          # sort order
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chapters: Mapped[list[Chapter]] = relationship(
        back_populates="subject",
        cascade="all, delete-orphan",
        order_by="Chapter.position",
    )


class Chapter(Base):
    """A chapter inside a subject; holds the uploaded notes."""

    __tablename__ = "chapters"

    id: Mapped[int] = mapped_column(primary_key=True)
    subject_id: Mapped[int] = mapped_column(
        ForeignKey("subjects.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(150))
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    subject: Mapped[Subject] = relationship(back_populates="chapters")
    contents: Mapped[list[Content]] = relationship(
        back_populates="chapter",
        cascade="all, delete-orphan",
        order_by="Content.created_at",
    )


class Content(Base):
    """One uploaded PDF or image. The file itself lives on disk or in Supabase."""

    __tablename__ = "content"
    __table_args__ = (
        CheckConstraint("kind IN ('pdf','image')", name="ck_content_kind"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    chapter_id: Mapped[int] = mapped_column(
        ForeignKey("chapters.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(10))
    mime: Mapped[str] = mapped_column(String(50))
    size_bytes: Mapped[int] = mapped_column(Integer)
    # Path inside instance/uploads (local) or the object key in Supabase
    storage_key: Mapped[str] = mapped_column(String(300), unique=True)
    original_filename: Mapped[str] = mapped_column(String(255))
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    chapter: Mapped[Chapter] = relationship(back_populates="contents")


# ===========================================================================
# Exams and results
# ===========================================================================
class Exam(Base):
    """An exam for one class in one academic year. Students see only published ones."""

    __tablename__ = "exams"
    __table_args__ = (
        CheckConstraint("status IN ('draft','published')", name="ck_exams_status"),
        Index("ix_exams_standard_year", "standard", "academic_year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    standard: Mapped[int] = mapped_column(Integer)
    academic_year: Mapped[str] = mapped_column(String(9))       # '2026-27'
    name: Mapped[str] = mapped_column(String(100))              # 'Unit Test 1'
    exam_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(10), default=EXAM_DRAFT)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)

    subjects: Mapped[list[ExamSubject]] = relationship(
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="ExamSubject.position",
    )
    results: Mapped[list[Result]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )
    remarks: Mapped[list[ExamRemark]] = relationship(
        back_populates="exam", cascade="all, delete-orphan"
    )

    @property
    def is_published(self) -> bool:
        return self.status == EXAM_PUBLISHED


class ExamSubject(Base):
    """Which subjects an exam covers and the maximum marks for each."""

    __tablename__ = "exam_subjects"
    __table_args__ = (
        UniqueConstraint("exam_id", "subject_id", name="uq_exam_subject"),
        CheckConstraint("max_marks > 0", name="ck_exam_subject_max"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"))
    # RESTRICT: a subject used in an exam can't be deleted (it gets archived instead)
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id", ondelete="RESTRICT"))
    max_marks: Mapped[float] = mapped_column(Float)
    position: Mapped[int] = mapped_column(Integer, default=0)

    exam: Mapped[Exam] = relationship(back_populates="subjects")
    subject: Mapped[Subject] = relationship()


class Result(Base):
    """Marks for one student in one subject of one exam (one cell of the grid)."""

    __tablename__ = "results"
    __table_args__ = (
        UniqueConstraint("exam_id", "student_id", "subject_id", name="uq_result_cell"),
        CheckConstraint("marks IS NULL OR marks >= 0", name="ck_result_marks"),
        Index("ix_results_student", "student_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    subject_id: Mapped[int] = mapped_column(ForeignKey("subjects.id", ondelete="RESTRICT"))
    marks: Mapped[float | None] = mapped_column(Float)          # one decimal, e.g. 42.5
    is_absent: Mapped[bool] = mapped_column(Boolean, default=False)   # typed as "AB"
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    exam: Mapped[Exam] = relationship(back_populates="results")


class ExamRemark(Base):
    """Optional teacher remark for one student in one exam."""

    __tablename__ = "exam_remarks"
    __table_args__ = (
        UniqueConstraint("exam_id", "student_id", name="uq_exam_remark"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exam_id: Mapped[int] = mapped_column(ForeignKey("exams.id", ondelete="CASCADE"))
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    remark: Mapped[str] = mapped_column(String(300))
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    exam: Mapped[Exam] = relationship(back_populates="remarks")


# ===========================================================================
# Fees
# ===========================================================================
class FeeInstalment(Base):
    """One amount a student owes by a due date (e.g. 'Term 1 fee')."""

    __tablename__ = "fee_instalments"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_fee_amount_positive"),
        CheckConstraint(
            "paid_amount_paise >= 0 AND paid_amount_paise <= amount_paise",
            name="ck_fee_paid_range",
        ),
        Index("ix_fee_student_year", "student_id", "academic_year"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id", ondelete="CASCADE"))
    academic_year: Mapped[str] = mapped_column(String(9))
    title: Mapped[str] = mapped_column(String(80))
    amount_paise: Mapped[int] = mapped_column(Integer)
    paid_amount_paise: Mapped[int] = mapped_column(Integer, default=0)
    due_date: Mapped[date] = mapped_column(Date)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    student: Mapped[Student] = relationship(back_populates="fee_instalments")
    payments: Mapped[list[Payment]] = relationship(
        back_populates="instalment", order_by="Payment.paid_on"
    )

    @property
    def balance_paise(self) -> int:
        return self.amount_paise - self.paid_amount_paise

    def status_on(self, on: date | None = None) -> str:
        """
        Paid    : fully paid
        Overdue : not fully paid and the due date has passed (even if partly paid)
        Partial : something paid, not yet due
        Due     : nothing paid, not yet due
        """
        on = on or today_ist()
        if self.paid_amount_paise >= self.amount_paise:
            return FEE_PAID
        if self.due_date < on:
            return FEE_OVERDUE
        if self.paid_amount_paise > 0:
            return FEE_PARTIAL
        return FEE_DUE

    @property
    def status(self) -> str:
        return self.status_on()


class Payment(Base):
    """A payment recorded by the admin. Each one gets a unique receipt number."""

    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_paise > 0", name="ck_payment_amount_positive"),
        CheckConstraint(
            "method IN ('UPI','Cash','Bank transfer')", name="ck_payment_method"
        ),
        # The database itself refuses a UPI payment without a reference number.
        CheckConstraint(
            "method <> 'UPI' OR reference IS NOT NULL", name="ck_payment_upi_reference"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    instalment_id: Mapped[int] = mapped_column(
        ForeignKey("fee_instalments.id", ondelete="RESTRICT"), index=True
    )
    student_id: Mapped[int] = mapped_column(
        ForeignKey("students.id", ondelete="CASCADE"), index=True
    )
    amount_paise: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(20))
    reference: Mapped[str | None] = mapped_column(String(60))   # UPI/bank ref no.
    paid_on: Mapped[date] = mapped_column(Date)
    receipt_no: Mapped[str] = mapped_column(String(20), unique=True)  # RCPT-2026-0001
    note: Mapped[str | None] = mapped_column(String(200))
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    instalment: Mapped[FeeInstalment] = relationship(back_populates="payments")


# ===========================================================================
# Notices, settings, audit
# ===========================================================================
class Notice(Base):
    """A notice for everyone (standard is NULL) or for one class."""

    __tablename__ = "notices"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(150))
    body: Mapped[str] = mapped_column(Text)
    standard: Mapped[int | None] = mapped_column(Integer)   # None = everyone
    expires_on: Mapped[date | None] = mapped_column(Date)    # None = never expires
    created_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    def is_visible_to(self, standard: int | None, on: date | None = None) -> bool:
        """True if not expired and addressed to everyone or to this class."""
        on = on or today_ist()
        if self.expires_on is not None and self.expires_on < on:
            return False
        return self.standard is None or self.standard == standard


class Setting(Base):
    """
    Simple key/value store: academy name, UPI ID, leaderboard switches
    (e.g. 'leaderboard.10' = 'off'), current academic year, etc.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class AuditLog(Base):
    """Who changed what and when (marks, fees, passwords, subjects, promotion)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_login_id: Mapped[str] = mapped_column(String(20))   # kept even if user changes
    action: Mapped[str] = mapped_column(String(40), index=True)  # e.g. 'marks.update'
    entity: Mapped[str] = mapped_column(String(30))              # e.g. 'exam'
    entity_id: Mapped[str | None] = mapped_column(String(40))
    details: Mapped[str | None] = mapped_column(Text)            # JSON text
