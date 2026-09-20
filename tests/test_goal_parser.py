"""Unit tests for the natural language GoalParser."""
import pytest
from omnitrain.parser.goal import GoalParser


def test_goal_parser_marathon():
    parser = GoalParser(ollama_url="http://127.0.0.1:99999")  # force offline
    text = "Ich möchte in 20 Wochen meinen ersten Marathon finishen. Ich laufe aktuell ca. 32 km pro Woche an Di, Do, Sa, So. Mein VDOT liegt bei 49."
    res = parser.parse(text)

    assert res.sport_type == "running"
    assert res.target_event == "marathon"
    assert res.target_weeks == 20
    assert res.current_baseline.value == 32.0
    assert res.user_profile.vdot == 49.0
    assert 2 in res.preferred_days
    assert 7 in res.preferred_days


def test_goal_parser_cycling():
    parser = GoalParser(ollama_url="http://127.0.0.1:99999")
    text = "Ich will einen 12 Wochen Rad FTP Builder machen mit 4 Einheiten pro Woche."
    res = parser.parse(text)

    assert res.sport_type == "cycling"
    assert res.target_event == "ftp_builder"
    assert res.target_weeks == 12
    assert res.sessions_per_week == 4


def test_goal_parser_swimming():
    parser = GoalParser(ollama_url="http://127.0.0.1:99999")
    text = "Ich will 10 Wochen Schwimmen trainieren, aktuell schwimme ich ca. 3500 m pro Woche an Mo, Mi, Fr."
    res = parser.parse(text)

    assert res.sport_type == "swimming"
    assert res.target_weeks == 10
    assert res.current_baseline.value == 3500.0
    assert res.current_baseline.unit == "m_per_week"
    assert res.preferred_days == [1, 3, 5]


def test_goal_parser_strength():
    parser = GoalParser(ollama_url="http://127.0.0.1:99999")
    text = "Ich starte einen 8 Wochen Hypertrophie Krafttraining Plan mit 16 Sätze pro Woche."
    res = parser.parse(text)

    assert res.sport_type == "strength"
    assert res.target_weeks == 8
    assert res.current_baseline.value == 16.0
    assert res.current_baseline.unit == "sets_per_week"

