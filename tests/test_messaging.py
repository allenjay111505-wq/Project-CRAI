import os
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import MAX_MESSAGE_LENGTH, app
from db import get_db


class MessagingTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.first_client = app.test_client()
        self.second_client = app.test_client()
        self.third_client = app.test_client()
        conn = get_db()
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM conversations")
        conn.execute("DELETE FROM tutor_rates")
        conn.execute("DELETE FROM availability")
        conn.execute("DELETE FROM tutor_subjects")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM subjects")
        conn.commit()
        conn.close()

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

    def start_conversation(self, client, other_user_id):
        return client.post(
            "/conversations", json={"other_user_id": other_user_id}
        )

    def test_conversation_creation_is_idempotent_and_validates_users(self):
        first_id = self.create_user(
            self.first_client, "Learner One", "learner1@example.com", "learner"
        )
        second_id = self.create_user(
            self.second_client, "Tutor One", "tutor1@example.com", "tutor"
        )

        created = self.start_conversation(self.first_client, second_id)
        self.assertEqual(created.status_code, 201)
        conversation_id = created.get_json()["id"]
        existing = self.start_conversation(self.second_client, first_id)
        self.assertEqual(existing.status_code, 200)
        self.assertEqual(existing.get_json()["id"], conversation_id)

        self.assertEqual(
            self.start_conversation(self.first_client, first_id).status_code, 400
        )
        self.assertEqual(
            self.start_conversation(self.first_client, 99999).status_code, 404
        )
        for payload in ([], {}, {"other_user_id": True}, {"other_user_id": "2"}):
            self.assertEqual(
                self.first_client.post("/conversations", json=payload).status_code,
                400,
            )
        self.assertEqual(self.first_client.post("/conversations").status_code, 400)

    def test_only_participants_can_list_and_access_messages(self):
        first_id = self.create_user(
            self.first_client, "Learner One", "learner1@example.com", "learner"
        )
        second_id = self.create_user(
            self.second_client, "Tutor One", "tutor1@example.com", "tutor"
        )
        self.create_user(
            self.third_client, "Learner Two", "learner2@example.com", "learner"
        )
        conversation_id = self.start_conversation(
            self.first_client, second_id
        ).get_json()["id"]
        self.assertEqual(
            self.first_client.post(
                f"/conversations/{conversation_id}/messages",
                json={"content": "Hello tutor"},
            ).status_code,
            201,
        )
        self.assertEqual(
            len(self.first_client.get("/conversations").get_json()["conversations"]),
            1,
        )
        self.assertEqual(
            self.third_client.get("/conversations").get_json()["conversations"], []
        )
        self.assertEqual(
            self.third_client.get(
                f"/conversations/{conversation_id}/messages"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.third_client.post(
                f"/conversations/{conversation_id}/messages",
                json={"content": "Not allowed"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.third_client.put(f"/conversations/{conversation_id}/read").status_code,
            404,
        )
        self.assertEqual(
            self.first_client.get(
                "/conversations/99999/messages"
            ).status_code,
            404,
        )
        self.assertNotEqual(first_id, second_id)

    def test_messages_are_ordered_attributed_and_marked_read(self):
        first_id = self.create_user(
            self.first_client, "Learner One", "learner1@example.com", "learner"
        )
        second_id = self.create_user(
            self.second_client, "Tutor One", "tutor1@example.com", "tutor"
        )
        conversation_id = self.start_conversation(
            self.first_client, second_id
        ).get_json()["id"]

        first_message = self.first_client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "  Hello tutor  "},
        )
        second_message = self.second_client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "Hello learner"},
        )
        self.assertEqual(first_message.status_code, 201)
        self.assertEqual(second_message.status_code, 201)
        self.assertEqual(first_message.get_json()["message"]["content"], "Hello tutor")

        messages = self.first_client.get(
            f"/conversations/{conversation_id}/messages"
        ).get_json()["messages"]
        self.assertEqual(
            [message["content"] for message in messages],
            ["Hello tutor", "Hello learner"],
        )
        self.assertEqual(
            [message["sender_name"] for message in messages],
            ["Learner One", "Tutor One"],
        )
        self.assertEqual(
            [message["sender_id"] for message in messages], [first_id, second_id]
        )
        self.assertIsNone(messages[0]["read_at"])

        inbox = self.first_client.get("/conversations").get_json()["conversations"]
        self.assertEqual(inbox[0]["latest_message"], "Hello learner")
        self.assertEqual(inbox[0]["unread_count"], 1)
        read_response = self.first_client.put(f"/conversations/{conversation_id}/read")
        self.assertEqual(read_response.status_code, 200)
        self.assertEqual(read_response.get_json()["updated_count"], 1)
        messages = self.first_client.get(
            f"/conversations/{conversation_id}/messages"
        ).get_json()["messages"]
        self.assertIsNotNone(messages[1]["read_at"])
        self.assertEqual(
            self.first_client.get("/conversations").get_json()["conversations"][0][
                "unread_count"
            ],
            0,
        )

    def test_rejects_empty_invalid_and_overlong_messages(self):
        self.create_user(self.first_client, "Learner One", "learner1@example.com", "learner")
        second_id = self.create_user(
            self.second_client, "Tutor One", "tutor1@example.com", "tutor"
        )
        conversation_id = self.start_conversation(
            self.first_client, second_id
        ).get_json()["id"]
        url = f"/conversations/{conversation_id}/messages"
        for payload in (
            [],
            {},
            {"content": ""},
            {"content": " \n\t "},
            {"content": True},
            {"content": "x" * (MAX_MESSAGE_LENGTH + 1)},
        ):
            self.assertEqual(
                self.first_client.post(url, json=payload).status_code, 400
            )
        self.assertEqual(self.first_client.post(url).status_code, 400)
        self.assertEqual(
            self.first_client.post(
                "/conversations/99999/messages", json={"content": "Hello"}
            ).status_code,
            404,
        )

    def test_messaging_endpoints_require_authentication(self):
        anonymous = app.test_client()
        self.assertEqual(anonymous.post("/conversations").status_code, 401)
        self.assertEqual(anonymous.get("/conversations").status_code, 401)
        self.assertEqual(
            anonymous.get("/conversations/1/messages").status_code, 401
        )
        self.assertEqual(
            anonymous.post(
                "/conversations/1/messages", json={"content": "Hello"}
            ).status_code,
            401,
        )
        self.assertEqual(anonymous.put("/conversations/1/read").status_code, 401)

    def test_inbox_lists_conversations_in_recent_message_order(self):
        self.create_user(
            self.first_client, "Learner One", "learner1@example.com", "learner"
        )
        first_tutor = self.create_user(
            self.second_client, "Tutor One", "tutor1@example.com", "tutor"
        )
        second_tutor = self.create_user(
            self.third_client, "Tutor Two", "tutor2@example.com", "tutor"
        )
        older_conversation = self.start_conversation(
            self.first_client, first_tutor
        ).get_json()["id"]
        newer_conversation = self.start_conversation(
            self.first_client, second_tutor
        ).get_json()["id"]
        self.second_client.post(
            f"/conversations/{older_conversation}/messages",
            json={"content": "First message"},
        )
        self.third_client.post(
            f"/conversations/{newer_conversation}/messages",
            json={"content": "Second message"},
        )
        self.second_client.post(
            f"/conversations/{older_conversation}/messages",
            json={"content": "Most recent message"},
        )

        conversations = self.first_client.get("/conversations").get_json()[
            "conversations"
        ]
        self.assertEqual(
            [conversation["id"] for conversation in conversations],
            [older_conversation, newer_conversation],
        )
        self.assertEqual(conversations[0]["latest_message"], "Most recent message")
        self.assertEqual(conversations[0]["other_user_name"], "Tutor One")


if __name__ == "__main__":
    unittest.main()
