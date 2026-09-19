"""Comprehensive tests for Cycling, Swimming, Strength and Cross-Sport TSS calculations."""
from datetime import date
import pytest
from omnitrain.sports.cycling import (
    calculate_ftp_20min,
    calculate_ftp_ramp,
    calculate_coggan_zones,
    calculate_btss,
    CyclingStrategy
)
from omnitrain.sports.swimming import (
    calculate_css_from_time_trials,
    calculate_swim_css_zones,
    calculate_stss,
    SwimStrategy
)
from omnitrain.sports.strength import (
    calculate_dots_score,
    StrengthStrategy,
    MOVEMENT_PATTERNS
)


def test_cycling_ftp_and_coggan_zones():
    # 20 min TT at 250W -> FTP 237.5W
    ftp = calculate_ftp_20min(250.0)
    assert ftp == 237.5

    # Ramp test 320W final step -> FTP 240W
    ftp_ramp = calculate_ftp_ramp(320.0)
    assert ftp_ramp == 240.0

    zones = calculate_coggan_zones(240.0)
    assert "Z1" in zones and "Z7" in zones
    assert zones["Z4"]["min_watts"] == round(240 * 0.91)
    assert zones["Z4"]["max_watts"] == round(240 * 1.05)


def test_cycling_btss_and_plan():
    # 1 hour at exact FTP (IF = 1.0) -> exactly 100 TSS
    btss = calculate_btss(duration_seconds=3600, np_watts=200, ftp=200)
    assert btss == 100.0

    strategy = CyclingStrategy()
    weeks = strategy.generate_plan(
        plan_id="plan-bike",
        start_date=date(2026, 10, 5),
        target_date=date(2026, 12, 28),
        base_weekly_volume=300.0,
        available_days=[2, 4, 6, 7],
        reference_value=250.0
    )
    assert len(weeks) >= 11
    assert weeks[3].is_recovery_week is True
    assert weeks[3].target_weekly_tss < weeks[2].target_weekly_tss


def test_swimming_css_and_zones():
    # 400m in 6:30 (390s), 200m in 3:00 (180s) -> CSS = 200 / 210 = 0.952 m/s -> 105.0 s/100m (1:45)
    css_sec_100m = calculate_css_from_time_trials(390.0, 180.0)
    assert 104.5 <= css_sec_100m <= 105.5

    zones = calculate_swim_css_zones(css_sec_100m)
    assert "Z1" in zones and "Z5" in zones
    assert zones["Z3"]["pace"] == "01:45"


def test_swimming_stss_and_plan():
    # 3600s swimming exactly at CSS pace -> 100 sTSS
    stss = calculate_stss(3600, 105.0, 105.0)
    assert stss == 100.0

    strategy = SwimStrategy()
    weeks = strategy.generate_plan(
        plan_id="plan-swim",
        start_date=date(2026, 10, 5),
        target_date=date(2026, 12, 14),
        base_weekly_volume=4000.0,
        available_days=[1, 3, 5],
        reference_value=105.0
    )
    assert len(weeks) >= 9
    assert weeks[0].workouts[0].metric_unit == "m"


def test_strength_strategy_and_dup():
    strategy = StrengthStrategy()
    zones = strategy.calculate_zones(100.0)
    assert "hypertrophy" in zones
    assert "max_strength" in zones

    # 7 Movement patterns must be present
    assert len(MOVEMENT_PATTERNS) == 7
    assert "squat" in MOVEMENT_PATTERNS and "hinge" in MOVEMENT_PATTERNS

    weeks = strategy.generate_plan(
        plan_id="plan-strength",
        start_date=date(2026, 10, 5),
        target_date=date(2026, 12, 28),
        base_weekly_volume=12.0,
        available_days=[1, 3, 5],
        sessions_per_week=3
    )
    assert len(weeks) >= 11
    # 4th week is deload week (-40% volume)
    assert weeks[3].is_recovery_week is True
    assert weeks[3].target_weekly_volume < weeks[2].target_weekly_volume


def test_dots_score_calculation():
    # 85kg lifter with 600kg total
    dots = calculate_dots_score(weight_kg=85.0, total_kg=600.0)
    assert 380.0 <= dots <= 430.0
