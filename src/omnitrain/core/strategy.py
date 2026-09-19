"""Abstract base protocol and interface for all sport strategies in OmniTrain."""
from typing import Any, Dict, List, Protocol
from datetime import date
from omnitrain.core.models import Week, CheckinEvent


class SportStrategy(Protocol):
    """Protocol that every sport engine must satisfy."""

    def calculate_zones(self, reference_value: float) -> Dict[str, Any]:
        """Calculates intensity zones from reference value (e.g. VDOT, FTP, CSS)."""
        ...

    def generate_plan(
        self,
        plan_id: str,
        start_date: date,
        target_date: date,
        base_weekly_volume: float,
        available_days: List[int],
        reference_value: float,
        sessions_per_week: int,
        **kwargs: Any
    ) -> List[Week]:
        """Generates weeks and workouts according to sport science periodization."""
        ...

    def calculate_tss(self, duration_seconds: float, **metrics: Any) -> float:
        """Calculates Unified Training Stress Score (rTSS, bTSS, sTSS, or wTSS)."""
        ...
