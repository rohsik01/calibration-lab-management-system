"""Route module: requests."""
from app import *

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
    technicians = db.execute("SELECT user_id, full_name, username FROM users WHERE role='technician' AND active=1 ORDER BY full_name").fetchall()
    standards = db.execute("SELECT standard_id, code, name, valid_until FROM reference_standards WHERE active=1 ORDER BY code").fetchall()
    procedures = db.execute("SELECT procedure_id, code, title, revision FROM calibration_procedures WHERE active=1 ORDER BY code").fetchall()
    return render_template("requests.html", rows=rows, counts=counts,
                           statuses=REQUEST_STATUSES, q=q, status_filter=status_filter,
                           technicians=technicians, standards=standards, procedures=procedures)


@app.route("/requests/new", methods=["GET", "POST"])
def new_calibration_request():
    if g.user["role"] != "general_user":
        abort(403)
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
            # General users submit only the calibration request. For an unregistered
            # sensor, the technician records the sensor/registration details later.
            pending = {k: None for k in (
                "sensor_type", "manufacturer", "serial_number", "interval_days",
                "tolerance", "unit", "station_name", "station_location"
            )}
            received_iso = received.isoformat()
            request_no = next_request_number(db, received_iso)
            now = datetime.now().isoformat(timespec="seconds")
            cur = db.execute(
                """INSERT INTO calibration_requests
                (request_no, client_name, contact_person, contact_phone, contact_email,
                 sensor_id, pending_sensor_type, pending_manufacturer, pending_serial_number,
                 pending_interval_days, pending_tolerance, pending_unit, pending_station_name, pending_station_location,
                 instrument_description, requested_service, requested_range,
                 received_date, requested_due_date, priority, condition_received, remarks,
                 status, created_by, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_no, client, f.get("contact_person","").strip(), f.get("contact_phone","").strip(),
                 f.get("contact_email","").strip(), sensor_id, pending["sensor_type"], pending["manufacturer"],
                 pending["serial_number"], pending["interval_days"], pending["tolerance"], pending["unit"],
                 pending["station_name"], pending["station_location"], description, service,
                 f.get("requested_range","").strip(), received_iso, due.isoformat() if due else None, priority,
                 f.get("condition_received","").strip(), f.get("remarks","").strip(),
                 "RECEIVED", g.user["full_name"], now, now)
            )
            db.execute("""INSERT INTO calibration_request_status_history
                (request_id, old_status, new_status, changed_by, changed_at, comments)
                VALUES (?,?,?,?,?,?)""",
                (cur.lastrowid, None, "RECEIVED", g.user["user_id"], now, "Request created"))
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
    procedures = db.execute(
        "SELECT procedure_id, code, title, instrument_type, method, revision, effective_date FROM calibration_procedures WHERE active=1 ORDER BY instrument_type, code"
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
    status_history = db.execute(
        """SELECT h.*, u.full_name AS changed_by_name
           FROM calibration_request_status_history h
           LEFT JOIN users u ON u.user_id=h.changed_by
           WHERE h.request_id=?
           ORDER BY h.changed_at DESC, h.history_id DESC""",
        (request_id,)
    ).fetchall()
    return render_template("request_detail.html", r=row, calibrations=calibrations,
                           statuses=REQUEST_STATUSES, technicians=technicians,
                           standards=standards, procedures=procedures, work_order=work_order,
                           status_history=status_history)


@app.route("/requests/<int:request_id>/status", methods=["POST"])
@admin_required
def update_calibration_request_status(request_id):
    new_status = request.form.get("status", "").strip()
    db = get_db()
    row = db.execute("SELECT request_no, status FROM calibration_requests WHERE request_id=?",
                     (request_id,)).fetchone()
    if not row:
        abort(404)
    allowed = {
        "RECEIVED": {"REVIEWED", "CANCELLED"},
        "REVIEWED": {"CANCELLED"},
        "ASSIGNED": {"CANCELLED"},
        "IN CALIBRATION": {"CANCELLED"},
    }
    if new_status not in allowed.get(row["status"], set()):
        flash("Invalid calibration request status transition.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    try:
        with db:
            transition_request_status(
                db, request_id, new_status, g.user["user_id"],
                request.form.get("comments", "").strip() or None
            )
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("calibration_request", request_id=request_id))
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
    audit_event(
        "REQUEST_DELETED", "calibration_request", request_id,
        old_value={"request_no": row["request_no"]},
        details={"reason": "Admin deleted request with no linked calibration records"}
    )
    db.execute("DELETE FROM calibration_requests WHERE request_id=?", (request_id,))
    db.commit()
    flash(f"Request {row['request_no']} was deleted.")
    return redirect(url_for("calibration_requests"))


