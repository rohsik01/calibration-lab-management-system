"""Route module: reviews."""
from app import *

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


@app.route("/reviews/<int:review_id>/calibration")
@admin_required
def review_calibration_details(review_id):
    db = get_db()
    row = db.execute(
        """SELECT h.review_id, h.cal_id, h.submitted_at,
                  c.certificate_no, c.cal_date, c.reference_standard,
                  c.reference_value, c.measured_value, c.error, c.result,
                  c.performed_by, c.n_points,
                  rq.request_no, rq.client_name, rq.instrument_description,
                  rq.requested_range, rq.condition_received, rq.remarks,
                  COALESCE(s.sensor_type, rq.pending_sensor_type) AS sensor_type,
                  COALESCE(s.manufacturer, rq.pending_manufacturer) AS manufacturer,
                  COALESCE(s.serial_number, rq.pending_serial_number) AS serial_number,
                  COALESCE(s.unit, rq.pending_unit) AS unit,
                  COALESCE(st.name, rq.pending_station_name) AS station
           FROM calibration_review_history h
           JOIN calibrations c ON c.cal_id=h.cal_id
           LEFT JOIN sensors s ON s.sensor_id=c.sensor_id
           LEFT JOIN calibration_requests rq ON rq.request_id=c.request_id
           LEFT JOIN stations st ON st.station_id=s.station_id
           WHERE h.review_id=? AND h.decision='PENDING'""",
        (review_id,)
    ).fetchone()
    if not row:
        abort(404)
    points = db.execute(
        """SELECT point_no, reference_value, measured_value, error, tolerance, result
           FROM calibration_points WHERE cal_id=? ORDER BY point_no""",
        (row["cal_id"],)
    ).fetchall()
    return {
        "certificate_no": row["certificate_no"],
        "cal_date": row["cal_date"],
        "reference_standard": row["reference_standard"],
        "sensor_type": row["sensor_type"],
        "manufacturer": row["manufacturer"],
        "serial_number": row["serial_number"],
        "station": row["station"],
        "unit": row["unit"],
        "performed_by": row["performed_by"],
        "request_no": row["request_no"],
        "client_name": row["client_name"],
        "instrument_description": row["instrument_description"],
        "requested_range": row["requested_range"],
        "condition_received": row["condition_received"],
        "remarks": row["remarks"],
        "overall_result": row["result"],
        "max_error": row["error"],
        "points": [dict(p) for p in points],
    }


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
        audit_event(
            "CALIBRATION_REVIEW_" + decision,
            "calibration_review", review_id,
            old_value={"decision": "PENDING"},
            new_value={"decision": decision},
            details={"comments": comments, "work_order_id": review["work_order_id"],
                     "cal_id": review["cal_id"]}
        )
        if decision == "APPROVED":
            req = db.execute(
                """SELECT r.* FROM calibration_requests r
                   JOIN calibration_work_orders w ON w.request_id=r.request_id
                   WHERE w.work_order_id=?""", (review["work_order_id"],)
            ).fetchone()
            if req and not req["sensor_id"]:
                if not req["pending_sensor_type"] or not req["pending_serial_number"] or not req["pending_station_name"]:
                    flash("Cannot approve: pending sensor details are incomplete.", "error")
                    return redirect(url_for("work_order_detail", work_order_id=review["work_order_id"]))
                station = db.execute("SELECT station_id FROM stations WHERE name=?", (req["pending_station_name"],)).fetchone()
                if station:
                    station_id = station["station_id"]
                else:
                    station_id = db.execute(
                        "INSERT INTO stations(name,location,type,updated_at) VALUES (?,?,?,?)",
                        (req["pending_station_name"], req["pending_station_location"] or "", "Meteorological", now)
                    ).lastrowid
                if db.execute("SELECT 1 FROM sensors WHERE serial_number=?", (req["pending_serial_number"],)).fetchone():
                    flash("Cannot approve: a sensor with this serial number already exists.", "error")
                    return redirect(url_for("work_order_detail", work_order_id=review["work_order_id"]))
                sensor_id = _station_sensor_id(db, station_id, req["pending_sensor_type"])
                db.execute(
                    "INSERT INTO sensors(sensor_id,station_id,sensor_type,manufacturer,serial_number,interval_days,tolerance,unit) VALUES (?,?,?,?,?,?,?,?)",
                    (sensor_id,station_id,req["pending_sensor_type"],req["pending_manufacturer"] or "",
                     req["pending_serial_number"],req["pending_interval_days"] or 365,
                     req["pending_tolerance"] if req["pending_tolerance"] is not None else 0.5,req["pending_unit"] or "")
                )
                db.execute("UPDATE calibration_requests SET sensor_id=?, updated_at=? WHERE request_id=?",
                           (sensor_id,now,req["request_id"]))
                db.execute("UPDATE calibrations SET sensor_id=? WHERE cal_id=? AND sensor_id IS NULL",
                           (sensor_id,review["cal_id"]))
            db.execute("UPDATE calibration_work_orders SET status='COMPLETED', updated_at=? WHERE work_order_id=?",
                       (now, review["work_order_id"]))
            transition_request_status(db, review["request_id"], "COMPLETED", g.user["user_id"],
                                      comments or "Calibration approved")
        else:
            db.execute("UPDATE calibration_work_orders SET status='IN PROGRESS', updated_at=? WHERE work_order_id=?",
                       (now, review["work_order_id"]))
            transition_request_status(db, review["request_id"], "IN CALIBRATION", g.user["user_id"],
                                      comments or "Calibration returned for correction")
    flash("Calibration approved and request completed." if decision == "APPROVED"
          else "Calibration returned to the technician for correction.")
    return redirect(url_for("work_order_detail", work_order_id=review["work_order_id"]))


# -------------------------- calibration requests ---------------------------

