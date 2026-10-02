"""Controlled calibration procedure management."""
from app import *

PROCEDURE_FIELDS = ("code", "title", "instrument_type", "method", "revision",
                    "effective_date", "tolerance_unit", "environmental_requirements",
                    "instructions")

def read_procedure(form):
    d = {k: form.get(k, "").strip() for k in PROCEDURE_FIELDS}
    if not d["code"] or not d["title"] or not d["instrument_type"] or not d["method"]:
        raise ValueError("Code, title, instrument type and method are required.")
    try:
        d["effective_date"] = date.fromisoformat(d["effective_date"]).isoformat()
    except ValueError:
        raise ValueError("Enter a valid effective date.")
    return d

def read_points(form):
    refs = form.getlist("reference_value")
    tols = form.getlist("tolerance")
    if not refs:
        raise ValueError("Add at least one calibration point.")
    if len(refs) != len(tols) or len(refs) > 30:
        raise ValueError("Enter 1–30 complete calibration points.")
    points = []
    for i, (ref, tol) in enumerate(zip(refs, tols), 1):
        try:
            rv, tv = float(ref), float(tol)
        except ValueError:
            raise ValueError("Calibration point values must be numeric.")
        if not math.isfinite(rv) or not math.isfinite(tv) or tv < 0:
            raise ValueError("Calibration point values are invalid.")
        points.append((i, rv, tv))
    return points

@app.route("/procedures")
@admin_required
def procedures():
    db = get_db()
    rows = db.execute(
        """SELECT p.*, COUNT(pp.procedure_point_id) AS point_count
           FROM calibration_procedures p
           LEFT JOIN calibration_procedure_points pp ON pp.procedure_id=p.procedure_id
           GROUP BY p.procedure_id
           ORDER BY p.active DESC, p.instrument_type, p.code"""
    ).fetchall()
    return render_template("procedures.html", rows=rows)

@app.route("/procedures/new", methods=["GET", "POST"])
@admin_required
def new_procedure():
    if request.method == "POST":
        try:
            d = read_procedure(request.form)
            points = read_points(request.form)
            db = get_db()
            now = datetime.now().isoformat(timespec="seconds")
            cur = db.execute(
                """INSERT INTO calibration_procedures
                   (code,title,instrument_type,method,revision,effective_date,tolerance_unit,
                    environmental_requirements,instructions,active,created_by,created_at,updated_at)
                   VALUES (:code,:title,:instrument_type,:method,:revision,:effective_date,:tolerance_unit,
                           :environmental_requirements,:instructions,1,:created_by,:created_at,:updated_at)""",
                {**d, "active": 1, "created_by": g.user["user_id"],
                 "created_at": now, "updated_at": now}
            )
            pid = cur.lastrowid
            db.executemany(
                "INSERT INTO calibration_procedure_points(procedure_id,point_no,reference_value,tolerance) VALUES (?,?,?,?)",
                [(pid, n, rv, tol) for n, rv, tol in points]
            )
            audit_event("PROCEDURE_CREATED", "calibration_procedure", pid,
                        new_value={"code": d["code"], "revision": d["revision"], "points": len(points)})
            db.commit()
            flash("Calibration procedure created.")
            return redirect(url_for("procedure_detail", procedure_id=pid))
        except ValueError as e:
            flash(str(e), "error")
        except sqlite3.IntegrityError:
            flash("Could not save the procedure: that procedure code already exists.", "error")
    return render_template("procedure_form.html", x=request.form if request.method == "POST" else None,
                           points=[], editing=False, today=date.today().isoformat())

@app.route("/procedures/<int:procedure_id>")
@admin_required
def procedure_detail(procedure_id):
    db = get_db()
    p = db.execute("SELECT * FROM calibration_procedures WHERE procedure_id=?", (procedure_id,)).fetchone()
    if not p:
        abort(404)
    points = db.execute(
        "SELECT * FROM calibration_procedure_points WHERE procedure_id=? ORDER BY point_no",
        (procedure_id,)
    ).fetchall()
    usage = db.execute(
        """SELECT w.work_order_no, w.status, r.request_no, r.instrument_description,
                  u.full_name AS technician_name
           FROM calibration_work_orders w
           JOIN calibration_requests r ON r.request_id=w.request_id
           JOIN users u ON u.user_id=w.assigned_technician_id
           WHERE w.procedure_id=? ORDER BY w.work_order_id DESC LIMIT 100""",
        (procedure_id,)
    ).fetchall()
    return render_template("procedure_detail.html", p=p, points=points, usage=usage)

@app.route("/procedures/<int:procedure_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_procedure(procedure_id):
    db = get_db()
    p = db.execute("SELECT * FROM calibration_procedures WHERE procedure_id=?", (procedure_id,)).fetchone()
    if not p:
        abort(404)
    if request.method == "POST":
        try:
            d = read_procedure(request.form)
            points = read_points(request.form)
            used_count = db.execute(
                "SELECT COUNT(*) FROM calibration_work_orders WHERE procedure_id=?",
                (procedure_id,)
            ).fetchone()[0]
            if used_count:
                old_points = db.execute(
                    "SELECT reference_value, tolerance FROM calibration_procedure_points WHERE procedure_id=? ORDER BY point_no",
                    (procedure_id,)
                ).fetchall()
                changed = (
                    any(str(d.get(k, "")) != str(p[k] or "") for k in
                        ("code", "title", "instrument_type", "method", "revision",
                         "effective_date", "tolerance_unit", "environmental_requirements", "instructions"))
                    or len(points) != len(old_points)
                    or any(abs(points[i][1] - old_points[i]["reference_value"]) > 1e-9
                           or abs(points[i][2] - old_points[i]["tolerance"]) > 1e-9
                           for i in range(min(len(points), len(old_points))))
                )
                if changed:
                    raise ValueError(
                        "This calibration procedure has been assigned to work orders. "
                        "Its method, revision, instructions and required points are locked. "
                        "Create a new procedure revision instead."
                    )
            now = datetime.now().isoformat(timespec="seconds")
            db.execute(
                """UPDATE calibration_procedures SET code=:code,title=:title,instrument_type=:instrument_type,
                   method=:method,revision=:revision,effective_date=:effective_date,tolerance_unit=:tolerance_unit,
                   environmental_requirements=:environmental_requirements,instructions=:instructions,
                   updated_at=:updated_at WHERE procedure_id=:procedure_id""",
                {**d, "procedure_id": procedure_id, "updated_at": now}
            )
            db.execute("DELETE FROM calibration_procedure_points WHERE procedure_id=?", (procedure_id,))
            db.executemany(
                "INSERT INTO calibration_procedure_points(procedure_id,point_no,reference_value,tolerance) VALUES (?,?,?,?)",
                [(procedure_id, n, rv, tol) for n, rv, tol in points]
            )
            audit_event("PROCEDURE_UPDATED", "calibration_procedure", procedure_id,
                        new_value={"revision": d["revision"], "points": len(points)})
            db.commit()
            flash("Calibration procedure updated.")
            return redirect(url_for("procedure_detail", procedure_id=procedure_id))
        except ValueError as e:
            flash(str(e), "error")
        p = request.form
    points = db.execute(
        "SELECT * FROM calibration_procedure_points WHERE procedure_id=? ORDER BY point_no",
        (procedure_id,)
    ).fetchall()
    return render_template("procedure_form.html", x=p, points=points,
                           editing=True, procedure_id=procedure_id, today=date.today().isoformat())

@app.route("/procedures/<int:procedure_id>/toggle", methods=["POST"])
@admin_required
def toggle_procedure(procedure_id):
    db = get_db()
    p = db.execute("SELECT * FROM calibration_procedures WHERE procedure_id=?", (procedure_id,)).fetchone()
    if not p:
        abort(404)
    active = 0 if p["active"] else 1
    db.execute("UPDATE calibration_procedures SET active=?, updated_at=? WHERE procedure_id=?",
               (active, datetime.now().isoformat(timespec="seconds"), procedure_id))
    audit_event("PROCEDURE_ACTIVATED" if active else "PROCEDURE_DEACTIVATED",
                "calibration_procedure", procedure_id,
                old_value={"active": p["active"]}, new_value={"active": active})
    db.commit()
    flash("Calibration procedure " + ("activated." if active else "deactivated."))
    return redirect(url_for("procedures"))
