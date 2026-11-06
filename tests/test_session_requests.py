import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from db import get_db


class SessionRequestTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.learner = app.test_client()
        self.tutor = app.test_client()
        self.other = app.test_client()
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
        self.learner_id = self.create_user(
            self.learner, "Learner", "learner@example.com", "learner"
        )
        self.tutor_id = self.create_user(
            self.tutor, "Tutor", "tutor@example.com", "tutor"
        )
        self.other_id = self.create_user(
            self.other, "Other learner", "other@example.com", "learner"
        )

    def create_user(self, client, name, email, role):
        response = client.post(
            "/register",
            json={
                "full_name": name,
                "email": email,
                "student_id": email,
                "course": "BSCS",
                "year_level": 1,
                "password": "password123",
                "role": role,
            },
        )
        self.assertEqual(response.status_code, 201)
        conn = get_db()
        user_id = conn.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()["id"]
        conn.close()
        response = client.post(
            "/login", json={"email": email, "password": "password123"}
        )
        self.assertEqual(response.status_code, 200)
        return user_id

    def future_time(self):
        return (datetime.now(timezone.utc) + timedelta(days=2)).replace(
            second=0, microsecond=0
        ).isoformat(timespec="minutes")

    def create_request(self, client=None, scheduled_for=None, tutor_id=None):
        return (client or self.learner).post(
            "/session-requests",
            json={
                "tutor_id": self.tutor_id if tutor_id is None else tutor_id,
                "scheduled_for": scheduled_for or self.future_time(),
                "note": "Help with calculus",
            },
        )

    def test_authentication_validation_and_role_requirements(self):
        anonymous = app.test_client()
        self.assertEqual(anonymous.get("/session-requests").status_code, 401)
        self.assertEqual(
            anonymous.post("/session-requests", json={}).status_code, 401
        )

        self.assertEqual(
            self.create_request(self.tutor).status_code, 403
        )
        for value in (None, "tomorrow", "2020-01-01T10:00", "2020-01-01T10:00Z"):
            self.assertEqual(
                self.learner.post(
                    "/session-requests",
                    json={"tutor_id": self.tutor_id, "scheduled_for": value},
                ).status_code,
                400,
            )
        self.assertEqual(
            self.create_request(tutor_id=self.learner_id).status_code, 404
        )
        self.assertEqual(
            self.create_request(tutor_id=self.learner_id + 1000).status_code, 404
        )
        self.assertEqual(
            self.learner.post(
                "/session-requests",
                json={"tutor_id": True, "scheduled_for": self.future_time()},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.learner.post(
                "/session-requests",
                json={
                    "tutor_id": self.tutor_id,
                    "scheduled_for": self.future_time(),
                    "note": "x" * 501,
                },
            ).status_code,
            400,
        )

    def test_request_creation_is_private_and_duplicate_pending_time_is_rejected(self):
        scheduled_for = self.future_time()
        created = self.create_request(scheduled_for=scheduled_for)
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.get_json()["status"], "pending")
        request_id = created.get_json()["id"]
        duplicate = self.create_request(scheduled_for=scheduled_for)
        self.assertEqual(duplicate.status_code, 409)

        learner_requests = self.learner.get("/session-requests").get_json()["requests"]
        tutor_requests = self.tutor.get("/session-requests").get_json()["requests"]
        other_requests = self.other.get("/session-requests").get_json()["requests"]
        self.assertEqual(len(learner_requests), 1)
        self.assertEqual(len(tutor_requests), 1)
        self.assertEqual(other_requests, [])
        self.assertEqual(learner_requests[0]["direction"], "sent")
        self.assertEqual(tutor_requests[0]["direction"], "received")
        self.assertEqual(learner_requests[0]["id"], request_id)
        self.assertEqual(learner_requests[0]["tutor_name"], "Tutor")
        self.assertEqual(learner_requests[0]["learner_name"], "Learner")
        self.assertFalse(tutor_requests[0]["learner_has_profile_photo"])
        self.assertNotIn("learner_profile_photo", tutor_requests[0])

        learner_profile = self.tutor.get(
            f"/session-requests/{request_id}/learner-profile"
        )
        self.assertEqual(learner_profile.status_code, 200)
        profile = learner_profile.get_json()
        self.assertEqual(profile["full_name"], "Learner")
        self.assertEqual(profile["email"], "learner@example.com")
        self.assertEqual(profile["student_id"], "learner@example.com")
        self.assertEqual(profile["course"], "BSCS")
        self.assertEqual(profile["year_level"], 1)
        self.assertFalse(profile["has_profile_photo"])
        self.assertEqual(
            self.learner.get(
                f"/session-requests/{request_id}/learner-profile"
            ).status_code,
            404,
        )
        self.assertEqual(
            app.test_client().get(
                f"/session-requests/{request_id}/learner-profile"
            ).status_code,
            401,
        )

    def test_tutor_can_view_only_requesting_learners_profile_photo(self):
        request_id = self.create_request().get_json()["id"]
        with tempfile.TemporaryDirectory() as upload_dir:
            previous_dir = app.config["PROFILE_PHOTO_DIR"]
            app.config["PROFILE_PHOTO_DIR"] = upload_dir
            try:
                filename = f"{uuid.uuid4().hex}.png"
                photo = b"\x89PNG\r\n\x1a\nrequested learner photo"
                with open(os.path.join(upload_dir, filename), "wb") as stored_photo:
                    stored_photo.write(photo)
                conn = get_db()
                conn.execute(
                    "UPDATE users SET profile_photo = ? WHERE id = ?",
                    (filename, self.learner_id),
                )
                conn.commit()
                conn.close()

                profile = self.tutor.get(
                    f"/session-requests/{request_id}/learner-profile"
                ).get_json()
                self.assertTrue(profile["has_profile_photo"])
                response = self.tutor.get(
                    f"/api/users/{self.learner_id}/photo"
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, photo)
                self.assertEqual(
                    self.other.get(
                        f"/api/users/{self.learner_id}/photo"
                    ).status_code,
                    404,
                )
            finally:
                app.config["PROFILE_PHOTO_DIR"] = previous_dir

    def test_only_the_correct_participant_can_update_a_pending_request(self):
        request_id = self.create_request().get_json()["id"]
        self.assertEqual(
            self.other.put(
                f"/session-requests/{request_id}", json={"status": "accepted"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.learner.put(
                f"/session-requests/{request_id}", json={"status": "accepted"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.tutor.put(
                f"/session-requests/{request_id}", json={"status": "accepted"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.tutor.put(
                f"/session-requests/{request_id}", json={"status": "declined"}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.learner.put(
                f"/session-requests/{request_id}", json={"status": "cancelled"}
            ).status_code,
            409,
        )

    def test_learner_can_cancel_pending_request(self):
        request_id = self.create_request().get_json()["id"]
        response = self.learner.put(
            f"/session-requests/{request_id}", json={"status": "cancelled"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "cancelled")
        self.assertEqual(
            self.tutor.get("/session-requests").get_json()["requests"][0]["status"],
            "cancelled",
        )


if __name__ == "__main__":
    unittest.main()
