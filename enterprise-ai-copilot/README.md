# Enterprise AI Co-Pilot System

A Flask-based enterprise platform: AI chat assistant with **RAG over your own documents**, document intelligence,
knowledge base, employee & task management, analytics, PDF reports, notifications and audit logs.
Works on **SQLite (zero setup)** or **MySQL 8**.

| Layer | Technology |
|---|---|
| Backend | Python 3.10–3.12, Flask, Flask-SQLAlchemy, Flask-Login, Flask-WTF (CSRF), Flask-Migrate |
| Database | SQLite (dev) → **MySQL 8 via PyMySQL** (switch with one line in `.env`) |
| AI | Google **Gemini** (REST), **Sentence-Transformers** embeddings, **FAISS** vector search, RAG |
| Documents | pypdf, python-docx, pandas, ReportLab |
| Frontend | Bootstrap 5, Chart.js, Font Awesome, vanilla JS (all via CDN, so the browser needs internet) |

---------------------------------------------------------------------------------------------------

## 1. Quick start (SQLite — 5 minutes)

```bash
# 1. Open a terminal in the project folder, then create a virtual environment
python -m venv venv
venv\Scripts\activate            # Windows
# source venv/bin/activate       # macOS / Linux

# 2. Install dependencies (sentence-transformers pulls PyTorch: first install is large, 5–10 min)
pip install -r requirements.txt

# 3. Configure
copy .env.example .env           # Windows      (macOS/Linux: cp .env.example .env)

# 4. Create tables and the first administrator
flask --app app init-db
flask --app app create-admin     # prompts for name, email and password

# 5. (optional) load clearly-flagged demo data
flask --app app seed-demo

# 6. Run
flask --app app run --debug      # open http://127.0.0.1:5000
```

No administrator yet? Opening `/login` shows a link to `/setup`, a **one-time** page that works only while no admin exists.

### Verify your installation
```bash
python scripts/smoke_test.py     # ~60 checks: login, roles, uploads, RAG, reports, CRUD, lockout
```

---------------------------------------------------------------------------------------------------

## 2. Connecting to MySQL (step by step)

**A. Install MySQL Server 8** (https://dev.mysql.com/downloads/installer/ — or XAMPP/WAMP). Remember the `root` password.

**B. Create the database and a dedicated user.** Edit the password in `database/mysql_setup.sql`
(replace `CHANGE_ME_STRONG_PASSWORD`), then run:

```bash
mysql -u root -p < database/mysql_setup.sql
```
(MySQL Workbench: *File → Open SQL Script → run*.) The script contains:
```sql
CREATE DATABASE IF NOT EXISTS enterprise_copilot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'copilot_user'@'localhost' IDENTIFIED BY 'CHANGE_ME_STRONG_PASSWORD';
GRANT ALL PRIVILEGES ON enterprise_copilot.* TO 'copilot_user'@'localhost';
FLUSH PRIVILEGES;
```

**C. Point the app at MySQL** — edit `.env`:
```ini
DB_ENGINE=mysql
MYSQL_HOST=127.0.0.1
MYSQL_PORT=3306
MYSQL_DATABASE=enterprise_copilot
MYSQL_USER=copilot_user
MYSQL_PASSWORD=the_password_you_chose
```
This builds `mysql+pymysql://copilot_user:***@127.0.0.1:3306/enterprise_copilot?charset=utf8mb4`
(`config.py → _database_uri()`). Alternatively set a full `DATABASE_URL`.

**D. Test the connection, then create the tables:**
```bash
flask --app app test-db      # "Connected to mysql. Tables (0): ..."
flask --app app init-db      # creates all 13 tables
flask --app app create-admin
```
Want to see the SQL? `flask --app app export-schema > database/schema.sql` prints the MySQL DDL generated from the models.

**E. Migrations (optional, for schema changes later):**
```bash
flask --app app db init       # once
flask --app app db migrate -m "initial"
flask --app app db upgrade
```
> Use either `init-db` **or** migrations for a given database, not both.

**Moving existing SQLite data to MySQL:** simplest path is to run `init-db` on MySQL and `seed-demo`, or export/import
with a tool such as MySQL Workbench's Data Import wizard.

**MySQL troubleshooting:** `Access denied` → wrong user/password or missing GRANT · `Can't connect` → service not running /
wrong port · `caching_sha2_password` error → `pip install cryptography` (already in requirements).

---------------------------------------------------------------------------------------------------

## 3. Enabling Gemini (live AI)

1. Create a key at https://aistudio.google.com/app/apikey
2. Put it in `.env`: `GEMINI_API_KEY=your_key` (the key never reaches the browser).
3. Restart the app. The sidebar shows **Gemini connected**.

Without a key the app runs in an **honest demo mode**: every AI output is labelled "Demo mode — not AI-generated".
Chat then shows the real retrieved passages from your documents; task helpers use simple rules; summaries are extractive.

---------------------------------------------------------------------------------------------------

## 4. Architecture

```
Browser (Bootstrap + Chart.js + fetch)
   │  HTML pages / JSON APIs (CSRF token on every POST)
Flask app (app.py: factory, error handlers, security headers)
   ├── routes/      Blueprints: auth, main, chat, documents, knowledge, employees, tasks, analytics, reports, admin
   ├── services/    Business logic (no HTTP): ai_service, rag_service, document_service, analytics_service,
   │                report_service, notification_service, audit_service, permissions, user_service, text_utils
   ├── models/      SQLAlchemy models (13 tables)
   └── SQLite / MySQL        uploads/ (random-named files, served only through permission-checked routes)
```

### How RAG works here
1. **Upload** → file validated (extension, magic bytes, size) and stored under a random name.
2. **Process** (background thread, status: processing → ready/failed): extract text (PDF per page, DOCX per heading, TXT) →
   split into ~900-character overlapping chunks → embed with Sentence-Transformers → store vectors in `document_chunks`.
3. **Index**: all vectors are loaded into a **FAISS `IndexFlatIP`** (cosine, normalised). The index rebuilds automatically
   when chunks change; if FAISS is not installed a NumPy fallback is used.
4. **Ask**: embed the question → FAISS top-200 → **filter by the asking user's permissions** → keep top-5.
5. **Generate**: retrieved passages go to Gemini as numbered context; the answer cites `[1] [2]` and the UI lists
   source names with page/section. Knowledge-base entries are indexed the same way.

If the embedding model cannot be downloaded, the app falls back to a lightweight hashing embedder (and says so on the
Documents page). Vectors remember which model made them, so switching later re-embeds automatically.

### Roles & permissions (enforced server-side)
| Action | Employee | Admin |
|---|---|---|
| Chat, upload docs, own tasks, directory (read-only) | ✅ | ✅ |
| See a document | owner, or shared with department/company | all |
| See a task | assigned to / created by them | all |
| Assign tasks to others, edit employees & departments, knowledge base, analytics, audit logs, announcements | ❌ (403) | ✅ |
| Reports | task completion (own), document summary (accessible) | all five types |

### Security features
Password hashing (PBKDF2-SHA256) · CSRF on every form and fetch call · login lockout (5 failures → 15 min) ·
generic login errors · open-redirect protection · signed 30-minute reset tokens · server-side role checks ·
upload type/size/magic-byte validation · random stored file names, no public `/uploads` route ·
audit trail of logins, failures, uploads, deletions, task/employee/report actions, permission denials ·
secrets only in `.env` · security headers (`nosniff`, `X-Frame-Options`) · cannot delete/deactivate the last admin.

---------------------------------------------------------------------------------------------------

## 5. Project structure

```
app.py  config.py  extensions.py  cli.py  requirements.txt  .env.example  README.md
models/__init__.py             all SQLAlchemy models
routes/                        auth.py main.py chat.py documents.py knowledge.py employees.py
                               tasks.py analytics.py reports.py admin.py
services/                      ai_service.py rag_service.py document_service.py analytics_service.py
                               report_service.py notification_service.py audit_service.py permissions.py
                               user_service.py text_utils.py utils.py
templates/                     base.html _macros.html + one folder per module
static/css/app.css  static/js/{app.js,charts.js}
database/mysql_setup.sql       MySQL database + user creation
scripts/smoke_test.py          end-to-end self-test
uploads/  instance/            runtime data (git-ignored)
```

## 6. Database tables
`users, departments, documents, document_chunks, conversations, messages, tasks, subtasks, notifications, reports,
audit_logs, knowledge_base, announcements` (+ `alembic_version` if you use migrations). Foreign keys with
`ON DELETE CASCADE / SET NULL`, timestamps on every table, unique constraints on e-mail and employee ID.

## 7. CLI commands
| Command | Purpose |
|---|---|
| `flask --app app init-db` | create tables |
| `flask --app app test-db` | check DB connection |
| `flask --app app create-admin` | create an administrator |
| `flask --app app seed-demo` | add demo employees/tasks/documents/KB (all flagged `is_demo`, shown with a DEMO tag) |
| `flask --app app clear-demo` | remove every demo record |
| `flask --app app export-schema` | print MySQL DDL |

Demo employees get a random password printed once by `seed-demo` (or pass `--password`). Nothing is hard-coded.

## 7b. Production notes
Set `SESSION_COOKIE_SECURE=true` behind HTTPS, set a fixed `SECRET_KEY`, use MySQL, and run with a WSGI server
(Linux: `gunicorn "app:app"`; Windows: `waitress-serve --call app:create_app`). Never run with `FLASK_DEBUG=1` in production.

## 8. Known limitations (be upfront in your viva)
* Scanned/image-only PDFs have no extractable text (no OCR).
* Document processing uses an in-process thread; for heavy load use a queue (Celery/RQ).
* The FAISS index is cached per process and rebuilt from the database, which is fine for thousands of chunks, not millions.
* Password-reset e-mail needs SMTP settings; without them the link is written to the server log (dev only).
* Tables are created with `create_all()` / migrations; there is no data-migration tool from SQLite to MySQL.

## 9. Viva cheat-sheet
* **Why RAG?** LLMs don't know private company data and hallucinate; retrieval grounds answers and enables citations.
* **Why filter after retrieval?** The vector index is global; permissions are checked per chunk before anything reaches the LLM.
* **Why chunk with overlap?** Keeps sentences that straddle a boundary retrievable.
* **Why cosine/inner-product on normalised vectors?** Measures meaning similarity independent of text length.
* **Why a fallback demo mode?** The app never pretends: output is labelled when it is not from Gemini.
* **Why SQLAlchemy?** One model layer works on SQLite and MySQL; only the URI changes.
