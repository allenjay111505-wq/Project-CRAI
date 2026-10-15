import os
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from db import get_db


class TutorRateTestCase(unittest.TestCase):
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

    def create_user(self, role="tutor"):
        email = f"{role}@example.com"
        response = self.client.post(
            "/register",
            json={
                "full_name": "Test User",
                "email": email,
                "student_id": "TEST-1",
                "course": "BSCS",
                "year_level": 1,
                "password": "password123",
                "role": role,
            },
        )
        self.assertEqual(response.status_code, 201)
        response = self.client.post(
            "/login", json={"email": email, "password": "password123"}
        )
        self.assertEqual(response.status_code, 200)

    def test_rate_endpoints_require_tutor_login(self):
        self.assertEqual(self.client.get("/tutor/rate").status_code, 401)
        self.assertEqual(
            self.client.put("/tutor/rate", json={"is_free": True}).status_code,
            401,
        )

    def test_learners_cannot_read_or_update_tutor_rate(self):
        self.create_user("learner")
        self.assertEqual(self.client.get("/tutor/rate").status_code, 403)
        self.assertEqual(
            self.client.put(
                "/tutor/rate",
                json={"is_free": False, "rate_per_hour": 25},
            ).status_code,
            403,
        )

    def test_paid_rate_saves_and_free_switch_clears_it(self):
        self.create_user()
        response = self.client.get("/tutor/rate")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"is_free": True, "rate_per_hour": None})

        response = self.client.put(
            "/tutor/rate",
            json={"is_free": False, "rate_per_hour": 25.5},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["rate_per_hour"], 25.5)
        self.assertEqual(
            self.client.get("/dashboard").get_json()["rate_per_hour"], 25.5
        )

        response = self.client.put("/tutor/rate", json={"is_free": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["is_free"])
        self.assertIsNone(response.get_json()["rate_per_hour"])
        conn = get_db()
        stored_rate = conn.execute(
            "SELECT rate_per_hour FROM tutor_rates"
        ).fetchone()["rate_per_hour"]
        conn.close()
        self.assertIsNone(stored_rate)

    def test_paid_rate_requires_finite_positive_numeric_amount(self):
        self.create_user()
        invalid_payloads = (
            {},
            {"is_free": "false", "rate_per_hour": 20},
            {"is_free": False},
            {"is_free": False, "rate_per_hour": 0},
            {"is_free": False, "rate_per_hour": -1},
            {"is_free": False, "rate_per_hour": "20"},
            {"is_free": False, "rate_per_hour": True},
            {"is_free": False, "rate_per_hour": float("inf")},
            [],
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.put("/tutor/rate", json=payload)
                self.assertEqual(response.status_code, 400)

    def test_free_mode_discards_any_submitted_amount(self):
        self.create_user()
        response = self.client.put(
            "/tutor/rate",
            json={"is_free": True, "rate_per_hour": 100},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["rate_per_hour"])


if __name__ == "__main__":
    unittest.main()
