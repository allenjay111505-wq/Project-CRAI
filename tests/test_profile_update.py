import os
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from db import get_db


class ProfileUpdateTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        conn = get_db()
        conn.execute("DELETE FROM session_requests")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM conversations")
        conn.execute("DELETE FROM tutor_rates")
        conn.execute("DELETE FROM availability")
        conn.execute("DELETE FROM tutor_subjects")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM subjects")
        conn.commit()
        conn.close()
        self.create_user("profile@example.com", "Profile User", "PROFILE-1")
        self.create_user("other@example.com", "Other User", "OTHER-1")
        response = self.client.post(
            "/login",
            json={"email": "profile@example.com", "password": "password123"},
        )
        self.assertEqual(response.status_code, 200)

    def create_user(self, email, name, student_id):
        response = self.client.post(
            "/register",
            json={
                "full_name": name,
                "email": email,
                "student_id": student_id,
                "course": "BSCS",
                "year_level": 1,
                "password": "password123",
                "role": "learner",
            },
        )
        self.assertEqual(response.status_code, 201)

    def valid_update(self, **overrides):
        profile = {
            "full_name": "Updated Name",
            "email": "updated@example.com",
            "student_id": "UPDATED-1",
            "course": "BSIT",
            "year_level": 3,
        }
        profile.update(overrides)
        return profile

    def test_profile_update_requires_authentication(self):
        client = app.test_client()
        response = client.put("/profile", json=self.valid_update())
        self.assertEqual(response.status_code, 401)

    def test_user_can_update_own_profile_without_changing_role_or_password(self):
        response = self.client.put("/profile", json=self.valid_update())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["profile"],
            {
                **self.valid_update(),
                "phone_number": None,
                "sms_notifications_enabled": False,
            },
        )

        dashboard = self.client.get("/dashboard").get_json()
        self.assertEqual(dashboard["full_name"], "Updated Name")
        self.assertEqual(dashboard["email"], "updated@example.com")
        self.assertEqual(dashboard["student_id"], "UPDATED-1")
        self.assertEqual(dashboard["course"], "BSIT")
        self.assertEqual(dashboard["year_level"], 3)
        self.assertEqual(dashboard["role"], "learner")

        conn = get_db()
        user = conn.execute(
            "SELECT password_hash FROM users WHERE email = ?",
            ("updated@example.com",),
        ).fetchone()
        other_user = conn.execute(
            "SELECT full_name, email FROM users WHERE student_id = ?",
            ("OTHER-1",),
        ).fetchone()
        conn.close()
        self.assertTrue(user["password_hash"])
        self.assertEqual(
            dict(other_user),
            {"full_name": "Other User", "email": "other@example.com"},
        )

    def test_phone_number_and_sms_consent_are_saved(self):
        response = self.client.put(
            "/profile",
            json=self.valid_update(
                phone_number="+15551234567",
                sms_notifications_enabled=True,
            ),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["profile"]["phone_number"], "+15551234567"
        )
        self.assertTrue(
            response.get_json()["profile"]["sms_notifications_enabled"]
        )
        dashboard = self.client.get("/dashboard").get_json()
        self.assertEqual(dashboard["phone_number"], "+15551234567")
        self.assertTrue(dashboard["sms_notifications_enabled"])

    def test_sms_consent_requires_a_valid_phone_number(self):
        missing_phone = self.client.put(
            "/profile",
            json=self.valid_update(sms_notifications_enabled=True),
        )
        invalid_phone = self.client.put(
            "/profile",
            json=self.valid_update(
                phone_number="5551234567",
                sms_notifications_enabled=True,
            ),
        )
        self.assertEqual(missing_phone.status_code, 400)
        self.assertIn("phone number", missing_phone.get_json()["error"].lower())
        self.assertEqual(invalid_phone.status_code, 400)

    def test_rejects_invalid_or_incomplete_profile_updates(self):
        invalid_profiles = [
            self.valid_update(email="not-an-email"),
            self.valid_update(year_level=7),
            self.valid_update(full_name=" "),
            self.valid_update(student_id="x" * 101),
        ]
        for profile in invalid_profiles:
            with self.subTest(profile=profile):
                response = self.client.put("/profile", json=profile)
                self.assertEqual(response.status_code, 400)

    def test_rejects_email_already_used_by_another_account(self):
        response = self.client.put(
            "/profile", json=self.valid_update(email="other@example.com")
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get("/dashboard").get_json()["email"], "profile@example.com")


if __name__ == "__main__":
    unittest.main()
