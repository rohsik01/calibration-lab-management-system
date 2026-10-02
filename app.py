"""Calibration Laboratory Management System - Flask web app (local server).
Run:  python app.py   then open http://127.0.0.1:5000
"""
import base64
import csv
import getpass
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

# Database location can be overridden for deployments, but defaults to the
# project directory so starting the app from another working directory cannot
# accidentally create a second, empty calibration database.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.environ.get("CALIBRATION_DB", os.path.join(BASE_DIR, "calibration.db"))

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
    sensor_id TEXT REFERENCES sensors(sensor_id),
    cal_date TEXT NOT NULL, reference_standard TEXT NOT NULL,
    reference_value REAL NOT NULL, measured_value REAL NOT NULL, error REAL NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('PASS','FAIL')),
    certificate_no TEXT UNIQUE, next_due TEXT NOT NULL,
    mean_error REAL, max_error REAL,
    adjustment_status TEXT NOT NULL DEFAULT 'NOT REQUIRED' CHECK (adjustment_status IN ('NOT REQUIRED','REQUIRED','PERFORMED')),
    adjustment_notes TEXT, technician_remarks TEXT,
    standard_uncertainty REAL, resolution REAL, repeatability REAL,
    environmental_uncertainty REAL, other_uncertainty REAL,
    combined_standard_uncertainty REAL, coverage_factor REAL DEFAULT 2.0,
    expanded_uncertainty REAL, uncertainty_method TEXT DEFAULT 'RSS',
    certificate_issued_by INTEGER, certificate_issued_at TEXT, approved_revision INTEGER);
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('superadmin','admin','technician','general_user')),
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
    as_found_value REAL, as_found_error REAL,
    as_left_value REAL, as_left_error REAL,
    result TEXT NOT NULL CHECK (result IN ('PASS','FAIL')),
    as_found_result TEXT CHECK (as_found_result IN ('PASS','FAIL')),
    as_left_result TEXT CHECK (as_left_result IN ('PASS','FAIL')));
CREATE TABLE IF NOT EXISTS calibration_requests (
    request_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_no TEXT UNIQUE NOT NULL,
    client_name TEXT NOT NULL,
    contact_person TEXT,
    contact_phone TEXT,
    contact_email TEXT,
    sensor_id TEXT REFERENCES sensors(sensor_id),
    pending_sensor_type TEXT, pending_manufacturer TEXT, pending_serial_number TEXT,
    pending_interval_days INTEGER, pending_tolerance REAL, pending_unit TEXT,
    pending_station_name TEXT, pending_station_location TEXT,
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
CREATE TABLE IF NOT EXISTS calibration_request_status_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL REFERENCES calibration_requests(request_id) ON DELETE CASCADE,
    old_status TEXT,
    new_status TEXT NOT NULL,
    changed_by INTEGER REFERENCES users(user_id),
    changed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    comments TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(user_id),
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    old_value TEXT,
    new_value TEXT,
    details TEXT,
    ip_address TEXT,
    user_agent TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id);

CREATE TABLE IF NOT EXISTS calibration_revisions (
    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    cal_id INTEGER NOT NULL REFERENCES calibrations(cal_id) ON DELETE CASCADE,
    revision_no INTEGER NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('CREATED','SUBMITTED','RETURNED','CORRECTED','APPROVED')),
    snapshot_json TEXT NOT NULL,
    created_by INTEGER REFERENCES users(user_id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    review_id INTEGER,
    comments TEXT,
    UNIQUE(cal_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_calibration_revisions_cal ON calibration_revisions(cal_id, revision_no DESC);

CREATE TABLE IF NOT EXISTS calibration_review_history (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_id INTEGER NOT NULL REFERENCES calibration_work_orders(work_order_id),
    cal_id INTEGER NOT NULL REFERENCES calibrations(cal_id),
    submitted_by INTEGER NOT NULL REFERENCES users(user_id),
    submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    submitted_revision INTEGER NOT NULL DEFAULT 1,
    reviewed_by INTEGER REFERENCES users(user_id),
    reviewed_at TEXT,
    decision TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (decision IN ('PENDING','APPROVED','RETURNED')),
    comments TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    alert_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    endpoint TEXT,
    entity_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at TEXT,
    UNIQUE(user_id, alert_key)
);
CREATE INDEX IF NOT EXISTS idx_notifications_user_read ON notifications(user_id, read_at);
CREATE INDEX IF NOT EXISTS idx_notifications_created_at ON notifications(created_at);
"""

LATEST = """
SELECT s.*, st.name AS station, c.cal_date, c.reference_standard, c.reference_value,
       c.measured_value, c.error, c.result, c.certificate_no, c.next_due, c.performed_by, c.n_points
FROM sensors s JOIN stations st USING(station_id)
LEFT JOIN calibrations c ON c.cal_id = (
    SELECT MAX(cal_id) FROM calibrations WHERE sensor_id = s.sensor_id)
"""


def audit_event(action, entity_type=None, entity_id=None, old_value=None, new_value=None, details=None):
    """Record an auditable user action without storing passwords, CSRF tokens, or secrets."""
    db = get_db()
    user_id = g.user["user_id"] if getattr(g, "user", None) else None
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip and "," in ip:
        ip = ip.split(",", 1)[0].strip()
    ua = request.headers.get("User-Agent", "")[:500]
    db.execute(
        """INSERT INTO audit_log
           (user_id, action, entity_type, entity_id, old_value, new_value,
            details, ip_address, user_agent, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (user_id, action, entity_type, str(entity_id) if entity_id is not None else None,
         json.dumps(old_value, ensure_ascii=False, default=str) if old_value is not None else None,
         json.dumps(new_value, ensure_ascii=False, default=str) if new_value is not None else None,
         json.dumps(details, ensure_ascii=False, default=str) if details is not None else None,
         ip, ua, datetime.now().isoformat(timespec="seconds"))
    )

def _configure_connection(db):
    """Apply SQLite settings needed for a multi-user local/LAN deployment."""
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 30000")
    # WAL lets readers continue while another connection is writing.
    db.execute("PRAGMA journal_mode = WAL")
    # NORMAL is the recommended balance for WAL durability/performance.
    db.execute("PRAGMA synchronous = NORMAL")
    db.execute("PRAGMA temp_store = MEMORY")
    db.execute("PRAGMA cache_size = -64000")  # approximately 64 MiB

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB, timeout=30)
        g.db.row_factory = sqlite3.Row
        _configure_connection(g.db)
    return g.db

def calculate_measurement_uncertainty(form):
    """Calculate Type A/B components with root-sum-of-squares and k coverage factor."""
    def num(name, default=0.0):
        raw = form.get(name, "").strip()
        if raw == "":
            return default
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid uncertainty component: {name}.")
        return value
    standard = num("standard_uncertainty")
    resolution = num("resolution")
    repeatability = num("repeatability")
    environmental = num("environmental_uncertainty")
    other = num("other_uncertainty")
    k = num("coverage_factor", 2.0)
    if k <= 0:
        raise ValueError("Coverage factor k must be greater than zero.")
    combined = math.sqrt(
        standard**2 + (resolution**2 / 12.0) + repeatability**2 +
        environmental**2 + other**2
    )
    expanded = combined * k
    return {
        "standard_uncertainty": round(standard, 9),
        "resolution": round(resolution, 9),
        "repeatability": round(repeatability, 9),
        "environmental_uncertainty": round(environmental, 9),
        "other_uncertainty": round(other, 9),
        "combined_standard_uncertainty": round(combined, 9),
        "coverage_factor": round(k, 6),
        "expanded_uncertainty": round(expanded, 9),
        "uncertainty_method": "RSS",
    }


def backup_database(destination=None):
    """Create a consistent online SQLite backup using SQLite's backup API."""
    if destination is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = os.path.join(BASE_DIR, "backups", f"calibration_{stamp}.db")
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)
    src = sqlite3.connect(DB)
    try:
        _configure_connection(src)
        dst = sqlite3.connect(destination)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return destination


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db:
        db.close()

@app.after_request
def audit_mutating_request(response):
    # Endpoint-level audit catches administrative and technician changes even
    # when a route does not have a dedicated audit_event() call.
    if getattr(g, "user", None) and request.method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            ignored = {"csrf", "password", "password2", "current_password",
                       "totp_secret", "otp", "recovery_codes"}
            safe_form = {k: request.form.getlist(k) for k in request.form.keys()
                         if k not in ignored and "token" not in k.lower()}
            db = get_db()
            audit_event(
                "HTTP " + request.method,
                "route",
                request.endpoint,
                details={"path": request.path, "status_code": response.status_code,
                         "form": safe_form}
            )
            db.commit()
        except Exception:
            db.rollback()
    return response


with sqlite3.connect(DB, timeout=30) as _c:
    _configure_connection(_c)
    # Some legacy-table migrations rebuild tables with foreign-key references
    # (for example, users and calibrations). Temporarily disable FK enforcement
    # during the schema migration so SQLite permits the controlled table swap.
    # Foreign keys are re-enabled before normal application use.
    _c.execute("PRAGMA foreign_keys = OFF")
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
    # Upgrade legacy user table so the general_user role is accepted while preserving accounts.
    _user_sql = _c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='users'").fetchone()[0]
    if "'superadmin'" not in _user_sql or "'general_user'" not in _user_sql:
        # A previous interrupted migration may have left the staging table behind.
        # It is safe to remove because it is only a temporary migration table.
        _c.execute("DROP TABLE IF EXISTS users_new")
        _c.execute("""CREATE TABLE users_new (
            user_id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL COLLATE NOCASE,
            full_name TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('superadmin','admin','technician','general_user')),
            active INTEGER NOT NULL DEFAULT 1,
            two_factor_enabled INTEGER NOT NULL DEFAULT 0,
            totp_secret TEXT,
            recovery_codes TEXT)""")
        _c.execute("""INSERT INTO users_new
            (user_id, username, full_name, password_hash, role, active,
             two_factor_enabled, totp_secret, recovery_codes)
            SELECT user_id, username, full_name, password_hash, role, active,
                   two_factor_enabled, totp_secret, recovery_codes FROM users""")
        _c.execute("DROP TABLE users")
        _c.execute("ALTER TABLE users_new RENAME TO users")
    # Upgrade legacy calibration table to allow unregistered instruments during review.
    _cal_sql = _c.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='calibrations'").fetchone()[0]
    if "sensor_id TEXT NOT NULL" in _cal_sql or "sensor_id INTEGER NOT NULL" in _cal_sql:
        _c.execute("""CREATE TABLE calibrations_new (
            cal_id INTEGER PRIMARY KEY AUTOINCREMENT, sensor_id TEXT REFERENCES sensors(sensor_id),
            cal_date TEXT NOT NULL, reference_standard TEXT NOT NULL,
            reference_value REAL NOT NULL, measured_value REAL NOT NULL, error REAL NOT NULL,
            result TEXT NOT NULL CHECK (result IN ('PASS','FAIL')),
            certificate_no TEXT UNIQUE NOT NULL, next_due TEXT NOT NULL,
            performed_by TEXT, n_points INTEGER NOT NULL DEFAULT 1,
            standard_id INTEGER, standard_details TEXT, request_id INTEGER)""")
        _c.execute("""INSERT INTO calibrations_new
            (cal_id,sensor_id,cal_date,reference_standard,reference_value,measured_value,error,result,
             certificate_no,next_due,performed_by,n_points,standard_id,standard_details,request_id)
            SELECT cal_id,sensor_id,cal_date,reference_standard,reference_value,measured_value,error,result,
                   certificate_no,next_due,
                   CASE WHEN EXISTS (SELECT 1 FROM pragma_table_info('calibrations') WHERE name='performed_by') THEN performed_by ELSE NULL END,
                   CASE WHEN EXISTS (SELECT 1 FROM pragma_table_info('calibrations') WHERE name='n_points') THEN n_points ELSE 1 END,
                   CASE WHEN EXISTS (SELECT 1 FROM pragma_table_info('calibrations') WHERE name='standard_id') THEN standard_id ELSE NULL END,
                   CASE WHEN EXISTS (SELECT 1 FROM pragma_table_info('calibrations') WHERE name='standard_details') THEN standard_details ELSE NULL END,
                   CASE WHEN EXISTS (SELECT 1 FROM pragma_table_info('calibrations') WHERE name='request_id') THEN request_id ELSE NULL END
            FROM calibrations""")
        _c.execute("DROP TABLE calibrations"); _c.execute("ALTER TABLE calibrations_new RENAME TO calibrations")
    _req_cols = [r[1] for r in _c.execute("PRAGMA table_info(calibration_requests)")]
    for _col,_ddl in (("pending_sensor_type","TEXT"),("pending_manufacturer","TEXT"),("pending_serial_number","TEXT"),
                       ("pending_interval_days","INTEGER"),("pending_tolerance","REAL"),("pending_unit","TEXT"),
                       ("pending_station_name","TEXT"),("pending_station_location","TEXT"),("pending_station_type","TEXT"),("pending_station_id","INTEGER")):
        if _col not in _req_cols: _c.execute(f"ALTER TABLE calibration_requests ADD COLUMN {_col} {_ddl}")
    _c.execute("""UPDATE calibration_requests SET
        pending_sensor_type=(SELECT sensor_type FROM sensors s WHERE s.sensor_id=calibration_requests.sensor_id),
        pending_manufacturer=(SELECT manufacturer FROM sensors s WHERE s.sensor_id=calibration_requests.sensor_id),
        pending_serial_number=(SELECT serial_number FROM sensors s WHERE s.sensor_id=calibration_requests.sensor_id),
        pending_interval_days=(SELECT interval_days FROM sensors s WHERE s.sensor_id=calibration_requests.sensor_id),
        pending_tolerance=(SELECT tolerance FROM sensors s WHERE s.sensor_id=calibration_requests.sensor_id),
        pending_unit=(SELECT unit FROM sensors s WHERE s.sensor_id=calibration_requests.sensor_id),
        pending_station_name=(SELECT st.name FROM sensors s JOIN stations st ON st.station_id=s.station_id WHERE s.sensor_id=calibration_requests.sensor_id),
        pending_station_location=(SELECT st.location FROM sensors s JOIN stations st ON st.station_id=s.station_id WHERE s.sensor_id=calibration_requests.sensor_id)
        WHERE sensor_id IS NOT NULL AND pending_sensor_type IS NULL""")
    # upgrade older databases: calibration lifecycle and revision control
    _cal_cols = [r[1] for r in _c.execute("PRAGMA table_info(calibrations)")]
    if "revision_no" not in _cal_cols:
        _c.execute("ALTER TABLE calibrations ADD COLUMN revision_no INTEGER NOT NULL DEFAULT 1")
    if "lifecycle_status" not in _cal_cols:
        _c.execute("ALTER TABLE calibrations ADD COLUMN lifecycle_status TEXT NOT NULL DEFAULT 'DRAFT'")
    if "created_at" not in _cal_cols:
        _c.execute("ALTER TABLE calibrations ADD COLUMN created_at TEXT")
        _c.execute("UPDATE calibrations SET created_at=COALESCE(created_at, CURRENT_TIMESTAMP)")
    if "updated_at" not in _cal_cols:
        _c.execute("ALTER TABLE calibrations ADD COLUMN updated_at TEXT")
        _c.execute("UPDATE calibrations SET updated_at=COALESCE(updated_at, created_at, CURRENT_TIMESTAMP)")
    if "approved_by" not in _cal_cols:
        _c.execute("ALTER TABLE calibrations ADD COLUMN approved_by INTEGER")
    if "approved_at" not in _cal_cols:
        _c.execute("ALTER TABLE calibrations ADD COLUMN approved_at TEXT")
    _c.execute("""
        UPDATE calibrations
        SET lifecycle_status = CASE
            WHEN EXISTS (
                SELECT 1
                FROM calibration_review_history h
                WHERE h.cal_id = calibrations.cal_id
                  AND h.decision = 'APPROVED'
            ) THEN 'APPROVED'
            WHEN EXISTS (
                SELECT 1
                FROM calibration_review_history h
                WHERE h.cal_id = calibrations.cal_id
                  AND h.decision = 'PENDING'
            ) THEN 'SUBMITTED'
            WHEN EXISTS (
                SELECT 1
                FROM calibration_review_history h
                WHERE h.cal_id = calibrations.cal_id
                  AND h.decision = 'RETURNED'
            ) THEN 'RETURNED'
            ELSE COALESCE(lifecycle_status, 'DRAFT')
        END
    """)
    _review_cols = [r[1] for r in _c.execute("PRAGMA table_info(calibration_review_history)")]
    if "submitted_revision" not in _review_cols:
        _c.execute("ALTER TABLE calibration_review_history ADD COLUMN submitted_revision INTEGER NOT NULL DEFAULT 1")
    _c.execute("""
        INSERT OR IGNORE INTO calibration_revisions
            (cal_id, revision_no, event_type, snapshot_json, created_by, created_at)
        SELECT c.cal_id, c.revision_no, 'CREATED', '{}', NULL,
               COALESCE(c.created_at, CURRENT_TIMESTAMP)
        FROM calibrations c
    """)

    # upgrade older databases: extended measurement/result summary
    _cal_cols = [r[1] for r in _c.execute("PRAGMA table_info(calibrations)")]
    for _col,_ddl in (("mean_error","REAL"),("max_error","REAL"),("adjustment_status","TEXT NOT NULL DEFAULT 'NOT REQUIRED'"),("adjustment_notes","TEXT"),("technician_remarks","TEXT"),("standard_uncertainty","REAL"),("resolution","REAL"),("repeatability","REAL"),("environmental_uncertainty","REAL"),("other_uncertainty","REAL"),("combined_standard_uncertainty","REAL"),("coverage_factor","REAL DEFAULT 2.0"),("expanded_uncertainty","REAL"),("uncertainty_method","TEXT DEFAULT 'RSS'")):
        if _col not in _cal_cols: _c.execute(f"ALTER TABLE calibrations ADD COLUMN {_col} {_ddl}")
    _c.execute("UPDATE calibrations SET max_error=ABS(error), mean_error=error WHERE mean_error IS NULL OR max_error IS NULL")
    _c.execute("UPDATE calibrations SET adjustment_status='NOT REQUIRED' WHERE adjustment_status IS NULL OR adjustment_status=''")
    # upgrade older databases: record who performed each calibration
    if "performed_by" not in [r[1] for r in _c.execute("PRAGMA table_info(calibrations)")]:
        _c.execute("ALTER TABLE calibrations ADD COLUMN performed_by TEXT")
    # upgrade older databases: multi-point calibration
    if "n_points" not in [r[1] for r in _c.execute("PRAGMA table_info(calibrations)")]:
        _c.execute("ALTER TABLE calibrations ADD COLUMN n_points INTEGER NOT NULL DEFAULT 1")

    # Official certificate issuance fields. New calibrations intentionally have
    # no official certificate number until administrator approval.
    _cal_cols = [r[1] for r in _c.execute("PRAGMA table_info(calibrations)")]
    if "certificate_issued_by" not in _cal_cols:
        _c.execute("ALTER TABLE calibrations ADD COLUMN certificate_issued_by INTEGER")
    if "certificate_issued_at" not in _cal_cols:
        _c.execute("ALTER TABLE calibrations ADD COLUMN certificate_issued_at TEXT")
    if "approved_revision" not in _cal_cols:
        _c.execute("ALTER TABLE calibrations ADD COLUMN approved_revision INTEGER")
    _c.execute(
        """UPDATE calibrations
           SET certificate_issued_by=COALESCE(certificate_issued_by, approved_by),
               certificate_issued_at=COALESCE(certificate_issued_at, approved_at)
           WHERE lifecycle_status='APPROVED' AND certificate_no IS NOT NULL"""
    )

    # Older installations declared certificate_no NOT NULL. Rebuild once so
    # pending/returned calibrations can exist without an official certificate.
    _cal_info = _c.execute("PRAGMA table_info(calibrations)").fetchall()
    _cert_notnull = next((row[3] for row in _cal_info if row[1] == "certificate_no"), 0)
    if _cert_notnull:
        _c.execute("""CREATE TABLE calibrations_new (
            cal_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sensor_id TEXT REFERENCES sensors(sensor_id),
            cal_date TEXT NOT NULL, reference_standard TEXT NOT NULL,
            reference_value REAL NOT NULL, measured_value REAL NOT NULL, error REAL NOT NULL,
            result TEXT NOT NULL CHECK (result IN ('PASS','FAIL')),
            certificate_no TEXT UNIQUE, next_due TEXT NOT NULL,
            mean_error REAL, max_error REAL,
            adjustment_status TEXT NOT NULL DEFAULT 'NOT REQUIRED' CHECK (adjustment_status IN ('NOT REQUIRED','REQUIRED','PERFORMED')),
            adjustment_notes TEXT, technician_remarks TEXT,
            standard_uncertainty REAL, resolution REAL, repeatability REAL,
            environmental_uncertainty REAL, other_uncertainty REAL,
            combined_standard_uncertainty REAL, coverage_factor REAL DEFAULT 2.0,
            expanded_uncertainty REAL, uncertainty_method TEXT DEFAULT 'RSS',
            performed_by TEXT, n_points INTEGER NOT NULL DEFAULT 1,
            standard_id INTEGER, standard_details TEXT, request_id INTEGER,
            revision_no INTEGER NOT NULL DEFAULT 1,
            lifecycle_status TEXT NOT NULL DEFAULT 'DRAFT',
            created_at TEXT, updated_at TEXT,
            approved_by INTEGER, approved_at TEXT,
            certificate_issued_by INTEGER, certificate_issued_at TEXT
        )""")
        _c.execute("""INSERT INTO calibrations_new (
            cal_id,sensor_id,cal_date,reference_standard,reference_value,measured_value,error,result,
            certificate_no,next_due,mean_error,max_error,adjustment_status,adjustment_notes,technician_remarks,
            standard_uncertainty,resolution,repeatability,environmental_uncertainty,other_uncertainty,
            combined_standard_uncertainty,coverage_factor,expanded_uncertainty,uncertainty_method,
            performed_by,n_points,standard_id,standard_details,request_id,revision_no,lifecycle_status,
            created_at,updated_at,approved_by,approved_at,certificate_issued_by,certificate_issued_at)
            SELECT cal_id,sensor_id,cal_date,reference_standard,reference_value,measured_value,error,result,
                   certificate_no,next_due,mean_error,max_error,adjustment_status,adjustment_notes,technician_remarks,
                   standard_uncertainty,resolution,repeatability,environmental_uncertainty,other_uncertainty,
                   combined_standard_uncertainty,coverage_factor,expanded_uncertainty,uncertainty_method,
                   performed_by,n_points,standard_id,standard_details,request_id,revision_no,lifecycle_status,
                   created_at,updated_at,approved_by,approved_at,certificate_issued_by,certificate_issued_at
            FROM calibrations""")
        _c.execute("DROP TABLE calibrations")
        _c.execute("ALTER TABLE calibrations_new RENAME TO calibrations")
    # older single-point calibrations become calibrations with one point
    _c.execute("""INSERT INTO calibration_points(cal_id, point_no, reference_value, measured_value, error, result)
                   SELECT cal_id, 1, reference_value, measured_value, error, result FROM calibrations c
                   WHERE NOT EXISTS (SELECT 1 FROM calibration_points p WHERE p.cal_id = c.cal_id)""")
    # upgrade older databases: preserve both as-found and as-left readings.
    _point_cols = [r[1] for r in _c.execute("PRAGMA table_info(calibration_points)")]
    for _col,_ddl in (("as_found_value","REAL"),("as_found_error","REAL"),("as_left_value","REAL"),("as_left_error","REAL"),("as_found_result","TEXT"),("as_left_result","TEXT")):
        if _col not in _point_cols:
            _c.execute(f"ALTER TABLE calibration_points ADD COLUMN {_col} {_ddl}")
    _c.execute("""UPDATE calibration_points
                  SET as_found_value=COALESCE(as_found_value, measured_value),
                      as_found_error=COALESCE(as_found_error, error),
                      as_left_value=COALESCE(as_left_value, measured_value),
                      as_left_error=COALESCE(as_left_error, error),
                      as_found_result=COALESCE(as_found_result, result),
                      as_left_result=COALESCE(as_left_result, result)
                  WHERE as_found_value IS NULL OR as_left_value IS NULL""")
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
    # Restore SQLite foreign-key enforcement after all legacy table rebuilds.
    _c.execute("PRAGMA foreign_keys = ON")

    # Query-performance indexes. Foreign keys are not automatically indexed
    # by SQLite, so add indexes for the relationships and common dashboard/report
    # filters. IF NOT EXISTS makes this safe for every startup and old databases.
    _c.executescript("""
    CREATE INDEX IF NOT EXISTS idx_sensors_station_id ON sensors(station_id);
    CREATE INDEX IF NOT EXISTS idx_sensors_serial_number ON sensors(serial_number);
    CREATE INDEX IF NOT EXISTS idx_calibrations_sensor_date ON calibrations(sensor_id, cal_date DESC);
    CREATE INDEX IF NOT EXISTS idx_calibrations_date ON calibrations(cal_date);
    CREATE INDEX IF NOT EXISTS idx_calibrations_result ON calibrations(result);
    CREATE INDEX IF NOT EXISTS idx_calibrations_standard_id ON calibrations(standard_id);
    CREATE INDEX IF NOT EXISTS idx_calibrations_request_id ON calibrations(request_id);
    CREATE INDEX IF NOT EXISTS idx_calibrations_lifecycle ON calibrations(lifecycle_status);
    CREATE INDEX IF NOT EXISTS idx_calibration_revisions_cal ON calibration_revisions(cal_id, revision_no DESC);
    CREATE INDEX IF NOT EXISTS idx_calibration_points_cal_id ON calibration_points(cal_id);
    CREATE INDEX IF NOT EXISTS idx_calibration_points_result ON calibration_points(result);
    CREATE INDEX IF NOT EXISTS idx_requests_status_updated ON calibration_requests(status, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_requests_received_date ON calibration_requests(received_date);
    CREATE INDEX IF NOT EXISTS idx_requests_sensor_id ON calibration_requests(sensor_id);
    CREATE INDEX IF NOT EXISTS idx_requests_created_by ON calibration_requests(created_by);
    CREATE INDEX IF NOT EXISTS idx_work_orders_technician_status
        ON calibration_work_orders(assigned_technician_id, status);
    CREATE INDEX IF NOT EXISTS idx_work_orders_target_date
        ON calibration_work_orders(target_date);
    CREATE INDEX IF NOT EXISTS idx_work_orders_standard_id
        ON calibration_work_orders(standard_id);
    CREATE INDEX IF NOT EXISTS idx_request_history_request_date
        ON calibration_request_status_history(request_id, changed_at DESC);
    CREATE INDEX IF NOT EXISTS idx_review_history_work_order
        ON calibration_review_history(work_order_id, submitted_at DESC);
    CREATE INDEX IF NOT EXISTS idx_review_history_decision
        ON calibration_review_history(decision, reviewed_at);
    CREATE INDEX IF NOT EXISTS idx_standards_valid_until
        ON reference_standards(valid_until, active);
    CREATE INDEX IF NOT EXISTS idx_standards_active_code
        ON reference_standards(active, code);
    CREATE INDEX IF NOT EXISTS idx_audit_entity_created
        ON audit_log(entity_type, entity_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_action_created
        ON audit_log(action, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_audit_user_created
        ON audit_log(user_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_notifications_user_created
        ON notifications(user_id, created_at DESC);
    """)
    # Upgrade older databases: seed status-history snapshots.
    _c.execute("""INSERT INTO calibration_request_status_history
        (request_id, old_status, new_status, changed_by, changed_at, comments)
        SELECT r.request_id, NULL, r.status, u.user_id,
               COALESCE(r.created_at, CURRENT_TIMESTAMP),
               'Initial workflow history snapshot'
        FROM calibration_requests r
        LEFT JOIN users u ON u.full_name = r.created_by
        WHERE NOT EXISTS (
            SELECT 1 FROM calibration_request_status_history h
            WHERE h.request_id = r.request_id
        )""")



def calibration_snapshot(db, cal_id):
    """Capture the complete editable calibration state for immutable revision history."""
    cal = db.execute("SELECT * FROM calibrations WHERE cal_id=?", (cal_id,)).fetchone()
    if not cal:
        raise ValueError("Calibration record not found.")
    points = db.execute("SELECT * FROM calibration_points WHERE cal_id=? ORDER BY point_no", (cal_id,)).fetchall()
    return {"calibration": dict(cal), "points": [dict(p) for p in points]}


def record_calibration_revision(db, cal_id, event_type, created_by=None, review_id=None, comments=None):
    """Persist an immutable snapshot of the current calibration revision."""
    cal = db.execute("SELECT revision_no FROM calibrations WHERE cal_id=?", (cal_id,)).fetchone()
    if not cal:
        raise ValueError("Calibration record not found.")
    snapshot = calibration_snapshot(db, cal_id)
    db.execute(
        """INSERT OR REPLACE INTO calibration_revisions
           (cal_id, revision_no, event_type, snapshot_json, created_by, created_at, review_id, comments)
           VALUES (?,?,?,?,?,?,?,?)""",
        (cal_id, cal["revision_no"], event_type, json.dumps(snapshot, ensure_ascii=False, default=str),
         created_by, datetime.now().isoformat(timespec="seconds"), review_id, comments)
    )
    return cal["revision_no"]


def backfill_calibration_revision_snapshots():
    """Populate real snapshots for legacy revision rows created during migration."""
    with app.app_context():
        db = get_db()
        rows = db.execute(
            "SELECT revision_id, cal_id FROM calibration_revisions WHERE snapshot_json='{}'"
        ).fetchall()
        for row in rows:
            snapshot = calibration_snapshot(db, row["cal_id"])
            db.execute(
                "UPDATE calibration_revisions SET snapshot_json=? WHERE revision_id=?",
                (json.dumps(snapshot, ensure_ascii=False, default=str), row["revision_id"])
            )
        db.commit()



backfill_calibration_revision_snapshots()


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
    """Number of sensors needing attention, shown as badges in the sidebar."""
    if not g.get("user"):
        return {}
    db = get_db()
    rows = db.execute(LATEST).fetchall()
    stds = db.execute("SELECT * FROM reference_standards WHERE active=1").fetchall()
    sensor_alerts = sum(1 for r in rows if status(r)[0] != "OK")
    standard_alerts = sum(1 for x in stds if standard_status(x)[0] != "Valid")
    pending_reviews = db.execute("SELECT COUNT(*) FROM calibration_review_history WHERE decision='PENDING'").fetchone()[0]
    if g.user["role"] in ("admin", "superadmin"):
        unassigned = db.execute("""SELECT COUNT(*) FROM calibration_requests r
                                   WHERE r.status='REVIEWED'
                                     AND NOT EXISTS (
                                       SELECT 1 FROM calibration_work_orders w
                                       WHERE w.request_id=r.request_id
                                         AND w.status IN ('ASSIGNED','IN PROGRESS','AWAITING REVIEW')
                                     )""").fetchone()[0]
        operational_alerts = sensor_alerts + standard_alerts + pending_reviews + unassigned
    else:
        assigned = db.execute("""SELECT COUNT(*) FROM calibration_work_orders
                                 WHERE assigned_technician_id=? AND status='ASSIGNED'""",
                              (g.user["user_id"],)).fetchone()[0]
        operational_alerts = sensor_alerts + assigned
    # Sidebar workflow counters.
    # Admins see all actionable new requests, active work orders and pending reviews.
    # Technicians see only their own actionable work orders; general users see
    # new requests relevant to their own submitted requests.
    if g.user["role"] in ("admin", "superadmin"):
        new_calibration_requests = db.execute(
            "SELECT COUNT(*) FROM calibration_requests WHERE status='RECEIVED'"
        ).fetchone()[0]
        work_order_count = db.execute(
            "SELECT COUNT(*) FROM calibration_work_orders "
            "WHERE status IN ('ASSIGNED','IN PROGRESS','AWAITING REVIEW')"
        ).fetchone()[0]
        calibration_review_count = pending_reviews
    elif g.user["role"] == "technician":
        new_calibration_requests = db.execute(
            """SELECT COUNT(*) FROM calibration_requests r
               JOIN calibration_work_orders w ON w.request_id=r.request_id
               WHERE w.assigned_technician_id=?
                 AND r.status IN ('ASSIGNED','IN CALIBRATION')""",
            (g.user["user_id"],)
        ).fetchone()[0]
        work_order_count = db.execute(
            """SELECT COUNT(*) FROM calibration_work_orders
               WHERE assigned_technician_id=?
                 AND status IN ('ASSIGNED','IN PROGRESS','AWAITING REVIEW')""",
            (g.user["user_id"],)
        ).fetchone()[0]
        calibration_review_count = 0
    else:
        new_calibration_requests = db.execute(
            """SELECT COUNT(*) FROM calibration_requests
               WHERE created_by=?
                 AND status IN ('RECEIVED','REVIEWED','ASSIGNED','IN CALIBRATION','UNDER REVIEW')""",
            (g.user["full_name"],)
        ).fetchone()[0]
        work_order_count = 0
        calibration_review_count = 0

    due_overdue_count = db.execute(
        """SELECT COUNT(*) FROM sensors s
           LEFT JOIN calibrations c ON c.cal_id = (
               SELECT MAX(c2.cal_id) FROM calibrations c2
               WHERE c2.sensor_id=s.sensor_id
           )
           WHERE c.next_due IS NOT NULL
             AND date(c.next_due) <= date('now','+30 day')"""
    ).fetchone()[0]

    notifications_table = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='notifications'"
    ).fetchone()
    if notifications_table:
        sync_notifications(db, build_operational_alerts(db))
        unread_notifications = db.execute(
            "SELECT COUNT(*) FROM notifications WHERE user_id=? AND read_at IS NULL",
            (g.user["user_id"],)
        ).fetchone()[0]
    else:
        unread_notifications = 0

    return {"nav_alerts": sensor_alerts, "std_alerts": standard_alerts,
            "operational_alerts": operational_alerts,
            "new_calibration_requests": new_calibration_requests,
            "work_order_count": work_order_count,
            "calibration_review_count": calibration_review_count,
            "due_overdue_count": due_overdue_count,
            "unread_notifications": unread_notifications}


def next_certificate(db, cal_date):
    """Return the next unused official certificate number for the calibration year."""
    prefix = f"CAL-{cal_date[:4]}-"
    row = db.execute(
        "SELECT MAX(CAST(substr(certificate_no, ?) AS INTEGER)) AS n "
        "FROM calibrations WHERE certificate_no LIKE ?",
        (len(prefix) + 1, prefix + "%")
    ).fetchone()
    n = int(row["n"] or 0) + 1
    candidate = f"{prefix}{n:04d}"
    while db.execute("SELECT 1 FROM calibrations WHERE certificate_no=?", (candidate,)).fetchone():
        n += 1
        candidate = f"{prefix}{n:04d}"
    return candidate


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

REQUEST_STATUS_TRANSITIONS = {
    "RECEIVED": {"REVIEWED", "CANCELLED"},
    "REVIEWED": {"ASSIGNED", "CANCELLED"},
    "ASSIGNED": {"IN CALIBRATION", "CANCELLED"},
    "IN CALIBRATION": {"UNDER REVIEW", "CANCELLED"},
    "UNDER REVIEW": {"COMPLETED", "IN CALIBRATION"},
    "COMPLETED": set(),
    "CANCELLED": set(),
}

def transition_request_status(db, request_id, new_status, changed_by=None, comments=None):
    row = db.execute("SELECT status FROM calibration_requests WHERE request_id=?", (request_id,)).fetchone()
    if not row:
        raise ValueError("Calibration request not found.")
    current = row["status"]
    if current == new_status:
        raise ValueError("Request is already " + new_status + ".")
    if new_status not in REQUEST_STATUS_TRANSITIONS.get(current, set()):
        raise ValueError("Invalid status transition: " + current + " -> " + new_status + ".")
    now = datetime.now().isoformat(timespec="seconds")
    db.execute("UPDATE calibration_requests SET status=?, updated_at=? WHERE request_id=?",
               (new_status, now, request_id))
    audit_event(
        "STATUS_CHANGED", "calibration_request", request_id,
        old_value={"status": current},
        new_value={"status": new_status},
        details={"comments": comments} if comments else None
    )
    db.execute("""INSERT INTO calibration_request_status_history
                  (request_id, old_status, new_status, changed_by, changed_at, comments)
                  VALUES (?,?,?,?,?,?)""",
               (request_id, current, new_status, changed_by, now, comments))
    return now



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
    if g.user and g.user["role"] == "general_user":
        allowed = {"index", "calibration_requests", "new_calibration_request",
                   "calibration_request", "certificate", "account", "logout",
                   "set_lang", "static"}
        if request.endpoint not in allowed:
            abort(403)


def admin_required(f):
    """Require a laboratory administrator or superadministrator."""
    @wraps(f)
    def wrapper(*a, **kw):
        if g.user["role"] not in ("admin", "superadmin"):
            abort(403)
        return f(*a, **kw)
    return wrapper


def superadmin_required(f):
    """Require the dedicated superadministrator role for critical system tasks."""
    @wraps(f)
    def wrapper(*a, **kw):
        if g.user["role"] != "superadmin":
            abort(403)
        return f(*a, **kw)
    return wrapper


def check_new_password(pw, pw2):
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if pw != pw2:
        return "Passwords do not match."
    return None


# When app.py is executed directly, Python names this module "__main__".
# Route modules import the shared Flask app as "app", so alias this module first
# to prevent Python from loading app.py a second time and creating a second Flask app.
if __name__ == "__main__":
    import sys
    sys.modules.setdefault("app", sys.modules[__name__])

# Route modules are loaded after the shared application setup and helpers.
from routes import auth, users, dashboard, work_orders, audit, reviews, requests, stations
from routes import sensors, calibrations, notifications, standards, reports, database

# Navigation counters use these notification helpers after all route modules load.
from routes.notifications import build_operational_alerts, sync_notifications

def run_server():
    """Run the application with the same app object used by imports/tests.

    Host and port can be overridden with CALIBRATION_HOST/CALIBRATION_PORT.
    By default the server remains local-only for safety.
    """
    host = os.environ.get("CALIBRATION_HOST", "127.0.0.1")
    port = int(os.environ.get("CALIBRATION_PORT", "5000"))
    print(f"Calibration Lab server: http://{host}:{port}", flush=True)
    serve(app, host=host, port=port)


def create_superadmin_cli():
    """Create the first superadministrator from the local server console."""
    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    _configure_connection(db)
    try:
        if db.execute("SELECT COUNT(*) FROM users WHERE role='superadmin' AND active=1").fetchone()[0]:
            print("An active superadmin already exists.", flush=True)
            return 1
        username = input("Superadmin username: ").strip()
        full_name = input("Full name: ").strip() or username
        password = getpass.getpass("Password: ")
        password2 = getpass.getpass("Confirm password: ")
        err = check_new_password(password, password2)
        if err:
            print(err, flush=True)
            return 1
        if not username:
            print("Username is required.", flush=True)
            return 1
        try:
            db.execute(
                "INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,'superadmin')",
                (username, full_name, generate_password_hash(password))
            )
            db.commit()
        except sqlite3.IntegrityError:
            print("That username already exists.", flush=True)
            return 1
        print(f"Superadmin '{username}' created successfully.", flush=True)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    if "--backup" in sys.argv:
        print(f"Database backup created: {backup_database()}", flush=True)
    elif "--create-superadmin" in sys.argv:
        raise SystemExit(create_superadmin_cli())
    else:
        run_server()


# workflow optimization marker
