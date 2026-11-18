import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from db import get_db
from notifications import send_push_to_user, send_sms_to_user


class NotificationTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        conn = get_db()
        conn.execute("DELETE FROM push_subscriptions")
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
        response = self.client.post(
            "/register",
            json={
                "full_name": "Notification User",
                "email": "notification@example.com",
                "student_id": "NOTIFY-1",
                "course": "BSCS",
                "year_level": 1,
                "password": "password123",
                "role": "learner",
            },
        )
        self.assertEqual(response.status_code, 201)
        conn = get_db()
        self.user_id = conn.execute(
            "SELECT id FROM users WHERE email = ?",
            ("notification@example.com",),
        ).fetchone()["id"]
        conn.close()
        response = self.client.post(
            "/login",
            json={"email": "notification@example.com", "password": "password123"},
        )
        self.assertEqual(response.status_code, 200)

    def test_push_subscription_requires_authentication_and_server_config(self):
        anonymous = app.test_client()
        self.assertEqual(
            anonymous.get("/api/push/subscription").status_code, 401
        )
        with patch.dict(
            os.environ,
            {
                "VAPID_PUBLIC_KEY": "",
                "VAPID_PRIVATE_KEY": "",
                "VAPID_SUBJECT": "",
            },
        ):
            response = self.client.post(
                "/api/push/subscription",
                json={
                    "endpoint": "https://fcm.googleapis.com/push/token",
                    "keys": {"p256dh": "key", "auth": "secret"},
                },
            )
        self.assertEqual(response.status_code, 503)

    def test_push_subscription_validates_provider_and_can_be_removed(self):
        configuration = {
            "VAPID_PUBLIC_KEY": "public",
            "VAPID_PRIVATE_KEY": "private",
            "VAPID_SUBJECT": "mailto:admin@example.com",
        }
        with patch.dict(os.environ, configuration):
            invalid = self.client.post(
                "/api/push/subscription",
                json={
                    "endpoint": "https://attacker.example/push",
                    "keys": {"p256dh": "key", "auth": "secret"},
                },
            )
            self.assertEqual(invalid.status_code, 400)
            saved = self.client.post(
                "/api/push/subscription",
                json={
                    "endpoint": "https://fcm.googleapis.com/push/token",
                    "keys": {"p256dh": "key", "auth": "secret"},
                },
            )
            self.assertEqual(saved.status_code, 200)
            self.assertTrue(
                self.client.get("/api/push/subscription").get_json()["enabled"]
            )
            removed = self.client.delete("/api/push/subscription")
        self.assertEqual(removed.status_code, 200)
        self.assertFalse(
            self.client.get("/api/push/subscription").get_json()["enabled"]
        )

    def test_push_and_sms_delivery_are_noops_when_disabled(self):
        with patch.dict(
            os.environ,
            {
                "VAPID_PUBLIC_KEY": "",
                "VAPID_PRIVATE_KEY": "",
                "VAPID_SUBJECT": "",
                "SMS_ENABLED": "false",
                "TWILIO_ACCOUNT_SID": "ACtest",
                "TWILIO_AUTH_TOKEN": "token",
                "TWILIO_FROM_NUMBER": "+15550000000",
            },
        ), patch("notifications.get_db") as database, patch(
            "notifications.urlopen"
        ) as send_request:
            send_push_to_user(self.user_id, "Title", "Body")
            send_sms_to_user(self.user_id, "Body")
        database.assert_not_called()
        send_request.assert_not_called()

    def test_sms_delivery_requires_user_opt_in_and_uses_mocked_twilio_request(self):
        conn = get_db()
        conn.execute(
            """UPDATE users SET phone_number = ?, sms_notifications_enabled = 0
               WHERE id = ?""",
            ("+15551234567", self.user_id),
        )
        conn.commit()
        conn.close()
        environment = {
            "SMS_ENABLED": "true",
            "TWILIO_ACCOUNT_SID": "ACtest",
            "TWILIO_AUTH_TOKEN": "token",
            "TWILIO_FROM_NUMBER": "+15550000000",
        }
        response = MagicMock()
        response.__enter__.return_value.status = 201
        with patch.dict(os.environ, environment), patch(
            "notifications.urlopen", return_value=response
        ) as send_request:
            send_sms_to_user(self.user_id, "Test alert")
            send_request.assert_not_called()
            conn = get_db()
            conn.execute(
                "UPDATE users SET sms_notifications_enabled = 1 WHERE id = ?",
                (self.user_id,),
            )
            conn.commit()
            conn.close()
            send_sms_to_user(self.user_id, "Test alert")

        request = send_request.call_args.args[0]
        self.assertEqual(request.full_url, (
            "https://api.twilio.com/2010-04-01/Accounts/ACtest/Messages.json"
        ))
        self.assertEqual(request.get_method(), "POST")
        body = parse_qs(request.data.decode("utf-8"))
        self.assertEqual(body["To"], ["+15551234567"])
        self.assertEqual(body["Body"], ["Test alert"])


if __name__ == "__main__":
    unittest.main()
