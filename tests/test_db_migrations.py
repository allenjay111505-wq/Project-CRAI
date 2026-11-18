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


if __name__ == "__main__":
    unittest.main()
