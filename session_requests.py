import sqlite3
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, session

from db import get_db
from notifications import send_push_to_user, send_sms_to_user

session_requests_bp = Blueprint("session_requests", __name__)
MAX_REQUEST_NOTE_LENGTH = 500


def fail(code, message):
    return jsonify(error=message), code


def _slot_datetime(value, timezone_offset_minutes=None):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if timezone_offset_minutes is None:
            return parsed.replace(second=0, microsecond=0)
        parsed = parsed.replace(
            tzinfo=timezone(timedelta(minutes=-timezone_offset_minutes))
        )
    return parsed.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _slot_is_booked(
    conn, tutor_id, scheduled_for, timezone_offset_minutes=None,
    exclude_request_id=None,
):
    query = """SELECT id, scheduled_for, timezone_offset_minutes FROM session_requests
               WHERE tutor_id = ?
                 AND status = 'accepted' AND scheduled_for IS NOT NULL"""
    params = [tutor_id]
    if exclude_request_id is not None:
        query += " AND id != ?"
        params.append(exclude_request_id)
    rows = conn.execute(query, params).fetchall()
    target = _slot_datetime(scheduled_for, timezone_offset_minutes)
    return any(
        _slot_datetime(
            row["scheduled_for"],
            row["timezone_offset_minutes"]
            if row["timezone_offset_minutes"] is not None
            else timezone_offset_minutes,
        ) == target
        for row in rows
    )


@session_requests_bp.get("/api/tutors/<int:tutor_id>/session-slots")
def list_tutor_session_slots(tutor_id):
    subject_id_value = request.args.get("subject_id", "")
    if not subject_id_value.isdecimal() or len(subject_id_value) > 18:
        return fail(400, "Choose a subject this tutor teaches")
    subject_id = int(subject_id_value)
    offset_value = request.args.get("timezone_offset_minutes", "0")
    try:
        timezone_offset_minutes = int(offset_value)
    except ValueError:
        return fail(400, "Invalid timezone offset")
    if not -840 <= timezone_offset_minutes <= 840:
        return fail(400, "Invalid timezone offset")
    conn = get_db()
    tutor = conn.execute(
        "SELECT role FROM users WHERE id = ? AND role IN ('tutor', 'both')",
        (tutor_id,),
    ).fetchone()
    subject = conn.execute(
        """SELECT 1 FROM tutor_subjects
           WHERE tutor_id = ? AND subject_id = ?""",
        (tutor_id, subject_id),
    ).fetchone()
    if not tutor or not subject:
        conn.close()
        return fail(404, "Tutor subject not found")
    slots = conn.execute(
        """SELECT id, day, start_time, end_time FROM availability
           WHERE tutor_id = ? AND subject_id = ?
           ORDER BY day, start_time""",
        (tutor_id, subject_id),
    ).fetchall()
    booked_rows = conn.execute(
        """SELECT scheduled_for, timezone_offset_minutes FROM session_requests
           WHERE tutor_id = ?
             AND status = 'accepted' AND scheduled_for IS NOT NULL""",
        (tutor_id,),
    ).fetchall()
    booked_times = {
        value for row in booked_rows
        if (value := _slot_datetime(
            row["scheduled_for"],
            row["timezone_offset_minutes"]
            if row["timezone_offset_minutes"] is not None
            else timezone_offset_minutes,
        )) is not None
    }
    available_slots = []
    local_now = datetime.now(timezone.utc).astimezone(
        timezone(timedelta(minutes=-timezone_offset_minutes))
    ).replace(tzinfo=None)
    today = local_now.date()
    for offset in range(91):
        occurrence = today + timedelta(days=offset)
        weekday = occurrence.strftime("%A")
        for slot in slots:
            if slot["day"] != weekday:
                continue
            scheduled_for = f"{occurrence.isoformat()}T{slot['start_time']}"
            local_slot = datetime.combine(
                occurrence, datetime.strptime(slot["start_time"], "%H:%M").time()
            )
            if local_slot <= local_now:
                continue
            slot_utc = _slot_datetime(scheduled_for, timezone_offset_minutes)
            available_slots.append(
                {
                    "availability_id": slot["id"],
                    "date": occurrence.isoformat(),
                    "day": weekday,
                    "start_time": slot["start_time"],
                    "end_time": slot["end_time"],
                    "scheduled_for": scheduled_for,
                    "available": slot_utc not in booked_times,
                }
            )
    conn.close()
    response = jsonify(slots=available_slots)
    response.headers["Cache-Control"] = "no-store"
    return response


@session_requests_bp.get("/session-requests")
def list_session_requests():
    user_id = session.get("uid")
    if not user_id:
        return fail(401, "Not authenticated")
    conn = get_db()
    rows = conn.execute(
        """SELECT session_requests.id, session_requests.tutor_id,
                  session_requests.learner_id, session_requests.requested_subject_id,
                  request_subject.subject_name AS requested_subject_name,
                  session_requests.scheduled_for,
                  session_requests.note, session_requests.status,
                  session_requests.created_at, session_requests.updated_at,
                  tutor.full_name AS tutor_name, learner.full_name AS learner_name,
                  learner.profile_photo AS learner_profile_photo
           FROM session_requests
           LEFT JOIN subjects AS request_subject
             ON request_subject.id = session_requests.requested_subject_id
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
    subject_id = data.get("subject_id")
    if isinstance(subject_id, bool) or not isinstance(subject_id, int) or subject_id < 1:
        return fail(400, "Choose a subject this tutor teaches")
    availability_id = data.get("availability_id")
    if isinstance(availability_id, bool) or not isinstance(availability_id, int) or availability_id < 1:
        return fail(400, "Choose an available date and time")
    scheduled_for = data.get("scheduled_for")
    timezone_offset_minutes = data.get("timezone_offset_minutes")
    if (
        not isinstance(scheduled_for, str)
        or isinstance(timezone_offset_minutes, bool)
        or not isinstance(timezone_offset_minutes, int)
        or not -840 <= timezone_offset_minutes <= 840
    ):
        return fail(400, "Choose an available date and time")
    try:
        local_scheduled_for = datetime.fromisoformat(scheduled_for)
    except ValueError:
        return fail(400, "Choose a valid date and time")
    if local_scheduled_for.strftime("%Y-%m-%dT%H:%M") != scheduled_for:
        return fail(400, "Choose a valid date and time")
    request_timezone = timezone(timedelta(minutes=-timezone_offset_minutes))
    scheduled_utc = local_scheduled_for.replace(tzinfo=request_timezone).astimezone(timezone.utc)
    if scheduled_utc <= datetime.now(timezone.utc):
        return fail(400, "Choose a date and time in the future")
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
        subject = conn.execute(
            """SELECT subjects.subject_name FROM tutor_subjects
               JOIN subjects ON subjects.id = tutor_subjects.subject_id
               WHERE tutor_subjects.tutor_id = ? AND tutor_subjects.subject_id = ?""",
            (tutor_id, subject_id),
        ).fetchone()
        if not subject:
            return fail(400, "Choose a subject this tutor teaches")
        availability = conn.execute(
            """SELECT day, start_time, end_time FROM availability
               WHERE id = ? AND tutor_id = ? AND subject_id = ?""",
            (availability_id, tutor_id, subject_id),
        ).fetchone()
        if (
            not availability
            or local_scheduled_for.strftime("%A") != availability["day"]
            or local_scheduled_for.strftime("%H:%M") != availability["start_time"]
        ):
            return fail(400, "Choose a date and time matching this tutor's subject availability")
        conn.execute("BEGIN IMMEDIATE")
        existing_request = conn.execute(
            """SELECT 1 FROM session_requests
               WHERE tutor_id = ? AND learner_id = ? AND status = 'pending'
               LIMIT 1""",
            (tutor_id, learner_id),
        ).fetchone()
        if existing_request:
            conn.rollback()
            return fail(409, "You already have a pending request with this tutor")
        if _slot_is_booked(
            conn, tutor_id, scheduled_for,
            timezone_offset_minutes=timezone_offset_minutes,
        ):
            conn.rollback()
            return fail(409, "That session time has already been booked. Choose another date.")
        cursor = conn.execute(
            """INSERT INTO session_requests
               (tutor_id, learner_id, requested_subject_id, scheduled_for,
                timezone_offset_minutes, note)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (tutor_id, learner_id, subject_id, scheduled_for,
             timezone_offset_minutes, note),
        )
        conn.commit()
        request_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        return fail(409, "You already have a pending request with this tutor")
    finally:
        conn.close()
    response = jsonify(
        id=request_id,
        status="pending",
        scheduled_for=scheduled_for,
        availability_id=availability_id,
        subject_id=subject_id,
        subject_name=subject["subject_name"],
    )
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
        if status == "accepted":
            conn.execute("BEGIN IMMEDIATE")
            request_details = conn.execute(
                """SELECT tutor_id, requested_subject_id, scheduled_for,
                          timezone_offset_minutes
                   FROM session_requests WHERE id = ?""",
                (request_id,),
            ).fetchone()
            if (
                request_details["scheduled_for"]
                and request_details["requested_subject_id"]
            ):
                scheduled_local = datetime.fromisoformat(
                    request_details["scheduled_for"].replace("Z", "+00:00")
                )
                if scheduled_local.tzinfo is not None:
                    offset = request_details["timezone_offset_minutes"]
                    target_zone = timezone(timedelta(minutes=-(offset or 0)))
                    scheduled_local = scheduled_local.astimezone(target_zone).replace(tzinfo=None)
                matching_slot = conn.execute(
                    """SELECT 1 FROM availability
                       WHERE tutor_id = ? AND subject_id = ? AND day = ? AND start_time = ?""",
                    (
                        request_details["tutor_id"],
                        request_details["requested_subject_id"],
                        scheduled_local.strftime("%A"),
                        scheduled_local.strftime("%H:%M"),
                    ),
                ).fetchone()
                if not matching_slot:
                    conn.rollback()
                    return fail(409, "The tutor's availability for this date has changed. Decline this request so the learner can choose another date.")
            if (
                request_details["scheduled_for"]
                and request_details["requested_subject_id"]
                and _slot_is_booked(
                    conn,
                    request_details["tutor_id"],
                    request_details["scheduled_for"],
                    timezone_offset_minutes=request_details["timezone_offset_minutes"],
                    exclude_request_id=request_id,
                )
            ):
                conn.rollback()
                return fail(409, "This session time has already been booked. Decline this request so the learner can choose another date.")
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
