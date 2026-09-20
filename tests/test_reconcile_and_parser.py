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


def test_multisport_checkin_parsing_offline():
    parser = CheckinParser(llm_client=None)

    # 1. Swim check-in with meters and shoulder pain
    c_swim = parser.parse("Heute 2500m geschwommen, aber Schulterschmerzen gehabt bei RPE 6.")
    assert c_swim.completion_status == CompletionStatus.COMPLETED
    assert c_swim.actual_metrics is not None
    assert c_swim.actual_metrics.metric_primary == 2500.0
    assert c_swim.actual_metrics.unit == "m"
    assert c_swim.perceived_rpe == 6
    assert len(c_swim.symptoms) == 1
    assert c_swim.symptoms[0].location == "shoulder"

    # 2. Strength check-in with sets and elbow pain
    c_str = parser.parse("15 Sätze absolviert, aber leichter Schmerz im Ellbogen.")
    assert c_str.completion_status == CompletionStatus.COMPLETED
    assert c_str.actual_metrics is not None
    assert c_str.actual_metrics.metric_primary == 15.0
    assert c_str.actual_metrics.unit == "sets"
    assert len(c_str.symptoms) == 1
    assert c_str.symptoms[0].location == "elbow"

    # 3. Cycling check-in with minutes and back pain
    c_bike = parser.parse("90 min geradelt, RPE 5, aber Rückenschmerzen gehabt.")
    assert c_bike.completion_status == CompletionStatus.COMPLETED
    assert c_bike.actual_metrics is not None
    assert c_bike.actual_metrics.metric_primary == 90.0
    assert c_bike.actual_metrics.unit == "min"
    assert c_bike.perceived_rpe == 5
    assert len(c_bike.symptoms) == 1
    assert c_bike.symptoms[0].location == "back"


def test_multisport_reconciler_niggle_demotions():
    today = date(2026, 10, 6)
    niggle_checkin = CheckinEvent(
        completion_status=CompletionStatus.COMPLETED,
        symptoms=[Symptom(location="shoulder", type="tendon_pain", severity=2)]
    )

    # Swimming demotion: css_threshold -> technique_drills
    swim_wo = Workout(
        id="s1",
        week_id="w1",
        sport_type=SportType.SWIMMING,
        date=date(2026, 10, 7),
        day_of_week=3,
        workout_type="css_threshold",
        metric_primary=2000.0,
        metric_unit="m",
        status=CompletionStatus.PLANNED
    )
    StateMachineReconciler.reconcile_checkin(niggle_checkin, today, [swim_wo])
    assert swim_wo.workout_type == "technique_drills"

    # Cycling demotion: sweetspot -> recovery
    bike_wo = Workout(
        id="b1",
        week_id="w1",
        sport_type=SportType.CYCLING,
        date=date(2026, 10, 7),
        day_of_week=3,
        workout_type="sweetspot",
        metric_primary=60.0,
        metric_unit="min",
        status=CompletionStatus.PLANNED
    )
    StateMachineReconciler.reconcile_checkin(niggle_checkin, today, [bike_wo])
    assert bike_wo.workout_type == "recovery"

    # Strength demotion: strength -> hypertrophy
    str_wo = Workout(
        id="st1",
        week_id="w1",
        sport_type=SportType.STRENGTH,
        date=date(2026, 10, 7),
        day_of_week=3,
        workout_type="strength",
        metric_primary=15.0,
        metric_unit="sets",
        status=CompletionStatus.PLANNED
    )
    StateMachineReconciler.reconcile_checkin(niggle_checkin, today, [str_wo])
    assert str_wo.workout_type == "hypertrophy"

