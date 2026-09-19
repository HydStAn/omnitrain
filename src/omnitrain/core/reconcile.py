"""Reconciliation state machine according to Section 5C Priority Matrix."""
from datetime import date, timedelta
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from omnitrain.core.models import CheckinEvent, CompletionStatus, Week, Workout


class MutationRule(str, Enum):
    SICK_PAUSE = "sick_pause"
    SICK_RECOVERY_RETURN = "sick_recovery_return"
    PAIN_REST_48H = "pain_rest_48h"
    PAIN_NIGGLE_EASY_ONLY = "pain_niggle_easy_only"
    FATIGUE_DROP_INTENSITY = "fatigue_drop_intensity"
    RPE_HIGH_VOLUME_CUT = "rpe_high_volume_cut"
    PARTIAL_TIME_CONSTRAINT = "partial_time_constraint"
    PARTIAL_EXHAUSTION = "partial_exhaustion"
    MISSED_DISCARD = "missed_discard"
    OVERPERFORMED_KEEP = "overperformed_keep"
    COMPLETED_AS_PLANNED = "completed_as_planned"


class PlanMutation(BaseModel):
    rule: MutationRule
    description: str
    affected_workout_ids: List[str] = []
    volume_adjustment_percent: float = 0.0


class StateMachineReconciler:
    """
    Evaluates check-in events against the priority matrix:
      1. SICK (highest)
      2. PAIN (joint/tendon/shin/bone)
      3. FATIGUE (readiness)
      4. RPE_HIGH (RPE >= 8 on easy)
      5. PARTIAL
      6. MISSED
      7. OVERPERFORMED
      8. COMPLETED
    """

    STRUCTURAL_PAIN_TYPES = {"joint_pain", "tendon_pain", "shin_pain", "bone_pain"}

    @classmethod
    def reconcile_checkin(
        cls,
        checkin: CheckinEvent,
        checkin_date: date,
        upcoming_workouts: List[Workout],
        next_week: Optional[Week] = None,
        is_luteal_phase: bool = False
    ) -> List[PlanMutation]:
        mutations: List[PlanMutation] = []

        # 1. SICK
        if checkin.sickness_reported or checkin.completion_status == CompletionStatus.SICK:
            affected_ids = []
            for wo in upcoming_workouts:
                wo.workout_type = "rest"
                wo.metric_primary = 0.0
                wo.intensity_target = "rest"
                wo.intensity_detail = "Ruhetag wegen Krankheit"
                affected_ids.append(wo.id)

            mutations.append(PlanMutation(
                rule=MutationRule.SICK_PAUSE,
                description="Krankheit gemeldet: Alle bevorstehenden Einheiten in Ruhetage umgewandelt bis Genesungsmeldung.",
                affected_workout_ids=affected_ids
            ))
            return mutations

        # 2. PAIN
        structural_pains = [
            s for s in checkin.symptoms if s.type in cls.STRUCTURAL_PAIN_TYPES
        ]
        if structural_pains:
            max_severity = max(s.severity for s in structural_pains)
            if max_severity >= 4:
                # Severity >= 4: Forced 48-72h pause (convert into rest)
                cutoff_date = checkin_date + timedelta(days=3)
                affected_ids = []
                for wo in upcoming_workouts:
                    if wo.date <= cutoff_date:
                        wo.workout_type = "rest"
                        wo.metric_primary = 0.0
                        wo.intensity_target = "rest"
                        wo.intensity_detail = f"Schmerzpause ({max_severity}/10): Regeneration"
                        affected_ids.append(wo.id)
                    elif wo.workout_type in ["tempo", "interval"]:
                        # Strip tempo/intervals for the rest of the week
                        wo.workout_type = "easy"
                        wo.intensity_target = "easy"
                        wo.intensity_detail = "Schonlauf (Zone E) statt Tempo"
                        affected_ids.append(wo.id)

                mutations.append(PlanMutation(
                    rule=MutationRule.PAIN_REST_48H,
                    description=f"Struktureller Schmerz (Severity {max_severity}/10): 72h Pause und Tempoeinheiten gestrichen.",
                    affected_workout_ids=affected_ids
                ))
                return mutations

            elif 1 <= max_severity <= 3:
                # Severity 1-3: Niggle -> replace tempo/intervals in next 48h with easy runs
                cutoff_date = checkin_date + timedelta(days=2)
                affected_ids = []
                for wo in upcoming_workouts:
                    if wo.date <= cutoff_date and wo.workout_type in ["tempo", "interval"]:
                        wo.workout_type = "easy"
                        wo.intensity_target = "easy"
                        wo.intensity_detail = f"Niggle-Beobachtung ({max_severity}/10): Schwellenlauf durch Easy Run ersetzt."
                        affected_ids.append(wo.id)

                mutations.append(PlanMutation(
                    rule=MutationRule.PAIN_NIGGLE_EASY_ONLY,
                    description=f"Niggle registriert ({max_severity}/10): Tempo in den nächsten 48h auf Easy gedrosselt.",
                    affected_workout_ids=affected_ids
                ))
                return mutations

        # 3. RPE_HIGH / Overexertion
        rpe_threshold = 9 if is_luteal_phase else 8
        is_rpe_high = (checkin.perceived_rpe is not None and checkin.perceived_rpe >= rpe_threshold)

        if is_rpe_high or checkin.completion_reason == "exhaustion":
            affected_ids = []
            if next_week:
                next_week.target_weekly_volume = round(next_week.target_weekly_volume * 0.85, 1)
                for wo in next_week.workouts:
                    wo.metric_primary = round(wo.metric_primary * 0.85, 1)
                    affected_ids.append(wo.id)

            mutations.append(PlanMutation(
                rule=MutationRule.RPE_HIGH_VOLUME_CUT,
                description=f"Übermässige Erschöpfung (RPE {checkin.perceived_rpe}): Folgewoche um 15% de-loaded.",
                affected_workout_ids=affected_ids,
                volume_adjustment_percent=-15.0
            ))
            return mutations

        # 4. PARTIAL / MISSED
        if checkin.completion_status == CompletionStatus.PARTIAL:
            mutations.append(PlanMutation(
                rule=MutationRule.PARTIAL_TIME_CONSTRAINT,
                description="Teilweise absolviert: Restvolumen verfällt ersatzlos (keine Übertragungen auf Folgetage)."
            ))
            return mutations

        if checkin.completion_status == CompletionStatus.SKIPPED:
            mutations.append(PlanMutation(
                rule=MutationRule.MISSED_DISCARD,
                description="Einheit verpasst: Verfällt ersatzlos (Schutz vor Aufhol-Verletzungen)."
            ))
            return mutations

        # 5. COMPLETED AS PLANNED
        mutations.append(PlanMutation(
            rule=MutationRule.COMPLETED_AS_PLANNED,
            description="Training planmässig absolviert. Keine Anpassung erforderlich."
        ))
        return mutations
