"""Initial schema migration for OmniTrain."""
import sqlite3

VERSION = 1
DESCRIPTION = "Initial schema: schema_version, users, training_zones, plans, weeks, workouts, checkin_logs"

def apply_migration(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.executescript("""
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL,
        description TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        display_name TEXT,
        birth_year INTEGER,
        resting_hr INTEGER,
        max_hr INTEGER,
        weight_kg REAL,
        profile_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS training_zones (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        sport_type TEXT NOT NULL,
        zone_model TEXT NOT NULL,
        reference_value REAL,
        zones_json TEXT NOT NULL,
        calculated_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS plans (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        sport_type TEXT NOT NULL,
        goal_type TEXT NOT NULL,
        start_date TEXT NOT NULL,
        target_date TEXT NOT NULL,
        available_days TEXT NOT NULL,
        base_weekly_volume REAL NOT NULL,
        sessions_per_week INTEGER NOT NULL,
        key_workout_day INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS weeks (
        id TEXT PRIMARY KEY,
        plan_id TEXT NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
        week_number INTEGER NOT NULL,
        week_start_date TEXT NOT NULL,
        phase TEXT NOT NULL,
        target_weekly_volume REAL NOT NULL,
        target_weekly_tss REAL,
        is_recovery_week INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS workouts (
        id TEXT PRIMARY KEY,
        week_id TEXT NOT NULL REFERENCES weeks(id) ON DELETE CASCADE,
        sport_type TEXT NOT NULL,
        date TEXT NOT NULL,
        day_of_week INTEGER NOT NULL,
        workout_type TEXT NOT NULL,
        metric_primary REAL NOT NULL,
        metric_unit TEXT NOT NULL,
        intensity_target TEXT,
        intensity_detail TEXT,
        target_duration_min INTEGER,
        structure_json TEXT,
        status TEXT NOT NULL DEFAULT 'planned',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS checkin_logs (
        id TEXT PRIMARY KEY,
        workout_id TEXT REFERENCES workouts(id) ON DELETE SET NULL,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        timestamp TEXT NOT NULL,
        raw_input TEXT NOT NULL,
        completion_status TEXT NOT NULL,
        actual_metrics_json TEXT,
        perceived_rpe INTEGER,
        symptoms_json TEXT,
        applied_mutations_json TEXT,
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_workouts_date ON workouts(date);
    CREATE INDEX IF NOT EXISTS idx_workouts_status ON workouts(status);
    CREATE INDEX IF NOT EXISTS idx_checkin_logs_user ON checkin_logs(user_id);
    """)
    conn.commit()
