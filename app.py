import os, csv, io, json, secrets, sqlite3, smtplib
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, jsonify, make_response
from flask_mail import Mail, Message
from werkzeug.security import check_password_hash
from dotenv import load_dotenv

load_dotenv()

from integrations import gammadyne
app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.secret_key = os.getenv("FLASK_SECRET_KEY", "change-this-development-key")
_mail_port = int(os.getenv("SMTP_PORT", "587"))
app.config.update(
    MAIL_SERVER=os.getenv("SMTP_HOST", ""),
    MAIL_PORT=_mail_port,
    MAIL_USE_SSL=_mail_port == 465,
    MAIL_USE_TLS=_mail_port != 465,
    MAIL_USERNAME=os.getenv("SMTP_USERNAME", ""),
    MAIL_PASSWORD=os.getenv("SMTP_PASSWORD", ""),
    MAIL_DEFAULT_SENDER=(
        os.getenv("SMTP_FROM")
        or os.getenv("SMTP_USERNAME")
        or "nwekee125@gmail.com"
    ),
)
mail = Mail(app)
DB_PATH = os.getenv("DATABASE_PATH", "phishlab.db")
BASE_URL = os.getenv("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
ALLOWED_RECIPIENTS = {x.strip().lower() for x in os.getenv("TEST_RECIPIENTS", "").split(",") if x.strip()}
DELIVERY_MODE = os.getenv("DELIVERY_MODE", "simulation").strip().lower()
SIMULATION_LOG_PATH = os.getenv("SIMULATION_LOG_PATH", "simulation_outbox.log")
SIMULATION_NOTIFICATION_RECIPIENT = os.getenv("NOTIFICATION_RECIPIENT", "nwekee125@gmail.com")
if DELIVERY_MODE not in ("smtp", "simulation"):
    raise RuntimeError(f"DELIVERY_MODE must be 'smtp' or 'simulation', got {DELIVERY_MODE!r}")

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS recipients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
            email TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            sent_at TEXT,
            UNIQUE(campaign_id, email)
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient_id INTEGER NOT NULL REFERENCES recipients(id),
            event_type TEXT NOT NULL CHECK(event_type IN ('open','click','submission_attempt')),
            created_at TEXT NOT NULL
        );
        """)

EVENT_TYPES = (
    "open", "click", "submission_attempt",
    "simulation_visit", "email_stage", "password_stage", "simulation_completed",
)

def migrate_db():
    with db() as con:
        row = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='events'").fetchone()
        if row and "simulation_visit" not in row["sql"]:
            con.executescript(f"""
            BEGIN TRANSACTION;
            ALTER TABLE events RENAME TO events_old;
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recipient_id INTEGER NOT NULL REFERENCES recipients(id),
                event_type TEXT NOT NULL CHECK(event_type IN ({",".join("'" + t + "'" for t in EVENT_TYPES)})),
                created_at TEXT NOT NULL
            );
            INSERT INTO events (id, recipient_id, event_type, created_at)
                SELECT id, recipient_id, event_type, created_at FROM events_old;
            DROP TABLE events_old;
            COMMIT;
            """)

def migrate_delivery_jobs():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS delivery_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id INTEGER NOT NULL REFERENCES campaigns(id),
            provider TEXT NOT NULL CHECK(provider IN ('smtp','gammadyne')),
            status TEXT NOT NULL CHECK(status IN ('dry_run','blocked','error','approved','running','completed','failed')),
            dry_run INTEGER NOT NULL DEFAULT 1,
            recipient_count INTEGER NOT NULL DEFAULT 0,
            command TEXT,
            exit_code INTEGER,
            stdout TEXT,
            stderr TEXT,
            error TEXT,
            requested_by TEXT,
            approved_by TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );
        -- Only one non-dry-run job may ever reach approved/running/completed per
        -- campaign+provider, so a campaign can't be re-approved and double-sent.
        -- Failed/blocked/error live attempts are excluded so a fixed config can retry.
        CREATE UNIQUE INDEX IF NOT EXISTS idx_delivery_jobs_live_unique
            ON delivery_jobs(campaign_id, provider)
            WHERE dry_run=0 AND status IN ('approved','running','completed');
        """)

def _record_delivery_job(campaign_id, provider, result, requested_by=None, approved_by=None):
    with db() as con:
        con.execute("""
            INSERT INTO delivery_jobs
              (campaign_id, provider, status, dry_run, recipient_count, command, exit_code,
               stdout, stderr, error, requested_by, approved_by, created_at, started_at, finished_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            campaign_id, provider, result.status, int(result.dry_run), result.recipient_count,
            json.dumps(result.command) if result.command else None, result.exit_code,
            (result.stdout or None), (result.stderr or None), result.error,
            requested_by, approved_by, now(), result.started_at, result.finished_at,
        ))

def migrate_event_details():
    with db() as con:
        cols = {row["name"] for row in con.execute("PRAGMA table_info(events)")}
        if "detail" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN detail TEXT")
        if "flag" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN flag INTEGER")
        if "detail2" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN detail2 TEXT")
        if "status" not in cols:
            con.execute("ALTER TABLE events ADD COLUMN status TEXT")
            # Backfill: every completed exercise before this column existed ran
            # in the default simulation delivery mode, where notification send
            # never fails, so treating historical rows as 'success' is accurate.
            con.execute("UPDATE events SET status='success' WHERE event_type='simulation_completed' AND status IS NULL")

init_db()
migrate_db()
migrate_delivery_jobs()
migrate_event_details()

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper

def record_event(token, event_type, created_at=None, detail=None, detail2=None, status=None):
    with db() as con:
        recipient = con.execute("SELECT id FROM recipients WHERE token=?", (token,)).fetchone()
        if not recipient:
            return False
        con.execute("INSERT INTO events(recipient_id,event_type,created_at,detail,detail2,status) VALUES(?,?,?,?,?,?)",
                    (recipient["id"], event_type, created_at or now(), detail, detail2, status))
        return True

def ensure_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token

@app.context_processor
def inject_csrf_token():
    return {"csrf_token": ensure_csrf_token()}

@app.context_processor
def inject_admin_chrome():
    if not session.get("admin"):
        return {}
    with db() as con:
        captured_count = con.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='simulation_completed'"
        ).fetchone()[0]
    return {"admin_username": ADMIN_USER, "captured_count": captured_count, "delivery_mode": DELIVERY_MODE}

CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

@app.before_request
def enforce_csrf():
    if request.method in CSRF_SAFE_METHODS or request.endpoint in (None, "static"):
        return
    expected = session.get("csrf_token")
    submitted = request.headers.get("X-CSRFToken")
    if submitted is None:
        submitted = (request.get_json(silent=True) or {}).get("csrf_token") if request.is_json else request.form.get("csrf_token")
    if not expected or not submitted or not secrets.compare_digest(str(submitted), str(expected)):
        abort(400, description="Missing or invalid CSRF token.")

@app.route("/")
def home():
    if session.get("admin"):
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if ADMIN_HASH and secrets.compare_digest(username, ADMIN_USER) and check_password_hash(ADMIN_HASH, password):
            session.clear()
            session["admin"] = True
            return redirect(url_for("dashboard"))
        flash("Invalid administrator credentials.", "error")
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/dashboard")
@login_required
def dashboard():
    with db() as con:
        campaigns = con.execute("""
          SELECT c.*,
          (SELECT COUNT(*) FROM recipients r WHERE r.campaign_id=c.id) AS recipient_count,
          (SELECT COUNT(*) FROM recipients r WHERE r.campaign_id=c.id AND r.sent_at IS NOT NULL) AS delivered,
          (SELECT COUNT(*) FROM events e JOIN recipients r ON r.id=e.recipient_id WHERE r.campaign_id=c.id AND e.event_type='open') AS opens,
          (SELECT COUNT(*) FROM events e JOIN recipients r ON r.id=e.recipient_id WHERE r.campaign_id=c.id AND e.event_type='click') AS clicks,
          (SELECT COUNT(*) FROM events e JOIN recipients r ON r.id=e.recipient_id WHERE r.campaign_id=c.id AND e.event_type='submission_attempt') AS submissions,
          (SELECT COUNT(DISTINCT e.recipient_id) FROM events e JOIN recipients r ON r.id=e.recipient_id WHERE r.campaign_id=c.id AND e.event_type='simulation_visit') AS sim_visits,
          (SELECT COUNT(DISTINCT e.recipient_id) FROM events e JOIN recipients r ON r.id=e.recipient_id WHERE r.campaign_id=c.id AND e.event_type='email_stage') AS email_stage,
          (SELECT COUNT(DISTINCT e.recipient_id) FROM events e JOIN recipients r ON r.id=e.recipient_id WHERE r.campaign_id=c.id AND e.event_type='password_stage') AS password_stage,
          (SELECT COUNT(DISTINCT e.recipient_id) FROM events e JOIN recipients r ON r.id=e.recipient_id WHERE r.campaign_id=c.id AND e.event_type='simulation_completed') AS completed
          FROM campaigns c ORDER BY c.id DESC
        """).fetchall()
        recent = con.execute("""
          SELECT e.event_type,e.created_at,c.name,r.email
          FROM events e JOIN recipients r ON r.id=e.recipient_id
          JOIN campaigns c ON c.id=r.campaign_id ORDER BY e.id DESC LIMIT 12
        """).fetchall()
        gammadyne_jobs = con.execute("""
          SELECT dj.*, c.name AS campaign_name
          FROM delivery_jobs dj JOIN campaigns c ON c.id = dj.campaign_id
          WHERE dj.provider='gammadyne'
          ORDER BY dj.id DESC LIMIT 25
        """).fetchall()
        daily_rows = con.execute("""
          SELECT date(created_at) AS day, COUNT(*) AS n
          FROM events WHERE event_type='simulation_completed'
            AND date(created_at) >= date('now','-6 days')
          GROUP BY date(created_at)
        """).fetchall()
    counts_by_day = {r["day"]: r["n"] for r in daily_rows}
    chart_labels, chart_counts = [], []
    for i in range(6, -1, -1):
        day = (datetime.now(timezone.utc) - timedelta(days=i)).date()
        chart_labels.append(day.strftime("%a"))
        chart_counts.append(counts_by_day.get(day.isoformat(), 0))
    return render_template("dashboard.html", campaigns=campaigns, recent=recent,
                            gammadyne_jobs=gammadyne_jobs,
                            chart_labels=chart_labels, chart_counts=chart_counts,
                            gammadyne_configured=bool(gammadyne.GAMMADYNE_EXE_PATH))

SUBMISSION_WINDOWS = {"7": 7, "30": 30, "90": 90, "all": None}
SUBMISSIONS_PAGE_SIZE = 10

def _submission_window_clauses(days):
    window = SUBMISSION_WINDOWS.get(days, 30)
    if window is None:
        return "1=1", "1=0", None
    current = f"date(e.created_at) >= date('now','-{window} days')"
    previous = (f"date(e.created_at) >= date('now','-{window*2} days') "
                f"AND date(e.created_at) < date('now','-{window} days')")
    return current, previous, window

def _pct_change(curr, prev):
    if not prev:
        return None
    return round((curr - prev) / prev * 100)

def _submission_rows(con, where_clause, campaign_id=None, limit=200, offset=0):
    campaign_clause = f"AND r.campaign_id={int(campaign_id)}" if campaign_id else ""
    return con.execute(f"""
      SELECT e.id, e.created_at, e.detail AS submitted_email, e.detail2 AS confirm_email, e.status,
             c.name AS campaign_name, c.subject AS campaign_subject, r.email AS recipient_email
      FROM events e JOIN recipients r ON r.id=e.recipient_id
      JOIN campaigns c ON c.id=r.campaign_id
      WHERE e.event_type='simulation_completed' {campaign_clause} AND {where_clause}
      ORDER BY e.id DESC LIMIT {limit} OFFSET {offset}
    """).fetchall()

@app.route("/submissions")
@login_required
def submissions():
    days = request.args.get("days", "30")
    if days not in SUBMISSION_WINDOWS:
        days = "30"
    campaign_id = request.args.get("campaign_id", type=int)
    page = max(1, request.args.get("page", 1, type=int))
    where_current, where_previous, _ = _submission_window_clauses(days)
    campaign_clause = f"AND r.campaign_id={campaign_id}" if campaign_id else ""
    with db() as con:
        campaign = None
        if campaign_id:
            campaign = con.execute("SELECT id, name FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not campaign:
                abort(404)

        def count(extra=""):
            return con.execute(f"""
              SELECT COUNT(*) FROM events e JOIN recipients r ON r.id=e.recipient_id
              WHERE e.event_type='simulation_completed' {campaign_clause} AND {extra}
            """).fetchone()[0]

        total_curr = count(where_current)
        total_prev = count(where_previous)
        success_curr = count(where_current + " AND e.status='success'")
        success_prev = count(where_previous + " AND e.status='success'")
        failed_curr = count(where_current + " AND e.status='failed'")
        failed_prev = count(where_previous + " AND e.status='failed'")
        last_row = con.execute(f"""
          SELECT e.created_at FROM events e JOIN recipients r ON r.id=e.recipient_id
          WHERE e.event_type='simulation_completed' {campaign_clause}
          ORDER BY e.id DESC LIMIT 1
        """).fetchone()
        last_submission = last_row["created_at"] if last_row else None

        total_pages = max(1, (total_curr + SUBMISSIONS_PAGE_SIZE - 1) // SUBMISSIONS_PAGE_SIZE)
        page = min(page, total_pages)
        rows = _submission_rows(con, where_current, campaign_id=campaign_id,
                                 limit=SUBMISSIONS_PAGE_SIZE, offset=(page - 1) * SUBMISSIONS_PAGE_SIZE)
    stats = {
        "total": {"value": total_curr, "pct": _pct_change(total_curr, total_prev)},
        "success": {"value": success_curr, "pct": _pct_change(success_curr, success_prev)},
        "failed": {"value": failed_curr, "pct": _pct_change(failed_curr, failed_prev)},
    }
    return render_template("submissions.html", rows=rows, stats=stats, days=days,
                            campaign=campaign, last_submission=last_submission,
                            page=page, total_pages=total_pages, total_curr=total_curr,
                            page_size=SUBMISSIONS_PAGE_SIZE)

@app.route("/submissions/export.csv")
@login_required
def export_submissions_csv():
    days = request.args.get("days", "30")
    if days not in SUBMISSION_WINDOWS:
        days = "30"
    campaign_id = request.args.get("campaign_id", type=int)
    where_current, _, _ = _submission_window_clauses(days)
    with db() as con:
        rows = _submission_rows(con, where_current, campaign_id=campaign_id, limit=10000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["recipient", "submitted_email", "confirm_email", "campaign", "status", "submitted_at"])
    writer.writerows([(r["recipient_email"], r["submitted_email"], r["confirm_email"], r["campaign_name"],
                        r["status"], r["created_at"]) for r in rows])
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv"
    response.headers["Content-Disposition"] = 'attachment; filename="submissions-report.csv"'
    return response

@app.route("/submissions/<int:event_id>")
@login_required
def submission_detail(event_id):
    with db() as con:
        row = con.execute("""
          SELECT e.id, e.created_at, e.detail AS submitted_email, e.detail2 AS confirm_email, e.status,
                 c.name AS campaign_name, c.subject AS campaign_subject, r.email AS recipient_email
          FROM events e JOIN recipients r ON r.id=e.recipient_id
          JOIN campaigns c ON c.id=r.campaign_id
          WHERE e.id=? AND e.event_type='simulation_completed'
        """, (event_id,)).fetchone()
    if not row:
        abort(404)
    return render_template("submission_detail.html", s=row)

@app.route("/campaigns/new", methods=["GET", "POST"])
@login_required
def new_campaign():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()
        emails = sorted({x.strip().lower() for x in request.form.get("recipients", "").replace(",", "\n").splitlines() if x.strip()})
        if not name or not subject or not body or not emails:
            flash("Complete every field and add at least one test recipient.", "error")
            return render_template("new_campaign.html")
        if not ALLOWED_RECIPIENTS:
            flash("No test recipients are allowlisted. Configure TEST_RECIPIENTS in .env first.", "error")
            return render_template("new_campaign.html")
        rejected = [e for e in emails if e not in ALLOWED_RECIPIENTS]
        if rejected:
            flash("Some addresses are not in TEST_RECIPIENTS. Only explicitly allowlisted test inboxes can receive mail.", "error")
            return render_template("new_campaign.html")
        with db() as con:
            cur = con.execute("INSERT INTO campaigns(name,subject,body,created_at) VALUES(?,?,?,?)",
                              (name, subject, body, now()))
            campaign_id = cur.lastrowid
            created = []
            for email in emails:
                token = secrets.token_urlsafe(24)
                con.execute("INSERT INTO recipients(campaign_id,email,token) VALUES(?,?,?)",
                            (campaign_id, email, token))
                created.append((email, token))
        sent = 0
        errors = []
        for email, token in created:
            try:
                deliver_email(email, subject, body, f"{BASE_URL}/simulation/{token}")
                with db() as con:
                    con.execute("UPDATE recipients SET sent_at=? WHERE token=?", (now(), token))
                sent += 1
            except Exception as exc:
                errors.append(f"{email}: {str(exc)[:120]}")
        mode_label = "live SMTP" if DELIVERY_MODE == "smtp" else "simulation (no network send, logged to " + SIMULATION_LOG_PATH + ")"
        flash(f"Campaign created via {mode_label}. Emails processed: {sent}/{len(created)}.", "success")
        if errors:
            flash("Delivery errors: " + " | ".join(errors), "error")
        return redirect(url_for("dashboard"))
    return render_template("new_campaign.html")

def _campaign_recipients(campaign_id):
    with db() as con:
        campaign = con.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not campaign:
            return None, []
        recipients = [(r["email"], r["token"]) for r in con.execute(
            "SELECT email, token FROM recipients WHERE campaign_id=?", (campaign_id,))]
        return campaign, recipients

@app.route("/gammadyne/dry-run", methods=["POST"])
@login_required
def gammadyne_dry_run():
    campaign_id = request.form.get("campaign_id", type=int)
    campaign, recipients = _campaign_recipients(campaign_id) if campaign_id else (None, [])
    if not campaign:
        flash("Select a valid campaign before running a Gammadyne dry run.", "error")
        return redirect(url_for("dashboard"))
    result = gammadyne.submit_job(campaign["subject"], campaign["body"], recipients, BASE_URL,
                                   ALLOWED_RECIPIENTS, dry_run=True)
    _record_delivery_job(campaign_id, "gammadyne", result, requested_by=ADMIN_USER)
    if result.status == "dry_run":
        flash(f"Gammadyne dry run OK for '{campaign['name']}': {result.recipient_count} allowlisted "
              f"recipient(s) previewed. No email was sent.", "success")
    else:
        flash(f"Gammadyne dry run failed: {result.error}", "error")
    return redirect(url_for("dashboard"))

@app.route("/gammadyne/approve", methods=["POST"])
@login_required
def gammadyne_approve():
    campaign_id = request.form.get("campaign_id", type=int)
    campaign, recipients = _campaign_recipients(campaign_id) if campaign_id else (None, [])
    if not campaign:
        flash("Select a valid campaign before approving a Gammadyne send.", "error")
        return redirect(url_for("dashboard"))
    with db() as con:
        existing_live = con.execute("""
            SELECT id FROM delivery_jobs WHERE campaign_id=? AND provider='gammadyne'
              AND dry_run=0 AND status IN ('approved','running','completed')
        """, (campaign_id,)).fetchone()
    if existing_live:
        flash(f"A Gammadyne send was already approved for '{campaign['name']}'. "
              f"Refusing to submit a duplicate.", "error")
        return redirect(url_for("dashboard"))
    result = gammadyne.submit_job(campaign["subject"], campaign["body"], recipients, BASE_URL,
                                   ALLOWED_RECIPIENTS, dry_run=False)
    _record_delivery_job(campaign_id, "gammadyne", result, requested_by=ADMIN_USER, approved_by=ADMIN_USER)
    if result.status == "completed":
        flash(f"Gammadyne job completed for '{campaign['name']}' (exit code {result.exit_code}).", "success")
    else:
        flash(f"Gammadyne job not sent: {result.error or result.stderr}", "error")
    return redirect(url_for("dashboard"))

def build_training_message(to_addr, subject, body, tracking_url):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("SMTP_FROM", os.getenv("SMTP_USERNAME", ""))
    msg["To"] = to_addr
    safe_body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")
    msg.set_content(body + "\n\nOpen the authorized awareness exercise: " + tracking_url)
    msg.add_alternative(
        f"""<html><body style="font-family:Arial,sans-serif">
        <div style="max-width:600px;margin:auto;padding:24px">
        <p style="font-size:12px;color:#64748b">AUTHORIZED SECURITY AWARENESS EXERCISE</p>
        <div>{safe_body}</div>
        <p><a href="{tracking_url}">Continue to the training exercise</a></p>
        <hr><small>This message is part of an approved security-awareness test.</small>
        </div></body></html>""", subtype="html")
    return msg

def send_via_smtp(msg):
    host = os.getenv("SMTP_HOST")
    if not host:
        raise RuntimeError("SMTP_HOST is not configured.")
    port = int(os.getenv("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=20) as server:
        server.starttls()
        username = os.getenv("SMTP_USERNAME")
        password = os.getenv("SMTP_PASSWORD")
        if username and password:
            server.login(username, password)
        server.send_message(msg)

def record_simulated_send(msg):
    with open(SIMULATION_LOG_PATH, "a", encoding="utf-8") as fh:
        fh.write(f"--- simulated send {now()} (no network delivery) ---\n")
        fh.write(msg.as_string())
        fh.write("\n\n")
    if isinstance(msg, EmailMessage):
        recipient, subject = msg["To"], msg["Subject"]
    else:
        recipient, subject = ", ".join(msg.recipients), msg.subject
    app.logger.info("Simulated send to %s: subject=%r", recipient, subject)

def deliver_email(to_addr, subject, body, tracking_url):
    if to_addr.lower() not in ALLOWED_RECIPIENTS:
        raise PermissionError(f"{to_addr} is not on the TEST_RECIPIENTS allowlist.")
    msg = build_training_message(to_addr, subject, body, tracking_url)
    if DELIVERY_MODE == "smtp":
        send_via_smtp(msg)
    else:
        record_simulated_send(msg)

def send_simulation_notification(email, confirm_email, timestamp, event_status):
    if DELIVERY_MODE == "smtp":
        if not app.config["MAIL_USERNAME"] or not app.config["MAIL_PASSWORD"]:
            raise RuntimeError("SMTP_USERNAME and SMTP_PASSWORD must be configured for notifications.")

    message = Message(
        subject="New Form Submission from Flask App",
        sender=app.config["MAIL_DEFAULT_SENDER"],
        recipients=[SIMULATION_NOTIFICATION_RECIPIENT],
        body=(
            f"Submitted email address: {email}\n"
            f"Confirmed email address: {confirm_email}\n"
            f"Submission timestamp (UTC): {timestamp}\n"
            f"Simulation event status: {event_status}\n"
        ),
    )
    if DELIVERY_MODE == "smtp":
        mail.send(message)
    else:
        record_simulated_send(message)

@app.route("/track/<token>")
def track(token):
    if not record_event(token, "click"):
        abort(404)
    return render_template("landing.html", token=token)

@app.route("/track/<token>/open.gif")
def open_pixel(token):
    record_event(token, "open")
    # Transparent 1x1 GIF. Email clients may suppress remote images.
    pixel = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
    response = make_response(pixel)
    response.headers["Content-Type"] = "image/gif"
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return response

@app.route("/track/<token>/submit", methods=["POST"])
def submit_attempt(token):
    if not record_event(token, "submission_attempt"):
        abort(404)
    # Deliberately discard all submitted fields. Never persist or log their values.
    return render_template("training_complete.html")

SIMULATION_EVENT_TYPES = {"email_stage", "password_stage", "simulation_completed"}

@app.route("/simulation/<token>")
def simulation(token):
    with db() as con:
        recipient = con.execute("SELECT id FROM recipients WHERE token=?", (token,)).fetchone()
    if not recipient:
        abort(404)
    record_event(token, "simulation_visit")
    return render_template("simulation.html", token=token)

@app.route("/simulation-event", methods=["POST"])
def simulation_event():
    payload = request.get_json(silent=True) or {}
    token = payload.get("token")
    event_type = payload.get("event")
    if (
        not isinstance(token, str)
        or not token
        or not isinstance(event_type, str)
        or event_type not in SIMULATION_EVENT_TYPES
    ):
        abort(400)

    email = None
    confirm_email = None
    if event_type == "simulation_completed":
        email = payload.get("email")
        confirm_email = payload.get("confirm_email")
        if (
            not isinstance(email, str)
            or not email.strip()
            or len(email) > 320
            or not isinstance(confirm_email, str)
            or not confirm_email.strip()
            or len(confirm_email) > 320
        ):
            abort(400)
        email = email.strip()
        confirm_email = confirm_email.strip()

    event_timestamp = now()
    detail = email if event_type == "simulation_completed" else None
    detail2 = confirm_email if event_type == "simulation_completed" else None
    notify_status = None
    notify_failed = False
    if event_type == "simulation_completed":
        try:
            send_simulation_notification(
                email,
                confirm_email,
                event_timestamp,
                event_type,
            )
            notify_status = "success"
        except Exception:
            app.logger.exception("Failed to send simulation completion notification.")
            notify_status = "failed"
            notify_failed = True
    if not record_event(token, event_type, event_timestamp, detail=detail, detail2=detail2, status=notify_status):
        abort(404)
    if notify_failed:
        return jsonify(status="error", message="Notification delivery failed."), 502
    return jsonify(status="ok")

@app.route("/api/events")
@login_required
def api_events():
    after_id = int(request.args.get("after", "0"))
    with db() as con:
        rows = con.execute("""
          SELECT e.id,e.event_type,e.created_at,e.detail,e.detail2,c.name,r.email
          FROM events e JOIN recipients r ON r.id=e.recipient_id
          JOIN campaigns c ON c.id=r.campaign_id
          WHERE e.id>? ORDER BY e.id ASC LIMIT 100
        """, (after_id,)).fetchall()
        totals = con.execute("""
          SELECT
          (SELECT COUNT(*) FROM events WHERE event_type='open') AS opens,
          (SELECT COUNT(*) FROM events WHERE event_type='click') AS clicks,
          (SELECT COUNT(*) FROM events WHERE event_type='submission_attempt') AS submissions,
          (SELECT COUNT(DISTINCT recipient_id) FROM events WHERE event_type='simulation_visit') AS sim_visits,
          (SELECT COUNT(DISTINCT recipient_id) FROM events WHERE event_type='email_stage') AS email_stage,
          (SELECT COUNT(DISTINCT recipient_id) FROM events WHERE event_type='password_stage') AS password_stage,
          (SELECT COUNT(DISTINCT recipient_id) FROM events WHERE event_type='simulation_completed') AS completed
        """).fetchone()
    return jsonify(events=[dict(r) for r in rows], totals=dict(totals))

@app.route("/campaigns/<int:campaign_id>/export.csv")
@login_required
def export_csv(campaign_id):
    with db() as con:
        campaign = con.execute("SELECT name FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not campaign:
            abort(404)
        rows = con.execute("""
          SELECT r.email,r.sent_at,e.event_type,e.created_at
          FROM recipients r LEFT JOIN events e ON e.recipient_id=r.id
          WHERE r.campaign_id=? ORDER BY r.email,e.id
        """, (campaign_id,)).fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["recipient", "email_sent_at", "event", "event_at"])
    writer.writerows([tuple(r) for r in rows])
    response = make_response(output.getvalue())
    response.headers["Content-Type"] = "text/csv"
    response.headers["Content-Disposition"] = f'attachment; filename="campaign-{campaign_id}-report.csv"'
    return response

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
