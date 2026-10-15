"""SQLite storage: conversation threads (web and boards) and web login state."""
from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,            -- 'web' or 'device'
    device_id TEXT,
    brain TEXT NOT NULL,             -- 'tana' or 'claude'
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role TEXT NOT NULL,              -- 'user' or 'assistant'
    content TEXT NOT NULL,
    brain TEXT,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread_id, id);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY,
    created REAL NOT NULL,
    expires REAL NOT NULL,
    ip TEXT,
    user_agent TEXT
);
CREATE TABLE IF NOT EXISTS login_failures (ip TEXT NOT NULL, ts REAL NOT NULL);
"""


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    # ------------------------------------------------------------ key/value
    def get(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0]["value"] if rows else None

    def set(self, key: str, value: str | None):
        if value is None:
            self._q("DELETE FROM kv WHERE key=?", (key,))
        else:
            self._q("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))

    # ------------------------------------------------------------ threads
    def create_thread(self, source: str, brain: str, device_id: str | None = None, title: str = "") -> str:
        tid = uuid.uuid4().hex
        now = time.time()
        self._q("INSERT INTO threads(id,title,source,device_id,brain,created,updated) VALUES(?,?,?,?,?,?,?)",
                (tid, title, source, device_id, brain, now, now))
        return tid

    def thread(self, tid: str) -> dict | None:
        rows = self._q("SELECT * FROM threads WHERE id=?", (tid,))
        return dict(rows[0]) if rows else None

    def threads(self, limit: int = 200) -> list[dict]:
        rows = self._q(
            """SELECT t.*, (SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id) AS n
               FROM threads t ORDER BY updated DESC LIMIT ?""", (limit,))
        return [dict(r) for r in rows if r["n"] > 0 or r["source"] == "web"]

    def update_thread(self, tid: str, **fields):
        allowed = {k: v for k, v in fields.items() if k in ("title", "brain")}
        if not allowed:
            return
        sets = ", ".join(f"{k}=?" for k in allowed)
        self._q(f"UPDATE threads SET {sets} WHERE id=?", (*allowed.values(), tid))

    def replace_thread(self, tid: str, source: str, brain: str, title: str,
                       messages: list[tuple[str, str, float]]):
        """Create or fully rewrite an imported thread (e.g. a Claude Code transcript)."""
        now = time.time()
        created = messages[0][2] if messages else now
        updated = messages[-1][2] if messages else now
        with self.lock:
            self.db.execute("BEGIN")
            try:
                self.db.execute(
                    """INSERT INTO threads(id,title,source,device_id,brain,created,updated) VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET title=excluded.title, updated=excluded.updated""",
                    (tid, title, source, None, brain, created, updated))
                self.db.execute("DELETE FROM messages WHERE thread_id=?", (tid,))
                self.db.executemany(
                    "INSERT INTO messages(thread_id,role,content,brain,created) VALUES(?,?,?,?,?)",
                    [(tid, role, content, brain if role == "assistant" else None, ts)
                     for role, content, ts in messages])
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def delete_thread(self, tid: str):
        self._q("DELETE FROM threads WHERE id=?", (tid,))

    def add_message(self, tid: str, role: str, content: str, brain: str | None = None):
        now = time.time()
        self._q("INSERT INTO messages(thread_id,role,content,brain,created) VALUES(?,?,?,?,?)",
                (tid, role, content, brain, now))
        self._q("UPDATE threads SET updated=? WHERE id=?", (now, tid))
        if role == "user":
            self._q("UPDATE threads SET title=? WHERE id=? AND title=''", (content[:80], tid))

    def messages(self, tid: str) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT role, content, brain, created FROM messages WHERE thread_id=? ORDER BY id", (tid,))]

    # ------------------------------------------------------------ web auth
    def add_session(self, token_hash: str, ttl: float, ip: str, ua: str):
        now = time.time()
        self._q("INSERT INTO web_sessions VALUES(?,?,?,?,?)", (token_hash, now, now + ttl, ip, ua[:200]))

    def session_valid(self, token_hash: str) -> bool:
        rows = self._q("SELECT expires FROM web_sessions WHERE token_hash=?", (token_hash,))
        return bool(rows) and rows[0]["expires"] > time.time()

    def delete_session(self, token_hash: str):
        self._q("DELETE FROM web_sessions WHERE token_hash=?", (token_hash,))

    def delete_all_sessions(self):
        self._q("DELETE FROM web_sessions")

    def record_failure(self, ip: str):
        self._q("INSERT INTO login_failures VALUES(?,?)", (ip, time.time()))
        self._q("DELETE FROM login_failures WHERE ts < ?", (time.time() - 86400,))

    def failures_since(self, since: float, ip: str | None = None) -> int:
        if ip is None:
            return self._q("SELECT COUNT(*) c FROM login_failures WHERE ts>=?", (since,))[0]["c"]
        return self._q("SELECT COUNT(*) c FROM login_failures WHERE ts>=? AND ip=?", (since, ip))[0]["c"]
