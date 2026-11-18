import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from call_realtime import add_call_client, remove_call_client, send_call_signal
from db import get_db


class VideoCallTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.tutor = app.test_client()
        self.learner = app.test_client()
        self.other = app.test_client()
        conn = get_db()
        conn.execute("DELETE FROM session_requests")
        conn.execute("DELETE FROM learning_resources")
        conn.execute("DELETE FROM tutor_subjects")
        conn.execute("DELETE FROM tutor_rates")
        conn.execute("DELETE FROM availability")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM subjects")
        conn.commit()
        conn.close()
        self.tutor_id = self.create_user(
            self.tutor, "Call Tutor", "call-tutor@example.com", "tutor"
        )
        self.learner_id = self.create_user(
            self.learner, "Call Learner", "call-learner@example.com", "learner"
        )
        self.other_id = self.create_user(
            self.other, "Other Learner", "call-other@example.com", "learner"
        )
        requested = self.learner.post(
            "/session-requests",
            json={
                "tutor_id": self.tutor_id,
                "scheduled_for": (
                    datetime.now(timezone.utc) + timedelta(days=2)
                ).isoformat(timespec="minutes"),
            },
        )
        self.assertEqual(requested.status_code, 201)
        self.request_id = requested.get_json()["id"]

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
        self.assertEqual(
            client.post(
                "/login", json={"email": email, "password": "password123"}
            ).status_code,
            200,
        )
        conn = get_db()
        user_id = conn.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()["id"]
        conn.close()
        return user_id

    def test_video_configuration_requires_an_accepted_request_participant(self):
        url = f"/api/session-requests/{self.request_id}/video-config"
        self.assertEqual(self.learner.get(url).status_code, 404)
        self.assertEqual(self.other.get(url).status_code, 404)
        anonymous = app.test_client()
        self.assertEqual(anonymous.get(url).status_code, 401)
        self.assertEqual(
            self.tutor.put(
                f"/session-requests/{self.request_id}",
                json={"status": "accepted"},
            ).status_code,
            200,
        )
        learner_config = self.learner.get(url)
        self.assertEqual(learner_config.status_code, 200)
        self.assertEqual(learner_config.get_json()["ice_servers"], [])
        self.assertFalse(learner_config.get_json()["turn_configured"])
        self.assertFalse(learner_config.get_json()["is_tutor"])
        self.assertEqual(learner_config.get_json()["peer_id"], self.tutor_id)
        self.assertEqual(self.tutor.get(url).get_json()["is_tutor"], True)
        self.assertEqual(self.other.get(url).status_code, 404)

    def test_turn_shared_secret_only_produces_short_lived_client_credentials(self):
        self.tutor.put(
            f"/session-requests/{self.request_id}",
            json={"status": "accepted"},
        )
        with patch.dict(
            os.environ,
            {
                "STUN_URLS": "stun:stun.example.test:3478",
                "TURN_URLS": "turn:turn.example.test:3478?transport=udp",
                "TURN_SHARED_SECRET": "server-only-secret",
            },
        ):
            response = self.learner.get(
                f"/api/session-requests/{self.request_id}/video-config"
            )
        self.assertEqual(response.status_code, 200)
        config = response.get_json()
        self.assertEqual(len(config["ice_servers"]), 2)
        self.assertTrue(config["turn_configured"])
        self.assertNotIn("server-only-secret", response.get_data(as_text=True))
        self.assertTrue(config["ice_servers"][1]["username"].endswith(
            f":{self.learner_id}:{self.request_id}"
        ))

    def test_call_signaling_is_relayed_only_to_the_other_client_in_that_session(self):
        class FakeSocket:
            def __init__(self):
                self.messages = []

            def send(self, message):
                self.messages.append(message)

        tutor_socket = FakeSocket()
        learner_socket = FakeSocket()
        unrelated_socket = FakeSocket()
        self.assertFalse(add_call_client(701, self.tutor_id, tutor_socket))
        self.assertTrue(add_call_client(701, self.learner_id, learner_socket))
        add_call_client(702, self.other_id, unrelated_socket)
        send_call_signal(
            701,
            self.tutor_id,
            self.learner_id,
            {"type": "offer", "payload": {"type": "offer", "sdp": "example"}},
        )
        self.assertEqual(len(learner_socket.messages), 1)
        self.assertEqual(tutor_socket.messages[0], (
            '{"type": "peer-joined", "user_id": ' + str(self.learner_id) + '}'
        ))
        self.assertEqual(unrelated_socket.messages, [])
        remove_call_client(701, self.tutor_id, tutor_socket)
        remove_call_client(701, self.learner_id, learner_socket)
        remove_call_client(702, self.other_id, unrelated_socket)


if __name__ == "__main__":
    unittest.main()
