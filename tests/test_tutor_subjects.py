import os
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from db import get_db


class TutorSubjectsTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()
        conn = get_db()
        conn.execute("DELETE FROM tutor_subjects")
        conn.execute("DELETE FROM availability")
        conn.execute("DELETE FROM tutor_rates")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM subjects")
        conn.commit()
        conn.close()

    def create_user(self, role="tutor"):
        response = self.client.post(
            "/register",
            json={
                "full_name": "Test User",
                "email": f"{role}@example.com",
                "student_id": "TEST-1",
                "course": "BSCS",
                "year_level": 1,
                "password": "password123",
                "role": role,
            },
        )
        self.assertEqual(response.status_code, 201)
        response = self.client.post(
            "/login",
            json={"email": f"{role}@example.com", "password": "password123"},
        )
        self.assertEqual(response.status_code, 200)

    def test_subject_endpoints_require_tutor_login(self):
        self.assertEqual(self.client.get("/tutor/subjects").status_code, 401)
        self.assertEqual(
            self.client.post(
                "/tutor/subjects", json={"subject_name": "Biology"}
            ).status_code,
            401,
        )

    def test_learners_cannot_manage_tutor_subjects(self):
        self.create_user("learner")
        self.assertEqual(self.client.get("/tutor/subjects").status_code, 403)
        self.assertEqual(
            self.client.post(
                "/tutor/subjects", json={"subject_name": "Biology"}
            ).status_code,
            403,
        )

    def test_adds_catalog_and_custom_subjects_to_profile(self):
        self.create_user()
        conn = get_db()
        conn.execute("INSERT INTO subjects (subject_name) VALUES ('Calculus')")
        conn.commit()
        conn.close()

        response = self.client.post(
            "/tutor/subjects", json={"subject_name": "Calculus"}
        )
        self.assertEqual(response.status_code, 201)
        response = self.client.post(
            "/tutor/subjects", json={"subject_name": "Organic Chemistry"}
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(
            self.client.post(
                "/tutor/subjects", json={"subject_name": "calculus"}
            ).status_code,
            200,
        )

        subjects = self.client.get("/tutor/subjects").get_json()["subjects"]
        self.assertEqual(
            [subject["subject_name"] for subject in subjects],
            ["Calculus", "Organic Chemistry"],
        )
        self.assertEqual(
            self.client.get("/dashboard").get_json()["subjects"],
            ["Calculus", "Organic Chemistry"],
        )

    def test_removal_updates_profile_but_cannot_remove_last_subject(self):
        self.create_user()
        self.client.post("/tutor/subjects", json={"subject_name": "Algebra"})
        self.client.post("/tutor/subjects", json={"subject_name": "Geometry"})
        subjects = self.client.get("/tutor/subjects").get_json()["subjects"]

        response = self.client.delete(f"/tutor/subjects/{subjects[0]['id']}")
        self.assertEqual(response.status_code, 200)
        remaining = self.client.get("/tutor/subjects").get_json()["subjects"]
        self.assertEqual(len(remaining), 1)
        response = self.client.delete(f"/tutor/subjects/{remaining[0]['id']}")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            len(self.client.get("/dashboard").get_json()["subjects"]), 1
        )


if __name__ == "__main__":
    unittest.main()
