"""Strength training strategy implementing DUP, 7 movement patterns, MEV/MAV/MRV landmarks, and RIR/RPE."""
import json
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List
from omnitrain.core.models import PhaseType, SportType, Week, Workout


# 7 Core Movement Patterns according to SPECIFICATION.md
MOVEMENT_PATTERNS = {
    "squat": ["Back Squat", "Front Squat", "Goblet Squat", "Split Squat"],
    "hinge": ["Deadlift", "Romanian Deadlift", "Hip Thrust"],
    "vertical_push": ["Overhead Press", "Push Press", "Dumbbell Shoulder Press"],
    "vertical_pull": ["Pull-Up", "Chin-Up", "Lat Pulldown"],
    "horizontal_push": ["Bench Press", "Incline DB Press", "Push-Up"],
    "horizontal_pull": ["Barbell Row", "Cable Row", "Dumbbell Row"],
    "core_carry": ["Farmer's Walk", "Plank", "Pallof Press"]
}


def calculate_dots_score(weight_kg: float, total_kg: float, is_female: bool = False) -> float:
    """
    Calculates IPF DOTS Points for comparative relative strength:
      DOTS = Total * 500 / (A*w^4 + B*w^3 + C*w^2 + D*w + E)
    Official IPF constants:
      Men:
        a = -0.0000010930
        b = 0.00073864
        c = -0.14380
        d = 12.892
        e = -329.64
      Women:
        a = -0.0000010706
        b = 0.0005158568
        c = -0.1126655495
        d = 13.6175032
        e = -362.476694
    """
    w = weight_kg
    if is_female:
        denom = (
            -0.0000010706 * (w ** 4)
            + 0.0005158568 * (w ** 3)
            - 0.1126655495 * (w ** 2)
            + 13.6175032 * w
            - 57.96288
        )
    else:
        denom = (
            -0.0000010930 * (w ** 4)
            + 0.0007391293 * (w ** 3)
            - 0.1918759221 * (w ** 2)
            + 24.0900756 * w
            - 307.75076
        )
    if denom <= 0:
        return 0.0
    return round(total_kg * 500.0 / denom, 2)


class StrengthStrategy:
    """
    Daily Undulating Periodization (DUP) & Renaissance Periodization (MEV/MAV/MRV).
    """

    def calculate_zones(self, reference_value: float = 100.0) -> Dict[str, Any]:
        """Intensity zones based on Reps In Reserve (RIR) and % 1RM."""
        return {
            "max_strength": {
                "name": "Maximalkraft (1-5 Wdh)",
                "intensity": "85-100% 1RM",
                "rpe": "8-10",
                "rir": "0-2 RIR",
                "rest": "3-5 min"
            },
            "hypertrophy": {
                "name": "Hypertrophie (6-12 Wdh)",
                "intensity": "65-80% 1RM",
                "rpe": "7-9",
                "rir": "1-3 RIR",
                "rest": "60-120 s"
            },
            "endurance_power": {
                "name": "Kraftausdauer / Speed (12-20 Wdh)",
                "intensity": "50-65% 1RM",
                "rpe": "6-8",
                "rir": "2-4 RIR",
                "rest": "30-60 s"
            }
        }

    def calculate_tss(self, duration_seconds: float, **metrics: Any) -> float:
        """
        wTSS (Weight Training Stress Score):
        Rough estimate based on working sets and average RPE:
          wTSS ~ total_sets * (average_rpe / 10) * 3.5
        """
        sets = metrics.get("sets", 15)
        avg_rpe = metrics.get("rpe", 7.5)
        return round(sets * (avg_rpe / 10.0) * 3.5, 1)

    def generate_plan(
        self,
        plan_id: str,
        start_date: date,
        target_date: date,
        base_weekly_volume: float = 12.0,  # e.g. 12 sets per major muscle group (MAV)
        available_days: List[int] = [1, 3, 5], # Mo, Mi, Fr
        reference_value: float = 100.0,
        sessions_per_week: int = 3,
        **kwargs: Any
    ) -> List[Week]:
        total_days = (target_date - start_date).days
        total_weeks = max(4, total_days // 7)

        weeks: List[Week] = []
        selected_days = sorted(available_days)[:sessions_per_week]

        for w in range(1, total_weeks + 1):
            w_start = start_date + timedelta(days=(w - 1) * 7)
            w_id = str(uuid.uuid4())
            # Deload every 4th week (-40% volume / MEV)
            is_deload = (w % 4 == 0)

            if is_deload:
                phase = PhaseType.BASE
                vol_factor = 0.60
            elif w >= total_weeks - 1:
                phase = PhaseType.PEAK
                vol_factor = 0.70
            else:
                phase = PhaseType.BUILD
                vol_factor = 1.0 + (w * 0.05) # progressive overload

            workouts: List[Workout] = []
            
            # DUP Schedule across 3 days:
            # Day 1: Hypertrophy (Higher Reps, 8-10 reps @ RPE 7-8)
            # Day 2: Strength (Heavy Lower Reps, 3-5 reps @ RPE 8-9)
            # Day 3: Power / Upper-Lower Accessory (6-8 reps @ RPE 7.5)
            dup_sessions = [
                ("DUP: Hypertrophy Focus", "hypertrophy", "3x8-10 @ RPE 7.5 (2 RIR)", 16),
                ("DUP: Maximal Strength Focus", "strength", "5x3-5 @ RPE 8.5 (1-2 RIR)", 15),
                ("DUP: Power / Balanced Movement", "power", "4x6 @ RPE 7.5 (2-3 RIR)", 14)
            ]

            for idx, d in enumerate(selected_days):
                name, target_type, detail, base_sets = dup_sessions[idx % len(dup_sessions)]
                effective_sets = round(base_sets * vol_factor)
                workouts.append(Workout(
                    id=str(uuid.uuid4()),
                    week_id=w_id,
                    sport_type=SportType.STRENGTH,
                    date=w_start + timedelta(days=d - 1),
                    day_of_week=d,
                    workout_type=target_type,
                    metric_primary=float(effective_sets),
                    metric_unit="sets",
                    intensity_target=target_type,
                    intensity_detail=detail,
                    target_duration_min=effective_sets * 4,
                    structure_json=json.dumps({"patterns": list(MOVEMENT_PATTERNS.keys())}),
                    status="planned"
                ))

            total_weekly_sets = sum(wo.metric_primary for wo in workouts)
            weeks.append(Week(
                id=w_id,
                plan_id=plan_id,
                week_number=w,
                week_start_date=w_start,
                phase=phase,
                target_weekly_volume=float(total_weekly_sets),
                target_weekly_tss=round(total_weekly_sets * 2.8, 1),
                is_recovery_week=is_deload,
                workouts=workouts
            ))

        return weeks
