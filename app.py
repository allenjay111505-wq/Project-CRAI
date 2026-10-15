import io
import math
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db, init_db

app = Flask(__name__, static_folder="public", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.permanent_session_lifetime = timedelta(hours=2)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=5 * 1024 * 1024 + 64 * 1024,
    PROFILE_PHOTO_DIR=os.path.join(os.path.dirname(__file__), "uploads", "profile_photos"),
)

init_db()

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_PROFILE_PHOTO_SIZE = 5 * 1024 * 1024
PROFILE_PHOTO_TYPES = {
    "image/png": (".png", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/jpeg": (".jpg", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/webp": (
        ".webp",
        lambda data: data.startswith(b"RIFF") and data[8:12] == b"WEBP",
    ),
}
MAX_MESSAGE_LENGTH = 2000


def fail(code, msg):
    return jsonify(error=msg), code


def require_tutor():
    uid = session.get("uid")
    if not uid:
        return None, fail(401, "Not authenticated")
    conn = get_db()
    user = conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
    conn.close()
    if not user:
        session.clear()
        return None, fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        return None, fail(403, "Tutor access required")
    return uid, None


def parse_availability(data):
    if not isinstance(data, dict):
        return None, "Availability must be a JSON object"
    day = str(data.get("day", "")).strip().casefold()
    day = next((name for name in DAYS if name.casefold() == day), None)
    start_time = str(data.get("start_time", "")).strip()
    end_time = str(data.get("end_time", "")).strip()
    if day is None:
        return None, "Select a valid day of the week"
    try:
        start = datetime.strptime(start_time, "%H:%M")
        end = datetime.strptime(end_time, "%H:%M")
    except ValueError:
        return None, "Enter start and end times in HH:MM format"
    if start.strftime("%H:%M") != start_time or end.strftime("%H:%M") != end_time:
        return None, "Enter start and end times in HH:MM format"
    if start >= end:
        return None, "End time must be later than start time"
    return (day, start_time, end_time), None


def has_availability_overlap(conn, uid, day, start_time, end_time, exclude_id=None):
    query = """SELECT 1 FROM availability
               WHERE tutor_id = ? AND day = ?
                 AND start_time < ? AND end_time > ?"""
    params = [uid, day, end_time, start_time]
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
    return conn.execute(query, params).fetchone() is not None


def tutor_rate_data(conn, uid):
    row = conn.execute(
        "SELECT rate_per_hour FROM tutor_rates WHERE tutor_id = ?",
        (uid,),
    ).fetchone()
    rate = row["rate_per_hour"] if row else None
    return {"is_free": rate is None, "rate_per_hour": rate}


def get_user_conversation(conn, conversation_id, uid):
    return conn.execute(
        """SELECT id, user_1_id, user_2_id, created_at
           FROM conversations
           WHERE id = ? AND (user_1_id = ? OR user_2_id = ?)""",
        (conversation_id, uid, uid),
    ).fetchone()


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/tutors/<int:tutor_id>")
def public_tutor_profile_page(tutor_id):
    return app.send_static_file("index.html")


@app.get("/tutors/search")
@app.get("/api/tutors")
def search_tutors():
    subject_name = request.args.get("subject", "").strip()
    if not subject_name:
        return fail(400, "Enter a subject to search")

    day = request.args.get("day", "").strip()
    start_time = request.args.get("start_time", "").strip()
    end_time = request.args.get("end_time", "").strip()
    if day:
        day = next((value for value in DAYS if value.casefold() == day.casefold()), None)
        if day is None:
            return fail(400, "Select a valid day of the week")
        if bool(start_time) != bool(end_time):
            return fail(400, "Enter both start and end times for availability")
        if start_time:
            parsed, error = parse_availability(
                {"day": day, "start_time": start_time, "end_time": end_time}
            )
            if error:
                return fail(400, error)
            day, start_time, end_time = parsed
    elif start_time or end_time:
        return fail(400, "Select a day when filtering by time")

    rate_filter = request.args.get("rate", "any").strip().casefold()
    free_filter = request.args.get("free")
    if free_filter is not None and free_filter.strip():
        free_value = free_filter.strip().casefold()
        if free_value in ("true", "1", "yes", "free"):
            free_rate_filter = "free"
        elif free_value in ("false", "0", "no", "paid"):
            free_rate_filter = "paid"
        else:
            return fail(400, "Select a valid free filter")
        if rate_filter != "any" and rate_filter != free_rate_filter:
            return fail(400, "Free and rate filters must agree")
        rate_filter = free_rate_filter
    if rate_filter not in ("any", "free", "paid"):
        return fail(400, "Select a valid rate filter")
    sort_order = request.args.get("sort", "name").strip().casefold()
    if sort_order not in ("name", "rate_asc", "rate_desc"):
        return fail(400, "Select a valid sort order")

    filters = [
        "users.role IN ('tutor', 'both')",
        """EXISTS (
               SELECT 1 FROM tutor_subjects AS matching_tutor_subjects
               JOIN subjects AS matching_subjects
                 ON matching_subjects.id = matching_tutor_subjects.subject_id
               WHERE matching_tutor_subjects.tutor_id = users.id
                 AND matching_subjects.subject_name = ? COLLATE NOCASE
           )""",
    ]
    params = [subject_name]
    if day:
        availability_filter = """EXISTS (
               SELECT 1 FROM availability AS matching_availability
               WHERE matching_availability.tutor_id = users.id
                 AND matching_availability.day = ?"""
        params.append(day)
        if start_time:
            availability_filter += """
                 AND matching_availability.start_time <= ?
                 AND matching_availability.end_time >= ?"""
            params.extend((start_time, end_time))
        filters.append(availability_filter + ")")
    if rate_filter == "free":
        filters.append(
            "(tutor_rates.rate_per_hour IS NULL OR tutor_rates.tutor_id IS NULL)"
        )
    elif rate_filter == "paid":
        filters.append("tutor_rates.rate_per_hour IS NOT NULL")
    if sort_order == "rate_asc":
        order_by = (
            "CASE WHEN tutor_rates.rate_per_hour IS NULL THEN 0 ELSE 1 END, "
            "tutor_rates.rate_per_hour ASC, users.full_name COLLATE NOCASE"
        )
    elif sort_order == "rate_desc":
        order_by = (
            "CASE WHEN tutor_rates.rate_per_hour IS NULL THEN 1 ELSE 0 END, "
            "tutor_rates.rate_per_hour DESC, users.full_name COLLATE NOCASE"
        )
    else:
        order_by = "users.full_name COLLATE NOCASE"

    conn = get_db()
    rows = conn.execute(
        """SELECT users.id, users.full_name, subjects.subject_name,
                  tutor_rates.rate_per_hour
           FROM users
           JOIN tutor_subjects ON tutor_subjects.tutor_id = users.id
           JOIN subjects ON subjects.id = tutor_subjects.subject_id
           LEFT JOIN tutor_rates ON tutor_rates.tutor_id = users.id
           WHERE """ + " AND ".join(filters) + """
           ORDER BY """ + order_by + """, subjects.subject_name COLLATE NOCASE""",
        params,
    ).fetchall()
    conn.close()

    tutors = {}
    for row in rows:
        tutor = tutors.setdefault(
            row["id"],
            {
                "id": row["id"],
                "full_name": row["full_name"],
                "subjects": [],
                "is_free": row["rate_per_hour"] is None,
                "rate_per_hour": row["rate_per_hour"],
            },
        )
        tutor["subjects"].append(row["subject_name"])
    response = jsonify(tutors=list(tutors.values()))
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/tutors/<int:tutor_id>")
def public_tutor_profile(tutor_id):
    conn = get_db()
    tutor = conn.execute(
        """SELECT id, full_name FROM users
           WHERE id = ? AND role IN ('tutor', 'both')""",
        (tutor_id,),
    ).fetchone()
    if not tutor:
        conn.close()
        return fail(404, "Tutor profile not found")
    subjects = conn.execute(
        """SELECT subjects.subject_name
           FROM tutor_subjects
           JOIN subjects ON subjects.id = tutor_subjects.subject_id
           WHERE tutor_subjects.tutor_id = ?
           ORDER BY subjects.subject_name COLLATE NOCASE""",
        (tutor_id,),
    ).fetchall()
    availability = conn.execute(
        """SELECT id, day, start_time, end_time FROM availability
           WHERE tutor_id = ?
           ORDER BY CASE day
               WHEN 'Monday' THEN 1 WHEN 'Tuesday' THEN 2
               WHEN 'Wednesday' THEN 3 WHEN 'Thursday' THEN 4
               WHEN 'Friday' THEN 5 WHEN 'Saturday' THEN 6
               WHEN 'Sunday' THEN 7 ELSE 8 END, start_time""",
        (tutor_id,),
    ).fetchall()
    profile = dict(tutor)
    profile["subjects"] = [row["subject_name"] for row in subjects]
    profile["availability"] = [dict(slot) for slot in availability]
    profile.update(tutor_rate_data(conn, tutor_id))
    conn.close()
    response = jsonify(profile)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/register")
def register():
    d = request.get_json(silent=True) or {}
    keys = ["full_name", "email", "student_id", "course", "year_level", "password", "role"]
    if any(d.get(k) is None or str(d.get(k)).strip() == "" for k in keys):
        return fail(400, "All fields are required")
    email = str(d["email"]).strip().lower()
    if not EMAIL_RE.match(email):
        return fail(400, "Invalid email format")
    if len(str(d["password"])) < 8:
        return fail(400, "Password must be at least 8 characters")
    if d["role"] not in ("tutor", "learner", "both"):
        return fail(400, "Invalid role")
    try:
        year = int(d["year_level"])
        assert 1 <= year <= 6
    except (ValueError, TypeError, AssertionError):
        return fail(400, "Invalid year level")

    conn = get_db()
    try:
        if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
            return fail(409, "Email is already registered")
        conn.execute(
            """INSERT INTO users (full_name, email, student_id, course, year_level, password_hash, role)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(d["full_name"]).strip(), email, str(d["student_id"]).strip(),
             str(d["course"]).strip(), year, generate_password_hash(str(d["password"])), d["role"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return fail(409, "Email is already registered")
    finally:
        conn.close()
    return jsonify(message="Registered successfully"), 201


@app.post("/login")
def login():
    d = request.get_json(silent=True) or {}
    email, password = str(d.get("email", "")).strip().lower(), str(d.get("password", ""))
    if not email or not password:
        return fail(400, "Email and password are required")
    conn = get_db()
    user = conn.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,)).fetchone()
    conn.close()
    if not user or not check_password_hash(user["password_hash"], password):
        return fail(401, "Invalid email or password")
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    return jsonify(message="Logged in")


@app.get("/dashboard")
def dashboard():
    uid = session.get("uid")
    if not uid:
        return fail(401, "Not authenticated")
    conn = get_db()
    user = conn.execute(
        """SELECT id, full_name, email, student_id, course, year_level, role,
                  profile_photo, created_at
           FROM users WHERE id = ?""", (uid,)).fetchone()
    if not user:
        conn.close()
        session.clear()
        return fail(401, "Not authenticated")
    subjects = conn.execute(
        """SELECT subjects.subject_name
           FROM tutor_subjects
           JOIN subjects ON subjects.id = tutor_subjects.subject_id
           WHERE tutor_subjects.tutor_id = ?
           ORDER BY subjects.subject_name COLLATE NOCASE""",
        (uid,),
    ).fetchall()
    user_data = dict(user)
    user_data["has_profile_photo"] = user_data.pop("profile_photo") is not None
    user_data["subjects"] = [subject["subject_name"] for subject in subjects]
    if user["role"] in ("tutor", "both"):
        user_data.update(tutor_rate_data(conn, uid))
    conn.close()
    resp = jsonify(user_data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/profile/photo")
def get_profile_photo():
    uid = session.get("uid")
    if not uid:
        return fail(401, "Not authenticated")
    conn = get_db()
    user = conn.execute(
        "SELECT profile_photo FROM users WHERE id = ?", (uid,)
    ).fetchone()
    conn.close()
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    filename = user["profile_photo"]
    if not filename or not re.fullmatch(
        r"[0-9a-f]{32}\.(?:png|jpg|webp)", filename
    ):
        return fail(404, "No profile photo has been uploaded")
    extension = os.path.splitext(filename)[1]
    mimetype = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}.get(
        extension
    )
    if not mimetype:
        return fail(404, "Profile photo not found")
    photo_path = os.path.join(app.config["PROFILE_PHOTO_DIR"], filename)
    with open(photo_path, "rb") as stored_photo:
        photo_contents = stored_photo.read()
    response = send_file(
        io.BytesIO(photo_contents),
        mimetype=mimetype,
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.post("/profile/photo")
def upload_profile_photo():
    uid = session.get("uid")
    if not uid:
        return fail(401, "Not authenticated")
    photo = request.files.get("photo")
    if not photo or not photo.filename:
        return fail(400, "Choose a profile photo to upload")
    with photo.stream as upload_stream:
        contents = upload_stream.read(MAX_PROFILE_PHOTO_SIZE + 1)
    if len(contents) > MAX_PROFILE_PHOTO_SIZE:
        return fail(413, "Profile photos must be 5 MB or smaller")
    photo_type = next(
        (
            (mimetype, extension)
            for mimetype, (extension, is_valid) in PROFILE_PHOTO_TYPES.items()
            if is_valid(contents)
        ),
        None,
    )
    if photo_type is None:
        return fail(400, "Choose a PNG, JPEG, or WebP image")
    mimetype, extension = photo_type
    filename = f"{uuid.uuid4().hex}{extension}"
    upload_dir = app.config["PROFILE_PHOTO_DIR"]
    os.makedirs(upload_dir, exist_ok=True)
    photo_path = os.path.join(upload_dir, filename)
    with open(photo_path, "wb") as stored_photo:
        stored_photo.write(contents)

    conn = get_db()
    try:
        user = conn.execute(
            "SELECT profile_photo FROM users WHERE id = ?", (uid,)
        ).fetchone()
        if not user:
            session.clear()
            os.remove(photo_path)
            return fail(401, "Not authenticated")
        old_filename = user["profile_photo"]
        conn.execute(
            "UPDATE users SET profile_photo = ? WHERE id = ?", (filename, uid)
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        os.remove(photo_path)
        raise
    finally:
        conn.close()
    if old_filename:
        old_path = os.path.join(upload_dir, old_filename)
        if os.path.isfile(old_path):
            os.remove(old_path)
    response = jsonify(message="Profile photo updated", mimetype=mimetype)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.delete("/profile/photo")
def delete_profile_photo():
    uid = session.get("uid")
    if not uid:
        return fail(401, "Not authenticated")
    conn = get_db()
    user = conn.execute(
        "SELECT profile_photo FROM users WHERE id = ?", (uid,)
    ).fetchone()
    if not user:
        conn.close()
        session.clear()
        return fail(401, "Not authenticated")
    filename = user["profile_photo"]
    conn.execute("UPDATE users SET profile_photo = NULL WHERE id = ?", (uid,))
    conn.commit()
    conn.close()
    if filename:
        photo_path = os.path.join(app.config["PROFILE_PHOTO_DIR"], filename)
        if os.path.isfile(photo_path):
            os.remove(photo_path)
    response = jsonify(message="Profile photo removed")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/tutor/subjects")
def tutor_subjects():
    uid, error = require_tutor()
    if error:
        return error
    conn = get_db()
    assigned = conn.execute(
        """SELECT subjects.id, subjects.subject_name
           FROM tutor_subjects
           JOIN subjects ON subjects.id = tutor_subjects.subject_id
           WHERE tutor_subjects.tutor_id = ?
           ORDER BY subjects.subject_name COLLATE NOCASE""",
        (uid,),
    ).fetchall()
    available = conn.execute(
        """SELECT id, subject_name FROM subjects
           ORDER BY subject_name COLLATE NOCASE"""
    ).fetchall()
    conn.close()
    return jsonify(
        subjects=[dict(subject) for subject in assigned],
        available_subjects=[dict(subject) for subject in available],
    )


@app.get("/tutor/rate")
def get_tutor_rate():
    uid, error = require_tutor()
    if error:
        return error
    conn = get_db()
    rate = tutor_rate_data(conn, uid)
    conn.close()
    return jsonify(rate)


@app.post("/tutor/subjects")
def add_tutor_subject():
    uid, error = require_tutor()
    if error:
        return error
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return fail(400, "Subject must be a JSON object")
    subject_name = data.get("subject_name")
    if not isinstance(subject_name, str) or not subject_name.strip():
        return fail(400, "Subject name is required")
    subject_name = subject_name.strip()
    if len(subject_name) > 100:
        return fail(400, "Subject name must be 100 characters or fewer")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        subject = conn.execute(
            "SELECT id FROM subjects WHERE subject_name = ? COLLATE NOCASE",
            (subject_name,),
        ).fetchone()
        if subject:
            subject_id = subject["id"]
        else:
            cursor = conn.execute(
                "INSERT INTO subjects (subject_name) VALUES (?)",
                (subject_name,),
            )
            subject_id = cursor.lastrowid
        already_assigned = conn.execute(
            """SELECT 1 FROM tutor_subjects
               WHERE tutor_id = ? AND subject_id = ?""",
            (uid, subject_id),
        ).fetchone()
        if already_assigned:
            conn.rollback()
            return jsonify(message="Subject is already on your profile")
        conn.execute(
            "INSERT INTO tutor_subjects (tutor_id, subject_id) VALUES (?, ?)",
            (uid, subject_id),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify(message="Subject added"), 201


@app.put("/tutor/subjects")
def update_tutor_subject():
    uid, error = require_tutor()
    if error:
        return error
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return fail(400, "Subject update must be a JSON object")
    subject_id = data.get("subject_id")
    subject_name = data.get("subject_name")
    if isinstance(subject_id, bool) or not isinstance(subject_id, int) or subject_id < 1:
        return fail(400, "A valid subject_id is required")
    if not isinstance(subject_name, str) or not subject_name.strip():
        return fail(400, "Subject name is required")
    subject_name = subject_name.strip()
    if len(subject_name) > 100:
        return fail(400, "Subject name must be 100 characters or fewer")

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        assignment = conn.execute(
            """SELECT 1 FROM tutor_subjects
               WHERE tutor_id = ? AND subject_id = ?""",
            (uid, subject_id),
        ).fetchone()
        if not assignment:
            conn.rollback()
            return fail(404, "Subject not found in your profile")
        subject = conn.execute(
            "SELECT id FROM subjects WHERE subject_name = ? COLLATE NOCASE",
            (subject_name,),
        ).fetchone()
        if subject:
            replacement_id = subject["id"]
        else:
            replacement_id = conn.execute(
                "INSERT INTO subjects (subject_name) VALUES (?)",
                (subject_name,),
            ).lastrowid
        if replacement_id != subject_id:
            existing_assignment = conn.execute(
                """SELECT 1 FROM tutor_subjects
                   WHERE tutor_id = ? AND subject_id = ?""",
                (uid, replacement_id),
            ).fetchone()
            if existing_assignment:
                conn.rollback()
                return fail(409, "Subject is already on your profile")
            conn.execute(
                """UPDATE tutor_subjects SET subject_id = ?
                   WHERE tutor_id = ? AND subject_id = ?""",
                (replacement_id, uid, subject_id),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify(message="Subject updated")


@app.delete("/tutor/subjects")
@app.delete("/tutor/subjects/<int:subject_id>")
def remove_tutor_subject(subject_id=None):
    uid, error = require_tutor()
    if error:
        return error
    if subject_id is None:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return fail(400, "A subject_id is required")
        subject_id = data.get("subject_id")
        if isinstance(subject_id, bool) or not isinstance(subject_id, int) or subject_id < 1:
            return fail(400, "A valid subject_id is required")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        assignment = conn.execute(
            """SELECT 1 FROM tutor_subjects
               WHERE tutor_id = ? AND subject_id = ?""",
            (uid, subject_id),
        ).fetchone()
        if not assignment:
            conn.rollback()
            return fail(404, "Subject not found in your profile")
        count = conn.execute(
            "SELECT COUNT(*) FROM tutor_subjects WHERE tutor_id = ?",
            (uid,),
        ).fetchone()[0]
        if count <= 1:
            conn.rollback()
            return fail(400, "Tutors must have at least one subject")
        conn.execute(
            "DELETE FROM tutor_subjects WHERE tutor_id = ? AND subject_id = ?",
            (uid, subject_id),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify(message="Subject removed")


@app.put("/tutor/rate")
def update_tutor_rate():
    uid, error = require_tutor()
    if error:
        return error
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or type(data.get("is_free")) is not bool:
        return fail(400, "Choose whether your sessions are free or paid")
    rate = None
    if not data["is_free"]:
        value = data.get("rate_per_hour")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return fail(400, "Enter a numeric rate greater than 0")
        if not math.isfinite(value) or value <= 0:
            return fail(400, "Enter a numeric rate greater than 0")
        rate = value

    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO tutor_rates (tutor_id, rate_per_hour) VALUES (?, ?)
               ON CONFLICT(tutor_id) DO UPDATE SET rate_per_hour = excluded.rate_per_hour""",
            (uid, rate),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify(message="Session rate saved", is_free=rate is None, rate_per_hour=rate)


@app.get("/tutor/availability")
def tutor_availability():
    uid, error = require_tutor()
    if error:
        return error
    conn = get_db()
    slots = conn.execute(
        """SELECT id, day, start_time, end_time FROM availability
           WHERE tutor_id = ?
           ORDER BY CASE day
               WHEN 'Monday' THEN 1 WHEN 'Tuesday' THEN 2
               WHEN 'Wednesday' THEN 3 WHEN 'Thursday' THEN 4
               WHEN 'Friday' THEN 5 WHEN 'Saturday' THEN 6
               WHEN 'Sunday' THEN 7 ELSE 8 END, start_time""",
        (uid,),
    ).fetchall()
    conn.close()
    return jsonify(availability=[dict(slot) for slot in slots])


@app.post("/tutor/availability")
def add_tutor_availability():
    uid, error = require_tutor()
    if error:
        return error
    parsed, error = parse_availability(request.get_json(silent=True) or {})
    if error:
        return fail(400, error)
    day, start_time, end_time = parsed
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if has_availability_overlap(conn, uid, day, start_time, end_time):
            conn.rollback()
            return fail(409, "This time overlaps another slot on that day")
        conn.execute(
            """INSERT INTO availability (tutor_id, day, start_time, end_time)
               VALUES (?, ?, ?, ?)""",
            (uid, day, start_time, end_time),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify(message="Availability added"), 201


@app.put("/tutor/availability")
@app.put("/tutor/availability/<int:slot_id>")
def update_tutor_availability(slot_id=None):
    uid, error = require_tutor()
    if error:
        return error
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return fail(400, "Availability update must be a JSON object")
    if slot_id is None:
        slot_id = data.get("slot_id")
        if isinstance(slot_id, bool) or not isinstance(slot_id, int) or slot_id < 1:
            return fail(400, "A valid slot_id is required")
    parsed, error = parse_availability(data)
    if error:
        return fail(400, error)
    day, start_time, end_time = parsed
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        slot = conn.execute(
            "SELECT 1 FROM availability WHERE id = ? AND tutor_id = ?",
            (slot_id, uid),
        ).fetchone()
        if not slot:
            conn.rollback()
            return fail(404, "Availability slot not found")
        if has_availability_overlap(conn, uid, day, start_time, end_time, slot_id):
            conn.rollback()
            return fail(409, "This time overlaps another slot on that day")
        conn.execute(
            """UPDATE availability SET day = ?, start_time = ?, end_time = ?
               WHERE id = ? AND tutor_id = ?""",
            (day, start_time, end_time, slot_id, uid),
        )
        conn.commit()
    finally:
        conn.close()
    return jsonify(message="Availability updated")


@app.delete("/tutor/availability")
@app.delete("/tutor/availability/<int:slot_id>")
def delete_tutor_availability(slot_id=None):
    uid, error = require_tutor()
    if error:
        return error
    if slot_id is None:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return fail(400, "A slot_id is required")
        slot_id = data.get("slot_id")
        if isinstance(slot_id, bool) or not isinstance(slot_id, int) or slot_id < 1:
            return fail(400, "A valid slot_id is required")
    conn = get_db()
    try:
        cursor = conn.execute(
            "DELETE FROM availability WHERE id = ? AND tutor_id = ?",
            (slot_id, uid),
        )
        if cursor.rowcount == 0:
            return fail(404, "Availability slot not found")
        conn.commit()
    finally:
        conn.close()
    return jsonify(message="Availability removed")


@app.post("/logout")
def logout():
    session.clear()
    return jsonify(message="Logged out")


@app.post("/conversations")
def create_conversation():
    uid = session.get("uid")
    if not uid:
        return fail(401, "Not authenticated")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return fail(400, "Conversation must be a JSON object")
    other_user_id = data.get("other_user_id")
    if isinstance(other_user_id, bool) or not isinstance(other_user_id, int) or other_user_id < 1:
        return fail(400, "A valid other_user_id is required")
    if other_user_id == uid:
        return fail(400, "You cannot start a conversation with yourself")

    user_1_id, user_2_id = sorted((uid, other_user_id))
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        other_user = conn.execute(
            "SELECT id FROM users WHERE id = ?", (other_user_id,)
        ).fetchone()
        if not other_user:
            conn.rollback()
            return fail(404, "User not found")
        insert_cursor = conn.execute(
            """INSERT OR IGNORE INTO conversations (user_1_id, user_2_id)
               VALUES (?, ?)""",
            (user_1_id, user_2_id),
        )
        created = insert_cursor.rowcount == 1
        conversation = conn.execute(
            """SELECT id, user_1_id, user_2_id, created_at
               FROM conversations WHERE user_1_id = ? AND user_2_id = ?""",
            (user_1_id, user_2_id),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()
    response = jsonify(
        id=conversation["id"],
        user_1_id=conversation["user_1_id"],
        user_2_id=conversation["user_2_id"],
        created_at=conversation["created_at"],
    )
    response.status_code = 201 if created else 200
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/conversations")
def list_conversations():
    uid = session.get("uid")
    if not uid:
        return fail(401, "Not authenticated")
    conn = get_db()
    rows = conn.execute(
        """SELECT conversations.id, conversations.created_at,
                  other_user.id AS other_user_id,
                  other_user.full_name AS other_user_name,
                  substr(latest.content, 1, 160) AS latest_message,
                  latest.sent_at AS latest_sent_at,
                  (SELECT COUNT(*) FROM messages AS unread
                   WHERE unread.conversation_id = conversations.id
                     AND unread.sender_id != ?
                     AND unread.read_at IS NULL) AS unread_count
           FROM conversations
           JOIN users AS other_user
             ON other_user.id = CASE
                 WHEN conversations.user_1_id = ? THEN conversations.user_2_id
                 ELSE conversations.user_1_id END
           LEFT JOIN messages AS latest
             ON latest.id = (
                 SELECT message.id FROM messages AS message
                 WHERE message.conversation_id = conversations.id
                 ORDER BY message.sent_at DESC, message.id DESC LIMIT 1
             )
           WHERE conversations.user_1_id = ? OR conversations.user_2_id = ?
           ORDER BY COALESCE(latest.sent_at, conversations.created_at) DESC,
                    latest.id DESC, conversations.id DESC""",
        (uid, uid, uid, uid),
    ).fetchall()
    conn.close()
    response = jsonify(
        conversations=[
            {
                "id": row["id"],
                "other_user_id": row["other_user_id"],
                "other_user_name": row["other_user_name"],
                "latest_message": row["latest_message"],
                "latest_sent_at": row["latest_sent_at"],
                "created_at": row["created_at"],
                "unread_count": row["unread_count"],
            }
            for row in rows
        ]
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/conversations/<int:conversation_id>/messages")
def list_messages(conversation_id):
    uid = session.get("uid")
    if not uid:
        return fail(401, "Not authenticated")
    conn = get_db()
    conversation = get_user_conversation(conn, conversation_id, uid)
    if not conversation:
        conn.close()
        return fail(404, "Conversation not found")
    rows = conn.execute(
        """SELECT messages.id, messages.sender_id, users.full_name AS sender_name,
                  messages.content, messages.sent_at, messages.read_at
           FROM messages JOIN users ON users.id = messages.sender_id
           WHERE messages.conversation_id = ?
           ORDER BY messages.sent_at, messages.id""",
        (conversation_id,),
    ).fetchall()
    conn.close()
    response = jsonify(messages=[dict(row) for row in rows])
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/conversations/<int:conversation_id>/messages")
def send_message(conversation_id):
    uid = session.get("uid")
    if not uid:
        return fail(401, "Not authenticated")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return fail(400, "Message must be a JSON object")
    content = data.get("content")
    if not isinstance(content, str) or not content.strip():
        return fail(400, "Message cannot be empty")
    content = content.strip()
    if len(content) > MAX_MESSAGE_LENGTH:
        return fail(400, f"Message must be {MAX_MESSAGE_LENGTH} characters or fewer")

    conn = get_db()
    conversation = get_user_conversation(conn, conversation_id, uid)
    if not conversation:
        conn.close()
        return fail(404, "Conversation not found")
    cursor = conn.execute(
        "INSERT INTO messages (conversation_id, sender_id, content) VALUES (?, ?, ?)",
        (conversation_id, uid, content),
    )
    conn.commit()
    row = conn.execute(
        """SELECT messages.id, messages.sender_id, users.full_name AS sender_name,
                  messages.content, messages.sent_at, messages.read_at
           FROM messages JOIN users ON users.id = messages.sender_id
           WHERE messages.id = ?""",
        (cursor.lastrowid,),
    ).fetchone()
    conn.close()
    response = jsonify(message=dict(row))
    response.status_code = 201
    response.headers["Cache-Control"] = "no-store"
    return response


@app.put("/conversations/<int:conversation_id>/read")
def mark_conversation_read(conversation_id):
    uid = session.get("uid")
    if not uid:
        return fail(401, "Not authenticated")
    conn = get_db()
    conversation = get_user_conversation(conn, conversation_id, uid)
    if not conversation:
        conn.close()
        return fail(404, "Conversation not found")
    cursor = conn.execute(
        """UPDATE messages SET read_at = strftime('%Y-%m-%d %H:%M:%f', 'now')
           WHERE conversation_id = ? AND sender_id != ? AND read_at IS NULL""",
        (conversation_id, uid),
    )
    conn.commit()
    updated_count = cursor.rowcount
    conn.close()
    response = jsonify(message="Conversation marked as read", updated_count=updated_count)
    response.headers["Cache-Control"] = "no-store"
    return response


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 3000)), debug=bool(os.environ.get("FLASK_DEBUG")))
