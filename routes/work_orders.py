"""Route module: work_orders."""
from app import *

@app.route("/work-orders")
def work_orders():
    db = get_db()
    q = request.args.get("q", "").strip()
    status_filter = request.args.get("status", "").strip()
    sql = """
        SELECT w.*, r.request_no, r.client_name, r.instrument_description,
               r.requested_service, r.priority, r.sensor_id,
               u.full_name AS technician_name, a.full_name AS assigned_by_name, cp.code AS procedure_code, cp.title AS procedure_title, cp.revision AS procedure_revision
        FROM calibration_work_orders w
        JOIN calibration_requests r ON r.request_id=w.request_id
        JOIN users u ON u.user_id=w.assigned_technician_id
        JOIN users a ON a.user_id=w.assigned_by
        LEFT JOIN calibration_procedures cp ON cp.procedure_id=w.procedure_id
    """
    where, params = [], []
    if g.user["role"] != "admin":
        where.append("w.assigned_technician_id=?")
        params.append(g.user["user_id"])
    if q:
        like = f"%{q}%"
        where.append("(w.work_order_no LIKE ? OR r.request_no LIKE ? OR r.client_name LIKE ? OR "
                     "r.instrument_description LIKE ? OR u.full_name LIKE ?)")
        params.extend([like, like, like, like, like])
    if status_filter in WORK_ORDER_STATUSES:
        where.append("w.status=?")
        params.append(status_filter)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE w.status WHEN 'ASSIGNED' THEN 1 WHEN 'IN PROGRESS' THEN 2 "
    sql += "WHEN 'AWAITING REVIEW' THEN 3 WHEN 'COMPLETED' THEN 4 ELSE 5 END, w.work_order_id DESC"
    rows = db.execute(sql, params).fetchall()
    counts = {}
    for st in WORK_ORDER_STATUSES:
        count_sql = "SELECT COUNT(*) FROM calibration_work_orders WHERE status=?"
        count_params = [st]
        if g.user["role"] != "admin":
            count_sql += " AND assigned_technician_id=?"
            count_params.append(g.user["user_id"])
        counts[st] = db.execute(count_sql, count_params).fetchone()[0]
    return render_template("work_orders.html", rows=rows, statuses=WORK_ORDER_STATUSES,
                           counts=counts, q=q, status_filter=status_filter)


@app.route("/work-orders/<int:work_order_id>")
def work_order_detail(work_order_id):
    db = get_db()
    row = db.execute(
        """SELECT w.*, r.request_no, r.client_name, r.contact_person, r.contact_phone,
                  r.contact_email, r.instrument_description, r.requested_service,
                  r.requested_range, r.received_date, r.requested_due_date, r.priority,
                  r.condition_received, r.remarks, r.sensor_id,
                  s.sensor_type, s.manufacturer, s.serial_number, st.name AS station,
                  u.full_name AS technician_name, u.username AS technician_username,
                  a.full_name AS assigned_by_name, rs.code AS standard_code, rs.name AS standard_name,
                  cp.code AS procedure_code, cp.title AS procedure_title, cp.revision AS procedure_revision
           FROM calibration_work_orders w
           JOIN calibration_requests r ON r.request_id=w.request_id
           JOIN users u ON u.user_id=w.assigned_technician_id
           JOIN users a ON a.user_id=w.assigned_by
           LEFT JOIN sensors s ON s.sensor_id=r.sensor_id
           LEFT JOIN stations st ON st.station_id=s.station_id
           LEFT JOIN reference_standards rs ON rs.standard_id=w.standard_id
           LEFT JOIN calibration_procedures cp ON cp.procedure_id=w.procedure_id
           WHERE w.work_order_id=?""", (work_order_id,)
    ).fetchone()
    if not row:
        abort(404)
    if g.user["role"] != "admin" and row["assigned_technician_id"] != g.user["user_id"]:
        abort(403)
    calibration = db.execute(
        """SELECT * FROM calibrations WHERE request_id=? ORDER BY cal_id DESC LIMIT 1""",
        (row["request_id"],)
    ).fetchone()
    points = db.execute(
        "SELECT * FROM calibration_points WHERE cal_id=? ORDER BY point_no",
        (calibration["cal_id"],)
    ).fetchall() if calibration else []
    reviews = db.execute(
        """SELECT h.*, u.full_name AS submitted_by_name, v.full_name AS reviewer_name
           FROM calibration_review_history h
           JOIN users u ON u.user_id=h.submitted_by
           LEFT JOIN users v ON v.user_id=h.reviewed_by
           WHERE h.work_order_id=? ORDER BY h.review_id DESC""",
        (work_order_id,)
    ).fetchall()
    return render_template("work_order_detail.html", w=row, statuses=WORK_ORDER_STATUSES,
                           calibration=calibration, points=points, reviews=reviews)


@app.route("/requests/bulk-assign", methods=["POST"])
@admin_required
def bulk_assign_calibration_requests():
    db=get_db()
    ids=list(dict.fromkeys(int(x) for x in request.form.getlist("request_ids") if str(x).isdigit()))
    tech_id=request.form.get("technician_id","").strip()
    tech=db.execute("SELECT user_id,full_name FROM users WHERE user_id=? AND role='technician' AND active=1",
                    (tech_id,)).fetchone() if tech_id.isdigit() else None
    if not ids or not tech:
        flash("Select at least one request and an active technician.","error"); return redirect(url_for("calibration_requests"))
    target_text=request.form.get("target_date","").strip()
    try: target=date.fromisoformat(target_text).isoformat() if target_text else None
    except ValueError:
        flash("Enter a valid target date.","error"); return redirect(url_for("calibration_requests"))
    method=request.form.get("calibration_method","").strip()
    procedure_text=request.form.get("procedure_id","").strip(); procedure_id=None
    if procedure_text:
        procedure=db.execute("SELECT procedure_id FROM calibration_procedures WHERE procedure_id=? AND active=1",
                             (int(procedure_text),)).fetchone() if procedure_text.isdigit() else None
        if not procedure:
            flash("Select a valid active calibration procedure.","error"); return redirect(url_for("calibration_requests"))
        procedure_id=procedure["procedure_id"]
    else:
        flash("Select a controlled calibration procedure.","error"); return redirect(url_for("calibration_requests"))
    std_text=request.form.get("standard_id","").strip(); standard_id=None
    if std_text:
        if not std_text.isdigit(): flash("Select a valid reference standard.","error"); return redirect(url_for("calibration_requests"))
        st=db.execute(
            """SELECT standard_id, calibrated_on, valid_until
               FROM reference_standards
               WHERE standard_id=? AND active=1""", (int(std_text),)
        ).fetchone()
        if not st:
            flash("Select an active reference standard.","error"); return redirect(url_for("calibration_requests"))
        if st["calibrated_on"] > date.today().isoformat() or st["valid_until"] < date.today().isoformat():
            flash("The selected reference standard is not currently within its calibration validity period.","error")
            return redirect(url_for("calibration_requests"))
        standard_id=st["standard_id"]
    now=datetime.now().isoformat(timespec="seconds"); assigned=0; skipped=0
    with db:
        for rid in ids:
            req=db.execute("SELECT * FROM calibration_requests WHERE request_id=?",(rid,)).fetchone()
            if not req or req["status"] in ("COMPLETED","CANCELLED","UNDER REVIEW","IN CALIBRATION"):
                skipped+=1; continue
            existing=db.execute("SELECT * FROM calibration_work_orders WHERE request_id=?",(rid,)).fetchone()
            if not existing and req["status"] != "REVIEWED":
                skipped+=1; continue
            if existing and existing["status"] != "ASSIGNED":
                skipped+=1; continue
            target_use=target or req["requested_due_date"]
            if existing:
                db.execute("""UPDATE calibration_work_orders SET assigned_technician_id=?,assigned_by=?,assigned_at=?,
                    target_date=?,calibration_method=?,procedure_id=?,standard_id=?,instructions=?,
                    status=CASE WHEN status IN ('ASSIGNED','CANCELLED') THEN 'ASSIGNED' ELSE status END,updated_at=?
                    WHERE request_id=?""",
                    (tech["user_id"],g.user["user_id"],now,target_use,method,procedure_id,standard_id,
                     request.form.get("instructions","").strip(),now,rid))
            else:
                wo=next_work_order_number(db,now[:10])
                db.execute("""INSERT INTO calibration_work_orders
                    (work_order_no,request_id,assigned_technician_id,assigned_by,assigned_at,target_date,
                     calibration_method,procedure_id,standard_id,instructions,status,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?, 'ASSIGNED',?,?)""",
                    (wo,rid,tech["user_id"],g.user["user_id"],now,target_use,method,procedure_id,standard_id,
                     request.form.get("instructions","").strip(),now,now))
            if req["status"] == "REVIEWED":
                transition_request_status(db, rid, "ASSIGNED", g.user["user_id"], "Technician assigned")
            assigned+=1
    flash(f"{assigned} request(s) assigned to {tech['full_name']}."+(f" Skipped {skipped} ineligible request(s)." if skipped else ""))
    return redirect(url_for("calibration_requests"))

@app.route("/requests/<int:request_id>/assign", methods=["POST"])
@admin_required
def assign_calibration_request(request_id):
    db = get_db()
    req = db.execute("SELECT * FROM calibration_requests WHERE request_id=?", (request_id,)).fetchone()
    if not req:
        abort(404)
    existing = db.execute("SELECT * FROM calibration_work_orders WHERE request_id=?", (request_id,)).fetchone()
    if req["status"] in ("COMPLETED", "CANCELLED", "IN CALIBRATION", "UNDER REVIEW"):
        flash("This request is not available for assignment or reassignment in its current state.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    if existing and existing["status"] != "ASSIGNED":
        flash("A work order can only be reassigned while it is in ASSIGNED status.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    if not existing and req["status"] != "REVIEWED":
        flash("The request must be marked REVIEWED before a technician can be assigned.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    technician_text = request.form.get("technician_id", "").strip()
    technician = db.execute(
        "SELECT user_id, full_name FROM users WHERE user_id=? AND role='technician' AND active=1",
        (technician_text,)
    ).fetchone() if technician_text.isdigit() else None
    if not technician:
        flash("Select an active technician account.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    target_text = request.form.get("target_date", "").strip()
    try:
        target = date.fromisoformat(target_text).isoformat() if target_text else req["requested_due_date"]
    except ValueError:
        flash("Enter a valid target date.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    method = request.form.get("calibration_method", "").strip()
    procedure_text = request.form.get("procedure_id", "").strip()
    procedure_id = None
    if procedure_text:
        procedure = db.execute(
            "SELECT procedure_id FROM calibration_procedures WHERE procedure_id=? AND active=1",
            (int(procedure_text),)
        ).fetchone() if procedure_text.isdigit() else None
        if not procedure:
            flash("Select a valid active calibration procedure.", "error")
            return redirect(url_for("calibration_request", request_id=request_id))
        procedure_id = procedure["procedure_id"]
    elif not existing:
        flash("Select a controlled calibration procedure.", "error")
        return redirect(url_for("calibration_request", request_id=request_id))
    standard_text = request.form.get("standard_id", "").strip()
    standard_id = None
    if standard_text:
        if not standard_text.isdigit():
            flash("Select a valid reference standard.", "error")
            return redirect(url_for("calibration_request", request_id=request_id))
        standard = db.execute(
            """SELECT standard_id, calibrated_on, valid_until
               FROM reference_standards
               WHERE standard_id=? AND active=1""",
            (int(standard_text),)
        ).fetchone()
        if not standard:
            flash("Select an active reference standard.", "error")
            return redirect(url_for("calibration_request", request_id=request_id))
        if standard["calibrated_on"] > date.today().isoformat() or standard["valid_until"] < date.today().isoformat():
            flash("The selected reference standard is not currently within its calibration validity period.", "error")
            return redirect(url_for("calibration_request", request_id=request_id))
        standard_id = standard["standard_id"]
    now = datetime.now().isoformat(timespec="seconds")
    with db:
        if existing:
            db.execute(
                """UPDATE calibration_work_orders
                   SET assigned_technician_id=?, assigned_by=?, assigned_at=?, target_date=?,
                       calibration_method=?, procedure_id=?, standard_id=?, instructions=?,
                       status=CASE WHEN status IN ('ASSIGNED','CANCELLED') THEN 'ASSIGNED' ELSE status END,
                       updated_at=? WHERE request_id=?""",
                (technician["user_id"], g.user["user_id"], now, target, method, procedure_id, standard_id,
                 request.form.get("instructions", "").strip(), now, request_id)
            )
            work_order_no = existing["work_order_no"]
        else:
            work_order_no = next_work_order_number(db, now[:10])
            db.execute(
                """INSERT INTO calibration_work_orders
                   (work_order_no, request_id, assigned_technician_id, assigned_by, assigned_at,
                    target_date, calibration_method, procedure_id, standard_id, instructions, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?, 'ASSIGNED',?,?)""",
                (work_order_no, request_id, technician["user_id"], g.user["user_id"], now,
                 target, method, procedure_id, standard_id, request.form.get("instructions", "").strip(), now, now)
            )
        if req["status"] == "REVIEWED":
            transition_request_status(db, request_id, "ASSIGNED", g.user["user_id"], "Technician assigned")
    flash(f"Work order {work_order_no} assigned to {technician['full_name']}.")
    work_order = db.execute("SELECT work_order_id FROM calibration_work_orders WHERE request_id=?",
                            (request_id,)).fetchone()
    return redirect(url_for("work_order_detail", work_order_id=work_order["work_order_id"]))


@app.route("/work-orders/<int:work_order_id>/status", methods=["POST"])
def update_work_order_status(work_order_id):
    db = get_db()
    row = db.execute("SELECT * FROM calibration_work_orders WHERE work_order_id=?", (work_order_id,)).fetchone()
    if not row:
        abort(404)
    if g.user["role"] != "admin" and row["assigned_technician_id"] != g.user["user_id"]:
        abort(403)

    current = row["status"]
    new_status = request.form.get("status", "").strip()
    allowed = WORK_ORDER_STATUS_TRANSITIONS.get(current, set()).copy()
    if g.user["role"] == "admin":
        allowed.discard("IN PROGRESS")
        allowed.discard("AWAITING REVIEW")
    else:
        allowed.discard("CANCELLED")
    if new_status not in allowed:
        reason = "Invalid work-order status transition."
        audit_work_order_transition_rejection(work_order_id, current, new_status, reason)
        db.commit()
        flash(reason, "error")
        return redirect(url_for("work_order_detail", work_order_id=work_order_id))

    now = datetime.now().isoformat(timespec="seconds")
    if new_status == "AWAITING REVIEW":
        calibration = db.execute(
            "SELECT cal_id, revision_no FROM calibrations WHERE request_id=? ORDER BY cal_id DESC LIMIT 1",
            (row["request_id"],)
        ).fetchone()
        if not calibration:
            flash("Record the calibration measurements before submitting this work order for review.", "error")
            return redirect(url_for("work_order_detail", work_order_id=work_order_id))
        try:
            validate_calibration_record_for_submission(
                db, calibration["cal_id"], work_order_id, g.user["user_id"]
            )
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("work_order_detail", work_order_id=work_order_id))
        pending = db.execute(
            "SELECT review_id FROM calibration_review_history WHERE work_order_id=? AND decision='PENDING'",
            (work_order_id,)
        ).fetchone()
        with db:
            if pending:
                audit_event(
                    "CALIBRATION_SUBMISSION_REJECTED",
                    "calibration",
                    calibration["cal_id"],
                    details={"work_order_id": work_order_id, "reason": "A review is already pending."},
                )
                flash("This calibration is already awaiting administrator review.", "error")
                return redirect(url_for("work_order_detail", work_order_id=work_order_id))
            db.execute(
                """INSERT INTO calibration_review_history
                   (work_order_id, cal_id, submitted_by, submitted_at, submitted_revision, decision)
                   VALUES (?,?,?,?,?, 'PENDING')""",
                (work_order_id, calibration["cal_id"], g.user["user_id"], now, calibration["revision_no"])
            )
            db.execute(
                "UPDATE calibrations SET lifecycle_status='SUBMITTED', updated_at=? WHERE cal_id=?",
                (now, calibration["cal_id"])
            )
            record_calibration_revision(
                db, calibration["cal_id"], "SUBMITTED", g.user["user_id"],
                comments="Calibration submitted for administrator review"
            )
            db.execute(
                "UPDATE calibration_work_orders SET status='AWAITING REVIEW', updated_at=? WHERE work_order_id=?",
                (now, work_order_id)
            )
            transition_request_status(
                db, row["request_id"], "UNDER REVIEW", g.user["user_id"],
                "Calibration submitted for administrator review"
            )
        flash("Calibration submitted for review.")
        return redirect(url_for("work_order_detail", work_order_id=work_order_id))

    with db:
        db.execute("UPDATE calibration_work_orders SET status=?, updated_at=? WHERE work_order_id=?",
                   (new_status, now, work_order_id))
        if new_status == "IN PROGRESS":
            # Calibration data entry may already have moved the linked request
            # from ASSIGNED to IN CALIBRATION. Keep the request/work-order
            # workflow in sync without attempting the same transition twice.
            req_state = db.execute(
                "SELECT status FROM calibration_requests WHERE request_id=?",
                (row["request_id"],)
            ).fetchone()
            if req_state and req_state["status"] == "ASSIGNED":
                transition_request_status(
                    db, row["request_id"], "IN CALIBRATION", g.user["user_id"],
                    "Technician started calibration"
                )
            elif req_state and req_state["status"] != "IN CALIBRATION":
                raise ValueError(
                    "Cannot start this work order because the linked request is already "
                    + req_state["status"] + "."
                )
        elif new_status == "CANCELLED":
            transition_request_status(
                db, row["request_id"], "CANCELLED", g.user["user_id"],
                "Request cancelled"
            )
    flash("Work order status updated.")
    return redirect(url_for("work_order_detail", work_order_id=work_order_id))


