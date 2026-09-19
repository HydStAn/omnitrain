"""Daniels VDOT calculation and pacing engine."""
import math
from typing import Dict, Tuple

def pace_to_seconds(pace_str: str) -> int:
    """Convert 'MM:SS' string to total seconds."""
    parts = pace_str.strip().split(":")
    return int(parts[0]) * 60 + int(parts[1])

def seconds_to_pace(seconds: float) -> str:
    """Convert seconds to 'MM:SS' string."""
    seconds = round(seconds)
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{mins:02d}:{secs:02d}"

def calculate_vdot_from_race(distance_meters: float, time_seconds: float) -> float:
    """
    Calculate Jack Daniels VDOT from race distance (meters) and finish time (seconds).
    Uses Daniels' & Gilbert's oxygen cost / drop-dead equation:
      VO2 = -4.60 + 0.182258 * v + 0.000104 * v^2
      %max = 0.8 + 0.1894393 * exp(-0.012778 * t) + 0.2989558 * exp(-0.1932605 * t)
      VDOT = VO2 / %max
    where v is velocity in meters/minute and t is time in minutes.
    """
    t_min = time_seconds / 60.0
    v = distance_meters / t_min
    vo2 = -4.60 + 0.182258 * v + 0.000104 * (v ** 2)
    percent_max = (
        0.8
        + 0.1894393 * math.exp(-0.012778 * t_min)
        + 0.2989558 * math.exp(-0.1932605 * t_min)
    )
    return round(vo2 / percent_max, 1)

def calculate_velocity_for_intensity(vdot: float, intensity_fraction: float) -> float:
    """
    Calculate running velocity (m/min) for a target VO2 fraction.
    Solves: 0.000104 * v^2 + 0.182258 * v - (target_vo2 + 4.60) = 0
    """
    target_vo2 = vdot * intensity_fraction
    c = -(target_vo2 + 4.60)
    b = 0.182258
    a = 0.000104
    # Quadratic formula: (-b + sqrt(b^2 - 4ac)) / (2a)
    disc = b**2 - 4 * a * c
    v = (-b + math.sqrt(disc)) / (2 * a)
    return v

def velocity_to_pace_per_km(velocity_m_per_min: float) -> str:
    """Convert m/min to pace min/km."""
    if velocity_m_per_min <= 0:
        return "00:00"
    minutes_per_km = 1000.0 / velocity_m_per_min
    total_seconds = minutes_per_km * 60.0
    return seconds_to_pace(total_seconds)

def get_daniels_zones(vdot: float) -> Dict[str, Dict[str, str]]:
    """
    Compute Daniels training paces (min/km) for a given VDOT:
      - Easy (E): 62% - 72% VO2max
      - Marathon (M): ~75% - 84% VO2max (typical marathon intensity)
      - Threshold (T): ~86% - 88% VO2max
      - Interval (I): ~95% - 100% VO2max
      - Repetition (R): ~105% - 110% VO2max (anaerobic pace)
    """
    v_e_slow = calculate_velocity_for_intensity(vdot, 0.62)
    v_e_fast = calculate_velocity_for_intensity(vdot, 0.72)
    
    v_m = calculate_velocity_for_intensity(vdot, 0.80)
    v_t = calculate_velocity_for_intensity(vdot, 0.88)
    v_i = calculate_velocity_for_intensity(vdot, 0.98)
    v_r = calculate_velocity_for_intensity(vdot, 1.08)

    return {
        "easy": {
            "name": "Easy (E)",
            "min_pace": velocity_to_pace_per_km(v_e_fast),  # faster pace (e.g. 05:20)
            "max_pace": velocity_to_pace_per_km(v_e_slow),  # slower pace (e.g. 06:00)
            "description": "Aerobe Grundlage, Erholungsläufe, Longruns"
        },
        "marathon": {
            "name": "Marathon (M)",
            "pace": velocity_to_pace_per_km(v_m),
            "description": "Geplantes Marathon-Renntempo"
        },
        "threshold": {
            "name": "Threshold (T)",
            "pace": velocity_to_pace_per_km(v_t),
            "description": "Laktatschwelle (ca. 60 Min All-Out Dauerleistung)"
        },
        "interval": {
            "name": "Interval (I)",
            "pace": velocity_to_pace_per_km(v_i),
            "description": "VO2max Intervalle (3-5 min Belastungen)"
        },
        "repetition": {
            "name": "Repetition (R)",
            "pace": velocity_to_pace_per_km(v_r),
            "description": "Schnelligkeit und Laufökonomie (200m - 400m Wiederholungen)"
        }
    }
