"""SQLite persistence using the standard-library sqlite3 module (no ORM)."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app.monitor import CheckResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS endpoints (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    url             TEXT    NOT NULL UNIQUE,
    method          TEXT    NOT NULL DEFAULT 'GET',
    expected_status INTEGER NOT NULL DEFAULT 200,
    timeout_seconds REAL    NOT NULL DEFAULT 5.0,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS check_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id      INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    status           TEXT    NOT NULL CHECK (status IN ('healthy', 'unhealthy', 'unreachable')),
    status_code      INTEGER,
    response_time_ms REAL,
    error            TEXT,
    checked_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_check_results_endpoint_time
    ON check_results (endpoint_id, checked_at DESC);
"""


class DuplicateEndpointError(Exception):
    pass


class Database:
    def __init__(self, path: str) -> None:
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Open a short-lived connection; commit on success, roll back on error."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")  # SQLite needs this for ON DELETE CASCADE
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    # --- endpoints -------------------------------------------------------

    def add_endpoint(
        self,
        name: str,
        url: str,
        method: str,
        expected_status: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        created_at = datetime.now(UTC).isoformat()
        try:
            with self.connect() as conn:
                cursor = conn.execute(
                    """INSERT INTO endpoints
                       (name, url, method, expected_status, timeout_seconds, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (name, url, method, expected_status, timeout_seconds, created_at),
                )
                endpoint_id = cursor.lastrowid
        except sqlite3.IntegrityError as exc:
            raise DuplicateEndpointError(url) from exc
        return self.get_endpoint(endpoint_id)  # type: ignore[return-value]

    def get_endpoint(self, endpoint_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM endpoints WHERE id = ?", (endpoint_id,)).fetchone()
        return dict(row) if row else None

    def list_endpoints(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM endpoints ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def delete_endpoint(self, endpoint_id: int) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
        return cursor.rowcount > 0

    # --- check results ---------------------------------------------------

    def save_result(self, endpoint_id: int, result: CheckResult) -> dict[str, Any]:
        with self.connect() as conn:
            cursor = conn.execute(
                """INSERT INTO check_results
                   (endpoint_id, status, status_code, response_time_ms, error, checked_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    endpoint_id,
                    result.status.value,
                    result.status_code,
                    result.response_time_ms,
                    result.error,
                    result.checked_at.isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM check_results WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row)

    def list_results(self, endpoint_id: int, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent results first."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM check_results
                   WHERE endpoint_id = ?
                   ORDER BY checked_at DESC, id DESC
                   LIMIT ?""",
                (endpoint_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def summarize(self, endpoint_id: int, window: int = 100) -> dict[str, Any]:
        """Uptime and average response time over the last `window` checks."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*)                                          AS checks_counted,
                          SUM(CASE WHEN status = 'healthy' THEN 1 ELSE 0 END) AS healthy_count,
                          AVG(response_time_ms)                             AS avg_response_time_ms
                   FROM (SELECT status, response_time_ms FROM check_results
                         WHERE endpoint_id = ?
                         ORDER BY checked_at DESC, id DESC
                         LIMIT ?)""",
                (endpoint_id, window),
            ).fetchone()
        counted = row["checks_counted"]
        avg = row["avg_response_time_ms"]
        return {
            "checks_counted": counted,
            "uptime_percent": round(100 * row["healthy_count"] / counted, 2) if counted else None,
            "avg_response_time_ms": round(avg, 2) if avg is not None else None,
        }
