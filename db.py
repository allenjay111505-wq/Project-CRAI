import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.environ.get("DB_FILE", os.path.join(BASE, "crai.db"))


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    with open(os.path.join(BASE, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    conn.close()
