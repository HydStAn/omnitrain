"""RunningStrategy implementation according to OmniTrain SPECIFICATION.md."""
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from omnitrain.core.models import PhaseType, SportType, Week, Workout
from omnitrain.sports.daniels import get_daniels_zones


class RunningStrategy:
    """
    Implements Daniels VDOT / Pfitzinger running periodization:
      - 3:1 Mesocycle structure (3 progressive weeks, 1 recovery week at -25% volume)
      - Dynamic volume increase based on baseline:
          < 30 km/week: max 8%
          30-60 km/week: max 8-10%
          > 60 km/week: max 5-8%
      - Long run cap at 30-33% of weekly volume (max 32-34 km)
      - Tapering over the final 3 weeks (-20%, -40%, -60% from peak)
    """

    @staticmethod
    def get_max_progression_rate(current_volume: float) -> float:
        if current_volume < 30.0:
            return 0.08
        elif current_volume <= 60.0:
            return 0.09
        else:
            return 0.06

    @classmethod
    def calculate_weekly_volumes(
        cls,
        base_volume: float,
        total_weeks: int
    ) -> List[dict]:
        """Generate weekly volume and phase for each week."""
        volumes: List[dict] = []
        current_vol = base_volume

        # Allocate phases:
        # Last 3 weeks are taper
        taper_weeks = min(3, max(1, total_weeks // 6)) if total_weeks >= 6 else 1
        training_weeks = total_weeks - taper_weeks

        # Mesocycle: 3 weeks build, 1 week recovery
        for w in range(1, training_weeks + 1):
            is_recovery = (w % 4 == 0) and (w != training_weeks)
            
            if w == 1:
                vol = base_volume
                phase = PhaseType.BASE
            else:
                prev_vol = volumes[-1]["target_volume"]
                if is_recovery:
                    vol = round(prev_vol * 0.75, 1)
                    phase = PhaseType.BASE if w <= total_weeks // 2 else PhaseType.BUILD
                else:
                    prev_normal_vol = volumes[-2]["target_volume"] if volumes[-1]["is_recovery"] else prev_vol
                    rate = cls.get_max_progression_rate(prev_normal_vol)
                    vol = round(prev_normal_vol * (1.0 + rate), 1)
                    phase = PhaseType.BASE if w <= total_weeks // 3 else (PhaseType.BUILD if w <= training_weeks - 2 else PhaseType.PEAK)

            volumes.append({
                "week_number": w,
                "target_volume": vol,
                "phase": phase,
                "is_recovery": is_recovery
            })

        peak_volume = max(v["target_volume"] for v in volumes)

        # Taper weeks
        taper_cuts = [0.80, 0.60, 0.40] if taper_weeks == 3 else [0.70, 0.50]
        for i, cut in enumerate(taper_cuts[:taper_weeks]):
            w = training_weeks + 1 + i
            vol = round(peak_volume * cut, 1)
            volumes.append({
                "week_number": w,
                "target_volume": vol,
                "phase": PhaseType.TAPER,
                "is_recovery": False
            })

        return volumes

    @classmethod
    def generate_plan(
        cls,
        plan_id: str,
        start_date: date,
        target_date: date,
        base_weekly_volume: float,
        available_days: List[int],  # 1=Mo ... 7=So
        vdot: float = 45.0,
        sessions_per_week: int = 4,
        key_workout_day: int = 7
    ) -> List[Week]:
        total_days = (target_date - start_date).days
        total_weeks = max(4, total_days // 7)

        weekly_specs = cls.calculate_weekly_volumes(base_weekly_volume, total_weeks)
        zones = get_daniels_zones(vdot)
        weeks: List[Week] = []

        for w_spec in weekly_specs:
            w_idx = w_spec["week_number"]
            w_start = start_date + timedelta(days=(w_idx - 1) * 7)
            w_id = str(uuid.uuid4())
            w_vol = w_spec["target_volume"]

            workouts: List[Workout] = []

            # 4-day distribution:
            # Day 1: Easy (20%)
            # Day 2: Tempo / Threshold (20%)
            # Day 3: Easy (25%)
            # Day 4: Longrun (35%, capped at 32-34km)
            selected_days = sorted(available_days)
            if len(selected_days) < sessions_per_week:
                selected_days = [2, 4, 6, 7][:sessions_per_week]

            # Long run volume
            long_run_vol = min(round(w_vol * 0.35, 1), 34.0)
            remaining_vol = w_vol - long_run_vol
            easy1_vol = round(remaining_vol * (20 / 65), 1)
            tempo_vol = round(remaining_vol * (20 / 65), 1)
            easy2_vol = round(remaining_vol - easy1_vol - tempo_vol, 1)

            distributions = [
                ("easy", easy1_vol, "easy", f"Pace: {zones['easy']['min_pace']}–{zones['easy']['max_pace']} min/km"),
                ("tempo", tempo_vol, "threshold", f"Pace: {zones['threshold']['pace']} min/km"),
                ("easy", easy2_vol, "easy", f"Pace: {zones['easy']['min_pace']}–{zones['easy']['max_pace']} min/km"),
                ("long_run", long_run_vol, "easy", f"Pace: {zones['easy']['min_pace']}–{zones['easy']['max_pace']} min/km")
            ]

            # Assign to days
            for idx, (w_type, dist_km, intensity, detail) in enumerate(distributions[:len(selected_days)]):
                day_num = selected_days[idx]
                workout_date = w_start + timedelta(days=(day_num - 1))
                workouts.append(Workout(
                    id=str(uuid.uuid4()),
                    week_id=w_id,
                    sport_type=SportType.RUNNING,
                    date=workout_date,
                    day_of_week=day_num,
                    workout_type=w_type,
                    metric_primary=dist_km,
                    metric_unit="km",
                    intensity_target=intensity,
                    intensity_detail=detail,
                    status="planned"
                ))

            weeks.append(Week(
                id=w_id,
                plan_id=plan_id,
                week_number=w_idx,
                week_start_date=w_start,
                phase=w_spec["phase"],
                target_weekly_volume=w_vol,
                target_weekly_tss=round(w_vol * 7.5, 1), # Approx rTSS
                is_recovery_week=w_spec["is_recovery"],
                workouts=workouts
            ))

        return weeks
