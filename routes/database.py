"""Route module: database maintenance."""
from app import *


@app.route("/database-maintenance", methods=["GET", "POST"])
@superadmin_required
def database_maintenance():
    db = get_db()

    if request.method == "POST":
        action = request.form.get("action")
        try:
            if action == "backup":
                path = backup_database()
                audit_event("DATABASE_BACKUP", "database", DB,
                            details={"backup": os.path.basename(path)})
                db.commit()
                flash("Database backup created successfully.")
            elif action == "integrity":
                result = db.execute("PRAGMA integrity_check").fetchone()[0]
                audit_event("DATABASE_INTEGRITY_CHECK", "database", DB,
                            details={"result": result})
                db.commit()
                if result == "ok":
                    flash("Database integrity check passed.")
                else:
                    flash("Database integrity check reported problems. Do not run destructive maintenance.", "error")
            elif action == "vacuum":
                db.commit()
                db.execute("VACUUM")
                audit_event("DATABASE_VACUUM", "database", DB)
                db.commit()
                flash("Database maintenance (VACUUM) completed.")
            else:
                flash("Unknown database maintenance action.", "error")
        except Exception as exc:
            db.rollback()
            flash(f"Database maintenance failed: {exc}", "error")
        return redirect(url_for("database_maintenance"))

    db_size = os.path.getsize(DB) if os.path.exists(DB) else 0
    wal_path, shm_path = DB + "-wal", DB + "-shm"

    table_specs = [
        ("stations", "Stations"),
        ("sensors", "Sensors"),
        ("calibrations", "Calibrations"),
        ("calibration_points", "Measurement points"),
        ("calibration_requests", "Calibration requests"),
        ("calibration_work_orders", "Work orders"),
        ("calibration_request_status_history", "Request history"),
        ("calibration_review_history", "Review history"),
        ("reference_standards", "Reference standards"),
        ("calibration_reference_standards", "Calibration reference standards"),
        ("work_order_reference_standards", "Work order reference standards"),
        ("audit_log", "Audit log"),
        ("notifications", "Notifications"),
        ("users", "Users"),
        ("user_roles", "User roles"),
    ]
    counts = [
        (label, table, db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table, label in table_specs
    ]

    index_count = db.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
    ).fetchone()[0]
    integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
    journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]

    backups = []
    backup_dir = os.path.join(BASE_DIR, "backups")
    if os.path.isdir(backup_dir):
        for name in sorted(os.listdir(backup_dir), reverse=True):
            if name.lower().endswith(".db"):
                path = os.path.join(backup_dir, name)
                try:
                    backups.append({
                        "name": name,
                        "size": os.path.getsize(path),
                        "mtime": datetime.fromtimestamp(
                            os.path.getmtime(path)
                        ).strftime("%Y-%m-%d %H:%M:%S"),
                    })
                except OSError:
                    pass

    return render_template(
        "database_maintenance.html",
        db_size=db_size,
        wal_size=os.path.getsize(wal_path) if os.path.exists(wal_path) else 0,
        shm_size=os.path.getsize(shm_path) if os.path.exists(shm_path) else 0,
        journal_mode=journal_mode,
        integrity=integrity,
        index_count=index_count,
        counts=counts,
        backups=backups[:20],
    )
