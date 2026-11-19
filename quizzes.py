import io
import json
import os
import re
import zipfile
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET

from flask import Blueprint, current_app, jsonify, request, session

from db import get_db

quizzes_bp = Blueprint("quizzes", __name__)
MAX_QUESTIONS_PER_TEST = 30
MAX_SOURCE_CHARS = 24000
MIN_SOURCE_CHARS = 120
MAX_FOCUS_EVENTS_PER_REQUEST = 30
MAX_FOCUS_EVENTS_PER_ATTEMPT = 300
STORAGE_KEY_RE = re.compile(r"^[0-9a-f]{32}\.(?:txt|md|pdf|docx)$")


class QuizGenerationError(Exception):
    status_code = 422


class QuizGenerationUnavailable(QuizGenerationError):
    status_code = 503


def fail(code, message):
    return jsonify(error=message), code


def _current_user(conn=None):
    user_id = session.get("uid")
    if not user_id:
        return None, None
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    user = conn.execute(
        "SELECT id, role FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if owns_connection:
        conn.close()
    return user_id, user


def _resource_for_tutor(conn, resource_id, tutor_id):
    return conn.execute(
        """SELECT resources.*, subjects.subject_name
           FROM learning_resources AS resources
           JOIN subjects ON subjects.id = resources.subject_id
           WHERE resources.id = ? AND resources.tutor_id = ?""",
        (resource_id, tutor_id),
    ).fetchone()


def _resource_path(storage_key):
    if not isinstance(storage_key, str) or not STORAGE_KEY_RE.fullmatch(storage_key):
        raise ValueError("Invalid resource storage key")
    return os.path.join(current_app.config["RESOURCE_STORAGE_DIR"], storage_key)


def extract_source_text(resource):
    extension = os.path.splitext(resource["file_name"])[1].lower()
    if extension not in (".txt", ".md", ".docx"):
        raise QuizGenerationError(
            "Quiz generation supports UTF-8 text, Markdown, and DOCX materials. "
            "This file type can still be downloaded, but its text cannot be used for generation."
        )
    path = _resource_path(resource["storage_key"])
    try:
        with open(path, "rb") as source_file:
            contents = source_file.read(5 * 1024 * 1024 + 1)
    except OSError as error:
        raise QuizGenerationError("The uploaded source material is unavailable.") from error
    if len(contents) > 5 * 1024 * 1024:
        raise QuizGenerationError("The uploaded source material exceeds the supported size.")
    if extension in (".txt", ".md"):
        try:
            text = contents.decode("utf-8")
        except UnicodeDecodeError as error:
            raise QuizGenerationError("The text source must use UTF-8 encoding.") from error
    else:
        try:
            with zipfile.ZipFile(io.BytesIO(contents)) as document:
                document_xml = document.open("word/document.xml").read(2 * 1024 * 1024 + 1)
            if len(document_xml) > 2 * 1024 * 1024:
                raise QuizGenerationError("The DOCX text exceeds the supported size.")
            root = ET.fromstring(document_xml)
            text = " ".join(
                node.text or ""
                for node in root.iter()
                if node.tag.endswith("}t")
            )
        except QuizGenerationError:
            raise
        except (KeyError, OSError, zipfile.BadZipFile, ET.ParseError) as error:
            raise QuizGenerationError("The DOCX source could not be read.") from error
    lesson_context = resource["description"].strip() if resource["description"] else ""
    if lesson_context:
        text = f"{text}\nTutor lesson context: {lesson_context}"
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < MIN_SOURCE_CHARS:
        raise QuizGenerationError(
            "There is not enough readable source content to create source-grounded questions. "
            "Upload more text or choose a more substantial source."
        )
    return text[:MAX_SOURCE_CHARS]


def _normalized_text(value):
    return " ".join(str(value).casefold().split())


def _require_source_evidence(answer, evidence, source_text):
    source_normalized = _normalized_text(source_text)
    evidence_normalized = _normalized_text(evidence)
    answer_normalized = _normalized_text(answer)
    if (
        len(evidence_normalized) < 12
        or evidence_normalized not in source_normalized
        or not answer_normalized
        or answer_normalized not in evidence_normalized
    ):
        raise QuizGenerationError(
            "Generated content could not be verified against an exact source excerpt. "
            "Add clearer source material or regenerate."
        )


def _validate_questions(payload, multiple_choice_count, identification_count, source_text):
    if not isinstance(payload, dict):
        raise QuizGenerationError("The generation provider returned an invalid question set.")
    multiple_choice = payload.get("test1")
    identification = payload.get("test2")
    if (
        not isinstance(multiple_choice, list)
        or len(multiple_choice) != multiple_choice_count
        or not isinstance(identification, list)
        or len(identification) != identification_count
    ):
        raise QuizGenerationError(
            "The provider did not return the requested number of questions. Regenerate or choose fewer."
        )
    validated = []
    for test_number, questions in ((1, multiple_choice), (2, identification)):
        for position, item in enumerate(questions, 1):
            if not isinstance(item, dict):
                raise QuizGenerationError("The provider returned an invalid question.")
            question = item.get("question")
            evidence = item.get("evidence")
            if not isinstance(question, str) or not question.strip() or len(question) > 1000:
                raise QuizGenerationError("A generated question was empty or too long.")
            if not isinstance(evidence, str) or len(evidence) > 1000:
                raise QuizGenerationError("A generated question did not include source evidence.")
            if test_number == 1:
                options = item.get("options")
                answer = item.get("answer")
                if (
                    not isinstance(options, list)
                    or not 3 <= len(options) <= 5
                    or any(not isinstance(option, str) or not option.strip() for option in options)
                    or len({_normalized_text(option) for option in options}) != len(options)
                    or isinstance(answer, bool)
                    or not isinstance(answer, int)
                    or not 0 <= answer < len(options)
                ):
                    raise QuizGenerationError("A generated multiple-choice question was invalid.")
                answer_text = options[answer].strip()
                _require_source_evidence(answer_text, evidence, source_text)
                options_json = json.dumps([option.strip() for option in options])
                answer_key = str(answer)
            else:
                options_json = "[]"
                answer = item.get("answer")
                if not isinstance(answer, str) or not answer.strip() or len(answer) > 250:
                    raise QuizGenerationError("A generated identification answer was invalid.")
                answer_text = answer.strip()
                _require_source_evidence(answer_text, evidence, source_text)
                answer_key = answer_text
            validated.append(
                {
                    "test_number": test_number,
                    "position": position,
                    "question": question.strip(),
                    "options_json": options_json,
                    "answer_key": answer_key,
                    "source_excerpt": evidence.strip(),
                }
            )
    return validated


def generate_questions(source_text, multiple_choice_count, identification_count):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
    if not api_key:
        raise QuizGenerationUnavailable(
            "Quiz generation is unavailable. Configure GEMINI_API_KEY on the server."
        )
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        raise QuizGenerationUnavailable("GEMINI_MODEL has an invalid value.")
    prompt = (
        "Create source-grounded assessment questions using only the source text below. "
        "Do not use outside knowledge or invent facts. Return only valid JSON with this shape: "
        '{"test1":[{"question":"...","options":["...","...","...","..."],'
        '"answer":0,"evidence":"an exact quote from the source"}],'
        '"test2":[{"question":"...","answer":"exact source wording",'
        '"evidence":"an exact quote from the source"}]}. '
        f"Create exactly {multiple_choice_count} Test 1 multiple-choice questions and "
        f"{identification_count} Test 2 identification questions. For multiple choice, "
        "provide 4 distinct options and use a zero-based integer answer index. Keep each "
        "answer explicitly supported by the exact evidence quote. Do not include markdown.\n\n"
        "SOURCE TEXT:\n"
        f"{source_text}"
    )
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model, safe='')}:generateContent?key={quote(api_key, safe='')}"
    )
    request_data = json.dumps(
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1,
            },
        }
    ).encode("utf-8")
    provider_request = Request(
        endpoint,
        data=request_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(provider_request, timeout=45) as response:
            provider_payload = json.loads(response.read(1024 * 1024 + 1))
    except HTTPError as error:
        raise QuizGenerationUnavailable(
            f"Gemini could not generate questions (HTTP {error.code}). Try again later."
        ) from error
    except (URLError, TimeoutError, OSError) as error:
        raise QuizGenerationUnavailable(
            "Could not connect to Gemini for quiz generation. Try again later."
        ) from error
    except (ValueError, json.JSONDecodeError) as error:
        raise QuizGenerationError("Gemini returned an unreadable response.") from error
    if not isinstance(provider_payload, dict) or not provider_payload.get("candidates"):
        raise QuizGenerationError(
            "Gemini did not return usable questions for this source material."
        )
    try:
        generated_text = provider_payload["candidates"][0]["content"]["parts"][0]["text"]
        generated = json.loads(generated_text)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise QuizGenerationError("Gemini returned questions in an unreadable format.") from error
    return _validate_questions(
        generated,
        multiple_choice_count,
        identification_count,
        source_text,
    )


def _parse_question_counts(data):
    counts = []
    for field in ("multiple_choice_count", "identification_count"):
        value = data.get(field)
        if isinstance(value, bool) or not (
            isinstance(value, int) or isinstance(value, str) and value.isdigit()
        ):
            return None, "Question counts must be whole numbers from 1 to 30"
        value = int(value)
        if value < 1 or value > MAX_QUESTIONS_PER_TEST:
            return None, "Question counts must be whole numbers from 1 to 30"
        counts.append(value)
    return tuple(counts), None


def _insert_questions(conn, quiz_id, questions):
    conn.executemany(
        """INSERT INTO quiz_questions
           (quiz_id, test_number, position, question, options_json, answer_key, source_excerpt)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                quiz_id,
                item["test_number"],
                item["position"],
                item["question"],
                item["options_json"],
                item["answer_key"],
                item["source_excerpt"],
            )
            for item in questions
        ],
    )


def _quiz_for_tutor(conn, quiz_id, tutor_id):
    return conn.execute(
        "SELECT * FROM quizzes WHERE id = ? AND tutor_id = ?",
        (quiz_id, tutor_id),
    ).fetchone()


def _session_request_for_participant(conn, session_request_id, user_id):
    return conn.execute(
        """SELECT id, tutor_id, learner_id, requested_subject_id, status
           FROM session_requests
           WHERE id = ? AND status = 'accepted'
             AND (tutor_id = ? OR learner_id = ?)""",
        (session_request_id, user_id, user_id),
    ).fetchone()


def _session_quiz_run_access(conn, run_id, user_id):
    return conn.execute(
        """SELECT runs.*, requests.tutor_id, requests.learner_id,
                  requests.status AS session_status, quizzes.title AS quiz_title,
                  quizzes.tutor_id AS quiz_tutor_id, quizzes.status AS quiz_status,
                  resources.subject_id
           FROM session_quiz_runs AS runs
           JOIN session_requests AS requests ON requests.id = runs.session_request_id
           JOIN quizzes ON quizzes.id = runs.quiz_id
           JOIN learning_resources AS resources ON resources.id = quizzes.resource_id
           WHERE runs.id = ? AND requests.status = 'accepted'
             AND (requests.tutor_id = ? OR requests.learner_id = ?)""",
        (run_id, user_id, user_id),
    ).fetchone()


def _grade_short_answers_with_gemini(questions, answers):
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    model = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
    identification = [
        {
            "id": question["id"],
            "question": question["question"],
            "expected_answer": question["answer_key"],
            "source_excerpt": question["source_excerpt"],
            "learner_answer": answers.get(str(question["id"]), ""),
        }
        for question in questions
        if question["test_number"] == 2 and answers.get(str(question["id"]))
    ]
    if not api_key or not identification:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", model):
        return None
    prompt = (
        "Grade each learner answer for meaning, using only the question, expected answer, "
        "and source excerpt. Accept a concise paraphrase only if it expresses the same "
        "fact. Do not infer missing facts. Return only JSON in the exact shape "
        '{"grades":[{"id":123,"correct":true}]}, one result per provided id. '
        "Never return explanations or extra keys.\n\n" + json.dumps(identification)
    )
    endpoint = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{quote(model, safe='')}:generateContent?key={quote(api_key, safe='')}"
    )
    provider_request = Request(
        endpoint,
        data=json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseMimeType": "application/json",
                    "temperature": 0,
                },
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(provider_request, timeout=30) as response:
            payload = json.loads(response.read(128 * 1024 + 1))
        generated_text = payload["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(generated_text)
        grades = parsed["grades"]
        if not isinstance(grades, list):
            return None
        expected_ids = {str(item["id"]) for item in identification}
        result = {}
        for grade in grades:
            if (
                not isinstance(grade, dict)
                or str(grade.get("id")) not in expected_ids
                or type(grade.get("correct")) is not bool
                or str(grade["id"]) in result
            ):
                return None
            result[str(grade["id"])] = grade["correct"]
        return result if set(result) == expected_ids else None
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def _quiz_access(conn, quiz_id, learner_id, subject_name=None):
    quiz = conn.execute(
        """SELECT quizzes.*, resources.subject_id, subjects.subject_name,
                  resources.title AS resource_title, tutor.full_name AS tutor_name
           FROM quizzes
           JOIN learning_resources AS resources ON resources.id = quizzes.resource_id
           JOIN subjects ON subjects.id = resources.subject_id
           JOIN users AS tutor ON tutor.id = quizzes.tutor_id
           WHERE quizzes.id = ? AND quizzes.status = 'published'""",
        (quiz_id,),
    ).fetchone()
    if not quiz:
        return None
    assignment = conn.execute(
        "SELECT 1 FROM quiz_assignments WHERE quiz_id = ? AND learner_id = ?",
        (quiz_id, learner_id),
    ).fetchone()
    if assignment:
        return quiz
    if not subject_name or quiz["subject_name"].casefold() != subject_name.casefold():
        return None
    listed_subject = conn.execute(
        """SELECT 1 FROM tutor_subjects
           WHERE tutor_id = ? AND subject_id = ?""",
        (quiz["tutor_id"], quiz["subject_id"]),
    ).fetchone()
    return quiz if listed_subject else None


def _serialize_questions(rows, include_key):
    questions = []
    for row in rows:
        question = {
            "id": row["id"],
            "test_number": row["test_number"],
            "position": row["position"],
            "question": row["question"],
            "options": json.loads(row["options_json"]),
        }
        if include_key:
            question["answer_key"] = row["answer_key"]
            question["source_excerpt"] = row["source_excerpt"]
        questions.append(question)
    return questions


def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


@quizzes_bp.get("/api/quizzes")
def list_quizzes():
    conn = get_db()
    user_id, user = _current_user(conn)
    if not user_id:
        conn.close()
        return fail(401, "Not authenticated")
    if not user:
        conn.close()
        session.clear()
        return fail(401, "Not authenticated")
    subject_name = request.args.get("subject", "").strip()
    tutor_id_filter = request.args.get("tutor_id")
    if tutor_id_filter is not None and (
        not tutor_id_filter.isdecimal()
        or len(tutor_id_filter) > 19
        or int(tutor_id_filter) < 1
    ):
        conn.close()
        return fail(400, "A valid tutor_id is required")
    if user["role"] in ("tutor", "both") and not subject_name:
        rows = conn.execute(
            """SELECT quizzes.id, quizzes.title, quizzes.status,
                      quizzes.focus_monitoring_enabled, quizzes.created_at,
                      quizzes.published_at, resources.id AS resource_id,
                      resources.title AS resource_title, subjects.subject_name,
                      (SELECT COUNT(*) FROM quiz_questions
                       WHERE quiz_questions.quiz_id = quizzes.id) AS question_count
               FROM quizzes
               JOIN learning_resources AS resources ON resources.id = quizzes.resource_id
               JOIN subjects ON subjects.id = resources.subject_id
               WHERE quizzes.tutor_id = ?
               ORDER BY quizzes.created_at DESC, quizzes.id DESC""",
            (user_id,),
        ).fetchall()
    else:
        if not subject_name:
            conn.close()
            return fail(400, "Choose a subject to browse published quizzes")
        rows = conn.execute(
            """SELECT DISTINCT quizzes.id, quizzes.title, quizzes.created_at,
                      quizzes.published_at, resources.id AS resource_id,
                      resources.title AS resource_title, subjects.subject_name,
                      tutor.full_name AS tutor_name,
                      (SELECT COUNT(*) FROM quiz_questions
                       WHERE quiz_questions.quiz_id = quizzes.id) AS question_count,
                      EXISTS (SELECT 1 FROM quiz_assignments AS assignments
                              WHERE assignments.quiz_id = quizzes.id
                                AND assignments.learner_id = ?) AS assigned
               FROM quizzes
               JOIN learning_resources AS resources ON resources.id = quizzes.resource_id
               JOIN subjects ON subjects.id = resources.subject_id
               JOIN users AS tutor ON tutor.id = quizzes.tutor_id
               LEFT JOIN tutor_subjects
                 ON tutor_subjects.tutor_id = quizzes.tutor_id
                AND tutor_subjects.subject_id = resources.subject_id
               WHERE quizzes.status = 'published'
                 AND subjects.subject_name = ? COLLATE NOCASE
                 AND quizzes.tutor_id = COALESCE(?, quizzes.tutor_id)
                 AND (tutor_subjects.tutor_id IS NOT NULL OR EXISTS (
                     SELECT 1 FROM quiz_assignments AS assigned
                     WHERE assigned.quiz_id = quizzes.id AND assigned.learner_id = ?
                 ))
               ORDER BY quizzes.published_at DESC, quizzes.id DESC""",
            (
                user_id,
                subject_name,
                int(tutor_id_filter) if tutor_id_filter else None,
                user_id,
            ),
        ).fetchall()
    conn.close()
    return jsonify(quizzes=[dict(row) for row in rows])


@quizzes_bp.get("/api/quizzes/<int:quiz_id>")
def get_quiz(quiz_id):
    conn = get_db()
    user_id, user = _current_user(conn)
    if not user_id:
        conn.close()
        return fail(401, "Not authenticated")
    if not user:
        conn.close()
        session.clear()
        return fail(401, "Not authenticated")
    quiz = conn.execute(
        """SELECT quizzes.*, resources.title AS resource_title,
                  subjects.subject_name, tutor.full_name AS tutor_name
           FROM quizzes
           JOIN learning_resources AS resources ON resources.id = quizzes.resource_id
           JOIN subjects ON subjects.id = resources.subject_id
           JOIN users AS tutor ON tutor.id = quizzes.tutor_id
           WHERE quizzes.id = ?""",
        (quiz_id,),
    ).fetchone()
    if not quiz:
        conn.close()
        return fail(404, "Quiz not found")
    tutor_view = user["role"] in ("tutor", "both") and quiz["tutor_id"] == user_id
    if not tutor_view and not _quiz_access(
        conn, quiz_id, user_id, request.args.get("subject", "").strip()
    ):
        conn.close()
        return fail(404, "Quiz not found")
    questions = conn.execute(
        """SELECT id, test_number, position, question, options_json,
                  answer_key, source_excerpt
           FROM quiz_questions WHERE quiz_id = ?
           ORDER BY test_number, position""",
        (quiz_id,),
    ).fetchall()
    payload = {
        "id": quiz["id"],
        "title": quiz["title"],
        "status": quiz["status"],
        "resource_id": quiz["resource_id"],
        "resource_title": quiz["resource_title"],
        "subject_name": quiz["subject_name"],
        "tutor_name": quiz["tutor_name"],
        "focus_monitoring_enabled": bool(quiz["focus_monitoring_enabled"]),
        "questions": _serialize_questions(questions, tutor_view),
    }
    conn.close()
    return jsonify(quiz=payload)


@quizzes_bp.post("/api/resources/<int:resource_id>/quizzes/generate")
def generate_resource_quiz(resource_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    data = _json_body()
    if not data:
        return fail(400, "Quiz generation must be a JSON object")
    counts, error = _parse_question_counts(data)
    if error:
        return fail(400, error)
    title = data.get("title")
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
        return fail(400, "Enter a quiz title of 120 characters or fewer")
    focus_monitoring = data.get("focus_monitoring_enabled", False)
    if type(focus_monitoring) is not bool:
        return fail(400, "Focus monitoring must be enabled or disabled")
    if data.get("external_ai_consent") is not True:
        return fail(
            400,
            "Confirm that the selected resource text will be sent to Google Gemini for quiz generation.",
        )
    conn = get_db()
    resource = _resource_for_tutor(conn, resource_id, user_id)
    conn.close()
    if not resource:
        return fail(404, "Resource not found")
    try:
        source_text = extract_source_text(resource)
        questions = generate_questions(source_text, counts[0], counts[1])
    except QuizGenerationError as error:
        return fail(error.status_code, str(error))
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            """INSERT INTO quizzes
               (tutor_id, resource_id, title, focus_monitoring_enabled)
               VALUES (?, ?, ?, ?)""",
            (user_id, resource_id, title.strip(), int(focus_monitoring)),
        )
        quiz_id = cursor.lastrowid
        _insert_questions(conn, quiz_id, questions)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify(
        id=quiz_id,
        message="Draft generated. Review every question and answer key before publishing.",
    ), 201


@quizzes_bp.post("/api/quizzes/<int:quiz_id>/regenerate")
def regenerate_quiz(quiz_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    data = _json_body()
    if not data:
        return fail(400, "Quiz regeneration must be a JSON object")
    counts, error = _parse_question_counts(data)
    if error:
        return fail(400, error)
    if data.get("external_ai_consent") is not True:
        return fail(400, "Confirm that the source text will be sent to Google Gemini.")
    conn = get_db()
    quiz = _quiz_for_tutor(conn, quiz_id, user_id)
    if not quiz:
        conn.close()
        return fail(404, "Quiz not found")
    if quiz["status"] != "draft":
        conn.close()
        return fail(409, "Only draft quizzes can be regenerated")
    resource = _resource_for_tutor(conn, quiz["resource_id"], user_id)
    conn.close()
    if not resource:
        return fail(404, "Source resource not found")
    try:
        questions = generate_questions(
            extract_source_text(resource),
            counts[0],
            counts[1],
        )
    except QuizGenerationError as error:
        return fail(error.status_code, str(error))
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "DELETE FROM quiz_questions WHERE quiz_id = ?",
            (quiz_id,),
        )
        _insert_questions(conn, quiz_id, questions)
        conn.execute(
            "UPDATE quizzes SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (quiz_id,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify(message="Draft regenerated. Review every question and answer key.")


@quizzes_bp.put("/api/quizzes/<int:quiz_id>")
def update_quiz_draft(quiz_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    data = _json_body()
    if not data:
        return fail(400, "Quiz update must be a JSON object")
    title = data.get("title")
    questions = data.get("questions")
    monitor = data.get("focus_monitoring_enabled", False)
    if not isinstance(title, str) or not title.strip() or len(title.strip()) > 120:
        return fail(400, "Enter a quiz title of 120 characters or fewer")
    if type(monitor) is not bool:
        return fail(400, "Focus monitoring must be enabled or disabled")
    if not isinstance(questions, list) or not 2 <= len(questions) <= 2 * MAX_QUESTIONS_PER_TEST:
        return fail(400, "Include 1 to 30 questions in each test")
    test_counts = {1: 0, 2: 0}
    normalized_questions = []
    conn = get_db()
    quiz = _quiz_for_tutor(conn, quiz_id, user_id)
    if not quiz:
        conn.close()
        return fail(404, "Quiz not found")
    if quiz["status"] != "draft":
        conn.close()
        return fail(409, "Only draft quizzes can be edited")
    resource = _resource_for_tutor(conn, quiz["resource_id"], user_id)
    conn.close()
    if not resource:
        return fail(404, "Source resource not found")
    try:
        source_text = extract_source_text(resource)
        for item in questions:
            if (
                not isinstance(item, dict)
                or isinstance(item.get("test_number"), bool)
                or item.get("test_number") not in (1, 2)
            ):
                raise ValueError("Choose Test 1 or Test 2 for every question")
            test_number = item["test_number"]
            test_counts[test_number] += 1
            if test_counts[test_number] > MAX_QUESTIONS_PER_TEST:
                raise ValueError("Each test may contain at most 30 questions")
            question = item.get("question")
            evidence = item.get("source_excerpt")
            if not isinstance(question, str) or not question.strip() or len(question) > 1000:
                raise ValueError("Each question must contain up to 1000 characters")
            if not isinstance(evidence, str) or len(evidence) > 1000:
                raise ValueError("Each question needs a source excerpt")
            if test_number == 1:
                options = item.get("options")
                answer = item.get("answer_key")
                if (
                    not isinstance(options, list)
                    or not 3 <= len(options) <= 5
                    or any(not isinstance(option, str) or not option.strip() for option in options)
                    or len({_normalized_text(option) for option in options}) != len(options)
                    or isinstance(answer, bool)
                    or not isinstance(answer, int)
                    or not 0 <= answer < len(options)
                ):
                    raise ValueError("Test 1 needs 3 to 5 distinct choices and a valid answer")
                answer_text = options[answer].strip()
                _require_source_evidence(answer_text, evidence, source_text)
                options_json = json.dumps([option.strip() for option in options])
                answer_key = str(answer)
            else:
                options_json = "[]"
                answer = item.get("answer_key")
                if not isinstance(answer, str) or not answer.strip() or len(answer) > 250:
                    raise ValueError("Test 2 needs a text answer")
                answer_key = answer.strip()
                _require_source_evidence(answer_key, evidence, source_text)
            normalized_questions.append(
                {
                    "test_number": test_number,
                    "position": test_counts[test_number],
                    "question": question.strip(),
                    "options_json": options_json,
                    "answer_key": answer_key,
                    "source_excerpt": evidence.strip(),
                }
            )
        if not test_counts[1] or not test_counts[2]:
            raise ValueError("Include at least one question in both tests")
    except QuizGenerationError as error:
        return fail(error.status_code, str(error))
    except ValueError as error:
        return fail(400, str(error))
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM quiz_questions WHERE quiz_id = ?", (quiz_id,))
        _insert_questions(conn, quiz_id, normalized_questions)
        conn.execute(
            """UPDATE quizzes
               SET title = ?, focus_monitoring_enabled = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND tutor_id = ? AND status = 'draft'""",
            (title.strip(), int(monitor), quiz_id, user_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return jsonify(message="Draft saved")


@quizzes_bp.post("/api/quizzes/<int:quiz_id>/publish")
def publish_quiz(quiz_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    conn = get_db()
    quiz = _quiz_for_tutor(conn, quiz_id, user_id)
    if not quiz:
        conn.close()
        return fail(404, "Quiz not found")
    if quiz["status"] != "draft":
        conn.close()
        return fail(409, "Quiz is already published")
    counts = conn.execute(
        """SELECT test_number, COUNT(*) AS count
           FROM quiz_questions WHERE quiz_id = ? GROUP BY test_number""",
        (quiz_id,),
    ).fetchall()
    if {row["test_number"] for row in counts} != {1, 2}:
        conn.close()
        return fail(400, "Review and save at least one question in each test before publishing")
    conn.execute(
        """UPDATE quizzes SET status = 'published',
                  published_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND tutor_id = ?""",
        (quiz_id, user_id),
    )
    conn.commit()
    conn.close()
    return jsonify(message="Quiz published")


@quizzes_bp.delete("/api/quizzes/<int:quiz_id>")
def discard_quiz(quiz_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    conn = get_db()
    quiz = _quiz_for_tutor(conn, quiz_id, user_id)
    if not quiz:
        conn.close()
        return fail(404, "Quiz not found")
    if quiz["status"] != "draft":
        conn.close()
        return fail(409, "Published quizzes cannot be discarded")
    conn.execute("DELETE FROM quizzes WHERE id = ?", (quiz_id,))
    conn.commit()
    conn.close()
    return jsonify(message="Draft discarded")


@quizzes_bp.post("/api/quizzes/<int:quiz_id>/assign")
def assign_quiz(quiz_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    data = _json_body()
    learner_id = data.get("learner_id") if data else None
    if isinstance(learner_id, bool) or not isinstance(learner_id, int) or learner_id < 1:
        return fail(400, "A valid learner_id is required")
    conn = get_db()
    quiz = _quiz_for_tutor(conn, quiz_id, user_id)
    if not quiz:
        conn.close()
        return fail(404, "Quiz not found")
    if quiz["status"] != "published":
        conn.close()
        return fail(409, "Publish the quiz before assigning it")
    learner = conn.execute(
        "SELECT role FROM users WHERE id = ?", (learner_id,)
    ).fetchone()
    if (
        learner_id == user_id
        or not learner
        or learner["role"] not in ("learner", "both")
    ):
        conn.close()
        return fail(404, "Learner not found")
    conn.execute(
        """INSERT INTO quiz_assignments (quiz_id, learner_id)
           VALUES (?, ?) ON CONFLICT(quiz_id, learner_id) DO NOTHING""",
        (quiz_id, learner_id),
    )
    conn.commit()
    conn.close()
    return jsonify(message="Quiz assigned")


@quizzes_bp.get("/api/quizzes/<int:quiz_id>/attempts")
def list_quiz_attempts(quiz_id):
    conn = get_db()
    user_id, user = _current_user(conn)
    if not user_id:
        conn.close()
        return fail(401, "Not authenticated")
    if not user:
        conn.close()
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        conn.close()
        return fail(403, "Tutor access required")
    quiz = _quiz_for_tutor(conn, quiz_id, user_id)
    if not quiz:
        conn.close()
        return fail(404, "Quiz not found")
    attempts = conn.execute(
        """SELECT attempts.id, attempts.learner_id, learner.full_name AS learner_name,
                  attempts.started_at, attempts.submitted_at, attempts.score,
                  attempts.total_questions, attempts.grading_method,
                  attempts.focus_monitoring_enabled,
                  attempts.focus_consent, attempts.answers_json
           FROM quiz_attempts AS attempts
           JOIN users AS learner ON learner.id = attempts.learner_id
           WHERE attempts.quiz_id = ?
           ORDER BY attempts.started_at DESC, attempts.id DESC""",
        (quiz_id,),
    ).fetchall()
    payload = []
    for attempt in attempts:
        events = conn.execute(
            """SELECT event_type, occurred_at FROM quiz_focus_events
               WHERE attempt_id = ? ORDER BY occurred_at, id""",
            (attempt["id"],),
        ).fetchall()
        payload.append(
            {
                "id": attempt["id"],
                "learner_id": attempt["learner_id"],
                "learner_name": attempt["learner_name"],
                "started_at": attempt["started_at"],
                "submitted_at": attempt["submitted_at"],
                "score": attempt["score"],
                "total_questions": attempt["total_questions"],
                "grading_method": attempt["grading_method"],
                "focus_monitoring_enabled": bool(attempt["focus_monitoring_enabled"]),
                "focus_consent": bool(attempt["focus_consent"]),
                "answers": json.loads(attempt["answers_json"]),
                "focus_events": [dict(event) for event in events],
            }
        )
    conn.close()
    return jsonify(attempts=payload)


@quizzes_bp.post("/api/quizzes/<int:quiz_id>/attempts")
def start_quiz_attempt(quiz_id):
    data = _json_body()
    if not data:
        return fail(400, "Quiz attempt must be a JSON object")
    consent = data.get("focus_consent")
    if consent is not None and type(consent) is not bool:
        return fail(400, "Focus-monitoring consent must be true or false")
    user_id = session.get("uid")
    if not user_id:
        return fail(401, "Not authenticated")
    conn = get_db()
    user = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("learner", "both"):
        conn.close()
        return fail(403, "Learner access required")
    quiz = _quiz_access(
        conn, quiz_id, user_id, request.args.get("subject", "").strip()
    )
    if not quiz:
        conn.close()
        return fail(404, "Quiz not found")
    monitoring = bool(quiz["focus_monitoring_enabled"] and consent is True)
    if quiz["focus_monitoring_enabled"] and consent is None:
        conn.close()
        return fail(400, "Choose whether to allow focus-event monitoring before starting")
    cursor = conn.execute(
        """INSERT INTO quiz_attempts
           (quiz_id, learner_id, focus_monitoring_enabled, focus_consent)
           VALUES (?, ?, ?, ?)""",
        (quiz_id, user_id, int(monitoring), int(monitoring)),
    )
    attempt_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return jsonify(
        attempt_id=attempt_id,
        focus_monitoring_enabled=monitoring,
        message=(
            "This attempt is unmonitored."
            if not monitoring
            else "Focus changes may be recorded for tutor review only; they are not proof of misconduct."
        ),
    ), 201


@quizzes_bp.post("/api/quiz-attempts/<int:attempt_id>/focus-events")
def record_focus_events(attempt_id):
    data = _json_body()
    events = data.get("events") if data else None
    if not isinstance(events, list) or not events or len(events) > MAX_FOCUS_EVENTS_PER_REQUEST:
        return fail(400, "Provide between 1 and 30 focus events")
    user_id = session.get("uid")
    if not user_id:
        return fail(401, "Not authenticated")
    conn = get_db()
    attempt = conn.execute(
        """SELECT id FROM quiz_attempts
           WHERE id = ? AND learner_id = ? AND submitted_at IS NULL
             AND focus_monitoring_enabled = 1 AND focus_consent = 1""",
        (attempt_id, user_id),
    ).fetchone()
    if not attempt:
        conn.close()
        return fail(404, "Monitored quiz attempt not found")
    existing_count = conn.execute(
        "SELECT COUNT(*) FROM quiz_focus_events WHERE attempt_id = ?",
        (attempt_id,),
    ).fetchone()[0]
    if existing_count + len(events) > MAX_FOCUS_EVENTS_PER_ATTEMPT:
        conn.close()
        return fail(400, "This quiz attempt has reached the focus-event limit")
    normalized_events = []
    now = datetime.now(timezone.utc)
    for event in events:
        if not isinstance(event, dict) or event.get("event_type") not in ("hidden", "blur"):
            conn.close()
            return fail(400, "Focus events may only be hidden or blur")
        normalized_events.append(
            (
                attempt_id,
                event["event_type"],
                now.isoformat(timespec="seconds"),
            )
        )
    conn.executemany(
        """INSERT INTO quiz_focus_events (attempt_id, event_type, occurred_at)
           VALUES (?, ?, ?)""",
        normalized_events,
    )
    conn.commit()
    conn.close()
    return jsonify(recorded=len(normalized_events))


@quizzes_bp.get("/api/session-requests/<int:session_request_id>/quiz-runs")
def list_session_quiz_runs(session_request_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    conn = get_db()
    session_request = _session_request_for_participant(conn, session_request_id, user_id)
    if not session_request:
        conn.close()
        return fail(404, "Accepted session not found")
    is_tutor = session_request["tutor_id"] == user_id
    rows = conn.execute(
        """SELECT runs.id, runs.quiz_id, runs.attempt_id, runs.status,
                  runs.learner_ready, runs.created_at, quizzes.title
           FROM session_quiz_runs AS runs
           JOIN quizzes ON quizzes.id = runs.quiz_id
           WHERE runs.session_request_id = ?
           ORDER BY runs.created_at, runs.id""",
        (session_request_id,),
    ).fetchall()
    runs = []
    for row in rows:
        payload = dict(row)
        payload["is_tutor"] = is_tutor
        if row["attempt_id"]:
            attempt = conn.execute(
                """SELECT submitted_at, score, total_questions, grading_method,
                          focus_monitoring_enabled, focus_consent
                   FROM quiz_attempts WHERE id = ?""",
                (row["attempt_id"],),
            ).fetchone()
            if attempt:
                payload["submitted_at"] = attempt["submitted_at"]
                payload["focus_monitoring_enabled"] = bool(attempt["focus_monitoring_enabled"])
                payload["focus_consent"] = bool(attempt["focus_consent"])
                if is_tutor or row["status"] == "released":
                    payload["score"] = attempt["score"]
                    payload["total_questions"] = attempt["total_questions"]
                    payload["grading_method"] = attempt["grading_method"]
                event_rows = conn.execute(
                    """SELECT event_type, occurred_at FROM quiz_focus_events
                       WHERE attempt_id = ? ORDER BY occurred_at, id""",
                    (row["attempt_id"],),
                ).fetchall()
                payload["focus_events"] = [dict(event) for event in event_rows] if is_tutor else []
        runs.append(payload)
    conn.close()
    response = jsonify(runs=runs, is_tutor=is_tutor)
    response.headers["Cache-Control"] = "no-store"
    return response


@quizzes_bp.post("/api/session-requests/<int:session_request_id>/quiz-runs")
def create_session_quiz_run(session_request_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user or user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    data = _json_body()
    quiz_id = data.get("quiz_id") if data else None
    if isinstance(quiz_id, bool) or not isinstance(quiz_id, int) or quiz_id < 1:
        return fail(400, "Choose a reviewed quiz draft")
    conn = get_db()
    session_request = _session_request_for_participant(conn, session_request_id, user_id)
    if not session_request or session_request["tutor_id"] != user_id:
        conn.close()
        return fail(404, "Accepted session not found")
    quiz = conn.execute(
        """SELECT quizzes.id, quizzes.status, resources.subject_id
           FROM quizzes JOIN learning_resources AS resources ON resources.id = quizzes.resource_id
           WHERE quizzes.id = ? AND quizzes.tutor_id = ?""",
        (quiz_id, user_id),
    ).fetchone()
    if not quiz or quiz["status"] != "draft":
        conn.close()
        return fail(404, "Reviewed quiz draft not found")
    if (
        session_request["requested_subject_id"] is not None
        and quiz["subject_id"] != session_request["requested_subject_id"]
    ):
        conn.close()
        return fail(400, "Use learning material for this session's subject")
    if not conn.execute(
        "SELECT 1 FROM quiz_questions WHERE quiz_id = ? GROUP BY quiz_id HAVING COUNT(*) > 0",
        (quiz_id,),
    ).fetchone():
        conn.close()
        return fail(400, "Review and save the quiz questions before preparing it")
    try:
        cursor = conn.execute(
            """INSERT INTO session_quiz_runs (session_request_id, quiz_id)
               VALUES (?, ?)""",
            (session_request_id, quiz_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return fail(409, "This quiz is already prepared for the session")
    conn.close()
    return jsonify(id=cursor.lastrowid, status="waiting", message="Quiz prepared for the session"), 201


@quizzes_bp.post("/api/session-quiz-runs/<int:run_id>/ready")
def mark_session_quiz_ready(run_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user or user["role"] not in ("learner", "both"):
        return fail(403, "Learner access required")
    data = _json_body()
    if (
        not data
        or type(data.get("focus_consent")) is not bool
        or type(data.get("ai_grading_consent")) is not bool
    ):
        return fail(400, "Choose your monitoring and AI-grading preferences")
    conn = get_db()
    run = _session_quiz_run_access(conn, run_id, user_id)
    if not run or run["learner_id"] != user_id:
        conn.close()
        return fail(404, "Session quiz not found")
    if run["status"] != "waiting":
        conn.close()
        return fail(409, "This quiz is no longer waiting for learner readiness")
    conn.execute(
        """UPDATE session_quiz_runs
           SET learner_ready = 1, focus_consent = ?, ai_grading_consent = ?,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = ? AND status = 'waiting'""",
        (int(data["focus_consent"]), int(data["ai_grading_consent"]), run_id),
    )
    conn.commit()
    conn.close()
    return jsonify(message="The tutor has been notified that you are ready")


@quizzes_bp.post("/api/session-quiz-runs/<int:run_id>/start")
def start_session_quiz_run(run_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user or user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        run = _session_quiz_run_access(conn, run_id, user_id)
        if not run or run["tutor_id"] != user_id:
            conn.rollback()
            return fail(404, "Session quiz not found")
        if run["status"] != "waiting" or not run["learner_ready"]:
            conn.rollback()
            return fail(409, "Wait until the learner marks themselves ready")
        another_live_run = conn.execute(
            """SELECT 1 FROM session_quiz_runs
               WHERE session_request_id = ? AND id != ?
                 AND status IN ('started', 'submitted') LIMIT 1""",
            (run["session_request_id"], run_id),
        ).fetchone()
        if another_live_run:
            conn.rollback()
            return fail(409, "Share the current quiz result before starting another quiz")
        counts = conn.execute(
            """SELECT test_number, COUNT(*) AS count FROM quiz_questions
               WHERE quiz_id = ? GROUP BY test_number""",
            (run["quiz_id"],),
        ).fetchall()
        if {row["test_number"] for row in counts} != {1, 2}:
            conn.rollback()
            return fail(409, "The reviewed quiz must include questions from both tests")
        quiz = conn.execute(
            "SELECT focus_monitoring_enabled FROM quizzes WHERE id = ?",
            (run["quiz_id"],),
        ).fetchone()
        focus_consent = bool(run["focus_consent"] and quiz["focus_monitoring_enabled"])
        cursor = conn.execute(
            """INSERT INTO quiz_attempts
               (quiz_id, learner_id, focus_monitoring_enabled, focus_consent,
                ai_grading_consent)
               VALUES (?, ?, ?, ?, ?)""",
            (
                run["quiz_id"], run["learner_id"], int(focus_consent),
                int(focus_consent), run["ai_grading_consent"],
            ),
        )
        conn.execute(
            """UPDATE session_quiz_runs
               SET attempt_id = ?, status = 'started', updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (cursor.lastrowid, run_id),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return fail(409, "This session quiz could not be started")
    finally:
        conn.close()
    return jsonify(attempt_id=cursor.lastrowid, status="started")


@quizzes_bp.get("/api/session-quiz-runs/<int:run_id>/quiz")
def get_session_quiz_for_learner(run_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user or user["role"] not in ("learner", "both"):
        return fail(403, "Learner access required")
    conn = get_db()
    run = _session_quiz_run_access(conn, run_id, user_id)
    if not run or run["learner_id"] != user_id or run["status"] != "started":
        conn.close()
        return fail(404, "Started session quiz not found")
    quiz = conn.execute(
        """SELECT quizzes.title, resources.title AS resource_title,
                  subjects.subject_name
           FROM quizzes JOIN learning_resources AS resources ON resources.id = quizzes.resource_id
           JOIN subjects ON subjects.id = resources.subject_id
           WHERE quizzes.id = ?""",
        (run["quiz_id"],),
    ).fetchone()
    questions = conn.execute(
        """SELECT id, test_number, position, question, options_json
           FROM quiz_questions WHERE quiz_id = ? ORDER BY test_number, position""",
        (run["quiz_id"],),
    ).fetchall()
    attempt = conn.execute(
        "SELECT focus_monitoring_enabled FROM quiz_attempts WHERE id = ?",
        (run["attempt_id"],),
    ).fetchone()
    conn.close()
    response = jsonify(
        quiz={
            "title": quiz["title"],
            "resource_title": quiz["resource_title"],
            "subject_name": quiz["subject_name"],
            "focus_monitoring_enabled": bool(attempt and attempt["focus_monitoring_enabled"]),
            "attempt_id": run["attempt_id"],
            "questions": [
                {
                    "id": question["id"],
                    "test_number": question["test_number"],
                    "position": question["position"],
                    "question": question["question"],
                    "options": json.loads(question["options_json"]),
                }
                for question in questions
            ],
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@quizzes_bp.post("/api/session-quiz-runs/<int:run_id>/release")
def release_session_quiz_result(run_id):
    user_id, user = _current_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user or user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    conn = get_db()
    run = _session_quiz_run_access(conn, run_id, user_id)
    if not run or run["tutor_id"] != user_id:
        conn.close()
        return fail(404, "Session quiz not found")
    if run["status"] != "submitted":
        conn.close()
        return fail(409, "The learner must submit the quiz before sharing its result")
    conn.execute(
        """UPDATE session_quiz_runs
           SET status = 'released', updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (run_id,),
    )
    conn.commit()
    conn.close()
    return jsonify(message="Quiz result shared with the learner")


@quizzes_bp.post("/api/quiz-attempts/<int:attempt_id>/submit")
def submit_quiz_attempt(attempt_id):
    data = _json_body()
    answers = data.get("answers") if data else None
    if not isinstance(answers, dict):
        return fail(400, "Answers must be a JSON object")
    user_id = session.get("uid")
    if not user_id:
        return fail(401, "Not authenticated")
    conn = get_db()
    attempt = conn.execute(
        """SELECT id, quiz_id, submitted_at, ai_grading_consent FROM quiz_attempts
           WHERE id = ? AND learner_id = ?""",
        (attempt_id, user_id),
    ).fetchone()
    if not attempt:
        conn.close()
        return fail(404, "Quiz attempt not found")
    if attempt["submitted_at"]:
        conn.close()
        return fail(409, "This quiz attempt has already been submitted")
    questions = conn.execute(
        """SELECT id, test_number, options_json, answer_key, question, source_excerpt
           FROM quiz_questions WHERE quiz_id = ? ORDER BY test_number, position""",
        (attempt["quiz_id"],),
    ).fetchall()
    question_ids = {str(question["id"]) for question in questions}
    if any(key not in question_ids for key in answers):
        conn.close()
        return fail(400, "Answers contain a question that is not part of this quiz")
    normalized_answers = {}
    score = 0
    exact_identification = {}
    for question in questions:
        answer = answers.get(str(question["id"]), "")
        if question["test_number"] == 1:
            options = json.loads(question["options_json"])
            if isinstance(answer, bool) or not isinstance(answer, int) or not 0 <= answer < len(options):
                if answer not in ("", None):
                    conn.close()
                    return fail(400, "Multiple-choice answers must select a listed option")
                normalized_answers[str(question["id"])] = None
                continue
            normalized_answers[str(question["id"])] = answer
            if str(answer) == question["answer_key"]:
                score += 1
        else:
            if answer is None:
                answer = ""
            if not isinstance(answer, str):
                conn.close()
                return fail(400, "Identification answers must be text")
            answer = answer.strip()
            normalized_answers[str(question["id"])] = answer
            exact_identification[str(question["id"])] = (
                _normalized_text(answer) == _normalized_text(question["answer_key"])
            )
            if exact_identification[str(question["id"] )]:
                score += 1
    ai_grades = (
        _grade_short_answers_with_gemini(questions, normalized_answers)
        if attempt["ai_grading_consent"]
        else None
    )
    grading_method = "gemini" if ai_grades is not None else "answer_key"
    if ai_grades is not None:
        for question_id, correct in ai_grades.items():
            score += int(correct) - int(exact_identification.get(question_id, False))
    conn.execute(
        """UPDATE quiz_attempts
           SET answers_json = ?, submitted_at = CURRENT_TIMESTAMP,
               score = ?, total_questions = ?, grading_method = ?
           WHERE id = ? AND submitted_at IS NULL""",
        (
            json.dumps(normalized_answers),
            score,
            len(questions),
            grading_method,
            attempt_id,
        ),
    )
    session_run = conn.execute(
        "SELECT id FROM session_quiz_runs WHERE attempt_id = ? AND status = 'started'",
        (attempt_id,),
    ).fetchone()
    if session_run:
        conn.execute(
            """UPDATE session_quiz_runs
               SET status = 'submitted', updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
            (session_run["id"],),
        )
    conn.commit()
    result = conn.execute(
        """SELECT submitted_at, score, total_questions
           FROM quiz_attempts WHERE id = ?""",
        (attempt_id,),
    ).fetchone()
    conn.close()
    if session_run:
        return jsonify(
            submitted_at=result["submitted_at"],
            awaiting_tutor=True,
            message="Submitted. Your tutor will review and share the result.",
        )
    return jsonify(
        submitted_at=result["submitted_at"],
        score=result["score"],
        total_questions=result["total_questions"],
        message="Your score is available. The answer key is only available to the tutor.",
    )


@quizzes_bp.get("/api/quiz-attempts/<int:attempt_id>")
def get_quiz_attempt_result(attempt_id):
    user_id = session.get("uid")
    if not user_id:
        return fail(401, "Not authenticated")
    conn = get_db()
    result = conn.execute(
        """SELECT attempts.submitted_at, attempts.score, attempts.total_questions,
                  attempts.grading_method, runs.status AS session_run_status
           FROM quiz_attempts AS attempts
           LEFT JOIN session_quiz_runs AS runs ON runs.attempt_id = attempts.id
           WHERE attempts.id = ? AND attempts.learner_id = ?""",
        (attempt_id, user_id),
    ).fetchone()
    conn.close()
    if not result:
        return fail(404, "Quiz result not found")
    if not result["submitted_at"]:
        return fail(409, "Submit the quiz before viewing results")
    if result["session_run_status"] and result["session_run_status"] != "released":
        return fail(409, "Your tutor has not shared this result yet")
    return jsonify(
        submitted_at=result["submitted_at"],
        score=result["score"],
        total_questions=result["total_questions"],
        grading_method=result["grading_method"],
    )
