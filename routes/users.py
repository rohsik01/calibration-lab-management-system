"""Route module: users."""
from app import *


def is_last_active_superadmin(db, user):
    """Return True when deactivating this user would remove the last active superadmin."""
    if user["role"] != "superadmin" or not user["active"]:
        return False
    active_count = db.execute(
        "SELECT COUNT(*) FROM users WHERE role='superadmin' AND active=1"
    ).fetchone()[0]
    return active_count <= 1

@app.route("/users", methods=["GET", "POST"])
@superadmin_required
def users():
    db = get_db()
    if request.method == "POST":
        f = request.form
        err = check_new_password(f["password"], f["password"])
        if err or not f["username"].strip() or f["role"] not in ("superadmin", "admin", "technician", "general_user"):
            flash(err or "Username and a valid role are required.")
        else:
            try:
                db.execute("INSERT INTO users(username, full_name, password_hash, role) "
                           "VALUES (?,?,?,?)",
                           (f["username"].strip(), f["full_name"].strip() or f["username"].strip(),
                            generate_password_hash(f["password"]), f["role"]))
                db.commit()
                flash("User created.")
            except sqlite3.IntegrityError:
                flash("That username already exists.")
        return redirect(url_for("users"))
    return render_template("users.html",
                           rows=db.execute("SELECT * FROM users ORDER BY username").fetchall())



@app.route("/users/<int:uid>/delete", methods=["POST"])
@superadmin_required
def delete_user(uid):
    if uid == g.user["user_id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("users"))
    db = get_db()
    u = db.execute("SELECT user_id, username, role, active FROM users WHERE user_id=?", (uid,)).fetchone()
    if not u:
        abort(404)
    if u["role"] == "superadmin" and u["active"]:
        active_admins = db.execute("SELECT COUNT(*) FROM users WHERE role='superadmin' AND active=1").fetchone()[0]
        if active_admins <= 1:
            flash("The last active superadministrator cannot be deleted.", "error")
            return redirect(url_for("users"))
    try:
        with db:
            db.execute("DELETE FROM users WHERE user_id=?", (uid,))
        flash(f"User '{u['username']}' was deleted.")
    except sqlite3.Error:
        flash("Could not delete the user.", "error")
    return redirect(url_for("users"))

@app.route("/users/<int:uid>/toggle", methods=["POST"])
@superadmin_required
def toggle_user(uid):
    if uid == g.user["user_id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("users"))

    db = get_db()
    user = db.execute(
        "SELECT user_id, username, role, active FROM users WHERE user_id=?", (uid,)
    ).fetchone()
    if not user:
        abort(404)

    if is_last_active_superadmin(db, user):
        flash("The last active superadministrator cannot be deactivated.", "error")
        return redirect(url_for("users"))

    new_active = 0 if user["active"] else 1
    with db:
        db.execute("UPDATE users SET active=? WHERE user_id=?", (new_active, uid))
        audit_event(
            "USER_ACTIVATED" if new_active else "USER_DEACTIVATED",
            "user",
            uid,
            old_value={"active": bool(user["active"])},
            new_value={"active": bool(new_active)},
            details={"username": user["username"], "role": user["role"]},
        )
    flash("User activated." if new_active else "User deactivated.")
    return redirect(url_for("users"))


@app.route("/users/<int:uid>/password", methods=["POST"])
@superadmin_required
def reset_password(uid):
    pw = request.form["password"]
    if (err := check_new_password(pw, pw)):
        flash(err)
    else:
        db = get_db()
        db.execute("UPDATE users SET password_hash=? WHERE user_id=?",
                   (generate_password_hash(pw), uid))
        db.commit()
        flash("Password reset.")
    return redirect(url_for("users"))



# ------------------------------ Nepali calendar ------------------------------

