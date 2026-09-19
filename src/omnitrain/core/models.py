"""Core domain models for OmniTrain."""
from datetime import date, datetime
from enum import Enum
from typing import Any, List, Optional
from pydantic import BaseModel, Field


class SportType(str, Enum):
    RUNNING = "running"
    CYCLING = "cycling"
    STRENGTH = "strength"
    SWIMMING = "swimming"
    MULTISPORT = "multisport"


class CompletionStatus(str, Enum):
    PLANNED = "planned"
    COMPLETED = "completed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    MODIFIED = "modified"
    SICK = "sick"


class PhaseType(str, Enum):
    BASE = "base"
    BUILD = "build"
    PEAK = "peak"
    TAPER = "taper"


class Symptom(BaseModel):
    location: str
    side: Optional[str] = None
    type: str  # e.g., 'joint_pain', 'muscle_soreness', 'tendon_pain'
    severity: int = Field(ge=1, le=10)


class ActualMetrics(BaseModel):
    metric_primary: float
    unit: str
    tss: Optional[float] = None
    completed_units: Optional[Any] = None


class CheckinEvent(BaseModel):
    event_type: str = "workout_checkin"
    completion_status: CompletionStatus
    completion_reason: Optional[str] = None
    actual_metrics: Optional[ActualMetrics] = None
    perceived_rpe: Optional[int] = Field(default=None, ge=1, le=10)
    sickness_reported: bool = False
    sickness_days: int = 0
    symptoms: List[Symptom] = Field(default_factory=list)
    notes: Optional[str] = None


class TrainingZoneItem(BaseModel):
    name: str
    min_pace: Optional[str] = None
    max_pace: Optional[str] = None
    min_power_watts: Optional[int] = None
    max_power_watts: Optional[int] = None
    description: Optional[str] = None


class UserProfile(BaseModel):
    vdot: Optional[float] = None
    ftp_watts: Optional[int] = None
    swim_css_100m: Optional[str] = None
    track_menstrual_cycle: bool = False


class Workout(BaseModel):
    id: str
    week_id: str
    sport_type: SportType
    date: date
    day_of_week: int = Field(ge=1, le=7)
    workout_type: str
    metric_primary: float
    metric_unit: str
    intensity_target: Optional[str] = None
    intensity_detail: Optional[str] = None
    target_duration_min: Optional[int] = None
    structure_json: Optional[str] = None
    status: CompletionStatus = CompletionStatus.PLANNED


class Week(BaseModel):
    id: str
    plan_id: str
    week_number: int
    week_start_date: date
    phase: PhaseType
    target_weekly_volume: float
    target_weekly_tss: Optional[float] = None
    is_recovery_week: bool = False
    workouts: List[Workout] = Field(default_factory=list)
