import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

_database_dir = tempfile.TemporaryDirectory()
os.environ["DB_FILE"] = os.path.join(_database_dir.name, "test.db")

from app import app
from db import get_db


SOURCE_TEXT = (
    "Photosynthesis converts light energy into chemical energy in plants. "
    "Chlorophyll absorbs light, primarily in the blue and red wavelengths. "
    "The process uses carbon dioxide and water to produce glucose and oxygen."
)
MC_EVIDENCE = "Photosynthesis converts light energy into chemical energy in plants."
ID_EVIDENCE = (
    "Chlorophyll absorbs light, primarily in the blue and red wavelengths."
)


class QuizTestCase(unittest.TestCase):
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
            self.tutor, "Quiz Tutor", "quiz-tutor@example.com", "tutor"
        )
        self.other_tutor_id = self.create_user(
            self.other_tutor, "Other Tutor", "quiz-other@example.com", "tutor"
        )
        self.learner_id = self.create_user(
            self.learner, "Quiz Learner", "quiz-learner@example.com", "learner"
        )
        self.tutor.post("/tutor/subjects", json={"subject_name": "Biology"})
        self.other_tutor.post("/tutor/subjects", json={"subject_name": "Biology"})
        conn = get_db()
        self.subject_id = conn.execute(
            "SELECT id FROM subjects WHERE subject_name = 'Biology'"
        ).fetchone()["id"]
        conn.close()
        uploaded = self.tutor.post(
            "/api/resources",
            data={
                "title": "Photosynthesis notes",
                "description": "Class handout",
                "subject_id": str(self.subject_id),
                "is_published": "1",
                "file": (io.BytesIO(SOURCE_TEXT.encode()), "photosynthesis.txt"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 201)
        self.resource_id = uploaded.get_json()["id"]

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

    def provider_response(self, evidence=MC_EVIDENCE):
        generated = {
            "test1": [
                {
                    "question": "What does photosynthesis convert light energy into?",
                    "options": [
                        "chemical energy",
                        "thermal energy",
                        "sound energy",
                        "electrical energy",
                    ],
                    "answer": 0,
                    "evidence": evidence,
                }
            ],
            "test2": [
                {
                    "question": "Which pigment absorbs light?",
                    "answer": "Chlorophyll",
                    "evidence": ID_EVIDENCE,
                }
            ],
        }
        response = {
            "candidates": [
                {"content": {"parts": [{"text": json.dumps(generated)}]}}
            ]
        }
        return io.BytesIO(json.dumps(response).encode())

    def generate(self):
        return self.tutor.post(
            f"/api/resources/{self.resource_id}/quizzes/generate",
            json={
                "title": "Photosynthesis check",
                "multiple_choice_count": 1,
                "identification_count": 1,
                "external_ai_consent": True,
            },
        )

    def test_generation_requires_configuration_consent_and_owned_resource(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            unavailable = self.generate()
        self.assertEqual(unavailable.status_code, 503)
        self.assertIn("GEMINI_API_KEY", unavailable.get_json()["error"])
        self.assertEqual(self.tutor.get("/api/quizzes").get_json()["quizzes"], [])

        no_consent = self.tutor.post(
            f"/api/resources/{self.resource_id}/quizzes/generate",
            json={
                "title": "No consent",
                "multiple_choice_count": 1,
                "identification_count": 1,
                "external_ai_consent": False,
            },
        )
        self.assertEqual(no_consent.status_code, 400)
        self.assertEqual(
            self.other_tutor.post(
                f"/api/resources/{self.resource_id}/quizzes/generate",
                json={
                    "title": "Not mine",
                    "multiple_choice_count": 1,
                    "identification_count": 1,
                    "external_ai_consent": True,
                },
            ).status_code,
            404,
        )
        self.assertEqual(
            self.learner.post(
                f"/api/resources/{self.resource_id}/quizzes/generate",
                json={},
            ).status_code,
            403,
        )

    def test_unreadable_or_unverified_material_never_creates_fake_quiz(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch(
            "quizzes.urlopen", return_value=self.provider_response("not in source")
        ):
            result = self.generate()
        self.assertEqual(result.status_code, 422)
        self.assertIn("verified", result.get_json()["error"])
        self.assertEqual(self.tutor.get("/api/quizzes").get_json()["quizzes"], [])

        conn = get_db()
        conn.execute(
            "UPDATE learning_resources SET file_name = 'source.pdf' WHERE id = ?",
            (self.resource_id,),
        )
        conn.commit()
        conn.close()
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch(
            "quizzes.urlopen"
        ) as provider:
            unsupported = self.generate()
        self.assertEqual(unsupported.status_code, 422)
        self.assertIn("supports UTF-8 text", unsupported.get_json()["error"])
        provider.assert_not_called()

    def test_draft_review_publish_submission_and_answer_key_privacy(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch(
            "quizzes.urlopen", return_value=self.provider_response()
        ):
            generated = self.generate()
        self.assertEqual(generated.status_code, 201)
        quiz_id = generated.get_json()["id"]
        tutor_quiz = self.tutor.get(f"/api/quizzes/{quiz_id}").get_json()["quiz"]
        self.assertEqual(tutor_quiz["status"], "draft")
        self.assertEqual(len(tutor_quiz["questions"]), 2)
        self.assertTrue(all("answer_key" in q for q in tutor_quiz["questions"]))
        self.assertEqual(
            self.learner.get(
                f"/api/quizzes/{quiz_id}?subject=Biology"
            ).status_code,
            404,
        )

        edited_questions = []
        for question in tutor_quiz["questions"]:
            edited_questions.append(
                {
                    "test_number": question["test_number"],
                    "question": question["question"],
                    "options": question["options"],
                    "answer_key": (
                        int(question["answer_key"])
                        if question["test_number"] == 1
                        else question["answer_key"]
                    ),
                    "source_excerpt": question["source_excerpt"],
                }
            )
        saved = self.tutor.put(
            f"/api/quizzes/{quiz_id}",
            json={
                "title": "Reviewed photosynthesis quiz",
                "questions": edited_questions,
                "focus_monitoring_enabled": True,
            },
        )
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(
            self.other_tutor.put(
                f"/api/quizzes/{quiz_id}",
                json={
                    "title": "Stolen",
                    "questions": edited_questions,
                    "focus_monitoring_enabled": False,
                },
            ).status_code,
            404,
        )
        self.assertEqual(
            self.learner.put(f"/api/quizzes/{quiz_id}", json={}).status_code,
            403,
        )
        self.assertEqual(
            self.tutor.post(f"/api/quizzes/{quiz_id}/publish").status_code, 200
        )
        learner_quiz = self.learner.get(
            f"/api/quizzes/{quiz_id}?subject=Biology"
        ).get_json()["quiz"]
        self.assertEqual(learner_quiz["status"], "published")
        self.assertTrue(learner_quiz["focus_monitoring_enabled"])
        self.assertTrue(all("answer_key" not in q for q in learner_quiz["questions"]))
        self.assertTrue(all("source_excerpt" not in q for q in learner_quiz["questions"]))

        missing_consent = self.learner.post(
            f"/api/quizzes/{quiz_id}/attempts?subject=Biology", json={}
        )
        self.assertEqual(missing_consent.status_code, 400)
        declined = self.learner.post(
            f"/api/quizzes/{quiz_id}/attempts?subject=Biology",
            json={"focus_consent": False},
        )
        self.assertEqual(declined.status_code, 201)
        self.assertFalse(declined.get_json()["focus_monitoring_enabled"])
        self.assertEqual(
            self.learner.post(
                f"/api/quiz-attempts/{declined.get_json()['attempt_id']}/focus-events",
                json={
                    "events": [
                        {"event_type": "hidden"}
                    ]
                },
            ).status_code,
            404,
        )

        attempt = self.learner.post(
            f"/api/quizzes/{quiz_id}/attempts?subject=Biology",
            json={"focus_consent": True},
        )
        self.assertEqual(attempt.status_code, 201)
        attempt_id = attempt.get_json()["attempt_id"]
        self.assertTrue(attempt.get_json()["focus_monitoring_enabled"])
        event = self.learner.post(
            f"/api/quiz-attempts/{attempt_id}/focus-events",
            json={
                "events": [
                    {"event_type": "blur"}
                ]
            },
        )
        self.assertEqual(event.status_code, 200)
        self.assertEqual(event.get_json()["recorded"], 1)
        self.assertEqual(
            self.learner.post(
                f"/api/quiz-attempts/{attempt_id}/focus-events",
                json={
                    "events": [
                        {"event_type": "screen_capture"}
                    ]
                },
            ).status_code,
            400,
        )
        answers = {}
        for question in learner_quiz["questions"]:
            answers[str(question["id"])] = (
                0 if question["test_number"] == 1 else "chlorophyll"
            )
        submitted = self.learner.post(
            f"/api/quiz-attempts/{attempt_id}/submit", json={"answers": answers}
        )
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(submitted.get_json()["score"], 2)
        self.assertNotIn("answer_key", submitted.get_json())
        self.assertNotIn(
            "answers",
            self.learner.get(
                f"/api/quiz-attempts/{attempt_id}"
            ).get_json(),
        )
        tutor_attempt = self.tutor.get(
            f"/api/quizzes/{quiz_id}/attempts"
        ).get_json()["attempts"][0]
        self.assertEqual(tutor_attempt["learner_id"], self.learner_id)
        self.assertEqual(len(tutor_attempt["focus_events"]), 1)
        self.assertTrue(tutor_attempt["focus_consent"])

    def test_invalid_counts_and_answer_shapes_are_rejected(self):
        for count in (0, 31, 1.5, True, "many"):
            result = self.tutor.post(
                f"/api/resources/{self.resource_id}/quizzes/generate",
                json={
                    "title": "Invalid count",
                    "multiple_choice_count": count,
                    "identification_count": 1,
                    "external_ai_consent": True,
                },
            )
            self.assertEqual(result.status_code, 400)

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}), patch(
            "quizzes.urlopen", return_value=self.provider_response()
        ):
            quiz_id = self.generate().get_json()["id"]
        self.assertEqual(
            self.tutor.delete(f"/api/quizzes/{quiz_id}").status_code, 200
        )
        self.assertEqual(
            self.tutor.delete(f"/api/quizzes/{quiz_id}").status_code, 404
        )


if __name__ == "__main__":
    unittest.main()
