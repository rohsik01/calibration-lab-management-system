"""Route module: dashboard."""
from app import *

def bs_date_label(ad_date, nepali=False):
    """Return a readable Bikram Sambat date for a Gregorian date."""
    from nepali_datetime import date as bs_date
    b = bs_date.from_datetime_date(ad_date)
    fmt = "%K %N %D" if nepali else "%Y %B %d"
    return b.strftime(fmt)


def bs_month_grid(year, month):
    """Return a Sunday-first BS month grid using the library's BS calendar data."""
    from nepali_datetime import date as bs_date
    first = bs_date(year, month, 1)
    days = 0
    cur = first
    while cur.month == month:
        days += 1
        cur = cur + timedelta(days=1)
    first_weekday = first.to_datetime_date().weekday()  # Mon=0 ... Sun=6
    sunday_index = (first_weekday + 1) % 7
    weeks = []
    week = [None] * sunday_index
    for day in range(1, days + 1):
        cell = bs_date(year, month, day)
        week.append({
            "day": day,
            "bs": cell,
            "ad": cell.to_datetime_date(),
        })
        if len(week) == 7:
            weeks.append(week)
            week = []
    if week:
        weeks.append(week + [None] * (7 - len(week)))
    return weeks


@app.route("/api/bs-date")
def api_bs_date():
    value = request.args.get("ad", "")
    try:
        ad = date.fromisoformat(value)
        from nepali_datetime import date as bs_date
        b = bs_date.from_datetime_date(ad)
        return {"ad": ad.isoformat(), "bs": f"{b.year:04d}-{b.month:02d}-{b.day:02d}"}
    except ValueError:
        return {"error": "Invalid Gregorian date"}, 400


@app.route("/calendar")
def calendar_view():
    from nepali_datetime import date as bs_date
    today_bs = bs_date.today()
    try:
        year = request.args.get("year", type=int) or today_bs.year
        month = request.args.get("month", type=int) or today_bs.month
        if not 1 <= month <= 12 or not 1901 <= year <= 2199:
            raise ValueError
        current = bs_date(year, month, 1)
    except ValueError:
        year, month = today_bs.year, today_bs.month
        current = bs_date(year, month, 1)
    prev = current - timedelta(days=1)
    # Find the first day of the next month, then step back one day.
    if month == 12:
        nxt = bs_date(year + 1, 1, 1)
    else:
        nxt = bs_date(year, month + 1, 1)
    last = nxt - timedelta(days=1)
    return render_template("calendar.html", year=year, month=month, weeks=bs_month_grid(year, month),
                           month_name=current.strftime("%B"), month_name_ne=current.strftime("%N"),
                           prev_year=prev.year, prev_month=prev.month,
                           next_year=nxt.year, next_month=nxt.month,
                           today_bs=today_bs, today_ad=date.today(), current=current, last=last)


@app.route("/")
def index():
    """Home page: summary, items needing attention, recent activity."""
    if g.user["role"] == "general_user":
        return redirect(url_for("calibration_requests"))
    db = get_db()
    rows = db.execute(LATEST + " ORDER BY s.sensor_id").fetchall()
    counts = dict(total=len(rows), ok=0, soon=0, overdue=0, failed=0, never=0)
    for r in rows:
        label = status(r)[0]
        key = ("ok" if label == "OK" else "soon" if label.startswith("Due in") else
               "overdue" if label == "Overdue" else "failed" if label == "Failed" else "never")
        counts[key] += 1
    order = {"Failed": 0, "Overdue": 1, "Never calibrated": 2}
    attention = sorted((r for r in rows if status(r)[0] != "OK"),
                       key=lambda r: (order.get(status(r)[0], 3), r["next_due"] or ""))
    recent = db.execute(
        "SELECT c.*, s.sensor_type, st.name AS station FROM calibrations c "
        "JOIN sensors s USING(sensor_id) JOIN stations st USING(station_id) "
        "ORDER BY c.cal_id DESC LIMIT 6").fetchall()
    stations_ = db.execute(
        "SELECT st.station_id, st.name, COUNT(s.sensor_id) AS n "
        "FROM stations st LEFT JOIN sensors s USING(station_id) "
        "GROUP BY st.station_id ORDER BY st.updated_at DESC, st.station_id DESC").fetchall()
    now = datetime.now()
    bs_today = bs_date_label(date.today())
    greeting = tr("Good morning" if now.hour < 12 else "Good afternoon" if now.hour < 18
                  else "Good evening")
    if g.lang == "ne":
        today = f"{tr(now.strftime('%A'))}, {now.day} {tr(now.strftime('%B'))} {now.year}"
    else:
        today = date.today().strftime("%A, %d %B %Y")
    std_issues = [x for x in db.execute(
        "SELECT * FROM reference_standards WHERE active=1 ORDER BY valid_until")
        if standard_status(x)[0] != "Valid"]
    # KPI dashboard data: workflow pipeline, review queue, workload, turnaround and trends.
    request_counts = {st: db.execute("SELECT COUNT(*) FROM calibration_requests WHERE status=?", (st,)).fetchone()[0]
                      for st in REQUEST_STATUSES}
    pending_reviews = db.execute("""SELECT COUNT(*) FROM calibration_review_history
                                  WHERE decision='PENDING'""").fetchone()[0]
    active_work_orders = db.execute("""SELECT COUNT(*) FROM calibration_work_orders
                                      WHERE status IN ('ASSIGNED','IN PROGRESS','AWAITING REVIEW')""").fetchone()[0]
    overdue_sensors = counts["overdue"]
    failed_calibrations = db.execute("SELECT COUNT(*) FROM calibrations WHERE result='FAIL'").fetchone()[0]
    completed_requests = request_counts["COMPLETED"]
    avg_turnaround = db.execute("""SELECT AVG(julianday(updated_at) - julianday(received_date))
                                  FROM calibration_requests
                                  WHERE status='COMPLETED' AND received_date IS NOT NULL AND updated_at IS NOT NULL""").fetchone()[0]
    avg_turnaround = round(avg_turnaround, 1) if avg_turnaround is not None else 0

    workload_sql = """SELECT u.user_id, u.full_name,
                             COUNT(CASE WHEN w.status IN ('ASSIGNED','IN PROGRESS','AWAITING REVIEW') THEN 1 END) AS active,
                             COUNT(CASE WHEN w.status='COMPLETED' THEN 1 END) AS completed
                      FROM users u
                      LEFT JOIN calibration_work_orders w ON w.assigned_technician_id=u.user_id
                      WHERE u.role='technician' AND u.active=1
                      GROUP BY u.user_id, u.full_name
                      ORDER BY active DESC, completed DESC, u.full_name"""
    technician_workload = db.execute(workload_sql).fetchall()

    monthly_rows = db.execute("""SELECT substr(cal_date,1,7) AS month,
                                        COUNT(*) AS total,
                                        SUM(CASE WHEN result='PASS' THEN 1 ELSE 0 END) AS passed,
                                        SUM(CASE WHEN result='FAIL' THEN 1 ELSE 0 END) AS failed
                                 FROM calibrations
                                 WHERE cal_date >= date('now','-5 months','start of month')
                                 GROUP BY substr(cal_date,1,7)
                                 ORDER BY month""").fetchall()
    monthly = [dict(x) for x in monthly_rows]
    max_monthly = max([x["total"] for x in monthly] or [1])

    recent_activity = db.execute("""SELECT a.created_at, a.action, a.entity_type, a.entity_id,
                                           u.full_name, u.role
                                    FROM audit_log a
                                    LEFT JOIN users u ON u.user_id=a.user_id
                                    ORDER BY a.audit_id DESC LIMIT 10""").fetchall()

    dashboard = {
        "request_counts": request_counts,
        "pending_reviews": pending_reviews,
        "active_work_orders": active_work_orders,
        "overdue_sensors": overdue_sensors,
        "failed_calibrations": failed_calibrations,
        "completed_requests": completed_requests,
        "avg_turnaround": avg_turnaround,
        "technician_workload": technician_workload,
        "monthly": monthly,
        "max_monthly": max_monthly,
        "recent_activity": recent_activity,
    }
    return render_template("home.html", counts=counts, attention=attention[:8],
                           attention_total=len(attention), recent=recent, stations=stations_,
                           sensors=[r["sensor_id"] for r in rows], greeting=greeting,
                           today=today, bs_today=bs_today, std_issues=std_issues,
                           dashboard=dashboard)


# ----------------------- calibration work orders ----------------------------

