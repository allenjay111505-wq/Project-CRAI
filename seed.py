from werkzeug.security import generate_password_hash

from db import get_db, init_db

init_db()
conn = get_db()

for name in ["Calculus", "Programming 1", "Data Structures", "Physics", "English Composition"]:
    conn.execute("INSERT OR IGNORE INTO subjects (subject_name) VALUES (?)", (name,))

pw = generate_password_hash("password123")
users = [
    ("Ana Reyes", "ana@example.com", "2021-0001", "BSCS", 3, "tutor"),
    ("Ben Cruz", "ben@example.com", "2023-0002", "BSIT", 1, "learner"),
    ("Cara Lim", "cara@example.com", "2022-0003", "BSED", 2, "both"),
]
for full_name, email, sid, course, year, role in users:
    conn.execute(
        """INSERT OR IGNORE INTO users
           (full_name, email, student_id, course, year_level, password_hash, role)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (full_name, email, sid, course, year, pw, role),
    )

conn.commit()
conn.close()
print("Seeded. Sample login: ana@example.com / password123")
