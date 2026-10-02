"""Route module: audit."""
from app import *

@app.route("/audit-log")
@superadmin_required
def audit_log():
    db = get_db()
    action = request.args.get("action", "").strip()
    user_id = request.args.get("user_id", "").strip()
    entity_type = request.args.get("entity_type", "").strip()
    q = request.args.get("q", "").strip()
    where = []
    params = []
    if action:
        where.append("a.action=?"); params.append(action)
    if user_id:
        where.append("a.user_id=?"); params.append(user_id)
    if entity_type:
        where.append("a.entity_type=?"); params.append(entity_type)
    if q:
        where.append("(a.entity_id LIKE ? OR a.details LIKE ? OR a.old_value LIKE ? OR a.new_value LIKE ?)")
        like = "%" + q + "%"
        params.extend([like, like, like, like])
    sql = """SELECT a.*, u.full_name, u.username, u.role
             FROM audit_log a LEFT JOIN users u ON u.user_id=a.user_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.audit_id DESC LIMIT 500"
    rows = db.execute(sql, params).fetchall()
    actions = db.execute("SELECT DISTINCT action FROM audit_log ORDER BY action").fetchall()
    users = db.execute("SELECT user_id, full_name, username FROM users ORDER BY full_name").fetchall()
    entities = db.execute("SELECT DISTINCT entity_type FROM audit_log WHERE entity_type IS NOT NULL ORDER BY entity_type").fetchall()
    return render_template("audit_log.html", rows=rows, actions=actions, users=users,
                           entities=entities, filters={"action": action, "user_id": user_id,
                                                        "entity_type": entity_type, "q": q})

