"""Status update models and recovery handler."""
from datetime import date, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from omnitrain.core.models import SportType, Workout
from omnitrain.core.reconcile import MutationRule, PlanMutation, demote_high_intensity


class UpdateType(str, Enum):
    RECOVERY = "recovery"
    PAIN_UPDATE = "pain_update"
    READINESS_UPDATE = "readiness_update"
    GOAL_CHANGE = "goal_change"


class StatusUpdateEvent(BaseModel):
    event_type: str = "status_update"
    update_type: UpdateType
    details: Dict[str, Any] = {}
    notes: Optional[str] = None


def restore_workout_to_easy(wo: Workout) -> None:
    """Restores a rested workout back to a safe baseline volume and intensity appropriate for its sport."""
    if wo.sport_type == SportType.SWIMMING:
        wo.workout_type = "technique_drills"
        wo.intensity_target = "Z1"
        wo.metric_primary = 1500.0
        wo.metric_unit = "m"
        wo.intensity_detail = "Technikdrills & Sculling in Z1"
    elif wo.sport_type == SportType.STRENGTH:
        wo.workout_type = "hypertrophy"
        wo.intensity_target = "hypertrophy"
        wo.metric_primary = 10.0
        wo.metric_unit = "sets"
        wo.intensity_detail = "Leichtes Wiedereinstiegstraining (10 Sätze, 3 RIR)"
    elif wo.sport_type == SportType.CYCLING:
        wo.workout_type = "recovery"
        wo.intensity_target = "Z1"
        wo.metric_primary = 45.0
        wo.metric_unit = "min"
        wo.intensity_detail = "Aktive Erholung Z1"
    else:  # running or default
        wo.workout_type = "easy"
        wo.intensity_target = "easy"
        wo.metric_primary = 6.0
        wo.metric_unit = "km"
        wo.intensity_detail = "Post-Recovery Schonlauf (Zone E)"


class StatusUpdateHandler:
    """
    Handles out-of-band status updates (Section 4C & 5C):
      1. Recovery from Sickness:
         - First training at max 50% planned volume
         - No tempo/intervals for 4 days post-recovery
         - Restore regular plan from day 5
      2. Pain Update (resolved):
         - Clears pain pause, restores planned workouts
      3. Readiness / Fatigue:
         - 48h drop in intensity (tempo -> easy)
    """

    @classmethod
    def handle_status_update(
        cls,
        update: StatusUpdateEvent,
        current_date: date,
        upcoming_workouts: List[Workout]
    ) -> List[PlanMutation]:
        mutations: List[PlanMutation] = []

        if update.update_type == UpdateType.RECOVERY:
            # Regel 1: Wiedereinstieg nach Genesung
            # Erstes Training: max 50% Volumen
            # Keine Tempoeinheiten in den ersten 4 Tagen
            first_training_adjusted = False
            four_days_cutoff = current_date + timedelta(days=4)
            affected_ids = []

            for wo in sorted(upcoming_workouts, key=lambda w: w.date):
                if wo.date >= current_date:
                    # If it was marked rest due to sickness, revive it
                    if wo.workout_type == "rest":
                        restore_workout_to_easy(wo)

                    # Restrict tempo/intervals in first 4 days
                    if wo.date <= four_days_cutoff and demote_high_intensity(wo, "Post-Recovery Schonung"):
                        affected_ids.append(wo.id)

                    # Cap first training at 50%
                    if not first_training_adjusted and wo.workout_type != "rest":
                        wo.metric_primary = round(wo.metric_primary * 0.5, 1 if wo.metric_unit == "km" else 0)
                        wo.intensity_detail = "Sanfter Wiedereinstieg (50% Volumen)"
                        first_training_adjusted = True
                        affected_ids.append(wo.id)

            mutations.append(PlanMutation(
                rule=MutationRule.SICK_RECOVERY_RETURN,
                description="Genesung gemeldet: 1. Training auf 50% gedrosselt, 4 Tage keine harten Intensitäten.",
                affected_workout_ids=affected_ids
            ))
            return mutations

        elif update.update_type == UpdateType.PAIN_UPDATE:
            status = update.details.get("status", "resolved")
            if status == "resolved":
                affected_ids = []
                for wo in upcoming_workouts:
                    if wo.workout_type == "rest" and "Schmerz" in (wo.intensity_detail or ""):
                        restore_workout_to_easy(wo)
                        affected_ids.append(wo.id)

                mutations.append(PlanMutation(
                    rule=MutationRule.COMPLETED_AS_PLANNED,
                    description="Schmerz als abgeklungen gemeldet: Ruhetage wieder zu Grundlageneinheiten aktiviert.",
                    affected_workout_ids=affected_ids
                ))
                return mutations

        elif update.update_type == UpdateType.READINESS_UPDATE:
            # Regel 3: FATIGUE / LOW READINESS
            severity = update.details.get("severity", 5)
            if severity >= 7:
                cutoff_48h = current_date + timedelta(days=2)
                affected_ids = []
                for wo in upcoming_workouts:
                    if wo.date <= cutoff_48h and demote_high_intensity(wo, f"Fatigue-Drosselung ({severity}/10)"):
                        affected_ids.append(wo.id)

                mutations.append(PlanMutation(
                    rule=MutationRule.FATIGUE_DROP_INTENSITY,
                    description=f"Akute Erschöpfung ({severity}/10): Nächste 48h High-Intensity auf Grundlagentempo gedrosselt.",
                    affected_workout_ids=affected_ids
                ))
                return mutations

        return mutations
