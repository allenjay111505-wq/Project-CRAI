import os
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from db import get_db


class TutorSearchTestCase(unittest.TestCase):
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

    def create_user(self, name, email, role):
        response = self.client.post(
            "/register",
            json={
                "full_name": name,
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

    def add_subject(self, tutor_id, subject_name):
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO subjects (subject_name) VALUES (?)",
            (subject_name,),
        )
        subject_id = conn.execute(
            "SELECT id FROM subjects WHERE subject_name = ?",
            (subject_name,),
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO tutor_subjects (tutor_id, subject_id) VALUES (?, ?)",
            (tutor_id, subject_id),
        )
        conn.commit()
        conn.close()

    def set_rate(self, tutor_id, rate):
        conn = get_db()
        conn.execute(
            "INSERT INTO tutor_rates (tutor_id, rate_per_hour) VALUES (?, ?)",
            (tutor_id, rate),
        )
        conn.commit()
        conn.close()

    def add_availability(self, tutor_id, day, start_time, end_time):
        conn = get_db()
        conn.execute(
            """INSERT INTO availability (tutor_id, day, start_time, end_time)
               VALUES (?, ?, ?, ?)""",
            (tutor_id, day, start_time, end_time),
        )
        conn.commit()
        conn.close()

    def test_search_is_case_insensitive_and_returns_tutor_details(self):
        tutor_id = self.create_user("Ana Tutor", "ana@example.com", "tutor")
        self.add_subject(tutor_id, "Calculus")
        self.add_subject(tutor_id, "Physics")
        conn = get_db()
        conn.execute(
            "INSERT INTO tutor_rates (tutor_id, rate_per_hour) VALUES (?, ?)",
            (tutor_id, 18.5),
        )
        conn.commit()
        conn.close()

        response = self.client.get("/api/tutors?subject=cAlCuLuS")
        self.assertEqual(response.status_code, 200)
        tutors = response.get_json()["tutors"]
        self.assertEqual(len(tutors), 1)
        self.assertEqual(
            tutors[0],
            {
                "id": tutor_id,
                "full_name": "Ana Tutor",
                "subjects": ["Calculus", "Physics"],
                "is_free": False,
                "rate_per_hour": 18.5,
            },
        )

    def test_search_returns_free_tutors_and_both_role_but_not_learners(self):
        tutor_id = self.create_user("Free Tutor", "free@example.com", "tutor")
        both_id = self.create_user("Tutor Learner", "both@example.com", "both")
        learner_id = self.create_user("Learner", "learner@example.com", "learner")
        for user_id in (tutor_id, both_id, learner_id):
            self.add_subject(user_id, "Programming 1")

        response = self.client.get("/api/tutors?subject=PROGRAMMING%201")
        self.assertEqual(response.status_code, 200)
        tutors = response.get_json()["tutors"]
        self.assertEqual([tutor["id"] for tutor in tutors], [tutor_id, both_id])
        self.assertTrue(all(tutor["is_free"] for tutor in tutors))
        self.assertTrue(all(tutor["rate_per_hour"] is None for tutor in tutors))

    def test_no_matching_tutors_returns_empty_list(self):
        response = self.client.get("/api/tutors?subject=Chemistry")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"tutors": []})

    def test_search_requires_a_subject(self):
        response = self.client.get("/api/tutors")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "Enter a subject to search")
        response = self.client.get("/api/tutors?subject=%20%20")
        self.assertEqual(response.status_code, 400)

    def test_subject_availability_and_free_filters_combine(self):
        free_monday = self.create_user(
            "Free Monday Tutor", "free-monday@example.com", "tutor"
        )
        paid_monday = self.create_user(
            "Paid Monday Tutor", "paid-monday@example.com", "tutor"
        )
        free_tuesday = self.create_user(
            "Free Tuesday Tutor", "free-tuesday@example.com", "tutor"
        )
        for tutor_id in (free_monday, paid_monday, free_tuesday):
            self.add_subject(tutor_id, "Calculus")
        self.set_rate(paid_monday, 12.5)
        self.add_availability(free_monday, "Monday", "09:00", "12:00")
        self.add_availability(paid_monday, "Monday", "09:00", "12:00")
        self.add_availability(free_tuesday, "Tuesday", "09:00", "12:00")

        response = self.client.get(
            "/api/tutors?subject=CALCULUS&day=monday&start_time=10:00"
            "&end_time=11:00&rate=free"
        )
        self.assertEqual(response.status_code, 200)
        tutors = response.get_json()["tutors"]
        self.assertEqual([tutor["id"] for tutor in tutors], [free_monday])

        response = self.client.get(
            "/api/tutors?subject=Calculus&day=Monday&start_time=10:00"
            "&end_time=11:00&rate=paid"
        )
        self.assertEqual(
            [tutor["id"] for tutor in response.get_json()["tutors"]],
            [paid_monday],
        )

    def test_rate_sorting_preserves_subject_and_availability_filters(self):
        cheap = self.create_user("Cheap Tutor", "cheap@example.com", "tutor")
        expensive = self.create_user(
            "Expensive Tutor", "expensive@example.com", "tutor"
        )
        unavailable = self.create_user(
            "Unavailable Tutor", "unavailable@example.com", "tutor"
        )
        wrong_subject = self.create_user(
            "Other Subject Tutor", "other@example.com", "tutor"
        )
        for tutor_id in (cheap, expensive, unavailable):
            self.add_subject(tutor_id, "Physics")
        for tutor_id in (cheap, expensive):
            self.add_availability(tutor_id, "Friday", "13:00", "17:00")
        self.add_subject(wrong_subject, "Chemistry")
        self.add_availability(wrong_subject, "Friday", "13:00", "17:00")
        self.add_availability(unavailable, "Saturday", "13:00", "17:00")
        self.set_rate(cheap, 5)
        self.set_rate(expensive, 50)
        self.set_rate(unavailable, 1)

        query = (
            "/api/tutors?subject=physics&day=Friday&start_time=14:00"
            "&end_time=15:00&rate=paid"
        )
        ascending = self.client.get(f"{query}&sort=rate_asc").get_json()["tutors"]
        descending = self.client.get(f"{query}&sort=rate_desc").get_json()["tutors"]
        self.assertEqual([tutor["id"] for tutor in ascending], [cheap, expensive])
        self.assertEqual([tutor["id"] for tutor in descending], [expensive, cheap])

    def test_invalid_availability_and_sort_filters_are_rejected(self):
        for query in (
            "subject=Calculus&day=Funday&start_time=09:00&end_time=10:00",
            "subject=Calculus&day=Monday&start_time=09:00",
            "subject=Calculus&start_time=09:00",
            "subject=Calculus&sort=unknown",
            "subject=Calculus&rate=unknown",
        ):
            with self.subTest(query=query):
                response = self.client.get(f"/api/tutors?{query}")
                self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
