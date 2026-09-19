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
