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
    session_request_columns = {
        row["name"]: row for row in conn.execute("PRAGMA table_info(session_requests)").fetchall()
    }
    scheduled_for_column = session_request_columns.get("scheduled_for")
    if scheduled_for_column and scheduled_for_column["notnull"]:
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """CREATE TABLE session_requests_new (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   tutor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                   learner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                   requested_subject_id INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
                   scheduled_for TEXT,
                   timezone_offset_minutes INTEGER,
                   note TEXT NOT NULL DEFAULT '',
                   status TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'accepted', 'declined', 'cancelled')),
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                   CHECK (tutor_id != learner_id)
                )"""
            )
            conn.execute(
                """INSERT INTO session_requests_new
                   (id, tutor_id, learner_id, requested_subject_id, scheduled_for, timezone_offset_minutes, note, status, created_at, updated_at)
                   SELECT id, tutor_id, learner_id, NULL, scheduled_for, NULL, note, status, created_at, updated_at
                   FROM session_requests"""
            )
            conn.execute("DROP TABLE session_requests")
            conn.execute("ALTER TABLE session_requests_new RENAME TO session_requests")
            conn.execute(
                """CREATE UNIQUE INDEX idx_session_requests_pending_time
                   ON session_requests (tutor_id, learner_id, scheduled_for)
                   WHERE status = 'pending'"""
            )
            conn.execute(
                """CREATE INDEX idx_session_requests_tutor_status
                   ON session_requests (tutor_id, status, created_at)"""
            )
            conn.execute(
                """CREATE INDEX idx_session_requests_learner_status
                   ON session_requests (learner_id, status, created_at)"""
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
    session_request_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(session_requests)").fetchall()
    }
    if "requested_subject_id" not in session_request_columns:
        conn.execute(
            """ALTER TABLE session_requests ADD COLUMN requested_subject_id INTEGER
               REFERENCES subjects(id) ON DELETE SET NULL"""
        )
    if "timezone_offset_minutes" not in session_request_columns:
        conn.execute(
            "ALTER TABLE session_requests ADD COLUMN timezone_offset_minutes INTEGER"
        )
    quiz_attempt_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(quiz_attempts)").fetchall()
    }
    if "ai_grading_consent" not in quiz_attempt_columns:
        conn.execute(
            "ALTER TABLE quiz_attempts ADD COLUMN ai_grading_consent INTEGER NOT NULL DEFAULT 0"
        )
    if "grading_method" not in quiz_attempt_columns:
        conn.execute(
            "ALTER TABLE quiz_attempts ADD COLUMN grading_method TEXT NOT NULL DEFAULT 'answer_key'"
        )
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
