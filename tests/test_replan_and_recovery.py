"""Unit tests for Status-Update (Recovery, Pain Resolved) and ReplanEngine."""
from datetime import date, timedelta
import pytest
from omnitrain.core.models import CompletionStatus, SportType, Workout
from omnitrain.core.replan import ReplanEngine
from omnitrain.core.status_update import StatusUpdateEvent, StatusUpdateHandler, UpdateType


def make_dummy_workout(
    w_id: str,
    w_date: date,
    w_type: str,
    dist: float,
    sport_type: SportType = SportType.RUNNING,
    metric_unit: str = "km"
) -> Workout:
    return Workout(
        id=w_id,
        week_id="week-1",
        sport_type=sport_type,
        date=w_date,
        day_of_week=w_date.isoweekday(),
        workout_type=w_type,
        metric_primary=dist,
        metric_unit=metric_unit,
        status=CompletionStatus.PLANNED
    )


def test_status_update_recovery_rules():
    today = date(2026, 10, 10)
    upcoming = [
        make_dummy_workout("w1", date(2026, 10, 11), "easy", 10.0),
        make_dummy_workout("w2", date(2026, 10, 13), "tempo", 8.0),  # within 4 days -> must be downgraded to easy
        make_dummy_workout("w3", date(2026, 10, 17), "tempo", 10.0)  # > 4 days -> tempo allowed
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


def test_status_update_multisport_recovery():
    today = date(2026, 10, 10)

    # 1. Swimming: css_threshold -> technique_drills, 50% volume
    swim_wo1 = make_dummy_workout("s1", date(2026, 10, 11), "css_threshold", 2400.0, SportType.SWIMMING, "m")
    event_swim = StatusUpdateEvent(
        update_type=UpdateType.RECOVERY,
        details={"condition": "sickness", "status": "resolved"}
    )
    StatusUpdateHandler.handle_status_update(event_swim, today, [swim_wo1])
    assert swim_wo1.metric_primary == 1200.0
    assert swim_wo1.workout_type == "technique_drills"
    assert swim_wo1.metric_unit == "m"

    # 2. Strength: strength -> hypertrophy, 50% volume
    str_wo1 = make_dummy_workout("st1", date(2026, 10, 11), "strength", 16.0, SportType.STRENGTH, "sets")
    StatusUpdateHandler.handle_status_update(event_swim, today, [str_wo1])
    assert str_wo1.metric_primary == 8.0
    assert str_wo1.workout_type == "hypertrophy"
    assert str_wo1.metric_unit == "sets"

    # 3. Cycling: threshold -> recovery, 50% volume
    bike_wo1 = make_dummy_workout("b1", date(2026, 10, 11), "threshold", 90.0, SportType.CYCLING, "min")
    StatusUpdateHandler.handle_status_update(event_swim, today, [bike_wo1])
    assert bike_wo1.metric_primary == 45.0
    assert bike_wo1.workout_type == "recovery"
    assert bike_wo1.metric_unit == "min"


def test_status_update_pain_resolved_restoration():
    today = date(2026, 10, 10)
    event_resolved = StatusUpdateEvent(
        update_type=UpdateType.PAIN_UPDATE,
        details={"location": "shoulder", "status": "resolved"}
    )

    # Rest workout for swimming restored to 1500m Z1
    rest_swim = make_dummy_workout("s1", date(2026, 10, 11), "rest", 0.0, SportType.SWIMMING, "m")
    rest_swim.intensity_detail = "Schmerz-Pause"
    mutations = StatusUpdateHandler.handle_status_update(event_resolved, today, [rest_swim])
    assert len(mutations) == 1
    assert rest_swim.workout_type == "technique_drills"
    assert rest_swim.metric_primary == 1500.0
    assert rest_swim.metric_unit == "m"

    # Rest workout for strength restored to 10 sets hypertrophy
    rest_str = make_dummy_workout("st1", date(2026, 10, 11), "rest", 0.0, SportType.STRENGTH, "sets")
    rest_str.intensity_detail = "Schmerz-Pause"
    mutations2 = StatusUpdateHandler.handle_status_update(event_resolved, today, [rest_str])
    assert len(mutations2) == 1
    assert rest_str.workout_type == "hypertrophy"
    assert rest_str.metric_primary == 10.0
    assert rest_str.metric_unit == "sets"


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


def test_replan_engine_multisport():
    today = date(2026, 10, 15)
    target = date(2026, 12, 31)

    # 1. Swimming replan
    swim_weeks = ReplanEngine.replan(
        plan_id="swim-replan",
        sport_type="swimming",
        goal_type="technique",
        current_date=today,
        target_date=target,
        reference_value=105.0,
        last_achieved_weekly_volume=3500.0
    )
    assert len(swim_weeks) >= 10
    # First week should be 3500 * 0.85 * 1.08 = 3213.0m
    assert swim_weeks[0].target_weekly_volume == pytest.approx(3213.0, abs=10.0)
    assert swim_weeks[0].workouts[0].metric_unit == "m"

    # 2. Strength replan
    strength_weeks = ReplanEngine.replan(
        plan_id="str-replan",
        sport_type="strength",
        goal_type="hypertrophy",
        current_date=today,
        target_date=target,
        reference_value=100.0,
        last_achieved_weekly_volume=16.0
    )
    assert len(strength_weeks) >= 10
    # First week has 3 DUP sessions (~17, ~16, ~15 sets = ~48 total weekly sets)
    assert strength_weeks[0].target_weekly_volume == pytest.approx(48.0, abs=2.0)
    assert strength_weeks[0].workouts[0].metric_unit == "sets"

    # 3. Cycling replan
    bike_weeks = ReplanEngine.replan(
        plan_id="bike-replan",
        sport_type="cycling",
        goal_type="ftp_builder",
        current_date=today,
        target_date=target,
        reference_value=240.0,
        last_achieved_weekly_volume=300.0
    )
    assert len(bike_weeks) >= 10
    # 4 sessions totaling 60 + 75 + 45 + 120 = 300 min
    assert bike_weeks[0].target_weekly_volume == 300.0
    assert bike_weeks[0].workouts[0].metric_unit == "min"
    # Adjusted baseline TSS is 300 * 0.85 = 255.0 TSS -> Week 1 build ~ 272.8 TSS
    assert bike_weeks[0].target_weekly_tss == pytest.approx(272.8, abs=2.0)

