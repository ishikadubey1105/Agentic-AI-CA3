"""
Small helper around a SQLite table that stores a snapshot of every attendance
report the agent has generated. This is separate from the Agents SDK's own
SQLiteSession (which remembers chat turns) -- this table is the app's own
long-term memory of past uploads, so the agent can answer things like
"how does this compare to last week?" across restarts.
"""
import sqlite3
import time

from config import DB_PATH


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    conn = _connect()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT,
            created_at REAL,
            overall_percentage REAL,
            stats_json TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def save_snapshot(label: str, overall_percentage: float, stats_json: str) -> int:
    conn = _connect()
    cur = conn.execute(
        "INSERT INTO attendance_history (label, created_at, overall_percentage, stats_json) "
        "VALUES (?, ?, ?, ?)",
        (label, time.time(), overall_percentage, stats_json),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_recent_snapshots(limit: int = 5) -> list[dict]:
    conn = _connect()
    rows = conn.execute(
        "SELECT id, label, created_at, overall_percentage FROM attendance_history "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "label": r[1],
            "created_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r[2])),
            "overall_percentage": r[3],
        }
        for r in rows
    ]
