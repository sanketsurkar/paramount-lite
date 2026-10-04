"""
tests/full_test.py
------------------
End-to-end test of EVERY feature (all phases), as admin, teacher and student.
Uses a temporary database and upload folder - your real data is untouched.

    python tests/full_test.py

Every line should start with PASS; the last line is ALL GOOD.
"""
import io
import os
import re
import sys
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = "sqlite:///" + (_tmp / "full.db").as_posix()
os.environ["APP_ENV"] = "development"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from sqlalchemy import select  # noqa: E402

import app.storage as storage  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Student, User  # noqa: E402
from app.security import hash_password, login_limiter  # noqa: E402

storage.UPLOAD_DIR = _tmp / "uploads"      # keep test uploads out of instance/
YEAR = __import__("app.config", fromlist=["x"]).today_ist().year % 100
COUNT = 0


def ok(condition, message):
    global COUNT
    print(("PASS " if condition else "FAIL ") + message)
    if not condition:
        sys.exit(1)
    COUNT += 1


def csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def page_csrf(client, path):
    return csrf(client.get(path).text)


def post(client, path, data, token_page, files=None):
    data = dict(data)
    data["csrf_token"] = page_csrf(client, token_page)
    return client.post(path, data=data, files=files, follow_redirects=True)


def credentials(html):
    return re.findall(r'<strong>([A-Z0-9-]+)</strong></td>\s*<td class="otp">(\w+)</td>', html)


def login(client, login_id, password):
    token = page_csrf(client, "/login")
    return client.post("/login", data={"login_id": login_id, "password": password, "csrf_token": token},
                       follow_redirects=True)


def first_login(client, login_id, otp, new="Welcome2026"):
    r = login(client, login_id, otp)
    assert "/change-password" in str(r.url), r.url
    token = csrf(r.text)
    return client.post("/change-password", data={
        "current_password": otp, "new_password": new, "confirm_password": new, "csrf_token": token},
        follow_redirects=True)


def json_post(client, path, payload, token_page):
    html = client.get(token_page).text
    token = re.search(r'data-csrf="([^"]+)"', html).group(1)
    return client.post(path, json=payload, headers={"X-CSRF-Token": token, "Accept": "application/json"})


with TestClient(app) as admin:
    # ---------- Admin login ----------
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.login_id == "ADM-001"))
        u.password_hash = hash_password("Admin2026x")
        u.must_change_password = False
        db.commit()
    r = login(admin, "ADM-001", "Admin2026x")
    ok(r.status_code == 200 and "/admin" in str(r.url), "admin logs in")

    # ---------- Settings ----------
    r = post(admin, "/admin/settings", {
        "name": "The Paramount Academy", "tagline": "Excellence", "address": "12 Knowledge Park",
        "phone": "+91 98765 43210", "email": "office@paramount.example", "upi_id": "paramount@okhdfcbank",
        "upi_payee": "The Paramount Academy", "leaderboard": ["10", "11"]}, "/admin/settings")
    ok("Settings saved" in r.text, "settings saved")
    r = post(admin, "/admin/settings", {"name": "X", "upi_id": "bad-upi"}, "/admin/settings")
    ok("name@bank" in r.text, "invalid UPI ID rejected")

    # ---------- Teachers ----------
    r = post(admin, "/admin/teachers", {"full_name": "Meera Iyer", "phone": "9876500001",
                                        "email": "meera@example.com", "standards": ["10"]}, "/admin/teachers")
    creds = credentials(r.text)
    ok(creds and creds[0][0] == "TCH-001", "teacher created as TCH-001 with one-time password")
    teacher_otp = creds[0][1]
    r = post(admin, "/admin/teachers", {"full_name": "X", "standards": ["10"]}, "/admin/teachers")
    ok("at least 2" in r.text, "teacher name validation")

    # ---------- Students (form) ----------
    ids = {}
    for name, std, stream in [("Aarav Sharma", "10", ""), ("Diya Patel", "10", ""), ("Kabir Singh", "10", ""),
                              ("Isha Rao", "11", "Science"), ("Rohan Das", "12", "")]:
        r = post(admin, "/admin/students", {"full_name": name, "standard": std, "stream": stream,
                                            "phone": "9812345678", "admission_year": "20" + str(YEAR)},
                 "/staff/students")
        c = credentials(r.text)
        ids[name] = c[0]
    ok(ids["Aarav Sharma"][0] == f"TPA{YEAR:02d}-0001" and ids["Rohan Das"][0] == f"TPA{YEAR:02d}-0005",
       "student IDs TPAyy-0001..0005")
    r = post(admin, "/admin/students", {"full_name": "Bad Class", "standard": "13"}, "/staff/students")
    ok("valid class" in r.text, "invalid class rejected")
    r = admin.get("/staff/students?standard=10")
    ok(r.text.count('class="row-link"') == 3, "student list filtered by class")

    # ---------- Import (xlsx + csv) ----------
    r = admin.get("/admin/import/template/students.xlsx")
    ok(r.status_code == 200 and r.content[:2] == b"PK", "import template download")
    wb = Workbook()
    ws = wb.active
    ws.append(["Name", "Class", "Stream", "Father's name", "Phone", "DOB"])
    ws.append(["Neha Gupta", "UKG", "", "Raj Gupta", "9800000001", "2020-06-01"])
    ws.append(["Vik Mehta", "11", "Commerce", "", "", "21-08-2010"])
    ws.append(["Bad Row", "Class 99", "", "", "", ""])
    buf = io.BytesIO()
    wb.save(buf)
    r = post(admin, "/admin/import", {"kind": "students"}, "/admin/import",
             files={"file": ("students.xlsx", buf.getvalue(), "application/octet-stream")})
    ok(len(credentials(r.text)) == 2 and "Row 4" in r.text, "xlsx import: 2 created, bad row reported")
    csv_data = "Name,Phone,Email,Classes\nRahul Verma,9876500002,,\"Nursery, LKG\"\n".encode()
    r = post(admin, "/admin/import", {"kind": "teachers"}, "/admin/import",
             files={"file": ("teachers.csv", csv_data, "text/csv")})
    ok(credentials(r.text) and credentials(r.text)[0][0] == "TCH-002", "csv teacher import")

    # ---------- Subjects ----------
    r = post(admin, "/admin/subjects", {"name": "Computer", "colour": "#7c3aed", "icon": "💻",
                                        "standards": ["10", "11"], "stream": "Science", "back": "10"},
             "/admin/subjects?standard=10")
    ok("Added to 2 classes" in r.text, "subject added to several classes")
    r = post(admin, "/admin/subjects", {"name": "computer", "standards": ["10"], "back": "10"},
             "/admin/subjects?standard=10")
    ok("Already existed" in r.text, "duplicate subject skipped")
    with SessionLocal() as db:
        from app.models import Subject
        comp10 = db.scalar(select(Subject).where(Subject.standard == 10, Subject.name == "Computer"))
        comp11 = db.scalar(select(Subject).where(Subject.standard == 11, Subject.name == "Computer"))
        eng10 = db.scalar(select(Subject).where(Subject.standard == 10, Subject.name == "English"))
        maths10 = db.scalar(select(Subject).where(Subject.standard == 10, Subject.name == "Mathematics"))
        sci10 = db.scalar(select(Subject).where(Subject.standard == 10, Subject.name == "Science"))
    ok(comp10.stream is None and comp11.stream == "Science", "stream only stored for Class 11-12")
    r = post(admin, f"/admin/subjects/{comp10.id}/edit", {"name": "Computer Science", "colour": "#7c3aed",
                                                          "icon": "💻"}, "/admin/subjects?standard=10")
    ok("Subject saved" in r.text and "Computer Science" in r.text, "subject renamed")
    post(admin, f"/admin/subjects/{comp10.id}/move", {"direction": "up"}, "/admin/subjects?standard=10")
    r = admin.get("/admin/subjects?standard=10")
    ok(r.text.index("Computer Science") < r.text.index("Science</strong>"), "subject reordered")
    r = post(admin, f"/admin/subjects/{comp10.id}/copy", {"standards": ["9"]}, "/admin/subjects?standard=10")
    ok("Copied to: Class 9" in r.text, "subject copied to another class")
    r = post(admin, f"/admin/subjects/{comp10.id}/remove", {}, "/admin/subjects?standard=10")
    ok("Computer Science deleted" in r.text, "unused subject deleted")

    # ---------- Notices ----------
    r = post(admin, "/staff/notices", {"title": "Diwali holiday", "body": "Closed 8-12 Nov.", "standard": ""},
             "/staff/notices")
    ok("Notice posted" in r.text, "admin posts notice to everyone")

    # ---------- Teacher ----------
    teacher = TestClient(app)
    r = first_login(teacher, "TCH-001", teacher_otp)
    ok("/staff" in str(r.url) and "Welcome, Meera Iyer" in r.text, "teacher first login + forced change")
    ok(teacher.get("/admin/fees").status_code == 403, "teacher cannot see fees")
    ok(teacher.get("/staff/students?standard=9").status_code == 403, "teacher limited to assigned class")
    r = teacher.get("/staff/students")
    ok("Isha Rao" not in r.text and "Aarav Sharma" in r.text, "teacher sees only own students")
    r = post(teacher, "/staff/notices", {"title": "Test", "body": "Hello", "standard": ""}, "/staff/notices")
    ok("own classes only" in r.text, "teacher cannot post to everyone")
    r = post(teacher, "/staff/notices", {"title": "Maths test Friday", "body": "Chapter 3", "standard": "10"},
             "/staff/notices")
    ok("Notice posted" in r.text, "teacher posts notice to own class")

    # Notes
    r = post(teacher, f"/staff/notes/subject/{maths10.id}/chapters", {"title": "Chapter 1: Real numbers"},
             f"/staff/notes/subject/{maths10.id}")
    ok("Chapter added" in r.text, "chapter added")
    chapter_id = int(re.search(r"/staff/notes/chapters/(\d+)/upload", r.text).group(1))
    pdf = b"%PDF-1.4\n%test\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"
    r = post(teacher, f"/staff/notes/chapters/{chapter_id}/upload", {"title": "Worksheet 1"},
             f"/staff/notes/subject/{maths10.id}", files={"file": ("ws1.pdf", pdf, "application/pdf")})
    ok("File uploaded" in r.text and "Worksheet 1" in r.text, "PDF uploaded")
    r = post(teacher, f"/staff/notes/chapters/{chapter_id}/upload", {"title": "Fake"},
             f"/staff/notes/subject/{maths10.id}", files={"file": ("evil.pdf", b"MZ\x90\x00 not a pdf", "application/pdf")})
    ok("Only PDF, JPG, PNG or WEBP" in r.text, "file signature check rejects fake PDF")
    content_id = int(re.search(r'href="/files/(\d+)"', teacher.get(f"/staff/notes/subject/{maths10.id}").text).group(1))
    ok(teacher.get(f"/files/{content_id}").content == pdf, "file served to staff")

    # Exam + marks grid
    r = teacher.get("/staff/exams?standard=10")
    form = {"standard": "10", "name": "Unit Test 1", "exam_date": "2026-09-15",
            "subject_ids": [str(eng10.id), str(maths10.id), str(sci10.id)],
            f"max_{eng10.id}": "50", f"max_{maths10.id}": "50", f"max_{sci10.id}": "50"}
    r = post(teacher, "/staff/exams", form, "/staff/exams?standard=10")
    ok("Exam created" in r.text and "data-marks-grid" in r.text, "exam created, marks grid shown")
    exam_id = int(re.search(r"/staff/exams/(\d+)/marks", r.text).group(1))
    with SessionLocal() as db:
        sid = {s.user.full_name: s.id for s in db.scalars(select(Student))}
    cells = []
    marks = {"Aarav Sharma": (45, 48, 47), "Diya Patel": (45, 48, 47), "Kabir Singh": ("AB", 30, 35)}
    for name, values in marks.items():
        for subject, value in zip((eng10, maths10, sci10), values):
            cells.append({"student_id": sid[name], "subject_id": subject.id, "value": str(value)})
    cells.append({"student_id": sid["Kabir Singh"], "subject_id": eng10.id, "value": "55"})   # > max
    res = json_post(teacher, f"/staff/exams/{exam_id}/marks",
                    {"cells": cells, "remarks": [{"student_id": sid["Aarav Sharma"], "remark": "Excellent work"}]},
                    f"/staff/exams/{exam_id}")
    data = res.json()
    ok(res.status_code == 200 and data["saved"] == 10 and len(data["errors"]) == 1, "marks autosave (1 invalid cell reported)")
    r = teacher.get(f"/staff/exams/{exam_id}")
    ok('value="AB"' in r.text and "Excellent work" in r.text, "AB and remark persisted")
    ok(teacher.post(f"/staff/exams/{exam_id}/publish",
                    data={"csrf_token": page_csrf(teacher, f"/staff/exams/{exam_id}")}).status_code == 403,
       "teacher cannot publish")
    bad = json_post(teacher, f"/staff/exams/{exam_id}/marks", {"cells": [{"student_id": sid["Isha Rao"],
                    "subject_id": eng10.id, "value": "10"}]}, f"/staff/exams/{exam_id}")
    ok(bad.json()["errors"], "marks for a student outside the class are refused")

    # ---------- Admin publishes ----------
    r = post(admin, f"/staff/exams/{exam_id}/publish", {}, f"/staff/exams/{exam_id}")
    ok("Results published" in r.text, "admin publishes results")
    locked = json_post(teacher, f"/staff/exams/{exam_id}/marks", {"cells": cells[:1]}, f"/staff/exams/{exam_id}")
    ok(locked.status_code == 409, "marks locked after publishing")
    r = post(admin, f"/admin/subjects/{eng10.id}/remove", {}, "/admin/subjects?standard=10")
    ok("archived" in r.text, "subject with marks is archived, not deleted")
    post(admin, f"/admin/subjects/{eng10.id}/restore", {}, "/admin/subjects?standard=10")

    # ---------- Fees ----------
    r = post(admin, "/admin/fees/instalments", {"standard": "10", "title": "Term 1 fee", "amount": "12,500",
                                                "due_date": "2026-09-30"}, "/admin/fees")
    ok("Instalment added for 3 students" in r.text, "class instalment added")
    r = admin.get("/admin/fees?standard=10&status=Overdue")
    ok(r.text.count('class="row-link"') == 3, "overdue status computed from due date")
    r = admin.get(f"/admin/fees/student/{sid['Aarav Sharma']}")
    inst_id = int(re.search(r"/admin/fees/instalments/(\d+)/pay", r.text).group(1))
    r = post(admin, f"/admin/fees/instalments/{inst_id}/pay", {"amount": "5000", "method": "UPI", "reference": "",
                                                               "paid_on": "2026-10-01"},
             f"/admin/fees/student/{sid['Aarav Sharma']}")
    ok("UTR" in r.text, "UPI payment needs a reference")
    r = post(admin, f"/admin/fees/instalments/{inst_id}/pay", {"amount": "99999", "method": "Cash",
                                                               "paid_on": "2026-10-01"},
             f"/admin/fees/student/{sid['Aarav Sharma']}")
    ok("more than the balance" in r.text, "overpayment refused")
    r = post(admin, f"/admin/fees/instalments/{inst_id}/pay", {"amount": "5000", "method": "UPI",
                                                               "reference": "UTR123456", "paid_on": "2026-10-01"},
             f"/admin/fees/student/{sid['Aarav Sharma']}")
    ok("RCPT-2026-0001" in r.text and "five thousand" in r.text, "receipt RCPT-2026-0001 with amount in words")
    receipt_url = str(r.url).replace("http://testserver", "")
    r = post(admin, f"/admin/fees/instalments/{inst_id}/pay", {"amount": "7500", "method": "Cash",
                                                               "paid_on": "2026-10-02"},
             f"/admin/fees/student/{sid['Aarav Sharma']}")
    ok("RCPT-2026-0002" in r.text, "second receipt numbered 0002")
    r = admin.get(f"/admin/fees/student/{sid['Aarav Sharma']}")
    ok("chip-fee-paid" in r.text, "instalment fully paid")

    # ---------- Student ----------
    student = TestClient(app)
    login_id, otp = ids["Aarav Sharma"]
    r = first_login(student, login_id, otp)
    ok("/student" in str(r.url) and "Hi, Aarav" in r.text, "student first login + forced change")
    ok('class="tabbar"' in r.text and "data-ring" in r.text, "student home: tab bar + progress ring")
    ok("Diwali holiday" in r.text and "Maths test Friday" in r.text, "student sees notices")
    r = student.get("/student/results")
    ok("Unit Test 1" in r.text and "Rank 1 of 3" in r.text and "trend-dot" in r.text, "results + trend chart")
    r = student.get(f"/student/results/{exam_id}")
    ok("Excellent work" in r.text and "140 out of 150" in r.text, "result detail with remark")
    r = student.get("/student/ranks")
    ok("podium-1" in r.text and "Aarav S." in r.text and "Diya P." in r.text, "leaderboard podium, short names")
    ok(r.text.count('class="podium-block"><span>1</span>') == 2, "ties share rank 1")
    r = student.get("/student/courses")
    ok("Mathematics" in r.text, "courses list")
    r = student.get(f"/student/courses/{maths10.id}")
    ok('data-action="save-offline"' in r.text, "save offline button")
    ok(student.get(f"/files/{content_id}").content == pdf, "student opens own-class file")
    r = student.get("/student/fees")
    ok("paid" in r.text and "Receipts" in r.text, "student fees page")
    ok(student.get(receipt_url).status_code == 200, "student sees own receipt")
    ok(student.get("/admin").status_code == 403 and student.get("/staff/students").status_code == 403,
       "student blocked from staff pages")

    other = TestClient(app)
    r = first_login(other, *ids["Isha Rao"])
    ok(other.get(f"/files/{content_id}").status_code == 403, "other class cannot open the file")
    ok(other.get(receipt_url).status_code == 403, "other student cannot see the receipt")
    ok(other.get(f"/student/courses/{comp11.id}").status_code == 200, "Science-stream student sees stream subject")

    # UPI QR for a student who owes money
    kabir = TestClient(app)
    first_login(kabir, *ids["Kabir Singh"])
    r = kabir.get("/student/fees")
    ok('class="qr"' in r.text and "upi://pay?pa=paramount@okhdfcbank" in r.text and "am=12500.00" in r.text,
       "UPI QR + pay link for amount due")

    # Leaderboard switch
    post(admin, "/admin/settings", {"name": "The Paramount Academy", "upi_id": "paramount@okhdfcbank",
                                    "leaderboard": ["11"]}, "/admin/settings")
    ok("switched off" in student.get("/student/ranks").text, "leaderboard can be switched off per class")

    # ---------- Password reset + deactivate ----------
    with SessionLocal() as db:
        aarav_user = db.scalar(select(User).where(User.login_id == login_id)).id
    r = post(admin, f"/admin/users/{aarav_user}/reset-password", {}, f"/staff/students/{sid['Aarav Sharma']}")
    new_otp = credentials(r.text)[0][1]
    ok(len(new_otp) == 6, "admin resets password (new one-time password)")
    ok("/login" in str(student.get("/student").url), "old session ended after reset")
    r = login(student, login_id, new_otp)
    ok("/change-password" in str(r.url), "must change password after reset")
    post(admin, f"/admin/users/{aarav_user}/toggle-active", {}, f"/staff/students/{sid['Aarav Sharma']}")
    r = login(TestClient(app), login_id, new_otp)
    ok("inactive" in r.text, "deactivated student cannot log in")

    # ---------- Promote ----------
    r = post(admin, "/admin/promote", {"confirm": "promote"}, "/admin/promote")
    ok("Type PROMOTE" in r.text, "promote needs exact PROMOTE")
    r = post(admin, "/admin/promote", {"confirm": "PROMOTE"}, "/admin/promote")
    ok("marked passed out" in r.text, "promotion done")
    with SessionLocal() as db:
        students = {s.user.full_name: s for s in db.scalars(select(Student))}
        ok(students["Diya Patel"].standard == 11 and students["Rohan Das"].status == "passed_out"
           and students["Neha Gupta"].standard == 1 and students["Aarav Sharma"].standard == 10,
           "10->11, 12->passed out, UKG->1, deactivated student untouched")

    # ---------- Audit ----------
    r = admin.get("/admin/audit")
    for action in ("Marks update", "Fees payment", "Password reset", "Subject archive", "Students promote"):
        ok(action in r.text, f"audit log has '{action}'")
    ok("Marks update" in admin.get("/admin/audit?kind=marks").text, "audit filter")

    # ---------- PWA files ----------
    r = admin.get("/sw.js")
    ok(r.status_code == 200 and r.headers.get("service-worker-allowed") == "/", "service worker served at root")
    ok(admin.get("/manifest.webmanifest").json()["short_name"] == "Paramount", "web manifest")
    ok("You're offline" in admin.get("/offline.html").text, "offline page")

    # ---------- Every page renders for every role ----------
    pages = {
        admin: ["/admin", "/admin/teachers", "/admin/subjects?standard=11", "/admin/fees",
                f"/admin/fees/student/{sid['Kabir Singh']}", "/admin/settings", "/admin/audit", "/admin/promote",
                "/admin/import?kind=teachers", "/staff/students", f"/staff/students/{sid['Diya Patel']}",
                "/staff/notes", f"/staff/notes/subject/{maths10.id}", "/staff/exams", f"/staff/exams/{exam_id}",
                "/staff/notices", "/change-password"],
        teacher: ["/staff", "/staff/notes", "/staff/exams?standard=10", "/staff/notices"],
        kabir: ["/student", "/student/courses", "/student/saved", "/student/results", "/student/ranks",
                "/student/fees", "/student/notices"],
    }
    for client, urls in pages.items():
        for url in urls:
            r = client.get(url)
            if r.status_code != 200 or "content-security-policy" not in r.headers:
                ok(False, f"page {url} -> {r.status_code}")
    ok(True, f"{sum(len(v) for v in pages.values())} pages render for admin/teacher/student")

print(f"ALL GOOD ({COUNT} checks)")
