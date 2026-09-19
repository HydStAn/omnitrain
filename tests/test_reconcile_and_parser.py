"""Unit tests for the State Machine Reconciler and offline Fallback Parser."""
from datetime import date
import pytest
from omnitrain.core.models import (
    CheckinEvent,
    CompletionStatus,
    PhaseType,
    SportType,
    Symptom,
    Week,
    Workout
)
from omnitrain.core.reconcile import MutationRule, StateMachineReconciler
from omnitrain.parser.checkin import CheckinParser


def make_dummy_workout(w_id: str, w_date: date, w_type: str, dist: float) -> Workout:
    return Workout(
        id=w_id,
        week_id="week-1",
        sport_type=SportType.RUNNING,
        date=w_date,
        day_of_week=w_date.isoweekday(),
        workout_type=w_type,
        metric_primary=dist,
        metric_unit="km",
        status=CompletionStatus.PLANNED
    )


def test_reconciler_sick_highest_priority():
    today = date(2026, 10, 6)
    upcoming = [
        make_dummy_workout("wo-1", date(2026, 10, 7), "tempo", 8.0),
        make_dummy_workout("wo-2", date(2026, 10, 9), "easy", 10.0),
    ]
    checkin = CheckinEvent(
        completion_status=CompletionStatus.SICK,
        sickness_reported=True,
        symptoms=[Symptom(location="throat", type="joint_pain", severity=8)]
    )

    mutations = StateMachineReconciler.reconcile_checkin(checkin, today, upcoming)
    assert len(mutations) == 1
    assert mutations[0].rule == MutationRule.SICK_PAUSE
    # All workouts must be converted to rest
    for wo in upcoming:
        assert wo.workout_type == "rest"
        assert wo.metric_primary == 0.0


def test_reconciler_pain_severity_thresholds():
    today = date(2026, 10, 6)
    # 1. Niggle: Severity 2 -> tempo replaced by easy, no pause
    upcoming = [
        make_dummy_workout("wo-1", date(2026, 10, 7), "tempo", 8.0),
        make_dummy_workout("wo-2", date(2026, 10, 10), "easy", 15.0),
    ]
    niggle_checkin = CheckinEvent(
        completion_status=CompletionStatus.COMPLETED,
        symptoms=[Symptom(location="achilles", type="tendon_pain", severity=2)]
    )
    mutations = StateMachineReconciler.reconcile_checkin(niggle_checkin, today, upcoming)
    assert mutations[0].rule == MutationRule.PAIN_NIGGLE_EASY_ONLY
    assert upcoming[0].workout_type == "easy"
    assert upcoming[0].metric_primary == 8.0  # Volume kept

    # 2. Severe Pain: Severity 6 -> 72h rest forced
    upcoming2 = [
        make_dummy_workout("wo-3", date(2026, 10, 7), "easy", 8.0),
        make_dummy_workout("wo-4", date(2026, 10, 12), "tempo", 10.0),
    ]
    severe_checkin = CheckinEvent(
        completion_status=CompletionStatus.PARTIAL,
        symptoms=[Symptom(location="knee", type="joint_pain", severity=6)]
    )
    mutations2 = StateMachineReconciler.reconcile_checkin(severe_checkin, today, upcoming2)
    assert mutations2[0].rule == MutationRule.PAIN_REST_48H
    assert upcoming2[0].workout_type == "rest"


def test_offline_fallback_parser_without_llm():
    parser = CheckinParser(llm_client=None)  # Explicitly offline / no LLM

    # Test 1: Sickness detection
    c1 = parser.parse("Ich liege flach mit Fieber und Halsschmerzen, heute geht gar nichts.")
    assert c1.completion_status == CompletionStatus.SICK
    assert c1.sickness_reported is True

    # Test 2: Pain & distance extraction
    c2 = parser.parse("8 km gelaufen, aber bei km 5 hat die Achillessehne geschmerzt. Anstrengung war RPE 7")
    assert c2.completion_status == CompletionStatus.COMPLETED
    assert c2.actual_metrics is not None
    assert c2.actual_metrics.metric_primary == 8.0
    assert c2.perceived_rpe == 7
    assert len(c2.symptoms) == 1
    assert c2.symptoms[0].location == "achilles"
    assert c2.symptoms[0].type == "tendon_pain"

    # Test 3: Skipped workout
    c3 = parser.parse("Wegen Überstunden ist das Training heute ausgefallen.")
    assert c3.completion_status == CompletionStatus.SKIPPED
