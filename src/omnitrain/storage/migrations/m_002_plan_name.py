"""Migration 002: Add name column to plans table."""
import sqlite3

VERSION = 2
DESCRIPTION = "Add name column to plans table"

def apply_migration(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    # Check if name column already exists
    cursor.execute("PRAGMA table_info(plans);")
    columns = [row[1] for row in cursor.fetchall()]
    if "name" not in columns:
        cursor.execute("ALTER TABLE plans ADD COLUMN name TEXT;")
