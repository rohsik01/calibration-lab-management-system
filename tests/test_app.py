import math
from pathlib import Path

import pytest

from flask import g

from app import (
    admin_required,
    app as flask_app,
    calculate_measurement_uncertainty,
    certificate_verification_token,
    reviewer_required,
    superadmin_required,
    user_has_role,
)
from routes.calibrations import calibration_delete_blocked
from routes.sensors import sensor_delete_blocked
from routes.users import is_last_active_superadmin
from routes.work_orders import WORK_ORDER_STATUS_TRANSITIONS
from app import validate_calibration_record_for_submission


@pytest.fixture
def client():
    with flask_app.test_client() as client:
        yield client


def test_home_redirects_to_login(client):
    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.location


def test_about_route_is_not_present(client):
    response = client.get("/about")
    assert response.status_code == 404


def test_uncertainty_calculation_uses_rss_and_coverage_factor():
    form = {
        "standard_uncertainty": "0.3",
        "resolution": "0.12",
        "repeatability": "0.4",
        "environmental_uncertainty": "0.2",
        "other_uncertainty": "0.1",
        "coverage_factor": "2",
    }

    result = calculate_measurement_uncertainty(form)
    expected_combined = math.sqrt(0.3**2 + 0.12**2 / 12 + 0.4**2 + 0.2**2 + 0.1**2)

    assert result["uncertainty_method"] == "RSS"
    assert math.isclose(result["combined_standard_uncertainty"], expected_combined, rel_tol=1e-8)
    assert math.isclose(result["expanded_uncertainty"], expected_combined * 2, rel_tol=1e-8)


@pytest.mark.parametrize("field,value", [
    ("standard_uncertainty", "-0.1"),
    ("resolution", "nan"),
    ("repeatability", "inf"),
    ("environmental_uncertainty", "-1"),
    ("other_uncertainty", "not-a-number"),
    ("coverage_factor", "0"),
])
def test_uncertainty_rejects_invalid_components(field, value):
    with pytest.raises(ValueError):
        calculate_measurement_uncertainty({field: value})


def test_calibration_review_template_uses_as_found_and_as_left_not_legacy_measured_value():
    template_path = Path(__file__).resolve().parents[1] / "templates" / "reviews.html"
    template = template_path.read_text(encoding="utf-8")

    assert "p.as_found_value" in template
    assert "p.as_found_error" in template
    assert "p.as_left_value" in template
    assert "p.as_left_error" in template
    assert "Final Error" in template
    assert "p.measured_value" not in template


def test_calibration_review_api_selects_as_found_and_as_left_fields():
    route_path = Path(__file__).resolve().parents[1] / "routes" / "reviews.py"
    route_source = route_path.read_text(encoding="utf-8")

    assert "as_found_value, as_found_error, as_found_result" in route_source
    assert "as_left_value, as_left_error, as_left_result" in route_source
    assert "SELECT point_no, reference_value, measured_value" not in route_source


@pytest.mark.parametrize(
    "lifecycle_status,certificate_no,revision_count,review_count,blocked",
    [
        ("APPROVED", "CAL-2026-0001", 1, 1, True),
        ("SUBMITTED", None, 1, 1, True),
        ("RETURNED", None, 2, 1, True),
        ("DRAFT", "CAL-2026-0002", 0, 0, True),
        ("DRAFT", None, 1, 0, True),
        ("DRAFT", None, 0, 1, True),
        ("DRAFT", None, 0, 0, False),
    ],
)
def test_calibration_delete_protection_preserves_controlled_history(
    lifecycle_status, certificate_no, revision_count, review_count, blocked
):
    row = {"lifecycle_status": lifecycle_status, "certificate_no": certificate_no}
    assert calibration_delete_blocked(row, revision_count, review_count) is blocked


def test_approved_calibration_edit_and_delete_paths_are_protected():
    route_path = Path(__file__).resolve().parents[1] / "routes" / "calibrations.py"
    route_source = route_path.read_text(encoding="utf-8")

    assert 'if cal["lifecycle_status"] == "APPROVED":' in route_source
    assert "Approved calibration records are immutable." in route_source
    assert "CALIBRATION_DELETE_BLOCKED" in route_source
    assert "calibration_revisions WHERE cal_id=?" in route_source
    assert "calibration_review_history WHERE cal_id=?" in route_source


def test_calibration_history_template_only_offers_delete_for_unrevisioned_drafts():
    template_path = Path(__file__).resolve().parents[1] / "templates" / "sensor.html"
    template = template_path.read_text(encoding="utf-8")

    assert 'h.lifecycle_status == "DRAFT"' in template
    assert "not h.certificate_no" in template
    assert "not h.revision_no" in template
    assert 'tr("Protected")' in template



@pytest.mark.parametrize(
    "rows,blocked",
    [
        ([], False),
        ([{"lifecycle_status": "DRAFT", "certificate_no": None, "revision_count": 0, "review_count": 0}], False),
        ([{"lifecycle_status": "APPROVED", "certificate_no": "CAL-2026-0001", "revision_count": 1, "review_count": 1}], True),
        ([{"lifecycle_status": "SUBMITTED", "certificate_no": None, "revision_count": 1, "review_count": 1}], True),
        ([{"lifecycle_status": "RETURNED", "certificate_no": None, "revision_count": 2, "review_count": 1}], True),
        ([{"lifecycle_status": "DRAFT", "certificate_no": "CAL-2026-0002", "revision_count": 0, "review_count": 0}], True),
        ([{"lifecycle_status": "DRAFT", "certificate_no": None, "revision_count": 1, "review_count": 0}], True),
        ([{"lifecycle_status": "DRAFT", "certificate_no": None, "revision_count": 0, "review_count": 1}], True),
        (
            [
                {"lifecycle_status": "DRAFT", "certificate_no": None, "revision_count": 0, "review_count": 0},
                {"lifecycle_status": "APPROVED", "certificate_no": "CAL-2026-0003", "revision_count": 1, "review_count": 1},
            ],
            True,
        ),
    ],
)
def test_sensor_delete_protection_preserves_any_controlled_calibration_history(rows, blocked):
    assert sensor_delete_blocked(rows) is blocked


def test_sensor_delete_route_checks_calibration_history_and_audits_blocked_attempts():
    route_path = Path(__file__).resolve().parents[1] / "routes" / "sensors.py"
    route_source = route_path.read_text(encoding="utf-8")

    assert "def sensor_delete_blocked(calibration_rows):" in route_source
    assert "calibration_revisions cr WHERE cr.cal_id=c.cal_id" in route_source
    assert "calibration_review_history rh WHERE rh.cal_id=c.cal_id" in route_source
    assert 'audit_event(' in route_source
    assert '"SENSOR_DELETE_BLOCKED"' in route_source
    assert "Retain the sensor and calibration records for traceability." in route_source


def test_sensor_history_ui_does_not_offer_sensor_deletion_when_calibrations_exist():
    template_path = Path(__file__).resolve().parents[1] / "templates" / "sensor.html"
    template = template_path.read_text(encoding="utf-8")

    assert "has_role('admin') or has_role('superadmin')" in template
    assert "not hist" in template
    assert "Sensor protected by calibration history" in template




def test_station_management_requires_administrator_and_explicit_station_id():
    route_path = Path(__file__).resolve().parents[1] / "routes" / "stations.py"
    template_path = Path(__file__).resolve().parents[1] / "templates" / "stations.html"
    route_source = route_path.read_text(encoding="utf-8")
    template_source = template_path.read_text(encoding="utf-8")

    assert '@admin_required\ndef stations():' in route_source
    assert 'station_id_raw = request.form.get("station_id", "").strip()' in route_source
    assert 'INSERT INTO stations(station_id, name, location, type, updated_at)' in route_source
    assert "Station ID" in template_source
    assert 'name="station_id"' in template_source
    assert 'name="station_id" type="number"' in template_source
    assert "Station ID is required." in route_source
    assert "Station ID: enter the unique positive numeric Station ID" in route_source


def test_technician_calibration_form_cannot_create_new_stations():
    route_path = Path(__file__).resolve().parents[1] / "routes" / "calibrations.py"
    template_path = Path(__file__).resolve().parents[1] / "templates" / "calibrate_pending.html"
    route_source = route_path.read_text(encoding="utf-8")
    template_source = template_path.read_text(encoding="utf-8")

    assert 'SELECT station_id, name, location, type FROM stations WHERE station_id=?' in route_source
    assert 'new_station_name' not in route_source
    assert 'id="add_station"' not in template_source
    assert 'id="new_station_fields"' not in template_source
    assert 'name="station_id"' in template_source
    assert "administrator-maintained station" in template_source

def test_certificate_verification_token_is_stable_and_nontrivial():
    first = certificate_verification_token("CAL-2026-0001")
    second = certificate_verification_token("CAL-2026-0001")
    other = certificate_verification_token("CAL-2026-0002")
    assert first == second
    assert first != other
    assert len(first) == 40


def test_certificate_verification_uses_compact_signed_url_not_embedded_measurement_payload():
    route_path = Path(__file__).resolve().parents[1] / "routes" / "calibrations.py"
    source = route_path.read_text(encoding="utf-8")
    assert "certificate_verification_url" in source
    assert "qr_code = _qr_data_uri(verification_url)" in source
    assert "NO WEB / LOCALHOST LINK" not in source
    assert '@app.route("/verify/<cert>/<token>")' in source


def test_certificate_integrity_and_lifecycle_controls_are_present():
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    app_source = app_path.read_text(encoding="utf-8")
    route_path = Path(__file__).resolve().parents[1] / "routes" / "calibrations.py"
    route_source = route_path.read_text(encoding="utf-8")
    template_path = Path(__file__).resolve().parents[1] / "templates" / "certificate.html"
    template_source = template_path.read_text(encoding="utf-8")

    assert "certificate_fingerprint" in app_source
    assert "certificate_history" in app_source
    assert "certificate_sequences" in app_source
    assert "CERTIFICATE_VERIFIED" in route_source
    assert "CERTIFICATE_INTEGRITY_FAILURE" in route_source
    assert "CERTIFICATE_WITHDRAWN" in route_source
    assert "CERTIFICATE_REISSUED" in route_source
    assert "certificate_pdf" in route_source
    assert "SCAN TO VERIFY" in template_source


def test_certificate_status_page_and_full_report_show_integrity_information():
    status_path = Path(__file__).resolve().parents[1] / "templates" / "certificate_status.html"
    full_path = Path(__file__).resolve().parents[1] / "templates" / "certificate_full.html"
    status_source = status_path.read_text(encoding="utf-8")
    full_source = full_path.read_text(encoding="utf-8")
    assert "Certificate Withdrawn" in status_source
    assert "Certificate Superseded" in status_source
    assert "INTEGRITY_FAILURE" in status_source
    assert "r.certificate_fingerprint" in full_source


def test_certificate_pdf_endpoint_and_dependency_are_present():
    req_path = Path(__file__).resolve().parents[1] / "requirements.txt"
    route_path = Path(__file__).resolve().parents[1] / "routes" / "calibrations.py"
    assert "reportlab" in req_path.read_text(encoding="utf-8")
    assert '@app.route("/certificate/<cert>/pdf")' in route_path.read_text(encoding="utf-8")


def test_certificate_numbering_is_transaction_safe():
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    source = app_path.read_text(encoding="utf-8")
    assert "certificate_sequences" in source
    assert "INSERT OR IGNORE INTO certificate_sequences" in source
    assert "UPDATE certificate_sequences SET next_number=next_number+1" in source



def test_corrected_revision_gets_new_certificate_and_supersedes_previous():
    route_path = Path(__file__).resolve().parents[1] / "routes" / "reviews.py"
    source = route_path.read_text(encoding="utf-8")
    assert "previous_cert" in source
    assert "A corrected revision is a new controlled certificate" in source
    assert "event_type, previous_certificate_no" in source
    assert "'SUPERSEDED'" in source



def test_general_users_can_access_certificate_pdf_endpoint():
    app_path = Path(__file__).resolve().parents[1] / "app.py"
    source = app_path.read_text(encoding="utf-8")
    assert '"certificate_pdf"' in source



def test_last_active_superadmin_cannot_be_deactivated():
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE users (user_id INTEGER, role TEXT, active INTEGER)")
    db.execute("CREATE TABLE user_roles (user_id INTEGER, role TEXT)")
    db.execute("INSERT INTO users VALUES (1, 'superadmin', 1)")
    db.execute("INSERT INTO user_roles VALUES (1, 'superadmin')")
    user = db.execute("SELECT user_id, role, active FROM users WHERE user_id=1").fetchone()

    assert is_last_active_superadmin(db, user) is True


def test_superadmin_can_be_deactivated_when_another_active_superadmin_exists():
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE users (user_id INTEGER, role TEXT, active INTEGER)")
    db.execute("CREATE TABLE user_roles (user_id INTEGER, role TEXT)")
    db.executemany(
        "INSERT INTO users VALUES (?, 'superadmin', 1)",
        [(1,), (2,)],
    )
    db.executemany(
        "INSERT INTO user_roles VALUES (?, 'superadmin')",
        [(1,), (2,)],
    )
    user = db.execute("SELECT user_id, role, active FROM users WHERE user_id=1").fetchone()

    assert is_last_active_superadmin(db, user) is False


def test_inactive_or_non_superadmin_does_not_trigger_last_superadmin_guard():
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE users (user_id INTEGER, role TEXT, active INTEGER)")
    db.execute("CREATE TABLE user_roles (user_id INTEGER, role TEXT)")
    db.execute("INSERT INTO users VALUES (1, 'superadmin', 0)")
    db.execute("INSERT INTO user_roles VALUES (1, 'superadmin')")
    inactive_superadmin = db.execute(
        "SELECT user_id, role, active FROM users WHERE user_id=1"
    ).fetchone()

    assert is_last_active_superadmin(db, inactive_superadmin) is False

    db.execute("INSERT INTO users VALUES (2, 'technician', 1)")
    db.execute("INSERT INTO user_roles VALUES (2, 'technician')")
    technician = db.execute("SELECT user_id, role, active FROM users WHERE user_id=2").fetchone()
    assert is_last_active_superadmin(db, technician) is False


def _workflow_validation_db():
    import sqlite3

    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE calibration_work_orders (
            work_order_id INTEGER PRIMARY KEY,
            request_id INTEGER,
            assigned_technician_id INTEGER,
            procedure_id INTEGER,
            status TEXT
        );
        CREATE TABLE calibrations (
            cal_id INTEGER PRIMARY KEY,
            request_id INTEGER,
            lifecycle_status TEXT,
            revision_no INTEGER,
            procedure_id INTEGER,
            n_points INTEGER,
            result TEXT,
            mean_error REAL,
            max_error REAL,
            standard_id INTEGER,
            cal_date TEXT
        );
        CREATE TABLE calibration_procedures (
            procedure_id INTEGER PRIMARY KEY,
            active INTEGER
        );
        CREATE TABLE calibration_procedure_points (
            procedure_id INTEGER,
            point_no INTEGER,
            reference_value REAL,
            tolerance REAL
        );
        CREATE TABLE calibration_points (
            cal_id INTEGER,
            point_no INTEGER,
            reference_value REAL,
            tolerance REAL,
            as_found_value REAL,
            as_found_error REAL,
            as_found_result TEXT,
            as_left_value REAL,
            as_left_error REAL,
            as_left_result TEXT,
            result TEXT
        );
        CREATE TABLE reference_standards (
            standard_id INTEGER PRIMARY KEY,
            active INTEGER,
            calibrated_on TEXT,
            valid_until TEXT,
            certificate_no TEXT,
            traceability TEXT
        );
        """
    )
    db.execute(
        "INSERT INTO calibration_work_orders VALUES (1,10,7,3,'IN PROGRESS')"
    )
    db.execute(
        "INSERT INTO calibrations VALUES (1,10,'DRAFT',1,3,2,'PASS',0.15,0.2,5,'2026-10-03')"
    )
    db.execute("INSERT INTO calibration_procedures VALUES (3,1)")
    db.executemany(
        "INSERT INTO calibration_procedure_points VALUES (3,?,?,?)",
        [(1,10.0,0.5), (2,20.0,0.5)],
    )
    db.executemany(
        "INSERT INTO calibration_points VALUES (1,?,?,?,?,?,?,?,?,?,?)",
        [
            (1,10.0,0.5,10.1,0.1,"PASS",10.1,0.1,"PASS","PASS"),
            (2,20.0,0.5,20.2,0.2,"PASS",20.2,0.2,"PASS","PASS"),
        ],
    )
    db.execute(
        "INSERT INTO reference_standards VALUES (5,1,'2026-01-01','2027-01-01','STD-CERT-1','NABL traceable')"
    )
    return db


def test_calibration_submission_validator_accepts_current_controlled_record():
    db = _workflow_validation_db()
    assert validate_calibration_record_for_submission(db, 1, 1) is True


@pytest.mark.parametrize(
    "lifecycle,work_order_status",
    [
        ("SUBMITTED", "IN PROGRESS"),
        ("APPROVED", "COMPLETED"),
    ],
)
def test_calibration_submission_validator_rejects_non_editable_lifecycle(lifecycle, work_order_status):
    db = _workflow_validation_db()
    db.execute("UPDATE calibrations SET lifecycle_status=?", (lifecycle,))
    db.execute("UPDATE calibration_work_orders SET status=?", (work_order_status,))
    with pytest.raises(ValueError, match="work order|submitted|draft or returned"):
        validate_calibration_record_for_submission(db, 1, 1)


def test_calibration_submission_validator_rejects_missing_required_point():
    db = _workflow_validation_db()
    db.execute("DELETE FROM calibration_points WHERE point_no=2")
    with pytest.raises(ValueError, match="every required procedure point"):
        validate_calibration_record_for_submission(db, 1, 1)


def test_calibration_submission_validator_rejects_tampered_summary():
    db = _workflow_validation_db()
    db.execute("UPDATE calibrations SET max_error=9.9")
    with pytest.raises(ValueError, match="summary"):
        validate_calibration_record_for_submission(db, 1, 1)


def test_calibration_submission_validator_rejects_invalid_reference_standard():
    db = _workflow_validation_db()
    db.execute("UPDATE reference_standards SET valid_until='2026-01-01'")
    with pytest.raises(ValueError, match="valid"):
        validate_calibration_record_for_submission(db, 1, 1)


def test_work_order_status_transitions_are_one_way_until_review():
    assert WORK_ORDER_STATUS_TRANSITIONS["ASSIGNED"] == {"IN PROGRESS", "CANCELLED"}
    assert WORK_ORDER_STATUS_TRANSITIONS["IN PROGRESS"] == {"AWAITING REVIEW", "CANCELLED"}
    assert WORK_ORDER_STATUS_TRANSITIONS["AWAITING REVIEW"] == set()
    assert WORK_ORDER_STATUS_TRANSITIONS["COMPLETED"] == set()
    assert WORK_ORDER_STATUS_TRANSITIONS["CANCELLED"] == set()


def test_workflow_routes_audit_rejected_status_and_revision_submission():
    route_path = Path(__file__).resolve().parents[1] / "routes" / "work_orders.py"
    source = route_path.read_text(encoding="utf-8")
    calibration_path = Path(__file__).resolve().parents[1] / "routes" / "calibrations.py"
    calibration_source = calibration_path.read_text(encoding="utf-8")
    review_path = Path(__file__).resolve().parents[1] / "routes" / "reviews.py"
    review_source = review_path.read_text(encoding="utf-8")

    assert "WORK_ORDER_STATUS_REJECTED" in source
    assert "CALIBRATION_SUBMISSION_REJECTED" in source
    assert 'record_calibration_revision(' in source
    assert '"SUBMITTED"' in source
    assert "CALIBRATION_REVIEW_REJECTED" in review_source
    assert "submitted_revision" in review_source
    assert "lifecycle_status='RETURNED'" in review_source
    assert "record_calibration_revision(" in review_source
    assert "already has a calibration awaiting administrator review" in calibration_source
    assert "Only a draft or returned calibration can be submitted." in Path(
        __file__
    ).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")


def test_operational_dashboard_exposes_lab_kpis_for_work_orders_reviews_standards_and_stations():
    template_path = Path(__file__).resolve().parents[1] / "templates" / "home.html"
    template = template_path.read_text(encoding="utf-8")

    assert "dashboard.active_work_orders" in template
    assert "dashboard.pending_reviews" in template
    assert "std_issues|length" in template
    assert "stations|length" in template
    assert 'tr("Active work orders")' in template
    assert 'tr("Pending reviews")' in template
    assert 'tr("Standards needing attention")' in template
    assert 'tr("Stations")' in template


def test_multi_role_helpers_support_combined_permissions():
    with flask_app.test_request_context("/"):
        g.user_roles = {"technician", "reviewer"}
        assert user_has_role("technician") is True
        assert user_has_role("reviewer") is True
        assert user_has_role("admin") is False


def test_multi_role_user_model_and_review_permissions_are_defined():
    app_source = Path(__file__).resolve().parents[1].joinpath("app.py").read_text(encoding="utf-8")
    users_source = Path(__file__).resolve().parents[1].joinpath("routes/users.py").read_text(encoding="utf-8")
    review_source = Path(__file__).resolve().parents[1].joinpath("routes/reviews.py").read_text(encoding="utf-8")
    users_template = Path(__file__).resolve().parents[1].joinpath("templates/users.html").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS user_roles" in app_source
    assert "role IN ('superadmin','admin','technician','reviewer','general_user')" in app_source
    assert "def user_has_role(role" in app_source
    assert "def reviewer_required" in app_source
    assert '@reviewer_required' in review_source
    assert '@app.route("/reviews/<int:review_id>/calibration")' in review_source
    assert '@app.route("/reviews/<int:review_id>/decision", methods=["POST"])' in review_source
    assert "the pending station must be created by an administrator" in review_source
    assert 'name="roles"' in users_template
    assert "update_user_roles" in users_source


@pytest.mark.parametrize(
    "roles,capability,expected",
    [
        ({"technician", "reviewer"}, "technician", True),
        ({"technician", "reviewer"}, "reviewer", True),
        ({"technician", "reviewer"}, "admin", False),
        ({"technician", "admin"}, "technician", True),
        ({"technician", "admin"}, "admin", True),
        ({"admin", "reviewer"}, "reviewer", True),
        ({"general_user", "technician"}, "technician", True),
        ({"general_user", "technician"}, "admin", False),
        ({"general_user"}, "general_user", True),
    ],
)
def test_multi_role_capabilities_are_independent(roles, capability, expected):
    with flask_app.test_request_context("/"):
        g.user_roles = roles
        assert user_has_role(capability) is expected


def test_multi_role_authorization_decorators_use_assigned_roles():
    @admin_required
    def admin_action():
        return "admin-ok"

    @reviewer_required
    def review_action():
        return "review-ok"

    @superadmin_required
    def superadmin_action():
        return "superadmin-ok"

    with flask_app.test_request_context("/"):
        g.user_roles = {"technician", "reviewer"}
        assert admin_action.__wrapped__ if False else review_action() == "review-ok"
        with pytest.raises(Exception):
            admin_action()
        with pytest.raises(Exception):
            superadmin_action()

        g.user_roles = {"technician", "admin"}
        assert admin_action() == "admin-ok"
        with pytest.raises(Exception):
            review_action()
        with pytest.raises(Exception):
            superadmin_action()

        g.user_roles = {"admin", "reviewer"}
        assert admin_action() == "admin-ok"
        assert review_action() == "review-ok"
        with pytest.raises(Exception):
            superadmin_action()

        g.user_roles = {"superadmin", "technician"}
        assert admin_action() == "admin-ok"
        assert review_action() == "review-ok"
        assert superadmin_action() == "superadmin-ok"
