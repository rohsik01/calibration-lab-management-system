import math
from pathlib import Path

import pytest

from app import app as flask_app, calculate_measurement_uncertainty


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
}
