# Project C.R.A.I. — Sprint 1

Registration, login, database and a role-based dashboard for the peer-to-peer tutoring system.
Stack: Python + Flask, SQLite (built into Python), hashed passwords (Werkzeug), signed httpOnly session cookie, plain HTML/CSS/JS frontend.

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

Sample logins after seeding (password `password123`): `ana@example.com` (tutor), `ben@example.com` (learner), `cara@example.com` (both).

## Environment variables (all optional)

| Variable | Default | Purpose |
|---|---|---|
| `PORT` | `3000` | Server port |
| `SECRET_KEY` | `dev-secret-change-me` | Signs the session cookie. Set your own before real use |
| `DB_FILE` | `./crai.db` | SQLite file path |

## Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/register` | Validates input, rejects duplicate email, stores a password hash |
| POST | `/login` | Starts a session, or `{ "error": "Invalid email or password" }` |
| GET | `/dashboard` | Protected; returns the logged-in user (no password hash) |
| POST | `/logout` | Ends the session |

## Structure

```
app.py          Flask API + serves the frontend
db.py           database connection + schema setup
seed.py         sample data
schema.sql      tables: users, subjects, tutor_subjects, availability, tutor_rates
public/index.html   login, register, dashboard
```

## Out of scope (later sprints)

Matching/search, booking, messaging, ratings, payments. The dashboard shortcuts are placeholders.
