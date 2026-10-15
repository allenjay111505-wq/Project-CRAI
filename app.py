import os
import re
import sqlite3
from datetime import timedelta

from flask import Flask, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from db import get_db, init_db

app = Flask(__name__, static_folder="public", static_url_path="")
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
app.permanent_session_lifetime = timedelta(hours=2)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

init_db()

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


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


@app.get("/")
def index():
    return app.send_static_file("index.html")


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
        """SELECT id, full_name, email, student_id, course, year_level, role, created_at
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
    conn.close()
    user_data = dict(user)
    user_data["subjects"] = [subject["subject_name"] for subject in subjects]
    resp = jsonify(user_data)
    resp.headers["Cache-Control"] = "no-store"
    return resp


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


@app.post("/tutor/subjects")
def add_tutor_subject():
    uid, error = require_tutor()
    if error:
        return error
    data = request.get_json(silent=True) or {}
    subject_name = str(data.get("subject_name", "")).strip()
    if not subject_name:
        return fail(400, "Subject name is required")
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


@app.delete("/tutor/subjects/<int:subject_id>")
def remove_tutor_subject(subject_id):
    uid, error = require_tutor()
    if error:
        return error
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


@app.post("/logout")
def logout():
    session.clear()
    return jsonify(message="Logged out")


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 3000)), debug=bool(os.environ.get("FLASK_DEBUG")))
