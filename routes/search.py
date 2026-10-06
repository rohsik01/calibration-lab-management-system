"""System-wide search routes."""
from app import *

@app.route("/search")
def global_search():
    q = request.args.get("q", "").strip()
    results = {"Sensors": [], "Stations": [], "Work orders": [], "Calibrations": [], "Certificates": [], "Reference standards": []}
    if not q:
        return render_template("search.html", q=q, results=results, total=0)
    like = f"%{q}%"
    db = get_db()
    results["Sensors"] = [dict(r) for r in db.execute("""SELECT s.sensor_id AS id, s.sensor_type, s.serial_number, st.name AS station FROM sensors s JOIN stations st ON st.station_id=s.station_id WHERE s.sensor_id LIKE ? OR s.sensor_type LIKE ? OR s.serial_number LIKE ? OR s.manufacturer LIKE ? OR st.name LIKE ? ORDER BY s.sensor_id LIMIT 20""", (like, like, like, like, like))]
    results["Stations"] = [dict(r) for r in db.execute("""SELECT station_id AS id, name, location, type FROM stations WHERE name LIKE ? OR location LIKE ? OR type LIKE ? ORDER BY name LIMIT 20""", (like, like, like))]
    results["Work orders"] = [dict(r) for r in db.execute("""SELECT wo.work_order_id AS id, wo.work_order_no, cr.request_no, cr.client_name, wo.status FROM calibration_work_orders wo JOIN calibration_requests cr ON cr.request_id=wo.request_id LEFT JOIN users u ON u.user_id=wo.assigned_technician_id WHERE wo.work_order_no LIKE ? OR cr.request_no LIKE ? OR cr.client_name LIKE ? OR cr.instrument_description LIKE ? OR u.full_name LIKE ? ORDER BY wo.work_order_id DESC LIMIT 20""", (like, like, like, like, like))]
    results["Calibrations"] = [dict(r) for r in db.execute("""SELECT c.cal_id AS id, c.sensor_id, c.cal_date, c.result, c.certificate_no, c.lifecycle_status FROM calibrations c LEFT JOIN sensors s ON s.sensor_id=c.sensor_id WHERE c.sensor_id LIKE ? OR c.reference_standard LIKE ? OR c.certificate_no LIKE ? OR c.cal_date LIKE ? OR s.serial_number LIKE ? ORDER BY c.cal_id DESC LIMIT 20""", (like, like, like, like, like))]
    results["Certificates"] = [dict(r) for r in db.execute("""SELECT c.cal_id AS id, c.certificate_no, c.cal_date, c.result, c.sensor_id FROM calibrations c WHERE c.certificate_no LIKE ? ORDER BY c.cal_id DESC LIMIT 20""", (like,))]
    results["Reference standards"] = [dict(r) for r in db.execute("""SELECT standard_id AS id, code, name, serial_number, certificate_no, valid_until, active FROM reference_standards WHERE code LIKE ? OR name LIKE ? OR serial_number LIKE ? OR certificate_no LIKE ? OR traceability LIKE ? ORDER BY code LIMIT 20""", (like, like, like, like, like))]
    total = sum(len(v) for v in results.values())
    return render_template("search.html", q=q, results=results, total=total)
