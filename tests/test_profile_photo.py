import io
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import MAX_PROFILE_PHOTO_SIZE, app
import db
from db import get_db


class ProfilePhotoTestCase(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.upload_dir = tempfile.TemporaryDirectory()
        app.config["PROFILE_PHOTO_DIR"] = self.upload_dir.name
        self.client = app.test_client()
        conn = get_db()
        conn.execute("DELETE FROM tutor_rates")
        conn.execute("DELETE FROM availability")
        conn.execute("DELETE FROM tutor_subjects")
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM subjects")
        conn.commit()
        conn.close()

    def tearDown(self):
        self.upload_dir.cleanup()

    def create_user(self):
        response = self.client.post(
            "/register",
            json={
                "full_name": "Profile Photo User",
                "email": "photo@example.com",
                "student_id": "PHOTO-1",
                "course": "BSCS",
                "year_level": 1,
                "password": "password123",
                "role": "learner",
            },
        )
        self.assertEqual(response.status_code, 201)
        response = self.client.post(
            "/login",
            json={"email": "photo@example.com", "password": "password123"},
        )
        self.assertEqual(response.status_code, 200)

    def upload(self, name, contents):
        stream = io.BytesIO(contents)
        try:
            return self.client.post(
                "/profile/photo",
                data={"photo": (stream, name)},
                content_type="multipart/form-data",
            )
        finally:
            stream.close()

    def test_existing_database_gets_profile_photo_column(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = os.path.join(directory, "legacy.db")
            conn = sqlite3.connect(database_path)
            conn.execute(
                """CREATE TABLE users (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       full_name TEXT NOT NULL,
                       email TEXT NOT NULL UNIQUE,
                       student_id TEXT NOT NULL,
                       course TEXT NOT NULL,
                       year_level INTEGER NOT NULL,
                       password_hash TEXT NOT NULL,
                       role TEXT NOT NULL,
                       created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                   )"""
            )
            conn.close()
            with patch.object(db, "DB_FILE", database_path):
                db.init_db()
                conn = db.get_db()
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(users)").fetchall()
                }
                conn.close()
        self.assertIn("profile_photo", columns)

    def test_photo_endpoints_require_authentication(self):
        self.assertEqual(self.client.get("/profile/photo").status_code, 401)
        self.assertEqual(
            self.upload("avatar.png", b"\x89PNG\r\n\x1a\n").status_code, 401
        )
        self.assertEqual(self.client.delete("/profile/photo").status_code, 401)

    def test_upload_photo_replaces_and_delete_restores_default_state(self):
        self.create_user()
        self.assertFalse(self.client.get("/dashboard").get_json()["has_profile_photo"])
        self.assertEqual(self.client.get("/profile/photo").status_code, 404)

        first_image = b"\x89PNG\r\n\x1a\nfirst image"
        response = self.upload("avatar.png", first_image)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["message"], "Profile photo updated")
        self.assertTrue(self.client.get("/dashboard").get_json()["has_profile_photo"])

        photo_response = self.client.get("/profile/photo")
        self.assertEqual(photo_response.status_code, 200)
        self.assertEqual(photo_response.data, first_image)
        self.assertEqual(photo_response.mimetype, "image/png")
        self.assertEqual(photo_response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", photo_response.headers["Cache-Control"])
        first_filename = os.listdir(self.upload_dir.name)[0]

        second_image = b"\xff\xd8\xffsecond image"
        response = self.upload("new-photo.jpg", second_image)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get("/profile/photo").data, second_image)
        conn = get_db()
        second_filename = conn.execute(
            "SELECT profile_photo FROM users WHERE email = 'photo@example.com'"
        ).fetchone()["profile_photo"]
        conn.close()
        self.assertEqual(os.listdir(self.upload_dir.name), [second_filename])
        self.assertFalse(
            os.path.exists(os.path.join(self.upload_dir.name, first_filename))
        )

        response = self.client.delete("/profile/photo")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.client.get("/dashboard").get_json()["has_profile_photo"])
        self.assertEqual(self.client.get("/profile/photo").status_code, 404)
        self.assertEqual(os.listdir(self.upload_dir.name), [])

    def test_rejects_non_image_and_oversized_uploads(self):
        self.create_user()
        response = self.upload("not-an-image.png", b"not an image")
        self.assertEqual(response.status_code, 400)
        self.assertIn("PNG, JPEG, or WebP", response.get_json()["error"])

        self.assertEqual(MAX_PROFILE_PHOTO_SIZE, 5 * 1024 * 1024)
        with patch("app.MAX_PROFILE_PHOTO_SIZE", 8):
            response = self.upload("large.png", b"\x89PNG\r\n\x1a\nx")
        self.assertEqual(response.status_code, 413)
        self.assertIn("5 MB or smaller", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
