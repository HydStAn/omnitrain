"""Replan engine for structural plan updates (Section 5D)."""
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from omnitrain.core.models import CompletionStatus, Week, Workout
from omnitrain.sports.running import RunningStrategy


class ReplanEngine:
    """
    Triggers structural re-generation of the plan (Section 5D):
      1. Long-term hiatus (> 2 consecutive weeks of inactivity/sickness):
         - Re-calibrates base volume starting from latest completed week
      2. Goal change:
         - Shifts target_date or goal distance
      3. Fitness change:
         - Re-calculates paces and intensity_detail
    """

    @classmethod
    def replan_running(
        cls,
        plan_id: str,
        current_date: date,
        target_date: date,
        vdot: float,
        last_achieved_weekly_volume: float,
        available_days: List[int] = [2, 4, 6, 7],
        sessions_per_week: int = 4
    ) -> List[Week]:
        # Calibrate starting baseline conservatively (max 85% of last achieved if coming from hiatus)
        adjusted_baseline = max(15.0, round(last_achieved_weekly_volume * 0.85, 1))

        # Start from next Monday
        days_ahead = (7 - current_date.weekday()) % 7
        next_monday = current_date + timedelta(days=days_ahead if days_ahead > 0 else 7)

        return RunningStrategy.generate_plan(
            plan_id=plan_id,
            start_date=next_monday,
            target_date=target_date,
            base_weekly_volume=adjusted_baseline,
            available_days=available_days,
            vdot=vdot,
            sessions_per_week=sessions_per_week
        )
