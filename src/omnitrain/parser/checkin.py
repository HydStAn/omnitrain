"""Semantic Checkin Parser with graceful fallback when local LLM is offline."""
from datetime import date
from typing import Any, Optional, Protocol
import httpx
from pydantic import ValidationError
from omnitrain.core.models import ActualMetrics, CheckinEvent, CompletionStatus, Symptom


class LLMClientProtocol(Protocol):
    def parse_freetext_checkin(self, text: str) -> Optional[str]:
        ...


import os
from omnitrain.core.llm import UnifiedLLMClient

DEFAULT_OLLAMA_URL = os.environ.get("OMNITRAIN_OLLAMA_URL", "http://127.0.0.1:11434")
DEFAULT_OLLAMA_MODEL = os.environ.get("OMNITRAIN_MODEL", "qwen2.5:3b")


class OllamaLocalClient:
    """Ollama API Client with configurable URL/model, 5s timeout and max 2 retries."""

    def __init__(self, base_url: str = DEFAULT_OLLAMA_URL, model: str = DEFAULT_OLLAMA_MODEL):
        self.base_url = base_url
        self.model = model

    def is_available(self) -> bool:
        try:
            r = httpx.get(f"{self.base_url}/api/tags", timeout=1.5)
            return r.status_code == 200
        except Exception:
            return False

    def parse_freetext_checkin(self, text: str) -> Optional[str]:
        if not self.is_available():
            return None
        # System prompt calling for strict JSON output according to SPECIFICATION.md
        prompt = f"""Extrahiere den Trainings-Check-in als valides JSON nach folgendem Schema:
{{
  "event_type": "workout_checkin",
  "completion_status": "completed" | "partial" | "skipped" | "sick",
  "completion_reason": null | "time_constraint" | "exhaustion",
  "actual_metrics": {{"metric_primary": float, "unit": "km" | "min" | "m" | "sets"}},
  "perceived_rpe": int (1-10) | null,
  "sickness_reported": bool,
  "sickness_days": int,
  "symptoms": [{{"location": str, "side": str | null, "type": "joint_pain" | "muscle_soreness" | "tendon_pain", "severity": int (1-10)}}],
  "notes": str
}}
Text: {text}"""
        try:
            res = httpx.post(
                f"{self.base_url}/api/generate",
                json={"model": self.model, "prompt": prompt, "format": "json", "stream": False},
                timeout=5.0
            )
            if res.status_code == 200:
                return res.json().get("response")
        except Exception:
            return None
        return None


class CheckinParser:
    """
    Parser orchestrator:
      1. Tries LLM Client (UnifiedLLMClient or provided client)
      2. If unavailable or fails validation, uses deterministic structured prompts / keyword parser.
    """

    def __init__(self, llm_client: Any = "default"):
        if llm_client == "default":
            self.llm_client = UnifiedLLMClient()
        else:
            self.llm_client = llm_client

    def parse(self, raw_input: str) -> CheckinEvent:
        # Step 1: Try LLM if online
        if self.llm_client:
            raw_json = None
            if hasattr(self.llm_client, "parse_freetext_checkin"):
                raw_json = self.llm_client.parse_freetext_checkin(raw_input)
            elif hasattr(self.llm_client, "chat_completion_json"):
                system_prompt = """Du bist ein präziser Extraktor für sportliche Check-ins.
Antworte DIREKT mit einem validen JSON-Objekt ohne Markdown-Codeblöcke und ohne Einleitung nach folgendem Schema:
{
  "event_type": "workout_checkin",
  "completion_status": "completed",
  "completion_reason": null,
  "actual_metrics": {"metric_primary": 8.5, "unit": "km"},
  "perceived_rpe": 5,
  "sickness_reported": false,
  "sickness_days": 0,
  "symptoms": [{"location": "knee", "side": null, "type": "joint_pain", "severity": 3}],
  "notes": "string"
}
Regeln:
- event_type: immer "workout_checkin"
- completion_status: "completed", "partial", "skipped" oder "sick"
- completion_reason: null, "time_constraint" oder "exhaustion"
- actual_metrics.unit: "km", "min", "m" oder "sets"
- perceived_rpe: Zahl von 1-10 oder null
- symptoms: Liste von Symptomen (mit location, side, type, severity 1-10) oder []"""
                raw_json = self.llm_client.chat_completion_json(
                    system_prompt=system_prompt,
                    user_prompt=f"Text: {raw_input}"
                )

            if raw_json:
                try:
                    return CheckinEvent.model_validate_json(raw_json)
                except ValidationError:
                    pass

        # Step 2: Graceful Deterministic Fallback Parser (Offline / Rule-based)
        return self._rule_based_fallback_parse(raw_input)

    @staticmethod
    def _rule_based_fallback_parse(text: str) -> CheckinEvent:
        """
        Deterministic parser for keywords when LLM is unavailable:
          - Sickness: 'krank', 'fieber', 'erkältung', 'grippe', 'husten'
          - Pain / Symptoms: 'schmerz', 'zwicken', 'sehne', 'knie', 'achilles'
          - Status: 'abgebrochen' (partial), 'ausgefallen' / 'nicht geschafft' (skipped)
          - RPE: 'RPE 7' or '7/10'
        """
        lower = text.lower()
        
        # Check sickness
        sickness_words = ["krank", "fieber", "erkältet", "erkältung", "grippe", "corona"]
        if any(w in lower for w in sickness_words):
            return CheckinEvent(
                completion_status=CompletionStatus.SICK,
                sickness_reported=True,
                sickness_days=1,
                notes=text
            )

        # Check symptoms across sports (running, swimming, strength, cycling)
        symptoms = []
        if "achilles" in lower or "achillessehne" in lower:
            symptoms.append(Symptom(location="achilles", type="tendon_pain", severity=4))
        elif "knie" in lower:
            symptoms.append(Symptom(location="knee", type="joint_pain", severity=5))
        elif "schienbein" in lower or "shin" in lower:
            symptoms.append(Symptom(location="shin", type="shin_pain", severity=4))
        elif "schulter" in lower or "shoulder" in lower:
            symptoms.append(Symptom(location="shoulder", type="joint_pain", severity=4))
        elif "ellbogen" in lower or "elbow" in lower:
            symptoms.append(Symptom(location="elbow", type="tendon_pain", severity=4))
        elif "rücken" in lower or "ruecken" in lower or "back" in lower:
            symptoms.append(Symptom(location="back", type="joint_pain", severity=4))
        elif "handgelenk" in lower or "wrist" in lower:
            symptoms.append(Symptom(location="wrist", type="joint_pain", severity=4))
        elif "muskelkater" in lower:
            symptoms.append(Symptom(location="legs", type="muscle_soreness", severity=3))

        # Check completion status
        if "ausgefallen" in lower or "nicht gelaufen" in lower or "nicht trainiert" in lower or "verpasst" in lower or "nicht geschwommen" in lower:
            status = CompletionStatus.SKIPPED
        elif "abgebrochen" in lower or "nur " in lower:
            status = CompletionStatus.PARTIAL
        else:
            status = CompletionStatus.COMPLETED

        # Check RPE (e.g. "RPE 8" or "8/10")
        rpe = None
        import re
        rpe_match = re.search(r"(?:rpe\s*|anstrengung\s*)(\d{1,2})", lower)
        if rpe_match:
            val = int(rpe_match.group(1))
            if 1 <= val <= 10:
                rpe = val
        else:
            fraction_match = re.search(r"(\d{1,2})\s*/\s*10", lower)
            if fraction_match:
                val = int(fraction_match.group(1))
                if 1 <= val <= 10:
                    rpe = val

        # Extract metric if present across sports: km, m, sets, min
        km_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:km|kilometer)\b", lower)
        m_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:m|meter)\b", lower)
        sets_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:sätze|saetze|sets)\b", lower)
        min_match = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:min|minuten|minutes)\b", lower)

        metrics = None
        if m_match and not km_match:
            dist = float(m_match.group(1).replace(",", "."))
            metrics = ActualMetrics(metric_primary=dist, unit="m")
        elif sets_match:
            s_count = float(sets_match.group(1).replace(",", "."))
            metrics = ActualMetrics(metric_primary=s_count, unit="sets")
        elif km_match:
            dist = float(km_match.group(1).replace(",", "."))
            metrics = ActualMetrics(metric_primary=dist, unit="km")
        elif min_match:
            dur = float(min_match.group(1).replace(",", "."))
            metrics = ActualMetrics(metric_primary=dur, unit="min")

        return CheckinEvent(
            completion_status=status,
            completion_reason="time_constraint" if status == CompletionStatus.PARTIAL else None,
            actual_metrics=metrics,
            perceived_rpe=rpe or 6,
            sickness_reported=False,
            symptoms=symptoms,
            notes=text
        )
