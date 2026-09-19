"""Unit tests for sports science calculations: Daniels VDOT, RunningStrategy, and PMC."""
from datetime import date, timedelta
import pytest
from omnitrain.sports.daniels import (
    calculate_vdot_from_race,
    get_daniels_zones,
    pace_to_seconds,
    seconds_to_pace
)
from omnitrain.sports.running import RunningStrategy
from omnitrain.load.pmc import PMCEngine, DailyLoad

def test_vdot_calculation():
    # 5000m in 20:00 (1200 seconds) -> VDOT approx 49.8 - 50.0
    vdot = calculate_vdot_from_race(5000, 1200)
    assert 49.0 <= vdot <= 51.0

    # 10000m in 40:00 (2400 seconds)
    vdot_10k = calculate_vdot_from_race(10000, 2400)
    assert 50.0 <= vdot_10k <= 53.0

def test_daniels_zones():
    zones = get_daniels_zones(vdot=50.0)
    assert "easy" in zones
    assert "threshold" in zones
    assert "interval" in zones
    
    # Check that interval pace is faster than threshold, and threshold faster than easy
    t_pace = pace_to_seconds(zones["threshold"]["pace"])
    i_pace = pace_to_seconds(zones["interval"]["pace"])
    e_pace_slow = pace_to_seconds(zones["easy"]["max_pace"])
    
    assert i_pace < t_pace < e_pace_slow

def test_running_strategy_plan_generation():
    start = date(2026, 10, 5) # Monday
    target = date(2027, 2, 28) # ~21 weeks
    weeks = RunningStrategy.generate_plan(
        plan_id="test-plan-1",
        start_date=start,
        target_date=target,
        base_weekly_volume=30.0,
        available_days=[2, 4, 6, 7],
        vdot=48.0,
        sessions_per_week=4
    )
    
    assert len(weeks) >= 20
    # Check 3:1 mesocycle: week 4 should be recovery
    assert weeks[3].is_recovery_week is True
    assert weeks[3].target_weekly_volume < weeks[2].target_weekly_volume
    
    # Check last week is taper
    assert weeks[-1].phase == "taper"
    
    # Check long run cap
    for w in weeks:
        long_runs = [wo for wo in w.workouts if wo.workout_type == "long_run"]
        assert len(long_runs) == 1
        assert long_runs[0].metric_primary <= 34.0

def test_pmc_engine():
    # Simulate 14 days of 50 TSS/day
    loads = [DailyLoad(date=f"2026-10-{i:02d}", tss=50.0) for i in range(1, 15)]
    series = PMCEngine.calculate_pmc_series(loads, initial_ctl=0.0, initial_atl=0.0)
    
    assert len(series) == 14
    # ATL (tau=7) rises much faster than CTL (tau=42)
    assert series[-1].atl > series[-1].ctl
    # TSB = CTL - ATL should be negative during heavy training load
    assert series[-1].tsb < 0.0

def test_running_rtss_calculation():
    # 3600 seconds (1 hour) at exact threshold pace should be exactly 100 rTSS
    rtss = PMCEngine.calculate_running_rtss(
        duration_seconds=3600,
        actual_pace_sec_km=240, # 4:00/km
        threshold_pace_sec_km=240
    )
    assert rtss == 100.0

    # Slower run (easy pace, 5:00/km = 300 sec)
    rtss_easy = PMCEngine.calculate_running_rtss(
        duration_seconds=3600,
        actual_pace_sec_km=300,
        threshold_pace_sec_km=240
    )
    assert 60.0 <= rtss_easy <= 68.0
