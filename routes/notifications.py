"""Route module: notifications."""
from app import *

@app.route("/due")
def due():
    days = request.args.get("days", 30, type=int)
    limit = (date.today() + timedelta(days=days)).isoformat()
    rows = [r for r in get_db().execute(LATEST + " ORDER BY s.sensor_id")
            if r["next_due"] is None or r["next_due"] <= limit or r["result"] == "FAIL"]
    return render_template("due.html", rows=rows, days=days)


def build_operational_alerts(db):
    """Build current role-specific operational alerts."""
    today = date.today()
    horizon = today + timedelta(days=30)
    items = []

    def add_alert(kind, severity, title, description, endpoint=None, alert_key=None, **kwargs):
        items.append({
            "kind": kind, "severity": severity, "title": title,
            "description": description, "endpoint": endpoint, "alert_key": alert_key, **kwargs
        })

    if not (user_has_role("general_user") and not any(user_has_role(role) for role in ("technician", "reviewer", "admin", "superadmin"))):
        sensor_rows = db.execute(LATEST + " ORDER BY s.sensor_id").fetchall()
    else:
        sensor_rows = []

    for r in sensor_rows:
        if r["result"] == "FAIL":
            add_alert("sensor", "critical", "Failed sensor calibration",
                      f"{r['sensor_id']} has a failed calibration result.",
                      "sensor", sensor_id=r["sensor_id"])
        elif r["next_due"]:
            try:
                due_date = date.fromisoformat(r["next_due"][:10])
                if due_date < today:
                    add_alert("sensor", "critical", "Sensor calibration overdue",
                              f"{r['sensor_id']} was due on {r['next_due']}.",
                              "sensor", sensor_id=r["sensor_id"])
                elif due_date <= horizon:
                    days_left = (due_date - today).days
                    add_alert("sensor", "warning", "Sensor calibration due soon",
                              f"{r['sensor_id']} is due in {days_left} day(s).",
                              "sensor", sensor_id=r["sensor_id"])
            except ValueError:
                pass

    review_rows = db.execute("""SELECT rh.review_id, rh.submitted_at, r.request_no,
                                       c.certificate_no
                                FROM calibration_review_history rh
                                JOIN calibrations c ON c.cal_id=rh.cal_id
                                JOIN calibration_requests r ON r.request_id=c.request_id
                                WHERE rh.decision='PENDING'
                                ORDER BY rh.submitted_at DESC""").fetchall()
    if user_has_role("reviewer") or user_has_role("admin") or user_has_role("superadmin"):
        for r in review_rows:
            add_alert("review", "critical", "Calibration awaiting review",
                      f"{r['request_no']} / {r['certificate_no']} is waiting for review.",
                      "calibration_reviews", alert_key="review|" + str(r["review_id"]), review_id=r["review_id"])

    if user_has_role("general_user") and not any(user_has_role(role) for role in ("technician", "reviewer", "admin", "superadmin")):
        request_rows = db.execute(
            """SELECT request_id, request_no, status, updated_at
               FROM calibration_requests
               WHERE created_by=?
               ORDER BY updated_at DESC LIMIT 20""",
            (g.user["full_name"],)
        ).fetchall()
        for r in request_rows:
            if r["status"] == "COMPLETED":
                severity, title = "info", "Calibration request completed"
            elif r["status"] == "CANCELLED":
                severity, title = "critical", "Calibration request cancelled"
            else:
                severity, title = "info", "Calibration request status updated"
            add_alert(
                "request", severity, title,
                f"{r['request_no']} is now {r['status']}.",
                "calibration_request",
                alert_key="request-status|" + str(r["request_id"]) + "|" + r["status"],
                request_id=r["request_id"]
            )
    work_rows = db.execute("""SELECT w.work_order_id, w.work_order_no, w.status,
                                     r.request_no, w.assigned_technician_id, u.full_name
                              FROM calibration_work_orders w
                              JOIN calibration_requests r ON r.request_id=w.request_id
                              JOIN users u ON u.user_id=w.assigned_technician_id
                              WHERE w.status IN ('ASSIGNED','IN PROGRESS')
                              ORDER BY w.assigned_at ASC, w.work_order_id ASC""").fetchall()
    for r in work_rows:
        if user_has_role("admin") or user_has_role("superadmin"):
            title = "Work order awaiting technician" if r["status"] == "ASSIGNED" else "Calibration in progress"
            sev = "warning" if r["status"] == "ASSIGNED" else "info"
            add_alert("work_order", sev, title,
                      f"{r['work_order_no']} ({r['request_no']}) is assigned to {r['full_name']}."
                      if r["status"] == "ASSIGNED" else
                      f"{r['work_order_no']} ({r['request_no']}) is currently in progress.",
                      "work_order_detail", alert_key="work-order|" + str(r["work_order_id"]) + "|" + r["status"], work_order_id=r["work_order_id"])
        elif r["assigned_technician_id"] == g.user["user_id"]:
            title = "Assigned work order" if r["status"] == "ASSIGNED" else "Calibration in progress"
            sev = "warning" if r["status"] == "ASSIGNED" else "info"
            add_alert("work_order", sev, title,
                      f"{r['work_order_no']} ({r['request_no']}) is ready to start."
                      if r["status"] == "ASSIGNED" else
                      f"{r['work_order_no']} ({r['request_no']}) remains in progress.",
                      "work_order_detail", alert_key="work-order|" + str(r["work_order_id"]) + "|" + r["status"], work_order_id=r["work_order_id"])

    if user_has_role("admin") or user_has_role("superadmin"):
        stalled = db.execute("""SELECT request_id, request_no, client_name
                                FROM calibration_requests
                                WHERE status='REVIEWED'
                                  AND NOT EXISTS (
                                      SELECT 1 FROM calibration_work_orders w
                                      WHERE w.request_id=calibration_requests.request_id
                                        AND w.status IN ('ASSIGNED','IN PROGRESS','AWAITING REVIEW')
                                  )
                                ORDER BY received_date ASC""").fetchall()
        for r in stalled:
            add_alert("request", "warning", "Reviewed request not assigned",
                      f"{r['request_no']} for {r['client_name']} is ready for technician assignment.",
                      "calibration_request", alert_key="request-assignment|" + str(r["request_id"]), request_id=r["request_id"])

    return items


def sync_notifications(db, items):
    """Persist newly observed role-specific alerts without duplicating them."""
    uid = g.user["user_id"]
    now = datetime.now().isoformat(timespec="seconds")
    for a in items:
        entity = next((a.get(k) for k in ("review_id", "work_order_id", "request_id", "sensor_id")
                       if a.get(k) is not None), "")
        key = a.get("alert_key") or "|".join([a["kind"], a["title"], str(entity)])
        db.execute("""INSERT OR IGNORE INTO notifications
                      (user_id, alert_key, kind, severity, title, description,
                       endpoint, entity_id, created_at)
                      VALUES (?,?,?,?,?,?,?,?,?)""",
                   (uid, key, a["kind"], a["severity"], a["title"], a["description"],
                    a.get("endpoint"), str(entity) if entity != "" else None, now))
    db.commit()


@app.route("/alerts")
def alerts():
    """Central operational alert center and notification history."""
    db = get_db()
    items = build_operational_alerts(db)
    sync_notifications(db, items)
    rows = db.execute("""SELECT * FROM notifications
                         WHERE user_id=? ORDER BY notification_id DESC""",
                      (g.user["user_id"],)).fetchall()
    unread = [r for r in rows if r["read_at"] is None]
    view = request.args.get("view", "all").strip().lower()
    if view == "unread":
        visible_notifications = unread
    elif view == "tasks":
        visible_notifications = [r for r in unread if r["kind"] in ("review", "work_order", "request")]
    else:
        visible_notifications = rows
    task_count = sum(1 for r in unread if r["kind"] in ("review", "work_order", "request"))
    return render_template(
        "alerts.html", alerts=items, notifications=visible_notifications,
        unread=unread, view=view, task_count=task_count
    )


@app.route("/notifications/read/<int:notification_id>", methods=["POST"])
def mark_notification_read(notification_id):
    db = get_db()
    row = db.execute("SELECT notification_id FROM notifications WHERE notification_id=? AND user_id=?",
                     (notification_id, g.user["user_id"])).fetchone()
    if not row:
        abort(404)
    db.execute("UPDATE notifications SET read_at=? WHERE notification_id=?",
               (datetime.now().isoformat(timespec="seconds"), notification_id))
    db.commit()
    return redirect(url_for("alerts"))


@app.route("/notifications/read-all", methods=["POST"])
def mark_all_notifications_read():
    db = get_db()
    db.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL",
               (datetime.now().isoformat(timespec="seconds"), g.user["user_id"]))
    db.commit()
    return redirect(url_for("alerts"))



# ------------------------------ reference standards ------------------------------
STD_FIELDS = ("code", "name", "standard_type", "manufacturer", "serial_number", "uncertainty",
              "traceability", "certificate_no", "calibrated_on", "valid_until")


