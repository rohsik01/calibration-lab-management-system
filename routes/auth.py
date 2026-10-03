"""Route module: auth."""
from app import *
from app import _complete_login, _qr_data_uri, _recovery_codes

@app.route("/lang/<code>")
def set_lang(code):
    if code not in LANGS:
        abort(404)
    resp = redirect(safe_next(request.args.get("next")))
    resp.set_cookie("lang", code, max_age=365 * 24 * 3600, samesite="Lax")
    return resp


@app.route("/setup", methods=["GET", "POST"])
def setup():
    """First run only: create the first administrator."""
    db = get_db()
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0:
        return redirect(url_for("login"))
    if request.method == "POST":
        f = request.form
        err = check_new_password(f["password"], f["password2"])
        if err or not f["username"].strip():
            flash(err or "Username is required.")
        else:
            cur = db.execute("INSERT INTO users(username, full_name, password_hash, role) "
                            "VALUES (?,?,?, 'superadmin')",
                            (f["username"].strip(), f["full_name"].strip() or f["username"].strip(),
                             generate_password_hash(f["password"])))
            db.execute("INSERT INTO user_roles(user_id, role) VALUES (?, 'superadmin')", (cur.lastrowid,))
            db.commit()
            flash("Superadministrator created. Please sign in.")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("index"))
    if request.method == "POST":
        name = request.form["username"].strip().lower()
        key = (name, request.remote_addr)
        if FAILS.get(key, (0, 0))[1] > time.time():
            flash("Too many failed attempts. Try again in 5 minutes.")
        else:
            u = get_db().execute("SELECT * FROM users WHERE username=? AND active=1",
                                 (name,)).fetchone()
            if u and check_password_hash(u["password_hash"], request.form["password"]):
                FAILS.pop(key, None)
                if u["two_factor_enabled"]:
                    session.clear()
                    session["pending_2fa_user_id"] = u["user_id"]
                    session["pending_2fa_next"] = safe_next(request.args.get("next"))
                    return redirect(url_for("two_factor"))
                return _complete_login(u["user_id"], request.args.get("next"))
            count = FAILS.get(key, (0, 0))[0] + 1
            FAILS[key] = (0, time.time() + 300) if count >= 5 else (count, 0)
            flash("Invalid username or password.")
    return render_template("login.html")


@app.route("/2fa", methods=["GET", "POST"])
def two_factor():
    """Verify a TOTP code or a one-time recovery code after password authentication."""
    if g.user:
        return redirect(url_for("index"))
    uid = session.get("pending_2fa_user_id")
    if not uid:
        return redirect(url_for("login"))
    u = get_db().execute("SELECT * FROM users WHERE user_id=? AND active=1", (uid,)).fetchone()
    if not u or not u["two_factor_enabled"] or not u["totp_secret"]:
        session.clear()
        return redirect(url_for("login"))

    if request.method == "POST":
        code = re.sub(r"\s+", "", request.form.get("code", "")).upper()
        ok = bool(re.fullmatch(r"\d{6}", code) and
                  pyotp.TOTP(u["totp_secret"]).verify(code, valid_window=1))
        recovery_used = False
        if not ok and u["recovery_codes"]:
            stored = json.loads(u["recovery_codes"])
            for i, hashed in enumerate(stored):
                if check_password_hash(hashed, code):
                    stored.pop(i)
                    get_db().execute("UPDATE users SET recovery_codes=? WHERE user_id=?",
                                     (json.dumps(stored), uid))
                    get_db().commit()
                    ok = recovery_used = True
                    break
        if ok:
            next_target = session.get("pending_2fa_next")
            return _complete_login(uid, next_target)
        flash("Invalid verification code. Please try again.", "error")
    remaining = len(json.loads(u["recovery_codes"] or "[]"))
    return render_template("two_factor.html", recovery_remaining=remaining)


@app.route("/account/2fa/setup", methods=["GET", "POST"])
def setup_2fa():
    if g.user["two_factor_enabled"]:
        return redirect(url_for("account"))
    secret = session.get("pending_totp_secret")
    if not secret:
        secret = pyotp.random_base32()
        session["pending_totp_secret"] = secret
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=g.user["username"], issuer_name="Calibration Lab Management System")
    qr = _qr_data_uri(uri)
    if request.method == "POST":
        code = re.sub(r"\s+", "", request.form.get("code", ""))
        if re.fullmatch(r"\d{6}", code) and totp.verify(code, valid_window=1):
            plain, stored = _recovery_codes()
            db = get_db()
            db.execute("UPDATE users SET two_factor_enabled=1, totp_secret=?, recovery_codes=? WHERE user_id=?",
                       (secret, stored, g.user["user_id"]))
            db.commit()
            session.pop("pending_totp_secret", None)
            session["show_recovery_codes"] = plain
            return redirect(url_for("account"))
        flash("Invalid verification code. Scan the QR code and enter the current 6-digit code.", "error")
    return render_template("two_factor_setup.html", qr=qr, secret=secret)


@app.route("/account/2fa/disable", methods=["POST"])
def disable_2fa():
    if not g.user["two_factor_enabled"]:
        return redirect(url_for("account"))
    password = request.form.get("current_password", "")
    code = re.sub(r"\s+", "", request.form.get("code", "")).upper()
    if not check_password_hash(g.user["password_hash"], password):
        flash("Current password is incorrect.", "error")
        return redirect(url_for("account"))
    valid = bool(re.fullmatch(r"\d{6}", code) and
                 pyotp.TOTP(g.user["totp_secret"]).verify(code, valid_window=1))
    if not valid:
        flash("Enter a valid authenticator code to disable two-factor authentication.", "error")
        return redirect(url_for("account"))
    db = get_db()
    db.execute("UPDATE users SET two_factor_enabled=0, totp_secret=NULL, recovery_codes=NULL WHERE user_id=?",
               (g.user["user_id"],))
    db.commit()
    flash("Two-factor authentication has been disabled.")
    return redirect(url_for("account"))


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been signed out.")
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account():
    if request.method == "POST":
        f = request.form
        db = get_db()
        if not check_password_hash(g.user["password_hash"], f["current"]):
            flash("Current password is incorrect.")
        elif (err := check_new_password(f["password"], f["password2"])):
            flash(err)
        else:
            db.execute("UPDATE users SET password_hash=? WHERE user_id=?",
                       (generate_password_hash(f["password"]), g.user["user_id"]))
            db.commit()
            flash("Password changed.")
            return redirect(url_for("index"))
    recovery_codes = session.pop("show_recovery_codes", None)
    return render_template("account.html", recovery_codes=recovery_codes)


