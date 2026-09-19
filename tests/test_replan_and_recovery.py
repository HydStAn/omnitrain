"""Unit tests for Status-Update (Recovery, Pain Resolved) and ReplanEngine."""
from datetime import date, timedelta
import pytest
from omnitrain.core.models import CompletionStatus, SportType, Workout
from omnitrain.core.replan import ReplanEngine
from omnitrain.core.status_update import StatusUpdateEvent, StatusUpdateHandler, UpdateType


def make_dummy_workout(w_id: str, w_date: date, w_type: str, dist: float) -> Workout:
    return Workout(
        id=w_id,
        week_id="week-1",
        sport_type=SportType.RUNNING,
        date=w_date,
        day_of_week=w_date.isoweekday(),
        workout_type=w_type,
        metric_primary=dist,
        metric_unit="km",
        status=CompletionStatus.PLANNED
    )


def test_status_update_recovery_rules():
    today = date(2026, 10, 10)
    upcoming = [
        make_dummy_workout("w1", date(2026, 10, 11), "easy", 10.0),
        make_dummy_workout("w2", date(2026, 10, 13), "tempo", 8.0), # within 4 days -> must be downgraded to easy
        make_dummy_workout("w3", date(2026, 10, 17), "tempo", 10.0) # > 4 days -> tempo allowed
    ]

    event = StatusUpdateEvent(
        update_type=UpdateType.RECOVERY,
        details={"condition": "sickness", "status": "resolved"}
    )
    mutations = StatusUpdateHandler.handle_status_update(event, today, upcoming)

    assert len(mutations) == 1
    # 1st workout must be capped at 50% volume (10.0 -> 5.0 km)
    assert upcoming[0].metric_primary == 5.0
    # 2nd workout within 4 days must not be tempo
    assert upcoming[1].workout_type == "easy"
    # 3rd workout > 4 days preserves tempo
    assert upcoming[2].workout_type == "tempo"


def test_replan_engine_running():
    today = date(2026, 10, 15)
    target = date(2027, 2, 28)
    new_weeks = ReplanEngine.replan_running(
        plan_id="test-replan",
        current_date=today,
        target_date=target,
        vdot=50.0,
        last_achieved_weekly_volume=35.0
    )
    assert len(new_weeks) >= 15
    # First week should have conservative baseline ~29.8km (85% of 35)
    assert new_weeks[0].target_weekly_volume == pytest.approx(29.8, abs=0.2)
