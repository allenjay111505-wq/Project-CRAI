# Project C.R.A.I. — Sprint 1

Registration, login, database, and a role-based dashboard with separate profile, learner search, tutor workspace, session request, and inbox pages for the peer-to-peer tutoring system.
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

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `3000` | Server port |
| `APP_ENV` | `development` | Set to `production` to enable production configuration checks |
| `SECRET_KEY` | Development-only key | Signs the session cookie. Required when `APP_ENV=production`; keep it private |
| `DB_FILE` | `./crai.db` | SQLite file path |
| `VAPID_PUBLIC_KEY` | unset | Public VAPID key used by browser push |
| `VAPID_PRIVATE_KEY` | unset | Path to the PEM-encoded VAPID private key |
| `VAPID_SUBJECT` | unset | VAPID contact URI, such as `mailto:admin@example.com` |
| `SMS_ENABLED` | `false` | Set to `true` to allow Twilio SMS delivery |
| `TWILIO_ACCOUNT_SID` | unset | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | unset | Twilio auth token; keep it private |
| `TWILIO_FROM_NUMBER` | unset | Twilio phone number in E.164 format |

The built-in secret is for local development only. Before deployment, configure a strong random `SECRET_KEY`; the app refuses to start in production without one.

Profile photos are stored locally in `uploads/profile_photos/` and excluded from Git.

## Optional live chat and notifications

Chat updates use an authenticated WebSocket at `/ws/chat`, with periodic HTTP polling retained as a fallback. The WebSocket client registry is process-local, so run a single app process; multiple workers require a shared pub/sub service.

Browser push is off until all three VAPID variables are configured and a signed-in user enables it in **My Profile**. The browser asks for notification permission only after that explicit action. `localhost` is treated as a secure context by modern browsers; deployments need HTTPS. If push is not configured, the setting remains unavailable and chat still works.

SMS is also off by default. To enable it, configure the Twilio variables above and set `SMS_ENABLED=true`. Users must separately enter an international phone number (E.164, for example `+15551234567`) and opt in under **My Profile**. SMS may incur Twilio and carrier charges; never enable it without appropriate account setup, user consent, and cost controls. No phone number, VAPID key, or Twilio credential is required for local chat.

The app sends notifications for new messages and session request creation or status updates. Push subscriptions and phone/SMS consent are stored per user; a user's browser push subscription can be disabled from their profile.

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/register` | Validates input, rejects duplicate email, stores a password hash |
| POST | `/login` | Starts a session, or `{ "error": "Invalid email or password" }` |
| GET | `/dashboard` | Protected; returns the logged-in user (no password hash) |
| PUT | `/profile` | Updates the logged-in user's profile fields, phone number, and SMS consent |
| GET | `/api/push/config` | Indicates whether browser push is configured and, if so, returns the public VAPID key |
| GET | `/api/push/subscription` | Returns the signed-in user's browser push setting |
| POST/DELETE | `/api/push/subscription` | Adds or removes the signed-in user's browser push subscription |
| WebSocket | `/ws/chat` | Authenticated realtime chat event connection |
| GET | `/profile/photo` | Returns the logged-in user's uploaded profile photo |
| GET | `/api/tutors/<id>/photo` | Returns the public profile photo for a tutor or responds with 404 when unavailable |
| POST | `/profile/photo` | Uploads a PNG, JPEG, or WebP profile photo (up to 5 MB) |
| DELETE | `/profile/photo` | Removes the logged-in user's photo and restores the default avatar |
| POST | `/conversations` | Starts or returns a conversation with `{ "other_user_id": 2 }` |
| GET | `/conversations` | Lists the logged-in user's conversations, latest-message previews, and unread counts |
| GET | `/api/users/<id>/photo` | Returns a tutor's public photo or a learner photo to an authorized conversation partner or requesting tutor |
| GET | `/conversations/<id>/messages` | Lists messages in a conversation the logged-in user participates in |
| POST | `/conversations/<id>/messages` | Sends a non-empty message (up to 2,000 characters) in a conversation |
| PUT | `/conversations/<id>/read` | Marks incoming messages in a participant's conversation as read |
| POST | `/session-requests` | Learner proposes a future date/time to a tutor, optionally with a note |
| GET | `/session-requests` | Lists requests involving the logged-in user, with sent/received direction and status |
| GET | `/session-requests/<id>/learner-profile` | Returns the learner's name, email, student number, course, year level, and photo availability to the tutor who received that request |
| PUT | `/session-requests/<id>` | Tutor accepts/declines a pending request, or the learner cancels it |
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

Session requests are proposals only. An accepted request records agreement between the users but does not reserve a calendar slot or process payment.

## Structure

```
app.py          Flask API + serves the frontend
session_requests.py  isolated session-request API blueprint
db.py           database connection + schema setup
seed.py         sample data
schema.sql      tables: users, subjects, tutor_subjects, availability, tutor_rates,
                conversations, messages, and session_requests
public/index.html   login, register, dashboard
public/default-avatar.svg   fallback avatar when a user has not uploaded a photo
```

## Out of scope (later sprints)

Calendar booking/reservation, ratings, and payments. Session requests are proposed and accepted in-app but do not reserve tutor availability. Tutor discovery supports subject, availability, and rate filters.
