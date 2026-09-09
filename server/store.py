"""Persistenza: stato corrente, storico eventi, subscription push. SQLite."""

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    kind     TEXT NOT NULL,          -- 'down' | 'up'
    duration REAL                    -- durata del blackout, solo su 'up'
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts DESC);
CREATE TABLE IF NOT EXISTS subs (
    endpoint TEXT PRIMARY KEY,
    sub      TEXT NOT NULL,
    label    TEXT,
    created  REAL NOT NULL
);
"""


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.commit()

    # --- stato ---
    def get_state(self):
        with self._lock:
            rows = self._db.execute("SELECT key, value FROM kv").fetchall()
        return {r["key"]: json.loads(r["value"]) for r in rows}

    def set_state(self, **kw):
        with self._lock:
            for k, v in kw.items():
                self._db.execute(
                    "INSERT INTO kv (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (k, json.dumps(v)),
                )
            self._db.commit()

    # --- eventi (base per le statistiche di uptime) ---
    def add_event(self, kind, ts=None, duration=None):
        with self._lock:
            self._db.execute(
                "INSERT INTO events (ts, kind, duration) VALUES (?, ?, ?)",
                (ts or time.time(), kind, duration),
            )
            self._db.commit()

    def all_events(self):
        """Tutti gli eventi, in ordine cronologico. Sono pochi per natura
        (due per blackout), quindi non serve paginare."""
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, kind, duration FROM events ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, limit=20):
        with self._lock:
            rows = self._db.execute(
                "SELECT ts, kind, duration FROM events ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # --- subscription push ---
    def add_sub(self, sub, label=None):
        with self._lock:
            self._db.execute(
                "INSERT INTO subs (endpoint, sub, label, created) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(endpoint) DO UPDATE SET sub=excluded.sub, label=excluded.label",
                (sub["endpoint"], json.dumps(sub), label, time.time()),
            )
            self._db.commit()

    def del_sub(self, endpoint):
        with self._lock:
            self._db.execute("DELETE FROM subs WHERE endpoint = ?", (endpoint,))
            self._db.commit()

    def all_subs(self):
        with self._lock:
            rows = self._db.execute("SELECT sub FROM subs").fetchall()
        return [json.loads(r["sub"]) for r in rows]

    def count_subs(self):
        with self._lock:
            return self._db.execute("SELECT COUNT(*) c FROM subs").fetchone()["c"]
