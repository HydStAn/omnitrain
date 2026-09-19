"""Swimming strategy implementing Critical Swim Speed (CSS), 5-zone model, and sTSS."""
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List
from omnitrain.core.models import PhaseType, SportType, Week, Workout
from omnitrain.sports.daniels import seconds_to_pace


def calculate_css_from_time_trials(t400_seconds: float, t200_seconds: float) -> float:
    """
    Calculates Critical Swim Speed (CSS) from 400m and 200m time trials:
      CSS (m/s) = (400 - 200) / (t400 - t200)
    Returns pace in seconds per 100m.
    """
    delta_t = t400_seconds - t200_seconds
    if delta_t <= 0:
        raise ValueError("t400 must be greater than t200")
    css_mps = 200.0 / delta_t
    pace_100m_sec = 100.0 / css_mps
    return round(pace_100m_sec, 1)


def calculate_swim_css_zones(css_pace_sec_100m: float) -> Dict[str, Dict[str, Any]]:
    """
    CSS 5-Zone Model (Pace per 100m):
      Z1 Recovery: CSS + 15-20 s/100m
      Z2 Endurance: CSS + 5-10 s/100m
      Z3 Threshold (CSS): CSS ± 3 s/100m
      Z4 VO2max: CSS - 5-10 s/100m
      Z5 Sprint: CSS - 15+ s/100m
    """
    return {
        "Z1": {
            "name": "Recovery",
            "pace": seconds_to_pace(css_pace_sec_100m + 18.0),
            "description": "Aktive Erholung, Technikfokus"
        },
        "Z2": {
            "name": "Endurance",
            "pace": seconds_to_pace(css_pace_sec_100m + 8.0),
            "description": "Aerobe Grundlage, lange Ausdauersets"
        },
        "Z3": {
            "name": "Threshold (CSS)",
            "pace": seconds_to_pace(css_pace_sec_100m),
            "description": "Schwellentempo (Dauerschwimm-Tempo)"
        },
        "Z4": {
            "name": "VO2max",
            "pace": seconds_to_pace(max(30.0, css_pace_sec_100m - 7.0)),
            "description": "Kurze intensive Intervalle (100-200m)"
        },
        "Z5": {
            "name": "Sprint",
            "pace": seconds_to_pace(max(20.0, css_pace_sec_100m - 15.0)),
            "description": "Maximalsprints (25-50m)"
        }
    }


def calculate_stss(duration_seconds: float, actual_pace_sec_100m: float, css_pace_sec_100m: float) -> float:
    """
    Calculate Swim Training Stress Score (sTSS):
      speed_ratio = css_pace / actual_pace
      sTSS = (duration_seconds * (speed_ratio^3 or speed_ratio^2) * 100) / 3600
    """
    if actual_pace_sec_100m <= 0 or css_pace_sec_100m <= 0:
        return 0.0
    speed_ratio = css_pace_sec_100m / actual_pace_sec_100m
    stss = (duration_seconds * (speed_ratio ** 2) * 100.0) / 3600.0
    return round(stss, 1)


class SwimStrategy:
    """CSS-based swimming plan generator."""

    def calculate_zones(self, reference_value: float) -> Dict[str, Any]:
        """reference_value: CSS pace in seconds per 100m (e.g. 105.0 sec = 1:45/100m)."""
        return calculate_swim_css_zones(reference_value)

    def calculate_tss(self, duration_seconds: float, **metrics: Any) -> float:
        actual_pace = metrics.get("actual_pace_sec_100m", 110.0)
        css_pace = metrics.get("css_pace_sec_100m", 105.0)
        return calculate_stss(duration_seconds, actual_pace, css_pace)

    def generate_plan(
        self,
        plan_id: str,
        start_date: date,
        target_date: date,
        base_weekly_volume: float,  # in meters (e.g. 5000m/week)
        available_days: List[int],
        reference_value: float = 105.0, # 1:45/100m
        sessions_per_week: int = 3,
        **kwargs: Any
    ) -> List[Week]:
        total_days = (target_date - start_date).days
        total_weeks = max(4, total_days // 7)
        zones = self.calculate_zones(reference_value)

        weeks: List[Week] = []
        current_meters = base_weekly_volume
        selected_days = sorted(available_days)[:sessions_per_week]

        for w in range(1, total_weeks + 1):
            w_start = start_date + timedelta(days=(w - 1) * 7)
            w_id = str(uuid.uuid4())
            is_recovery = (w % 4 == 0) and (w != total_weeks)

            if is_recovery:
                target_m = round(current_meters * 0.75)
                phase = PhaseType.BASE
            elif w >= total_weeks - 1:
                target_m = round(current_meters * 0.60)
                phase = PhaseType.TAPER
            else:
                target_m = round(current_meters * 1.08)
                current_meters = target_m
                phase = PhaseType.BUILD if w <= total_weeks - 3 else PhaseType.PEAK

            session_types = [
                ("technique_drills", 0.30, "Z1", f"Technikdrills & Sculling in {zones['Z1']['pace']}/100m"),
                ("css_threshold", 0.35, "Z3", f"CSS Intervalle (z.B. 10x100m) in {zones['Z3']['pace']}/100m"),
                ("endurance_pull", 0.35, "Z2", f"Ausdauer mit Pullbuoy in {zones['Z2']['pace']}/100m")
            ]

            workouts: List[Workout] = []
            for idx, d in enumerate(selected_days):
                st_type, frac, zone, detail = session_types[idx % len(session_types)]
                dist_m = round(target_m * frac)
                workouts.append(Workout(
                    id=str(uuid.uuid4()),
                    week_id=w_id,
                    sport_type=SportType.SWIMMING,
                    date=w_start + timedelta(days=d - 1),
                    day_of_week=d,
                    workout_type=st_type,
                    metric_primary=float(dist_m),
                    metric_unit="m",
                    intensity_target=zone,
                    intensity_detail=detail,
                    status="planned"
                ))

            weeks.append(Week(
                id=w_id,
                plan_id=plan_id,
                week_number=w,
                week_start_date=w_start,
                phase=phase,
                target_weekly_volume=float(target_m),
                target_weekly_tss=round(target_m * 0.04, 1), # ~40 sTSS per 1000m
                is_recovery_week=is_recovery,
                workouts=workouts
            ))

        return weeks
