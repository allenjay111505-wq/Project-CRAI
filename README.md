# Project C.R.A.I. — Sprint 1

Registration, login, database and a role-based dashboard for the peer-to-peer tutoring system.
Stack: Python + Flask, SQLite (built into Python), hashed passwords (Werkzeug), signed httpOnly session cookie, plain HTML/CSS/JS frontend.

Tutors can add subjects from the shared subject list or enter a new subject, and remove subjects from their profile. A tutor must keep at least one subject.

## Run locally

Requires Python 3.9+.

```bash
python -m venv venv
venv\Scripts\activate          # Windows   (macOS/Linux: source venv/bin/activate)
pip install -r requirements.txt
python seed.py                 # optional: sample users and subjects
python app.py                  # http://localhost:3000
```

The database file `crai.db` and all tables are created automatically from `schema.sql`.
Messages and conversations are stored locally in SQLite.

Sample logins after seeding (password `password123`): `ana@example.com` (tutor), `ben@example.com` (learner), `cara@example.com` (both).

## Environment variables (all optional)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `3000` | Server port |
| `SECRET_KEY` | `dev-secret-change-me` | Signs the session cookie. Set your own before real use |
| `DB_FILE` | `./crai.db` | SQLite file path |

Profile photos are stored locally in `uploads/profile_photos/` and excluded from Git.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/register` | Validates input, rejects duplicate email, stores a password hash |
| POST | `/login` | Starts a session, or `{ "error": "Invalid email or password" }` |
| GET | `/dashboard` | Protected; returns the logged-in user (no password hash) |
| GET | `/profile/photo` | Returns the logged-in user's uploaded profile photo |
| POST | `/profile/photo` | Uploads a PNG, JPEG, or WebP profile photo (up to 5 MB) |
| DELETE | `/profile/photo` | Removes the logged-in user's photo and restores the default avatar |
| POST | `/conversations` | Starts or returns a conversation with `{ "other_user_id": 2 }` |
| GET | `/conversations` | Lists the logged-in user's conversations, latest-message previews, and unread counts |
| GET | `/conversations/<id>/messages` | Lists messages in a conversation the logged-in user participates in |
| POST | `/conversations/<id>/messages` | Sends a non-empty message (up to 2,000 characters) in a conversation |
| PUT | `/conversations/<id>/read` | Marks incoming messages in a participant's conversation as read |
| GET | `/tutors/search?subject=<name>` | Ticket search route; supports case-insensitive subject, `day`, optional paired `start_time`/`end_time`, `free=true|false`, and `sort=name|rate_asc|rate_desc` filters |
| GET | `/api/tutors?subject=<name>` | Backward-compatible alias for tutor search; also supports `rate=free|paid` |
| GET | `/tutor/subjects` | Lists a tutor's subjects and the shared subject catalog |
| POST | `/tutor/subjects` | Adds a listed or newly entered subject to the logged-in tutor |
| PUT | `/tutor/subjects` | Replaces one assigned subject using `{ "subject_id": 1, "subject_name": "Biology" }` |
| DELETE | `/tutor/subjects` | Removes an assigned subject using `{ "subject_id": 1 }`; the last subject cannot be removed |
| DELETE | `/tutor/subjects/<id>` | Removes a subject from the logged-in tutor; the last subject cannot be removed |
| GET | `/tutors/<id>` | Public-facing tutor profile page |
| GET | `/api/tutors/<id>` | Public tutor name, subjects, weekly availability, and session rate |
| GET | `/tutor/rate` | Returns whether the logged-in tutor offers free sessions or their hourly rate |
| PUT | `/tutor/rate` | Saves `{ "is_free": true }` or `{ "is_free": false, "rate_per_hour": 25 }` |
| GET | `/tutor/availability` | Lists the logged-in tutor's weekly slots by day and start time |
| POST | `/tutor/availability` | Adds a weekly slot; overlapping slots on the same day are rejected |
| PUT | `/tutor/availability` | Edits a slot using `{ "slot_id": 1, "day": "Monday", "start_time": "09:00", "end_time": "10:00" }` |
| DELETE | `/tutor/availability` | Removes a slot using `{ "slot_id": 1 }` |
| PUT | `/tutor/availability/<id>` | Edits one of the logged-in tutor's slots |
| DELETE | `/tutor/availability/<id>` | Removes one of the logged-in tutor's slots |
| POST | `/logout` | Ends the session |

## Structure

```
app.py          Flask API + serves the frontend
db.py           database connection + schema setup
seed.py         sample data
schema.sql      tables: users, subjects, tutor_subjects, availability, tutor_rates,
                conversations, and messages
public/index.html   login, register, dashboard
public/default-avatar.svg   fallback avatar when a user has not uploaded a photo
```

## Out of scope (later sprints)

Bookings, ratings, and payments. Tutor discovery currently supports subject, availability, and rate filters.
