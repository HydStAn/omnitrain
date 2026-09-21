"""Database manager and migration engine for OmniTrain."""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class Database:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def get_current_version(self, conn: sqlite3.Connection) -> int:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT count(*) FROM sqlite_master 
            WHERE type='table' AND name='schema_version';
        """)
        if cursor.fetchone()[0] == 0:
            return 0
        cursor.execute("SELECT MAX(version) FROM schema_version;")
        row = cursor.fetchone()
        return row[0] if row and row[0] is not None else 0

    def migrate(self) -> int:
        with self.get_connection() as conn:
            current_v = self.get_current_version(conn)
            if current_v < 1:
                from omnitrain.storage.migrations import m_001_initial
                m_001_initial.apply_migration(conn)
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
                    (1, now, m_001_initial.DESCRIPTION)
                )
                conn.commit()
                current_v = 1

            if current_v < 2:
                from omnitrain.storage.migrations import m_002_plan_name
                m_002_plan_name.apply_migration(conn)
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
                    (2, now, m_002_plan_name.DESCRIPTION)
                )
                conn.commit()
                current_v = 2

            return current_v
