"""Replan engine for structural plan updates across all sports (Section 5D)."""
from datetime import date, timedelta
from typing import Any, Dict, List, Optional
from omnitrain.core.models import CompletionStatus, Week, Workout
from omnitrain.sports.cycling import CyclingStrategy
from omnitrain.sports.running import RunningStrategy
from omnitrain.sports.strength import StrengthStrategy
from omnitrain.sports.swimming import SwimStrategy
from omnitrain.sports.triathlon import TriathlonStrategy


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
    def replan(
        cls,
        plan_id: str,
        sport_type: str,
        goal_type: str,
        current_date: date,
        target_date: date,
        reference_value: float,
        last_achieved_weekly_volume: float,
        available_days: Optional[List[int]] = None,
        sessions_per_week: Optional[int] = None
    ) -> List[Week]:
        # Start from next Monday
        days_ahead = (7 - current_date.weekday()) % 7
        next_monday = current_date + timedelta(days=days_ahead if days_ahead > 0 else 7)

        if sport_type == "cycling":
            strat = CyclingStrategy()
            adjusted_baseline = max(100.0, round(last_achieved_weekly_volume * 0.85, 1))
            days = available_days or [2, 4, 6, 7]
            sessions = sessions_per_week or 4
            return strat.generate_plan(
                plan_id=plan_id,
                start_date=next_monday,
                target_date=target_date,
                base_weekly_volume=adjusted_baseline,
                available_days=days,
                reference_value=reference_value if reference_value > 100 else 220.0,
                sessions_per_week=sessions
            )
        elif sport_type == "swimming":
            strat = SwimStrategy()
            adjusted_baseline = max(1500.0, round(last_achieved_weekly_volume * 0.85, 0))
            days = available_days or [1, 3, 5]
            sessions = sessions_per_week or 3
            return strat.generate_plan(
                plan_id=plan_id,
                start_date=next_monday,
                target_date=target_date,
                base_weekly_volume=adjusted_baseline,
                available_days=days,
                reference_value=reference_value if reference_value > 50 else 105.0,
                sessions_per_week=sessions
            )
        elif sport_type == "strength":
            strat = StrengthStrategy()
            adjusted_baseline = max(8.0, round(last_achieved_weekly_volume * 0.85, 0))
            days = available_days or [1, 3, 5]
            sessions = sessions_per_week or 3
            return strat.generate_plan(
                plan_id=plan_id,
                start_date=next_monday,
                target_date=target_date,
                base_weekly_volume=adjusted_baseline,
                available_days=days,
                reference_value=reference_value,
                sessions_per_week=sessions
            )
        elif sport_type in ["triathlon", "multisport"]:
            adjusted_baseline = max(150.0, round(last_achieved_weekly_volume * 0.85, 1))
            days = available_days or [1, 2, 3, 4, 5, 6, 7]
            return TriathlonStrategy.generate_plan(
                plan_id=plan_id,
                start_date=next_monday,
                target_date=target_date,
                base_weekly_volume=adjusted_baseline,
                available_days=days,
                limiter="swim",
                target_event=goal_type if "triathlon" in goal_type else "triathlon_olympic"
            )
        else:  # running
            adjusted_baseline = max(15.0, round(last_achieved_weekly_volume * 0.85, 1))
            days = available_days or [2, 4, 6, 7]
            sessions = sessions_per_week or 4
            return RunningStrategy.generate_plan(
                plan_id=plan_id,
                start_date=next_monday,
                target_date=target_date,
                base_weekly_volume=adjusted_baseline,
                available_days=days,
                vdot=reference_value,
                sessions_per_week=sessions
            )

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
        return cls.replan(
            plan_id=plan_id,
            sport_type="running",
            goal_type="marathon",
            current_date=current_date,
            target_date=target_date,
            reference_value=vdot,
            last_achieved_weekly_volume=last_achieved_weekly_volume,
            available_days=available_days,
            sessions_per_week=sessions_per_week
        )
