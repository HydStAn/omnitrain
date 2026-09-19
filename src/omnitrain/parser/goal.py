"""Onboarding and Goal Parser according to Section 4A."""
import re
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
import httpx


class BaselineVolume(BaseModel):
    value: float
    unit: str = "km_per_week"


class GoalUserProfile(BaseModel):
    age: Optional[int] = None
    resting_hr: Optional[int] = None
    max_hr: Optional[int] = None
    recent_race: Optional[str] = None
    weight_kg: Optional[float] = None
    vdot: Optional[float] = None


class GoalParseResult(BaseModel):
    sport_type: str = "running"
    target_event: str = "marathon"
    target_weeks: int = 16
    current_baseline: BaselineVolume = Field(default_factory=lambda: BaselineVolume(value=25.0))
    sessions_per_week: int = 4
    preferred_days: List[int] = Field(default_factory=lambda: [2, 4, 6, 7])
    key_session_day: int = 7
    fitness_level: str = "intermediate"
    user_profile: GoalUserProfile = Field(default_factory=GoalUserProfile)
    constraints: List[str] = Field(default_factory=list)


class GoalParser:
    """Parses natural language onboarding goal into structured plan parameters."""

    def __init__(self, ollama_url: str = "http://127.0.0.1:11434", model: str = "qwen2.5:3b"):
        self.ollama_url = ollama_url
        self.model = model

    def parse(self, text: str) -> GoalParseResult:
        # Step 1: Try local LLM if online
        llm_result = self._try_llm_parse(text)
        if llm_result:
            return llm_result

        # Step 2: Deterministic offline rule-based parser
        return self._rule_based_parse(text)

    def _try_llm_parse(self, text: str) -> Optional[GoalParseResult]:
        prompt = f"""Extrahiere das sportliche Trainingsziel als valides JSON nach folgendem Schema:
{{
  "sport_type": "running" | "cycling" | "swimming" | "strength",
  "target_event": "marathon" | "half_marathon" | "10k" | "5k" | "ftp_builder" | "hypertrophy",
  "target_weeks": int,
  "current_baseline": {{"value": float, "unit": "km_per_week" | "min_per_week" | "tss_per_week"}},
  "sessions_per_week": int,
  "preferred_days": [int],
  "key_session_day": int,
  "fitness_level": "beginner" | "intermediate" | "advanced",
  "user_profile": {{
    "age": int | null,
    "resting_hr": int | null,
    "max_hr": int | null,
    "recent_race": str | null,
    "weight_kg": float | null,
    "vdot": float | null
  }},
  "constraints": [str]
}}
Text: {text}"""
        try:
            res = httpx.post(
                f"{self.ollama_url}/api/generate",
                json={"model": self.model, "prompt": prompt, "format": "json", "stream": False},
                timeout=5.0
            )
            if res.status_code == 200:
                raw_json = res.json().get("response")
                if raw_json:
                    return GoalParseResult.model_validate_json(raw_json)
        except Exception:
            return None
        return None

    @staticmethod
    def _rule_based_parse(text: str) -> GoalParseResult:
        lower = text.lower()

        # Sport type
        sport = "running"
        if "rad" in lower or "bike" in lower or "cycling" in lower:
            sport = "cycling"
        elif "schwimm" in lower or "swim" in lower:
            sport = "swimming"
        elif "kraft" in lower or "strength" in lower or "gym" in lower:
            sport = "strength"

        # Goal event
        goal = "marathon"
        if "halbmarathon" in lower or "half marathon" in lower or "hm" in lower:
            goal = "half_marathon"
        elif "10k" in lower or "10 km" in lower or "10 kilometer" in lower:
            goal = "10k"
        elif "5k" in lower or "5 km" in lower or "5 kilometer" in lower:
            goal = "5k"
        elif "marathon" in lower:
            goal = "marathon"
        elif "ftp" in lower:
            goal = "ftp_builder"
        elif "hypertrophie" in lower or "muskelaufbau" in lower:
            goal = "hypertrophy"

        # Weeks
        weeks = 16
        weeks_match = re.search(r"(\d{1,2})\s*(?:wochen|weeks|wo)", lower)
        if weeks_match:
            weeks = int(weeks_match.group(1))

        # Baseline volume
        baseline = 25.0
        base_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:km|kilometer)(?:\s*(?:pro|die|\/)\s*woche|\s*wöchentlich|\s*aktuell)?", lower)
        if base_match:
            baseline = float(base_match.group(1).replace(",", "."))

        # Sessions per week
        sessions = 4
        sessions_match = re.search(r"(\d)\s*(?:tage|x|mal|einheiten)", lower)
        if sessions_match:
            sessions = int(sessions_match.group(1))

        # Days
        preferred_days = [2, 4, 6, 7][:sessions]  # Default Di, Do, Sa, So
        day_map = {
            "mo": 1, "montag": 1,
            "di": 2, "dienstag": 2,
            "mi": 3, "mittwoch": 3,
            "do": 4, "donnerstag": 4,
            "fr": 5, "freitag": 5,
            "sa": 6, "samstag": 6,
            "so": 7, "sonntag": 7
        }
        detected_days = []
        for word, day_num in day_map.items():
            if re.search(rf"\b{word}\b", lower):
                if day_num not in detected_days:
                    detected_days.append(day_num)
        if len(detected_days) >= 2:
            preferred_days = sorted(detected_days)
            sessions = len(preferred_days)

        # VDOT / Fitness
        vdot = 45.0
        vdot_match = re.search(r"vdot[^\d]*(\d+(?:[.,]\d+)?)", lower)
        if vdot_match:
            vdot = float(vdot_match.group(1).replace(",", "."))

        # Profile fields (e.g. age, weight)
        age = None
        age_match = re.search(r"(\d{2})\s*jahre", lower)
        if age_match:
            age = int(age_match.group(1))

        weight = None
        weight_match = re.search(r"(\d{2,3}(?:[.,]\d+)?)\s*kg", lower)
        if weight_match:
            weight = float(weight_match.group(1).replace(",", "."))

        return GoalParseResult(
            sport_type=sport,
            target_event=goal,
            target_weeks=weeks,
            current_baseline=BaselineVolume(value=baseline, unit="km_per_week" if sport == "running" else "units"),
            sessions_per_week=sessions,
            preferred_days=preferred_days,
            key_session_day=preferred_days[-1] if preferred_days else 7,
            fitness_level="intermediate",
            user_profile=GoalUserProfile(age=age, weight_kg=weight, vdot=vdot)
        )
