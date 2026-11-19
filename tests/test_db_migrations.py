import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import db


class DatabaseMigrationTestCase(unittest.TestCase):
    def test_existing_user_data_survives_additive_schema_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "existing.db")
            conn = sqlite3.connect(database_path)
            conn.execute(
                """CREATE TABLE users (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   full_name TEXT NOT NULL,
                   email TEXT NOT NULL UNIQUE,
                   student_id TEXT NOT NULL,
                   course TEXT NOT NULL,
                   year_level INTEGER NOT NULL,
                   password_hash TEXT NOT NULL,
                   role TEXT NOT NULL CHECK (role IN ('tutor', 'learner', 'both')),
                   created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            conn.execute(
                """INSERT INTO users
                   (full_name, email, student_id, course, year_level, password_hash, role)
                   VALUES ('Existing User', 'existing@example.com', 'ID-1',
                           'BSCS', 2, 'existing-hash', 'learner')"""
            )
            conn.commit()
            conn.close()

            with patch.object(db, "DB_FILE", database_path):
                db.init_db()
                conn = db.get_db()
                user = conn.execute(
                    "SELECT full_name, email FROM users WHERE email = ?",
                    ("existing@example.com",),
                ).fetchone()
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(users)").fetchall()
                }
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                conn.close()

            self.assertEqual(tuple(user), ("Existing User", "existing@example.com"))
            self.assertTrue(
                {
                    "profile_photo",
                    "phone_number",
                    "sms_notifications_enabled",
                }.issubset(columns)
            )
            self.assertTrue(
                {
                    "learning_resources",
                    "quizzes",
                    "quiz_questions",
                    "quiz_assignments",
                    "quiz_attempts",
                    "quiz_focus_events",
                }.issubset(tables)
            )

    def test_existing_availability_is_linked_only_when_tutor_has_one_subject(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "availability.db")
            conn = sqlite3.connect(database_path)
            conn.executescript(
                """
                CREATE TABLE users (
                  id INTEGER PRIMARY KEY,
                  full_name TEXT NOT NULL,
                  email TEXT NOT NULL,
                  student_id TEXT NOT NULL,
                  course TEXT NOT NULL,
                  year_level INTEGER NOT NULL,
                  password_hash TEXT NOT NULL,
                  role TEXT NOT NULL,
                  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE subjects (
                  id INTEGER PRIMARY KEY,
                  subject_name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE tutor_subjects (
                  tutor_id INTEGER NOT NULL,
                  subject_id INTEGER NOT NULL,
                  PRIMARY KEY (tutor_id, subject_id)
                );
                CREATE TABLE availability (
                  id INTEGER PRIMARY KEY,
                  tutor_id INTEGER NOT NULL,
                  day TEXT NOT NULL,
                  start_time TEXT NOT NULL,
                  end_time TEXT NOT NULL
                );
                INSERT INTO users
                  (id, full_name, email, student_id, course, year_level, password_hash, role)
                  VALUES
                    (1, 'One Subject Tutor', 'one@example.com', '1', 'BSCS', 1, 'hash', 'tutor'),
                    (2, 'Multiple Subject Tutor', 'many@example.com', '2', 'BSCS', 1, 'hash', 'tutor');
                INSERT INTO subjects (id, subject_name)
                  VALUES (10, 'Biology'), (20, 'Math'), (30, 'Physics');
                INSERT INTO tutor_subjects (tutor_id, subject_id)
                  VALUES (1, 10), (2, 20), (2, 30);
                INSERT INTO availability (id, tutor_id, day, start_time, end_time)
                  VALUES
                    (100, 1, 'Monday', '09:00', '10:00'),
                    (200, 2, 'Tuesday', '11:00', '12:00');
                """
            )
            conn.commit()
            conn.close()

            with patch.object(db, "DB_FILE", database_path):
                db.init_db()
                conn = db.get_db()
                slots = conn.execute(
                    "SELECT id, subject_id FROM availability ORDER BY id"
                ).fetchall()
                conn.close()

            self.assertEqual(
                [(slot["id"], slot["subject_id"]) for slot in slots],
                [(100, 10), (200, None)],
            )


if __name__ == "__main__":
    unittest.main()
