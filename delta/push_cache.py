import sqlite3
from pathlib import Path
from typing import Dict, List, Tuple


class PushCache:
    """
    Manages the local SQLite cache for Delta Cloud delta pushes.
    Tracks previously synchronized test coverage mappings to filter out unmodified entries.
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        """Initialize the local push cache database schema."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS push_cache (
                    branch TEXT,
                    test_name TEXT,
                    file_path TEXT,
                    ranges TEXT,
                    duration_ms INTEGER DEFAULT 0,
                    last_pushed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (branch, test_name, file_path)
                )
            """)
            # Schema migration: add duration_ms column if it doesn't exist
            # Check column existence first to avoid swallowing real OperationalErrors
            cursor = conn.execute("PRAGMA table_info(push_cache)")
            columns = {row[1] for row in cursor.fetchall()}
            if "duration_ms" not in columns:
                try:
                    conn.execute("ALTER TABLE push_cache ADD COLUMN duration_ms INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass  # Race condition: column was added by another process
            conn.execute("CREATE INDEX IF NOT EXISTS idx_push_cache_lookup ON push_cache(branch)")

    def get_cached_state(self, branch: str) -> Dict[Tuple[str, str], Tuple[str, int]]:
        """
        Fetch the last successfully pushed state for the active branch.

        Returns:
            {(test_name, file_path): (ranges_string, duration_ms)}
        """
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                "SELECT test_name, file_path, ranges, duration_ms FROM push_cache WHERE branch = ?",
                (branch,)
            )
            return {(row[0], row[1]): (row[2], row[3] if row[3] is not None else 0) for row in cursor}

    def batch_upsert(self, branch: str, mappings: List[Dict]):
        """
        Atomically update the cache with successfully pushed mappings.

        Args:
            branch: Active git branch
            mappings: List of dicts [{"test_name": ..., "file_path": ..., "ranges": ..., "duration_ms": ...}]
        """
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany("""
                INSERT INTO push_cache (branch, test_name, file_path, ranges, duration_ms, last_pushed_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(branch, test_name, file_path) DO UPDATE SET
                    ranges = EXCLUDED.ranges,
                    duration_ms = CASE WHEN EXCLUDED.duration_ms > 0 THEN EXCLUDED.duration_ms ELSE push_cache.duration_ms END,
                    last_pushed_at = CURRENT_TIMESTAMP
            """, [(branch, m["test_name"], m["file_path"], m["ranges"], m.get("duration_ms", 0)) for m in mappings])
