"""Calibration Laboratory Management System - Flask web app (local server).
Run:  python app.py   then open http://127.0.0.1:5000
"""
import base64
import csv
import hmac
import io
import json
import math
import os
import re
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from functools import wraps
from waitress import serve
import pyotp
import qrcode

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from markupsafe import Markup
from werkzeug.security import check_password_hash, generate_password_hash

from translations import NE

app = Flask(__name__)


def _load_key():
    """Random secret key, created once and kept in secret.key (do not share this file)."""
    if not os.path.exists("secret.key"):
        with open("secret.key", "w") as f:
            f.write(secrets.token_hex(32))
    with open("secret.key") as f:
        return f.read().strip()


app.secret_key = _load_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
DB = "calibration.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS stations (
    station_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, location TEXT,
    type TEXT NOT NULL DEFAULT 'Meteorological', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS sensors (
    sensor_id TEXT PRIMARY KEY,
    station_id INTEGER NOT NULL REFERENCES stations(station_id),
    sensor_type TEXT NOT NULL, manufacturer TEXT, serial_number TEXT UNIQUE NOT NULL,
    interval_days INTEGER NOT NULL DEFAULT 365, tolerance REAL NOT NULL DEFAULT 0.5,
    unit TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS calibrations (
    cal_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sensor_id TEXT NOT NULL REFERENCES sensors(sensor_id),
    cal_date TEXT NOT NULL, reference_standard TEXT NOT NULL,
    reference_value REAL NOT NULL, measured_value REAL NOT NULL, error REAL NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('PASS','FAIL')),
    certificate_no TEXT UNIQUE NOT NULL, next_due TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','technician')),
    active INTEGER NOT NULL DEFAULT 1,
    two_factor_enabled INTEGER NOT NULL DEFAULT 0,
    totp_secret TEXT,
    recovery_codes TEXT);
CREATE TABLE IF NOT EXISTS reference_standards (
    standard_id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL COLLATE NOCASE, name TEXT NOT NULL,
    standard_type TEXT, manufacturer TEXT, serial_number TEXT, uncertainty TEXT,
    traceability TEXT, certificate_no TEXT,
    calibrated_on TEXT NOT NULL, valid_until TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS calibration_points (
    point_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cal_id INTEGER NOT NULL REFERENCES calibrations(cal_id) ON DELETE CASCADE,
    point_no INTEGER NOT NULL,
    reference_value REAL NOT NULL, measured_value REAL NOT NULL, error REAL NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('PASS','FAIL')));
CREATE TABLE IF NOT EXISTS calibration_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_no TEXT UNIQUE NOT NULL,
    client_name TEXT NOT NULL,
    contact_person TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    sensor_id TEXT REFERENCES sensors(sensor_id),
    instrument_description TEXT NOT NULL,
    requested_service TEXT NOT NULL,
    requested_range TEXT,
    received_date TEXT NOT NULL,
    requested_due_date TEXT,
    priority TEXT NOT NULL DEFAULT 'Normal' CHECK (priority IN ('Low','Normal','High','Urgent')),
    condition_received TEXT,
    remarks TEXT,
    status TEXT NOT NULL DEFAULT 'RECEIVED'
        CHECK (status IN ('RECEIVED','REVIEWED','ASSIGNED','IN CALIBRATION','UNDER REVIEW','COMPLETED','CANCELLED')),
    created_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS calibration_work_orders (
    work_order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_no TEXT UNIQUE NOT NULL,
    request_id INTEGER NOT NULL UNIQUE REFERENCES calibration_requests(request_id),
    assigned_technician_id INTEGER NOT NULL REFERENCES users(user_id),
    assigned_by INTEGER NOT NULL REFERENCES users(user_id),
    assigned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    target_date TEXT,
    calibration_method TEXT,
    standard_id INTEGER REFERENCES reference_standards(standard_id),
    instructions TEXT,
    status TEXT NOT NULL DEFAULT 'ASSIGNED'
        CHECK (status IN ('ASSIGNED','IN PROGRESS','AWAITING REVIEW','COMPLETED','CANCELLED')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS calibration_review_history (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_id INTEGER NOT NULL REFERENCES calibration_work_orders(work_order_id),
    cal_id INTEGER NOT NULL REFERENCES calibrations(cal_id),
    submitted_by INTEGER NOT NULL REFERENCES users(user_id),
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_by INTEGER REFERENCES users(user_id),
    reviewed_at TEXT,
    decision TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (decision IN ('PENDING','APPROVED','RETURNED')),
    comments TEXT
);
"""

LATEST = """
SELECT s.*, st.name AS station, c.cal_date, c.reference_standard, c.reference_value,
       c.measured_value, c.error, c.result, c.certificate_no, c.next_due, c.performed_by, c.n_points
FROM sensors s JOIN stations st USING(station_id)
LEFT JOIN calibrations c ON c.cal_id = (
    SELECT MAX(cal_id) FROM calibrations WHERE sensor_id = s.sensor_id)
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()


with sqlite3.connect(DB) as _c:
    _c.executescript(SCHEMA)
    # upgrade older databases: station type and last-edited timestamp
    _station_cols = [r[1] for r in _c.execute("PRAGMA table_info(stations)")]
    if "type" not in _station_cols:
        _c.execute("ALTER TABLE stations ADD COLUMN type TEXT NOT NULL DEFAULT 'Meteorological'")
    if "updated_at" not in _station_cols:
        _c.execute("ALTER TABLE stations ADD COLUMN updated_at TEXT")
        _c.execute("UPDATE stations SET updated_at=? WHERE updated_at IS NULL",
                    (datetime.now().isoformat(timespec="seconds"),))
    # upgrade older databases: optional TOTP two-factor authentication
    _cols = [r[1] for r in _c.execute("PRAGMA table_info(users)")]
    if "two_factor_enabled" not in _cols:
        _c.execute("ALTER TABLE users ADD COLUMN two_factor_enabled INTEGER NOT NULL DEFAULT 0")
    if "totp_secret" not in _cols:
        _c.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
    if "recovery_codes" not in _cols:
        _c.execute("ALTER TABLE users ADD COLUMN recovery_codes TEXT")
    # upgrade older databases: record who performed each calibration
    if "performed_by" not in [r[1] for r in _c.execute("PRAGMA table_info(calibrations)")]:
        _c.execute("ALTER TABLE calibrations ADD COLUMN performed_by TEXT")
    # upgrade older databases: multi-point calibration
    if "n_points" not in [r[1] for r in _c.execute("PRAGMA table_info(calibrations)")]:
        _c.execute("ALTER TABLE calibrations ADD COLUMN n_points INTEGER NOT NULL DEFAULT 1")
    # older single-point calibrations become calibrations with one point
    _c.execute("""INSERT INTO calibration_points(cal_id, point_no, reference_value, measured_value, error, result)
                   SELECT cal_id, 1, reference_value, measured_value, error, result FROM calibrations c
                   WHERE NOT EXISTS (SELECT 1 FROM calibration_points p WHERE p.cal_id = c.cal_id)""")
    # upgrade older databases: tolerance per measurement point
    if "tolerance" not in [r[1] for r in _c.execute("PRAGMA table_info(calibration_points)")]:
        _c.execute("ALTER TABLE calibration_points ADD COLUMN tolerance REAL")
    _c.execute("""UPDATE calibration_points SET tolerance = (
                   SELECT s.tolerance FROM calibrations c JOIN sensors s USING(sensor_id)
                   WHERE c.cal_id = calibration_points.cal_id) WHERE tolerance IS NULL""")
    # upgrade older databases: link calibrations to the reference-standards register
    for _col, _ddl in (("standard_id", "INTEGER"), ("standard_details", "TEXT"), ("request_id", "INTEGER")):
        if _col not in [r[1] for r in _c.execute("PRAGMA table_info(calibrations)")]:
            _c.execute(f"ALTER TABLE calibrations ADD COLUMN {_col} {_ddl}")


def status(row):
    """Return (label, css_class) for a sensor row from LATEST."""
    if not row["next_due"]:
        return "Never calibrated", "grey"
    if row["result"] == "FAIL":
        return "Failed", "red"
    days = (date.fromisoformat(row["next_due"]) - date.today()).days
    if days < 0:
        return "Overdue", "red"
    if days <= 30:
        return f"Due in {days}d", "amber"
    return "OK", "green"


app.jinja_env.globals["status"] = status


def flash_kind(msg):
    """CSS class for a flash message: red for problems, green otherwise."""
    bad = r"could not|invalid|incorrect|do not match|at least|required|already|cannot|too many|no records|check the"
    return "error" if re.search(bad, msg, re.I) else "ok"


app.jinja_env.globals["flash_kind"] = flash_kind

# ------------------------- language (English / Nepali) -------------------------
LANGS = {"en": "English", "ne": "नेपाली"}

# Letterhead shown at the top of the home page and on every report. Edit the wording here.
BANNER = {
    "ne": ["नेपाल सरकार", "उर्जा, जलश्रोत तथा सिँचाइ मन्त्रालय", "नेपाल मौसम विज्ञान विभाग",
           "बबरमहल, काठमाडौ"],
    "en": ["Government of Nepal", "Ministry of Energy, Water Resource and Irrigation",
           "Nepal Meteorological Department", "Babarmahal, Kathmandu"],
}


def tr(text):
    """Translate an English interface string when Nepali is selected (see translations.py)."""
    return NE.get(text, text) if g.get("lang") == "ne" else text


_flash = flash


def flash(msg, kind=None):
    """Flash a message in the current language. kind is 'error' or 'ok'."""
    _flash(tr(msg), kind or flash_kind(msg))


def status_label(label):
    """Translate a status label such as 'Overdue' or 'Due in 5d'."""
    for prefix, key in (("Due in ", "Due in {n}d"), ("Expires in ", "Expires in {n}d")):
        if label.startswith(prefix):
            return tr(key).format(n=label[len(prefix):-1])
    return tr(label)


def status_key(label):
    return ("ok" if label == "OK" else "soon" if label.startswith("Due in") else
            "overdue" if label == "Overdue" else "failed" if label == "Failed" else "never")


def standard_status(row):
    """Return (label, css_class) for a reference standard."""
    if not row["active"]:
        return "Inactive", "grey"
    days = (date.fromisoformat(row["valid_until"]) - date.today()).days
    if days < 0:
        return "Expired", "red"
    if days <= 30:
        return f"Expires in {days}d", "amber"
    return "Valid", "green"


def bs_date_pair(iso_value):
    """Display an ISO Gregorian date together with its Bikram Sambat equivalent."""
    if not iso_value:
        return "—"
    try:
        ad = date.fromisoformat(str(iso_value)[:10])
        from nepali_datetime import date as bs_date
        b = bs_date.from_datetime_date(ad)
        return f"{ad.isoformat()} / {b.year:04d}-{b.month:02d}-{b.day:02d} BS"
    except (ValueError, TypeError):
        return str(iso_value)

app.jinja_env.globals.update(tr=tr, stl=status_label, status_key=status_key,
                             std_status=standard_status, date_pair=bs_date_pair)


@app.context_processor
def i18n():
    return {"banner": BANNER[g.get("lang", "en")], "LANGS": LANGS}


@app.context_processor
def nav_counts():
    """Number of sensors needing attention, shown as a badge in the sidebar."""
    if not g.get("user"):
        return {}
    db = get_db()
    rows = db.execute(LATEST).fetchall()
    stds = db.execute("SELECT * FROM reference_standards WHERE active=1").fetchall()
    return {"nav_alerts": sum(1 for r in rows if status(r)[0] != "OK"),
            "std_alerts": sum(1 for x in stds if standard_status(x)[0] != "Valid")}


def next_certificate(db, cal_date):
    n = db.execute("SELECT COUNT(*) FROM calibrations WHERE certificate_no LIKE ?",
                   (f"CAL-{cal_date[:4]}-%",)).fetchone()[0]
    return f"CAL-{cal_date[:4]}-{n + 1:04d}"


def next_request_number(db, received_date):
    """Generate the next laboratory calibration request number for the year."""
    year = received_date[:4]
    row = db.execute(
        "SELECT request_no FROM calibration_requests WHERE request_no LIKE ? "
        "ORDER BY request_id DESC LIMIT 1", (f"REQ-{year}-%",)
    ).fetchone()
    try:
        n = int(row["request_no"].rsplit("-", 1)[1]) + 1 if row else 1
    except (ValueError, IndexError):
        n = db.execute("SELECT COUNT(*) FROM calibration_requests WHERE request_no LIKE ?",
                       (f"REQ-{year}-%",)).fetchone()[0] + 1
    candidate = f"REQ-{year}-{n:04d}"
    while db.execute("SELECT 1 FROM calibration_requests WHERE request_no=?", (candidate,)).fetchone():
        n += 1
        candidate = f"REQ-{year}-{n:04d}"
    return candidate


REQUEST_STATUSES = (
    "RECEIVED", "REVIEWED", "ASSIGNED", "IN CALIBRATION",
    "UNDER REVIEW", "COMPLETED", "CANCELLED"
)


def next_work_order_number(db, assigned_date=None):
    """Generate a sequential work order number for the assignment year."""
    year = (assigned_date or date.today().isoformat())[:4]
    row = db.execute(
        "SELECT work_order_no FROM calibration_work_orders WHERE work_order_no LIKE ? "
        "ORDER BY work_order_id DESC LIMIT 1", (f"WO-{year}-%",)
    ).fetchone()
    try:
        n = int(row["work_order_no"].rsplit("-", 1)[1]) + 1 if row else 1
    except (ValueError, IndexError):
        n = db.execute("SELECT COUNT(*) FROM calibration_work_orders WHERE work_order_no LIKE ?",
                       (f"WO-{year}-%",)).fetchone()[0] + 1
    candidate = f"WO-{year}-{n:04d}"
    while db.execute("SELECT 1 FROM calibration_work_orders WHERE work_order_no=?", (candidate,)).fetchone():
        n += 1
        candidate = f"WO-{year}-{n:04d}"
    return candidate


WORK_ORDER_STATUSES = ("ASSIGNED", "IN PROGRESS", "AWAITING REVIEW", "COMPLETED", "CANCELLED")


# ------------------------------- authentication -------------------------------
OPEN_ENDPOINTS = {"login", "setup", "static", "set_lang", "two_factor", "calendar_view", "api_bs_date"}
FAILS = {}   # (username, ip) -> (failed count, locked-until timestamp)


def csrf_input():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return Markup(f'<input type="hidden" name="csrf" value="{session["csrf"]}">')


app.jinja_env.globals["csrf_input"] = csrf_input


def safe_next(target):
    return target if target and target.startswith("/") and not target.startswith("//") \
        else url_for("index")

def _complete_login(user_id, next_target=None):
    """Create the authenticated session after password + optional 2FA verification."""
    session.clear()
    session.permanent = True
    session["user_id"] = user_id
    return redirect(safe_next(next_target))


def _recovery_codes():
    """Create one-time recovery codes and return (plain_codes, stored_hashes)."""
    plain = [secrets.token_hex(5).upper() for _ in range(10)]
    return plain, json.dumps([generate_password_hash(x) for x in plain])


def _qr_data_uri(uri):
    """Generate a self-contained QR image for offline/local installations."""
    img = qrcode.make(uri)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")



@app.before_request
def gate():
    g.user = None
    g.lang = request.cookies.get("lang") if request.cookies.get("lang") in LANGS else "en"
    if request.endpoint is None:
        return
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        return None if request.endpoint in ("setup", "static", "set_lang") else redirect(url_for("setup"))
    if session.get("user_id"):
        g.user = db.execute("SELECT * FROM users WHERE user_id=? AND active=1",
                            (session["user_id"],)).fetchone()
        if not g.user:
            session.clear()
    if request.method == "POST":
        sent = (request.form.get("csrf") or "").encode()
        if not hmac.compare_digest(sent, (session.get("csrf") or "!").encode()):
            abort(400, "Invalid or missing security token. Reload the page and try again.")
    if not g.user and request.endpoint not in OPEN_ENDPOINTS:
        return redirect(url_for("login", next=request.full_path.rstrip("?")))


def admin_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if g.user["role"] != "admin":
            abort(403)
        return f(*a, **kw)
    return wrapper


def check_new_password(pw, pw2):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw != pw2:
        return "Passwords do not match."
    return None


@app.route("/lang/<code>")
def set_lang(code):
    if code not in LANGS:
        abort(404)
    resp = redirect(safe_next(request.args.get("next")))
    resp.set_cookie("lang", code, max_age=365 * 24 * 3600, samesite="Lax")
    return resp


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First run only: create the first administrator."""
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        err = check_new_password(f["password"], f["password2"])
        if err or not f["username"].strip():
            flash(err or "Username is required.")
        else:
            db.execute("INSERT INTO users(username, full_name, password_hash, role) "
                       "VALUES (?,?,?, 'admin')",
                       (f["username"].strip(), f["full_name"].strip() or f["username"].strip(),
                        generate_password_hash(f["password"])))
            db.commit()
            flash("Administrator created. Please sign in.")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form["username"].strip().lower()
        key = (name, request.remote_addr)
        if FAILS.get(key, (0, 0))[1] > time.time():
            flash("Too many failed attempts. Try again in 5 minutes.")
        else:
            u = get_db().execute("SELECT * FROM users WHERE username=? AND active=1",
                                 (name,)).fetchone()
            if u and check_password_hash(u["password_hash"], request.form["password"]):
                FAILS.pop(key, None)
                if u["two_factor_enabled"]:
                    session.clear()
                    session["pending_2fa_user_id"] = u["user_id"]
                    session["pending_2fa_next"] = safe_next(request.args.get("next"))
                    return redirect(url_for("two_factor"))
                return _complete_login(u["user_id"], request.args.get("next"))
            count = FAILS.get(key, (0, 0))[0] + 1
            FAILS[key] = (0, time.time() + 300) if count >= 5 else (count, 0)
            flash("Invalid username or password.")
    return render_template("login.html")


@app.route("/2fa", methods=["GET", "POST"])
def two_factor():
    """Verify a TOTP code or a one-time recovery code after password authentication."""
    if g.user:
        return redirect(url_for("index"))
    uid = session.get("pending_2fa_user_id")
    if not uid:
        return redirect(url_for("login"))
    u = get_db().execute("SELECT * FROM users WHERE user_id=? AND active=1", (uid,)).fetchone()
    if not u or not u["two_factor_enabled"] or not u["totp_secret"]:
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        code = re.sub(r"\s+", "", request.form.get("code", "")).upper()
        ok = bool(re.fullmatch(r"\d{6}", code) and
                  pyotp.TOTP(u["totp_secret"]).verify(code, valid_window=1))
        recovery_used = False
        if not ok and u["recovery_codes"]:
            stored = json.loads(u["recovery_codes"])
            for i, hashed in enumerate(stored):
                if check_password_hash(hashed, code):
                    stored.pop(i)
                    get_db().execute("UPDATE users SET recovery_codes=? WHERE user_id=?",
                                     (json.dumps(stored), uid))
                    get_db().commit()
                    ok = recovery_used = True
                    break
        if ok:
            next_target = session.get("pending_2fa_next")
            return _complete_login(uid, next_target)
        flash("Invalid verification code. Please try again.", "error")
    remaining = len(json.loads(u["recovery_codes"] or "[]"))
    return render_template("two_factor.html", recovery_remaining=remaining)


@app.route("/account/2fa/setup", methods=["GET", "POST"])
def setup_2fa():
    if g.user["two_factor_enabled"]:
        return redirect(url_for("account"))
    secret = session.get("pending_totp_secret")
    if not secret:
        secret = pyotp.random_base32()
        session["pending_totp_secret"] = secret
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=g.user["username"], issuer_name="Calibration Lab Management System")
    qr = _qr_data_uri(uri)
    if request.method == "POST":
        code = re.sub(r"\s+", "", request.form.get("code", ""))
        if re.fullmatch(r"\d{6}", code) and totp.verify(code, valid_window=1):
            plain, stored = _recovery_codes()
            db = get_db()
            db.execute("UPDATE users SET two_factor_enabled=1, totp_secret=?, recovery_codes=? WHERE user_id=?",
                       (secret, stored, g.user["user_id"]))
            db.commit()
            session.pop("pending_totp_secret", None)
            session["show_recovery_codes"] = plain
            return redirect(url_for("account"))
        flash("Invalid verification code. Scan the QR code and enter the current 6-digit code.", "error")
    return render_template("two_factor_setup.html", qr=qr, secret=secret)


@app.route("/account/2fa/disable", methods=["POST"])
def disable_2fa():
    if not g.user["two_factor_enabled"]:
        return redirect(url_for("account"))
    password = request.form.get("current_password", "")
    code = re.sub(r"\s+", "", request.form.get("code", "")).upper()
    if not check_password_hash(g.user["password_hash"], password):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("account"))
    valid = bool(re.fullmatch(r"\d{6}", code) and
                 pyotp.TOTP(g.user["totp_secret"]).verify(code, valid_window=1))
    if not valid:
        flash("Enter a valid authenticator code to disable two-factor authentication.", "error")
        return redirect(url_for("account"))
    db = get_db()
    db.execute("UPDATE users SET two_factor_enabled=0, totp_secret=NULL, recovery_codes=NULL WHERE user_id=?",
               (g.user["user_id"],))
    db.commit()
    flash("Two-factor authentication has been disabled.")
    return redirect(url_for("account"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.")
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account():
    if request.method == "POST":
        f = request.form
        db = get_db()
        if not check_password_hash(g.user["password_hash"], f["current"]):
            flash("Current password is incorrect.")
        elif (err := check_new_password(f["password"], f["password2"])):
            flash(err)
        else:
            db.execute("UPDATE users SET password_hash=? WHERE user_id=?",
                       (generate_password_hash(f["password"]), g.user["user_id"]))
            db.commit()
            flash("Password changed.")
            return redirect(url_for("index"))
    recovery_codes = session.pop("show_recovery_codes", None)
    return render_template("account.html", recovery_codes=recovery_codes)


@app.route("/users", methods=["GET", "POST"])
@admin_required
def users():
    db = get_db()
    if request.method == "POST":
        f = request.form
        err = check_new_password(f["password"], f["password"])
        if err or not f["username"].strip() or f["role"] not in ("admin", "technician"):
            flash(err or "Username and a valid role are required.")
        else:
            try:
                db.execute("INSERT INTO users(username, full_name, password_hash, role) "
                           "VALUES (?,?,?,?)",
                           (f["username"].strip(), f["full_name"].strip() or f["username"].strip(),
                            generate_password_hash(f["password"]), f["role"]))
                db.commit()
                flash("User created.")
            except sqlite3.IntegrityError:
                flash("That username already exists.")
        return redirect(url_for("users"))
    return render_template("users.html",
                           rows=db.execute("SELECT * FROM users ORDER BY username").fetchall())



@app.route("/users/<int:uid>/delete", methods=["POST"])
@admin_required
def delete_user(uid):
    if uid == g.user["user_id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("users"))
    db = get_db()
    u = db.execute("SELECT user_id, username, role, active FROM users WHERE user_id=?", (uid,)).fetchone()
    if not u:
        abort(404)
    if u["role"] == "admin" and u["active"]:
        active_admins = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0]
        if active_admins <= 1:
            flash("The last active administrator cannot be deleted.", "error")
            return redirect(url_for("users"))
    try:
        with db:
            db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        flash(f"User '{u['username']}' was deleted.")
    except sqlite3.Error:
        flash("Could not delete the user.", "error")
    return redirect(url_for("users"))

@app.route("/users/<int:uid>/toggle", methods=["POST"])
@admin_required
def toggle_user(uid):
    if uid == g.user["user_id"]:
        flash("You cannot deactivate your own account.")
    else:
        db = get_db()
        db.execute("UPDATE users SET active = 1 - active WHERE user_id=?", (uid,))
        db.commit()
        flash("User updated.")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/password", methods=["POST"])
@admin_required
def reset_password(uid):
    pw = request.form["password"]
    if (err := check_new_password(pw, pw)):
        flash(err)
    else:
        db = get_db()
        db.execute("UPDATE users SET password_hash=? WHERE user_id=?",
                   (generate_password_hash(pw), uid))
        db.commit()
        flash("Password reset.")
    return redirect(url_for("users"))



# ------------------------------ Nepali calendar ------------------------------

def bs_date_label(ad_date, nepali=False):
    """Return a readable Bikram Sambat date for a Gregorian date."""
    from nepali_datetime import date as bs_date
    b = bs_date.from_datetime_date(ad_date)
    fmt = "%K %N %D" if nepali else "%Y %B %d"
    return b.strftime(fmt)


def bs_month_grid(year, month):
    """Return a Sunday-first BS month grid using the library's BS calendar data."""
    from nepali_datetime import date as bs_date
    first = bs_date(year, month, 1)
    days = 0
    cur = first
    while cur.month == month:
        days += 1
        cur = cur + timedelta(days=1)
    first_weekday = first.to_datetime_date().weekday()  # Mon=0 ... Sun=6
    sunday_index = (first_weekday + 1) % 7
    weeks = []
    week = [None] * sunday_index
    for day in range(1, days + 1):
        cell = bs_date(year, month, day)
        week.append({
            "day": day,
            "bs": cell,
            "ad": cell.to_datetime_date(),
        })
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        weeks.append(week + [None] * (7 - len(week)))
    return weeks


@app.route("/api/bs-date")
def api_bs_date():
    value = request.args.get("ad", "")
    try:
        ad = date.fromisoformat(value)
        from nepali_datetime import date as bs_date
        b = bs_date.from_datetime_date(ad)
        return {"ad": ad.isoformat(), "bs": f"{b.year:04d}-{b.month:02d}-{b.day:02d}"}
    except ValueError:
        return {"error": "Invalid Gregorian date"}, 400


@app.route("/calendar")
def calendar_view():
    from nepali_datetime import date as bs_date
    today_bs = bs_date.today()
    try:
        year = request.args.get("year", type=int) or today_bs.year
        month = request.args.get("month", type=int) or today_bs.month
        if not 1 <= month <= 12 or not 1901 <= year <= 2199:
            raise ValueError
        current = bs_date(year, month, 1)
    except ValueError:
        year, month = today_bs.year, today_bs.month
        current = bs_date(year, month, 1)
    prev = current - timedelta(days=1)
    # Find the first day of the next month, then step back one day.
    if month == 12:
        nxt = bs_date(year + 1, 1, 1)
    else:
        nxt = bs_date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return render_template("calendar.html", year=year, month=month, weeks=bs_month_grid(year, month),
                           month_name=current.strftime("%B"), month_name_ne=current.strftime("%N"),
                           prev_year=prev.year, prev_month=prev.month,
                           next_year=nxt.year, next_month=nxt.month,
                           today_bs=today_bs, today_ad=date.today(), current=current, last=last)


@app.route("/")
def index():
    """Home page: summary, items needing attention, recent activity."""
    db = get_db()
    rows = db.execute(LATEST + " ORDER BY s.sensor_id").fetchall()
    counts = dict(total=len(rows), ok=0, soon=0, overdue=0, failed=0, never=0)
    for r in rows:
        label = status(r)[0]
        key = ("ok" if label == "OK" else "soon" if label.startswith("Due in") else
               "overdue" if label == "Overdue" else "failed" if label == "Failed" else "never")
        counts[key] += 1
    order = {"Failed": 0, "Overdue": 1, "Never calibrated": 2}
    attention = sorted((r for r in rows if status(r)[0] != "OK"),
                       key=lambda r: (order.get(status(r)[0], 3), r["next_due"] or ""))
    recent = db.execute(
        "SELECT c.*, s.sensor_type, st.name AS station FROM calibrations c "
        "JOIN sensors s USING(sensor_id) JOIN stations st USING(station_id) "
        "ORDER BY c.cal_id DESC LIMIT 6").fetchall()
    stations_ = db.execute(
        "SELECT st.station_id, st.name, COUNT(s.sensor_id) AS n "
        "FROM stations st LEFT JOIN sensors s USING(station_id) "
        "GROUP BY st.station_id ORDER BY st.updated_at DESC, st.station_id DESC").fetchall()
    now = datetime.now()
    bs_today = bs_date_label(date.today())
    greeting = tr("Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18
                  else "Good evening")
    if g.lang == "ne":
        today = f"{tr(now.strftime('%A'))}, {now.day} {tr(now.strftime('%B'))} {now.year}"
    else:
        today = date.today().strftime("%A, %d %B %Y")
    std_issues = [x for x in db.execute(
        "SELECT * FROM reference_standards WHERE active=1 ORDER BY valid_until")
        if standard_status(x)[0] != "Valid"]
    return render_template("home.html", counts=counts, attention=attention[:8],
                           attention_total=len(attention), recent=recent, stations=stations_,
                           sensors=[r["sensor_id"] for r in rows], greeting=greeting,
                           today=today, bs_today=bs_today, std_issues=std_issues)


# ----------------------- calibration work orders ----------------------------

@app.route("/work-orders")
def work_orders():
    db = get_db()
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "").strip()
    sql = """
        SELECT w.*, r.request_no, r.client_name, r.instrument_description,
               r.requested_service, r.priority, r.sensor_id,
               u.full_name AS technician_name, a.full_name AS assigned_by_name
        FROM calibration_work_orders w
        JOIN calibration_requests r ON r.request_id=w.request_id
        JOIN users u ON u.user_id=w.assigned_technician_id
        JOIN users a ON a.user_id=w.assigned_by
    """
    where, params = [], []
    if g.user["role"] != "admin":
        where.append("w.assigned_technician_id=?")
        params.append(g.user["user_id"])
    if q:
        like = f"%{q}%"
        where.append("(w.work_order_no LIKE ? OR r.request_no LIKE ? OR r.client_name LIKE ? OR "
                     "r.instrument_description LIKE ? OR u.full_name LIKE ?)")
        params.extend([like, like, like, like, like])
    if status_filter in WORK_ORDER_STATUSES:
        where.append("w.status=?")
        params.append(status_filter)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE w.status WHEN 'ASSIGNED' THEN 1 WHEN 'IN PROGRESS' THEN 2 "
    sql += "WHEN 'AWAITING REVIEW' THEN 3 WHEN 'COMPLETED' THEN 4 ELSE 5 END, w.work_order_id DESC"
    rows = db.execute(sql, params).fetchall()
    counts = {}
    for st in WORK_ORDER_STATUSES:
        count_sql = "SELECT COUNT(*) FROM calibration_work_orders WHERE status=?"
        count_params = [st]
        if g.user["role"] != "admin":
            count_sql += " AND assigned_technician_id=?"
            count_params.append(g.user["user_id"])
        counts[st] = db.execute(count_sql, count_params).fetchone()[0]
    return render_template("work_orders.html", rows=rows, statuses=WORK_ORDER_STATUSES,
                           counts=counts, q=q, status_filter=status_filter)


@app.route("/work-orders/<int:work_order_id>")
def work_order_detail(work_order_id):
    db = get_db()
    row = db.execute(
        """SELECT w.*, r.request_no, r.client_name, r.contact_person, r.contact_phone,
                  r.contact_email, r.instrument_description, r.requested_service,
                  r.requested_range, r.received_date, r.requested_due_date, r.priority,
                  r.condition_received, r.remarks, r.sensor_id,
                  s.sensor_type, s.manufacturer, s.serial_number, st.name AS station,
                  u.full_name AS technician_name, u.username AS technician_username,
                  a.full_name AS assigned_by_name, rs.code AS standard_code, rs.name AS standard_name
           FROM calibration_work_orders w
           JOIN calibration_requests r ON r.request_id=w.request_id
           JOIN users u ON u.user_id=w.assigned_technician_id
           JOIN users a ON a.user_id=w.assigned_by
           LEFT JOIN sensors s ON s.sensor_id=r.sensor_id
           LEFT JOIN stations st ON st.station_id=s.station_id
           LEFT JOIN reference_standards rs ON rs.standard_id=w.standard_id
           WHERE w.work_order_id=?""", (work_order_id,)
    ).fetchone()
    if not row:
        abort(404)
    if g.user["role"] != "admin" and row["assigned_technician_id"] != g.user["user_id"]:
        abort(403)
    calibration = db.execute(
        """SELECT * FROM calibrations WHERE request_id=? ORDER BY cal_id DESC LIMIT 1""",
        (row["request_id"],)
    ).fetchone()
    points = db.execute(
        "SELECT * FROM calibration_points WHERE cal_id=? ORDER BY point_no",
        (calibration["cal_id"],)
    ).fetchall() if calibration else []
    reviews = db.execute(
        """SELECT h.*, u.full_name AS submitted_by_name, v.full_name AS reviewer_name
           FROM calibration_review_history h
           JOIN users u ON u.user_id=h.submitted_by
           LEFT JOIN users v ON v.user_id=h.reviewed_by
           WHERE h.work_order_id=? ORDER BY h.review_id DESC""",
        (work_order_id,)
    ).fetchall()
    return render_template("work_order_detail.html", w=row, statuses=WORK_ORDER_STATUSES,
                           calibration=calibration, points=points, reviews=reviews)


@app.route("/requests/<int:request_id>/assign", methods=["POST"])
@admin_required
def assign_calibration_request(request_id):
    db = get_db()
    req = db.execute("SELECT * FROM calibration_requests WHERE request_id=?", (request_id,)).fetchone()
    if not req:
        abort(404)
    if req["status"] in ("COMPLETED", "CANCELLED"):
        flash("Completed or cancelled requests cannot be assigned.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    technician_text = request.form.get("technician_id", "").strip()
    technician = db.execute(
        "SELECT user_id, full_name FROM users WHERE user_id=? AND role='technician' AND active=1",
        (technician_text,)
    ).fetchone() if technician_text.isdigit() else None
    if not technician:
        flash("Select an active technician account.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    target_text = request.form.get("target_date", "").strip()
    try:
        target = date.fromisoformat(target_text).isoformat() if target_text else req["requested_due_date"]
    except ValueError:
        flash("Enter a valid target date.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    method = request.form.get("calibration_method", "").strip()
    standard_text = request.form.get("standard_id", "").strip()
    standard_id = None
    if standard_text:
        if not standard_text.isdigit():
            flash("Select a valid reference standard.", "error")
            return redirect(url_for("calibration_request", request_id=request_id))
        standard = db.execute("SELECT standard_id FROM reference_standards WHERE standard_id=? AND active=1",
                              (int(standard_text),)).fetchone()
        if not standard:
            flash("Select an active reference standard.", "error")
            return redirect(url_for("calibration_request", request_id=request_id))
        standard_id = standard["standard_id"]
    now = datetime.now().isoformat(timespec="seconds")
    existing = db.execute("SELECT * FROM calibration_work_orders WHERE request_id=?", (request_id,)).fetchone()
    with db:
        if existing:
            db.execute(
                """UPDATE calibration_work_orders
                   SET assigned_technician_id=?, assigned_by=?, assigned_at=?, target_date=?,
                       calibration_method=?, standard_id=?, instructions=?,
                       status=CASE WHEN status IN ('ASSIGNED','CANCELLED') THEN 'ASSIGNED' ELSE status END,
                       updated_at=? WHERE request_id=?""",
                (technician["user_id"], g.user["user_id"], now, target, method, standard_id,
                 request.form.get("instructions", "").strip(), now, request_id)
            )
            work_order_no = existing["work_order_no"]
        else:
            work_order_no = next_work_order_number(db, now[:10])
            db.execute(
                """INSERT INTO calibration_work_orders
                   (work_order_no, request_id, assigned_technician_id, assigned_by, assigned_at,
                    target_date, calibration_method, standard_id, instructions, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,'ASSIGNED',?,?)""",
                (work_order_no, request_id, technician["user_id"], g.user["user_id"], now,
                 target, method, standard_id, request.form.get("instructions", "").strip(), now, now)
            )
        db.execute("UPDATE calibration_requests SET status='ASSIGNED', updated_at=? WHERE request_id=?",
                   (now, request_id))
    flash(f"Work order {work_order_no} assigned to {technician['full_name']}.")
    work_order = db.execute("SELECT work_order_id FROM calibration_work_orders WHERE request_id=?",
                            (request_id,)).fetchone()
    return redirect(url_for("work_order_detail", work_order_id=work_order["work_order_id"]))


@app.route("/work-orders/<int:work_order_id>/status", methods=["POST"])
def update_work_order_status(work_order_id):
    db = get_db()
    row = db.execute("SELECT * FROM calibration_work_orders WHERE work_order_id=?", (work_order_id,)).fetchone()
    if not row:
        abort(404)
    if g.user["role"] != "admin" and row["assigned_technician_id"] != g.user["user_id"]:
        abort(403)
    new_status = request.form.get("status", "").strip()
    if row["status"] in ("COMPLETED", "CANCELLED"):
        flash("This work order is already closed and cannot be changed.", "error")
        return redirect(url_for("work_order_detail", work_order_id=work_order_id))
    if g.user["role"] != "admin" and row["status"] == "AWAITING REVIEW":
        flash("A submitted work order can only be returned by an administrator reviewer.", "error")
        return redirect(url_for("work_order_detail", work_order_id=work_order_id))
    allowed = ("ASSIGNED", "IN PROGRESS", "AWAITING REVIEW") if g.user["role"] != "admin" else ("CANCELLED",)
    if new_status not in allowed:
        flash("Completed work orders must be finalized through calibration review approval.", "error")
        return redirect(url_for("work_order_detail", work_order_id=work_order_id))
    now = datetime.now().isoformat(timespec="seconds")
    if new_status == "AWAITING REVIEW":
        calibration = db.execute(
            "SELECT cal_id FROM calibrations WHERE request_id=? ORDER BY cal_id DESC LIMIT 1",
            (row["request_id"],)
        ).fetchone()
        if not calibration:
            flash("Record the calibration measurements before submitting this work order for review.", "error")
            return redirect(url_for("work_order_detail", work_order_id=work_order_id))
        pending = db.execute(
            "SELECT review_id FROM calibration_review_history WHERE work_order_id=? AND decision='PENDING'",
            (work_order_id,)
        ).fetchone()
        with db:
            if not pending:
                db.execute(
                    """INSERT INTO calibration_review_history
                       (work_order_id, cal_id, submitted_by, submitted_at, decision)
                       VALUES (?,?,?,?, 'PENDING')""",
                    (work_order_id, calibration["cal_id"], g.user["user_id"], now)
                )
            db.execute("UPDATE calibration_work_orders SET status='AWAITING REVIEW', updated_at=? WHERE work_order_id=?",
                       (now, work_order_id))
            db.execute("UPDATE calibration_requests SET status='UNDER REVIEW', updated_at=? WHERE request_id=?",
                       (now, row["request_id"]))
        flash("Calibration submitted for review.")
        return redirect(url_for("work_order_detail", work_order_id=work_order_id))
    with db:
        db.execute("UPDATE calibration_work_orders SET status=?, updated_at=? WHERE work_order_id=?",
                   (new_status, now, work_order_id))
        if new_status == "IN PROGRESS":
            db.execute("UPDATE calibration_requests SET status='IN CALIBRATION', updated_at=? WHERE request_id=?",
                       (now, row["request_id"]))
        elif new_status == "ASSIGNED":
            db.execute("UPDATE calibration_requests SET status='ASSIGNED', updated_at=? WHERE request_id=?",
                       (now, row["request_id"]))
        elif new_status == "CANCELLED":
            db.execute("UPDATE calibration_requests SET status='CANCELLED', updated_at=? WHERE request_id=?",
                       (now, row["request_id"]))
    flash(f"Work order status updated to {new_status}.")
    return redirect(url_for("work_order_detail", work_order_id=work_order_id))


@app.route("/reviews")
@admin_required
def calibration_reviews():
    db = get_db()
    pending = db.execute(
        """SELECT h.review_id, h.submitted_at, w.work_order_id, w.work_order_no,
                  r.request_no, r.client_name, r.instrument_description,
                  u.full_name AS technician_name, c.certificate_no, c.cal_date, c.result
           FROM calibration_review_history h
           JOIN calibration_work_orders w ON w.work_order_id=h.work_order_id
           JOIN calibration_requests r ON r.request_id=w.request_id
           JOIN users u ON u.user_id=h.submitted_by
           JOIN calibrations c ON c.cal_id=h.cal_id
           WHERE h.decision='PENDING'
           ORDER BY h.submitted_at, h.review_id"""
    ).fetchall()
    recent = db.execute(
        """SELECT h.*, w.work_order_no, r.request_no, r.client_name, c.certificate_no,
                  u.full_name AS reviewer_name
           FROM calibration_review_history h
           JOIN calibration_work_orders w ON w.work_order_id=h.work_order_id
           JOIN calibration_requests r ON r.request_id=w.request_id
           JOIN calibrations c ON c.cal_id=h.cal_id
           LEFT JOIN users u ON u.user_id=h.reviewed_by
           WHERE h.decision!='PENDING'
           ORDER BY h.reviewed_at DESC, h.review_id DESC LIMIT 20"""
    ).fetchall()
    return render_template("reviews.html", pending=pending, recent=recent)


@app.route("/reviews/<int:review_id>/decision", methods=["POST"])
@admin_required
def decide_calibration_review(review_id):
    db = get_db()
    review = db.execute(
        """SELECT h.*, w.request_id, w.status AS work_order_status
           FROM calibration_review_history h
           JOIN calibration_work_orders w ON w.work_order_id=h.work_order_id
           WHERE h.review_id=?""", (review_id,)
    ).fetchone()
    if not review:
        abort(404)
    if review["decision"] != "PENDING":
        flash("This review has already been decided.", "error")
        return redirect(url_for("calibration_reviews"))
    decision = request.form.get("decision", "").strip()
    comments = request.form.get("comments", "").strip()
    if decision not in ("APPROVED", "RETURNED"):
        flash("Choose approve or return for correction.", "error")
        return redirect(url_for("calibration_reviews"))
    if decision == "RETURNED" and not comments:
        flash("Enter review comments when returning a calibration for correction.", "error")
        return redirect(url_for("work_order_detail", work_order_id=review["work_order_id"]))
    now = datetime.now().isoformat(timespec="seconds")
    with db:
        db.execute(
            """UPDATE calibration_review_history
               SET decision=?, comments=?, reviewed_by=?, reviewed_at=? WHERE review_id=?""",
            (decision, comments, g.user["user_id"], now, review_id)
        )
        if decision == "APPROVED":
            db.execute("UPDATE calibration_work_orders SET status='COMPLETED', updated_at=? WHERE work_order_id=?",
                       (now, review["work_order_id"]))
            db.execute("UPDATE calibration_requests SET status='COMPLETED', updated_at=? WHERE request_id=?",
                       (now, review["request_id"]))
        else:
            db.execute("UPDATE calibration_work_orders SET status='IN PROGRESS', updated_at=? WHERE work_order_id=?",
                       (now, review["work_order_id"]))
            db.execute("UPDATE calibration_requests SET status='IN CALIBRATION', updated_at=? WHERE request_id=?",
                       (now, review["request_id"]))
    flash("Calibration approved and request completed." if decision == "APPROVED"
          else "Calibration returned to the technician for correction.")
    return redirect(url_for("work_order_detail", work_order_id=review["work_order_id"]))


# -------------------------- calibration requests ---------------------------

@app.route("/requests")
def calibration_requests():
    db = get_db()
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "").strip()
    sql = """
        SELECT r.*, s.sensor_type, s.serial_number, st.name AS station
        FROM calibration_requests r
        LEFT JOIN sensors s ON s.sensor_id = r.sensor_id
        LEFT JOIN stations st ON st.station_id = s.station_id
    """
    where, params = [], []
    if q:
        where.append("""(r.request_no LIKE ? OR r.client_name LIKE ? OR
                         r.instrument_description LIKE ? OR COALESCE(r.contact_person,'') LIKE ? OR
                         COALESCE(s.serial_number,'') LIKE ?)""")
        like = f"%{q}%"
        params.extend([like, like, like, like, like])
    if status_filter in REQUEST_STATUSES:
        where.append("r.status=?")
        params.append(status_filter)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY r.request_id DESC"
    rows = db.execute(sql, params).fetchall()
    counts = {st: db.execute("SELECT COUNT(*) FROM calibration_requests WHERE status=?", (st,)).fetchone()[0]
              for st in REQUEST_STATUSES}
    return render_template("requests.html", rows=rows, counts=counts,
                           statuses=REQUEST_STATUSES, q=q, status_filter=status_filter)


@app.route("/requests/new", methods=["GET", "POST"])
def new_calibration_request():
    db = get_db()
    sensors_ = db.execute(
        "SELECT s.sensor_id, s.sensor_type, s.manufacturer, s.serial_number, st.name AS station "
        "FROM sensors s JOIN stations st USING(station_id) ORDER BY s.sensor_id"
    ).fetchall()
    if request.method == "POST":
        f = request.form
        try:
            client = f.get("client_name", "").strip()
            description = f.get("instrument_description", "").strip()
            service = f.get("requested_service", "").strip()
            received = date.fromisoformat(f.get("received_date", "").strip())
            due_text = f.get("requested_due_date", "").strip()
            due = date.fromisoformat(due_text) if due_text else None
            priority = f.get("priority", "Normal").strip()
            if not client or not description or not service:
                raise ValueError("Client, instrument description and requested service are required.")
            if priority not in ("Low", "Normal", "High", "Urgent"):
                raise ValueError("Invalid priority.")
            if due and due < received:
                raise ValueError("Requested due date cannot be before the received date.")
            sensor_id = f.get("sensor_id", "").strip() or None
            if sensor_id and not db.execute("SELECT 1 FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone():
                raise ValueError("Selected sensor does not exist.")
            received_iso = received.isoformat()
            request_no = next_request_number(db, received_iso)
            now = datetime.now().isoformat(timespec="seconds")
            cur = db.execute(
                """INSERT INTO calibration_requests
                (request_no, client_name, contact_person, contact_phone, contact_email,
                 sensor_id, instrument_description, requested_service, requested_range,
                 received_date, requested_due_date, priority, condition_received, remarks,
                 status, created_by, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_no, client, f.get("contact_person","").strip(),
                 f.get("contact_phone","").strip(), f.get("contact_email","").strip(),
                 sensor_id, description, service, f.get("requested_range","").strip(),
                 received_iso, due.isoformat() if due else None, priority,
                 f.get("condition_received","").strip(), f.get("remarks","").strip(),
                 "RECEIVED", g.user["full_name"], now, now)
            )
            db.commit()
            flash(f"Calibration request {request_no} was created.")
            return redirect(url_for("calibration_request", request_id=cur.lastrowid))
        except ValueError as e:
            flash(str(e), "error")
    return render_template("request_form.html", sensors=sensors_, today=date.today().isoformat())


@app.route("/requests/<int:request_id>")
def calibration_request(request_id):
    db = get_db()
    row = db.execute(
        """SELECT r.*, s.sensor_type, s.manufacturer, s.serial_number,
                  s.unit, st.name AS station, st.location AS station_location
           FROM calibration_requests r
           LEFT JOIN sensors s ON s.sensor_id=r.sensor_id
           LEFT JOIN stations st ON st.station_id=s.station_id
           WHERE r.request_id=?""", (request_id,)
    ).fetchone()
    if not row:
        abort(404)
    calibrations = db.execute(
        """SELECT c.*, s.sensor_type, s.unit
           FROM calibrations c JOIN sensors s USING(sensor_id)
           WHERE c.request_id=? ORDER BY c.cal_id DESC""", (request_id,)
    ).fetchall()
    technicians = db.execute(
        "SELECT user_id, full_name, username FROM users "
        "WHERE role='technician' AND active=1 ORDER BY full_name"
    ).fetchall()
    standards = db.execute(
        "SELECT standard_id, code, name, valid_until FROM reference_standards "
        "WHERE active=1 ORDER BY code"
    ).fetchall()
    work_order = db.execute(
        "SELECT w.*, u.full_name AS technician_name FROM calibration_work_orders w "
        "JOIN users u ON u.user_id=w.assigned_technician_id WHERE w.request_id=?",
        (request_id,)
    ).fetchone()
    return render_template("request_detail.html", r=row, calibrations=calibrations,
                           statuses=REQUEST_STATUSES, technicians=technicians,
                           standards=standards, work_order=work_order)


@app.route("/requests/<int:request_id>/status", methods=["POST"])
@admin_required
def update_calibration_request_status(request_id):
    new_status = request.form.get("status", "").strip()
    if new_status not in REQUEST_STATUSES:
        flash("Invalid calibration request status.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    db = get_db()
    row = db.execute("SELECT request_no FROM calibration_requests WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        abort(404)
    db.execute("UPDATE calibration_requests SET status=?, updated_at=? WHERE request_id=?",
               (new_status, datetime.now().isoformat(timespec="seconds"), request_id))
    db.commit()
    flash(f"Request {row['request_no']} status changed to {new_status}.")
    return redirect(url_for("calibration_request", request_id=request_id))


@app.route("/requests/<int:request_id>/delete", methods=["POST"])
@admin_required
def delete_calibration_request(request_id):
    db = get_db()
    row = db.execute("SELECT request_no FROM calibration_requests WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        abort(404)
    linked = db.execute("SELECT COUNT(*) FROM calibrations WHERE request_id=?", (request_id,)).fetchone()[0]
    if linked:
        flash(f"Request {row['request_no']} cannot be deleted because it is linked to {linked} calibration record(s).", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    db.execute("DELETE FROM calibration_requests WHERE request_id=?", (request_id,))
    db.commit()
    flash(f"Request {row['request_no']} was deleted.")
    return redirect(url_for("calibration_requests"))


@app.route("/register")
def register():
    rows = get_db().execute(LATEST + " ORDER BY s.sensor_id").fetchall()
    return render_template("register.html", rows=rows)



def _delete_station_records(db, station_ids):
    station_ids = [int(x) for x in station_ids]
    if not station_ids:
        return 0
    sp = ",".join("?" * len(station_ids))
    sensor_rows = db.execute(f"SELECT sensor_id FROM sensors WHERE station_id IN ({sp})", station_ids).fetchall()
    sensor_ids = [r["sensor_id"] for r in sensor_rows]
    if sensor_ids:
        xp = ",".join("?" * len(sensor_ids))
        cal_rows = db.execute(f"SELECT cal_id FROM calibrations WHERE sensor_id IN ({xp})", sensor_ids).fetchall()
        cal_ids = [r["cal_id"] for r in cal_rows]
        if cal_ids:
            cp = ",".join("?" * len(cal_ids))
            db.execute(f"DELETE FROM calibration_points WHERE cal_id IN ({cp})", cal_ids)
            db.execute(f"DELETE FROM calibrations WHERE cal_id IN ({cp})", cal_ids)
        db.execute(f"DELETE FROM sensors WHERE sensor_id IN ({xp})", sensor_ids)
    db.execute(f"DELETE FROM stations WHERE station_id IN ({sp})", station_ids)
    return len(station_ids)


@app.route("/stations/<int:station_id>/delete", methods=["POST"])
@admin_required
def delete_station(station_id):
    db = get_db()
    station = db.execute("SELECT station_id, name FROM stations WHERE station_id=?", (station_id,)).fetchone()
    if not station:
        abort(404)
    try:
        with db:
            _delete_station_records(db, [station_id])
        flash(f"Station '{station['name']}' and its sensors/calibration records were deleted.")
    except sqlite3.Error:
        flash("Could not delete the station and its related records.", "error")
    return redirect(url_for("stations"))


@app.route("/stations/bulk-delete", methods=["POST"])
@admin_required
def bulk_delete_stations():
    raw_ids = request.form.getlist("station_ids")
    station_ids = []
    for value in raw_ids:
        try:
            station_ids.append(int(value))
        except (TypeError, ValueError):
            continue
    station_ids = list(dict.fromkeys(station_ids))
    if not station_ids:
        flash("Select at least one station to delete.", "error")
        return redirect(url_for("stations"))
    db = get_db()
    placeholders = ",".join("?" * len(station_ids))
    existing = db.execute(
        f"SELECT station_id FROM stations WHERE station_id IN ({placeholders})", station_ids
    ).fetchall()
    existing_ids = [r["station_id"] for r in existing]
    try:
        with db:
            deleted = _delete_station_records(db, existing_ids)
        flash(f"{deleted} station(s) and their sensors/calibration records were deleted.")
    except sqlite3.Error:
        flash("Could not delete the selected stations and their related records.", "error")
    return redirect(url_for("stations"))


@app.route("/stations", methods=["GET", "POST"])
def stations():
    db = get_db()
    if request.method == "POST":
        if g.user["role"] != "admin":
            abort(403)
        try:
            name = request.form["name"].strip()
            location = request.form.get("location", "").strip()
            station_type = request.form.get("type", "").strip() or "Meteorological"
            if not name:
                raise ValueError("Station name is required.")
            db.execute("INSERT INTO stations(name, location, type, updated_at) VALUES (?,?,?,?)",
                       (name, location, station_type, datetime.now().isoformat(timespec="seconds")))
            db.commit()
            flash("Station added.")
        except sqlite3.IntegrityError:
            flash("That station already exists.")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("stations"))
    return render_template("stations.html",
                           rows=db.execute("SELECT * FROM stations ORDER BY updated_at DESC, station_id DESC").fetchall())


def _excel_workbook(instructions, sheet_name, headers, sample_rows):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    info = wb.create_sheet("Instructions")
    info.append(["Calibration Lab Management System - Bulk Upload"])
    for line in instructions:
        info.append([line])
    info["A1"].font = Font(bold=True, size=14)
    for cell in info["A"]:
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    info.column_dimensions["A"].width = 110
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F5FBF")
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for row in sample_rows:
        ws.append(row)
    for col in range(1, len(headers) + 1):
        ws.column_dimensions[chr(64 + col) if col <= 26 else "A"].width = 24
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@app.route("/stations/bulk-sample")
def station_bulk_sample():
    data = _excel_workbook(
        [
            "Fill one station per row in the 'Stations' sheet.",
            "Station ID: leave blank when creating a new station. Enter an existing numeric Station ID only when updating that station.",
            "Station Name is required and must be unique.",
            "Location and Type are required for complete station details. Example Type: Meteorological, Hydrological, Agrometeorological, Radar.",
            "Do not change the column headings."
        ],
        "Stations",
        ["Station ID", "Station Name", "Location", "Type"],
        [["", "Example Station", "Dharan, Sunsari", "Meteorological"]]
    )
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="station_bulk_upload_sample.xlsx"'})


@app.route("/stations/bulk-upload", methods=["POST"])
@admin_required
def station_bulk_upload():
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("Please choose an Excel .xlsx file.", "error")
        return redirect(url_for("stations"))
    try:
        from openpyxl import load_workbook
        wb = load_workbook(upload, read_only=True, data_only=True)
        ws = wb["Stations"] if "Stations" in wb.sheetnames else wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise ValueError("The Excel file is empty.")
        headers = [str(x).strip() if x is not None else "" for x in rows[0]]
        expected = ["Station ID", "Station Name", "Location", "Type"]
        if headers != expected:
            raise ValueError("Invalid columns. Download the station Excel sample and use its column headings.")
        db = get_db()
        parsed = []
        errors = []
        seen_ids = set()
        seen_names = set()
        for row_no, row in enumerate(rows[1:], 2):
            if not any(v not in (None, "") for v in row):
                continue
            vals = list(row) + [""] * (4 - len(row))
            sid, name, location, station_type = vals[:4]
            name = str(name).strip() if name is not None else ""
            location = str(location).strip() if location is not None else ""
            station_type = str(station_type).strip() if station_type is not None else ""
            if not name:
                errors.append(f"Row {row_no}: Station Name is required.")
                continue
            if not station_type:
                errors.append(f"Row {row_no}: Type is required.")
                continue
            sid_int = None
            if sid not in (None, ""):
                try:
                    sid_int = int(float(sid))
                    if sid_int <= 0:
                        raise ValueError
                except (TypeError, ValueError):
                    errors.append(f"Row {row_no}: Station ID must be a positive number or blank.")
                    continue
                if sid_int in seen_ids:
                    errors.append(f"Row {row_no}: duplicate Station ID {sid_int} in the file.")
                    continue
                seen_ids.add(sid_int)
            name_key = name.casefold()
            if name_key in seen_names:
                errors.append(f"Row {row_no}: duplicate Station Name '{name}'.")
                continue
            seen_names.add(name_key)
            parsed.append((sid_int, name, location, station_type, row_no))
        if errors:
            raise ValueError("Upload stopped. " + " ".join(errors[:12]) +
                             (f" Showing first 12 of {len(errors)} errors." if len(errors) > 12 else ""))
        with db:
            for sid, name, location, station_type, row_no in parsed:
                if sid is None:
                    if db.execute("SELECT 1 FROM stations WHERE name=? COLLATE NOCASE", (name,)).fetchone():
                        raise ValueError(f"Row {row_no}: station name '{name}' already exists.")
                    db.execute("INSERT INTO stations(name, location, type, updated_at) VALUES (?,?,?,?)",
                               (name, location, station_type, datetime.now().isoformat(timespec="seconds")))
                else:
                    existing = db.execute("SELECT station_id FROM stations WHERE station_id=?", (sid,)).fetchone()
                    if existing:
                        conflict = db.execute("SELECT station_id FROM stations WHERE name=? COLLATE NOCASE AND station_id<>?",
                                               (name, sid)).fetchone()
                        if conflict:
                            raise ValueError(f"Row {row_no}: station name '{name}' belongs to another station.")
                        db.execute("UPDATE stations SET name=?, location=?, type=?, updated_at=? WHERE station_id=?",
                                   (name, location, station_type, datetime.now().isoformat(timespec="seconds"), sid))
                    else:
                        if db.execute("SELECT 1 FROM stations WHERE name=? COLLATE NOCASE", (name,)).fetchone():
                            raise ValueError(f"Row {row_no}: station name '{name}' already exists.")
                        db.execute("INSERT INTO stations(station_id, name, location, type) VALUES (?,?,?,?)",
                                   (sid, name, location, station_type))
        flash(f"{len(parsed)} station record(s) uploaded successfully.")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Could not process the Excel file: {e}", "error")
    return redirect(url_for("stations"))




def _sensor_id_prefix(sensor_type):
    """Return the standard two-letter Sensor ID prefix for a sensor type."""
    value = re.sub(r"[^A-Za-z]", "", (sensor_type or "").strip()).lower()
    prefixes = {
        "temperature": "TS",
        "pressure": "PS",
        "humidity": "HS",
        "relativehumidity": "RHS",
        "rainfall": "RS",
        "precipitation": "RS",
        "wind": "WS",
        "windspeed": "WS",
        "winddirection": "WD",
        "solar": "SS",
        "radiation": "RS",
        "visibility": "VS",
        "waterlevel": "WL",
    }
    if value in prefixes:
        return prefixes[value]
    letters = re.findall(r"[A-Za-z]+", (sensor_type or "").upper())
    initials = "".join(word[0] for word in letters)
    return (initials[:3] or "SN").upper()


def _station_sensor_id(db, station_id, sensor_type):
    """Generate IDs such as PS_Tarahara_1001, incrementing per station and type."""
    station = db.execute("SELECT name FROM stations WHERE station_id=?", (station_id,)).fetchone()
    if not station:
        raise ValueError("Selected station does not exist.")
    prefix = _sensor_id_prefix(sensor_type)
    station_label = re.sub(r"[^A-Za-z0-9]+", "_", station["name"].strip()).strip("_")
    if not station_label:
        station_label = "Station"
    pattern = f"{prefix}_{station_label}_%"
    rows = db.execute("SELECT sensor_id FROM sensors WHERE sensor_id LIKE ?",
                      (pattern,)).fetchall()
    numbers = []
    for row in rows:
        match = re.fullmatch(rf"{re.escape(prefix)}_{re.escape(station_label)}_(\\d+)", row["sensor_id"])
        if match:
            numbers.append(int(match.group(1)))
    next_number = max(numbers, default=1000) + 1
    sensor_id = f"{prefix}_{station_label}_{next_number:04d}"
    while db.execute("SELECT 1 FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone():
        next_number += 1
        sensor_id = f"{prefix}_{station_label}_{next_number:04d}"
    return sensor_id


@app.route("/sensors/new", methods=["GET", "POST"])
def new_sensor():
    db = get_db()
    stations_ = db.execute("SELECT * FROM stations ORDER BY name").fetchall()
    if request.method == "POST":
        f = request.form
        try:
            station_id = int(f["station_id"])
            sensor_type = f["sensor_type"].strip()
            if not sensor_type:
                raise ValueError("Sensor type is required.")
            sensor_id = _station_sensor_id(db, station_id, sensor_type)
            db.execute("INSERT INTO sensors VALUES (?,?,?,?,?,?,?,?)",
                       (sensor_id, station_id, sensor_type, f["manufacturer"].strip(),
                        f["serial_number"].strip(), int(f["interval_days"]),
                        float(f["tolerance"]), f["unit"].strip()))
            db.commit()
            flash(f"Sensor registered with ID {sensor_id}.")
            return redirect(url_for("sensor", sensor_id=sensor_id))
        except (sqlite3.IntegrityError, ValueError) as e:
            flash(tr("Could not save sensor") + f": {e}", "error")
    return render_template("sensor_form.html", stations=stations_)


@app.route("/sensors/bulk-sample")
def sensor_bulk_sample():
    first_station = get_db().execute("SELECT station_id FROM stations ORDER BY station_id LIMIT 1").fetchone()
    example_station_id = first_station["station_id"] if first_station else 1
    data = _excel_workbook(
        [
            "Fill one sensor per row in the 'Sensors' sheet.",
            "All columns are required except Manufacturer and Unit.",
            "Station ID must already exist in the Station Details register.",
            "Sensor ID and Serial Number must be unique.",
            "Calibration Interval (days) is normally 365 for annual calibration.",
            "Tolerance is the default allowed error used during calibration.",
            "Do not change the column headings."
        ],
        "Sensors",
        ["Sensor ID", "Station ID", "Sensor Type", "Manufacturer", "Serial Number",
         "Calibration Interval (days)", "Tolerance", "Unit"],
        [["TS-EXAMPLE", example_station_id, "Temperature", "Example Manufacturer", "SN-EXAMPLE",
          365, 0.5, "°C"]]
    )
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="sensor_bulk_upload_sample.xlsx"'})


@app.route("/sensors/bulk-upload", methods=["POST"])
def sensor_bulk_upload():
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("Please choose an Excel .xlsx file.", "error")
        return redirect(url_for("register"))
    try:
        from openpyxl import load_workbook
        wb = load_workbook(upload, read_only=True, data_only=True)
        ws = wb["Sensors"] if "Sensors" in wb.sheetnames else wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise ValueError("The Excel file is empty.")
        headers = [str(x).strip() if x is not None else "" for x in rows[0]]
        expected = ["Sensor ID", "Station ID", "Sensor Type", "Manufacturer", "Serial Number",
                    "Calibration Interval (days)", "Tolerance", "Unit"]
        if headers != expected:
            raise ValueError("Invalid columns. Download the sensor Excel sample and use its column headings.")
        db = get_db()
        parsed, errors = [], []
        seen_ids, seen_serials = set(), set()
        for row_no, row in enumerate(rows[1:], 2):
            if not any(v not in (None, "") for v in row):
                continue
            vals = list(row) + [""] * (8 - len(row))
            sensor_id, station_id, sensor_type, manufacturer, serial, interval, tolerance, unit = vals[:8]
            sensor_id = str(sensor_id).strip().upper() if sensor_id is not None else ""
            sensor_type = str(sensor_type).strip() if sensor_type is not None else ""
            manufacturer = str(manufacturer).strip() if manufacturer is not None else ""
            serial = str(serial).strip() if serial is not None else ""
            unit = str(unit).strip() if unit is not None else ""
            if not sensor_id or not sensor_type or not serial:
                errors.append(f"Row {row_no}: Sensor ID, Sensor Type and Serial Number are required.")
                continue
            try:
                station_id = int(float(station_id))
                interval = int(float(interval))
                tolerance = float(tolerance)
                if station_id <= 0 or interval <= 0 or tolerance < 0 or not math.isfinite(tolerance):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"Row {row_no}: Station ID, interval and tolerance must be valid positive numeric values (tolerance may be 0).")
                continue
            if sensor_id in seen_ids or serial.casefold() in seen_serials:
                errors.append(f"Row {row_no}: duplicate Sensor ID or Serial Number in the file.")
                continue
            seen_ids.add(sensor_id)
            seen_serials.add(serial.casefold())
            if not db.execute("SELECT 1 FROM stations WHERE station_id=?", (station_id,)).fetchone():
                errors.append(f"Row {row_no}: Station ID {station_id} does not exist.")
                continue
            if db.execute("SELECT 1 FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone():
                errors.append(f"Row {row_no}: Sensor ID '{sensor_id}' already exists.")
                continue
            if db.execute("SELECT 1 FROM sensors WHERE serial_number=? COLLATE NOCASE", (serial,)).fetchone():
                errors.append(f"Row {row_no}: Serial Number '{serial}' already exists.")
                continue
            parsed.append((sensor_id, station_id, sensor_type, manufacturer, serial, interval, tolerance, unit))
        if errors:
            raise ValueError("Upload stopped. " + " ".join(errors[:12]) +
                             (f" Showing first 12 of {len(errors)} errors." if len(errors) > 12 else ""))
        with db:
            db.executemany("INSERT INTO sensors(sensor_id,station_id,sensor_type,manufacturer,serial_number,interval_days,tolerance,unit) VALUES (?,?,?,?,?,?,?,?)",
                           parsed)
        flash(f"{len(parsed)} sensor record(s) uploaded successfully.")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Could not process the Excel file: {e}", "error")
    return redirect(url_for("register"))



@app.route("/sensors/<sensor_id>/delete", methods=["POST"])
@admin_required
def delete_sensor(sensor_id):
    db = get_db()
    sensor = db.execute("SELECT sensor_id FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone()
    if not sensor:
        abort(404)
    try:
        with db:
            cal_rows = db.execute("SELECT cal_id FROM calibrations WHERE sensor_id=?", (sensor_id,)).fetchall()
            cal_ids = [r["cal_id"] for r in cal_rows]
            if cal_ids:
                cp = ",".join("?" * len(cal_ids))
                db.execute(f"DELETE FROM calibration_points WHERE cal_id IN ({cp})", cal_ids)
                db.execute(f"DELETE FROM calibrations WHERE cal_id IN ({cp})", cal_ids)
            db.execute("DELETE FROM sensors WHERE sensor_id=?", (sensor_id,))
        flash(f"Sensor '{sensor_id}' and its calibration history were deleted.")
    except sqlite3.Error:
        flash("Could not delete the sensor and its calibration history.", "error")
    return redirect(url_for("register"))


@app.route("/calibrations/<int:cal_id>/delete", methods=["POST"])
@admin_required
def delete_calibration(cal_id):
    db = get_db()
    row = db.execute("SELECT certificate_no, sensor_id FROM calibrations WHERE cal_id=?", (cal_id,)).fetchone()
    if not row:
        abort(404)
    try:
        with db:
            db.execute("DELETE FROM calibration_points WHERE cal_id=?", (cal_id,))
            db.execute("DELETE FROM calibrations WHERE cal_id=?", (cal_id,))
        flash(f"Calibration record '{row['certificate_no']}' was deleted.")
    except sqlite3.Error:
        flash("Could not delete the calibration record.", "error")
    return redirect(url_for("sensor", sensor_id=row["sensor_id"]))

@app.route("/sensors/<sensor_id>")
def sensor(sensor_id):
    db = get_db()
    s = db.execute(LATEST + " WHERE s.sensor_id=?", (sensor_id,)).fetchone()
    if not s:
        abort(404)
    hist = db.execute("SELECT * FROM calibrations WHERE sensor_id=? ORDER BY cal_id DESC",
                      (sensor_id,)).fetchall()
    return render_template("sensor.html", s=s, hist=hist)


@app.route("/sensors/<sensor_id>/calibrate", methods=["GET", "POST"])
def calibrate(sensor_id):
    db = get_db()
    s = db.execute("SELECT * FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone()
    if not s:
        abort(404)
    linked_request_id = request.values.get("request_id", "").strip()
    if g.user["role"] == "admin":
        flash("Administrators verify calibration data but do not enter technician measurements.", "error")
        return redirect(url_for("sensor", sensor_id=sensor_id))
    if not linked_request_id.isdigit():
        flash("Calibration measurements must be entered from an assigned calibration work order.", "error")
        return redirect(url_for("calibration_requests"))
    if linked_request_id.isdigit():
        linked_request = db.execute(
            "SELECT request_id, sensor_id, status FROM calibration_requests WHERE request_id=?",
            (int(linked_request_id),)
        ).fetchone()
        if not linked_request:
            abort(404)
        if linked_request["sensor_id"] and linked_request["sensor_id"] != sensor_id:
            flash("This request belongs to a different registered instrument.", "error")
            return redirect(url_for("calibration_request", request_id=int(linked_request_id)))
        linked_order = db.execute(
            "SELECT work_order_id, assigned_technician_id, status FROM calibration_work_orders WHERE request_id=?",
            (int(linked_request_id),)
        ).fetchone()
        if not linked_order or linked_order["assigned_technician_id"] != g.user["user_id"]:
            abort(403)
        if linked_order["status"] in ("AWAITING REVIEW", "COMPLETED", "CANCELLED"):
            flash("This work order is awaiting review or already closed; calibration data cannot be changed.", "error")
            return redirect(url_for("work_order_detail", work_order_id=linked_order["work_order_id"]))
    if request.method == "POST":
        f = request.form
        try:
            cal_date = datetime.strptime(f["cal_date"], "%Y-%m-%d").date().isoformat()
            refs = [float(x) for x in f.getlist("reference_value")]
            meass = [float(x) for x in f.getlist("measured_value")]
            raw = f.getlist("tolerance")
            tols = ([float(t) if t.strip() else s["tolerance"] for t in raw]
                    if raw else [s["tolerance"]] * len(refs))
            if (not refs or len(refs) != len(meass) or len(refs) != len(tols) or len(refs) > 30
                    or not all(math.isfinite(x) for x in refs + meass + tols)
                    or any(t < 0 for t in tols)):
                raise ValueError
        except ValueError:
            flash("Check the date and the numeric values for every measurement point.")
            return redirect(url_for("calibrate", sensor_id=sensor_id))
        std_id, std_details = None, None
        sid = f.get("standard_id", "")
        if sid.isdigit():
            std = db.execute("SELECT * FROM reference_standards WHERE standard_id=? AND active=1",
                             (int(sid),)).fetchone()
            problem = None
            if not std:
                problem = tr("Could not use that reference standard. Choose another one.")
            elif std["valid_until"] < cal_date:
                problem = tr("Cannot save: {code} expired on {d}. Use a standard that was valid "
                             "on the calibration date.").format(code=std["code"], d=std["valid_until"])
            elif std["calibrated_on"] > cal_date:
                problem = tr("Cannot save: {code} was only calibrated on {d}, after this "
                             "calibration date.").format(code=std["code"], d=std["calibrated_on"])
            if problem:
                flash(problem, "error")
                return redirect(url_for("calibrate", sensor_id=sensor_id))
            ref_text, std_id = f"{std['code']} – {std['name']}", std["standard_id"]
            std_details = json.dumps({"serial": std["serial_number"], "traceability": std["traceability"],
                                      "certificate": std["certificate_no"], "valid_until": std["valid_until"],
                                      "uncertainty": std["uncertainty"]}, ensure_ascii=False)
        else:
            ref_text = f.get("reference_standard", "").strip()
            if not ref_text:
                flash("Could not save: choose a reference standard or type its name.")
                return redirect(url_for("calibrate", sensor_id=sensor_id))
        request_id = None
        if f.get("request_id", "").isdigit():
            request_id = int(f["request_id"])
            if str(request_id) != linked_request_id:
                flash("The selected request does not match the assigned work order.", "error")
                return redirect(url_for("calibration_requests"))
            req = db.execute(
                "SELECT request_id, sensor_id, status FROM calibration_requests WHERE request_id=?",
                (request_id,)
            ).fetchone()
            if not req:
                flash("Could not link the selected calibration request.", "error")
                return redirect(url_for("calibrate", sensor_id=sensor_id))
            if req["sensor_id"] and req["sensor_id"] != sensor_id:
                flash("The selected request belongs to a different sensor.", "error")
                return redirect(url_for("calibrate", sensor_id=sensor_id))
            work_order = db.execute(
                "SELECT work_order_id, status FROM calibration_work_orders WHERE request_id=?",
                (request_id,)
            ).fetchone()
            if work_order and work_order["status"] in ("AWAITING REVIEW", "COMPLETED", "CANCELLED"):
                flash("This work order is awaiting review or already closed. New measurements cannot be recorded until it is returned to the technician.", "error")
                return redirect(url_for("work_order_detail", work_order_id=work_order["work_order_id"]))
        points = []
        for ref, meas, tol in zip(refs, meass, tols):
            err = round(meas - ref, 6)
            points.append((ref, meas, err, "PASS" if abs(err) <= tol else "FAIL", tol))
        worst = max(points, key=lambda p: abs(p[2]))          # point with the largest error
        result = "FAIL" if any(p[3] == "FAIL" for p in points) else "PASS"
        due = (date.fromisoformat(cal_date) + timedelta(days=s["interval_days"])).isoformat()
        cert = next_certificate(db, cal_date)
        cur = db.execute(
            "INSERT INTO calibrations(sensor_id,cal_date,reference_standard,reference_value,"
            "measured_value,error,result,certificate_no,next_due,performed_by,n_points,"
            "standard_id,standard_details,request_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sensor_id, cal_date, ref_text, worst[0], worst[1], worst[2],
             result, cert, due, g.user["full_name"], len(points), std_id, std_details, request_id))
        db.executemany(
            "INSERT INTO calibration_points(cal_id,point_no,reference_value,measured_value,"
            "error,result,tolerance) VALUES (?,?,?,?,?,?,?)",
            [(cur.lastrowid, i, *p) for i, p in enumerate(points, 1)])
        db.commit()
        if request_id:
            db.execute(
                "UPDATE calibration_requests SET status=?, updated_at=? "
                "WHERE request_id=? AND status NOT IN ('COMPLETED','CANCELLED')",
                ("IN CALIBRATION", datetime.now().isoformat(timespec="seconds"), request_id)
            )
            db.commit()
        work_order = db.execute(
            "SELECT work_order_id FROM calibration_work_orders WHERE request_id=?",
            (request_id,)
        ).fetchone() if request_id else None
        if work_order:
            flash("Calibration measurements saved. Submit the work order for review before the certificate is released.")
            return redirect(url_for("work_order_detail", work_order_id=work_order["work_order_id"]))
        msg = tr("{n} point, max error {err} {unit} → {result}. Certificate {cert} issued."
                 if len(points) == 1 else
                 "{n} points, max error {err} {unit} → {result}. Certificate {cert} issued.")
        flash(msg.format(n=len(points), err=f"{worst[2]:+}", unit=s["unit"], result=tr(result),
                         cert=cert), "ok")
        return redirect(url_for("certificate", cert=cert))
    standards_ = db.execute("SELECT * FROM reference_standards WHERE active=1 ORDER BY code").fetchall()
    requests_ = db.execute(
        "SELECT request_id, request_no, client_name, instrument_description FROM calibration_requests "
        "WHERE status NOT IN ('COMPLETED','CANCELLED') ORDER BY request_id DESC"
    ).fetchall()
    return render_template("calibrate.html", s=s, today=date.today().isoformat(),
                           standards=standards_, requests=requests_)


@app.route("/certificate/<cert>")
def certificate(cert):
    db = get_db()
    r = db.execute(
        "SELECT c.*, s.sensor_type, s.manufacturer, s.serial_number, s.tolerance, s.unit, "
        "st.name AS station FROM calibrations c JOIN sensors s USING(sensor_id) "
        "JOIN stations st USING(station_id) WHERE certificate_no=?", (cert,)).fetchone()
    if not r:
        abort(404)
    work_order = None
    if r["request_id"]:
        work_order = db.execute(
            "SELECT work_order_id, assigned_technician_id, status FROM calibration_work_orders WHERE request_id=?",
            (r["request_id"],)
        ).fetchone()
    # Release only the exact calibration record approved by an administrator.
    approved = db.execute(
        "SELECT 1 FROM calibration_review_history WHERE cal_id=? AND decision='APPROVED' LIMIT 1",
        (r["cal_id"],)
    ).fetchone()
    if not approved:
        if not work_order or g.user["role"] != "technician" or work_order["assigned_technician_id"] != g.user["user_id"]:
            abort(403)
        preview = True
    else:
        preview = False
    pts = db.execute("SELECT * FROM calibration_points WHERE cal_id=? ORDER BY point_no",
                     (r["cal_id"],)).fetchall()
    details = json.loads(r["standard_details"]) if r["standard_details"] else None
    return render_template("certificate.html", r=r, pts=pts, det=details, preview=preview)


@app.route("/due")
def due():
    days = request.args.get("days", 30, type=int)
    limit = (date.today() + timedelta(days=days)).isoformat()
    rows = [r for r in get_db().execute(LATEST + " ORDER BY s.sensor_id")
            if r["next_due"] is None or r["next_due"] <= limit or r["result"] == "FAIL"]
    return render_template("due.html", rows=rows, days=days)


# ------------------------------ reference standards ------------------------------
STD_FIELDS = ("code", "name", "standard_type", "manufacturer", "serial_number", "uncertainty",
              "traceability", "certificate_no", "calibrated_on", "valid_until")


def read_standard(f):
    d = {k: f.get(k, "").strip() for k in STD_FIELDS}
    if not d["code"] or not d["name"]:
        raise ValueError("Code and name are required.")
    try:
        cal, val = date.fromisoformat(d["calibrated_on"]), date.fromisoformat(d["valid_until"])
    except ValueError:
        raise ValueError("Enter valid calibration and expiry dates.")
    if val < cal:
        raise ValueError("The expiry date cannot be before the calibration date.")
    d["code"] = d["code"].upper()
    return d


@app.route("/standards")
def standards():
    rows = get_db().execute("SELECT * FROM reference_standards ORDER BY code").fetchall()
    return render_template("standards.html", rows=rows)


@app.route("/standards/new", methods=["GET", "POST"])
@admin_required
def new_standard():
    if request.method == "POST":
        try:
            d = read_standard(request.form)
            db = get_db()
            cur = db.execute(
                "INSERT INTO reference_standards(code,name,standard_type,manufacturer,serial_number,"
                "uncertainty,traceability,certificate_no,calibrated_on,valid_until) VALUES "
                "(:code,:name,:standard_type,:manufacturer,:serial_number,:uncertainty,:traceability,"
                ":certificate_no,:calibrated_on,:valid_until)", d)
            db.commit()
            flash("Reference standard added.")
            return redirect(url_for("standard", sid=cur.lastrowid))
        except ValueError as e:
            flash(tr("Could not save the standard") + ": " + tr(str(e)), "error")
        except sqlite3.IntegrityError:
            flash(tr("Could not save the standard") + ": " + tr("That code already exists."), "error")
    return render_template("standard_form.html", x=request.form if request.method == "POST" else None,
                           editing=False)


@app.route("/standards/<int:sid>")
def standard(sid):
    db = get_db()
    x = db.execute("SELECT * FROM reference_standards WHERE standard_id=?", (sid,)).fetchone()
    if not x:
        abort(404)
    used = db.execute("SELECT cal_date, sensor_id, result, certificate_no FROM calibrations "
                      "WHERE standard_id=? ORDER BY cal_id DESC LIMIT 100", (sid,)).fetchall()
    return render_template("standard.html", x=x, used=used)



@app.route("/standards/<int:sid>/delete", methods=["POST"])
@admin_required
def delete_standard(sid):
    db = get_db()
    x = db.execute("SELECT code FROM reference_standards WHERE standard_id=?", (sid,)).fetchone()
    if not x:
        abort(404)
    used = db.execute("SELECT COUNT(*) FROM calibrations WHERE standard_id=?", (sid,)).fetchone()[0]
    if used:
        flash(f"Reference standard '{x['code']}' cannot be deleted because it is linked to {used} calibration record(s).", "error")
        return redirect(url_for("standard", sid=sid))
    try:
        with db:
            db.execute("DELETE FROM reference_standards WHERE standard_id=?", (sid,))
        flash(f"Reference standard '{x['code']}' was deleted.")
    except sqlite3.Error:
        flash("Could not delete the reference standard.", "error")
    return redirect(url_for("standards"))

@app.route("/standards/<int:sid>/edit", methods=["GET", "POST"])
@admin_required
def edit_standard(sid):
    db = get_db()
    x = db.execute("SELECT * FROM reference_standards WHERE standard_id=?", (sid,)).fetchone()
    if not x:
        abort(404)
    if request.method == "POST":
        try:
            d = read_standard(request.form)
            d.update(active=1 if request.form.get("active") else 0, sid=sid)
            db.execute("UPDATE reference_standards SET code=:code, name=:name, standard_type=:standard_type,"
                       " manufacturer=:manufacturer, serial_number=:serial_number, uncertainty=:uncertainty,"
                       " traceability=:traceability, certificate_no=:certificate_no,"
                       " calibrated_on=:calibrated_on, valid_until=:valid_until, active=:active"
                       " WHERE standard_id=:sid", d)
            db.commit()
            flash("Reference standard updated.")
            return redirect(url_for("standard", sid=sid))
        except ValueError as e:
            flash(tr("Could not save the standard") + ": " + tr(str(e)), "error")
        except sqlite3.IntegrityError:
            flash(tr("Could not save the standard") + ": " + tr("That code already exists."), "error")
        x = request.form
    return render_template("standard_form.html", x=x, editing=True, sid=sid)


# ---------------------------------- export ----------------------------------
EXPORT_COLUMNS = [
    ("sensor_id", "Sensor ID"), ("station", "Station"), ("sensor_type", "Sensor type"),
    ("manufacturer", "Manufacturer"), ("serial_number", "Serial number"),
    ("cal_date", "Calibration date"), ("reference_standard", "Reference standard"),
    ("n_points", "Number of points"), ("point_no", "Point no."),
    ("point_tolerance", "Point tolerance (+/-)"),
    ("reference_value", "Reference value"), ("measured_value", "Results (reading)"),
    ("error", "Error"), ("result", "Pass/Fail"), ("overall_result", "Overall result"),
    ("certificate_no", "Certificate"), ("next_due", "Next due date"),
    ("performed_by", "Calibrated by"), ("unit", "Unit"), ("tolerance", "Tolerance (+/-)"),
    ("status", "Status"),
]

HISTORY_SQL = """
SELECT s.*, st.name AS station, c.cal_date, c.reference_standard, c.reference_value,
       c.measured_value, c.error, c.result, c.certificate_no, c.next_due, c.performed_by,
       c.n_points
FROM calibrations c JOIN sensors s USING(sensor_id) JOIN stations st USING(station_id)
"""

POINTS_SQL = """
SELECT s.*, st.name AS station, c.cal_date, c.reference_standard, c.n_points, p.point_no,
       p.reference_value, p.measured_value, p.error, p.result, p.tolerance AS point_tolerance,
       c.result AS overall_result,
       c.certificate_no, c.next_due, c.performed_by
FROM calibration_points p JOIN calibrations c USING(cal_id)
JOIN sensors s USING(sensor_id) JOIN stations st USING(station_id)
"""

STATUS_FILTERS = {"ok": "OK", "overdue": "Overdue", "failed": "Failed",
                  "never": "Never calibrated", "due_soon": "Due in"}


def export_rows(args):
    """Rows for the export, filtered by the query-string options. Returns (rows, scope)."""
    scope = args.get("scope") if args.get("scope") in ("history", "points") else "latest"
    where, params = [], []
    stations_ = [int(x) for x in args.getlist("station") if x.isdigit()]
    if stations_:
        where.append(f"s.station_id IN ({','.join('?' * len(stations_))})")
        params += stations_
    sensors_ = [x for x in args.getlist("sensor") if x]
    if sensors_:
        where.append(f"s.sensor_id IN ({','.join('?' * len(sensors_))})")
        params += sensors_
    for key, op in (("date_from", ">="), ("date_to", "<=")):
        val = args.get(key, "")
        try:
            date.fromisoformat(val)
        except ValueError:
            continue
        where.append(f"c.cal_date {op} ?")
        params.append(val)
    if args.get("result") in ("PASS", "FAIL"):
        where.append(("p.result" if scope == "points" else "c.result") + " = ?")
        params.append(args["result"])
    sql = {"history": HISTORY_SQL, "points": POINTS_SQL}.get(scope, LATEST)
    sql += (" WHERE " + " AND ".join(where) if where else "")
    sql += " ORDER BY s.sensor_id" + (", c.cal_date, c.cal_id" if scope != "latest" else "")
    sql += ", p.point_no" if scope == "points" else ""
    rows = [dict(r) for r in get_db().execute(sql, params)]
    for r in rows:
        r["status"] = status(r)[0] if scope == "latest" else ""
        r.setdefault("point_no", "")
        r.setdefault("point_tolerance", "")
        r.setdefault("overall_result", r.get("result"))
    wanted = STATUS_FILTERS.get(args.get("status", ""))
    if wanted and scope == "latest":
        rows = [r for r in rows if r["status"].startswith(wanted)]
    return rows, scope


def safe_cell(v):
    """Stop spreadsheet programs from running text that starts like a formula."""
    if v is None:
        return ""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@"):
        return "'" + v
    return v


@app.route("/export")
def export_csv():
    db = get_db()
    return render_template(
        "export.html", columns=EXPORT_COLUMNS,
        stations=db.execute("SELECT * FROM stations ORDER BY name").fetchall(),
        sensors=db.execute("SELECT s.sensor_id, s.sensor_type, s.station_id, st.name AS station "
                           "FROM sensors s JOIN stations st USING(station_id) "
                           "ORDER BY s.sensor_id").fetchall())


DATE_KEYS = {"cal_date", "next_due", "calibrated_on", "valid_until"}
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
STD_SHEET_COLUMNS = [("code", "Code"), ("name", "Name"), ("standard_type", "Type"),
                     ("manufacturer", "Manufacturer"), ("serial_number", "Serial number"),
                     ("uncertainty", "Uncertainty"), ("traceability", "Traceability"),
                     ("certificate_no", "Certificate"), ("calibrated_on", "Calibrated on"),
                     ("valid_until", "Valid until"), ("status", "Status"), ("active_txt", "Active")]


def export_label(args):
    sensors_ = [x for x in args.getlist("sensor") if x]
    stations_ = [x for x in args.getlist("station") if x.isdigit()]
    if len(sensors_) == 1:
        label = sensors_[0]
    elif len(stations_) == 1:
        st = get_db().execute("SELECT name FROM stations WHERE station_id=?",
                              (int(stations_[0]),)).fetchone()
        label = st["name"] if st else "station"
    else:
        label = "selection" if (sensors_ or stations_) else "all"
    return re.sub(r"[^A-Za-z0-9_-]+", "-", label).strip("-") or "export"


def export_value(key, v):
    """Pass/Fail and status words follow the report language."""
    if g.get("lang") != "ne" or v in (None, ""):
        return v
    if key in ("result", "overall_result"):
        return tr(v)
    if key == "status":
        return status_label(v)
    return v


def build_xlsx(rows, cols, scope, per_station, with_standards, banner):
    """Excel workbook with the banner on top of every sheet. Raises ImportError without openpyxl."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    fills = {k: PatternFill("solid", fgColor=v) for k, v in
             dict(green="D5F0DD", red="F8D4D1", amber="FBE8C4", grey="E3E6EB").items()}
    head_fill = PatternFill("solid", fgColor="14203A")
    wb = Workbook()
    wb.remove(wb.active)
    used = set()

    def new_sheet(name):
        base = re.sub(r"[\[\]:*?/\\]", "-", name)[:28] or "Sheet"
        n, i = base, 2
        while n.lower() in used:
            n, i = f"{base[:25]}-{i}", i + 1
        used.add(n.lower())
        return wb.create_sheet(n)

    def color(key, v):
        if key in ("result", "overall_result"):
            return "green" if v == "PASS" else "red" if v == "FAIL" else None
        if key == "status" and v:
            v = str(v)
            return ("green" if v in ("OK", "Valid") else "amber" if v.startswith(("Due in", "Expires in"))
                    else "red" if v in ("Overdue", "Failed", "Expired") else "grey")
        return None

    def write_table(ws, keys, headers, data):
        top, width = 1, max(len(headers), 4)
        if banner:
            for i, line in enumerate(banner, 1):
                ws.merge_cells(start_row=i, start_column=1, end_row=i, end_column=width)
                c = ws.cell(row=i, column=1, value=line)
                c.alignment = Alignment(horizontal="center")
                c.font = Font(bold=i in (1, 3), size=14 if i == 3 else 11,
                              color="B6202F" if i == 1 else "14203A")
            top = 6
        for j, h in enumerate(headers, 1):
            c = ws.cell(row=top, column=j, value=h)
            c.font, c.fill = Font(bold=True, color="FFFFFF"), head_fill
        for i, r in enumerate(data, top + 1):
            for j, k in enumerate(keys, 1):
                v = r.get(k)
                col = color(k, v)                       # colour from the original (English) value
                if v in (None, ""):
                    v = None
                else:
                    if k in DATE_KEYS and isinstance(v, str):
                        try:
                            v = date.fromisoformat(v)
                        except ValueError:
                            pass
                    v = export_value(k, v)
                cell = ws.cell(row=i, column=j, value=v)
                if isinstance(v, str) and v[:1] == "=":
                    cell.data_type = "s"                # text, never a formula
                if isinstance(v, date):
                    cell.number_format = "yyyy-mm-dd"
                if col:
                    cell.fill = fills[col]
        ws.freeze_panes = ws.cell(row=top + 1, column=1)
        ws.auto_filter.ref = f"A{top}:{get_column_letter(len(headers))}{max(top + len(data), top)}"
        for j, h in enumerate(headers, 1):
            longest = max([len(str(h))] + [len(str(ws.cell(row=i, column=j).value or ""))
                                           for i in range(top + 1, min(top + len(data), top + 200) + 1)])
            ws.column_dimensions[get_column_letter(j)].width = min(longest + 3, 42)

    keys, headers = [k for k, _ in cols], [h for _, h in cols]
    if per_station:
        groups = {}
        for r in rows:
            groups.setdefault(r["station"], []).append(r)
        if scope == "latest":
            summary = []
            for name, grp in groups.items():
                cnt = {"ok": 0, "soon": 0, "overdue": 0, "failed": 0, "never": 0}
                for r in grp:
                    cnt[status_key(r["status"])] += 1
                summary.append(dict(station=name, sensors=len(grp), **cnt))
            write_table(new_sheet(tr("Summary")),
                        ["station", "sensors", "ok", "soon", "overdue", "failed", "never"],
                        [tr(h) for h in ("Station", "Sensors", "OK", "Due within 30 days", "Overdue",
                                         "Failed", "Never calibrated")], summary)
        for name, grp in groups.items():
            write_table(new_sheet(name), keys, headers, grp)
    else:
        write_table(new_sheet(tr({"latest": "Register", "history": "History", "points": "Points"}[scope])),
                    keys, headers, rows)
    if with_standards:
        stds = [dict(x) for x in get_db().execute("SELECT * FROM reference_standards ORDER BY code")]
        for x in stds:
            x["status"] = standard_status(x)[0]
            x["active_txt"] = tr("Yes") if x["active"] else tr("No")
        write_table(new_sheet(tr("Reference standards")), [k for k, _ in STD_SHEET_COLUMNS],
                    [tr(h) for _, h in STD_SHEET_COLUMNS], stds)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


@app.route("/export/download")
def export_download():
    rows, scope = export_rows(request.args)
    if not rows:
        flash("No records match those filters.")
        return redirect(url_for("export_csv"))
    chosen = set(request.args.getlist("cols"))
    cols = [c for c in EXPORT_COLUMNS if c[0] in chosen] or list(EXPORT_COLUMNS)
    if scope != "latest":                        # status only makes sense for the register view
        cols = [c for c in cols if c[0] != "status"]
    if scope != "points":                        # per-point columns only for the points export
        cols = [c for c in cols if c[0] not in ("point_no", "overall_result", "point_tolerance")]
        rename = {"reference_value": "Reference value (worst point)",
                  "measured_value": "Results (worst point)", "error": "Max error"}
        cols = [(k, rename.get(k, h)) for k, h in cols]
    cols = [(k, tr(h)) for k, h in cols]         # column headings follow the report language
    banner = None if request.args.get("nobanner") else BANNER[g.lang]
    fmt = request.args.get("format", "csv")
    stamp = f"calibration_{scope}_{export_label(request.args)}_{date.today().isoformat()}"
    if fmt == "xlsx":
        try:
            data = build_xlsx(rows, cols, scope, bool(request.args.get("per_station")),
                              bool(request.args.get("with_standards")), banner)
        except ImportError:
            flash("Could not create the Excel file: the openpyxl package is missing. Install it with "
                  "“python -m pip install openpyxl” and restart the app.")
            return redirect(url_for("export_csv"))
        return Response(data, mimetype=XLSX_MIME,
                        headers={"Content-Disposition": f'attachment; filename="{stamp}.xlsx"'})
    out = io.StringIO()
    w = csv.writer(out)
    if banner:
        for line in banner:
            w.writerow([line])
        w.writerow([])
    w.writerow([h for _, h in cols])
    for r in rows:
        w.writerow([safe_cell(export_value(k, r[k])) for k, _ in cols])
    data = out.getvalue()
    if fmt == "excel" or g.lang == "ne":         # UTF-8 BOM so Excel shows °C and Nepali text
        data = "\ufeff" + data
    return Response(data.encode("utf-8"), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{stamp}.csv"'})


#if __name__ == "__main__":
    # host="0.0.0.0" lets other PCs on your network connect; use "127.0.0.1" for this PC only
  #  app.run(host="127.0.0.1", port=5000, debug=True)
if __name__ == "__main__":
    serve(app, host="127.0.0.1", port=5000)