import io
import os
import tempfile
import unittest

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from db import get_db


class ResourceLibraryTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.storage = tempfile.TemporaryDirectory()
        app.config["RESOURCE_STORAGE_DIR"] = self.storage.name
        self.tutor = app.test_client()
        self.other_tutor = app.test_client()
        self.learner = app.test_client()
        conn = get_db()
        conn.execute("DELETE FROM learning_resources")
        conn.execute("DELETE FROM tutor_subjects")
        conn.execute("DELETE FROM tutor_rates")
        conn.execute("DELETE FROM availability")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM subjects")
        conn.commit()
        conn.close()
        self.tutor_id = self.create_user(
            self.tutor, "Tutor", "tutor-resource@example.com", "tutor"
        )
        self.other_tutor_id = self.create_user(
            self.other_tutor, "Other Tutor", "other-resource@example.com", "tutor"
        )
        self.learner_id = self.create_user(
            self.learner, "Learner", "learner-resource@example.com", "learner"
        )
        subject = self.tutor.post("/tutor/subjects", json={"subject_name": "Biology"})
        self.assertEqual(subject.status_code, 201)
        conn = get_db()
        self.subject_id = conn.execute(
            "SELECT id FROM subjects WHERE subject_name = 'Biology'"
        ).fetchone()["id"]
        conn.close()
        self.other_tutor.post(
            "/tutor/subjects", json={"subject_name": "Biology"}
        )

    def tearDown(self):
        self.storage.cleanup()

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
        response = client.post(
            "/login", json={"email": email, "password": "password123"}
        )
        self.assertEqual(response.status_code, 200)
        conn = get_db()
        user_id = conn.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()["id"]
        conn.close()
        return user_id

    def upload(self, client=None, published="1", subject_id=None, filename="notes.txt", contents=b"Cell biology notes"):
        return (client or self.tutor).post(
            "/api/resources",
            data={
                "title": "Cell notes",
                "description": "Study notes",
                "subject_id": str(subject_id or self.subject_id),
                "is_published": published,
                "file": (io.BytesIO(contents), filename),
            },
            content_type="multipart/form-data",
        )

    def test_tutor_upload_list_publish_update_and_delete(self):
        uploaded = self.upload()
        self.assertEqual(uploaded.status_code, 201)
        resource_id = uploaded.get_json()["id"]
        listing = self.tutor.get("/api/resources")
        self.assertEqual(listing.status_code, 200)
        resource = listing.get_json()["resources"][0]
        self.assertEqual(resource["subject_name"], "Biology")
        self.assertEqual(resource["file_type"], "text/plain")
        self.assertEqual(resource["file_size"], len(b"Cell biology notes"))
        self.assertEqual(resource["tutor_name"], "Tutor")
        self.assertTrue(resource["is_published"])

        updated = self.tutor.put(
            f"/api/resources/{resource_id}",
            json={"title": "Updated notes", "description": "New description"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(
            self.tutor.get("/api/resources").get_json()["resources"][0]["title"],
            "Updated notes",
        )
        self.assertEqual(
            self.tutor.delete(f"/api/resources/{resource_id}").status_code, 200
        )
        self.assertEqual(self.tutor.get("/api/resources").get_json()["resources"], [])
        self.assertEqual(os.listdir(self.storage.name), [])

    def test_learners_see_and_download_only_published_relevant_resources(self):
        visible = self.upload()
        hidden = self.upload(published="0")
        self.assertEqual(visible.status_code, 201)
        self.assertEqual(hidden.status_code, 201)
        self.assertEqual(self.learner.get("/api/resources").status_code, 400)
        listing = self.learner.get("/api/resources?subject=biology")
        self.assertEqual(listing.status_code, 200)
        resources = listing.get_json()["resources"]
        self.assertEqual([resource["title"] for resource in resources], ["Cell notes"])
        self.assertNotIn("storage_key", resources[0])

        resource_id = visible.get_json()["id"]
        downloaded = self.learner.get(
            f"/api/resources/{resource_id}/download?subject=Biology"
        )
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.data, b"Cell biology notes")
        self.assertEqual(downloaded.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            self.learner.get(
                f"/api/resources/{resource_id}/download?subject=Chemistry"
            ).status_code,
            404,
        )
        self.assertEqual(
            self.learner.get(
                f"/api/resources/{hidden.get_json()['id']}/download?subject=Biology"
            ).status_code,
            404,
        )

    def test_learners_cannot_manage_resources_and_tutors_cannot_manage_others(self):
        resource_id = self.upload().get_json()["id"]
        self.assertEqual(self.learner.post("/api/resources").status_code, 403)
        self.assertEqual(
            self.learner.put(
                f"/api/resources/{resource_id}", json={"title": "Changed"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.learner.delete(f"/api/resources/{resource_id}").status_code, 403
        )
        self.assertEqual(
            self.other_tutor.put(
                f"/api/resources/{resource_id}", json={"title": "Changed"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.other_tutor.delete(f"/api/resources/{resource_id}").status_code,
            404,
        )
        self.assertEqual(
            self.other_tutor.get(
                f"/api/resources/{resource_id}/download?subject=Biology"
            ).status_code,
            404,
        )

    def test_upload_validates_subject_metadata_type_and_size(self):
        self.assertEqual(
            self.upload(filename="../notes.exe").status_code,
            400,
        )
        self.assertEqual(
            self.upload(filename="fake.pdf", contents=b"not a PDF").status_code,
            400,
        )
        self.assertEqual(
            self.upload(subject_id=9999).status_code,
            400,
        )
        self.assertEqual(
            self.upload(contents=b"x" * (5 * 1024 * 1024 + 1)).status_code,
            400,
        )
        conn = get_db()
        conn.execute(
            "DELETE FROM tutor_subjects WHERE tutor_id = ? AND subject_id = ?",
            (self.tutor_id, self.subject_id),
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.upload().status_code, 400)

    def test_resource_files_are_not_served_from_static_directory(self):
        uploaded = self.upload()
        self.assertEqual(uploaded.status_code, 201)
        storage_path = os.path.realpath(app.config["RESOURCE_STORAGE_DIR"])
        static_path = os.path.realpath(app.static_folder)
        self.assertNotEqual(os.path.commonpath((static_path, storage_path)), static_path)
        self.assertNotIn("storage_key", uploaded.get_json())


if __name__ == "__main__":
    unittest.main()
