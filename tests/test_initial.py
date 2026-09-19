import pytest
from omnitrain.storage.db import Database
from omnitrain.core.models import CheckinEvent, CompletionStatus, Symptom

def test_database_migration(tmp_path):
    db_file = tmp_path / "test_omnitrain.db"
    db = Database(db_file)
    version = db.migrate()
    assert version == 1

    with db.get_connection() as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
        assert "schema_version" in tables
        assert "users" in tables
        assert "plans" in tables
        assert "weeks" in tables
        assert "workouts" in tables
        assert "checkin_logs" in tables
        assert "training_zones" in tables

def test_checkin_schema_validation():
    payload = {
        "event_type": "workout_checkin",
        "completion_status": "partial",
        "completion_reason": "time_constraint",
        "actual_metrics": {
            "metric_primary": 12.0,
            "unit": "km",
            "tss": 85.4
        },
        "perceived_rpe": 8,
        "sickness_reported": False,
        "symptoms": [
            {
                "location": "knee_right",
                "side": "right",
                "type": "joint_pain",
                "severity": 6
            }
        ],
        "notes": "Musste bei km 12 abbrechen."
    }
    event = CheckinEvent.model_validate(payload)
    assert event.completion_status == CompletionStatus.PARTIAL
    assert len(event.symptoms) == 1
    assert event.symptoms[0].severity == 6
