# Project C.R.A.I. — Peer Tutoring

Role-based peer tutoring with tutor discovery, availability, private subject resources, source-grounded quiz drafts, session requests, messaging, and optional browser-based video calls.
Stack: Python + Flask, SQLite (built into Python), hashed passwords (Werkzeug), signed httpOnly session cookie, and a plain HTML/CSS/JS frontend.

Tutors add a subject and one weekly availability slot together using a single save action in Tutor Workspace. Existing subjects can be selected to add additional slots; subjects and slots can still be removed or edited separately. A tutor must keep at least one subject.

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
New resource, quiz, assignment, attempt, and focus-indicator tables are created additively by the existing idempotent schema initialization; existing account and tutoring data is not dropped.

Tutors manage their own weekly availability. Learners search by tutor name and/or subject, then view availability on the tutor profile and propose a time within a published slot. The server validates the selected slot; a request does not reserve or modify it. Availability has no separate tutor-timezone setting, so schedule matching assumes tutor and learner use the same local timezone.

At the bottom of the tutor’s **My Profile** page, a subject-per-row table shows each subject, available days and times, and a **My learning materials** button for that subject. The button opens the subject’s materials and upload/management controls. Public tutor profiles group shared PDFs by subject. Tutor resources accept UTF-8 TXT, Markdown, PDF, and DOCX uploads up to 5 MB. Files are stored outside `public/`, use generated storage names, and are served only through permission-checked download routes. Quiz generation currently extracts readable text from TXT, Markdown, and DOCX; PDF files remain downloadable but are not supported as generation sources.

Sample logins after seeding (password `password123`): `ana@example.com` (tutor), `ben@example.com` (learner), `cara@example.com` (both).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `3000` | Server port |
| `APP_ENV` | `development` | Set to `production` to enable production configuration checks |
| `SECRET_KEY` | Development-only key | Signs the session cookie. Required when `APP_ENV=production`; keep it private |
| `DB_FILE` | `./crai.db` | SQLite file path |
| `RESOURCE_STORAGE_DIR` | `./uploads/resources` | Private resource upload directory. Required to point to persistent storage in production and must not be under `public/` |
| `GEMINI_API_KEY` | unset | Server-side Google Gemini API key. Quiz generation returns unavailable when unset |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Gemini model name used by the server |
| `STUN_URLS` | unset | Optional comma-separated STUN URLs used for WebRTC connectivity |
| `TURN_URLS` | unset | Optional comma-separated TURN URLs. Configure this for networks that need TURN relay |
| `TURN_SHARED_SECRET` | unset | Coturn REST shared secret used to create short-lived TURN credentials; required with `TURN_URLS` |
| `VAPID_PUBLIC_KEY` | unset | Public VAPID key used by browser push |
| `VAPID_PRIVATE_KEY` | unset | Path to the PEM-encoded VAPID private key |
| `VAPID_SUBJECT` | unset | VAPID contact URI, such as `mailto:admin@example.com` |
| `SMS_ENABLED` | `false` | Set to `true` to allow Twilio SMS delivery |
| `TWILIO_ACCOUNT_SID` | unset | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | unset | Twilio auth token; keep it private |
| `TWILIO_FROM_NUMBER` | unset | Twilio phone number in E.164 format |

The built-in secret is for local development only. Before deployment, configure a strong random `SECRET_KEY`; the app refuses to start in production without one. Production also requires `DB_FILE` and `RESOURCE_STORAGE_DIR` to be explicitly configured on persistent storage. Mount the persistent volume to contain both paths so accounts, messages, and uploaded files survive redeployments.

Profile photos are stored locally in `uploads/profile_photos/` and excluded from Git. Mount persistent storage for this directory as well if uploaded profile photos must survive restarts.

## Tutor resources and quizzes

Tutors can upload a resource under a subject they currently teach, keep it private, or explicitly publish it for learners. Learners browse by subject and can download only published resources from tutors who still list that subject. The server validates file size and content, stores random private filenames, and never exposes the storage directory as static content.

Quiz generation is disabled unless `GEMINI_API_KEY` is configured. Before generation or regeneration, the tutor must explicitly authorize sending the selected resource’s extracted text to Google Gemini. Only the extracted source text and requested question counts are sent; credentials stay server-side. Review each generated question, answer key, and source excerpt before publishing. The generator verifies exact source excerpts and fails clearly when the material is unreadable, unsupported, insufficient, or the provider response cannot be checked; it does not provide fake fallback questions.

Each quiz has Test 1 multiple-choice and Test 2 identification questions. Tutors can edit drafts, review answer keys, regenerate, publish, and assign to a learner by account ID. Learners see questions without answer keys, can submit answers, and see their score and submission time afterward. Answer keys remain tutor-only. Identification answers are checked case-insensitively with whitespace normalized.

Focus-event monitoring is optional per quiz and off by default. When a tutor enables it, the learner sees what is recorded and must explicitly opt in to monitoring; declining allows an unmonitored attempt. Only quiz-page hidden/focus-loss event types and server timestamps are stored. Tutors see these as review indicators, not proof of cheating. No external tabs, applications, browsing history, outside keystrokes, or screen contents are collected; events never cause automatic penalties.

## Video calls

Video calls are offered only for accepted session requests. The browser requests camera/microphone permission after the learner or tutor joins; the app provides mute, video-disable, and leave controls. Calls are not recorded, and audio/video media is not sent through the Flask server. Signaling is authenticated and limited to the two participants in the accepted request.

Production calls require HTTPS/WSS. WebRTC connectivity depends on the participants’ networks and ICE configuration; peer-to-peer calls will not work across every network. Configure `STUN_URLS` and a TURN service using `TURN_URLS` plus a compatible Coturn REST `TURN_SHARED_SECRET` for reliable connectivity. TURN credentials are short-lived but are necessarily provided to authorized browsers. No STUN or TURN service is enabled by default.

The chat and call WebSocket registries are process-local. Run one app process/instance for this implementation; multiple workers or instances require shared pub/sub signaling. Use a production WSGI server that supports Flask-Sock, with enough threads for expected concurrent WebSocket connections (for example, on Linux: `gunicorn --workers 1 --threads 100 app:app`). Keep WebSocket connections on HTTPS/WSS and configure the host/proxy to pass upgrades.

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
| POST | `/session-requests` | Learner proposes a future time within the selected tutor availability slot, with an optional note |
| GET | `/session-requests` | Lists requests involving the logged-in user, with sent/received direction and status |
| GET | `/session-requests/<id>/learner-profile` | Returns the learner's name, email, student number, course, year level, and photo availability to the tutor who received that request |
| PUT | `/session-requests/<id>` | Tutor accepts/declines a pending request, or the learner cancels it |
| GET | `/api/resources?subject=<name>` | Learner browses published resources for a selected subject; tutors list their own resources without a subject |
| POST | `/api/resources` | Tutor uploads a private or published TXT, Markdown, PDF, or DOCX resource |
| PUT/DELETE | `/api/resources/<id>` | Tutor updates their own resource metadata/visibility or deletes it |
| GET | `/api/resources/<id>/download` | Permission-checked resource download; learner requests must include the matching `subject` |
| GET | `/api/quizzes?subject=<name>` | Lists published quizzes for a learner-selected subject; tutors list their own drafts and published quizzes |
| POST | `/api/resources/<id>/quizzes/generate` | Tutor generates a draft from their resource; requires counts, explicit Google Gemini processing consent, and server configuration |
| POST | `/api/quizzes/<id>/regenerate` | Tutor regenerates a draft after confirming external processing again |
| GET/PUT | `/api/quizzes/<id>` | Tutor reviews/edits a draft or answer key; learners get published questions without answer keys |
| POST | `/api/quizzes/<id>/publish` | Publishes a reviewed two-part quiz |
| POST | `/api/quizzes/<id>/assign` | Assigns a published quiz to a learner account ID |
| POST | `/api/quizzes/<id>/attempts` | Authorized learner starts an attempt and sets focus-event consent |
| POST | `/api/quiz-attempts/<id>/focus-events` | Records only consented hidden/blur event types with server timestamps |
| POST | `/api/quiz-attempts/<id>/submit` | Saves answers and submission time; returns the score, not the answer key |
| GET | `/api/quizzes/<id>/attempts` | Tutor sees learner answers, results, and non-proof focus indicators |
| GET | `/api/session-requests/<id>/video-config` | ICE configuration for a participant in an accepted request |
| WebSocket | `/ws/calls/<id>` | Authenticated WebRTC signaling for accepted-request participants only |
| GET | `/tutors/search?subject=<name>&name=<partial>` | Searches tutors by case-insensitive subject and/or partial tutor name; when both are supplied, both must match |
| GET | `/api/tutors?subject=<name>&name=<partial>` | Alias for tutor search; at least one of `subject` or `name` is required |
| GET | `/api/resources?subject=<name>&tutor_id=<id>` | Lists published resources relevant to a subject, optionally limited to one tutor |
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
| POST | `/tutor/teaching-profile` | Atomically adds a tutor subject and a weekly availability slot |
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
resource_library.py  private tutor resource routes and upload validation
quizzes.py      Gemini generation, review/publish, submissions, and focus-event routes
call_realtime.py  process-local WebRTC signaling registry
db.py           database connection + schema setup
seed.py         sample data
schema.sql      existing tables plus resources, quizzes, assignments, attempts, and focus events
public/index.html   login, register, dashboard
public/default-avatar.svg   fallback avatar when a user has not uploaded a photo
```

## Out of scope (later sprints)

Calendar booking/reservation, ratings, and payments. Session requests are proposed and accepted in-app but do not reserve tutor availability. Tutor discovery currently supports name and subject search only.
