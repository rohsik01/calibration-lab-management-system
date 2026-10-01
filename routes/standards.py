"""Route module: standards."""
from app import *

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


