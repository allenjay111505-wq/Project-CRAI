from datetime import datetime, timezone
import sqlite3

from flask import Blueprint, jsonify, request, session

from db import get_db
from notifications import send_push_to_user, send_sms_to_user

session_requests_bp = Blueprint("session_requests", __name__)
MAX_REQUEST_NOTE_LENGTH = 500


def fail(code, message):
    return jsonify(error=message), code


def parse_scheduled_for(value):
    if not isinstance(value, str):
        return None
    try:
        scheduled_for = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if scheduled_for.tzinfo is None:
        return None
    if scheduled_for <= datetime.now(timezone.utc):
        return None
    return scheduled_for.astimezone(timezone.utc).isoformat(timespec="minutes")


@session_requests_bp.get("/session-requests")
def list_session_requests():
    user_id = session.get("uid")
    if not user_id:
        return fail(401, "Not authenticated")
    conn = get_db()
    rows = conn.execute(
        """SELECT session_requests.id, session_requests.tutor_id,
                  session_requests.learner_id, session_requests.scheduled_for,
                  session_requests.note, session_requests.status,
                  session_requests.created_at, session_requests.updated_at,
                  tutor.full_name AS tutor_name, learner.full_name AS learner_name,
                  learner.profile_photo AS learner_profile_photo
           FROM session_requests
           JOIN users AS tutor ON tutor.id = session_requests.tutor_id
           JOIN users AS learner ON learner.id = session_requests.learner_id
           WHERE session_requests.tutor_id = ? OR session_requests.learner_id = ?
           ORDER BY session_requests.created_at DESC, session_requests.id DESC""",
        (user_id, user_id),
    ).fetchall()
    conn.close()
    requests_payload = []
    for row in rows:
        request_data = dict(row)
        request_data["learner_has_profile_photo"] = (
            request_data.pop("learner_profile_photo") is not None
        )
        request_data["direction"] = (
            "received" if row["tutor_id"] == user_id else "sent"
        )
        requests_payload.append(request_data)
    response = jsonify(requests=requests_payload)
    response.headers["Cache-Control"] = "no-store"
    return response


@session_requests_bp.get("/session-requests/<int:request_id>/learner-profile")
def get_session_request_learner_profile(request_id):
    tutor_id = session.get("uid")
    if not tutor_id:
        return fail(401, "Not authenticated")
    conn = get_db()
    request_row = conn.execute(
        """SELECT learner.id, learner.full_name, learner.email,
                  learner.student_id, learner.course, learner.year_level,
                  learner.profile_photo
           FROM session_requests
           JOIN users AS learner ON learner.id = session_requests.learner_id
           WHERE session_requests.id = ? AND session_requests.tutor_id = ?""",
        (request_id, tutor_id),
    ).fetchone()
    conn.close()
    if not request_row:
        return fail(404, "Learner profile not found")
    profile = dict(request_row)
    profile["has_profile_photo"] = profile.pop("profile_photo") is not None
    response = jsonify(profile)
    response.headers["Cache-Control"] = "no-store"
    return response


@session_requests_bp.post("/session-requests")
def create_session_request():
    learner_id = session.get("uid")
    if not learner_id:
        return fail(401, "Not authenticated")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return fail(400, "Session request must be a JSON object")
    tutor_id = data.get("tutor_id")
    if isinstance(tutor_id, bool) or not isinstance(tutor_id, int) or tutor_id < 1:
        return fail(400, "A valid tutor_id is required")
    scheduled_for = parse_scheduled_for(data.get("scheduled_for"))
    if scheduled_for is None:
        return fail(400, "Choose a valid date and time in the future")
    note = data.get("note", "")
    if not isinstance(note, str):
        return fail(400, "Request note must be text")
    note = note.strip()
    if len(note) > MAX_REQUEST_NOTE_LENGTH:
        return fail(
            400,
            f"Request note must be {MAX_REQUEST_NOTE_LENGTH} characters or fewer",
        )

    conn = get_db()
    try:
        learner = conn.execute(
            "SELECT role FROM users WHERE id = ?", (learner_id,)
        ).fetchone()
        if not learner:
            session.clear()
            return fail(401, "Not authenticated")
        if learner["role"] not in ("learner", "both"):
            return fail(403, "Only learners can request a session")
        tutor = conn.execute(
            "SELECT role FROM users WHERE id = ?", (tutor_id,)
        ).fetchone()
        if not tutor or tutor["role"] not in ("tutor", "both"):
            return fail(404, "Tutor not found")
        if tutor_id == learner_id:
            return fail(400, "You cannot request a session with yourself")
        cursor = conn.execute(
            """INSERT INTO session_requests
               (tutor_id, learner_id, scheduled_for, note)
               VALUES (?, ?, ?, ?)""",
            (tutor_id, learner_id, scheduled_for, note),
        )
        conn.commit()
        request_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        return fail(409, "A pending request already exists for that time")
    finally:
        conn.close()
    response = jsonify(id=request_id, status="pending", scheduled_for=scheduled_for)
    response.status_code = 201
    response.headers["Cache-Control"] = "no-store"
    send_push_to_user(
        tutor_id, "New session request", "A learner sent you a session request."
    )
    send_sms_to_user(
        tutor_id, "A learner sent you a C.R.A.I. session request."
    )
    return response


@session_requests_bp.put("/session-requests/<int:request_id>")
def update_session_request(request_id):
    user_id = session.get("uid")
    if not user_id:
        return fail(401, "Not authenticated")
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or data.get("status") not in (
        "accepted",
        "declined",
        "cancelled",
    ):
        return fail(400, "Choose accepted, declined, or cancelled")
    status = data["status"]

    conn = get_db()
    try:
        request_row = conn.execute(
            """SELECT tutor_id, learner_id, status FROM session_requests
               WHERE id = ?""",
            (request_id,),
        ).fetchone()
        if not request_row:
            return fail(404, "Session request not found")
        if status in ("accepted", "declined") and request_row["tutor_id"] != user_id:
            return fail(403, "Only the tutor can accept or decline this request")
        if status == "cancelled" and request_row["learner_id"] != user_id:
            return fail(403, "Only the learner can cancel this request")
        if request_row["status"] != "pending":
            return fail(409, "Only pending requests can be updated")
        cursor = conn.execute(
            """UPDATE session_requests
               SET status = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status = 'pending'""",
            (status, request_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return fail(409, "This request was already updated")
        conn.commit()
    finally:
        conn.close()
    response = jsonify(id=request_id, status=status)
    response.headers["Cache-Control"] = "no-store"
    recipient_id = request_row["learner_id"]
    send_push_to_user(
        recipient_id,
        "Session request update",
        f"Your C.R.A.I. session request was {status}.",
    )
    send_sms_to_user(
        recipient_id, f"Your C.R.A.I. session request was {status}."
    )
    return response
