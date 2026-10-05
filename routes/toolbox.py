"""Calibration Toolbox and certificate traceability routes."""
from app import *

@app.route("/toolbox")
def toolbox():
    return render_template("toolbox.html")

@app.route("/traceability/certificate/<cert>")
def certificate_traceability(cert):
    db = get_db()
    r = db.execute("""SELECT c.*, s.sensor_type, s.manufacturer, s.serial_number, s.unit,
                  st.name AS station, st.location AS station_location,
                  cp.code AS procedure_code, cp.title AS procedure_title, cp.revision AS procedure_revision,
                  (SELECT u.full_name FROM users u WHERE u.user_id=c.performed_by) AS technician_name,
                  (SELECT u.full_name FROM users u WHERE u.user_id=c.certificate_issued_by) AS issuer_name,
                  (SELECT u.full_name FROM calibration_review_history rh JOIN users u ON u.user_id=rh.reviewed_by
                   WHERE rh.cal_id=c.cal_id AND rh.decision='APPROVED'
                   ORDER BY rh.reviewed_at DESC, rh.review_id DESC LIMIT 1) AS approver_name
           FROM calibrations c
           LEFT JOIN sensors s ON s.sensor_id=c.sensor_id
           LEFT JOIN stations st ON st.station_id=s.station_id
           LEFT JOIN calibration_procedures cp ON cp.procedure_id=c.procedure_id
           WHERE c.certificate_no=?""", (cert,)).fetchone()
    if not r:
        abort(404)
    standards = [dict(x) for x in calibration_reference_standards(db, r["cal_id"])]
    points = [dict(x) for x in db.execute("SELECT * FROM calibration_points WHERE cal_id=? ORDER BY point_no", (r["cal_id"],))]
    reviews = [dict(x) for x in db.execute("""SELECT rh.*, u.full_name AS reviewer_name FROM calibration_review_history rh LEFT JOIN users u ON u.user_id=rh.reviewed_by WHERE rh.cal_id=? ORDER BY rh.review_id""", (r["cal_id"],))]
    revisions = [dict(x) for x in db.execute("""SELECT cr.*, u.full_name AS creator_name FROM calibration_revisions cr LEFT JOIN users u ON u.user_id=cr.created_by WHERE cr.cal_id=? ORDER BY cr.revision_no""", (r["cal_id"],))]
    return render_template("certificate_traceability.html", r=r, standards=standards, points=points, reviews=reviews, revisions=revisions)
