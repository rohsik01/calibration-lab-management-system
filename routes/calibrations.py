"""Route module: calibrations."""
from app import *
from app import _qr_data_uri
import os



def build_offline_certificate_payload(r, pts, standards):
    """Build a self-contained, scanner-readable certificate record.

    The QR deliberately contains the calibration record itself rather than a
    web URL. A field technician can therefore scan it with no network access.
    Keys are compact to keep the A5 QR practical while retaining the complete
    technical record needed for field reference.
    """
    def v(value):
        return value if value is not None else None

    payload = {
        "v": 1,
        "type": "DHM-CAL-OFFLINE",
        "certificate": v(r["certificate_no"]),
        "sensor_id": v(r["sensor_id"]),
        "sensor_type": v(r["sensor_type"]),
        "manufacturer": v(r["manufacturer"]),
        "serial": v(r["serial_number"]),
        "station": v(r["station"]),
        "unit": v(r["unit"]),
        "calibration_date": v(r["cal_date"]),
        "next_due": v(r["next_due"]),
        "tolerance": v(r["tolerance"]),
        "result": v(r["result"]),
        "approved_by": v(r["approved_by"]),
        "issued_by": v(r["certificate_issued_by_name"]),
        "issued_at": v(r["certificate_issued_at"]),
        "procedure": {
            "code": v(r["procedure_code"]),
            "title": v(r["procedure_title"]),
            "revision": v(r["procedure_revision"]),
        },
        "adjustment": {
            "status": v(r["adjustment_status"]),
            "notes": v(r["adjustment_notes"]),
        },
        "remarks": v(r["technician_remarks"]),
        "environment": {
            "temperature_c": v(r["environment_temperature"]),
            "humidity_pct": v(r["environment_humidity"]),
        },
        "uncertainty": {
            "method": v(r["uncertainty_method"]),
            "standard": v(r["standard_uncertainty"]),
            "resolution": v(r["resolution"]),
            "repeatability": v(r["repeatability"]),
            "environmental": v(r["environmental_uncertainty"]),
            "other": v(r["other_uncertainty"]),
            "combined": v(r["combined_standard_uncertainty"]),
            "k": v(r["coverage_factor"]),
            "expanded": v(r["expanded_uncertainty"]),
        },
        "summary": {
            "points": len(pts),
            "mean_error": v(r["mean_error"]),
            "max_error": v(r["max_error"]),
        },
        "reference_standards": [
            {
                "code": v(std["code"]),
                "name": v(std["name"]),
                "serial": v(std["serial_number"]),
                "certificate": v(std["certificate_no"]),
                "traceability": v(std["traceability"]),
                "calibrated_on": v(std["calibrated_on"]),
                "valid_until": v(std["valid_until"]),
                "uncertainty": v(std["uncertainty"]),
            }
            for std in standards
        ],
        "measurements": [
            [
                v(p["point_no"]),
                v(p["reference_value"]),
                v(p["tolerance"]),
                v(p["as_found_value"] if p["as_found_value"] is not None else p["measured_value"]),
                v(p["as_found_error"] if p["as_found_error"] is not None else p["error"]),
                v(p["as_left_value"]),
                v(p["as_left_error"]),
                v(p["result"]),
            ]
            for p in pts
        ],
        "fingerprint": v(r["certificate_fingerprint"]),
    }
    return "DHM-CAL-OFFLINE|" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    )




@app.route("/qr-reader")
@app.route("/qr-reader/")
def qr_reader():
    """Public offline-first DHM certificate QR reader."""
    return render_template("qr_reader.html")


@app.route("/qr-reader/manifest.webmanifest")
def qr_reader_manifest():
    """PWA manifest for the offline DHM QR reader."""
    return Response(
        json.dumps({
            "name": "DHM Offline QR Reader",
            "short_name": "DHM QR Reader",
            "start_url": "/qr-reader/",
            "scope": "/qr-reader/",
            "display": "standalone",
            "background_color": "#f4f6f8",
            "theme_color": "#17365d",
            "description": "Offline reader for DHM calibration certificate QR records.",
            "icons": [],
        }),
        mimetype="application/manifest+json",
    )


@app.route("/qr-reader/sw.js")
def qr_reader_service_worker():
    """Serve the reader service worker with permission to control /qr-reader/."""
    worker = """const CACHE = "dhm-qr-reader-v1";
const APP = "/qr-reader/";
self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll([APP, "/qr-reader/manifest.webmanifest"])).then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", event => {
  if (event.request.method !== "GET") return;
  event.respondWith(caches.match(event.request).then(cached => cached || fetch(event.request).then(response => {
    const copy = response.clone();
    caches.open(CACHE).then(cache => cache.put(event.request, copy));
    return response;
  }).catch(() => caches.match(APP))));
});"""
    response = Response(worker, mimetype="application/javascript")
    response.headers["Service-Worker-Allowed"] = "/qr-reader/"
    response.headers["Cache-Control"] = "no-cache"
    return response


def selected_reference_standards(db, form, cal_date, required_standard_ids=None):
    """Validate and snapshot one or more registered reference standards for a calibration."""
    required_standard_ids = [int(x) for x in (required_standard_ids or [])]
    if hasattr(form, "getlist"):
        raw_ids = form.getlist("standard_id")
    else:
        raw_ids = form.get("standard_id", [])
        if not isinstance(raw_ids, (list, tuple)):
            raw_ids = [raw_ids]
    ids = []
    for raw in raw_ids:
        raw = str(raw).strip()
        if not raw:
            continue
        if not raw.isdigit():
            raise ValueError("Select valid registered reference standards.")
        sid = int(raw)
        if sid not in ids:
            ids.append(sid)
    if not ids:
        raise ValueError("At least one registered reference standard must be selected.")
    if required_standard_ids:
        if any(sid not in ids for sid in required_standard_ids):
            raise ValueError("The calibration must include every reference standard designated on the work order.")
        if ids[0] != required_standard_ids[0]:
            raise ValueError("The work order primary reference standard must remain the primary calibration standard.")
    standards = []
    for sid in ids:
        std = db.execute(
            "SELECT * FROM reference_standards WHERE standard_id=? AND active=1", (sid,)
        ).fetchone()
        if not std:
            raise ValueError("One of the selected reference standards is unavailable or inactive.")
        if std["calibrated_on"] > cal_date:
            raise ValueError(f"Cannot use {std['code']}: it was calibrated on {std['calibrated_on']}, after this calibration date.")
        if std["valid_until"] < cal_date:
            raise ValueError(f"Cannot use {std['code']}: its validity ended on {std['valid_until']}.")
        if not std["certificate_no"] or not std["traceability"]:
            raise ValueError(f"Reference standard {std['code']} is missing certificate or traceability information.")
        standards.append(std)
    return standards


def persist_calibration_reference_standards(db, cal_id, standards):
    """Replace the controlled standard links for a calibration."""
    db.execute("DELETE FROM calibration_reference_standards WHERE cal_id=?", (cal_id,))
    db.executemany(
        """INSERT INTO calibration_reference_standards
           (cal_id, standard_id, selection_order, is_primary, usage_role)
           VALUES (?,?,?,?,?)""",
        [(cal_id, std["standard_id"], i, 1 if i == 1 else 0, "REFERENCE")
         for i, std in enumerate(standards, 1)],
    )


def calibration_delete_blocked(row, revision_count, review_count):
    """Return whether a calibration record must be retained for traceability."""
    return bool(
        row["lifecycle_status"] == "APPROVED"
        or row["certificate_no"]
        or revision_count
        or review_count
    )


@app.route("/calibrations/<int:cal_id>/delete", methods=["POST"])
@admin_required
def delete_calibration(cal_id):
    db = get_db()
    row = db.execute(
        """SELECT certificate_no, sensor_id, lifecycle_status, revision_no
           FROM calibrations WHERE cal_id=?""",
        (cal_id,),
    ).fetchone()
    if not row:
        abort(404)

    # Calibration records are part of the laboratory's controlled technical
    # record. Once a revision, review, certificate, or approval exists, the
    # record must remain available so its history cannot be destroyed.
    revision_count = db.execute(
        "SELECT COUNT(*) FROM calibration_revisions WHERE cal_id=?",
        (cal_id,),
    ).fetchone()[0]
    review_count = db.execute(
        "SELECT COUNT(*) FROM calibration_review_history WHERE cal_id=?",
        (cal_id,),
    ).fetchone()[0]

    if calibration_delete_blocked(row, revision_count, review_count):
        audit_event(
            "CALIBRATION_DELETE_BLOCKED",
            "calibration",
            cal_id,
            details={
                "lifecycle_status": row["lifecycle_status"],
                "certificate_no": row["certificate_no"],
                "revision_count": revision_count,
                "review_count": review_count,
            },
        )
        db.commit()
        flash(
            "Calibration records with approval, certificates, reviews, or revision history "
            "are protected and cannot be deleted. Use the controlled correction/recalibration workflow.",
            "error",
        )
        return redirect(url_for("sensor", sensor_id=row["sensor_id"]))

    try:
        with db:
            db.execute("DELETE FROM calibration_points WHERE cal_id=?", (cal_id,))
            db.execute("DELETE FROM calibrations WHERE cal_id=?", (cal_id,))
        flash(f"Calibration record '{row['certificate_no']}' was deleted.")
    except sqlite3.Error:
        flash("Could not delete the calibration record.", "error")
    return redirect(url_for("sensor", sensor_id=row["sensor_id"]))

@app.route("/calibrations/<int:cal_id>/revisions/<int:revision_no>")
def calibration_revision_detail(cal_id, revision_no):
    """Display an immutable calibration revision snapshot for traceability."""
    db = get_db()
    cal = db.execute(
        """SELECT c.cal_id, c.certificate_no, c.request_id, c.sensor_id,
                  c.revision_no AS current_revision, r.request_no,
                  w.work_order_id, w.assigned_technician_id
           FROM calibrations c
           LEFT JOIN calibration_requests r ON r.request_id=c.request_id
           LEFT JOIN calibration_work_orders w ON w.request_id=c.request_id
           WHERE c.cal_id=?""",
        (cal_id,)
    ).fetchone()
    if not cal:
        abort(404)
    if not (user_has_role("admin") or user_has_role("superadmin") or user_has_role("reviewer")):
        if not cal["work_order_id"] or cal["assigned_technician_id"] != g.user["user_id"]:
            abort(403)

    revisions = db.execute(
        """SELECT cr.*, u.full_name AS created_by_name
           FROM calibration_revisions cr
           LEFT JOIN users u ON u.user_id=cr.created_by
           WHERE cr.cal_id=?
           ORDER BY cr.revision_no DESC""",
        (cal_id,)
    ).fetchall()
    revision = db.execute(
        """SELECT cr.*, u.full_name AS created_by_name
           FROM calibration_revisions cr
           LEFT JOIN users u ON u.user_id=cr.created_by
           WHERE cr.cal_id=? AND cr.revision_no=?""",
        (cal_id, revision_no)
    ).fetchone()
    if not revision:
        abort(404)
    try:
        snapshot = json.loads(revision["snapshot_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        abort(500)
    return render_template(
        "calibration_revision.html",
        cal=cal,
        revisions=revisions,
        revision=revision,
        snapshot=snapshot,
    )


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
    if not user_has_role("technician"):
        flash("Calibration measurements can only be entered by a user with the Technician role.", "error")
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
            "SELECT work_order_id, assigned_technician_id, status, procedure_id FROM calibration_work_orders WHERE request_id=?",
            (int(linked_request_id),)
        ).fetchone()
        linked_order_standard_rows = db.execute(
            "SELECT standard_id FROM work_order_reference_standards WHERE work_order_id=? ORDER BY selection_order",
            (linked_order["work_order_id"],)
        ).fetchall() if linked_order else []
        linked_order_standard_ids = [r["standard_id"] for r in linked_order_standard_rows]
        if not linked_order or linked_order["assigned_technician_id"] != g.user["user_id"]:
            abort(403)
        procedure_id = linked_order["procedure_id"]
        procedure = db.execute("SELECT * FROM calibration_procedures WHERE procedure_id=?", (procedure_id,)).fetchone() if procedure_id else None
        procedure_points = db.execute(
            "SELECT * FROM calibration_procedure_points WHERE procedure_id=? ORDER BY point_no",
            (procedure_id,)
        ).fetchall() if procedure_id else []
        if procedure_id and (not procedure or not procedure["active"]):
            flash("The assigned calibration procedure is no longer active.", "error")
            return redirect(url_for("work_order_detail", work_order_id=linked_order["work_order_id"]))
        if procedure_id and not procedure_points:
            flash("The assigned calibration procedure has no required measurement points.", "error")
            return redirect(url_for("work_order_detail", work_order_id=linked_order["work_order_id"]))
        if linked_order["status"] in ("AWAITING REVIEW", "COMPLETED", "CANCELLED"):
            flash("This work order is awaiting review or already closed; calibration data cannot be changed.", "error")
            return redirect(url_for("work_order_detail", work_order_id=linked_order["work_order_id"]))
    if request.method == "POST":
        f = request.form
        try:
            cal_date = datetime.strptime(f["cal_date"], "%Y-%m-%d").date().isoformat()
            refs = [float(x) for x in f.getlist("reference_value")]
            found_values = [float(x) for x in f.getlist("as_found_value")]
            left_raw = f.getlist("as_left_value")
            as_left = [float(x) if x.strip() else None for x in left_raw] if left_raw else [None] * len(refs)
            raw = f.getlist("tolerance")
            tols = ([float(t) if t.strip() else s["tolerance"] for t in raw]
                    if raw else [s["tolerance"]] * len(refs))
            if (not refs or len(refs) != len(found_values) or len(refs) != len(tols) or len(as_left) != len(refs) or len(refs) > 30
                    or not all(math.isfinite(x) for x in refs + found_values + tols + [x for x in as_left if x is not None])
                    or any(t < 0 for t in tols)):
                raise ValueError("Check the date and the numeric values for every measurement point.")
            if procedure_id:
                proc = db.execute("SELECT procedure_id, active FROM calibration_procedures WHERE procedure_id=?",
                                  (procedure_id,)).fetchone()
                if not proc or not proc["active"]:
                    raise ValueError("The assigned calibration procedure is no longer active.")
                proc_points = db.execute(
                    "SELECT reference_value, tolerance FROM calibration_procedure_points WHERE procedure_id=? ORDER BY point_no",
                    (procedure_id,)
                ).fetchall()
                if (not proc_points or len(proc_points) != len(refs)
                        or any(abs(refs[i] - proc_points[i]["reference_value"]) > 1e-9
                               or abs(tols[i] - proc_points[i]["tolerance"]) > 1e-9
                               for i in range(len(refs)))):
                    raise ValueError("Measurement points must match the assigned controlled calibration procedure.")
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("calibrate", sensor_id=sensor_id))
        try:
            standards_selected = selected_reference_standards(db, f, cal_date, linked_order_standard_ids)
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("calibrate", sensor_id=sensor_id))
        std = standards_selected[0]
        std_id = std["standard_id"]
        ref_text = "; ".join(f"{x['code']} – {x['name']}" for x in standards_selected)
        std_details = json.dumps([
            {"standard_id": x["standard_id"], "code": x["code"], "name": x["name"],
             "standard_type": x["standard_type"], "manufacturer": x["manufacturer"],
             "serial": x["serial_number"], "traceability": x["traceability"],
             "certificate": x["certificate_no"], "calibrated_on": x["calibrated_on"],
             "valid_until": x["valid_until"], "uncertainty": x["uncertainty"]}
            for x in standards_selected
        ], ensure_ascii=False)
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
        for ref, found, left, tol in zip(refs, found_values, as_left, tols):
            found_err = round(found - ref, 6)
            left_err = round(left - ref, 6) if left is not None else found_err
            found_result = "PASS" if abs(found_err) <= tol else "FAIL"
            left_result = "PASS" if abs(left_err) <= tol else "FAIL"
            points.append((ref, found, found_err, found_result, tol, left, left_err, left_result))
        if f.get("adjustment_status", "NOT REQUIRED").strip().upper() == "PERFORMED" and any(p[5] is None for p in points):
            flash("Enter an As-Left reading for every point when adjustment is marked as performed.", "error")
            return redirect(url_for("calibrate", sensor_id=sensor_id))
        if procedure_id and not std_id:
            raise ValueError("A registered reference standard is required for a controlled calibration procedure.")
        adjustment_status = f.get("adjustment_status", "NOT REQUIRED").strip().upper()
        if adjustment_status not in ("NOT REQUIRED", "REQUIRED", "PERFORMED"):
            flash("Invalid adjustment status.", "error")
            return redirect(url_for("calibrate", sensor_id=sensor_id))
        final_errors = [p[6] if p[5] is not None else p[2] for p in points]
        worst_index = max(range(len(points)), key=lambda i: abs(final_errors[i]))
        worst = points[worst_index]
        result = "FAIL" if any(abs(final_errors[i]) > points[i][4] for i in range(len(points))) else "PASS"
        mean_error = round(sum(final_errors) / len(final_errors), 6)
        max_error = round(max(abs(x) for x in final_errors), 6)
        try:
            uncertainty = validate_calibration_controls(f, procedure, std, s["unit"])
        except (ValueError, TypeError) as e:
            flash(str(e), "error")
            return redirect(url_for("calibrate", sensor_id=sensor_id))
        adjustment_status = f.get("adjustment_status", "NOT REQUIRED").strip().upper()
        if adjustment_status not in ("NOT REQUIRED", "REQUIRED", "PERFORMED"):
            adjustment_status = "NOT REQUIRED"
        adjustment_notes = f.get("adjustment_notes", "").strip()
        technician_remarks = f.get("technician_remarks", "").strip()
        if adjustment_status == "PERFORMED" and not adjustment_notes:
            flash("Enter adjustment notes when adjustment is marked as performed.", "error")
            return redirect(url_for("calibrate", sensor_id=sensor_id))
        due = (date.fromisoformat(cal_date) + timedelta(days=s["interval_days"])).isoformat()
        cert = None
        cur = db.execute(
            "INSERT INTO calibrations(sensor_id,cal_date,reference_standard,reference_value,"
            "error,result,certificate_no,next_due,performed_by,n_points,"
            "standard_id,standard_details,request_id,mean_error,max_error,adjustment_status,"
            "adjustment_notes,technician_remarks,standard_uncertainty,resolution,repeatability,"
            "environmental_uncertainty,other_uncertainty,combined_standard_uncertainty,coverage_factor,"
            "expanded_uncertainty,uncertainty_method,uncertainty_calculation_json,environment_temperature,environment_humidity,procedure_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sensor_id, cal_date, ref_text, worst[0], final_errors[worst_index],
             result, cert, due, g.user["full_name"], len(points), std_id, std_details, request_id,
             mean_error, max_error, adjustment_status, adjustment_notes, technician_remarks,
             uncertainty["standard_uncertainty"], uncertainty["resolution"], uncertainty["repeatability"],
             uncertainty["environmental_uncertainty"], uncertainty["other_uncertainty"],
             uncertainty["combined_standard_uncertainty"], uncertainty["coverage_factor"],
             uncertainty["expanded_uncertainty"], uncertainty["uncertainty_method"], json.dumps(uncertainty["calculation"], ensure_ascii=False),
             uncertainty["environment_temperature"], uncertainty["environment_humidity"], procedure_id))
        persist_calibration_reference_standards(db, cur.lastrowid, standards_selected)
        db.executemany(
            "INSERT INTO calibration_points(cal_id,point_no,reference_value,error,result,tolerance,"
            "as_found_value,as_found_error,as_found_result,as_left_value,as_left_error,as_left_result)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(cur.lastrowid, i, p[0], final_errors[i-1],
              "PASS" if abs(final_errors[i-1]) <= p[4] else "FAIL", p[4],
              p[1], p[2], p[3],
              (p[5] if p[5] is not None else p[1]),
              (p[6] if p[5] is not None else p[2]),
              (p[7] if p[5] is not None else p[3])) for i, p in enumerate(points, 1)])
        record_calibration_revision(db, cur.lastrowid, "CREATED", g.user["user_id"])
        if request_id:
            # Submitting calibration data is the technician's review submission.
            # No second manual work-order status action is required.
            validate_calibration_record_for_submission(db, cur.lastrowid, linked_order["work_order_id"], g.user["user_id"])
            now = datetime.now().isoformat(timespec="seconds")
            pending = db.execute(
                "SELECT review_id FROM calibration_review_history WHERE work_order_id=? AND decision='PENDING'",
                (linked_order["work_order_id"],)
            ).fetchone()
            if pending:
                audit_event(
                    "CALIBRATION_SUBMISSION_REJECTED",
                    "calibration",
                    cur.lastrowid,
                    details={"work_order_id": linked_order["work_order_id"], "reason": "A review is already pending."},
                )
                db.rollback()
                flash("This work order already has a calibration awaiting administrator review.", "error")
                return redirect(url_for("work_order_detail", work_order_id=linked_order["work_order_id"]))
            db.execute(
                """INSERT INTO calibration_review_history
                   (work_order_id, cal_id, submitted_by, submitted_at, submitted_revision, decision)
                   VALUES (?,?,?,?,?, 'PENDING')""",
                (linked_order["work_order_id"], cur.lastrowid, g.user["user_id"], now, 1)
            )
            db.execute(
                "UPDATE calibrations SET lifecycle_status='SUBMITTED', updated_at=? WHERE cal_id=?",
                (now, cur.lastrowid)
            )
            record_calibration_revision(
                db, cur.lastrowid, "SUBMITTED", g.user["user_id"],
                comments="Calibration submitted for administrator review"
            )
            db.execute(
                "UPDATE calibration_work_orders SET status='AWAITING REVIEW', updated_at=? WHERE work_order_id=?",
                (now, linked_order["work_order_id"])
            )
            req_state = db.execute("SELECT status FROM calibration_requests WHERE request_id=?",
                                   (request_id,)).fetchone()
            if req_state and req_state["status"] == "ASSIGNED":
                transition_request_status(db, request_id, "IN CALIBRATION",
                                          g.user["user_id"], "Calibration measurements recorded")
            req_state = db.execute("SELECT status FROM calibration_requests WHERE request_id=?",
                                   (request_id,)).fetchone()
            if req_state and req_state["status"] == "IN CALIBRATION":
                transition_request_status(db, request_id, "UNDER REVIEW",
                                          g.user["user_id"], "Calibration submitted for administrator review")
        db.commit()
        work_order = db.execute(
            "SELECT work_order_id FROM calibration_work_orders WHERE request_id=?",
            (request_id,)
        ).fetchone() if request_id else None
        if work_order:
            flash("Calibration submitted to the administrator for review.")
            return redirect(url_for("work_order_detail", work_order_id=work_order["work_order_id"]))
        msg = tr("{n} point, max error {err} {unit} → {result}. Certificate {cert} issued."
                 if len(points) == 1 else
                 "{n} points, max error {err} {unit} → {result}. Certificate {cert} issued.")
        flash(msg.format(n=len(points), err=f"{final_errors[worst_index]:+}", unit=s["unit"], result=tr(result),
                         cert=cert), "ok")
        return redirect(url_for("certificate", cert=cert))
    standards_ = db.execute("SELECT * FROM reference_standards WHERE active=1 ORDER BY code").fetchall()
    procedure = db.execute("SELECT * FROM calibration_procedures WHERE procedure_id=?", (linked_order["procedure_id"],)).fetchone() if linked_order and linked_order["procedure_id"] else None
    procedure_points = db.execute("SELECT * FROM calibration_procedure_points WHERE procedure_id=? ORDER BY point_no", (procedure["procedure_id"],)).fetchall() if procedure else []
    requests_ = db.execute(
        "SELECT request_id, request_no, client_name, instrument_description FROM calibration_requests "
        "WHERE status NOT IN ('COMPLETED','CANCELLED') ORDER BY request_id DESC"
    ).fetchall()
    return render_template("calibrate.html", s=s, today=date.today().isoformat(),
                           standards=standards_, selected_standard_ids=linked_order_standard_ids,
                           requests=requests_, procedure=procedure, procedure_points=procedure_points)


@app.route("/calibrate-request/<int:request_id>", methods=["GET", "POST"])
def calibrate_pending_request(request_id):
    db=get_db()
    req=db.execute("SELECT * FROM calibration_requests WHERE request_id=?",(request_id,)).fetchone()
    if not req or req["sensor_id"]: abort(404)
    wo=db.execute("SELECT * FROM calibration_work_orders WHERE request_id=?",(request_id,)).fetchone()
    if not wo or wo["assigned_technician_id"]!=g.user["user_id"]: abort(403)
    work_order_standard_rows = db.execute(
        "SELECT standard_id FROM work_order_reference_standards WHERE work_order_id=? ORDER BY selection_order",
        (wo["work_order_id"],)
    ).fetchall()
    work_order_standard_ids = [r["standard_id"] for r in work_order_standard_rows]
    procedure_id = wo["procedure_id"]
    procedure = db.execute("SELECT * FROM calibration_procedures WHERE procedure_id=?", (procedure_id,)).fetchone() if procedure_id else None
    procedure_points = db.execute(
        "SELECT * FROM calibration_procedure_points WHERE procedure_id=? ORDER BY point_no",
        (procedure_id,)
    ).fetchall() if procedure_id else []
    if procedure_id and (not procedure or not procedure["active"]):
        flash("The assigned calibration procedure is no longer active.", "error")
        return redirect(url_for("work_order_detail", work_order_id=wo["work_order_id"]))
    if procedure_id and not procedure_points:
        flash("The assigned calibration procedure has no required measurement points.", "error")
        return redirect(url_for("work_order_detail", work_order_id=wo["work_order_id"]))
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
            station_id_raw = request.form.get("station_id", "").strip()
            if not station_id_raw.isdigit() or int(station_id_raw) <= 0:
                raise ValueError("Select an existing station from the administrator-maintained station list.")
            station = db.execute(
                "SELECT station_id, name, location, type FROM stations WHERE station_id=?",
                (int(station_id_raw),)
            ).fetchone()
            if not station:
                raise ValueError("The selected station no longer exists. Refresh the station list.")
            station_id = station["station_id"]
            station_name = station["name"]
            station_location = station["location"] or ""
            station_type = station["type"] or "Meteorological"
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
            found_values=[float(x) for x in request.form.getlist("as_found_value")]
            left_raw=request.form.getlist("as_left_value")
            as_left=[float(x) if x.strip() else None for x in left_raw] if left_raw else [None]*len(refs)
            tols=[float(x) for x in request.form.getlist("tolerance")]
            if (not refs or len(refs)>30 or len(refs)!=len(found_values) or len(refs)!=len(tols) or len(as_left)!=len(refs)
                    or not all(math.isfinite(x) for x in refs + found_values + tols + [x for x in as_left if x is not None])
                    or any(t < 0 for t in tols)):
                raise ValueError("Enter complete, valid measurement points.")
            if procedure_id:
                if len(procedure_points)!=len(refs) or any(
                    abs(refs[i]-procedure_points[i]["reference_value"])>1e-9
                    or abs(tols[i]-procedure_points[i]["tolerance"])>1e-9
                    for i in range(len(refs))
                ):
                    raise ValueError("Measurement points must match the assigned controlled calibration procedure.")
            pts=[]
            for r,found,left,t in zip(refs,found_values,as_left,tols):
                found_err=round(found-r,6); left_err=round(left-r,6) if left is not None else found_err
                found_result="PASS" if abs(found_err)<=t else "FAIL"
                left_result="PASS" if abs(left_err)<=t else "FAIL"
                pts.append((r,found,found_err,found_result,t,left,left_err,left_result))
            adjustment_status=request.form.get("adjustment_status","NOT REQUIRED").strip().upper()
            if adjustment_status=="PERFORMED" and any(p[5] is None for p in pts):
                raise ValueError("Enter an As-Left reading for every point when adjustment is marked as performed.")
            adjustment_status=request.form.get("adjustment_status","NOT REQUIRED").strip().upper()
            if adjustment_status not in ("NOT REQUIRED","REQUIRED","PERFORMED"):
                raise ValueError("Invalid adjustment status.")
            final_errors=[p[6] if p[5] is not None else p[2] for p in pts]
            worst_index=max(range(len(pts)),key=lambda i:abs(final_errors[i]))
            worst=pts[worst_index]
            result="FAIL" if any(abs(final_errors[i])>pts[i][4] for i in range(len(pts))) else "PASS"
            mean_error=round(sum(final_errors)/len(final_errors),6)
            max_error=round(max(abs(x) for x in final_errors),6)
            adjustment_notes=request.form.get("adjustment_notes","").strip()
            technician_remarks=request.form.get("technician_remarks","").strip()
            if adjustment_status=="PERFORMED" and not adjustment_notes:
                raise ValueError("Enter adjustment notes when adjustment is marked as performed.")
            standards_selected = selected_reference_standards(db, request.form, cal_date, work_order_standard_ids)
            std = standards_selected[0]
            std_id = std["standard_id"]
            std_text = "; ".join(f"{x['code']} – {x['name']}" for x in standards_selected)
            std_details = json.dumps([
                {"standard_id": x["standard_id"], "code": x["code"], "name": x["name"],
                 "standard_type": x["standard_type"], "manufacturer": x["manufacturer"],
                 "serial": x["serial_number"], "traceability": x["traceability"],
                 "certificate": x["certificate_no"], "calibrated_on": x["calibrated_on"],
                 "valid_until": x["valid_until"], "uncertainty": x["uncertainty"]}
                for x in standards_selected
            ], ensure_ascii=False)
            if db.execute("SELECT cal_id FROM calibrations WHERE request_id=?",(request_id,)).fetchone():
                raise ValueError("A calibration record already exists for this request.")
            # Keep the sensor unregistered until administrator approval.
            # These details live on the request while the calibration is under review.
            db.execute("""UPDATE calibration_requests SET
                pending_sensor_type=?, pending_manufacturer=?, pending_serial_number=?,
                pending_interval_days=?, pending_tolerance=?, pending_unit=?,
                pending_station_id=?, pending_station_name=?, pending_station_location=?, pending_station_type=?, updated_at=?
                WHERE request_id=?""",
                       (sensor_type, manufacturer, serial_number, interval_days, tolerance, unit,
                        station_id, station_name, station_location, station_type, datetime.now().isoformat(timespec="seconds"), request_id))
            cert=None
            due=(date.fromisoformat(cal_date)+timedelta(days=interval_days)).isoformat()
            cur=db.execute("""INSERT INTO calibrations
                (sensor_id,cal_date,reference_standard,reference_value,error,result,certificate_no,
                 next_due,performed_by,n_points,standard_id,standard_details,request_id,mean_error,max_error,
                 adjustment_status,adjustment_notes,technician_remarks,standard_uncertainty,resolution,repeatability,
                 environmental_uncertainty,other_uncertainty,combined_standard_uncertainty,coverage_factor,
                 expanded_uncertainty,uncertainty_method,uncertainty_calculation_json,environment_temperature,environment_humidity,procedure_id)
                VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (cal_date,std_text,worst[0],final_errors[worst_index],result,cert,due,g.user["full_name"],len(pts),std_id,std_details,
                 request_id,mean_error,max_error,adjustment_status,adjustment_notes,technician_remarks,
                 uncertainty["standard_uncertainty"],uncertainty["resolution"],uncertainty["repeatability"],
                 uncertainty["environmental_uncertainty"],uncertainty["other_uncertainty"],
                 uncertainty["combined_standard_uncertainty"],uncertainty["coverage_factor"],
                 uncertainty["expanded_uncertainty"],uncertainty["uncertainty_method"],
                 json.dumps(uncertainty["calculation"], ensure_ascii=False), uncertainty["environment_temperature"],
                 uncertainty["environment_humidity"],procedure_id))
            persist_calibration_reference_standards(db, cur.lastrowid, standards_selected)
            db.executemany("""INSERT INTO calibration_points
                (cal_id,point_no,reference_value,error,result,tolerance,
                 as_found_value,as_found_error,as_found_result,as_left_value,as_left_error,as_left_result)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(cur.lastrowid,i,p[0],final_errors[i-1],
                   "PASS" if abs(final_errors[i-1])<=p[4] else "FAIL",p[4],
                   p[1],p[2],p[3],
                   (p[5] if adjustment_status=="PERFORMED" else p[1]),
                   (p[6] if adjustment_status=="PERFORMED" else p[2]),
                   (p[7] if adjustment_status=="PERFORMED" else p[3])) for i,p in enumerate(pts,1)])
            record_calibration_revision(db, cur.lastrowid, "CREATED", g.user["user_id"])
            # Saving the calibration is also the technician's submission to the administrator.
            validate_calibration_record_for_submission(db, cur.lastrowid, wo["work_order_id"], g.user["user_id"])
            now = datetime.now().isoformat(timespec="seconds")
            pending = db.execute(
                "SELECT review_id FROM calibration_review_history WHERE work_order_id=? AND decision='PENDING'",
                (wo["work_order_id"],)
            ).fetchone()
            if pending:
                audit_event(
                    "CALIBRATION_SUBMISSION_REJECTED",
                    "calibration",
                    cur.lastrowid,
                    details={"work_order_id": wo["work_order_id"], "reason": "A review is already pending."},
                )
                db.rollback()
                flash("This work order already has a calibration awaiting administrator review.", "error")
                return redirect(url_for("work_order_detail", work_order_id=wo["work_order_id"]))
            db.execute(
                """INSERT INTO calibration_review_history
                   (work_order_id, cal_id, submitted_by, submitted_at, submitted_revision, decision)
                   VALUES (?,?,?,?,?, 'PENDING')""",
                (wo["work_order_id"], cur.lastrowid, g.user["user_id"], now, 1)
            )
            db.execute(
                "UPDATE calibrations SET lifecycle_status='SUBMITTED', updated_at=? WHERE cal_id=?",
                (now, cur.lastrowid)
            )
            record_calibration_revision(
                db, cur.lastrowid, "SUBMITTED", g.user["user_id"],
                comments="Calibration submitted for administrator review"
            )
            db.execute(
                "UPDATE calibration_work_orders SET status='AWAITING REVIEW', updated_at=? WHERE work_order_id=?",
                (now, wo["work_order_id"])
            )
            req_state = db.execute("SELECT status FROM calibration_requests WHERE request_id=?",
                                   (request_id,)).fetchone()
            if req_state and req_state["status"] == "ASSIGNED":
                transition_request_status(db, request_id, "IN CALIBRATION",
                                          g.user["user_id"], "Calibration measurements recorded")
            req_state = db.execute("SELECT status FROM calibration_requests WHERE request_id=?",
                                   (request_id,)).fetchone()
            if req_state and req_state["status"] == "IN CALIBRATION":
                transition_request_status(db, request_id, "UNDER REVIEW",
                                          g.user["user_id"], "Calibration submitted for administrator review")
            db.commit()
            flash("Calibration submitted to the administrator for review.")
            return redirect(url_for("work_order_detail",work_order_id=wo["work_order_id"]))
        except (ValueError,sqlite3.IntegrityError) as e:
            flash(str(e),"error")
    standards_=db.execute("SELECT * FROM reference_standards WHERE active=1 ORDER BY code").fetchall()
    stations_=db.execute("SELECT station_id, name, location, type FROM stations ORDER BY name COLLATE NOCASE").fetchall()
    return render_template("calibrate_pending.html",req=req,today=date.today().isoformat(),standards=standards_,stations=stations_,selected_standard_ids=work_order_standard_ids,procedure=procedure,procedure_points=procedure_points)


@app.route("/calibrations/<int:cal_id>/edit", methods=["GET", "POST"])
def edit_calibration(cal_id):
    """Allow the assigned technician to correct a returned calibration and resubmit it."""
    db = get_db()
    cal = db.execute("SELECT * FROM calibrations WHERE cal_id=?", (cal_id,)).fetchone()
    if not cal or not cal["request_id"]:
        abort(404)
    wo = db.execute("SELECT * FROM calibration_work_orders WHERE request_id=?", (cal["request_id"],)).fetchone()
    work_order_standard_rows = db.execute(
        "SELECT standard_id FROM work_order_reference_standards WHERE work_order_id=? ORDER BY selection_order",
        (wo["work_order_id"],)
    ).fetchall() if wo else []
    work_order_standard_ids = [r["standard_id"] for r in work_order_standard_rows]
    if not wo or wo["assigned_technician_id"] != g.user["user_id"]:
        abort(403)
    if cal["lifecycle_status"] == "APPROVED":
        flash("Approved calibration records are immutable. Start a controlled correction/recalibration workflow instead of editing the approved record.", "error")
        return redirect(url_for("work_order_detail", work_order_id=wo["work_order_id"]))
    if wo["status"] != "IN PROGRESS" or cal["lifecycle_status"] != "RETURNED":
        flash("Only a calibration returned by the administrator can be edited.", "error")
        return redirect(url_for("work_order_detail", work_order_id=wo["work_order_id"]))
    req = db.execute("SELECT * FROM calibration_requests WHERE request_id=?", (cal["request_id"],)).fetchone()
    if not req:
        abort(404)
    sensor = db.execute("SELECT * FROM sensors WHERE sensor_id=?", (cal["sensor_id"],)).fetchone() if cal["sensor_id"] else None
    if cal["sensor_id"] and not sensor:
        abort(404)
    points = db.execute("SELECT * FROM calibration_points WHERE cal_id=? ORDER BY point_no", (cal_id,)).fetchall()
    procedure_id = wo["procedure_id"]
    procedure = db.execute("SELECT * FROM calibration_procedures WHERE procedure_id=?", (procedure_id,)).fetchone() if procedure_id else None
    procedure_points = db.execute(
        "SELECT * FROM calibration_procedure_points WHERE procedure_id=? ORDER BY point_no",
        (procedure_id,)
    ).fetchall() if procedure_id else []

    if procedure_id and (not procedure or not procedure["active"]):
        flash("The assigned calibration procedure is no longer active.", "error")
        return redirect(url_for("work_order_detail", work_order_id=wo["work_order_id"]))
    if procedure_id and not procedure_points:
        flash("The assigned calibration procedure has no required measurement points.", "error")
        return redirect(url_for("work_order_detail", work_order_id=wo["work_order_id"]))

    if request.method == "POST":
        f = request.form
        try:
            cal_date = datetime.strptime(f["cal_date"], "%Y-%m-%d").date().isoformat()
            refs = [float(x) for x in f.getlist("reference_value")]
            found_values = [float(x) for x in f.getlist("as_found_value")]
            left_raw = f.getlist("as_left_value")
            as_left = [float(x) if x.strip() else None for x in left_raw] if left_raw else [None] * len(refs)
            default_tol = sensor["tolerance"] if sensor else (req["pending_tolerance"] or 0.5)
            raw = f.getlist("tolerance")
            tols = ([float(t) if t.strip() else default_tol for t in raw] if raw else [default_tol] * len(refs))
            if (not refs or len(refs) != len(found_values) or len(refs) != len(tols) or len(as_left) != len(refs)
                    or len(refs) > 30
                    or not all(math.isfinite(x) for x in refs + found_values + tols + [x for x in as_left if x is not None])
                    or any(t < 0 for t in tols)):
                raise ValueError("Check the date and the numeric values for every measurement point.")
            if procedure_id:
                if len(procedure_points) != len(refs) or any(
                    abs(refs[i] - procedure_points[i]["reference_value"]) > 1e-9
                    or abs(tols[i] - procedure_points[i]["tolerance"]) > 1e-9
                    for i in range(len(refs))
                ):
                    raise ValueError("Measurement points must match the assigned controlled calibration procedure.")
            points_new = []
            for ref, found, left, tol in zip(refs, found_values, as_left, tols):
                found_err = round(found - ref, 6)
                left_err = round(left - ref, 6) if left is not None else found_err
                found_result = "PASS" if abs(found_err) <= tol else "FAIL"
                left_result = "PASS" if abs(left_err) <= tol else "FAIL"
                points_new.append((ref, found, found_err, found_result, tol, left, left_err, left_result))
            adjustment_status = f.get("adjustment_status", "NOT REQUIRED").strip().upper()
            if adjustment_status not in ("NOT REQUIRED", "REQUIRED", "PERFORMED"):
                adjustment_status = "NOT REQUIRED"
            if adjustment_status == "PERFORMED" and any(p[5] is None for p in points_new):
                raise ValueError("Enter an As-Left reading for every point when adjustment is marked as performed.")
            adjustment_notes = f.get("adjustment_notes", "").strip()
            technician_remarks = f.get("technician_remarks", "").strip()
            if adjustment_status == "PERFORMED" and not adjustment_notes:
                raise ValueError("Enter adjustment notes when adjustment is marked as performed.")

            standards_selected = selected_reference_standards(db, f, cal_date, work_order_standard_ids)
            std = standards_selected[0]
            std_id = std["standard_id"]
            ref_text = "; ".join(f"{x['code']} – {x['name']}" for x in standards_selected)
            std_details = json.dumps([
                {"standard_id": x["standard_id"], "code": x["code"], "name": x["name"],
                 "standard_type": x["standard_type"], "manufacturer": x["manufacturer"],
                 "serial": x["serial_number"], "traceability": x["traceability"],
                 "certificate": x["certificate_no"], "calibrated_on": x["calibrated_on"],
                 "valid_until": x["valid_until"], "uncertainty": x["uncertainty"]}
                for x in standards_selected
            ], ensure_ascii=False)

            final_errors = [p[6] if p[5] is not None else p[2] for p in points_new]
            worst_index = max(range(len(points_new)), key=lambda i: abs(final_errors[i]))
            worst = points_new[worst_index]
            result = "FAIL" if any(abs(final_errors[i]) > points_new[i][4] for i in range(len(points_new))) else "PASS"
            mean_error = round(sum(final_errors) / len(final_errors), 6)
            max_error = round(max(abs(x) for x in final_errors), 6)
            uncertainty = validate_calibration_controls(f, procedure, std, sensor["unit"] if sensor else (req["pending_unit"] or ""))
            interval_days = sensor["interval_days"] if sensor else (req["pending_interval_days"] or 365)
            due = (date.fromisoformat(cal_date) + timedelta(days=interval_days)).isoformat()

            with db:
                current_revision = db.execute("SELECT revision_no FROM calibrations WHERE cal_id=?", (cal_id,)).fetchone()["revision_no"]
                next_revision = current_revision + 1
                db.execute("""UPDATE calibrations SET
                    cal_date=?, reference_standard=?, reference_value=?, error=?,
                    result=?, next_due=?, performed_by=?, n_points=?, standard_id=?, standard_details=?,
                    mean_error=?, max_error=?, adjustment_status=?, adjustment_notes=?, technician_remarks=?,
                    standard_uncertainty=?, resolution=?, repeatability=?, environmental_uncertainty=?,
                    other_uncertainty=?, combined_standard_uncertainty=?, coverage_factor=?,
                    expanded_uncertainty=?, uncertainty_method=?, uncertainty_calculation_json=?, environment_temperature=?, environment_humidity=?, revision_no=?, lifecycle_status='RETURNED', updated_at=? WHERE cal_id=?""",
                    (cal_date, ref_text, worst[0], final_errors[worst_index], result, due, g.user["full_name"],
                     len(points_new), std_id, std_details, mean_error, max_error, adjustment_status,
                     adjustment_notes, technician_remarks, uncertainty["standard_uncertainty"],
                     uncertainty["resolution"], uncertainty["repeatability"], uncertainty["environmental_uncertainty"],
                     uncertainty["other_uncertainty"], uncertainty["combined_standard_uncertainty"],
                     uncertainty["coverage_factor"], uncertainty["expanded_uncertainty"], uncertainty["uncertainty_method"],
                     json.dumps(uncertainty["calculation"], ensure_ascii=False), uncertainty["environment_temperature"], uncertainty["environment_humidity"],
                     next_revision, datetime.now().isoformat(timespec="seconds"), cal_id))
                persist_calibration_reference_standards(db, cal_id, standards_selected)
                db.execute("DELETE FROM calibration_points WHERE cal_id=?", (cal_id,))
                db.executemany("""INSERT INTO calibration_points
                    (cal_id,point_no,reference_value,error,result,tolerance,
                     as_found_value,as_found_error,as_found_result,as_left_value,as_left_error,as_left_result)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [(cal_id, i, p[0], final_errors[i-1],
                      "PASS" if abs(final_errors[i-1]) <= p[4] else "FAIL", p[4],
                      p[1], p[2], p[3],
                       (p[5] if p[5] is not None else p[1]),
                       (p[6] if p[5] is not None else p[2]),
                       (p[7] if p[5] is not None else p[3])
                     ) for i, p in enumerate(points_new, 1)])
                record_calibration_revision(db, cal_id, "CORRECTED", g.user["user_id"])
                if not sensor:
                    sensor_type = f.get("sensor_type", "").strip()
                    manufacturer = f.get("manufacturer", "").strip()
                    serial_number = f.get("serial_number", "").strip()
                    station_id_raw = f.get("station_id", "").strip()
                    station_id = int(station_id_raw) if station_id_raw.isdigit() else None
                    station_name = f.get("station_name", "").strip()
                    station_location = f.get("station_location", "").strip()
                    station_type = f.get("station_type", "").strip() or "Meteorological"
                    unit = f.get("unit", "").strip()
                    if not sensor_type or not serial_number or not station_name:
                        raise ValueError("Sensor type, serial number and station name are required.")
                    db.execute("""UPDATE calibration_requests SET
                        pending_sensor_type=?, pending_manufacturer=?, pending_serial_number=?,
                        pending_interval_days=?, pending_tolerance=?, pending_unit=?, pending_station_id=?,
                        pending_station_name=?, pending_station_location=?, pending_station_type=?, updated_at=?
                        WHERE request_id=?""",
                        (sensor_type, manufacturer, serial_number,
                         int(f.get("interval_days", req["pending_interval_days"] or 365)),
                         float(f.get("sensor_tolerance", req["pending_tolerance"] or 0.5)), unit, station_id,
                         station_name, station_location, station_type,
                         datetime.now().isoformat(timespec="seconds"), req["request_id"]))
                # A correction is immediately submitted as a new review revision.
                validate_calibration_record_for_submission(db, cal_id, wo["work_order_id"], g.user["user_id"])
                now = datetime.now().isoformat(timespec="seconds")
                pending = db.execute(
                    "SELECT review_id FROM calibration_review_history WHERE work_order_id=? AND decision='PENDING'",
                    (wo["work_order_id"],)
                ).fetchone()
                if pending:
                    raise ValueError("This calibration is already awaiting administrator review.")
                db.execute(
                    """INSERT INTO calibration_review_history
                       (work_order_id, cal_id, submitted_by, submitted_at, submitted_revision, decision)
                       VALUES (?,?,?,?,?, 'PENDING')""",
                    (wo["work_order_id"], cal_id, g.user["user_id"], now, next_revision)
                )
                db.execute(
                    "UPDATE calibrations SET lifecycle_status='SUBMITTED', updated_at=? WHERE cal_id=?",
                    (now, cal_id)
                )
                record_calibration_revision(
                    db, cal_id, "SUBMITTED", g.user["user_id"],
                    comments="Corrected calibration resubmitted for administrator review"
                )
                db.execute(
                    "UPDATE calibration_work_orders SET status='AWAITING REVIEW', updated_at=? WHERE work_order_id=?",
                    (now, wo["work_order_id"])
                )
                req_state = db.execute("SELECT status FROM calibration_requests WHERE request_id=?",
                                       (cal["request_id"],)).fetchone()
                if req_state and req_state["status"] == "IN CALIBRATION":
                    transition_request_status(db, cal["request_id"], "UNDER REVIEW",
                                              g.user["user_id"], "Corrected calibration resubmitted for administrator review")
                db.commit()
            flash("Corrected calibration submitted to the administrator for review.")
            return redirect(url_for("work_order_detail", work_order_id=wo["work_order_id"]))
        except (ValueError, TypeError, sqlite3.Error) as e:
            flash(str(e), "error")

    standards_ = db.execute("SELECT * FROM reference_standards WHERE active=1 ORDER BY code").fetchall()
    stations_ = db.execute("SELECT station_id, name, location, type FROM stations ORDER BY name COLLATE NOCASE").fetchall()
    if sensor:
        return render_template("calibrate.html", s=sensor, today=cal["cal_date"], standards=standards_,
                               requests=[], calibration=cal, selected_standard_ids=[r["standard_id"] for r in calibration_reference_standards(db, cal_id)], points=points, edit_mode=True,
                               work_order_id=wo["work_order_id"], procedure=procedure,
                               procedure_points=procedure_points)
    return render_template("calibrate_pending.html", req=req, today=cal["cal_date"], standards=standards_,
                           stations=stations_, calibration=cal, selected_standard_ids=[r["standard_id"] for r in calibration_reference_standards(db, cal_id)], points=points, edit_mode=True,
                           work_order_id=wo["work_order_id"], procedure=procedure, procedure_points=procedure_points)

@app.route("/calibrations/<int:cal_id>/certificate-preview")
def calibration_certificate_preview(cal_id):
    """Render an unverified certificate preview without an official certificate number."""
    db = get_db()
    r = db.execute(
        """SELECT c.*, COALESCE(s.sensor_type, rq.pending_sensor_type) AS sensor_type,
                  COALESCE(s.manufacturer, rq.pending_manufacturer) AS manufacturer,
                  COALESCE(s.serial_number, rq.pending_serial_number) AS serial_number,
                  COALESCE(s.tolerance, rq.pending_tolerance) AS tolerance,
                  COALESCE(s.unit, rq.pending_unit) AS unit,
                  COALESCE(st.name, rq.pending_station_name) AS station,
                  cp.code AS procedure_code, cp.title AS procedure_title, cp.revision AS procedure_revision
           FROM calibrations c
           LEFT JOIN sensors s ON s.sensor_id=c.sensor_id
           LEFT JOIN stations st ON st.station_id=s.station_id
           LEFT JOIN calibration_requests rq ON rq.request_id=c.request_id
           LEFT JOIN calibration_procedures cp ON cp.procedure_id=c.procedure_id
           WHERE c.cal_id=?""",
        (cal_id,)
    ).fetchone()
    if not r:
        abort(404)
    if not (user_has_role("admin") or user_has_role("superadmin") or user_has_role("reviewer")):
        wo = db.execute(
            "SELECT assigned_technician_id FROM calibration_work_orders WHERE request_id=?",
            (r["request_id"],)
        ).fetchone()
        if not wo or not user_has_role("technician") or wo["assigned_technician_id"] != g.user["user_id"]:
            abort(403)
    pts = db.execute(
        "SELECT * FROM calibration_points WHERE cal_id=? ORDER BY point_no",
        (cal_id,)
    ).fetchall()
    details = json.loads(r["standard_details"]) if r["standard_details"] else None
    standards = calibration_reference_standards(db, cal_id)
    standard = standards[0] if standards else None
    return render_template("certificate.html", r=r, pts=pts, det=details, standard=standard,
                           standards=standards, preview=True)


@app.route("/certificate/<cert>")
def certificate(cert):
    """Render the controlled official certificate and its compact verification QR."""
    db = get_db()
    r = db.execute(
        """SELECT c.*, COALESCE(s.sensor_type, rq.pending_sensor_type) AS sensor_type,
                  COALESCE(s.manufacturer, rq.pending_manufacturer) AS manufacturer,
                  COALESCE(s.serial_number, rq.pending_serial_number) AS serial_number,
                  COALESCE(s.tolerance, rq.pending_tolerance) AS tolerance,
                  COALESCE(s.unit, rq.pending_unit) AS unit,
                  COALESCE(st.name, rq.pending_station_name) AS station,
                  cp.code AS procedure_code, cp.title AS procedure_title, cp.revision AS procedure_revision,
                  (SELECT u.full_name FROM calibration_review_history rh
                   JOIN users u ON u.user_id=rh.reviewed_by
                   WHERE rh.cal_id=c.cal_id AND rh.decision='APPROVED'
                   ORDER BY rh.reviewed_at DESC, rh.review_id DESC LIMIT 1) AS approved_by,
                  (SELECT u.full_name FROM users u WHERE u.user_id=c.certificate_issued_by) AS certificate_issued_by_name
           FROM calibrations c
           LEFT JOIN sensors s ON s.sensor_id=c.sensor_id
           LEFT JOIN stations st ON st.station_id=s.station_id
           LEFT JOIN calibration_requests rq ON rq.request_id=c.request_id
           LEFT JOIN calibration_procedures cp ON cp.procedure_id=c.procedure_id
           WHERE c.certificate_no=?""", (cert,)).fetchone()
    if not r:
        abort(404)

    if r["lifecycle_status"] != "APPROVED":
        abort(404)
    if r["certificate_status"] != "ACTIVE":
        latest = db.execute(
            "SELECT reason, previous_certificate_no FROM certificate_history "
            "WHERE certificate_no=? ORDER BY history_id DESC LIMIT 1", (cert,)
        ).fetchone()
        return render_template(
            "certificate_status.html",
            certificate_no=cert,
            status=r["certificate_status"] or "INVALID",
            reason=latest["reason"] if latest else None,
            replacement=latest["previous_certificate_no"] if latest else None,
        )

    pts = db.execute("SELECT * FROM calibration_points WHERE cal_id=? ORDER BY point_no",
                     (r["cal_id"],)).fetchall()
    details = json.loads(r["standard_details"]) if r["standard_details"] else None
    standards = calibration_reference_standards(db, r["cal_id"])
    standard = standards[0] if standards else None

    # The printed certificate is designed to work in the field without internet.
    # The QR carries the complete compact calibration record itself. The normal
    # online verification endpoint remains available separately for live status
    # and fingerprint validation.
    offline_payload = build_offline_certificate_payload(r, pts, standards)
    qr_code = _qr_data_uri(offline_payload)
    return render_template(
        "certificate.html", r=r, pts=pts, det=details, standard=standard, standards=standards,
        preview=False, qr_code=qr_code, offline_payload=offline_payload,
    )


@app.route("/certificate/<cert>/pdf")
def certificate_pdf(cert):
    """Generate the A5 portrait certificate matching the browser print design."""
    db = get_db()
    r = db.execute(
        """SELECT c.*, COALESCE(s.sensor_type, rq.pending_sensor_type) AS sensor_type,
                  COALESCE(s.manufacturer, rq.pending_manufacturer) AS manufacturer,
                  COALESCE(s.serial_number, rq.pending_serial_number) AS serial_number,
                  COALESCE(s.tolerance, rq.pending_tolerance) AS tolerance,
                  COALESCE(s.unit, rq.pending_unit) AS unit,
                  COALESCE(st.name, rq.pending_station_name) AS station,
                  cp.code AS procedure_code, cp.title AS procedure_title, cp.revision AS procedure_revision,
                  (SELECT u.full_name FROM calibration_review_history rh
                   JOIN users u ON u.user_id=rh.reviewed_by
                   WHERE rh.cal_id=c.cal_id AND rh.decision='APPROVED'
                   ORDER BY rh.reviewed_at DESC, rh.review_id DESC LIMIT 1) AS approved_by,
                  (SELECT u.full_name FROM users u WHERE u.user_id=c.certificate_issued_by) AS certificate_issued_by_name
           FROM calibrations c
           LEFT JOIN sensors s ON s.sensor_id=c.sensor_id
           LEFT JOIN stations st ON st.station_id=s.station_id
           LEFT JOIN calibration_requests rq ON rq.request_id=c.request_id
           LEFT JOIN calibration_procedures cp ON cp.procedure_id=c.procedure_id
           WHERE c.certificate_no=? AND c.lifecycle_status='APPROVED'
                 AND c.certificate_status='ACTIVE'""",
        (cert,),
    ).fetchone()
    if not r:
        abort(404)
    try:
        from reportlab.lib.pagesizes import A5
        from reportlab.pdfgen import canvas
        from reportlab.lib.utils import ImageReader
        from reportlab.lib import colors
    except ImportError:
        abort(503, "PDF generation requires reportlab.")

    pts = db.execute("SELECT * FROM calibration_points WHERE cal_id=? ORDER BY point_no", (r["cal_id"],)).fetchall()
    standards = calibration_reference_standards(db, r["cal_id"])
    offline_payload = build_offline_certificate_payload(r, pts, standards)
    qr = qrcode.make(offline_payload)
    qr_bytes = io.BytesIO()
    qr.save(qr_bytes, format="PNG")
    qr_bytes.seek(0)

    out = io.BytesIO()
    pdf = canvas.Canvas(out, pagesize=A5)
    width, height = A5
    pdf.setTitle("DHM Calibration Certificate " + cert)
    pdf.setAuthor("DHM Calibration Laboratory")
    navy, ink, muted = colors.HexColor("#174b7b"), colors.HexColor("#172b43"), colors.HexColor("#607187")
    pale, line = colors.HexColor("#f5f9fc"), colors.HexColor("#d7e1ec")
    margin, right, top = 25, width - 25, height - 25

    pdf.setStrokeColor(navy); pdf.setLineWidth(1); pdf.roundRect(margin,margin,width-2*margin,height-2*margin,6,stroke=1,fill=0)

    # Official Nepal emblem: keep the watermark very light and never underneath the QR panel.
    emblem_path = os.path.join(app.root_path, "static", "images", "nepal-emblem.png")
    if os.path.exists(emblem_path):
        pdf.saveState()
        pdf.setFillAlpha(0.045)
        pdf.drawImage(ImageReader(emblem_path), width*.22, height*.35, width=width*.56, height=height*.30,
                      preserveAspectRatio=True, anchor="c", mask="auto")
        pdf.restoreState()

    if os.path.exists(emblem_path):
        pdf.drawImage(ImageReader(emblem_path), margin+6, top-43, width=42, height=36,
                      preserveAspectRatio=True, anchor="c", mask="auto")

    pdf.setFillColor(navy); pdf.setFont("Helvetica-Bold",9.5); pdf.drawString(margin+54,top-14,"Government of Nepal")
    pdf.setFont("Helvetica-Bold",8.5); pdf.drawString(margin+54,top-25,"Ministry of Energy, Water Resources and Irrigation")
    pdf.setFont("Helvetica-Bold",9); pdf.drawString(margin+54,top-36,"Department of Hydrology and Meteorology")
    pdf.setFillColor(muted); pdf.setFont("Helvetica-Bold",7); pdf.drawString(margin+54,top-46,"Calibration Laboratory")
    pdf.setFillColor(navy); pdf.setFont("Helvetica-Bold",11.5); pdf.drawRightString(right-8,top-16,"CALIBRATION CERTIFICATE")
    pdf.setFillColor(ink); pdf.setFont("Helvetica-Bold",6.7); pdf.drawRightString(right-8,top-28,"No. "+cert)
    pdf.setStrokeColor(navy); pdf.line(margin+8,top-46,right-8,top-46)

    left_x=margin+8; qr_panel_w=118; gap=13; panel_x=right-8-qr_panel_w; left_right=panel_x-gap
    value_x=left_x+64; y=top-64
    pdf.setFillColor(navy); pdf.setFont("Helvetica-Bold",7); pdf.drawString(left_x,y,"FIELD INSTALLATION IDENTITY"); y-=13
    for label,value in (("Sensor ID",r["sensor_id"] or "—"),("Instrument",r["sensor_type"] or "—"),
                        ("Serial number",r["serial_number"] or "—"),("Manufacturer",r["manufacturer"] or "—"),
                        ("Station",r["station"] or "—"),("Unit",r["unit"] or "—")):
        pdf.setFillColor(muted); pdf.setFont("Helvetica",6.2); pdf.drawString(left_x,y,label)
        pdf.setFillColor(ink); pdf.setFont("Helvetica-Bold",6.2); pdf.drawString(value_x,y,str(value)[:30]); y-=13

    y-=2; pdf.setFillColor(navy); pdf.setFont("Helvetica-Bold",7); pdf.drawString(left_x,y,"CALIBRATION STATUS"); y-=13
    ref_summary="; ".join(std["code"] for std in standards) if standards else (r["reference_standard"] or "—")
    tol=((str(r["tolerance"])+" "+(r["unit"] or "")).strip() if r["tolerance"] is not None else "—")
    for label,value in (("Cal. date",r["cal_date"] or "—"),("Next due",r["next_due"] or "—"),
                        ("Tolerance",tol),("Reference",ref_summary),("Approved by",r["approved_by"] or "—")):
        pdf.setFillColor(muted); pdf.setFont("Helvetica",6.2); pdf.drawString(left_x,y,label)
        pdf.setFillColor(ink); pdf.setFont("Helvetica-Bold",6.2); pdf.drawString(value_x,y,str(value)[:30]); y-=13

    y-=3; card_h=55; pdf.setFillColor(pale); pdf.roundRect(left_x,y-card_h+5,left_right-left_x,card_h,5,stroke=0,fill=1)
    pdf.setFillColor(navy); pdf.setFont("Helvetica-Bold",7); pdf.drawString(left_x+7,y-12,"CALIBRATION RESULT")
    pdf.setFillColor(colors.HexColor("#b42318") if r["result"]=="FAIL" else colors.HexColor("#087443"))
    pdf.setFont("Helvetica-Bold",12); pdf.drawCentredString((left_x+left_right)/2,y-30,str(r["result"] or "—"))
    max_error=r["max_error"] if r["max_error"] is not None else (abs(r["error"]) if r["error"] is not None else "—")
    pdf.setFillColor(muted); pdf.setFont("Helvetica",5.8); pdf.drawString(left_x+7,y-44,"Maximum error")
    pdf.setFillColor(ink); pdf.setFont("Helvetica-Bold",6.5); pdf.drawString(left_x+55,y-44,(str(max_error)+" "+(r["unit"] or "")).strip())
    pdf.setFillColor(muted); pdf.setFont("Helvetica",5.8); pdf.drawRightString(left_right-7,y-44,"Points: "+str(len(pts)))

    panel_y=margin+30; panel_h=height-2*margin-92
    pdf.setStrokeColor(navy); pdf.setLineWidth(.8); pdf.roundRect(panel_x,panel_y,qr_panel_w,panel_h,5,stroke=1,fill=0)
    pdf.setFillColor(navy); pdf.setFont("Helvetica-Bold",7); pdf.drawCentredString(panel_x+qr_panel_w/2,top-64,"OFFLINE RECORD")
    qr_size=91; qr_x=panel_x+(qr_panel_w-qr_size)/2; qr_y=top-78-qr_size
    pdf.drawImage(ImageReader(qr_bytes),qr_x,qr_y,qr_size,qr_size,preserveAspectRatio=True,mask="auto")
    pdf.setFillColor(navy); pdf.setFont("Helvetica-Bold",6.5); pdf.drawCentredString(panel_x+qr_panel_w/2,qr_y-12,"SCAN WITH ANY QR READER")
    pdf.setFillColor(muted); pdf.setFont("Helvetica",5.5); pdf.drawCentredString(panel_x+qr_panel_w/2,qr_y-23,"Complete calibration record")
    pdf.drawCentredString(panel_x+qr_panel_w/2,qr_y-31,"is embedded — no internet.")
    pdf.setFillColor(ink); pdf.setFont("Helvetica-Bold",5.8)
    pdf.drawCentredString(panel_x+qr_panel_w/2,panel_y+36,"Measurements, uncertainty,")
    pdf.drawCentredString(panel_x+qr_panel_w/2,panel_y+27,"traceability, procedure")
    pdf.drawCentredString(panel_x+qr_panel_w/2,panel_y+18,"and approval data included.")

    pdf.setStrokeColor(line); pdf.line(margin+8,margin+20,right-8,margin+20)
    pdf.setFillColor(muted); pdf.setFont("Helvetica",5.4)
    pdf.drawString(margin+8,margin+11,"Retain this certificate with the installed sensor record.")
    pdf.drawRightString(right-8,margin+11,"DHM Calibration Laboratory")
    pdf.showPage(); pdf.save(); out.seek(0)
    return Response(out.getvalue(),mimetype="application/pdf",
                    headers={"Content-Disposition":'inline; filename="'+cert+'.pdf"'})

@app.route("/certificates/<cert>/withdraw", methods=["POST"])
@admin_required
def withdraw_certificate(cert):
    """Withdraw an issued certificate without deleting its controlled record."""
    db = get_db()
    row = db.execute(
        "SELECT cal_id, certificate_no, certificate_status, certificate_fingerprint "
        "FROM calibrations WHERE certificate_no=? AND lifecycle_status='APPROVED'",
        (cert,),
    ).fetchone()
    if not row:
        abort(404)
    if row["certificate_status"] != "ACTIVE":
        flash("Only an active certificate can be withdrawn.", "error")
        return redirect(url_for("certificate", cert=cert))

    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("A reason is required when withdrawing a certificate.", "error")
        return redirect(url_for("certificate", cert=cert))

    now = datetime.now().isoformat(timespec="seconds")
    with db:
        db.execute(
            "UPDATE calibrations SET certificate_status='WITHDRAWN', updated_at=? WHERE cal_id=? AND certificate_status='ACTIVE'",
            (now, row["cal_id"]),
        )
        db.execute(
            """INSERT INTO certificate_history
               (cal_id, certificate_no, event_type, fingerprint, reason, changed_by, changed_at)
               VALUES (?,?,'WITHDRAWN',?,?,?,?)""",
            (row["cal_id"], cert, row["certificate_fingerprint"], reason, g.user["user_id"], now),
        )
        audit_event(
            "CERTIFICATE_WITHDRAWN", "certificate", cert,
            old_value={"status": "ACTIVE"},
            new_value={"status": "WITHDRAWN"},
            details={"cal_id": row["cal_id"], "reason": reason},
        )
    flash(f"Certificate {cert} has been withdrawn.")
    return redirect(url_for("calibration_reviews"))


@app.route("/certificates/<cert>/reissue", methods=["POST"])
@admin_required
def reissue_certificate(cert):
    """Issue a replacement certificate number while preserving the approved measurements."""
    db = get_db()
    row = db.execute(
        "SELECT cal_id, certificate_no, cal_date, approved_revision, certificate_status, certificate_fingerprint "
        "FROM calibrations WHERE certificate_no=? AND lifecycle_status='APPROVED'",
        (cert,),
    ).fetchone()
    if not row:
        abort(404)
    if row["certificate_status"] not in ("ACTIVE", "WITHDRAWN"):
        flash("This certificate cannot be reissued in its current state.", "error")
        return redirect(url_for("certificate", cert=cert))

    reason = request.form.get("reason", "").strip()
    if not reason:
        flash("A reason is required when reissuing a certificate.", "error")
        return redirect(url_for("certificate", cert=cert))

    now = datetime.now().isoformat(timespec="seconds")
    with db:
        new_cert = next_certificate(db, row["cal_date"])
        db.execute(
            """UPDATE calibrations
               SET certificate_no=?, certificate_status='ACTIVE',
                   certificate_reissued_from=?, certificate_reissued_at=?,
                   certificate_issued_by=?, certificate_issued_at=?, updated_at=?
               WHERE cal_id=? AND lifecycle_status='APPROVED'""",
            (new_cert, cert, now, g.user["user_id"], now, now, row["cal_id"]),
        )
        # Recalculate because the certificate identifier itself is part of the fingerprint.
        fingerprint = build_certificate_fingerprint(db, row["cal_id"])
        db.execute(
            "UPDATE calibrations SET certificate_fingerprint=? WHERE cal_id=?",
            (fingerprint, row["cal_id"]),
        )
        db.execute(
            """INSERT INTO certificate_history
               (cal_id, certificate_no, event_type, previous_certificate_no, fingerprint, reason, changed_by, changed_at)
               VALUES (?,?,'REISSUED',?,?,?,?,?)""",
            (row["cal_id"], new_cert, cert, fingerprint, reason, g.user["user_id"], now),
        )
        # Retain a searchable historical event for the old certificate number.
        db.execute(
            """INSERT INTO certificate_history
               (cal_id, certificate_no, event_type, previous_certificate_no, fingerprint, reason, changed_by, changed_at)
               VALUES (?,?,'SUPERSEDED',?,?,?,?,?)""",
            (row["cal_id"], cert, new_cert, row["certificate_fingerprint"], reason, g.user["user_id"], now),
        )
        audit_event(
            "CERTIFICATE_REISSUED", "certificate", new_cert,
            old_value={"certificate_no": cert, "status": row["certificate_status"]},
            new_value={"certificate_no": new_cert, "status": "ACTIVE", "fingerprint": fingerprint},
            details={"cal_id": row["cal_id"], "reason": reason, "previous_certificate_no": cert},
        )
    flash(f"Certificate {cert} was superseded and replacement certificate {new_cert} was issued.")
    return redirect(url_for("certificate", cert=new_cert))


@app.route("/verify/<cert>/<token>")
def verify_certificate(cert, token):
    """Public, read-only certificate verification endpoint."""
    expected = certificate_verification_token(cert)
    if not hmac.compare_digest(str(token), expected):
        abort(404)

    db = get_db()
    r = db.execute(
        """SELECT c.*, COALESCE(s.sensor_type, rq.pending_sensor_type) AS sensor_type,
                  COALESCE(s.manufacturer, rq.pending_manufacturer) AS manufacturer,
                  COALESCE(s.serial_number, rq.pending_serial_number) AS serial_number,
                  COALESCE(s.tolerance, rq.pending_tolerance) AS tolerance,
                  COALESCE(s.unit, rq.pending_unit) AS unit,
                  COALESCE(st.name, rq.pending_station_name) AS station,
                  cp.code AS procedure_code, cp.title AS procedure_title, cp.revision AS procedure_revision,
                  (SELECT u.full_name FROM calibration_review_history rh
                   JOIN users u ON u.user_id=rh.reviewed_by
                   WHERE rh.cal_id=c.cal_id AND rh.decision='APPROVED'
                   ORDER BY rh.reviewed_at DESC, rh.review_id DESC LIMIT 1) AS approved_by,
                  (SELECT u.full_name FROM users u WHERE u.user_id=c.certificate_issued_by) AS certificate_issued_by_name
           FROM calibrations c
           LEFT JOIN sensors s ON s.sensor_id=c.sensor_id
           LEFT JOIN stations st ON st.station_id=s.station_id
           LEFT JOIN calibration_requests rq ON rq.request_id=c.request_id
           LEFT JOIN calibration_procedures cp ON cp.procedure_id=c.procedure_id
           WHERE c.certificate_no=?""", (cert,)
    ).fetchone()
    if not r:
        # Reissued certificates retain a searchable historical verification result.
        history = db.execute(
            "SELECT * FROM certificate_history WHERE certificate_no=? ORDER BY history_id DESC LIMIT 1",
            (cert,),
        ).fetchone()
        if history and history["event_type"] == "SUPERSEDED":
            return render_template("certificate_status.html", certificate_no=cert,
                                   status="SUPERSEDED", reason=history["reason"],
                                   replacement=history["previous_certificate_no"])
        abort(404)

    if r["certificate_status"] != "ACTIVE" or r["lifecycle_status"] != "APPROVED":
        latest = db.execute(
            "SELECT event_type, reason, previous_certificate_no FROM certificate_history "
            "WHERE certificate_no=? ORDER BY history_id DESC LIMIT 1", (cert,)
        ).fetchone()
        return render_template(
            "certificate_status.html",
            certificate_no=cert,
            status=r["certificate_status"] or "INVALID",
            reason=latest["reason"] if latest else None,
            replacement=latest["previous_certificate_no"] if latest and latest["event_type"] == "SUPERSEDED" else None,
        )

    actual_fingerprint = build_certificate_fingerprint(db, r["cal_id"])
    if not r["certificate_fingerprint"]:
        # Legacy certificate: establish its fingerprint once, without changing
        # any measurement data.
        db.execute("UPDATE calibrations SET certificate_fingerprint=? WHERE cal_id=?",
                   (actual_fingerprint, r["cal_id"]))
        db.commit()
    elif not hmac.compare_digest(actual_fingerprint, r["certificate_fingerprint"]):
        audit_event(
            "CERTIFICATE_INTEGRITY_FAILURE", "certificate", cert,
            details={"cal_id": r["cal_id"], "stored_fingerprint": r["certificate_fingerprint"],
                     "calculated_fingerprint": actual_fingerprint},
        )
        db.commit()
        return render_template("certificate_status.html", certificate_no=cert,
                               status="INTEGRITY_FAILURE",
                               reason="The current laboratory record does not match the fingerprint stored when this certificate was issued.",
                               replacement=None), 409

    audit_event(
        "CERTIFICATE_VERIFIED", "certificate", cert,
        details={"cal_id": r["cal_id"], "fingerprint": r["certificate_fingerprint"] or actual_fingerprint},
    )
    db.commit()

    pts = db.execute("SELECT * FROM calibration_points WHERE cal_id=? ORDER BY point_no",
                     (r["cal_id"],)).fetchall()
    details = json.loads(r["standard_details"]) if r["standard_details"] else None
    standards = calibration_reference_standards(db, r["cal_id"])
    standard = standards[0] if standards else None
    return render_template("certificate_full.html", r=r, pts=pts, det=details, standard=standard, standards=standards)


