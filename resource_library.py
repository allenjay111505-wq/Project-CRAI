import io
import os
import re
import uuid
import zipfile
from pathlib import PurePath

from flask import Blueprint, current_app, jsonify, request, send_file, session
from werkzeug.utils import secure_filename

from db import get_db

resource_library_bp = Blueprint("resource_library", __name__)
MAX_RESOURCE_SIZE = 5 * 1024 * 1024
MAX_DESCRIPTION_LENGTH = 2000
RESOURCE_TYPES = {
    ".txt": ("text/plain", "text"),
    ".md": ("text/markdown", "text"),
    ".pdf": ("application/pdf", "pdf"),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
}
STORAGE_KEY_RE = re.compile(r"^[0-9a-f]{32}\.(?:txt|md|pdf|docx)$")


def fail(code, message):
    return jsonify(error=message), code


def _logged_in_user():
    user_id = session.get("uid")
    if not user_id:
        return None, None
    conn = get_db()
    user = conn.execute("SELECT id, role FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return user_id, user


def _parse_metadata(data, partial=False):
    metadata = {}
    if not partial or "title" in data:
        title = data.get("title")
        if not isinstance(title, str) or not title.strip():
            return None, "A resource title is required"
        title = title.strip()
        if len(title) > 120:
            return None, "Resource title must be 120 characters or fewer"
        metadata["title"] = title
    if not partial or "description" in data:
        description = data.get("description", "")
        if not isinstance(description, str):
            return None, "Resource description must be text"
        description = description.strip()
        if len(description) > MAX_DESCRIPTION_LENGTH:
            return None, (
                f"Resource description must be {MAX_DESCRIPTION_LENGTH} characters or fewer"
            )
        metadata["description"] = description
    if "subject_id" in data or not partial:
        subject_id = data.get("subject_id")
        try:
            parsed_subject_id = int(subject_id)
        except (TypeError, ValueError):
            return None, "Select a subject you teach"
        if isinstance(subject_id, bool) or parsed_subject_id < 1:
            return None, "Select a subject you teach"
        metadata["subject_id"] = parsed_subject_id
    if "is_published" in data or not partial:
        published = data.get("is_published", "0")
        if isinstance(published, bool):
            is_published = published
        elif published in ("0", "1"):
            is_published = published == "1"
        else:
            return None, "Choose whether to make the resource available to learners"
        metadata["is_published"] = int(is_published)
    return metadata, None


def _validate_upload(upload):
    if upload is None or not upload.filename:
        return None, "Choose a resource file to upload"
    safe_name = secure_filename(PurePath(upload.filename).name)
    extension = os.path.splitext(safe_name)[1].lower()
    resource_type = RESOURCE_TYPES.get(extension)
    if resource_type is None:
        return None, "Choose a TXT, Markdown, PDF, or DOCX file"
    contents = upload.stream.read(MAX_RESOURCE_SIZE + 1)
    if len(contents) > MAX_RESOURCE_SIZE:
        return None, "Resource files must be 5 MB or smaller"
    mime_type, content_kind = resource_type
    if content_kind == "text":
        try:
            contents.decode("utf-8")
        except UnicodeDecodeError:
            return None, "Text resources must use UTF-8 encoding"
        if b"\x00" in contents:
            return None, "Text resources cannot contain binary data"
    elif content_kind == "pdf" and not contents.startswith(b"%PDF-"):
        return None, "The file contents do not match a PDF document"
    elif content_kind == "docx":
        try:
            with zipfile.ZipFile(io.BytesIO(contents)) as document:
                document.getinfo("word/document.xml")
        except (KeyError, OSError, zipfile.BadZipFile):
            return None, "The file contents do not match a DOCX document"
    if not contents:
        return None, "The resource file is empty"
    storage_key = f"{uuid.uuid4().hex}{extension}"
    return {
        "contents": contents,
        "file_name": safe_name[:180] or f"resource{extension}",
        "file_type": mime_type,
        "file_size": len(contents),
        "storage_key": storage_key,
    }, None


def _resource_path(storage_key):
    if not isinstance(storage_key, str) or not STORAGE_KEY_RE.fullmatch(storage_key):
        raise ValueError("Invalid resource storage key")
    return os.path.join(current_app.config["RESOURCE_STORAGE_DIR"], storage_key)


def _write_resource(resource):
    directory = current_app.config["RESOURCE_STORAGE_DIR"]
    os.makedirs(directory, exist_ok=True)
    path = _resource_path(resource["storage_key"])
    with open(path, "xb") as stored_file:
        stored_file.write(resource["contents"])
    return path


def _select_resource_access(conn, resource_id, user_id, role, subject_name=None):
    resource = conn.execute(
        """SELECT resources.*, subjects.subject_name, tutor.full_name AS tutor_name
           FROM learning_resources AS resources
           JOIN subjects ON subjects.id = resources.subject_id
           JOIN users AS tutor ON tutor.id = resources.tutor_id
           WHERE resources.id = ?""",
        (resource_id,),
    ).fetchone()
    if not resource:
        return None
    if role in ("tutor", "both") and resource["tutor_id"] == user_id:
        return resource
    if role not in ("learner", "both") or not resource["is_published"]:
        return None
    if subject_name is None or resource["subject_name"].casefold() != subject_name.casefold():
        return None
    listed_subject = conn.execute(
        """SELECT 1 FROM tutor_subjects
           WHERE tutor_id = ? AND subject_id = ?""",
        (resource["tutor_id"], resource["subject_id"]),
    ).fetchone()
    return resource if listed_subject else None


@resource_library_bp.get("/api/resources")
def list_resources():
    user_id, user = _logged_in_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    conn = get_db()
    subject_name = request.args.get("subject", "").strip()
    tutor_id_filter = request.args.get("tutor_id")
    if tutor_id_filter is not None:
        if (
            not tutor_id_filter.isdecimal()
            or len(tutor_id_filter) > 19
            or int(tutor_id_filter) < 1
        ):
            conn.close()
            return fail(400, "A valid tutor_id is required")
        tutor_id_filter = int(tutor_id_filter)
    if user["role"] in ("tutor", "both") and not subject_name:
        rows = conn.execute(
            """SELECT resources.id, resources.title, resources.description,
                      resources.file_name, resources.file_type, resources.file_size,
                      resources.is_published, resources.uploaded_at,
                      resources.updated_at, subjects.id AS subject_id,
                      subjects.subject_name, tutor.full_name AS tutor_name
               FROM learning_resources AS resources
               JOIN subjects ON subjects.id = resources.subject_id
               JOIN users AS tutor ON tutor.id = resources.tutor_id
               WHERE resources.tutor_id = ?
               ORDER BY resources.uploaded_at DESC, resources.id DESC""",
            (user_id,),
        ).fetchall()
    else:
        if not subject_name:
            conn.close()
            return fail(400, "Choose a subject to browse its learning resources")
        query = """SELECT resources.id, resources.title, resources.description,
                          resources.file_name, resources.file_type, resources.file_size,
                          resources.is_published, resources.uploaded_at,
                          resources.updated_at, subjects.id AS subject_id,
                          subjects.subject_name, tutor.full_name AS tutor_name
                   FROM learning_resources AS resources
                   JOIN subjects ON subjects.id = resources.subject_id
                   JOIN users AS tutor ON tutor.id = resources.tutor_id
                   JOIN tutor_subjects
                     ON tutor_subjects.tutor_id = resources.tutor_id
                    AND tutor_subjects.subject_id = resources.subject_id
                   WHERE resources.is_published = 1
                     AND subjects.subject_name = ? COLLATE NOCASE
                     AND tutor.role IN ('tutor', 'both')"""
        params = [subject_name]
        if tutor_id_filter is not None:
            query += " AND resources.tutor_id = ?"
            params.append(tutor_id_filter)
        query += " ORDER BY resources.uploaded_at DESC, resources.id DESC"
        rows = conn.execute(
            query,
            params,
        ).fetchall()
    conn.close()
    return jsonify(resources=[dict(row) for row in rows])


@resource_library_bp.post("/api/resources")
def upload_resource():
    user_id, user = _logged_in_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    metadata, error = _parse_metadata(request.form)
    if error:
        return fail(400, error)
    resource, error = _validate_upload(request.files.get("file"))
    if error:
        return fail(400, error)
    conn = get_db()
    stored_path = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        subject = conn.execute(
            """SELECT 1 FROM tutor_subjects
               WHERE tutor_id = ? AND subject_id = ?""",
            (user_id, metadata["subject_id"]),
        ).fetchone()
        if not subject:
            conn.rollback()
            return fail(400, "Select a subject you teach")
        stored_path = _write_resource(resource)
        cursor = conn.execute(
            """INSERT INTO learning_resources
               (tutor_id, subject_id, storage_key, title, description, file_name,
                file_type, file_size, is_published)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                metadata["subject_id"],
                resource["storage_key"],
                metadata["title"],
                metadata["description"],
                resource["file_name"],
                resource["file_type"],
                resource["file_size"],
                metadata["is_published"],
            ),
        )
        conn.commit()
        resource_id = cursor.lastrowid
    except Exception:
        conn.rollback()
        if stored_path and os.path.exists(stored_path):
            os.remove(stored_path)
        raise
    finally:
        conn.close()
    return jsonify(id=resource_id, message="Resource uploaded"), 201


@resource_library_bp.put("/api/resources/<int:resource_id>")
def update_resource(resource_id):
    user_id, user = _logged_in_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return fail(400, "Resource metadata must be a JSON object")
    metadata, error = _parse_metadata(data, partial=True)
    if error:
        return fail(400, error)
    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        resource = conn.execute(
            "SELECT id FROM learning_resources WHERE id = ? AND tutor_id = ?",
            (resource_id, user_id),
        ).fetchone()
        if not resource:
            conn.rollback()
            return fail(404, "Resource not found")
        if "subject_id" in metadata:
            subject = conn.execute(
                """SELECT 1 FROM tutor_subjects
                   WHERE tutor_id = ? AND subject_id = ?""",
                (user_id, metadata["subject_id"]),
            ).fetchone()
            if not subject:
                conn.rollback()
                return fail(400, "Select a subject you teach")
        fields = {
            "title": "title",
            "description": "description",
            "subject_id": "subject_id",
            "is_published": "is_published",
        }
        updates = [f"{column} = ?" for key, column in fields.items() if key in metadata]
        values = [metadata[key] for key in fields if key in metadata]
        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            conn.execute(
                f"UPDATE learning_resources SET {', '.join(updates)} WHERE id = ?",
                (*values, resource_id),
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify(message="Resource details updated")


@resource_library_bp.delete("/api/resources/<int:resource_id>")
def delete_resource(resource_id):
    user_id, user = _logged_in_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    if user["role"] not in ("tutor", "both"):
        return fail(403, "Tutor access required")
    conn = get_db()
    resource = conn.execute(
        """SELECT storage_key FROM learning_resources
           WHERE id = ? AND tutor_id = ?""",
        (resource_id, user_id),
    ).fetchone()
    if not resource:
        conn.close()
        return fail(404, "Resource not found")
    path = _resource_path(resource["storage_key"])
    try:
        conn.execute("DELETE FROM learning_resources WHERE id = ?", (resource_id,))
        conn.commit()
    finally:
        conn.close()
    if os.path.exists(path):
        os.remove(path)
    return jsonify(message="Resource deleted")


@resource_library_bp.get("/api/resources/<int:resource_id>/download")
def download_resource(resource_id):
    user_id, user = _logged_in_user()
    if not user_id:
        return fail(401, "Not authenticated")
    if not user:
        session.clear()
        return fail(401, "Not authenticated")
    subject_name = request.args.get("subject", "").strip()
    conn = get_db()
    resource = _select_resource_access(
        conn, resource_id, user_id, user["role"], subject_name
    )
    conn.close()
    if not resource:
        return fail(404, "Resource not found")
    path = _resource_path(resource["storage_key"])
    if not os.path.isfile(path):
        current_app.logger.error("Stored resource file is missing for resource %s", resource_id)
        return fail(404, "Resource file is unavailable")
    with open(path, "rb") as stored_file:
        contents = stored_file.read(MAX_RESOURCE_SIZE + 1)
    if len(contents) > MAX_RESOURCE_SIZE:
        current_app.logger.error("Stored resource exceeds the size limit: %s", resource_id)
        return fail(500, "Resource file could not be served")
    response = send_file(
        io.BytesIO(contents),
        mimetype=resource["file_type"],
        as_attachment=True,
        download_name=resource["file_name"],
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
