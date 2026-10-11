# The Paramount Academy app (paramount-lite)

Installable mobile web app (PWA) for The Paramount Academy: course notes, exams and
marks, results with progress charts, class leaderboards, fees with UPI and receipts,
notices, and offline access for students.

**Stack:** Python 3.12+, FastAPI, SQLAlchemy 2.0, Jinja2 (server-rendered HTML), one
CSS file, one JavaScript file, a service worker. No Node, no build step, no CDN.

---

## 1. Run it on your laptop (Windows, PowerShell)

```powershell
cd paramount-lite
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

If activation fails with "running scripts is disabled", run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.

On the first start the console prints the admin login:

```
====================================================
  Admin account created
  Login ID          : ADM-001
  One-time password : k7m2xp
====================================================
```

Open http://127.0.0.1:8000, log in, and choose your own password.

| Command | What it does |
|---|---|
| `python run.py` | Start the app with auto-reload |
| `python run.py --lan` | Also reachable from a phone on the same Wi-Fi (`http://<laptop-IP>:8000`) |
| `python -m app.seed --reset-admin` | New one-time password for ADM-001 (if locked out) |
| `python tests/smoke_test.py` | Quick login/security test (23 checks) |
| `python tests/full_test.py` | Every feature as admin, teacher and student (79 checks) |

Both tests use a temporary database, so your real data is never touched.
To start again from an empty database, stop the server and delete the `instance` folder.

## 2. First-day setup (as admin)

1. **Settings**: academy name, address, phone, **UPI ID**, and which classes show a leaderboard.
2. **Teachers**: add each teacher and tick their classes, or import from Excel.
   You get a printable list of login IDs and one-time passwords.
3. **Students**: add students or import them (Students > Import). The template is
   downloadable from the import page.
4. **Subjects**: every class starts with English, Mathematics and Science. Add more
   (one subject can go to several classes at once) and set streams for Class 11-12.
5. **Fees**: add an instalment for a whole class, then record payments as they come in.

## 3. What each role can do

| | Admin | Teacher | Student / parent |
|---|---|---|---|
| Students | Add, edit, import, reset password, deactivate | View own classes | - |
| Teachers | Add, edit, import, assign classes | - | - |
| Subjects | Add, rename, reorder, copy, archive/restore | - | See own subjects |
| Course notes | Manage all classes | Manage own classes | Open, save offline |
| Exams & marks | Everything + publish/unpublish | Create, enter marks (own classes) | See published results |
| Results & ranks | - | - | Marks, %, rank, remark, trend chart, leaderboard |
| Fees | Instalments, payments, receipts | Never sees fees | See dues, pay by UPI, receipts |
| Notices | To everyone or a class | To own classes | See notices |
| Promote, audit log, settings | Yes | - | - |

### How results are calculated

- **Percentage** = marks obtained / total maximum of the subjects that apply to the
  student x 100. "AB" (absent) counts as 0.
- **Ranks**: ties share a rank (1, 1, 3). Only students with marks are ranked.
- **Leaderboard**: based on the latest published exam of the class. It shows the top 10,
  plus "your rank", with names as "Aarav S.".

## 4. Put it online for free (Phase 8)

The free setup uses three services. Free plans change, so check their current limits.

| Piece | Service | Why |
|---|---|---|
| App | **Render** (free web service) | Runs `python run.py`, gives HTTPS |
| Database | **Neon** (free PostgreSQL) | Render's own free database expires |
| Files | **Supabase Storage** (free) | Render's disk is wiped on every restart |

**Steps:**

1. **Neon**: create a project and copy the connection string
   (`postgresql://...neon.tech/neondb?sslmode=require`).
2. **Supabase**: create a project and a **private** bucket called `paramount-files`.
   From Project Settings > API, copy the project URL and the `service_role` key.
3. **GitHub**: push this folder to a private repository. `.gitignore` already
   excludes `instance/` and `.env`.
4. **Render**: choose New > Blueprint and pick the repository. `render.yaml` sets
   everything up. Paste `DATABASE_URL`, `SUPABASE_URL` and `SUPABASE_SERVICE_KEY`
   when asked.
5. Open the Render **Logs** and copy the admin one-time password from the box. Then
   open your `https://....onrender.com` address.

**Things to know about the free tier:**
- **Cold starts.** The app sleeps after about 15 minutes without visitors, and the
  next visit takes 30-60 seconds. A free uptime pinger (for example
  cron-job.org) calling `/healthz` every 10 minutes keeps it awake during school hours.
- **Production checks.** In production (`APP_ENV=production`) the app refuses to start
  without a strong `SECRET_KEY`, PostgreSQL and Supabase. This is deliberate.
- **Backups.** Neon keeps a short restore history on the free plan. Also export your
  data regularly.

## 5. Android and iPhone

- **Android APK.** Open https://www.pwabuilder.com, enter your live URL, then choose
  Package for Android. You get a signed APK to share directly; publishing on
  Google Play costs a one-time developer fee. The APK opens your live site
  full-screen, so updates need no new APK.
- **iPhone.** Open the site in Safari, tap Share, then **Add to Home Screen**. It runs
  full-screen with offline support. An App Store version would need an Apple Developer
  account and a Mac.
- **Android browser.** Chrome shows an "Install app" button in the top bar.

## 6. Offline (students)

- Pages a student has opened recently (home, results, ranks, fees, notes lists) still
  open without internet. Pages never opened show an "You're offline" page.
- "Save offline" on a note keeps the PDF/image on the phone. The saved files are listed
  under Courses > Saved on this phone.
- **Logout** removes all saved pages and files from the phone (important on shared phones).
- Staff pages are never cached.

## 7. Security summary

- Passwords are hashed with PBKDF2-SHA256 (600,000 iterations).
- One-time passwords must be changed at first login. New passwords need 8+ characters
  with letters and numbers.
- Login is limited to 5 failed attempts per ID per minute.
- Changing or resetting a password logs that account out on every device.
- The session is a signed cookie: HttpOnly, SameSite=Lax, and Secure in production.
- Every POST carries a CSRF token. The Content Security Policy allows only the app's
  own scripts and styles, with no inline code.
- Uploads are checked by their real file signature (PDF/JPG/PNG/WEBP), not the file
  name, and limited to 10 MB. Files are served only to users allowed to see them.
- The audit log records changes to marks, fees, passwords, subjects, people, promotion
  and settings.

## 8. Known limits (by design, for a single small server)

- **In-memory state.** The login rate limit and the 15-minute one-time password lists
  live in server memory. This is fine for one server process (the free plan); they
  reset on restart.
- **No migrations.** Tables are created with `create_all`. If a future change adds
  columns, it will come with a small upgrade script.
- **Supabase not tested here.** The Supabase storage code follows Supabase's REST API but
  was not tested against a live project during development. Upload one file after
  deploying to confirm your keys.

## 9. Project layout

```
paramount-lite/
  run.py                    start the server (also used by Render)
  requirements.txt  render.yaml  .env.example
  app/
    main.py                 app setup, security headers (CSP), error pages, PWA files
    config.py               settings + constants (classes, rules, dates)
    db.py  models.py        database engine, sessions, all 16 tables
    security.py             password hashing, one-time passwords, CSRF, rate limit
    deps.py                 login/role checks used by routes
    services.py             business rules (results, ranks, fees, import, promote...)
    storage.py              note files: local disk or Supabase Storage
    qr.py                   UPI QR code generator (pure Python, no packages)
    templating.py           render(), toasts, navigation, filters (rupees, dates)
    seed.py                 first-run data (admin, settings, default subjects)
    routes/                 every file with a `router` is loaded automatically
      auth.py               login, logout, change password
      admin.py              dashboard, settings, audit log
      admin_people.py       students, teachers, import, passwords, promote
      admin_subjects.py     subjects
      admin_fees.py         fees, payments, receipts
      staff.py              teacher home, student list/profile, notices
      notes.py              course notes + file download
      exams.py              exams, marks grid autosave, publish
      student.py            all student pages
    templates/              Jinja2 pages: base.html (layout), macros.html (UI parts)
    static/                 app.css, app.js, theme.js, sw.js, manifest, offline.html, icons
  tests/smoke_test.py  tests/full_test.py
```

**Notes for a .NET developer:**
- **`routes/*.py`** are the controllers. `services.py` is the service layer.
- **`models.py`** holds the EF-style entities. A `Session` from `deps.DbSession` is the `DbContext`.
- **Dependencies** like `AdminUser` and `StaffUser` in function parameters work like
  `[Authorize(Roles=...)]`.
- **`render()`** returns a view; `redirect()` is `RedirectToAction` plus TempData (toast).
- **No inline JavaScript.** Templates add `data-*` attributes and `app.js` attaches
  the behaviour. That's why the strict CSP works.
