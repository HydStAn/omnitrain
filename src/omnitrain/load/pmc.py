"""Unified Training Load and Performance Management Chart (PMC) engine."""
from typing import Dict, List, Tuple
from pydantic import BaseModel


class DailyLoad(BaseModel):
    date: str
    tss: float = 0.0


class PMCPoint(BaseModel):
    date: str
    tss: float
    ctl: float  # Fitness (Chronic Training Load - 42 day EWMA)
    atl: float  # Fatigue (Acute Training Load - 7 day EWMA)
    tsb: float  # Form (Training Stress Balance = CTL - ATL)


class PMCEngine:
    """
    Calculates Banister impulse response / Coggan PMC metrics:
      CTL_today = CTL_yesterday + (TSS_today - CTL_yesterday) * (1 - e^(-1/42))
      ATL_today = ATL_yesterday + (TSS_today - ATL_yesterday) * (1 - e^(-1/7))
      TSB_today = CTL_yesterday - ATL_yesterday  (or CTL_today - ATL_today)
    """

    TIME_CONSTANT_CTL = 42.0
    TIME_CONSTANT_ATL = 7.0

    @classmethod
    def calculate_pmc_series(
        cls,
        daily_loads: List[DailyLoad],
        initial_ctl: float = 0.0,
        initial_atl: float = 0.0
    ) -> List[PMCPoint]:
        ctl = initial_ctl
        atl = initial_atl
        k_ctl = 1.0 / cls.TIME_CONSTANT_CTL
        k_atl = 1.0 / cls.TIME_CONSTANT_ATL

        series: List[PMCPoint] = []
        for item in daily_loads:
            ctl = ctl + (item.tss - ctl) * k_ctl
            atl = atl + (item.tss - atl) * k_atl
            tsb = ctl - atl
            series.append(PMCPoint(
                date=item.date,
                tss=round(item.tss, 1),
                ctl=round(ctl, 1),
                atl=round(atl, 1),
                tsb=round(tsb, 1)
            ))
        return series

    @staticmethod
    def calculate_running_rtss(
        duration_seconds: float,
        actual_pace_sec_km: float,
        threshold_pace_sec_km: float
    ) -> float:
        """
        Calculate rTSS (Running Training Stress Score) based on Functional Threshold Pace:
          NGP (Normalized Graded Pace) ratio = threshold_pace / actual_pace (higher speed = higher ratio)
          IF = threshold_pace / actual_pace
          rTSS = (duration_sec * IF^2) / 3600 * 100
        """
        if actual_pace_sec_km <= 0 or threshold_pace_sec_km <= 0:
            return 0.0
        # Intensity factor = speed_actual / speed_threshold = threshold_pace / actual_pace
        intensity_factor = threshold_pace_sec_km / actual_pace_sec_km
        rtss = (duration_seconds * (intensity_factor ** 2)) / 3600.0 * 100.0
        return round(rtss, 1)
