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
        conn.execute(
            "UPDATE users SET profile_photo = ? WHERE id = ?",
            ("a" * 32 + ".png", tutor_id),
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
                "has_profile_photo": True,
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

    def test_search_supports_partial_name_and_subject_terms(self):
        ana_id = self.create_user("Ana Tutor", "ana@example.com", "tutor")
        bob_id = self.create_user("Bob Helper", "bob@example.com", "tutor")
        self.add_subject(ana_id, "Calculus")
        self.add_subject(bob_id, "Biology")

        by_name = self.client.get("/api/tutors?name=ana")
        self.assertEqual(
            [tutor["id"] for tutor in by_name.get_json()["tutors"]], [ana_id]
        )
        by_subject = self.client.get("/api/tutors?subject=calcu")
        self.assertEqual(
            [tutor["id"] for tutor in by_subject.get_json()["tutors"]], [ana_id]
        )
        combined = self.client.get("/api/tutors?subject=calcu&name=ana")
        self.assertEqual(
            [tutor["id"] for tutor in combined.get_json()["tutors"]], [ana_id]
        )
        no_combined_match = self.client.get("/api/tutors?subject=calcu&name=bob")
        self.assertEqual(no_combined_match.get_json(), {"tutors": []})

    def test_search_does_not_filter_by_schedule_or_rate(self):
        first = self.create_user("First Tutor", "first@example.com", "tutor")
        second = self.create_user("Second Tutor", "second@example.com", "tutor")
        for tutor_id in (first, second):
            self.add_subject(tutor_id, "Calculus")
        self.set_rate(second, 20)
        self.add_availability(first, "Monday", "09:00", "10:00")
        self.add_availability(second, "Tuesday", "11:00", "12:00")

        response = self.client.get(
            "/api/tutors?subject=Calculus&day=Sunday&rate=free&sort=rate_desc"
        )
        self.assertEqual(
            [tutor["id"] for tutor in response.get_json()["tutors"]], [first, second]
        )
        self.assertNotIn("availability", response.get_json()["tutors"][0])

    def test_no_matching_tutors_returns_empty_list(self):
        response = self.client.get("/api/tutors?subject=Chemistry")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"tutors": []})

    def test_search_requires_a_name_or_subject(self):
        response = self.client.get("/api/tutors")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"], "Enter a tutor name or subject to search"
        )
        response = self.client.get("/api/tutors?subject=%20%20&name=%20%20")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            self.client.get("/api/tutors?subject=" + ("x" * 101)).status_code, 400
        )
        self.assertEqual(
            self.client.get("/api/tutors?name=" + ("x" * 121)).status_code, 400
        )


if __name__ == "__main__":
    unittest.main()
