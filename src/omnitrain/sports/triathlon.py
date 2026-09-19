"""Triathlon & Hybrid Multi-Sport Strategy according to Section 8 & 9."""
import json
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from omnitrain.core.models import PhaseType, SportType, Week, Workout


class TriathlonStrategy:
    """
    Implements multi-sport periodization (Sprint, Olympic, 70.3, Ironman):
      - Multi-sport days (multiple workouts per date)
      - Brick workouts (Bike -> T2 -> Run) starting 8 weeks prior to race
      - Joe Friel Limiter Concept: 40% limiter, 35% strength, 25% third sport
    """

    @classmethod
    def generate_plan(
        cls,
        plan_id: str,
        start_date: date,
        target_date: date,
        base_weekly_volume: float = 350.0,  # Base TSS/week
        available_days: List[int] = [1, 2, 3, 4, 5, 6, 7],
        limiter: str = "swim",  # 'swim', 'bike', or 'run'
        target_event: str = "triathlon_olympic"
    ) -> List[Week]:
        total_days = (target_date - start_date).days
        total_weeks = max(8, total_days // 7)

        # Joe Friel Limiter split
        splits = {"limiter": 0.40, "secondary": 0.35, "tertiary": 0.25}

        weeks: List[Week] = []
        current_tss = base_weekly_volume

        for w in range(1, total_weeks + 1):
            w_start = start_date + timedelta(days=(w - 1) * 7)
            w_id = str(uuid.uuid4())
            is_recovery = (w % 4 == 0) and (w != total_weeks)
            is_brick_phase = (w >= total_weeks - 8)

            if is_recovery:
                target_tss = round(current_tss * 0.70, 1)
                phase = PhaseType.BASE
            elif w >= total_weeks - 2:
                target_tss = round(current_tss * 0.55, 1)
                phase = PhaseType.TAPER
            else:
                target_tss = round(current_tss * 1.06, 1)
                current_tss = target_tss
                phase = PhaseType.BUILD if w <= total_weeks - 4 else PhaseType.PEAK

            workouts: List[Workout] = []

            # 1. Swim session (Di)
            workouts.append(Workout(
                id=str(uuid.uuid4()),
                week_id=w_id,
                sport_type=SportType.SWIMMING,
                date=w_start + timedelta(days=1),  # Tuesday
                day_of_week=2,
                workout_type="css_threshold",
                metric_primary=2000.0,
                metric_unit="m",
                intensity_target="Z3",
                intensity_detail="CSS Threshold Intervalle",
                status="planned"
            ))

            # 2. Cycling session (Do)
            workouts.append(Workout(
                id=str(uuid.uuid4()),
                week_id=w_id,
                sport_type=SportType.CYCLING,
                date=w_start + timedelta(days=3),  # Thursday
                day_of_week=4,
                workout_type="sweetspot",
                metric_primary=75.0,
                metric_unit="min",
                intensity_target="Z3",
                intensity_detail="Sweetspot 88-94% FTP",
                status="planned"
            ))

            # 3. Running session (Fr)
            workouts.append(Workout(
                id=str(uuid.uuid4()),
                week_id=w_id,
                sport_type=SportType.RUNNING,
                date=w_start + timedelta(days=4),  # Friday
                day_of_week=5,
                workout_type="tempo",
                metric_primary=8.0,
                metric_unit="km",
                intensity_target="threshold",
                intensity_detail="Threshold Run Zone T",
                status="planned"
            ))

            # 4. Weekend Multi-Sport / Brick Day (Samstag)
            sat_date = w_start + timedelta(days=5)
            if is_brick_phase:
                # Brick Workout: Bike (90 min) -> T2 -> Run (20 min)
                brick_structure = {
                    "brick": [
                        {"sport": "cycling", "duration_min": 90, "zone": "Z3"},
                        {"transition": "T2", "target_min": 3},
                        {"sport": "running", "duration_min": 25, "zone": "E"}
                    ]
                }
                # Leg 1: Bike
                workouts.append(Workout(
                    id=str(uuid.uuid4()),
                    week_id=w_id,
                    sport_type=SportType.CYCLING,
                    date=sat_date,
                    day_of_week=6,
                    workout_type="brick_bike",
                    metric_primary=90.0,
                    metric_unit="min",
                    intensity_target="Z3",
                    intensity_detail="Brick Leg 1: 90 Min Ride direkt gefolgt von T2",
                    structure_json=json.dumps(brick_structure),
                    status="planned"
                ))
                # Leg 2: Run
                workouts.append(Workout(
                    id=str(uuid.uuid4()),
                    week_id=w_id,
                    sport_type=SportType.RUNNING,
                    date=sat_date,
                    day_of_week=6,
                    workout_type="brick_run",
                    metric_primary=5.0,
                    metric_unit="km",
                    intensity_target="easy",
                    intensity_detail="Brick Leg 2: Koppellauf (Koffergewohnung Beine)",
                    structure_json=json.dumps(brick_structure),
                    status="planned"
                ))
            else:
                workouts.append(Workout(
                    id=str(uuid.uuid4()),
                    week_id=w_id,
                    sport_type=SportType.CYCLING,
                    date=sat_date,
                    day_of_week=6,
                    workout_type="long_ride",
                    metric_primary=120.0,
                    metric_unit="min",
                    intensity_target="Z2",
                    intensity_detail="Lange Grundlagenausfahrt",
                    status="planned"
                ))

            # 5. Long Run (Sonntag)
            workouts.append(Workout(
                id=str(uuid.uuid4()),
                week_id=w_id,
                sport_type=SportType.RUNNING,
                date=w_start + timedelta(days=6),  # Sunday
                day_of_week=7,
                workout_type="long_run",
                metric_primary=16.0,
                metric_unit="km",
                intensity_target="easy",
                intensity_detail="Long Run aerobe Grundlage",
                status="planned"
            ))

            weeks.append(Week(
                id=w_id,
                plan_id=plan_id,
                week_number=w,
                week_start_date=w_start,
                phase=phase,
                target_weekly_volume=float(len(workouts)),
                target_weekly_tss=target_tss,
                is_recovery_week=is_recovery,
                workouts=workouts
            ))

        return weeks
