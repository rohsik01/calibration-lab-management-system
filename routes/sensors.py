"""Route module: sensors."""
from app import *

def _sensor_id_prefix(sensor_type):
    """Return the standard two-letter Sensor ID prefix for a sensor type."""
    value = re.sub(r"[^A-Za-z]", "", (sensor_type or "").strip()).lower()
    prefixes = {
        "temperature": "TS",
        "pressure": "PS",
        "humidity": "HS",
        "relativehumidity": "RHS",
        "rainfall": "RS",
        "precipitation": "RS",
        "wind": "WS",
        "windspeed": "WS",
        "winddirection": "WD",
        "solar": "SS",
        "radiation": "RS",
        "visibility": "VS",
        "waterlevel": "WL",
    }
    if value in prefixes:
        return prefixes[value]
    letters = re.findall(r"[A-Za-z]+", (sensor_type or "").upper())
    initials = "".join(word[0] for word in letters)
    return (initials[:3] or "SN").upper()


def _station_sensor_id(db, station_id, sensor_type):
    """Generate IDs such as PS_Tarahara_1001, incrementing per station and type."""
    station = db.execute("SELECT name FROM stations WHERE station_id=?", (station_id,)).fetchone()
    if not station:
        raise ValueError("Selected station does not exist.")
    prefix = _sensor_id_prefix(sensor_type)
    station_label = re.sub(r"[^A-Za-z0-9]+", "_", station["name"].strip()).strip("_")
    if not station_label:
        station_label = "Station"
    pattern = f"{prefix}_{station_label}_%"
    rows = db.execute("SELECT sensor_id FROM sensors WHERE sensor_id LIKE ?",
                      (pattern,)).fetchall()
    numbers = []
    for row in rows:
        match = re.fullmatch(rf"{re.escape(prefix)}_{re.escape(station_label)}_(\\d+)", row["sensor_id"])
        if match:
            numbers.append(int(match.group(1)))
    next_number = max(numbers, default=1000) + 1
    sensor_id = f"{prefix}_{station_label}_{next_number:04d}"
    while db.execute("SELECT 1 FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone():
        next_number += 1
        sensor_id = f"{prefix}_{station_label}_{next_number:04d}"
    return sensor_id


@app.route("/sensors/new", methods=["GET", "POST"])
def new_sensor():
    db = get_db()
    stations_ = db.execute("SELECT * FROM stations ORDER BY name").fetchall()
    if request.method == "POST":
        f = request.form
        try:
            station_id = int(f["station_id"])
            sensor_type = f["sensor_type"].strip()
            if not sensor_type:
                raise ValueError("Sensor type is required.")
            sensor_id = _station_sensor_id(db, station_id, sensor_type)
            db.execute("INSERT INTO sensors VALUES (?,?,?,?,?,?,?,?)",
                       (sensor_id, station_id, sensor_type, f["manufacturer"].strip(),
                        f["serial_number"].strip(), int(f["interval_days"]),
                        float(f["tolerance"]), f["unit"].strip()))
            db.commit()
            flash(f"Sensor registered with ID {sensor_id}.")
            return redirect(url_for("sensor", sensor_id=sensor_id))
        except (sqlite3.IntegrityError, ValueError) as e:
            flash(tr("Could not save sensor") + f": {e}", "error")
    return render_template("sensor_form.html", stations=stations_)


@app.route("/sensors/bulk-sample")
def sensor_bulk_sample():
    first_station = get_db().execute("SELECT station_id FROM stations ORDER BY station_id LIMIT 1").fetchone()
    example_station_id = first_station["station_id"] if first_station else 1
    data = _excel_workbook(
        [
            "Fill one sensor per row in the 'Sensors' sheet.",
            "All columns are required except Manufacturer and Unit.",
            "Station ID must already exist in the Station Details register.",
            "Sensor ID and Serial Number must be unique.",
            "Calibration Interval (days) is normally 365 for annual calibration.",
            "Tolerance is the default allowed error used during calibration.",
            "Do not change the column headings."
        ],
        "Sensors",
        ["Sensor ID", "Station ID", "Sensor Type", "Manufacturer", "Serial Number",
         "Calibration Interval (days)", "Tolerance", "Unit"],
        [["TS-EXAMPLE", example_station_id, "Temperature", "Example Manufacturer", "SN-EXAMPLE",
          365, 0.5, "°C"]]
    )
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="sensor_bulk_upload_sample.xlsx"'})


@app.route("/sensors/bulk-upload", methods=["POST"])
def sensor_bulk_upload():
    upload = request.files.get("file")
    if not upload or not upload.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("Please choose an Excel .xlsx file.", "error")
        return redirect(url_for("register"))
    try:
        from openpyxl import load_workbook
        wb = load_workbook(upload, read_only=True, data_only=True)
        ws = wb["Sensors"] if "Sensors" in wb.sheetnames else wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            raise ValueError("The Excel file is empty.")
        headers = [str(x).strip() if x is not None else "" for x in rows[0]]
        expected = ["Sensor ID", "Station ID", "Sensor Type", "Manufacturer", "Serial Number",
                    "Calibration Interval (days)", "Tolerance", "Unit"]
        if headers != expected:
            raise ValueError("Invalid columns. Download the sensor Excel sample and use its column headings.")
        db = get_db()
        parsed, errors = [], []
        seen_ids, seen_serials = set(), set()
        for row_no, row in enumerate(rows[1:], 2):
            if not any(v not in (None, "") for v in row):
                continue
            vals = list(row) + [""] * (8 - len(row))
            sensor_id, station_id, sensor_type, manufacturer, serial, interval, tolerance, unit = vals[:8]
            sensor_id = str(sensor_id).strip().upper() if sensor_id is not None else ""
            sensor_type = str(sensor_type).strip() if sensor_type is not None else ""
            manufacturer = str(manufacturer).strip() if manufacturer is not None else ""
            serial = str(serial).strip() if serial is not None else ""
            unit = str(unit).strip() if unit is not None else ""
            if not sensor_id or not sensor_type or not serial:
                errors.append(f"Row {row_no}: Sensor ID, Sensor Type and Serial Number are required.")
                continue
            try:
                station_id = int(float(station_id))
                interval = int(float(interval))
                tolerance = float(tolerance)
                if station_id <= 0 or interval <= 0 or tolerance < 0 or not math.isfinite(tolerance):
                    raise ValueError
            except (TypeError, ValueError):
                errors.append(f"Row {row_no}: Station ID, interval and tolerance must be valid positive numeric values (tolerance may be 0).")
                continue
            if sensor_id in seen_ids or serial.casefold() in seen_serials:
                errors.append(f"Row {row_no}: duplicate Sensor ID or Serial Number in the file.")
                continue
            seen_ids.add(sensor_id)
            seen_serials.add(serial.casefold())
            if not db.execute("SELECT 1 FROM stations WHERE station_id=?", (station_id,)).fetchone():
                errors.append(f"Row {row_no}: Station ID {station_id} does not exist.")
                continue
            if db.execute("SELECT 1 FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone():
                errors.append(f"Row {row_no}: Sensor ID '{sensor_id}' already exists.")
                continue
            if db.execute("SELECT 1 FROM sensors WHERE serial_number=? COLLATE NOCASE", (serial,)).fetchone():
                errors.append(f"Row {row_no}: Serial Number '{serial}' already exists.")
                continue
            parsed.append((sensor_id, station_id, sensor_type, manufacturer, serial, interval, tolerance, unit))
        if errors:
            raise ValueError("Upload stopped. " + " ".join(errors[:12]) +
                             (f" Showing first 12 of {len(errors)} errors." if len(errors) > 12 else ""))
        with db:
            db.executemany("INSERT INTO sensors(sensor_id,station_id,sensor_type,manufacturer,serial_number,interval_days,tolerance,unit) VALUES (?,?,?,?,?,?,?,?)",
                           parsed)
        flash(f"{len(parsed)} sensor record(s) uploaded successfully.")
    except ValueError as e:
        flash(str(e), "error")
    except Exception as e:
        flash(f"Could not process the Excel file: {e}", "error")
    return redirect(url_for("register"))



@app.route("/sensors/<sensor_id>/delete", methods=["POST"])
@admin_required
def delete_sensor(sensor_id):
    db = get_db()
    sensor = db.execute("SELECT sensor_id FROM sensors WHERE sensor_id=?", (sensor_id,)).fetchone()
    if not sensor:
        abort(404)
    try:
        with db:
            cal_rows = db.execute("SELECT cal_id FROM calibrations WHERE sensor_id=?", (sensor_id,)).fetchall()
            cal_ids = [r["cal_id"] for r in cal_rows]
            if cal_ids:
                cp = ",".join("?" * len(cal_ids))
                db.execute(f"DELETE FROM calibration_points WHERE cal_id IN ({cp})", cal_ids)
                db.execute(f"DELETE FROM calibrations WHERE cal_id IN ({cp})", cal_ids)
            db.execute("DELETE FROM sensors WHERE sensor_id=?", (sensor_id,))
        flash(f"Sensor '{sensor_id}' and its calibration history were deleted.")
    except sqlite3.Error:
        flash("Could not delete the sensor and its calibration history.", "error")
    return redirect(url_for("register"))


