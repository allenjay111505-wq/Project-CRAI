import os
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from db import get_db


class PublicTutorProfileTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        conn = get_db()
        conn.execute("DELETE FROM tutor_rates")
        conn.execute("DELETE FROM availability")
        conn.execute("DELETE FROM tutor_subjects")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM subjects")
        conn.commit()
        conn.close()

    def create_user(self, role="tutor", email=None):
        email = email or f"{role}@example.com"
        response = self.client.post(
            "/register",
            json={
                "full_name": "Public Tutor",
                "email": email,
                "student_id": "TEST-1",
                "course": "BSCS",
                "year_level": 1,
                "password": "password123",
                "role": role,
            },
        )
        self.assertEqual(response.status_code, 201)
        conn = get_db()
        user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        return user["id"]

    def test_profile_page_is_available_by_tutor_id(self):
        tutor_id = self.create_user()
        response = self.client.get(f"/tutors/{tutor_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"publicTutorSubjects", response.data)
        self.assertIn(b"Message tutor", response.data)
        response.close()

    def test_empty_tutor_profile_returns_empty_lists_and_free_rate(self):
        tutor_id = self.create_user()
        response = self.client.get(f"/api/tutors/{tutor_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "id": tutor_id,
                "full_name": "Public Tutor",
                "subjects": [],
                "availability": [],
                "is_free": True,
                "rate_per_hour": None,
            },
        )

    def test_profile_contains_sorted_public_details_without_private_data(self):
        tutor_id = self.create_user("both")
        conn = get_db()
        calculus_id = conn.execute(
            "INSERT INTO subjects (subject_name) VALUES ('Calculus')"
        ).lastrowid
        algebra_id = conn.execute(
            "INSERT INTO subjects (subject_name) VALUES ('Algebra')"
        ).lastrowid
        conn.executemany(
            "INSERT INTO tutor_subjects (tutor_id, subject_id) VALUES (?, ?)",
            [(tutor_id, calculus_id), (tutor_id, algebra_id)],
        )
        conn.executemany(
            """INSERT INTO availability (tutor_id, day, start_time, end_time)
               VALUES (?, ?, ?, ?)""",
            [
                (tutor_id, "Wednesday", "13:00", "14:00"),
                (tutor_id, "Monday", "10:00", "11:00"),
                (tutor_id, "Monday", "09:00", "10:00"),
            ],
        )
        conn.execute(
            "INSERT INTO tutor_rates (tutor_id, rate_per_hour) VALUES (?, ?)",
            (tutor_id, 22.5),
        )
        conn.commit()
        conn.close()

        response = self.client.get(f"/api/tutors/{tutor_id}")
        self.assertEqual(response.status_code, 200)
        profile = response.get_json()
        self.assertEqual(profile["subjects"], ["Algebra", "Calculus"])
        self.assertEqual(
            [(slot["day"], slot["start_time"]) for slot in profile["availability"]],
            [("Monday", "09:00"), ("Monday", "10:00"), ("Wednesday", "13:00")],
        )
        self.assertEqual(profile["rate_per_hour"], 22.5)
        self.assertFalse(profile["is_free"])
        self.assertNotIn("email", profile)
        self.assertNotIn("student_id", profile)
        self.assertNotIn("password_hash", profile)

    def test_missing_or_non_tutor_profiles_return_not_found(self):
        learner_id = self.create_user("learner")
        self.assertEqual(self.client.get(f"/api/tutors/{learner_id}").status_code, 404)
        self.assertEqual(self.client.get("/api/tutors/99999").status_code, 404)


if __name__ == "__main__":
    unittest.main()
