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
    conn.close()
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    resp = jsonify(dict(user))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/logout")
def logout():
    session.clear()
    return jsonify(message="Logged out")


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", 3000)), debug=bool(os.environ.get("FLASK_DEBUG")))
