"""Cycling strategy implementing Coggan 7 power zones, FTP test protocols, and bTSS."""
import uuid
from datetime import date, timedelta
from typing import Any, Dict, List
from omnitrain.core.models import PhaseType, SportType, Week, Workout


def calculate_ftp_20min(avg_power_watts: float) -> float:
    """Standard 20-min TT protocol: FTP = 95% of 20-min average power."""
    return round(avg_power_watts * 0.95, 1)


def calculate_ftp_ramp(last_completed_step_watts: float) -> float:
    """Ramp protocol: FTP = 75% of last completed 1-minute step wattage."""
    return round(last_completed_step_watts * 0.75, 1)


def calculate_coggan_zones(ftp: float) -> Dict[str, Dict[str, Any]]:
    """
    Coggan 7-Zone Power Model:
      Z1: Active Recovery (< 55% FTP)
      Z2: Endurance (55-75% FTP)
      Z3: Tempo (76-90% FTP)
      Z4: Lactate Threshold (91-105% FTP)
      Z5: VO2 Max (106-120% FTP)
      Z6: Anaerobic Capacity (121-150% FTP)
      Z7: Neuromuscular Power (> 150% FTP)
    """
    return {
        "Z1": {
            "name": "Active Recovery",
            "min_watts": 0,
            "max_watts": round(ftp * 0.55),
            "description": "Erholung, Regeneration"
        },
        "Z2": {
            "name": "Endurance",
            "min_watts": round(ftp * 0.55),
            "max_watts": round(ftp * 0.75),
            "description": "Grundlagenausdauer, lange Einheiten"
        },
        "Z3": {
            "name": "Tempo / Sweetspot",
            "min_watts": round(ftp * 0.76),
            "max_watts": round(ftp * 0.90),
            "description": "Aerobe Belastung, Sweetspot bei 88-94%"
        },
        "Z4": {
            "name": "Lactate Threshold",
            "min_watts": round(ftp * 0.91),
            "max_watts": round(ftp * 1.05),
            "description": "Schwellenleistung (ca. 60 Min maximal haltbar)"
        },
        "Z5": {
            "name": "VO2 Max",
            "min_watts": round(ftp * 1.06),
            "max_watts": round(ftp * 1.20),
            "description": "Sauerstoffaufnahme-Intervalle (3-5 Min)"
        },
        "Z6": {
            "name": "Anaerobic Capacity",
            "min_watts": round(ftp * 1.21),
            "max_watts": round(ftp * 1.50),
            "description": "Kurze anaerobe Intervalle (30-120s)"
        },
        "Z7": {
            "name": "Neuromuscular Power",
            "min_watts": round(ftp * 1.51),
            "max_watts": None,
            "description": "Maximalsprints (< 15s)"
        }
    }


def calculate_btss(duration_seconds: float, np_watts: float, ftp: float) -> float:
    """
    Calculate Bike Training Stress Score (bTSS):
      IF = NP / FTP
      bTSS = (duration_seconds * NP * IF) / (FTP * 3600) * 100
    """
    if ftp <= 0 or np_watts <= 0:
        return 0.0
    intensity_factor = np_watts / ftp
    btss = (duration_seconds * np_watts * intensity_factor) / (ftp * 3600.0) * 100.0
    return round(btss, 1)


class CyclingStrategy:
    """Periodized cycling plans based on FTP and Coggan TSS."""

    def calculate_zones(self, reference_value: float) -> Dict[str, Any]:
        return calculate_coggan_zones(reference_value)

    def calculate_tss(self, duration_seconds: float, **metrics: Any) -> float:
        np_watts = metrics.get("np_watts", 0.0)
        ftp = metrics.get("ftp", 200.0)
        return calculate_btss(duration_seconds, np_watts, ftp)

    def generate_plan(
        self,
        plan_id: str,
        start_date: date,
        target_date: date,
        base_weekly_volume: float,  # in TSS (e.g. 300 TSS/week) or minutes
        available_days: List[int],
        reference_value: float = 220.0, # FTP in Watts
        sessions_per_week: int = 4,
        **kwargs: Any
    ) -> List[Week]:
        total_days = (target_date - start_date).days
        total_weeks = max(4, total_days // 7)
        zones = self.calculate_zones(reference_value)

        weeks: List[Week] = []
        current_tss = base_weekly_volume
        selected_days = sorted(available_days)[:sessions_per_week]

        for w in range(1, total_weeks + 1):
            w_start = start_date + timedelta(days=(w - 1) * 7)
            w_id = str(uuid.uuid4())
            is_recovery = (w % 4 == 0) and (w != total_weeks)

            if is_recovery:
                target_tss = round(current_tss * 0.65, 1)
                phase = PhaseType.BASE
            elif w >= total_weeks - 1:
                target_tss = round(current_tss * 0.60, 1)
                phase = PhaseType.TAPER
            else:
                # Progression max 5-7 TSS/day per week (approx 35-50 TSS increase/week max)
                target_tss = round(current_tss * 1.07, 1)
                current_tss = target_tss
                phase = PhaseType.BUILD if w <= total_weeks - 3 else PhaseType.PEAK

            # Create workouts distributed across selected days
            workouts: List[Workout] = []
            # Day 1: Endurance Z2 (60 min)
            # Day 2: Sweetspot / Threshold Z3/Z4 (75 min)
            # Day 3: Recovery Z1 (45 min) or Endurance Z2
            # Day 4: Long Endurance / Over-Unders (120 min)
            session_types = [
                ("endurance", 60, "Z2", f"Watts: {zones['Z2']['min_watts']}-{zones['Z2']['max_watts']}W"),
                ("sweetspot", 75, "Z3", f"Sweetspot: {round(reference_value*0.88)}-{round(reference_value*0.94)}W"),
                ("recovery", 45, "Z1", f"Active Recovery < {zones['Z1']['max_watts']}W"),
                ("long_ride", 120, "Z2", f"Endurance Z2: {zones['Z2']['min_watts']}-{zones['Z2']['max_watts']}W")
            ]

            for idx, d in enumerate(selected_days):
                st_type, duration_min, zone, detail = session_types[idx % len(session_types)]
                workouts.append(Workout(
                    id=str(uuid.uuid4()),
                    week_id=w_id,
                    sport_type=SportType.CYCLING,
                    date=w_start + timedelta(days=d - 1),
                    day_of_week=d,
                    workout_type=st_type,
                    metric_primary=float(duration_min),
                    metric_unit="min",
                    intensity_target=zone,
                    intensity_detail=detail,
                    target_duration_min=duration_min,
                    status="planned"
                ))

            weeks.append(Week(
                id=w_id,
                plan_id=plan_id,
                week_number=w,
                week_start_date=w_start,
                phase=phase,
                target_weekly_volume=float(sum(wo.target_duration_min or 0 for wo in workouts)),
                target_weekly_tss=target_tss,
                is_recovery_week=is_recovery,
                workouts=workouts
            ))

        return weeks
