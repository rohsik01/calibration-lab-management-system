"""Route module: users."""
from app import *


ROLE_ORDER = ("superadmin", "admin", "reviewer", "technician", "general_user")
ROLE_LABELS = {
    "superadmin": "Superadministrator",
    "admin": "Administrator",
    "reviewer": "Reviewer",
    "technician": "Technician",
    "general_user": "General user",
}


def parse_roles(form):
    roles = [role for role in form.getlist("roles") if role in ROLE_ORDER]
    return [role for role in ROLE_ORDER if role in roles]


def is_last_active_superadmin(db, user):
    """Return True when removing this user's active superadmin role would leave none."""
    if not db.execute(
        "SELECT 1 FROM user_roles WHERE user_id=? AND role='superadmin'", (user["user_id"],)
    ).fetchone() or not user["active"]:
        return False
    active_count = db.execute(
        """SELECT COUNT(*) FROM user_roles ur
           JOIN users u ON u.user_id=ur.user_id
           WHERE ur.role='superadmin' AND u.active=1"""
    ).fetchone()[0]
    return active_count <= 1


@app.route("/users", methods=["GET", "POST"])
@superadmin_required
def users():
    db = get_db()
    if request.method == "POST":
        f = request.form
        err = check_new_password(f.get("password", ""), f.get("password", ""))
        roles = parse_roles(f)
        if err or not f.get("username", "").strip() or not roles:
            flash(err or "Username and at least one valid role are required.", "error")
        else:
            try:
                cur = db.execute(
                    "INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)",
                    (f["username"].strip(), f.get("full_name", "").strip() or f["username"].strip(),
                     generate_password_hash(f["password"]), roles[0])
                )
                uid = cur.lastrowid
                db.executemany(
                    "INSERT INTO user_roles(user_id, role) VALUES (?,?)",
                    [(uid, role) for role in roles]
                )
                audit_event("USER_CREATED", "user", uid,
                            new_value={"username": f["username"].strip(), "roles": roles})
                db.commit()
                flash("User created.")
            except sqlite3.IntegrityError:
                db.rollback()
                flash("That username already exists.", "error")
        return redirect(url_for("users"))

    rows = db.execute("SELECT * FROM users ORDER BY username").fetchall()
    rows = [dict(row, roles=user_roles_for(row["user_id"])) for row in rows]
    return render_template("users.html", rows=rows, role_order=ROLE_ORDER, role_labels=ROLE_LABELS)


@app.route("/users/<int:uid>/roles", methods=["POST"])
@superadmin_required
def update_user_roles(uid):
    db = get_db()
    user = db.execute(
        "SELECT user_id, username, full_name, role, active FROM users WHERE user_id=?", (uid,)
    ).fetchone()
    if not user:
        abort(404)
    roles = parse_roles(request.form)
    if not roles:
        flash("A user must have at least one role.", "error")
        return redirect(url_for("users"))

    old_roles = user_roles_for(uid)
    if uid == g.user["user_id"] and "superadmin" not in roles:
        flash("You cannot remove the superadministrator role from your own account.", "error")
        return redirect(url_for("users"))

    if "superadmin" in old_roles and "superadmin" not in roles and user["active"]:
        active_superadmins = db.execute(
            """SELECT COUNT(*) FROM user_roles ur
               JOIN users u ON u.user_id=ur.user_id
               WHERE ur.role='superadmin' AND u.active=1"""
        ).fetchone()[0]
        if active_superadmins <= 1:
            flash("The last active superadministrator must retain the superadministrator role.", "error")
            return redirect(url_for("users"))

    try:
        with db:
            db.execute("DELETE FROM user_roles WHERE user_id=?", (uid,))
            db.executemany(
                "INSERT INTO user_roles(user_id, role) VALUES (?,?)",
                [(uid, role) for role in roles]
            )
            db.execute("UPDATE users SET role=? WHERE user_id=?", (roles[0], uid))
            audit_event(
                "USER_ROLES_UPDATED", "user", uid,
                old_value={"roles": old_roles},
                new_value={"roles": roles},
            )
    except sqlite3.IntegrityError:
        db.rollback()
        flash("Could not update the user's roles.", "error")
    else:
        flash("User roles updated.")
    return redirect(url_for("users"))


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
    if is_last_active_superadmin(db, u):
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
            details={"username": user["username"], "roles": user_roles_for(uid)},
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
