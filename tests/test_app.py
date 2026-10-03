import math
from pathlib import Path

import pytest

from app import app as flask_app, calculate_measurement_uncertainty
from routes.calibrations import calibration_delete_blocked
from routes.sensors import sensor_delete_blocked


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

    assert 'g.user.role == "admin" and not hist' in template
    assert "Sensor protected by calibration history" in template
