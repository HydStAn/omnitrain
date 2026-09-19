"""Unit tests for Triathlon multi-sport periodization and Brick workouts."""
from datetime import date
import json
import pytest
from omnitrain.core.models import SportType
from omnitrain.sports.triathlon import TriathlonStrategy


def test_triathlon_plan_generation_and_brick_phase():
    start = date(2026, 10, 5)
    target = date(2026, 12, 28)  # 12 weeks
    weeks = TriathlonStrategy.generate_plan(
        plan_id="plan-tri",
        start_date=start,
        target_date=target,
        base_weekly_volume=350.0,
        limiter="swim",
        target_event="triathlon_olympic"
    )

    assert len(weeks) == 12
    # Check that each week contains swim, cycling, and running
    w1_sports = {wo.sport_type for wo in weeks[0].workouts}
    assert SportType.SWIMMING in w1_sports
    assert SportType.CYCLING in w1_sports
    assert SportType.RUNNING in w1_sports

    # Brick phase: starting 8 weeks prior to race (Week 5 to 12)
    # Week 8 must contain a Brick Workout on Saturday (2 workouts on the same date: bike + run)
    w8_workouts = weeks[7].workouts
    sat_workouts = [wo for wo in w8_workouts if wo.day_of_week == 6]
    assert len(sat_workouts) == 2  # Bike Leg + Run Leg
    types = {wo.workout_type for wo in sat_workouts}
    assert "brick_bike" in types and "brick_run" in types

    # Verify JSON structure in brick
    structure = json.loads(sat_workouts[0].structure_json)
    assert "brick" in structure
    assert structure["brick"][1]["transition"] == "T2"
