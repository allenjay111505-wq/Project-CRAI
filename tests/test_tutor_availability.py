import os
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from db import get_db


class TutorAvailabilityTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        conn = get_db()
        conn.execute("DELETE FROM availability")
        conn.execute("DELETE FROM tutor_rates")
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

    def add_slot(self, day, start_time, end_time):
        return self.client.post(
            "/tutor/availability",
            json={"day": day, "start_time": start_time, "end_time": end_time},
        )

    def test_tutor_endpoints_require_tutor_login(self):
        self.assertEqual(self.client.get("/tutor/availability").status_code, 401)
        self.assertEqual(
            self.add_slot("Monday", "09:00", "10:00").status_code, 401
        )
        self.assertEqual(
            self.client.put(
                "/tutor/availability",
                json={
                    "slot_id": 1,
                    "day": "Monday",
                    "start_time": "09:00",
                    "end_time": "10:00",
                },
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.delete("/tutor/availability", json={"slot_id": 1}).status_code,
            401,
        )

    def test_learners_cannot_manage_availability(self):
        self.create_user("learner")
        self.assertEqual(self.client.get("/tutor/availability").status_code, 403)
        self.assertEqual(
            self.add_slot("Monday", "09:00", "10:00").status_code, 403
        )
        self.assertEqual(
            self.client.put(
                "/tutor/availability",
                json={
                    "slot_id": 1,
                    "day": "Monday",
                    "start_time": "09:00",
                    "end_time": "10:00",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete("/tutor/availability", json={"slot_id": 1}).status_code,
            403,
        )

    def test_adds_slots_and_rejects_overlap_but_allows_adjacent_slots(self):
        self.create_user()
        self.assertEqual(
            self.add_slot("Monday", "09:00", "10:00").status_code, 201
        )
        response = self.add_slot("monday", "09:30", "10:30")
        self.assertEqual(response.status_code, 409)
        self.assertIn("overlaps", response.get_json()["error"])
        self.assertEqual(
            self.add_slot("Monday", "10:00", "11:00").status_code, 201
        )
        self.assertEqual(
            len(self.client.get("/tutor/availability").get_json()["availability"]),
            2,
        )

    def test_rejects_invalid_day_and_times(self):
        self.create_user()
        self.assertEqual(
            self.client.post("/tutor/availability", json=[]).status_code, 400
        )
        for day, start, end in (
            ("Funday", "09:00", "10:00"),
            ("Monday", "9:00", "10:00"),
            ("Monday", "10:00", "09:00"),
            ("Monday", "09:00", "09:00"),
        ):
            response = self.add_slot(day, start, end)
            self.assertEqual(response.status_code, 400)

    def test_edits_slots_excluding_self_from_overlap_check(self):
        self.create_user()
        self.add_slot("Monday", "09:00", "10:00")
        self.add_slot("Monday", "11:00", "12:00")
        slots = self.client.get("/tutor/availability").get_json()["availability"]

        response = self.client.put(
            f"/tutor/availability/{slots[0]['id']}",
            json={"day": "Monday", "start_time": "09:15", "end_time": "10:15"},
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.put(
            f"/tutor/availability/{slots[0]['id']}",
            json={"day": "Monday", "start_time": "11:30", "end_time": "12:30"},
        )
        self.assertEqual(response.status_code, 409)
        updated = self.client.get("/tutor/availability").get_json()["availability"]
        self.assertEqual(updated[0]["start_time"], "09:15")

    def test_schedule_is_sorted_by_weekday_then_time(self):
        self.create_user()
        self.add_slot("Wednesday", "13:00", "14:00")
        self.add_slot("Monday", "11:00", "12:00")
        self.add_slot("Monday", "09:00", "10:00")
        self.add_slot("Tuesday", "08:00", "09:00")
        slots = self.client.get("/tutor/availability").get_json()["availability"]
        self.assertEqual(
            [(slot["day"], slot["start_time"]) for slot in slots],
            [
                ("Monday", "09:00"),
                ("Monday", "11:00"),
                ("Tuesday", "08:00"),
                ("Wednesday", "13:00"),
            ],
        )

    def test_tutor_can_edit_and_delete_only_their_own_slots(self):
        self.create_user()
        self.add_slot("Friday", "15:00", "16:00")
        slot_id = self.client.get("/tutor/availability").get_json()["availability"][0]["id"]
        self.assertEqual(
            self.client.put(
                f"/tutor/availability/{slot_id + 100}",
                json={"day": "Friday", "start_time": "16:00", "end_time": "17:00"},
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(f"/tutor/availability/{slot_id + 100}").status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(f"/tutor/availability/{slot_id}").status_code, 200
        )
        self.assertEqual(
            self.client.get("/tutor/availability").get_json()["availability"], []
        )

    def test_collection_routes_update_and_delete_slots_with_validation(self):
        self.create_user()
        self.add_slot("Friday", "15:00", "16:00")
        slot_id = self.client.get("/tutor/availability").get_json()["availability"][0]["id"]
        response = self.client.put(
            "/tutor/availability",
            json={
                "slot_id": slot_id,
                "day": "Saturday",
                "start_time": "10:00",
                "end_time": "11:00",
            },
        )
        self.assertEqual(response.status_code, 200)
        slots = self.client.get("/tutor/availability").get_json()["availability"]
        self.assertEqual(slots[0]["day"], "Saturday")

        self.assertEqual(
            self.client.put(
                "/tutor/availability",
                json={
                    "slot_id": True,
                    "day": "Saturday",
                    "start_time": "10:00",
                    "end_time": "11:00",
                },
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.put("/tutor/availability", json=[]).status_code, 400
        )
        self.assertEqual(
            self.client.delete("/tutor/availability", json={"slot_id": "1"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.delete("/tutor/availability", json={"slot_id": 999}).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(
                "/tutor/availability", json={"slot_id": slot_id}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get("/tutor/availability").get_json()["availability"], []
        )


if __name__ == "__main__":
    unittest.main()
