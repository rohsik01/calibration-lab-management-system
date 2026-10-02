"""Route module: standards."""
from app import *

# Form fields used by the reference-standard create/edit routes.
STD_FIELDS = (
    "code", "name", "standard_type", "manufacturer", "serial_number",
    "uncertainty", "traceability", "certificate_no", "calibrated_on", "valid_until",
)

def standard_snapshot(row):
    """Serialize the complete reference-standard state for immutable history."""
    return {k: row[k] for k in row.keys()}


def record_standard_history(db, standard_id, event_type, changed_by=None, details=None):
    row = db.execute(
        "SELECT * FROM reference_standards WHERE standard_id=?", (standard_id,)
    ).fetchone()
    if not row:
        return
    db.execute(
        """INSERT INTO reference_standard_history
           (standard_id, event_type, snapshot_json, changed_by, changed_at, details)
           VALUES (?,?,?,?,?,?)""",
        (standard_id, event_type,
         json.dumps(standard_snapshot(row), ensure_ascii=False, default=str),
         changed_by,
         datetime.now().isoformat(timespec="seconds"),
         details)
    )


def read_standard(f):
    d = {k: f.get(k, "").strip() for k in STD_FIELDS}
    if not d["code"] or not d["name"]:
        raise ValueError("Code and name are required.")
    if not d["certificate_no"]:
        raise ValueError("A calibration certificate number is required for a reference standard.")
    if not d["traceability"]:
        raise ValueError("Traceability information is required for a reference standard.")
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
            record_standard_history(
                db, cur.lastrowid, "CREATED", g.user["user_id"],
                "Reference standard registered"
            )
            audit_event("STANDARD_CREATED", "reference_standard", cur.lastrowid,
                        new_value=d)
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
    used = db.execute(
        """SELECT c.cal_id, c.cal_date, c.sensor_id, c.result, c.certificate_no,
                  c.lifecycle_status, c.revision_no, c.standard_details, w.work_order_id, w.work_order_no,
                  u.full_name AS technician_name
           FROM calibrations c
           LEFT JOIN calibration_work_orders w ON w.request_id=c.request_id
           LEFT JOIN users u ON u.user_id=w.assigned_technician_id
           WHERE c.standard_id=?
           ORDER BY c.cal_id DESC
           LIMIT 250""",
        (sid,)
    ).fetchall()
    usage_count = db.execute(
        "SELECT COUNT(*) FROM calibrations WHERE standard_id=?", (sid,)
    ).fetchone()[0]
    history = db.execute(
        """SELECT h.*, u.full_name AS changed_by_name
           FROM reference_standard_history h
           LEFT JOIN users u ON u.user_id=h.changed_by
           WHERE h.standard_id=?
           ORDER BY h.history_id DESC
           LIMIT 100""",
        (sid,)
    ).fetchall()
    return render_template("standard.html", x=x, used=used, usage_count=usage_count, history=history)



@app.route("/standards/<int:sid>/history")
@admin_required
def standard_history(sid):
    db = get_db()
    x = db.execute("SELECT * FROM reference_standards WHERE standard_id=?", (sid,)).fetchone()
    if not x:
        abort(404)
    history = db.execute(
        """SELECT h.*, u.full_name AS changed_by_name
           FROM reference_standard_history h
           LEFT JOIN users u ON u.user_id=h.changed_by
           WHERE h.standard_id=?
           ORDER BY h.history_id DESC""",
        (sid,)
    ).fetchall()
    return render_template("standard_history.html", x=x, history=history)


@app.route("/standards/<int:sid>/delete", methods=["POST"])
@admin_required
def delete_standard(sid):
    db = get_db()
    x = db.execute("SELECT * FROM reference_standards WHERE standard_id=?", (sid,)).fetchone()
    if not x:
        abort(404)
    if not x["active"]:
        flash(f"Reference standard '{x['code']}' is already inactive.")
        return redirect(url_for("standard", sid=sid))
    try:
        db.execute("UPDATE reference_standards SET active=0 WHERE standard_id=?", (sid,))
        record_standard_history(
            db, sid, "DEACTIVATED", g.user["user_id"],
            "Standard deactivated; record retained for traceability"
        )
        audit_event("STANDARD_DEACTIVATED", "reference_standard", sid,
                    old_value={"active": 1}, new_value={"active": 0})
        db.commit()
        flash(f"Reference standard '{x['code']}' was deactivated and retained for traceability.")
    except sqlite3.Error:
        db.rollback()
        flash("Could not deactivate the reference standard.", "error")
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
            used_count = db.execute(
                "SELECT COUNT(*) FROM calibrations WHERE standard_id=?", (sid,)
            ).fetchone()[0]
            critical_fields = (
                "code", "name", "standard_type", "manufacturer", "serial_number",
                "uncertainty", "traceability", "certificate_no", "calibrated_on", "valid_until"
            )
            changed = any(str(d.get(k, "")) != str(x[k] or "") for k in critical_fields)
            if used_count and changed:
                raise ValueError(
                    "This reference standard has been used by calibration records. "
                    "Its identity, certificate, traceability and validity fields are immutable. "
                    "Create a new standard record for a recalibrated or replaced standard."
                )
            old_active = int(x["active"] or 0)
            new_active = 1 if request.form.get("active") else 0
            changed = any(str(d.get(k, "")) != str(x[k] or "") for k in critical_fields)
            d.update(active=new_active, sid=sid)
            db.execute(
                "UPDATE reference_standards SET code=:code, name=:name, standard_type=:standard_type,"
                " manufacturer=:manufacturer, serial_number=:serial_number, uncertainty=:uncertainty,"
                " traceability=:traceability, certificate_no=:certificate_no,"
                " calibrated_on=:calibrated_on, valid_until=:valid_until, active=:active"
                " WHERE standard_id=:sid", d
            )
            if changed or old_active != new_active:
                event_type = (
                    "ACTIVATED" if old_active == 0 and new_active == 1
                    else "DEACTIVATED" if old_active == 1 and new_active == 0
                    else "UPDATED"
                )
                record_standard_history(
                    db, sid, event_type, g.user["user_id"],
                    "Reference standard record updated"
                )
                audit_event(
                    "STANDARD_UPDATED", "reference_standard", sid,
                    old_value=standard_snapshot(x),
                    new_value=standard_snapshot(
                        db.execute("SELECT * FROM reference_standards WHERE standard_id=?", (sid,)).fetchone()
                    )
                )
            db.commit()
            flash("Reference standard updated.")
            return redirect(url_for("standard", sid=sid))
        except ValueError as e:
            flash(tr("Could not save the standard") + ": " + tr(str(e)), "error")
        except sqlite3.IntegrityError:
            flash(tr("Could not save the standard") + ": " + tr("That code already exists."), "error")
        x = request.form
    return render_template("standard_form.html", x=x, editing=True, sid=sid)


