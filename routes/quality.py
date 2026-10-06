"""Quality management: nonconforming work, impact analysis and CAPA."""
from app import *

QUALITY_ROLES = ("technician", "reviewer", "admin", "superadmin")
QUALITY_STATUSES = ("OPEN", "CONTAINED", "INVESTIGATING", "CAPA", "VERIFICATION", "CLOSED", "CANCELLED")
QUALITY_TRANSITIONS = {
    "OPEN": {"CONTAINED", "CANCELLED"},
    "CONTAINED": {"INVESTIGATING", "CANCELLED"},
    "INVESTIGATING": {"CAPA", "CANCELLED"},
    "CAPA": {"VERIFICATION"},
    "VERIFICATION": {"CLOSED", "CAPA"},
    "CLOSED": set(),
    "CANCELLED": set(),
}

def quality_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not any(user_has_role(r) for r in QUALITY_ROLES):
            abort(403)
        return f(*a, **kw)
    return wrapper

def _nc_number(db, detected_at=None):
    year = (detected_at or date.today().isoformat())[:4]
    row = db.execute("SELECT nc_no FROM quality_nonconformities WHERE nc_no LIKE ? ORDER BY nc_id DESC LIMIT 1",
                     (f"NC-{year}-%",)).fetchone()
    try:
        n = int(row["nc_no"].rsplit("-", 1)[1]) + 1 if row else 1
    except (ValueError, IndexError):
        n = db.execute("SELECT COUNT(*) FROM quality_nonconformities WHERE nc_no LIKE ?", (f"NC-{year}-%",)).fetchone()[0] + 1
    while True:
        candidate = f"NC-{year}-{n:04d}"
        if not db.execute("SELECT 1 FROM quality_nonconformities WHERE nc_no=?", (candidate,)).fetchone():
            return candidate
        n += 1

def _history(db, nc_id, old_status, new_status, comments):
    db.execute("""INSERT INTO quality_history(nc_id,from_status,to_status,changed_by,comments,created_at)
                  VALUES (?,?,?,?,?,?)""",
               (nc_id, old_status, new_status, g.user["user_id"], comments, datetime.now().isoformat(timespec="seconds")))

def _discover_impacts(db, nc_id, cal_id=None, standard_id=None, sensor_id=None, certificate_no=None):
    ids = set()
    if cal_id:
        ids.add(int(cal_id))
    clauses, params = [], []
    if standard_id:
        clauses.append("""EXISTS (SELECT 1 FROM calibration_reference_standards crs
                                  WHERE crs.cal_id=c.cal_id AND crs.standard_id=?)""")
        params.append(int(standard_id))
    if sensor_id:
        clauses.append("c.sensor_id=?"); params.append(sensor_id)
    if certificate_no:
        clauses.append("c.certificate_no=?"); params.append(certificate_no)
    if clauses:
        rows = db.execute("SELECT c.cal_id FROM calibrations c WHERE " + " OR ".join(clauses), params).fetchall()
        ids.update(r["cal_id"] for r in rows)
    for cid in sorted(ids):
        db.execute("""INSERT OR IGNORE INTO quality_impacts
                      (nc_id,cal_id,impact_type,impact_status,created_at)
                      VALUES (?,?,?,'UNASSESSED',?)""",
                   (nc_id, cid, "AUTO-LINKED", datetime.now().isoformat(timespec="seconds")))

def _validate_action(form):
    action_type = form.get("action_type", "CORRECTIVE").strip().upper()
    if action_type not in ("CONTAINMENT", "CORRECTION", "CORRECTIVE", "PREVENTIVE"):
        raise ValueError("Select a valid action type.")
    description = form.get("description", "").strip()
    if not description:
        raise ValueError("Action description is required.")
    due_date = form.get("due_date", "").strip() or None
    if due_date:
        try: date.fromisoformat(due_date)
        except ValueError: raise ValueError("Enter a valid action due date.")
    owner = form.get("owner_id", "").strip()
    if owner and not owner.isdigit():
        raise ValueError("Select a valid action owner.")
    return action_type, description, due_date, int(owner) if owner else None

@app.route("/quality")
@quality_required
def quality_dashboard():
    db = get_db()
    counts = {s: db.execute("SELECT COUNT(*) FROM quality_nonconformities WHERE status=?", (s,)).fetchone()[0] for s in QUALITY_STATUSES}
    overdue = db.execute("""SELECT COUNT(*) FROM quality_actions
                            WHERE completed_at IS NULL AND due_date IS NOT NULL AND due_date < ?""",
                         (date.today().isoformat(),)).fetchone()[0]
    recent = db.execute("""SELECT q.*, u.full_name AS reporter_name
                          FROM quality_nonconformities q LEFT JOIN users u ON u.user_id=q.detected_by
                          ORDER BY q.nc_id DESC LIMIT 12""").fetchall()
    critical = db.execute("""SELECT q.nc_id,q.nc_no,q.title,q.severity,q.status
                             FROM quality_nonconformities q
                             WHERE q.severity IN ('CRITICAL','MAJOR') AND q.status NOT IN ('CLOSED','CANCELLED')
                             ORDER BY CASE q.severity WHEN 'CRITICAL' THEN 1 ELSE 2 END,q.nc_id DESC LIMIT 12""").fetchall()
    return render_template("quality_dashboard.html", counts=counts, overdue=overdue, recent=recent, critical=critical)

@app.route("/quality/nonconformities")
@quality_required
def quality_nonconformities():
    db = get_db()
    status = request.args.get("status", "").strip().upper()
    severity = request.args.get("severity", "").strip().upper()
    q = request.args.get("q", "").strip()
    where, params = [], []
    if status in QUALITY_STATUSES: where.append("q.status=?"); params.append(status)
    if severity in ("LOW","MEDIUM","MAJOR","CRITICAL"): where.append("q.severity=?"); params.append(severity)
    if q:
        like=f"%{q}%"; where.append("(q.nc_no LIKE ? OR q.title LIKE ? OR q.description LIKE ? OR q.certificate_no LIKE ?)"); params += [like]*4
    sql="""SELECT q.*,u.full_name AS reporter_name,cb.full_name AS closed_by_name,
                  (SELECT COUNT(*) FROM quality_impacts qi WHERE qi.nc_id=q.nc_id) AS impact_count,
                  (SELECT COUNT(*) FROM quality_actions qa WHERE qa.nc_id=q.nc_id AND qa.completed_at IS NULL) AS open_actions
           FROM quality_nonconformities q
           LEFT JOIN users u ON u.user_id=q.detected_by
           LEFT JOIN users cb ON cb.user_id=q.closed_by"""
    if where: sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY CASE q.severity WHEN 'CRITICAL' THEN 1 WHEN 'MAJOR' THEN 2 WHEN 'MEDIUM' THEN 3 ELSE 4 END,q.nc_id DESC"
    rows=db.execute(sql,params).fetchall()
    return render_template("quality_nonconformities.html", rows=rows, statuses=QUALITY_STATUSES, status=status, severity=severity, q=q)

@app.route("/quality/nonconformities/new", methods=["GET","POST"])
@quality_required
def new_nonconformity():
    db=get_db()
    calibrations=db.execute("""SELECT c.cal_id,c.certificate_no,c.cal_date,c.result,c.sensor_id,
                                      s.serial_number,st.name AS station
                               FROM calibrations c LEFT JOIN sensors s ON s.sensor_id=c.sensor_id
                               LEFT JOIN stations st ON st.station_id=s.station_id
                               ORDER BY c.cal_id DESC LIMIT 500""").fetchall()
    standards=db.execute("SELECT standard_id,code,name,valid_until,active FROM reference_standards ORDER BY code").fetchall()
    users=db.execute("""SELECT u.user_id,u.full_name FROM users u
                        WHERE u.active=1 AND EXISTS (SELECT 1 FROM user_roles ur WHERE ur.user_id=u.user_id AND ur.role IN ('technician','reviewer','admin','superadmin'))
                        ORDER BY u.full_name""").fetchall()
    if request.method=="POST":
        f=request.form
        try:
            title=f.get("title","").strip(); description=f.get("description","").strip()
            if not title or not description: raise ValueError("Title and description are required.")
            severity=f.get("severity","MEDIUM").upper()
            if severity not in ("LOW","MEDIUM","MAJOR","CRITICAL"): raise ValueError("Select a valid severity.")
            detected_at=f.get("detected_at","").strip() or date.today().isoformat()
            date.fromisoformat(detected_at)
            cal_id=int(f["cal_id"]) if f.get("cal_id","").isdigit() else None
            standard_id=int(f["standard_id"]) if f.get("standard_id","").isdigit() else None
            sensor_id=f.get("sensor_id","").strip() or None
            cert=f.get("certificate_no","").strip() or None
            if cal_id:
                c=db.execute("SELECT sensor_id,certificate_no FROM calibrations WHERE cal_id=?",(cal_id,)).fetchone()
                if not c: raise ValueError("Selected calibration was not found.")
                sensor_id=sensor_id or c["sensor_id"]; cert=cert or c["certificate_no"]
            now=datetime.now().isoformat(timespec="seconds"); nc_no=_nc_number(db,detected_at)
            cur=db.execute("""INSERT INTO quality_nonconformities
                (nc_no,status,severity,title,description,detected_at,detected_by,source_type,source_id,
                 cal_id,certificate_no,standard_id,sensor_id,immediate_action,impact_assessment,
                 root_cause_category,root_cause,customer_notification,containment_status,due_date,created_at,updated_at)
                VALUES (?, 'OPEN', ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (nc_no,severity,title,description,detected_at,g.user["user_id"],
                 f.get("source_type","OTHER").strip().upper(),f.get("source_id","").strip() or None,
                 cal_id,cert,standard_id,sensor_id,f.get("immediate_action","").strip(),
                 f.get("impact_assessment","").strip(),f.get("root_cause_category","").strip(),
                 f.get("root_cause","").strip(),f.get("customer_notification","").strip(),
                 f.get("containment_status","").strip(),f.get("due_date","").strip() or None,now,now))
            nc_id=cur.lastrowid
            _discover_impacts(db,nc_id,cal_id,standard_id,sensor_id,cert)
            _history(db,nc_id,None,"OPEN","Nonconformity registered")
            audit_event("QUALITY_NC_CREATED","quality_nonconformity",nc_id,new_value={"nc_no":nc_no,"severity":severity})
            db.commit()
            flash(f"Nonconformity {nc_no} was registered.")
            return redirect(url_for("quality_nonconformity",nc_id=nc_id))
        except (ValueError,sqlite3.Error) as e:
            db.rollback(); flash(str(e),"error")
    return render_template("quality_form.html",calibrations=calibrations,standards=standards,users=users,today=date.today().isoformat())

@app.route("/quality/nonconformities/<int:nc_id>")
@quality_required
def quality_nonconformity(nc_id):
    db=get_db()
    nc=db.execute("""SELECT q.*,u.full_name AS reporter_name,cb.full_name AS closed_by_name
                     FROM quality_nonconformities q LEFT JOIN users u ON u.user_id=q.detected_by
                     LEFT JOIN users cb ON cb.user_id=q.closed_by WHERE q.nc_id=?""",(nc_id,)).fetchone()
    if not nc: abort(404)
    impacts=db.execute("""SELECT qi.*,c.certificate_no,c.cal_date,c.result,c.lifecycle_status,c.certificate_status,
                                 c.sensor_id,s.serial_number,st.name AS station
                          FROM quality_impacts qi JOIN calibrations c ON c.cal_id=qi.cal_id
                          LEFT JOIN sensors s ON s.sensor_id=c.sensor_id
                          LEFT JOIN stations st ON st.station_id=s.station_id
                          WHERE qi.nc_id=? ORDER BY qi.impact_id""",(nc_id,)).fetchall()
    actions=db.execute("""SELECT qa.*,u.full_name AS owner_name,uc.full_name AS completed_by_name
                          FROM quality_actions qa LEFT JOIN users u ON u.user_id=qa.owner_id
                          LEFT JOIN users uc ON uc.user_id=qa.completed_by
                          WHERE qa.nc_id=? ORDER BY qa.action_id""",(nc_id,)).fetchall()
    history=db.execute("""SELECT h.*,u.full_name AS changed_by_name FROM quality_history h
                          LEFT JOIN users u ON u.user_id=h.changed_by WHERE h.nc_id=? ORDER BY h.history_id""",(nc_id,)).fetchall()
    users=db.execute("""SELECT u.user_id,u.full_name FROM users u WHERE u.active=1 ORDER BY u.full_name""").fetchall()
    candidates=db.execute("""SELECT c.cal_id,c.certificate_no,c.cal_date,c.result,c.sensor_id,s.serial_number
                            FROM calibrations c LEFT JOIN sensors s ON s.sensor_id=c.sensor_id
                            WHERE NOT EXISTS (SELECT 1 FROM quality_impacts qi WHERE qi.nc_id=? AND qi.cal_id=c.cal_id)
                            ORDER BY c.cal_id DESC LIMIT 300""",(nc_id,)).fetchall()
    return render_template("quality_detail.html",nc=nc,impacts=impacts,actions=actions,history=history,users=users,candidates=candidates,transitions=QUALITY_TRANSITIONS.get(nc["status"],set()))

@app.route("/quality/nonconformities/<int:nc_id>/status",methods=["POST"])
@quality_required
def quality_status(nc_id):
    db=get_db(); nc=db.execute("SELECT * FROM quality_nonconformities WHERE nc_id=?",(nc_id,)).fetchone()
    if not nc: abort(404)
    target=request.form.get("status","").strip().upper(); current=nc["status"]
    if target not in QUALITY_TRANSITIONS.get(current,set()): flash(f"Invalid quality workflow transition: {current} → {target}.","error"); return redirect(url_for("quality_nonconformity",nc_id=nc_id))
    if target=="CLOSED" and not (user_has_role("admin") or user_has_role("superadmin")):
        abort(403)
    if target=="VERIFICATION":
        open_actions=db.execute("SELECT COUNT(*) FROM quality_actions WHERE nc_id=? AND completed_at IS NULL AND action_type IN ('CORRECTION','CORRECTIVE','PREVENTIVE')",(nc_id,)).fetchone()[0]
        unverified=db.execute("SELECT COUNT(*) FROM quality_actions WHERE nc_id=? AND action_type IN ('CORRECTION','CORRECTIVE','PREVENTIVE') AND (completed_at IS NULL OR verification_status='PENDING')",(nc_id,)).fetchone()[0]
        unassessed=db.execute("SELECT COUNT(*) FROM quality_impacts WHERE nc_id=? AND impact_status!='ASSESSED'",(nc_id,)).fetchone()[0]
        if open_actions or unverified or unassessed:
            flash("Complete and verify corrective/preventive actions and assess every impacted calibration before verification.","error")
            return redirect(url_for("quality_nonconformity",nc_id=nc_id))
    if target=="CLOSED":
        pending=db.execute("SELECT COUNT(*) FROM quality_actions WHERE nc_id=? AND (completed_at IS NULL OR verification_status='PENDING')",(nc_id,)).fetchone()[0]
        unassessed=db.execute("SELECT COUNT(*) FROM quality_impacts WHERE nc_id=? AND impact_status!='ASSESSED'",(nc_id,)).fetchone()[0]
        if pending or unassessed:
            flash("A quality event cannot be closed until actions are verified and every impact is assessed.","error")
            return redirect(url_for("quality_nonconformity",nc_id=nc_id))
    comments=request.form.get("comments","").strip()
    now=datetime.now().isoformat(timespec="seconds")
    with db:
        db.execute("UPDATE quality_nonconformities SET status=?,updated_at=?,closed_at=CASE WHEN ?='CLOSED' THEN ? ELSE closed_at END,closed_by=CASE WHEN ?='CLOSED' THEN ? ELSE closed_by END WHERE nc_id=?",
                   (target,now,target,now,target,g.user["user_id"],nc_id))
        _history(db,nc_id,current,target,comments)
        audit_event("QUALITY_NC_STATUS","quality_nonconformity",nc_id,old_value={"status":current},new_value={"status":target},details={"comments":comments})
    flash(f"Nonconformity moved to {target}.")
    return redirect(url_for("quality_nonconformity",nc_id=nc_id))

@app.route("/quality/nonconformities/<int:nc_id>/action",methods=["POST"])
@quality_required
def quality_action(nc_id):
    db=get_db(); nc=db.execute("SELECT * FROM quality_nonconformities WHERE nc_id=?",(nc_id,)).fetchone()
    if not nc: abort(404)
    if nc["status"] in ("CLOSED","CANCELLED"): abort(400)
    try:
        action_type,description,due_date,owner=_validate_action(request.form)
        now=datetime.now().isoformat(timespec="seconds")
        cur=db.execute("""INSERT INTO quality_actions(nc_id,action_type,description,owner_id,due_date,created_by,created_at)
                          VALUES (?,?,?,?,?,?,?)""",(nc_id,action_type,description,owner,due_date,g.user["user_id"],now))
        audit_event("QUALITY_ACTION_CREATED","quality_action",cur.lastrowid,new_value={"nc_id":nc_id,"action_type":action_type})
        db.commit(); flash("Quality action added.")
    except ValueError as e: db.rollback(); flash(str(e),"error")
    return redirect(url_for("quality_nonconformity",nc_id=nc_id))

@app.route("/quality/actions/<int:action_id>/complete",methods=["POST"])
@quality_required
def quality_action_complete(action_id):
    db=get_db(); action=db.execute("SELECT * FROM quality_actions WHERE action_id=?",(action_id,)).fetchone()
    if not action: abort(404)
    if action["completed_at"]: return redirect(url_for("quality_nonconformity",nc_id=action["nc_id"]))
    notes=request.form.get("verification_notes","").strip()
    now=datetime.now().isoformat(timespec="seconds")
    with db:
        db.execute("""UPDATE quality_actions SET completed_at=?,completed_by=?,verification_status='PENDING',verification_notes=?
                      WHERE action_id=? AND completed_at IS NULL""",(now,g.user["user_id"],notes,action_id))
        audit_event("QUALITY_ACTION_COMPLETED","quality_action",action_id,details={"nc_id":action["nc_id"]})
    return redirect(url_for("quality_nonconformity",nc_id=action["nc_id"]))

@app.route("/quality/actions/<int:action_id>/verify",methods=["POST"])
@reviewer_required
def quality_action_verify(action_id):
    db=get_db()
    action=db.execute("SELECT * FROM quality_actions WHERE action_id=?",(action_id,)).fetchone()
    if not action: abort(404)
    if not action["completed_at"]:
        flash("Complete the action before verifying its effectiveness.","error")
        return redirect(url_for("quality_nonconformity",nc_id=action["nc_id"]))
    status=request.form.get("status","").strip().upper()
    if status not in ("EFFECTIVE","INEFFECTIVE","NOT_REQUIRED"):
        abort(400)
    notes=request.form.get("notes","").strip()
    now=datetime.now().isoformat(timespec="seconds")
    with db:
        db.execute("UPDATE quality_actions SET verification_status=?,verification_notes=? WHERE action_id=?",
                   (status,notes,action_id))
        db.execute("""INSERT INTO quality_action_verifications(action_id,verified_by,verified_at,status,notes)
                      VALUES (?,?,?,?,?)""",(action_id,g.user["user_id"],now,status,notes))
        audit_event("QUALITY_ACTION_VERIFIED","quality_action",action_id,
                    new_value={"verification_status":status},details={"notes":notes,"nc_id":action["nc_id"]})
    flash("Action verification recorded.")
    return redirect(url_for("quality_nonconformity",nc_id=action["nc_id"]))

@app.route("/quality/nonconformities/<int:nc_id>/impact",methods=["POST"])
@quality_required
def quality_impact(nc_id):
    db=get_db(); nc=db.execute("SELECT nc_id,status FROM quality_nonconformities WHERE nc_id=?",(nc_id,)).fetchone()
    if not nc: abort(404)
    cal_id=request.form.get("cal_id","").strip()
    if not cal_id.isdigit(): flash("Select a calibration to assess.","error"); return redirect(url_for("quality_nonconformity",nc_id=nc_id))
    if not db.execute("SELECT 1 FROM calibrations WHERE cal_id=?",(int(cal_id),)).fetchone(): abort(404)
    disposition=request.form.get("disposition","UNASSESSED").strip().upper()
    if disposition not in ("UNASSESSED","NOT_AFFECTED","HOLD","REVIEW","RECALIBRATE","WITHDRAW_CERTIFICATE"): disposition="UNASSESSED"
    notes=request.form.get("notes","").strip()
    with db:
        db.execute("""INSERT INTO quality_impacts(nc_id,cal_id,impact_type,impact_status,disposition,notes,reviewed_by,reviewed_at,created_at)
                      VALUES (?,?, 'MANUAL', ?,?,?,?, ?,?) ON CONFLICT(nc_id,cal_id) DO UPDATE SET
                      impact_status=excluded.impact_status,disposition=excluded.disposition,notes=excluded.notes,
                      reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at""",
                   (nc_id,int(cal_id),"ASSESSED",disposition,notes,g.user["user_id"],datetime.now().isoformat(timespec="seconds"),datetime.now().isoformat(timespec="seconds")))
        audit_event("QUALITY_IMPACT_ASSESSED","quality_impact",f"{nc_id}:{cal_id}",new_value={"disposition":disposition,"notes":notes})
        db.commit()
    if disposition=="WITHDRAW_CERTIFICATE":
        c=db.execute("SELECT certificate_no FROM calibrations WHERE cal_id=?",(int(cal_id),)).fetchone()
        if c and c["certificate_no"]:
            # Use the existing controlled certificate history mechanism.
            with db:
                row=db.execute("SELECT certificate_status,certificate_fingerprint FROM calibrations WHERE cal_id=?",(int(cal_id),)).fetchone()
                if row and row["certificate_status"]=="ACTIVE":
                    reason=f"Withdrawn under nonconformity {nc['nc_id']}."
                    db.execute("UPDATE calibrations SET certificate_status='WITHDRAWN',updated_at=? WHERE cal_id=?",(datetime.now().isoformat(timespec="seconds"),int(cal_id)))
                    db.execute("""INSERT INTO certificate_history(cal_id,certificate_no,event_type,fingerprint,reason,changed_by,changed_at)
                                  VALUES (?,?,'WITHDRAWN',?,?,?,?,?)""",(int(cal_id),c["certificate_no"],row["certificate_fingerprint"],reason,g.user["user_id"],datetime.now().isoformat(timespec="seconds")))
                    audit_event("CERTIFICATE_WITHDRAWN_FOR_NC","certificate",c["certificate_no"],details={"nc_id":nc_id,"cal_id":int(cal_id),"reason":reason})
    return redirect(url_for("quality_nonconformity",nc_id=nc_id))

@app.route("/quality/nonconformities/<int:nc_id>/update",methods=["POST"])
@quality_required
def quality_update(nc_id):
    db=get_db(); nc=db.execute("SELECT * FROM quality_nonconformities WHERE nc_id=?",(nc_id,)).fetchone()
    if not nc: abort(404)
    allowed={"title","description","immediate_action","impact_assessment","root_cause_category","root_cause","customer_notification","containment_status","due_date"}
    vals={k:request.form.get(k,"").strip() for k in allowed}
    if not vals["title"] or not vals["description"]: flash("Title and description are required.","error"); return redirect(url_for("quality_nonconformity",nc_id=nc_id))
    now=datetime.now().isoformat(timespec="seconds")
    with db:
        db.execute("""UPDATE quality_nonconformities SET title=?,description=?,immediate_action=?,impact_assessment=?,
                      root_cause_category=?,root_cause=?,customer_notification=?,containment_status=?,due_date=?,updated_at=?
                      WHERE nc_id=?""",
                   (*[vals[k] or None for k in ("title","description","immediate_action","impact_assessment","root_cause_category","root_cause","customer_notification","containment_status","due_date")],now,nc_id))
        audit_event("QUALITY_NC_UPDATED","quality_nonconformity",nc_id,details={"fields":list(vals)})
    flash("Nonconformity record updated.")
    return redirect(url_for("quality_nonconformity",nc_id=nc_id))
