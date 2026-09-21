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


import os
from omnitrain.core.llm import UnifiedLLMClient

DEFAULT_OLLAMA_URL = os.environ.get("OMNITRAIN_OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_OLLAMA_MODEL = os.environ.get("OMNITRAIN_MODEL", "qwen2.5:3b")


class GoalParser:
    """Parses natural language onboarding goal into structured plan parameters."""

    def __init__(self, llm_client: Any = "default", ollama_url: Optional[str] = None, model: Optional[str] = None):
        if ollama_url or model:
            self.llm_client = UnifiedLLMClient(
                provider="ollama",
                base_url=ollama_url or DEFAULT_OLLAMA_URL,
                model=model or DEFAULT_OLLAMA_MODEL
            )
        elif llm_client == "default":
            self.llm_client = UnifiedLLMClient()
        else:
            self.llm_client = llm_client

    def parse(self, text: str) -> GoalParseResult:
        # Step 1: Try local LLM if online
        llm_result = self._try_llm_parse(text)
        if llm_result:
            return llm_result

        # Step 2: Deterministic offline rule-based parser
        return self._rule_based_parse(text)

    def _try_llm_parse(self, text: str) -> Optional[GoalParseResult]:
        system_prompt = """Du bist ein präziser Extraktor für sportliche Trainingsziele.
Antworte DIREKT mit einem validen JSON-Objekt ohne Markdown-Codeblöcke nach folgendem Schema:
{
  "sport_type": "running",
  "target_event": "marathon",
  "target_weeks": 16,
  "current_baseline": {"value": 25.0, "unit": "km_per_week"},
  "sessions_per_week": 4,
  "preferred_days": [2, 4, 6, 7],
  "key_session_day": 7,
  "fitness_level": "intermediate",
  "user_profile": {
    "age": null,
    "resting_hr": null,
    "max_hr": null,
    "recent_race": null,
    "weight_kg": null,
    "vdot": null
  },
  "constraints": []
}
Erlaubte Werte:
- sport_type: "running" | "cycling" | "swimming" | "strength"
- target_event: "marathon" | "half_marathon" | "10k" | "5k" | "ftp_builder" | "hypertrophy"
- target_weeks: Ganzzahl (z.B. 12, 16)
- current_baseline.unit: "km_per_week" | "min_per_week" | "tss_per_week"
- fitness_level: "beginner" | "intermediate" | "advanced"
"""
        try:
            if hasattr(self.llm_client, "chat_completion_json"):
                raw_json = self.llm_client.chat_completion_json(
                    system_prompt=system_prompt,
                    user_prompt=f"Text: {text}",
                    max_tokens=1200
                )
            elif hasattr(self.llm_client, "ollama_url"):
                prompt = f"{system_prompt}\nText: {text}"
                res = httpx.post(
                    f"{self.llm_client.ollama_url}/api/generate",
                    json={"model": getattr(self.llm_client, "model", DEFAULT_OLLAMA_MODEL), "prompt": prompt, "format": "json", "stream": False},
                    timeout=5.0
                )
                raw_json = res.json().get("response") if res.status_code == 200 else None
            else:
                raw_json = None

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
        if "triathlon" in lower or "ironman" in lower or "70.3" in lower:
            sport = "triathlon"
        elif "rad" in lower or "bike" in lower or "cycling" in lower:
            sport = "cycling"
        elif "schwimm" in lower or "swim" in lower:
            sport = "swimming"
        elif "kraft" in lower or "strength" in lower or "gym" in lower:
            sport = "strength"

        # Goal event according to sport
        if sport == "cycling":
            goal = "ftp_builder"
            if "gran fondo" in lower or "granfondo" in lower or "marathon" in lower or "radmarathon" in lower or "century" in lower:
                goal = "gran_fondo"
            elif "zeitfahren" in lower or "time trial" in lower or "tt" in lower:
                goal = "time_trial"
            elif "kriterium" in lower or "criterium" in lower or "crit" in lower:
                goal = "criterium"
            elif "climbing" in lower or "berg" in lower or "alpen" in lower:
                goal = "climbing"
        elif sport == "swimming":
            goal = "css_improvement"
            if "open water" in lower or "freiwasser" in lower or "see" in lower:
                goal = "open_water"
            elif "1500" in lower or "ausdauer" in lower or "endurance" in lower:
                goal = "endurance_1500m"
            elif "sprint" in lower or "50m" in lower or "100m" in lower:
                goal = "speed_sprint"
        elif sport == "strength":
            goal = "hypertrophy"
            if "kraft" in lower or "powerlifting" in lower or "max_strength" in lower or "1rm" in lower:
                goal = "max_strength"
            elif "kraftausdauer" in lower or "definition" in lower:
                goal = "strength_endurance"
        elif sport == "triathlon":
            goal = "triathlon_olympic"
            if "sprint" in lower:
                goal = "triathlon_sprint"
            elif "70.3" in lower or "mitteldistanz" in lower or "half ironman" in lower:
                goal = "triathlon_70_3"
            elif "ironman" in lower or "langdistanz" in lower or "140.6" in lower:
                goal = "triathlon_ironman"
        else:
            goal = "marathon"
            if "halbmarathon" in lower or "half marathon" in lower or "hm" in lower:
                goal = "half_marathon"
            elif "10k" in lower or "10 km" in lower or "10 kilometer" in lower:
                goal = "10k"
            elif "5k" in lower or "5 km" in lower or "5 kilometer" in lower:
                goal = "5k"
            elif "marathon" in lower:
                goal = "marathon"

        # Weeks
        weeks = 16
        weeks_match = re.search(r"(\d{1,2})\s*(?:wochen|weeks|wo)", lower)
        if weeks_match:
            weeks = int(weeks_match.group(1))

        # Baseline volume according to sport
        km_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:km|kilometer)(?:\s*(?:pro|die|\/)\s*woche|\s*wöchentlich|\s*aktuell)?", lower)
        m_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:m|meter)(?:\s*(?:pro|die|\/)\s*woche|\s*wöchentlich|\s*aktuell)?", lower)
        sets_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:sätze|saetze|sets)(?:\s*(?:pro|die|\/)\s*woche|\s*wöchentlich|\s*aktuell)?", lower)
        tss_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:tss)(?:\s*(?:pro|die|\/)\s*woche|\s*wöchentlich|\s*aktuell)?", lower)

        if sport == "swimming":
            if m_match:
                baseline = float(m_match.group(1).replace(",", "."))
            elif km_match:
                baseline = float(km_match.group(1).replace(",", ".")) * 1000.0
            else:
                baseline = 4000.0
            base_unit = "m_per_week"
        elif sport == "strength":
            if sets_match:
                baseline = float(sets_match.group(1).replace(",", "."))
            else:
                baseline = 14.0
            base_unit = "sets_per_week"
        elif sport == "cycling":
            if tss_match:
                baseline = float(tss_match.group(1).replace(",", "."))
            else:
                baseline = 250.0
            base_unit = "tss_per_week"
        elif sport == "triathlon":
            if tss_match:
                baseline = float(tss_match.group(1).replace(",", "."))
            else:
                baseline = 350.0
            base_unit = "tss_per_week"
        else:
            if km_match:
                baseline = float(km_match.group(1).replace(",", "."))
            else:
                baseline = 25.0
            base_unit = "km_per_week"

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
            current_baseline=BaselineVolume(value=baseline, unit=base_unit),
            sessions_per_week=sessions,
            preferred_days=preferred_days,
            key_session_day=preferred_days[-1] if preferred_days else 7,
            fitness_level="intermediate",
            user_profile=GoalUserProfile(age=age, weight_kg=weight, vdot=vdot)
        )
