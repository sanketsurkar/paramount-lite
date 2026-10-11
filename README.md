# The Paramount Academy App (`paramount-lite`)

An installable mobile web app (PWA) for The Paramount Academy. Features include course notes, exams and marks, results with progress charts, class leaderboards, fees with UPI payments and receipts, notices, and offline access for students.

**Tech stack:** Python 3.12+, FastAPI, SQLAlchemy 2.0, Jinja2 (server-rendered HTML), one CSS file, one JavaScript file, and a service worker. No Node.js, build step, or CDN required.

---

## 1. Run It on Your Laptop (Windows / PowerShell)

Open PowerShell in the project directory:

```powershell
cd paramount-lite
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

If virtual-environment activation fails with a message that script execution is disabled, run the following command once:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

On the first start, the console prints the administrator login credentials.

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), log in, and choose your own password.

### Useful commands

| Command | Purpose |
|---|---|
| `python run.py` | Start the application with auto-reload |
| `python run.py --lan` | Make the application accessible from a phone on the same Wi-Fi network |
| `python -m app.seed --reset-admin` | Generate a new one-time password for `ADM-001` if locked out |
| `python tests/smoke_test.py` | Run the quick login and security tests |
| `python tests/full_test.py` | Run the full feature tests for admin, teacher, and student roles |

The tests use a temporary database so that the real application data is not modified.

To start again with an empty database, stop the server and delete the `instance` folder. **This permanently removes the local database stored there**, so back it up first if it contains data you need.

---

## 2. First-Day Setup (Administrator)

1. **Settings:** Configure the academy name, address, phone number, UPI ID, and which classes display a leaderboard.
2. **Teachers:** Add teachers, assign their classes, or import teachers from Excel. Generate a printable list of login IDs and one-time passwords.
3. **Students:** Add students individually or import them using the template available on the student import page.
4. **Subjects:** Each class starts with English, Mathematics, and Science. Add more subjects as needed and configure streams for Classes 11–12.
5. **Fees:** Create an instalment for a class and record payments as they arrive.

---

## 3. What Each Role Can Do

| Feature | Administrator | Teacher | Student / Parent |
|---|---|---|---|
| Students | Add, edit, import, reset passwords, deactivate | View assigned classes | — |
| Teachers | Add, edit, import, assign classes | — | — |
| Subjects | Add, rename, reorder, copy, archive, restore | — | View assigned subjects |
| Course notes | Manage all classes | Manage assigned classes | Open and save offline |
| Exams and marks | Manage and publish/unpublish | Create exams and enter marks for assigned classes | View published results |
| Results and ranks | — | — | View marks, percentage, rank, remarks, trends, and leaderboard |
| Fees | Manage instalments, payments, and receipts | No access to fees | View dues, pay by UPI, view receipts |
| Notices | Send to everyone or a class | Send to assigned classes | View notices |
| Promotion, audit log, settings | Full access | — | — |

### How Results Are Calculated

- **Percentage:** Marks obtained divided by the total maximum marks of applicable subjects, multiplied by 100. An absent (`AB`) result counts as zero.
- **Ranks:** Students with equal marks share a rank. For example, `1, 1, 3`. Only students with marks are ranked.
- **Leaderboard:** Based on the latest published exam for the class. It displays the top 10 students and the current student's rank, with names displayed in a shortened format such as `Aarav S.`.

---

## 4. Deploy Online for Free (Phase 8)

The proposed deployment uses three services. Free-plan limits and features can change, so verify the current terms before deployment.

| Component | Service | Purpose |
|---|---|---|
| Application | [Render](https://render.com/) | Hosts the Python application and provides HTTPS |
| Database | [Neon](https://neon.tech/) | Provides PostgreSQL database hosting |
| File storage | [Supabase Storage](https://supabase.com/) | Stores uploaded files persistently |

### Deployment Steps

1. **Neon:** Create a project and copy the PostgreSQL connection string. It typically resembles `postgresql://...neon.tech/neondb?sslmode=require`.
2. **Supabase:** Create a project and a private bucket named `paramount-files`. Copy the project URL and service-role key from the project settings.
3. **GitHub:** Push the project to a private repository. The `.gitignore` file should exclude `instance/` and `.env`.
4. **Render:** Create a new Blueprint and select the repository. If the project contains a valid `render.yaml`, Render can use it to configure the service. Provide the required environment variables, including `DATABASE_URL`, `SUPABASE_URL`, and `SUPABASE_SERVICE_KEY`.
5. **Verify deployment:** Review the Render logs and application health. Complete the initial administrator setup using the deployment's documented credential-generation process.

**Security:** Never commit `.env` files, database passwords, service-role keys, secret keys, or real administrator passwords to GitHub. Store deployment secrets in Render's environment settings.

### Free-Tier Considerations

- **Cold starts:** Free web services may sleep after inactivity. The first request may take longer while the service starts.
- **Production configuration:** The application is expected to require a strong `SECRET_KEY`, PostgreSQL, and Supabase when `APP_ENV=production`.
- **Backups:** Check your database provider's backup and restore capabilities, and export important data regularly.
- **Persistent files:** Verify that uploads are stored in Supabase rather than temporary local storage after deployment.

---

## 5. Android and iPhone

- **Android APK:** Visit [PWABuilder](https://www.pwabuilder.com/), enter the deployed URL, and review the Android packaging options. Signing and distribution requirements may apply.
- **iPhone:** Open the deployed website in Safari, tap **Share**, and select **Add to Home Screen**.
- **Android browser:** Chrome may offer an **Install app** option for a compatible PWA.

A home-screen installation opens the web app like an application. Updates to the hosted website generally do not require reinstalling it, although cached resources may need refreshing.

---

## 6. Offline Access for Students

- Recently opened student pages, including home, results, ranks, fees, and notes lists, may remain available offline.
- Pages that were never cached display an offline page when there is no connection.
- The **Save offline** option stores supported note files on the device.
- Saved files are listed under **Courses → Saved on this phone**.
- Logging out removes saved pages and files from the device, according to the application's offline-storage implementation.
- Staff pages are not cached.

Offline functionality depends on the service worker, browser support, and which resources have already been cached.

---

## 7. Security Summary

The application is designed with the following security measures:

- Password hashing using PBKDF2-SHA256 with 600,000 iterations.
- One-time passwords that must be changed at first login.
- Password requirements of at least eight characters, including letters and numbers.
- Rate limiting for repeated failed login attempts.
- Password changes and resets that invalidate existing sessions.
- Signed session cookies using `HttpOnly`, `SameSite=Lax`, and `Secure` in production.
- CSRF protection for POST requests.
- A Content Security Policy restricting scripts and styles to the application's own resources.
- File-upload validation based on file signatures, with supported formats and a 10 MB size limit.
- Access controls for uploaded files.
- Audit logging for sensitive changes involving marks, fees, passwords, subjects, people, promotion, and settings.

Review and test these controls in the deployed environment before using real student or payment data.

---

## 8. Known Limitations

- **In-memory state:** Login rate limits and temporary one-time-password tracking are held in server memory and reset when the process restarts.
- **Database migrations:** Tables are created using SQLAlchemy's `create_all`. Schema changes may require explicit upgrade scripts.
- **Supabase integration:** Test file uploads, downloads, permissions, and error handling against a real Supabase project before production use.
- **Single-server design:** In-memory controls may not be shared between multiple application workers.
- **Offline limitations:** Not every page or action is available without an internet connection.

---

## 9. Project Layout

```text
paramount-lite/
├── run.py
├── requirements.txt
├── render.yaml
├── .env.example
├── app/
│   ├── main.py
│   ├── config.py
│   ├── db.py
│   ├── models.py
│   ├── security.py
│   ├── deps.py
│   ├── services.py
│   ├── storage.py
│   ├── qr.py
│   ├── templating.py
│   ├── seed.py
│   ├── routes/
│   │   ├── auth.py
│   │   ├── admin.py
│   │   ├── admin_people.py
│   │   ├── admin_subjects.py
│   │   ├── admin_fees.py
│   │   ├── staff.py
│   │   ├── notes.py
│   │   ├── exams.py
│   │   └── student.py
│   ├── templates/
│   │   ├── base.html
│   │   └── macros.html
│   └── static/
│       ├── app.css
│       ├── app.js
│       ├── theme.js
│       ├── sw.js
│       ├── manifest
│       ├── offline.html
│       └── icons/
└── tests/
    ├── smoke_test.py
    └── full_test.py
```

---

## Notes for .NET Developers

- `routes/*.py` serve a role similar to ASP.NET Core controllers.
- `services.py` contains application and business logic.
- `models.py` contains SQLAlchemy ORM models, similar in purpose to Entity Framework entities.
- `deps.py` provides dependencies such as database sessions and role checks.
- A database `Session` is conceptually similar to an Entity Framework `DbContext`, although their lifecycles and APIs differ.
- `render()` returns a server-rendered view, similar in purpose to returning a view from an MVC controller.
- Templates use `data-*` attributes, and `app.js` attaches client-side behavior instead of relying on inline JavaScript. This supports the application's strict Content Security Policy.

---

## License and Contributions

Add your chosen license and contribution guidelines before distributing the project publicly.
