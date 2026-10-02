"""Route module: calibrations."""
from app import *

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
        mean_error = round(sum(p[2] for p in points) / len(points), 6)
        max_error = round(max(abs(p[2]) for p in points), 6)
        adjustment_status = f.get("adjustment_status", "NOT REQUIRED").strip().upper()
        if adjustment_status not in ("NOT REQUIRED", "REQUIRED", "PERFORMED"):
            adjustment_status = "NOT REQUIRED"
        adjustment_notes = f.get("adjustment_notes", "").strip()
        technician_remarks = f.get("technician_remarks", "").strip()
        if adjustment_status == "PERFORMED" and not adjustment_notes:
            flash("Enter adjustment notes when adjustment is marked as performed.", "error")
            return redirect(url_for("calibrate", sensor_id=sensor_id))
        due = (date.fromisoformat(cal_date) + timedelta(days=s["interval_days"])).isoformat()
        cert = next_certificate(db, cal_date)
        cur = db.execute(
            "INSERT INTO calibrations(sensor_id,cal_date,reference_standard,reference_value,"
            "measured_value,error,result,certificate_no,next_due,performed_by,n_points,"
            "standard_id,standard_details,request_id,mean_error,max_error,adjustment_status,"
            "adjustment_notes,technician_remarks) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sensor_id, cal_date, ref_text, worst[0], worst[1], worst[2],
             result, cert, due, g.user["full_name"], len(points), std_id, std_details, request_id,
             mean_error, max_error, adjustment_status, adjustment_notes, technician_remarks))
        db.executemany(
            "INSERT INTO calibration_points(cal_id,point_no,reference_value,measured_value,"
            "error,result,tolerance) VALUES (?,?,?,?,?,?,?)",
            [(cur.lastrowid, i, *p) for i, p in enumerate(points, 1)])
        db.commit()
        if request_id:
            req_state = db.execute("SELECT status FROM calibration_requests WHERE request_id=?",
                                   (request_id,)).fetchone()
            if req_state and req_state["status"] == "ASSIGNED":
                transition_request_status(db, request_id, "IN CALIBRATION",
                                          g.user["user_id"], "Calibration measurements recorded")
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


@app.route("/calibrate-request/<int:request_id>", methods=["GET", "POST"])
def calibrate_pending_request(request_id):
    db=get_db()
    req=db.execute("SELECT * FROM calibration_requests WHERE request_id=?",(request_id,)).fetchone()
    if not req or req["sensor_id"]: abort(404)
    wo=db.execute("SELECT * FROM calibration_work_orders WHERE request_id=?",(request_id,)).fetchone()
    if not wo or wo["assigned_technician_id"]!=g.user["user_id"]: abort(403)
    if wo["status"] in ("AWAITING REVIEW","COMPLETED","CANCELLED"):
        flash("This work order is already submitted or closed.","error")
        return redirect(url_for("work_order_detail",work_order_id=wo["work_order_id"]))
    if request.method=="POST":
        try:
            # The technician must complete the registration details for an
            # unregistered sensor before entering its calibration measurements.
            sensor_type = request.form.get("sensor_type", "").strip()
            manufacturer = request.form.get("manufacturer", "").strip()
            serial_number = request.form.get("serial_number", "").strip()
            station_name = request.form.get("station_name", "").strip()
            station_location = request.form.get("station_location", "").strip()
            unit = request.form.get("unit", "").strip()
            interval_days = int(request.form.get("interval_days", "365").strip() or 365)
            tolerance = float(request.form.get("sensor_tolerance", "0.5").strip() or 0.5)
            if not sensor_type or not serial_number or not station_name:
                raise ValueError("Sensor type, serial number and station name are required.")
            if interval_days <= 0 or tolerance < 0:
                raise ValueError("Calibration interval must be positive and tolerance cannot be negative.")
            if db.execute("SELECT 1 FROM sensors WHERE serial_number=? COLLATE NOCASE", (serial_number,)).fetchone():
                raise ValueError("A sensor with this serial number already exists.")
            cal_date=date.fromisoformat(request.form.get("cal_date","").strip()).isoformat()
            refs=[float(x) for x in request.form.getlist("reference_value")]
            meass=[float(x) for x in request.form.getlist("measured_value")]
            tols=[float(x) for x in request.form.getlist("tolerance")]
            if not refs or len(refs)!=len(meass) or len(refs)!=len(tols): raise ValueError("Enter complete measurement points.")
            pts=[(r,m,round(m-r,6),"PASS" if abs(m-r)<=t else "FAIL",t) for r,m,t in zip(refs,meass,tols)]
            worst=max(pts,key=lambda p:abs(p[2])); result="FAIL" if any(p[3]=="FAIL" for p in pts) else "PASS"
            std_sel=request.form.get("standard_id","").strip(); std_text=request.form.get("reference_standard","").strip(); std_id=None; std_details=None
            if std_sel:
                std=db.execute("SELECT * FROM reference_standards WHERE standard_id=? AND active=1",(int(std_sel),)).fetchone()
                if not std: raise ValueError("Select a valid reference standard.")
                std_id=std["standard_id"]; std_text=f"{std['code']} – {std['name']}"
                std_details=json.dumps({"serial":std["serial_number"],"traceability":std["traceability"],"certificate":std["certificate_no"],"valid_until":std["valid_until"],"uncertainty":std["uncertainty"]},ensure_ascii=False)
            elif not std_text: raise ValueError("Choose a reference standard or type its name.")
            if db.execute("SELECT cal_id FROM calibrations WHERE request_id=?",(request_id,)).fetchone():
                raise ValueError("A calibration record already exists for this request.")
            # Keep the sensor unregistered until administrator approval.
            # These details live on the request while the calibration is under review.
            db.execute("""UPDATE calibration_requests SET
                pending_sensor_type=?, pending_manufacturer=?, pending_serial_number=?,
                pending_interval_days=?, pending_tolerance=?, pending_unit=?,
                pending_station_name=?, pending_station_location=?, updated_at=?
                WHERE request_id=?""",
                       (sensor_type, manufacturer, serial_number, interval_days, tolerance, unit,
                        station_name, station_location, datetime.now().isoformat(timespec="seconds"), request_id))
            cert=next_certificate(db,cal_date)
            due=(date.fromisoformat(cal_date)+timedelta(days=interval_days)).isoformat()
            cur=db.execute("""INSERT INTO calibrations
                (sensor_id,cal_date,reference_standard,reference_value,measured_value,error,result,certificate_no,
                 next_due,performed_by,n_points,standard_id,standard_details,request_id)
                VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cal_date,std_text,worst[0],worst[1],worst[2],result,cert,due,g.user["full_name"],len(pts),std_id,std_details,request_id))
            db.executemany("""INSERT INTO calibration_points
                (cal_id,point_no,reference_value,measured_value,error,result,tolerance)
                VALUES (?,?,?,?,?,?,?)""",[(cur.lastrowid,i,*p) for i,p in enumerate(pts,1)])
            req_state = db.execute("SELECT status FROM calibration_requests WHERE request_id=?",
                                   (request_id,)).fetchone()
            if req_state and req_state["status"] == "ASSIGNED":
                transition_request_status(db, request_id, "IN CALIBRATION",
                                          g.user["user_id"], "Calibration measurements recorded")
            db.commit()
            flash("Calibration measurements saved. Submit the work order for administrator review.")
            return redirect(url_for("work_order_detail",work_order_id=wo["work_order_id"]))
        except (ValueError,sqlite3.IntegrityError) as e:
            flash(str(e),"error")
    standards_=db.execute("SELECT * FROM reference_standards WHERE active=1 ORDER BY code").fetchall()
    return render_template("calibrate_pending.html",req=req,today=date.today().isoformat(),standards=standards_)

@app.route("/certificate/<cert>")
def certificate(cert):
    db = get_db()
    r = db.execute(
        """SELECT c.*, COALESCE(s.sensor_type, rq.pending_sensor_type) AS sensor_type,
                  COALESCE(s.manufacturer, rq.pending_manufacturer) AS manufacturer,
                  COALESCE(s.serial_number, rq.pending_serial_number) AS serial_number,
                  COALESCE(s.tolerance, rq.pending_tolerance) AS tolerance,
                  COALESCE(s.unit, rq.pending_unit) AS unit,
                  COALESCE(st.name, rq.pending_station_name) AS station,
                  (SELECT u.full_name FROM calibration_review_history rh
                   JOIN users u ON u.user_id=rh.reviewed_by
                   WHERE rh.cal_id=c.cal_id AND rh.decision='APPROVED'
                   ORDER BY rh.reviewed_at DESC, rh.review_id DESC LIMIT 1) AS approved_by
           FROM calibrations c
           LEFT JOIN sensors s ON s.sensor_id=c.sensor_id
           LEFT JOIN stations st ON st.station_id=s.station_id
           LEFT JOIN calibration_requests rq ON rq.request_id=c.request_id
           WHERE c.certificate_no=?""", (cert,)).fetchone()
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


