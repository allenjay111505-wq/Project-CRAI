import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
configured_db_file = os.environ.get("DB_FILE", "").strip()
DB_FILE = (
    os.path.abspath(configured_db_file)
    if configured_db_file
    else os.path.join(BASE, "crai.db")
)


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    with open(os.path.join(BASE, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    user_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()
    }
    if "profile_photo" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN profile_photo TEXT")
    if "phone_number" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN phone_number TEXT")
    if "sms_notifications_enabled" not in user_columns:
        conn.execute(
            "ALTER TABLE users ADD COLUMN sms_notifications_enabled INTEGER NOT NULL DEFAULT 0"
        )
    availability_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(availability)").fetchall()
    }
    if "subject_id" not in availability_columns:
        conn.execute(
            """ALTER TABLE availability ADD COLUMN subject_id INTEGER
               REFERENCES subjects(id) ON DELETE SET NULL"""
        )
        conn.execute(
            """UPDATE availability
               SET subject_id = (
                   SELECT MIN(tutor_subjects.subject_id)
                   FROM tutor_subjects
                   WHERE tutor_subjects.tutor_id = availability.tutor_id
               )
               WHERE (
                   SELECT COUNT(*)
                   FROM tutor_subjects
                   WHERE tutor_subjects.tutor_id = availability.tutor_id
               ) = 1"""
        )
    conn.commit()
    conn.close()
